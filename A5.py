#!/usr/bin/env python3
import json
import queue
import time
from multiprocessing import Process, Manager
from typing import Optional
import os
import requests
from communication.android import AndroidLink, AndroidMessage
from communication.stm32 import STMLink
from consts import SYMBOL_MAP
from logger import setup_logger
from settings import API_IP, API_PORT


class RobotAction:
    def __init__(self, action_type, data):
        self._type = action_type
        self._data = data

    @property
    def type(self):
        return self._type

    @property
    def data(self):
        return self._data


class RobotController:
    def __init__(self):
        self.logger = setup_logger()
        self.android_connection = AndroidLink()
        self.stm_connection = STMLink()

        self.manager = Manager()

        self.android_disconnected = self.manager.Event()
        self.start_movement = self.manager.Event()

        self.motion_lock = self.manager.Lock()

        self.android_message_queue = self.manager.Queue()
        self.robot_action_queue = self.manager.Queue()
        self.instruction_queue = self.manager.Queue()
        self.coordinate_queue = self.manager.Queue()

        self.android_receiver = None
        self.stm_receiver = None
        self.android_transmitter = None
        self.instruction_executor = None
        self.action_processor = None
        self.gyro_reset = False
        self.completed_targets = self.manager.list()
        self.missed_targets = self.manager.list()
        self.targets = self.manager.dict()
        self.robot_position = self.manager.dict()
        self.retry_mode = False

    def initialize(self):
        try:
            self.android_connection.connect()
            self.android_message_queue.put(
                AndroidMessage("info", "Connection to robot established!")
            )
            self.stm_connection.connect()
            self.verify_api_status()

            self.android_receiver = Process(target=self.receive_android_messages)
            self.stm_receiver = Process(target=self.receive_stm_messages)
            self.android_transmitter = Process(target=self.send_android_messages)
            self.instruction_executor = Process(target=self.execute_instructions)
            self.action_processor = Process(target=self.process_actions)

            self.android_receiver.start()
            self.stm_receiver.start()
            self.android_transmitter.start()
            self.instruction_executor.start()
            self.action_processor.start()

            self.logger.info("All processes initiated")

            self.android_message_queue.put(AndroidMessage("info", "Robot initialized!"))
            self.android_message_queue.put(AndroidMessage("mode", "path"))
            self.handle_android_reconnection()

        except KeyboardInterrupt:
            self.shutdown()

    def shutdown(self):
        self.android_connection.disconnect()
        self.stm_connection.disconnect()
        self.logger.info("System shutdown complete")

    def handle_android_reconnection(self):
        self.logger.info("Android reconnection monitor active")

        while True:
            self.android_disconnected.wait()

            self.logger.error("Android connection lost")

            self.logger.debug("Terminating Android processes")
            self.android_transmitter.terminate()
            self.android_receiver.terminate()

            self.android_transmitter.join()
            self.android_receiver.join()
            assert not self.android_transmitter.is_alive()
            assert not self.android_receiver.is_alive()
            self.logger.debug("Android processes terminated")

            self.android_connection.disconnect()
            self.android_connection.connect()

            self.android_receiver = Process(target=self.receive_android_messages)
            self.android_transmitter = Process(target=self.send_android_messages)

            self.android_receiver.start()
            self.android_transmitter.start()

            self.logger.info("Android processes restarted")
            self.android_message_queue.put(
                AndroidMessage("info", "Reconnection successful!")
            )
            self.android_message_queue.put(AndroidMessage("mode", "path"))

            self.android_disconnected.clear()

    def receive_android_messages(self):
        while True:
            msg_str: Optional[str] = None
            try:
                msg_str = self.android_connection.recv()
            except OSError:
                self.android_disconnected.set()
                self.logger.debug("Android connection lost")

            if msg_str is None:
                continue

            message: dict = json.loads(msg_str)

            if message["cat"] == "obstacles":
                self.robot_action_queue.put(RobotAction(**message))
                self.logger.debug(f"New obstacle RobotAction queued: {message}")

            elif message["cat"] == "control" and message["value"] == "start":
                if not self.verify_api_status():
                    self.logger.error("API unavailable. Start command aborted.")
                    self.android_message_queue.put(
                        AndroidMessage("error", "API unavailable, start aborted.")
                    )

                if not self.instruction_queue.empty():
                    self.logger.info("Resetting gyro")
                    self.stm_connection.send("RS00")
                    self.start_movement.set()
                    self.logger.info("Initiating robot movement")
                    self.android_message_queue.put(
                        AndroidMessage("info", "Robot movement initiated!")
                    )
                    self.android_message_queue.put(AndroidMessage("status", "running"))
                else:
                    self.logger.warning(
                        "No instructions in queue. Set obstacles first."
                    )
                    self.android_message_queue.put(
                        AndroidMessage("error", "No instructions. Set obstacles first.")
                    )

    def receive_stm_messages(self):
        while True:
            message: str = self.stm_connection.recv()

            if message.startswith("ACK"):
                if not self.gyro_reset:
                    self.gyro_reset = True
                    self.logger.debug("ACK for RS00 from STM32 received.")
                    continue
                try:
                    self.motion_lock.release()
                    try:
                        self.retry_lock.release()
                    except:
                        pass
                    self.logger.debug("ACK from STM32 received, motion lock released.")

                    current_location = self.coordinate_queue.get_nowait()

                    self.robot_position["x"] = current_location["x"]
                    self.robot_position["y"] = current_location["y"]
                    self.robot_position["d"] = current_location["d"]
                    self.logger.info(f"Robot position: {self.robot_position}")
                    self.android_message_queue.put(
                        AndroidMessage(
                            "location",
                            {
                                "x": current_location["x"],
                                "y": current_location["y"],
                                "d": current_location["d"],
                            },
                        )
                    )

                except Exception:
                    self.logger.warning("Tried to release an unlocked lock!")
            else:
                self.logger.warning(f"Unknown message from STM: {message}")

    def send_android_messages(self):
        while True:
            try:
                message: AndroidMessage = self.android_message_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                self.android_connection.send(message)
            except OSError:
                self.android_disconnected.set()
                self.logger.debug("Android connection lost")

    def execute_instructions(self):
        while True:
            instruction: str = self.instruction_queue.get()
            self.logger.debug("Waiting for start signal")
            try:
                self.logger.debug("Waiting for retry lock")
                self.retry_lock.acquire()
                self.retry_lock.release()
            except:
                self.logger.debug("Waiting for start signal")
                self.start_movement.wait()
            self.logger.debug("Waiting for motion lock")
            self.motion_lock.acquire()

            stm32_prefixes = (
                "FS",
                "BS",
                "FW",
                "BW",
                "FL",
                "FR",
                "BL",
                "BR",
                "TL",
                "TR",
                "A",
                "C",
                "DT",
                "STOP",
                "ZZ",
                "RS",
            )
            if instruction.startswith(stm32_prefixes):
                self.stm_connection.send(instruction)
                self.logger.debug(f"Sending to STM32: {instruction}")

            elif instruction.startswith("SNAP"):
                target_id_with_signal = instruction.replace("SNAP", "")
                self.robot_action_queue.put(
                    RobotAction(action_type="snap", data=target_id_with_signal)
                )

            elif instruction == "FIN":
                self.logger.info(f"End of path, missed targets: {self.missed_targets}")
                self.logger.info(f"End of path, robot position: {self.robot_position}")
                if len(self.missed_targets) != 0 and not self.retry_mode:
                    new_target_list = list(self.missed_targets)
                    for i in list(self.completed_targets):
                        i["d"] = 8
                        new_target_list.append(i)

                    self.logger.info("Attempting to revisit missed targets")
                    self.retry_mode = True
                    self.request_algo(
                        {"obstacles": new_target_list, "mode": "0"},
                        self.robot_position["x"],
                        self.robot_position["y"],
                        self.robot_position["d"],
                        retrying=True,
                    )
                    self.retry_lock = self.manager.Lock()
                    self.motion_lock.release()
                    continue

                self.start_movement.clear()
                self.motion_lock.release()
                self.logger.info("Instruction queue finished.")
                self.android_message_queue.put(
                    AndroidMessage("info", "Instruction queue finished.")
                )
                self.android_message_queue.put(AndroidMessage("status", "finished"))
                self.robot_action_queue.put(RobotAction(action_type="stitch", data=""))
            else:
                raise Exception(f"Unknown instruction: {instruction}")

    def process_actions(self):
        while True:
            action: RobotAction = self.robot_action_queue.get()
            self.logger.debug(f"RobotAction retrieved: {action.type} {action.data}")

            if action.type == "obstacles":
                for target in action.data["obstacles"]:
                    self.targets[target["id"]] = target
                self.request_algo(action.data)
            elif action.type == "snap":
                self.capture_image_and_recognize(target_id_with_signal=action.data)
            elif action.type == "stitch":
                self.request_stitch()

    def capture_image_and_recognize(self, target_id_with_signal: str) -> None:
        target_id, signal = target_id_with_signal.split("_")
        self.logger.info(f"Capturing image for target id: {target_id}")
        self.android_message_queue.put(
            AndroidMessage("info", f"Capturing image for target id: {target_id}")
        )
        url = f"http://{API_IP}:{API_PORT}/image"
        filename = f"{int(time.time())}_{target_id}_{signal}.jpg"

        config_file = "PiLCConfig9.txt"
        Home_Files = []
        Home_Files.append(os.getlogin())
        config_file = "/home/" + Home_Files[0] + "/" + config_file

        extns = ["jpg", "png", "bmp", "rgb", "yuv420", "raw"]
        shutters = [
            -2000,
            -1600,
            -1250,
            -1000,
            -800,
            -640,
            -500,
            -400,
            -320,
            -288,
            -250,
            -240,
            -200,
            -160,
            -144,
            -125,
            -120,
            -100,
            -96,
            -80,
            -60,
            -50,
            -48,
            -40,
            -30,
            -25,
            -20,
            -15,
            -13,
            -10,
            -8,
            -6,
            -5,
            -4,
            -3,
            0.4,
            0.5,
            0.6,
            0.8,
            1,
            1.1,
            1.2,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            9,
            10,
            11,
            15,
            20,
            25,
            30,
            40,
            50,
            60,
            75,
            100,
            112,
            120,
            150,
            200,
            220,
            230,
            239,
            435,
        ]
        meters = ["centre", "spot", "average"]
        awbs = [
            "off",
            "auto",
            "incandescent",
            "tungsten",
            "fluorescent",
            "indoor",
            "daylight",
            "cloudy",
        ]
        denoises = ["off", "cdn_off", "cdn_fast", "cdn_hq"]

        config = []
        with open(config_file, "r") as file:
            line = file.readline()
            while line:
                config.append(line.strip())
                line = file.readline()
            config = list(map(int, config))
        mode = config[0]
        speed = config[1]
        gain = config[2]
        brightness = config[3]
        contrast = config[4]
        red = config[6]
        blue = config[7]
        ev = config[8]
        extn = config[15]
        saturation = config[19]
        meter = config[20]
        awb = config[21]
        sharpness = config[22]
        denoise = config[23]
        quality = config[24]

        retry_count = 0

        while True:
            retry_count += 1

            shutter = shutters[speed]
            if shutter < 0:
                shutter = abs(1 / shutter)
            sspeed = int(shutter * 1000000)
            if (shutter * 1000000) - int(shutter * 1000000) > 0.5:
                sspeed += 1

            rpistr = "libcamera-still -e " + extns[extn] + " -n -t 500 -o " + filename
            rpistr += (
                " --brightness "
                + str(brightness / 100)
                + " --contrast "
                + str(contrast / 100)
            )
            rpistr += " --shutter " + str(sspeed)
            if ev != 0:
                rpistr += " --ev " + str(ev)
            if sspeed > 1000000 and mode == 0:
                rpistr += " --gain " + str(gain) + " --immediate "
            else:
                rpistr += " --gain " + str(gain)
                if awb == 0:
                    rpistr += " --awbgains " + str(red / 10) + "," + str(blue / 10)
                else:
                    rpistr += " --awb " + awbs[awb]
            rpistr += " --metering " + meters[meter]
            rpistr += " --saturation " + str(saturation / 10)
            rpistr += " --sharpness " + str(sharpness / 10)
            rpistr += " --quality " + str(quality)
            rpistr += " --denoise " + denoises[denoise]
            rpistr += " --metadata - --metadata-format txt >> PiLibtext.txt"

            os.system(rpistr)

            self.logger.debug("Requesting from image API")

            response = requests.post(
                url, files={"file": (filename, open(filename, "rb"))}
            )

            if response.status_code != 200:
                self.logger.error(
                    "Something went wrong when requesting path from image-rec API. Please try again."
                )
                return

            results = json.loads(response.content)

            if results["image_id"] != "NA" or retry_count > 6:
                break
            elif retry_count > 3:
                self.logger.info(f"Image recognition results: {results}")
                self.logger.info("Recapturing with lower shutter speed...")
                speed -= 1
            elif retry_count <= 3:
                self.logger.info(f"Image recognition results: {results}")
                self.logger.info("Recapturing with higher shutter speed...")
                speed += 1

        self.motion_lock.release()
        try:
            self.retry_lock.release()
        except:
            pass

        self.logger.info(f"Results: {results}")
        self.logger.info(f"Targets: {self.targets}")
        self.logger.info(
            f"Image recognition results: {results} ({SYMBOL_MAP.get(results['image_id'])})"
        )

        if results["image_id"] == "NA":
            self.missed_targets.append(self.targets[int(results["obstacle_id"])])
            self.logger.info(
                f"Added target {results['obstacle_id']} to missed targets."
            )
            self.logger.info(f"Missed targets: {self.missed_targets}")
        else:
            self.completed_targets.append(self.targets[int(results["obstacle_id"])])
            self.logger.info(f"Completed targets: {self.completed_targets}")
        self.android_message_queue.put(AndroidMessage("image-rec", results))

    def request_algo(self, data, robot_x=1, robot_y=1, robot_dir=0, retrying=False):
        self.logger.info("Requesting path from algo...")
        self.android_message_queue.put(
            AndroidMessage("info", "Requesting path from algo...")
        )
        self.logger.info(f"Data: {data}")
        body = {
            **data,
            "big_turn": "0",
            "robot_x": robot_x,
            "robot_y": robot_y,
            "robot_dir": robot_dir,
            "retrying": retrying,
        }
        url = f"http://{API_IP}:{API_PORT}/path"
        response = requests.post(url, json=body)

        if response.status_code != 200:
            self.android_message_queue.put(
                AndroidMessage(
                    "error", "Something went wrong when requesting path from Algo API."
                )
            )
            self.logger.error(
                "Something went wrong when requesting path from Algo API."
            )
            return

        result = json.loads(response.content)["data"]
        instructions = result["commands"]
        path = result["path"]

        self.logger.debug(f"Instructions received from API: {instructions}")

        self.clear_queues()
        for i in instructions:
            self.instruction_queue.put(i)
        for p in path[1:]:
            self.coordinate_queue.put(p)

        self.android_message_queue.put(
            AndroidMessage(
                "info",
                "Instructions and path received from Algo API. Robot is ready to move.",
            )
        )
        self.logger.info(
            "Instructions and path received from Algo API. Robot is ready to move."
        )

    def request_stitch(self):
        url = f"http://{API_IP}:{API_PORT}/stitch"
        response = requests.get(url)

        if response.status_code != 200:
            self.android_message_queue.put(
                AndroidMessage(
                    "error", "Something went wrong when requesting stitch from the API."
                )
            )
            self.logger.error(
                "Something went wrong when requesting stitch from the API."
            )
            return

        self.logger.info("Images stitched!")
        self.android_message_queue.put(AndroidMessage("info", "Images stitched!"))

    def clear_queues(self):
        while not self.instruction_queue.empty():
            self.instruction_queue.get()
        while not self.coordinate_queue.empty():
            self.coordinate_queue.get()

    def verify_api_status(self) -> bool:
        url = f"http://{API_IP}:{API_PORT}/status"
        try:
            response = requests.get(url, timeout=1)
            if response.status_code == 200:
                self.logger.debug("API is up!")
                return True
            return False
        except ConnectionError:
            self.logger.warning("API Connection Error")
            return False
        except requests.Timeout:
            self.logger.warning("API Timeout")
            return False
        except Exception as e:
            self.logger.warning(f"API Exception: {e}")
            return False


if __name__ == "__main__":
    robot = RobotController()
    robot.initialize()
