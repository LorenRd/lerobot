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
Processor step to map Quest VR controller actions to robot end-effector targets.

Converts the Quest teleoperator's output (calibrated position, rotation, grip,
enabled flag) into the standard robot action format consumed by the IK pipeline
(EEReferenceAndDelta → EEBoundsAndSafety → InverseKinematicsEEToJoints).
"""

from dataclasses import dataclass, field

from lerobot.configs.types import FeatureType, PipelineFeatureType, PolicyFeature
from lerobot.processor import ProcessorStepRegistry, RobotAction, RobotActionProcessorStep
from lerobot.teleoperators.quest.coordinate_transform import (
    vr_to_robot_position,
    vr_to_robot_rotvec,
)


@ProcessorStepRegistry.register("map_quest_action_to_robot_action")
@dataclass
class MapQuestActionToRobotAction(RobotActionProcessorStep):
    """
    Maps Quest VR controller teleoperator output to standardized robot action inputs.

    This processor step bridges the Quest teleoperator's 6DOF output and the
    robot's expected action format. It converts the controller's calibrated pose
    (already in delta/relative form from the teleoperator) into target end-effector
    positions and orientations, applying VR→Robot coordinate frame transformation
    and configurable scaling.

    Attributes:
        position_scale: Factor to scale position deltas (VR meters → robot workspace).
        rotation_scale: Factor to scale rotation deltas.
        noise_threshold: Magnitude below which position deltas are treated as noise.
        apply_coordinate_transform: Whether to apply VR→Robot frame conversion.
        gripper_threshold: Grip trigger threshold for binary gripper control.
    """

    position_scale: float = 1.0
    rotation_scale: float = 1.0
    noise_threshold: float = 0.001
    apply_coordinate_transform: bool = True
    gripper_threshold: float = 0.5

    _enabled_prev: bool = field(default=False, init=False, repr=False)

    def action(self, action: RobotAction) -> RobotAction:
        """
        Process Quest action dict into robot target action dict.

        Input keys: quest.pos, quest.rot, quest.grip, quest.enabled, quest.buttons
        Output keys: enabled, target_x/y/z, target_wx/wy/wz, gripper_vel
        """
        enabled = bool(action.pop("quest.enabled"))
        pos = action.pop("quest.pos")
        rot = action.pop("quest.rot")
        grip = float(action.pop("quest.grip"))
        buttons = action.pop("quest.buttons", {})

        if pos is None or rot is None:
            action["enabled"] = False
            action["target_x"] = 0.0
            action["target_y"] = 0.0
            action["target_z"] = 0.0
            action["target_wx"] = 0.0
            action["target_wy"] = 0.0
            action["target_wz"] = 0.0
            action["gripper_vel"] = 0.0
            return action

        # Get rotation vector from Rotation object
        rotvec = rot.as_rotvec()

        # Apply VR → Robot coordinate frame transform
        if self.apply_coordinate_transform:
            pos = vr_to_robot_position(pos)
            rotvec = vr_to_robot_rotvec(rotvec)

        # Apply scaling
        pos = pos * self.position_scale
        rotvec = rotvec * self.rotation_scale

        # Gripper: convert grip trigger to velocity-like signal
        # grip > threshold → closing (negative vel), else → opening (positive vel)
        if grip > self.gripper_threshold:
            gripper_vel = -1.0  # Close
        else:
            gripper_vel = 1.0   # Open

        # Set output action
        action["enabled"] = enabled
        action["target_x"] = float(pos[0]) if enabled else 0.0
        action["target_y"] = float(pos[1]) if enabled else 0.0
        action["target_z"] = float(pos[2]) if enabled else 0.0
        action["target_wx"] = float(rotvec[0]) if enabled else 0.0
        action["target_wy"] = float(rotvec[1]) if enabled else 0.0
        action["target_wz"] = float(rotvec[2]) if enabled else 0.0
        action["gripper_vel"] = gripper_vel

        self._enabled_prev = enabled
        return action

    def transform_features(
        self, features: dict[PipelineFeatureType, dict[str, PolicyFeature]]
    ) -> dict[PipelineFeatureType, dict[str, PolicyFeature]]:
        # Remove Quest-specific features
        for feat in ["enabled", "pos", "rot", "grip", "buttons"]:
            features[PipelineFeatureType.ACTION].pop(f"quest.{feat}", None)

        # Add standard robot action features
        for feat in [
            "enabled",
            "target_x",
            "target_y",
            "target_z",
            "target_wx",
            "target_wy",
            "target_wz",
            "gripper_vel",
        ]:
            features[PipelineFeatureType.ACTION][feat] = PolicyFeature(
                type=FeatureType.ACTION, shape=(1,)
            )

        return features
