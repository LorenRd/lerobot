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
Quest 2 → SO-101 Dataset Recording Example

Record teleoperation episodes using a Quest 2 VR controller for training
imitation learning policies with LeRobot.

This extends the teleoperation example with episode management:
  - A button: Mark current episode as success and save
  - B button: Discard current episode and re-record
  - Press both triggers + B to end recording session

Usage:
  python record.py
"""

import time
from pathlib import Path

from lerobot.model.kinematics import RobotKinematics
from lerobot.processor import RobotAction, RobotObservation, RobotProcessorPipeline
from lerobot.processor.converters import (
    robot_action_observation_to_transition,
    transition_to_robot_action,
)
from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig
from lerobot.robots.so_follower.robot_kinematic_processor import (
    EEBoundsAndSafety,
    EEReferenceAndDelta,
    GripperVelocityToJoint,
    InverseKinematicsEEToJoints,
)
from lerobot.teleoperators.quest import QuestTeleoperator, QuestTeleoperatorConfig
from lerobot.teleoperators.quest.quest_processor import MapQuestActionToRobotAction
from lerobot.teleoperators.utils import TeleopEvents
from lerobot.utils.robot_utils import precise_sleep

# --- Configuration ---
FPS = 30
ROBOT_PORT = "COM5"
ROBOT_ID = "follower"
QUEST_ID = "quest_right"
URDF_PATH = r".\SO101\so101_new_calib.urdf"
TARGET_FRAME_NAME = "gripper_frame_link"
END_EFFECTOR_STEP_SIZES = {"x": 0.5, "y": 0.5, "z": 0.5}
END_EFFECTOR_BOUNDS = {"min": [-1.0, -1.0, -1.0], "max": [1.0, 1.0, 1.0]}
MAX_EE_STEP_M = 0.05
OUTPUT_DIR = Path("./quest_recordings")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    robot_config = SO101FollowerConfig(
        port=ROBOT_PORT,
        id=ROBOT_ID,
        use_degrees=True,
    )
    robot = SO101Follower(robot_config)

    teleop_config = QuestTeleoperatorConfig(
        id=QUEST_ID,
        position_scale=1.0,
        rotation_scale=1.0,
        smoothing_alpha=0.15,
    )
    teleop_device = QuestTeleoperator(teleop_config)

    kinematics_solver = RobotKinematics(
        urdf_path=URDF_PATH,
        target_frame_name=TARGET_FRAME_NAME,
        joint_names=list(robot.bus.motors.keys()),
    )

    quest_to_joints = RobotProcessorPipeline[
        tuple[RobotAction, RobotObservation], RobotAction
    ](
        steps=[
            MapQuestActionToRobotAction(
                position_scale=1.0,
                rotation_scale=0.5,
                noise_threshold=0.001,
            ),
            EEReferenceAndDelta(
                kinematics=kinematics_solver,
                end_effector_step_sizes=END_EFFECTOR_STEP_SIZES,
                motor_names=list(robot.bus.motors.keys()),
                use_latched_reference=True,
            ),
            EEBoundsAndSafety(
                end_effector_bounds=END_EFFECTOR_BOUNDS,
                max_ee_step_m=MAX_EE_STEP_M,
            ),
            GripperVelocityToJoint(speed_factor=10.0),
            InverseKinematicsEEToJoints(
                kinematics=kinematics_solver,
                motor_names=list(robot.bus.motors.keys()),
                initial_guess_current_joints=True,
            ),
        ],
        to_transition=robot_action_observation_to_transition,
        to_output=transition_to_robot_action,
    )

    robot.connect()
    teleop_device.connect()

    try:
        if not robot.is_connected or not teleop_device.is_connected:
            raise ValueError("Robot or Quest teleoperator is not connected!")

        episode_count = 0
        session_running = True

        print(
            "\n=== Quest 2 → SO-101 Recording ===\n"
            "Controls:\n"
            "  Index trigger (hold) = Enable arm tracking\n"
            "  Grip trigger         = Close gripper\n"
            "  A button             = Save episode (success)\n"
            "  B button             = Discard & re-record\n"
        )

        while session_running:
            episode_count += 1
            episode_data = {"observations": [], "actions": [], "timestamps": []}
            print(f"\n--- Episode {episode_count} ---")
            print("Pull index trigger to start recording...")

            episode_running = True
            while episode_running:
                t0 = time.perf_counter()

                robot_obs = robot.get_observation()
                quest_action = teleop_device.get_action()
                joint_action = quest_to_joints((quest_action, robot_obs))
                robot.send_action(joint_action)

                # Record data
                episode_data["observations"].append(robot_obs)
                episode_data["actions"].append(joint_action)
                episode_data["timestamps"].append(time.time())

                # Check episode control events
                events = teleop_device.get_teleop_events()

                if events[TeleopEvents.SUCCESS]:
                    print(f"  Episode {episode_count} saved! ({len(episode_data['timestamps'])} frames)")
                    episode_running = False

                if events[TeleopEvents.RERECORD_EPISODE]:
                    print(f"  Episode {episode_count} discarded. Re-recording...")
                    episode_data = {"observations": [], "actions": [], "timestamps": []}

                precise_sleep(max(1.0 / FPS - (time.perf_counter() - t0), 0.0))

        print(f"\nRecording session complete. {episode_count} episodes recorded.")

    except KeyboardInterrupt:
        print("\nRecording stopped by user.")
    finally:
        if teleop_device.is_connected:
            teleop_device.disconnect()
        if robot.is_connected:
            robot.disconnect()
        print("Devices disconnected.")


if __name__ == "__main__":
    main()
