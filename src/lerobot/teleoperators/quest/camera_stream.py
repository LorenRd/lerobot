#!/usr/bin/env python

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
Camera feed streaming to Meta Quest 2 headset.

This module provides functionality to capture frames from a USB camera mounted
on the robot arm and stream them to the Quest 2 headset display as a floating
2D panel (OpenXR quad layer).

The camera capture uses OpenCV and runs in a background thread. The VR display
uses an OpenXR composition layer to render the camera feed as a quad in front
of the user's view.
"""

import logging
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class CameraStreamConfig:
    """Configuration for the camera-to-VR streaming pipeline."""
    # Camera device index or path (e.g., 0 for first USB camera)
    camera_index: int | str = 0
    # Capture resolution
    capture_width: int = 640
    capture_height: int = 480
    # Target FPS for camera capture
    capture_fps: int = 30
    # Position of the virtual display in VR space (meters from user)
    display_distance: float = 1.0
    # Size of the virtual display (meters)
    display_width: float = 0.8
    display_height: float = 0.6


class CameraStream:
    """
    Captures frames from a USB camera and makes them available for VR display.

    The camera capture runs in a background thread. The latest frame can be
    retrieved thread-safely for rendering in an OpenXR quad layer or for
    streaming via other methods.
    """

    def __init__(self, config: CameraStreamConfig | None = None):
        self.config = config or CameraStreamConfig()
        self._capture: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()
        self._latest_frame: np.ndarray | None = None
        self._frame_count = 0

    @property
    def is_connected(self) -> bool:
        return self._capture is not None and self._capture.isOpened()

    def connect(self) -> None:
        """Open the camera and start the capture thread."""
        self._capture = cv2.VideoCapture(self.config.camera_index)
        if not self._capture.isOpened():
            raise RuntimeError(
                f"Failed to open camera at index {self.config.camera_index}. "
                "Check that the USB camera is connected."
            )

        self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.capture_width)
        self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.capture_height)
        self._capture.set(cv2.CAP_PROP_FPS, self.config.capture_fps)

        actual_w = int(self._capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self._capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = self._capture.get(cv2.CAP_PROP_FPS)
        logger.info(f"Camera opened: {actual_w}x{actual_h} @ {actual_fps:.1f} FPS")

        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def _capture_loop(self) -> None:
        """Background thread that continuously captures camera frames."""
        interval = 1.0 / self.config.capture_fps

        while self._running:
            t0 = time.perf_counter()

            if self._capture is not None and self._capture.isOpened():
                ret, frame = self._capture.read()
                if ret:
                    with self._lock:
                        self._latest_frame = frame
                        self._frame_count += 1

            elapsed = time.perf_counter() - t0
            sleep_time = interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def get_latest_frame(self) -> np.ndarray | None:
        """Get the latest camera frame (thread-safe). Returns BGR numpy array or None."""
        with self._lock:
            if self._latest_frame is not None:
                return self._latest_frame.copy()
            return None

    def get_latest_frame_rgb(self) -> np.ndarray | None:
        """Get the latest camera frame in RGB format (for OpenXR/VR rendering)."""
        frame = self.get_latest_frame()
        if frame is not None:
            return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return None

    @property
    def frame_count(self) -> int:
        with self._lock:
            return self._frame_count

    def disconnect(self) -> None:
        """Stop capture and release camera."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None

        if self._capture is not None:
            self._capture.release()
            self._capture = None

        logger.info("Camera stream disconnected.")
