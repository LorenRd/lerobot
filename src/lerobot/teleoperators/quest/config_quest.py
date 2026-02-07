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
