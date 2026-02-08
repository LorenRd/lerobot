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

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..config import TeleoperatorConfig


class ControllerHand(Enum):
    RIGHT = "right"
    LEFT = "left"


@TeleoperatorConfig.register_subclass("quest")
@dataclass
class QuestTeleoperatorConfig(TeleoperatorConfig):
    """Configuration for Meta Quest 2 VR controller teleoperation."""

    # Which controller hand to use
    controller_hand: ControllerHand = ControllerHand.RIGHT

    # Scaling factors for controller motion → robot motion
    position_scale: float = 1.0
    rotation_scale: float = 1.0

    # Noise filtering threshold (meters). Movements below this are ignored.
    noise_threshold: float = 0.001

    # Smoothing factor for exponential moving average (0.0 = no smoothing, 1.0 = max smoothing)
    smoothing_alpha: float = 0.2

    # Gripper trigger threshold (0.0-1.0). Above this = closed.
    gripper_threshold: float = 0.5

    # Clutch trigger threshold (0.0-1.0). Above this = tracking enabled.
    clutch_threshold: float = 0.3

    # OpenXR polling rate in Hz (Quest 2 native refresh: 72/90/120 Hz)
    polling_rate_hz: float = 90.0

    # --- Camera-to-VR Display ---
    # Enable camera feed display in the Quest 2 headset
    enable_camera_display: bool = False
    # Camera backend type: "opencv" for USB webcam, "depthai" for Luxonis OAK-D
    camera_type: str = "opencv"
    # Camera device index or path (opencv) or MxID (depthai). Empty string for auto-detect.
    camera_index: int | str = 0
    # DepthAI device ID (MxID or IP). Only used when camera_type="depthai".
    camera_device_id: str = ""
    # Camera capture resolution
    camera_width: int = 640
    camera_height: int = 480
    # Camera capture FPS
    camera_fps: int = 30
    # VR display panel size in meters
    vr_display_width: float = 0.6
    vr_display_height: float = 0.45
    # Distance of the virtual display from the user's head (meters)
    vr_display_distance: float = 1.0
    # Vertical offset of the display (meters, positive = up)
    vr_display_offset_y: float = -0.2
    # Show side-by-side RGB + depth in VR (requires camera_type="depthai")
    show_depth_in_vr: bool = False
