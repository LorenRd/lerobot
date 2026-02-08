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
Quest 2 → SO-101 Teleoperation Example

Control an SO-101 robot arm using a Meta Quest 2 VR right-hand controller.
The controller's 6DOF pose drives the arm's end-effector via inverse kinematics.

Controls:
  - Index trigger (hold): Enable tracking (clutch). Release to reposition hand.
  - Grip trigger (squeeze): Close gripper. Release to open.
  - A button: Mark success / end episode (for recording)
  - B button: Discard / re-record episode

Prerequisites:
  1. Meta Quest 2 connected via USB (Quest Link enabled)
  2. Oculus app running with OpenXR runtime active (Windows) or Monado (Linux)
  3. SO-101 arm connected via USB serial
  4. Install: pip install lerobot[quest]

Usage:
  python teleoperate.py
  python teleoperate.py --camera    # Enable camera-to-VR display (USB webcam)
  python teleoperate.py --camera --camera-type depthai --show-depth  # OAK-D Lite RGB+depth
"""

import argparse
import time

import cv2

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
from lerobot.utils.robot_utils import precise_sleep

# --- Configuration ---
FPS = 30
ROBOT_PORT = "COM5"  # Change to your serial port (e.g., "/dev/ttyACM0" on Linux)
ROBOT_ID = "follower"
QUEST_ID = "quest_right"

# URDF for SO-101 kinematics (download from https://github.com/TheRobotStudio/SO-ARM100)
URDF_PATH = r".\SO101\so101_new_calib.urdf"
TARGET_FRAME_NAME = "gripper_frame_link"

# End-effector control parameters
END_EFFECTOR_STEP_SIZES = {"x": 0.5, "y": 0.5, "z": 0.5}
END_EFFECTOR_BOUNDS = {"min": [-1.0, -1.0, -1.0], "max": [1.0, 1.0, 1.0]}
MAX_EE_STEP_M = 0.05  # Conservative step limit for safety


def main():
    parser = argparse.ArgumentParser(description="Quest 2 → SO-101 Teleoperation")
    parser.add_argument(
        "--camera", action="store_true",
        help="Enable camera-to-VR display in the Quest 2 headset",
    )
    parser.add_argument(
        "--camera-index", type=int, default=0,
        help="Camera device index for OpenCV backend (default: 0)",
    )
    parser.add_argument(
        "--camera-type", type=str, default="opencv", choices=["opencv", "depthai"],
        help="Camera backend: opencv (USB webcam) or depthai (OAK-D Lite)",
    )
    parser.add_argument(
        "--camera-device-id", type=str, default="",
        help="OAK-D device MxID (empty for auto-detect, depthai only)",
    )
    parser.add_argument(
        "--show-depth", action="store_true",
        help="Show side-by-side RGB+depth in VR (depthai only)",
    )
    parser.add_argument(
        "--recalibrate", action="store_true",
        help="Force fresh 5-pose calibration (ignores saved calibration)",
    )
    parser.add_argument(
        "--monitor-preview", action="store_true",
        help="Show camera feed on desktop monitor (requires --camera)",
    )
    parser.add_argument(
        "--robot-port", type=str, default=ROBOT_PORT,
        help=f"Serial port for the SO-101 arm (default: {ROBOT_PORT})",
    )
    args = parser.parse_args()

    # Initialize robot
    robot_config = SO101FollowerConfig(
        port=args.robot_port,
        id=ROBOT_ID,
        use_degrees=True,
    )
    robot = SO101Follower(robot_config)

    # Initialize Quest teleoperator
    teleop_config = QuestTeleoperatorConfig(
        id=QUEST_ID,
        position_scale=1.0,
        rotation_scale=1.0,
        smoothing_alpha=0.15,
        enable_camera_display=args.camera,
        camera_index=args.camera_index,
        camera_type=args.camera_type,
        camera_device_id=args.camera_device_id,
        show_depth_in_vr=args.show_depth,
    )
    teleop_device = QuestTeleoperator(teleop_config)

    # Set up kinematics solver
    kinematics_solver = RobotKinematics(
        urdf_path=URDF_PATH,
        target_frame_name=TARGET_FRAME_NAME,
        joint_names=list(robot.bus.motors.keys()),
    )

    # Build the processing pipeline: Quest action → EE pose → Joint commands
    quest_to_robot_joints_processor = RobotProcessorPipeline[
        tuple[RobotAction, RobotObservation], RobotAction
    ](
        steps=[
            MapQuestActionToRobotAction(
                position_scale=1.0,
                rotation_scale=0.5,  # Reduce rotation sensitivity
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
            GripperVelocityToJoint(
                speed_factor=10.0,
            ),
            InverseKinematicsEEToJoints(
                kinematics=kinematics_solver,
                motor_names=list(robot.bus.motors.keys()),
                initial_guess_current_joints=True,
            ),
        ],
        to_transition=robot_action_observation_to_transition,
        to_output=transition_to_robot_action,
    )

    # Connect devices
    robot.connect()
    teleop_device.connect(recalibrate=args.recalibrate)

    try:
        if not robot.is_connected or not teleop_device.is_connected:
            raise ValueError("Robot or Quest teleoperator is not connected!")

        print(
            "\n=== Quest 2 → SO-101 Teleoperation ===\n"
            "Controls:\n"
            "  Index trigger (hold) = Enable arm tracking\n"
            "  Grip trigger         = Close gripper\n"
            "  A button             = End episode (success)\n"
            "  B button             = Re-record episode\n"
            "\nStarting teleoperation loop...\n"
        )

        while robot.is_connected and teleop_device.is_connected:
            t0 = time.perf_counter()

            # Get robot observation (current joint positions + cameras)
            robot_obs = robot.get_observation()

            # Get Quest controller action
            quest_action = teleop_device.get_action()

            # Process through pipeline: Quest → EE → IK → Joints
            joint_action = quest_to_robot_joints_processor((quest_action, robot_obs))

            # Send joint commands to robot
            robot.send_action(joint_action)

            # Desktop monitor preview (camera feed with HUD)
            if args.monitor_preview and args.camera:
                monitor_frame = teleop_device.get_monitor_frame()
                if monitor_frame is not None:
                    cv2.imshow("SO-101 Camera", monitor_frame)
                    if cv2.waitKey(1) & 0xFF == 27:  # ESC to quit
                        print("\nESC pressed — exiting.")
                        break

            # Maintain target FPS
            precise_sleep(max(1.0 / FPS - (time.perf_counter() - t0), 0.0))

    except KeyboardInterrupt:
        print("\nTeleoperation stopped by user.")
    finally:
        if args.monitor_preview:
            cv2.destroyAllWindows()
        if teleop_device.is_connected:
            teleop_device.disconnect()
        if robot.is_connected:
            robot.disconnect()
        print("Devices disconnected. Goodbye!")


if __name__ == "__main__":
    main()
