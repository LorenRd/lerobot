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
Coordinate frame transformations between OpenXR (VR) space and robot workspace.

OpenXR coordinate system:
  - Right-handed
  - Y-up, -Z forward, X right
  - Units: meters

SO-101 robot coordinate system (typical):
  - X forward, Y left, Z up
  - Units: meters

The transform maps VR controller motion into the robot's workspace frame.
"""

import numpy as np

from lerobot.utils.rotation import Rotation


def vr_to_robot_position(vr_pos: np.ndarray) -> np.ndarray:
    """
    Convert a position from OpenXR (Y-up) to robot frame (Z-up).

    OpenXR: X=right, Y=up, Z=backward
    Robot:  X=forward, Y=left, Z=up

    Mapping: robot_x = -vr_z, robot_y = -vr_x, robot_z = vr_y
    """
    return np.array([-vr_pos[2], -vr_pos[0], vr_pos[1]], dtype=np.float64)


def vr_to_robot_rotation(vr_rot: Rotation) -> Rotation:
    """
    Convert a rotation from OpenXR frame to robot frame.

    Applies the same axis remapping as position: the rotation is conjugated
    by the frame change matrix.
    """
    # Frame change rotation: rotates from VR axes to robot axes
    # This is the rotation that maps VR basis vectors to robot basis vectors.
    # VR(x,y,z) → Robot(-z,-x,y) which is a rotation matrix:
    #   [ 0  0 -1]
    #   [-1  0  0]
    #   [ 0  1  0]
    R_frame = np.array([
        [0.0, 0.0, -1.0],
        [-1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
    ])
    frame_rot = Rotation.from_matrix(R_frame)
    # Conjugate: R_robot = R_frame * R_vr * R_frame^-1
    return frame_rot * vr_rot * frame_rot.inv()


def vr_to_robot_rotvec(vr_rotvec: np.ndarray) -> np.ndarray:
    """Convert a rotation vector from VR frame to robot frame."""
    vr_rot = Rotation.from_rotvec(vr_rotvec)
    robot_rot = vr_to_robot_rotation(vr_rot)
    return robot_rot.as_rotvec()
