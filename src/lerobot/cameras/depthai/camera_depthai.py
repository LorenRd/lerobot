# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Provides the DepthAICamera class for capturing frames from Luxonis DepthAI cameras
(OAK-D, OAK-D Lite, etc.) using the DepthAI v3 SDK.
"""

import logging
import time
from datetime import timedelta
from threading import Event, Lock, Thread
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

try:
    import depthai as dai
except ImportError:
    dai = None  # type: ignore[assignment]

from lerobot.utils.errors import DeviceAlreadyConnectedError, DeviceNotConnectedError

from ..camera import Camera
from ..configs import ColorMode
from ..utils import get_cv2_rotation
from .configuration_depthai import DepthAICameraConfig

logger = logging.getLogger(__name__)

# Default resolution for OAK-D Lite RGB camera
_DEFAULT_WIDTH = 640
_DEFAULT_HEIGHT = 480
_DEFAULT_FPS = 30

# DepthAI v3 board sockets
_RGB_SOCKET = dai.CameraBoardSocket.CAM_A if dai else None
_LEFT_SOCKET = dai.CameraBoardSocket.CAM_B if dai else None
_RIGHT_SOCKET = dai.CameraBoardSocket.CAM_C if dai else None


class DepthAICamera(Camera):
    """
    Manages interactions with Luxonis DepthAI cameras (OAK-D, OAK-D Lite, etc.).

    This class provides an interface following the LeRobot Camera base class,
    using the DepthAI v3 SDK. It supports both RGB and stereo depth capture
    with synchronized frames via on-device processing.

    Use the provided utility to find available cameras:
    ```bash
    lerobot-find-cameras depthai
    ```

    Example:
        ```python
        from lerobot.cameras.depthai import DepthAICamera, DepthAICameraConfig

        # Basic RGB usage
        config = DepthAICameraConfig(device_id="", fps=30, width=640, height=480)
        camera = DepthAICamera(config)
        camera.connect()
        color_image = camera.read()

        # With depth capture
        config = DepthAICameraConfig(
            device_id="", fps=30, width=640, height=480, use_depth=True
        )
        camera = DepthAICamera(config)
        camera.connect()
        color_image = camera.read()
        depth_map = camera.read_depth()
        camera.disconnect()
        ```
    """

    def __init__(self, config: DepthAICameraConfig):
        if dai is None:
            raise ImportError(
                "DepthAI SDK is not installed. Install it with: pip install depthai>=3.0.0"
            )

        super().__init__(config)

        self.config = config
        self.device_id = config.device_id
        self.color_mode = config.color_mode
        self.use_depth = config.use_depth
        self.warmup_s = config.warmup_s

        self._device: dai.Device | None = None
        self._pipeline: dai.Pipeline | None = None
        self._rgb_queue: Any = None
        self._depth_queue: Any = None

        self.thread: Thread | None = None
        self.stop_event: Event | None = None
        self.frame_lock: Lock = Lock()
        self.latest_color_frame: NDArray[Any] | None = None
        self.latest_depth_frame: NDArray[Any] | None = None
        self.latest_timestamp: float | None = None
        self.new_frame_event: Event = Event()

        self.rotation: int | None = get_cv2_rotation(config.rotation)

        if self.height and self.width:
            self.capture_width, self.capture_height = self.width, self.height
            if self.rotation in [cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE]:
                self.capture_width, self.capture_height = self.height, self.width

    def __str__(self) -> str:
        return f"{self.__class__.__name__}({self.device_id or 'auto'})"

    @property
    def is_connected(self) -> bool:
        """Checks if the DepthAI device is open and pipeline is running."""
        return self._device is not None and not self._device.isClosed()

    def connect(self, warmup: bool = True) -> None:
        """
        Connects to the DepthAI camera specified in the configuration.

        Initializes the DepthAI pipeline with Color camera and optionally StereoDepth
        nodes, starts the device, and validates frame capture.

        Args:
            warmup: If True, waits until at least one valid frame has been captured.

        Raises:
            DeviceAlreadyConnectedError: If the camera is already connected.
            ConnectionError: If the device cannot be found or pipeline fails to start.
        """
        if self.is_connected:
            raise DeviceAlreadyConnectedError(f"{self} is already connected.")

        # In DepthAI v3, the Device is created first, then passed to Pipeline
        device_info = self._find_device_info()
        try:
            if device_info is not None:
                self._device = dai.Device(device_info)
            else:
                self._device = dai.Device()
        except Exception as e:
            raise ConnectionError(
                f"Failed to connect to DepthAI device for {self}. "
                f"Run `lerobot-find-cameras depthai` to find available cameras."
            ) from e

        self._pipeline = dai.Pipeline(self._device)
        self._build_pipeline()

        try:
            self._pipeline.start()
        except Exception as e:
            self._pipeline = None
            self._device = None
            raise ConnectionError(
                f"Failed to start DepthAI pipeline for {self}. "
                f"Run `lerobot-find-cameras depthai` to find available cameras."
            ) from e

        self._configure_capture_settings()
        self._start_read_thread()

        # Wait for first valid frame(s) to arrive
        warmup_timeout = max(self.warmup_s, 2.0)
        start_time = time.time()
        while time.time() - start_time < warmup_timeout:
            with self.frame_lock:
                has_color = self.latest_color_frame is not None
                has_depth = (not self.use_depth) or self.latest_depth_frame is not None
            if has_color and has_depth:
                break
            time.sleep(0.1)

        with self.frame_lock:
            if self.latest_color_frame is None or (self.use_depth and self.latest_depth_frame is None):
                raise ConnectionError(f"{self} failed to capture frames during warmup.")

        logger.info(f"{self} connected.")

    @staticmethod
    def find_cameras() -> list[dict[str, Any]]:
        """
        Detects available Luxonis DepthAI cameras connected to the system.

        Returns:
            list[dict[str, Any]]: A list of dictionaries with device information
            including name, deviceId, state, and available camera sensors.

        Raises:
            ImportError: If depthai is not installed.
        """
        if dai is None:
            raise ImportError("DepthAI SDK is not installed. Install with: pip install depthai>=3.0.0")

        found_cameras_info = []
        infos = dai.Device.getAllAvailableDevices()

        for info in infos:
            state = str(info.state).split("X_LINK_")[-1] if "X_LINK_" in str(info.state) else str(info.state)
            camera_info: dict[str, Any] = {
                "name": info.name,
                "type": "DepthAI",
                "id": info.deviceId,
                "state": state,
            }

            # Try to get sensor information by briefly connecting
            try:
                with dai.Device(info) as device:
                    sensors = device.getCameraSensorNames()
                    camera_info["sensors"] = {str(k): v for k, v in sensors.items()}
                    calib = device.readCalibration()
                    eeprom = calib.getEepromData()
                    camera_info["product_name"] = eeprom.productName
                    camera_info["board_name"] = eeprom.boardName
            except Exception:
                pass

            found_cameras_info.append(camera_info)

        return found_cameras_info

    def _find_device_info(self) -> Any:
        """Finds device info matching the configured device_id."""
        if not self.device_id:
            return None  # Auto-detect (use first available)

        infos = dai.Device.getAllAvailableDevices()
        for info in infos:
            if info.deviceId == self.device_id or info.name == self.device_id:
                return info

        available = [f"{i.name} ({i.deviceId})" for i in infos]
        raise ConnectionError(
            f"DepthAI device '{self.device_id}' not found. Available devices: {available}"
        )

    def _build_pipeline(self) -> None:
        """Builds the DepthAI v3 pipeline with Camera and optional StereoDepth nodes."""
        pipeline = self._pipeline
        if pipeline is None:
            raise RuntimeError("Pipeline not initialized.")

        cap_w = self.capture_width if hasattr(self, "capture_width") else (self.width or _DEFAULT_WIDTH)
        cap_h = self.capture_height if hasattr(self, "capture_height") else (self.height or _DEFAULT_HEIGHT)
        fps = self.fps or _DEFAULT_FPS

        # RGB camera
        cam_rgb = pipeline.create(dai.node.Camera).build(_RGB_SOCKET)
        rgb_out = cam_rgb.requestOutput(
            size=(cap_w, cap_h),
            type=dai.ImgFrame.Type.BGR888i,
            fps=fps,
        )

        if self.use_depth:
            # Mono cameras for stereo depth
            cam_left = pipeline.create(dai.node.Camera).build(_LEFT_SOCKET)
            cam_right = pipeline.create(dai.node.Camera).build(_RIGHT_SOCKET)

            stereo = pipeline.create(dai.node.StereoDepth)
            stereo.setExtendedDisparity(self.config.stereo_extended_disparity)
            stereo.setLeftRightCheck(self.config.stereo_lr_check)
            stereo.setRectification(True)

            left_out = cam_left.requestFullResolutionOutput()
            right_out = cam_right.requestFullResolutionOutput()
            left_out.link(stereo.left)
            right_out.link(stereo.right)

            if self.config.depth_align_to_color:
                # Align depth to the RGB camera frame
                platform = self._device.getPlatform() if self._device else dai.Platform.RVC2
                if platform == dai.Platform.RVC4:
                    align = pipeline.create(dai.node.ImageAlign)
                    stereo.depth.link(align.input)
                    rgb_out.link(align.inputAlignTo)
                    # Use Sync to synchronize RGB and aligned depth
                    sync = pipeline.create(dai.node.Sync)
                    sync.setSyncThreshold(timedelta(seconds=1 / (2 * fps)))
                    rgb_out.link(sync.inputs["rgb"])
                    align.outputAligned.link(sync.inputs["depth"])
                    self._sync_queue = sync.out.createOutputQueue()
                    self._rgb_queue = None
                    self._depth_queue = None
                    return
                else:
                    # RVC2/RVC3: stereo node handles alignment directly
                    rgb_out.link(stereo.inputAlignTo)

            # Use Sync to synchronize RGB and depth
            sync = pipeline.create(dai.node.Sync)
            sync.setSyncThreshold(timedelta(seconds=1 / (2 * fps)))
            rgb_out.link(sync.inputs["rgb"])
            stereo.depth.link(sync.inputs["depth"])
            self._sync_queue = sync.out.createOutputQueue()
            self._rgb_queue = None
            self._depth_queue = None
        else:
            # RGB only
            self._rgb_queue = rgb_out.createOutputQueue()
            self._depth_queue = None
            self._sync_queue = None

    def _configure_capture_settings(self) -> None:
        """Sets fps, width, and height from defaults if not already configured."""
        if self.fps is None:
            self.fps = _DEFAULT_FPS
        if self.width is None or self.height is None:
            self.width = _DEFAULT_WIDTH
            self.height = _DEFAULT_HEIGHT
            self.capture_width = _DEFAULT_WIDTH
            self.capture_height = _DEFAULT_HEIGHT
            if self.rotation in [cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE]:
                self.width, self.height = _DEFAULT_HEIGHT, _DEFAULT_WIDTH

    def read_depth(self, timeout_ms: int = 200) -> NDArray[Any]:
        """
        Reads a single depth frame synchronously from the camera.

        Returns:
            np.ndarray: The depth map as a NumPy array (height, width)
                  of type `np.uint16` (raw depth values in millimeters).

        Raises:
            RuntimeError: If depth stream is not enabled or no frame available.
            DeviceNotConnectedError: If the camera is not connected.
        """
        if not self.use_depth:
            raise RuntimeError(
                f"Depth stream is not enabled for {self}. Set use_depth=True in config."
            )

        if not self.is_connected:
            raise DeviceNotConnectedError(f"{self} is not connected.")

        if self.thread is None or not self.thread.is_alive():
            raise RuntimeError(f"{self} read thread is not running.")

        self.new_frame_event.clear()
        _ = self.async_read(timeout_ms=10000)

        with self.frame_lock:
            depth_map = self.latest_depth_frame

        if depth_map is None:
            raise RuntimeError("No depth frame available. Ensure camera is streaming.")

        return depth_map

    def read(self, color_mode: ColorMode | None = None, timeout_ms: int = 0) -> NDArray[Any]:
        """
        Reads a single color frame synchronously from the camera.

        Returns:
            np.ndarray: The captured color frame as a NumPy array
              (height, width, channels), processed according to color_mode and rotation.

        Raises:
            DeviceNotConnectedError: If the camera is not connected.
            RuntimeError: If reading fails.
        """
        if not self.is_connected:
            raise DeviceNotConnectedError(f"{self} is not connected.")

        if self.thread is None or not self.thread.is_alive():
            raise RuntimeError(f"{self} read thread is not running.")

        self.new_frame_event.clear()
        frame = self.async_read(timeout_ms=10000)

        return frame

    def _postprocess_image(self, image: NDArray[Any], depth_frame: bool = False) -> NDArray[Any]:
        """Applies color conversion and rotation to a raw frame."""
        if depth_frame:
            processed = image
        else:
            if self.color_mode == ColorMode.RGB:
                processed = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            else:
                processed = image  # Already BGR from DepthAI

        if self.rotation in [cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE, cv2.ROTATE_180]:
            processed = cv2.rotate(processed, self.rotation)

        return processed

    def _read_loop(self) -> None:
        """
        Background thread loop for continuous frame capture from DepthAI queues.

        On each iteration:
        1. Gets frames from the appropriate queue(s)
        2. Processes and stores frames (thread-safe)
        3. Signals new_frame_event
        """
        if self.stop_event is None:
            raise RuntimeError(f"{self}: stop_event is not initialized.")

        failure_count = 0
        while not self.stop_event.is_set():
            try:
                if hasattr(self, "_sync_queue") and self._sync_queue is not None:
                    # Synced RGB + depth mode
                    msg_group = self._sync_queue.tryGet()
                    if msg_group is None:
                        time.sleep(0.001)
                        continue

                    rgb_frame = msg_group["rgb"]
                    color_data = rgb_frame.getCvFrame()
                    processed_color = self._postprocess_image(color_data)

                    depth_data = None
                    try:
                        depth_frame = msg_group["depth"]
                        depth_data = depth_frame.getFrame()
                        if depth_data is not None:
                            depth_data = self._postprocess_image(depth_data, depth_frame=True)
                    except (KeyError, RuntimeError):
                        pass

                    capture_time = time.perf_counter()
                    with self.frame_lock:
                        self.latest_color_frame = processed_color
                        if depth_data is not None:
                            self.latest_depth_frame = depth_data
                        self.latest_timestamp = capture_time
                    self.new_frame_event.set()

                elif self._rgb_queue is not None:
                    # RGB-only mode
                    rgb_msg = self._rgb_queue.tryGet()
                    if rgb_msg is None:
                        time.sleep(0.001)
                        continue

                    color_data = rgb_msg.getCvFrame()
                    processed_color = self._postprocess_image(color_data)

                    capture_time = time.perf_counter()
                    with self.frame_lock:
                        self.latest_color_frame = processed_color
                        self.latest_timestamp = capture_time
                    self.new_frame_event.set()

                failure_count = 0

            except DeviceNotConnectedError:
                break
            except Exception as e:
                if failure_count <= 10:
                    failure_count += 1
                    logger.warning(f"Error reading frame in background thread for {self}: {e}")
                else:
                    raise RuntimeError(f"{self} exceeded maximum consecutive read failures.") from e

    def _start_read_thread(self) -> None:
        """Starts or restarts the background read thread."""
        self._stop_read_thread()

        self.stop_event = Event()
        self.thread = Thread(target=self._read_loop, args=(), name=f"{self}_read_loop")
        self.thread.daemon = True
        self.thread.start()

    def _stop_read_thread(self) -> None:
        """Signals the background read thread to stop and waits for it to join."""
        if self.stop_event is not None:
            self.stop_event.set()

        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=2.0)

        self.thread = None
        self.stop_event = None

        with self.frame_lock:
            self.latest_color_frame = None
            self.latest_depth_frame = None
            self.latest_timestamp = None
            self.new_frame_event.clear()

    def async_read(self, timeout_ms: float = 200) -> NDArray[Any]:
        """
        Reads the latest available color frame asynchronously.

        Args:
            timeout_ms: Maximum time in milliseconds to wait for a frame.

        Returns:
            np.ndarray: The latest captured color frame.

        Raises:
            DeviceNotConnectedError: If the camera is not connected.
            TimeoutError: If no frame becomes available within timeout.
        """
        if not self.is_connected:
            raise DeviceNotConnectedError(f"{self} is not connected.")

        if self.thread is None or not self.thread.is_alive():
            raise RuntimeError(f"{self} read thread is not running.")

        if not self.new_frame_event.wait(timeout=timeout_ms / 1000.0):
            raise TimeoutError(
                f"Timed out waiting for frame from {self} after {timeout_ms} ms. "
                f"Read thread alive: {self.thread.is_alive()}."
            )

        with self.frame_lock:
            frame = self.latest_color_frame
            self.new_frame_event.clear()

        if frame is None:
            raise RuntimeError(f"Internal error: Event set but no frame available for {self}.")

        return frame

    def read_latest(self, max_age_ms: int = 1000) -> NDArray[Any]:
        """Return the most recent color frame captured immediately (peeking).

        Returns:
            NDArray[Any]: The frame image (numpy array).

        Raises:
            TimeoutError: If the latest frame is older than max_age_ms.
            DeviceNotConnectedError: If the camera is not connected.
        """
        if not self.is_connected:
            raise DeviceNotConnectedError(f"{self} is not connected.")

        if self.thread is None or not self.thread.is_alive():
            raise RuntimeError(f"{self} read thread is not running.")

        with self.frame_lock:
            frame = self.latest_color_frame
            timestamp = self.latest_timestamp

        if frame is None or timestamp is None:
            raise RuntimeError(f"{self} has not captured any frames yet.")

        age_ms = (time.perf_counter() - timestamp) * 1e3
        if age_ms > max_age_ms:
            raise TimeoutError(
                f"{self} latest frame is too old: {age_ms:.1f} ms (max allowed: {max_age_ms} ms)."
            )

        return frame

    def disconnect(self) -> None:
        """
        Disconnects from the camera and releases all resources.

        Raises:
            DeviceNotConnectedError: If the camera is already disconnected.
        """
        if not self.is_connected and self.thread is None:
            raise DeviceNotConnectedError(
                f"Attempted to disconnect {self}, but it appears already disconnected."
            )

        if self.thread is not None:
            self._stop_read_thread()

        if self._pipeline is not None:
            try:
                self._pipeline.stop()
            except Exception:
                pass
            self._pipeline = None

        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
            self._device = None
        self._rgb_queue = None
        self._depth_queue = None
        if hasattr(self, "_sync_queue"):
            self._sync_queue = None

        with self.frame_lock:
            self.latest_color_frame = None
            self.latest_depth_frame = None
            self.latest_timestamp = None
            self.new_frame_event.clear()

        logger.info(f"{self} disconnected.")
