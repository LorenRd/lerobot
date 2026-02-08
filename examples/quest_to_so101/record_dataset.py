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
Quest 2 → SO-101 Dataset Recording with OAK-D Lite Camera

Records teleoperation episodes using a Quest 2 VR controller and a Luxonis OAK-D Lite
camera (RGB + depth) into LeRobot datasets for training imitation learning policies.

Supports two teleoperation modes:
  - IK mode: Uses inverse kinematics (requires placo, Linux only)
  - Direct mode: Proportional mapping (no IK, works on Windows)

Dataset features recorded:
  - observation.images.wrist_rgb: RGB frames from OAK-D (H, W, 3)
  - observation.images.wrist_depth: Depth maps from OAK-D stereo (H, W, 1)
  - observation.state: Joint positions (6 joints)
  - action: Joint commands (6 joints)

Controls:
  - Index trigger (hold): Enable arm tracking
  - Grip trigger: Close gripper
  - A button: Save episode (success)
  - B button: Discard episode and re-record

Usage:
  # Direct mode (Windows/Linux)
  python record_dataset.py --repo-id my_dataset --robot-port COM5

  # IK mode (Linux only, requires placo)
  python record_dataset.py --repo-id my_dataset --robot-port COM5 --mode ik

  # With custom OAK-D device
  python record_dataset.py --repo-id my_dataset --camera-device-id 18443010211F850E00
"""

import argparse
import time
from pathlib import Path

import cv2
import numpy as np

from lerobot.cameras.depthai import DepthAICamera, DepthAICameraConfig
from lerobot.datasets.lerobot_dataset import LeRobotDataset
from lerobot.datasets.utils import hw_to_dataset_features
from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig
from lerobot.teleoperators.quest import QuestTeleoperator, QuestTeleoperatorConfig
from lerobot.teleoperators.utils import TeleopEvents
from lerobot.utils.constants import ACTION, OBS_STR
from lerobot.utils.robot_utils import precise_sleep

# --- Configuration ---
FPS = 30
ROBOT_PORT = "COM5"
ROBOT_ID = "follower"
QUEST_ID = "quest_right"
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480

# IK mode configuration (Linux only)
URDF_PATH = r".\SO101\so101_new_calib.urdf"
TARGET_FRAME_NAME = "gripper_frame_link"
END_EFFECTOR_STEP_SIZES = {"x": 0.5, "y": 0.5, "z": 0.5}
END_EFFECTOR_BOUNDS = {"min": [-1.0, -1.0, -1.0], "max": [1.0, 1.0, 1.0]}
MAX_EE_STEP_M = 0.05

MOTOR_NAMES = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]


def build_ik_pipeline(robot, kinematics_solver):
    """Build the IK-based processing pipeline (requires placo, Linux only)."""
    from lerobot.processor import RobotAction, RobotObservation, RobotProcessorPipeline
    from lerobot.processor.converters import (
        robot_action_observation_to_transition,
        transition_to_robot_action,
    )
    from lerobot.robots.so_follower.robot_kinematic_processor import (
        EEBoundsAndSafety,
        EEReferenceAndDelta,
        GripperVelocityToJoint,
        InverseKinematicsEEToJoints,
    )
    from lerobot.teleoperators.quest.quest_processor import MapQuestActionToRobotAction

    return RobotProcessorPipeline[
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


def build_direct_pipeline(robot, args):
    """Build direct proportional mapping (no IK needed)."""
    from lerobot.utils.rotation import Rotation

    joint_limits = {
        "shoulder_pan": (-150, 150),
        "shoulder_lift": (-90, 90),
        "elbow_flex": (-90, 90),
        "wrist_flex": (-90, 90),
        "wrist_roll": (-150, 150),
        "gripper": (0, 100),
    }

    state = {
        "home_joints": None,
        "current_joints": None,
        "was_enabled": False,
        "pos_scale": args.pos_scale,
        "rot_scale": args.rot_scale,
    }

    def process(quest_action, robot_obs):
        """Process quest action into joint commands using direct mapping."""
        enabled = quest_action.get("quest.enabled", False)
        pos = quest_action.get("quest.pos", np.zeros(3))
        rot = quest_action.get("quest.rot", Rotation.from_rotvec(np.zeros(3)))
        grip = quest_action.get("quest.grip", 0.0)

        if state["current_joints"] is None:
            state["current_joints"] = {
                m: robot_obs.get(f"{m}.pos", 0.0) for m in MOTOR_NAMES
            }

        if enabled:
            if not state["was_enabled"]:
                state["home_joints"] = {m: state["current_joints"][m] for m in MOTOR_NAMES}

            if state["home_joints"] is not None:
                rotvec = rot.as_rotvec()
                rot_deg = np.degrees(rotvec)

                mapped = {
                    "shoulder_pan": state["home_joints"]["shoulder_pan"] + pos[0] * state["pos_scale"],
                    "shoulder_lift": state["home_joints"]["shoulder_lift"] - pos[1] * state["pos_scale"],
                    "elbow_flex": state["home_joints"]["elbow_flex"] - pos[2] * state["pos_scale"],
                    "wrist_flex": state["home_joints"]["wrist_flex"] + rot_deg[0] * state["rot_scale"],
                    "wrist_roll": state["home_joints"]["wrist_roll"] + rot_deg[2] * state["rot_scale"],
                }

                for motor, value in mapped.items():
                    lo, hi = joint_limits[motor]
                    state["current_joints"][motor] = float(np.clip(value, lo, hi))

        state["was_enabled"] = enabled
        state["current_joints"]["gripper"] = float(grip * 100.0)

        return {f"{m}.pos": float(state["current_joints"][m]) for m in MOTOR_NAMES}

    return process


def create_dataset_features(use_depth: bool, use_videos: bool) -> dict:
    """Create the LeRobot dataset feature dictionary."""
    # Camera features
    cam_features = {"wrist_rgb": (CAMERA_HEIGHT, CAMERA_WIDTH, 3)}
    if use_depth:
        cam_features["wrist_depth"] = (CAMERA_HEIGHT, CAMERA_WIDTH, 1)

    # Joint state features
    joint_features = {m: float for m in MOTOR_NAMES}

    # Build feature dict
    obs_features = hw_to_dataset_features(
        {**cam_features, **joint_features}, OBS_STR, use_video=use_videos
    )
    action_features = hw_to_dataset_features(
        {m: float for m in MOTOR_NAMES}, ACTION, use_video=use_videos
    )

    return {**obs_features, **action_features}


def depth_to_uint8(depth_frame: np.ndarray) -> np.ndarray:
    """Normalize uint16 depth (mm) to uint8 for dataset storage."""
    if depth_frame is None:
        return np.zeros((CAMERA_HEIGHT, CAMERA_WIDTH, 1), dtype=np.uint8)

    if depth_frame.ndim == 2:
        depth_frame = depth_frame[:, :, np.newaxis]

    # Clip to reasonable range (0-10m) and normalize
    depth_clipped = np.clip(depth_frame.astype(np.float32), 0, 10000)
    depth_normalized = (depth_clipped / 10000.0 * 255).astype(np.uint8)
    return depth_normalized


def main():
    parser = argparse.ArgumentParser(description="Quest 2 → SO-101 Dataset Recording with OAK-D Lite")
    parser.add_argument("--repo-id", type=str, required=True, help="Dataset repository ID (e.g., user/my_dataset)")
    parser.add_argument("--robot-port", type=str, default=ROBOT_PORT, help=f"Serial port (default: {ROBOT_PORT})")
    parser.add_argument("--fps", type=int, default=FPS, help=f"Recording FPS (default: {FPS})")
    parser.add_argument("--num-episodes", type=int, default=0, help="Number of episodes (0=unlimited)")
    parser.add_argument("--mode", type=str, default="direct", choices=["direct", "ik"],
                        help="Teleoperation mode: direct (Windows/Linux) or ik (Linux only)")
    parser.add_argument("--camera-device-id", type=str, default="",
                        help="OAK-D device MxID (empty for auto-detect)")
    parser.add_argument("--no-depth", action="store_true", help="Disable depth recording")
    parser.add_argument("--no-videos", action="store_true", help="Store images instead of videos")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Dataset output directory (default: ~/.cache/lerobot)")
    parser.add_argument("--show-vr-preview", action="store_true",
                        help="Show camera feed in Quest VR headset")
    parser.add_argument("--pos-scale", type=float, default=300.0,
                        help="Position scale for direct mode (degrees per meter)")
    parser.add_argument("--rot-scale", type=float, default=0.7,
                        help="Rotation scale for direct mode")
    parser.add_argument("--task", type=str, default="teleoperation",
                        help="Task description for the dataset")
    args = parser.parse_args()

    use_depth = not args.no_depth
    use_videos = not args.no_videos

    # --- Initialize OAK-D Lite Camera ---
    print("Connecting OAK-D Lite camera...")
    camera_config = DepthAICameraConfig(
        device_id=args.camera_device_id,
        fps=args.fps,
        width=CAMERA_WIDTH,
        height=CAMERA_HEIGHT,
        use_depth=use_depth,
    )
    camera = DepthAICamera(camera_config)
    camera.connect()
    print(f"  OAK-D Lite connected: {CAMERA_WIDTH}x{CAMERA_HEIGHT} @ {args.fps} FPS"
          f" (depth={'enabled' if use_depth else 'disabled'})")

    # --- Initialize Robot ---
    print("Connecting SO-101 robot...")
    robot_config = SO101FollowerConfig(
        port=args.robot_port,
        id=ROBOT_ID,
        use_degrees=True,
    )
    robot = SO101Follower(robot_config)
    robot.connect()
    print(f"  Robot connected on {args.robot_port}")

    # --- Initialize Quest Teleoperator ---
    print("Connecting Quest 2...")
    teleop_config = QuestTeleoperatorConfig(
        id=QUEST_ID,
        position_scale=1.0,
        rotation_scale=1.0,
        smoothing_alpha=0.15,
        enable_camera_display=args.show_vr_preview,
        camera_type="depthai" if args.show_vr_preview else "opencv",
        camera_device_id=args.camera_device_id,
        camera_width=CAMERA_WIDTH,
        camera_height=CAMERA_HEIGHT,
        camera_fps=args.fps,
        show_depth_in_vr=use_depth and args.show_vr_preview,
    )
    teleop_device = QuestTeleoperator(teleop_config)
    teleop_device.connect()
    print("  Quest 2 connected")

    # --- Build Processing Pipeline ---
    if args.mode == "ik":
        from lerobot.model.kinematics import RobotKinematics
        kinematics_solver = RobotKinematics(
            urdf_path=URDF_PATH,
            target_frame_name=TARGET_FRAME_NAME,
            joint_names=list(robot.bus.motors.keys()),
        )
        pipeline = build_ik_pipeline(robot, kinematics_solver)
        is_ik = True
    else:
        pipeline = build_direct_pipeline(robot, args)
        is_ik = False

    # --- Create Dataset ---
    print("Creating dataset...")
    features = create_dataset_features(use_depth=use_depth, use_videos=use_videos)
    root = Path(args.output_dir) if args.output_dir else None

    dataset = LeRobotDataset.create(
        repo_id=args.repo_id,
        fps=args.fps,
        features=features,
        root=root,
        robot_type="so101_follower",
        use_videos=use_videos,
        image_writer_threads=4,
    )
    print(f"  Dataset: {args.repo_id} (features: {list(features.keys())})")

    # --- Recording Loop ---
    try:
        if not robot.is_connected or not teleop_device.is_connected:
            raise ValueError("Robot or Quest teleoperator is not connected!")

        episode_count = 0
        max_episodes = args.num_episodes if args.num_episodes > 0 else float("inf")

        print(
            f"\n=== Quest 2 → SO-101 Dataset Recording ({'IK' if is_ik else 'Direct'} Mode) ===\n"
            f"Camera: OAK-D Lite (depth={'on' if use_depth else 'off'})\n"
            f"Dataset: {args.repo_id}\n"
            f"Task: {args.task}\n\n"
            "Controls:\n"
            "  Index trigger (hold) = Enable arm tracking\n"
            "  Grip trigger         = Close gripper\n"
            "  A button             = Save episode (success)\n"
            "  B button             = Discard & re-record\n"
            "  Ctrl+C               = End session\n"
        )

        while episode_count < max_episodes:
            episode_count += 1
            frame_count = 0
            print(f"\n--- Episode {episode_count} ---")
            print("Pull index trigger to start recording...")

            # Reset episode buffer
            dataset.episode_buffer = dataset.create_episode_buffer()
            episode_active = True

            while episode_active:
                t0 = time.perf_counter()

                # 1. Get robot observation (joint positions)
                robot_obs = robot.get_observation()

                # 2. Get camera frames
                rgb_frame = camera.async_read(timeout_ms=500)
                depth_frame = None
                if use_depth:
                    with camera.frame_lock:
                        if camera.latest_depth_frame is not None:
                            depth_frame = camera.latest_depth_frame.copy()

                # 3. Get Quest controller action
                quest_action = teleop_device.get_action()

                # 4. Process through pipeline → joint commands
                if is_ik:
                    from lerobot.processor.converters import (
                        robot_action_observation_to_transition,
                        transition_to_robot_action,
                    )
                    joint_action = pipeline((quest_action, robot_obs))
                else:
                    joint_action = pipeline(quest_action, robot_obs)

                # 5. Send joint commands to robot
                robot.send_action(joint_action)

                # 6. Build and add frame to dataset
                # Extract joint state as array
                state_array = np.array(
                    [robot_obs.get(f"{m}.pos", 0.0) for m in MOTOR_NAMES],
                    dtype=np.float32,
                )
                action_array = np.array(
                    [joint_action.get(f"{m}.pos", 0.0) for m in MOTOR_NAMES],
                    dtype=np.float32,
                )

                frame_data = {
                    "task": args.task,
                    "observation.images.wrist_rgb": rgb_frame,
                    "observation.state": state_array,
                    "action": action_array,
                }

                if use_depth:
                    frame_data["observation.images.wrist_depth"] = depth_to_uint8(depth_frame)

                dataset.add_frame(frame_data)
                frame_count += 1

                # Update VR HUD overlay
                teleop_device.update_hud_status(
                    is_recording=True, episode=episode_count, frame_count=frame_count,
                )

                # 7. Check episode control events
                events = teleop_device.get_teleop_events()

                if events[TeleopEvents.SUCCESS]:
                    print(f"  ✓ Episode {episode_count} saved! ({frame_count} frames)")
                    dataset.save_episode()
                    episode_active = False

                if events[TeleopEvents.RERECORD_EPISODE]:
                    print(f"  ✗ Episode {episode_count} discarded. Re-recording...")
                    # Reset buffer
                    dataset.episode_buffer = dataset.create_episode_buffer()
                    frame_count = 0

                # Status display
                if frame_count % 30 == 0:
                    elapsed_s = frame_count / args.fps
                    print(f"\r  Recording: {frame_count} frames ({elapsed_s:.1f}s)", end="", flush=True)

                # Maintain target FPS
                precise_sleep(max(1.0 / args.fps - (time.perf_counter() - t0), 0.0))

        print(f"\n\nRecording complete. {episode_count} episodes saved to {args.repo_id}")

    except KeyboardInterrupt:
        print("\n\nRecording stopped by user.")
        # Save any in-progress episode
        if dataset.episode_buffer and dataset.episode_buffer.get("size", 0) > 0:
            print("Saving in-progress episode...")
            dataset.save_episode()
    finally:
        # Finalize dataset
        print("Finalizing dataset...")
        dataset.finalize()

        # Disconnect devices
        if camera.is_connected:
            camera.disconnect()
        if teleop_device.is_connected:
            teleop_device.disconnect()
        if robot.is_connected:
            robot.disconnect()

        print(f"Dataset saved to: {dataset.root}")
        print("Devices disconnected. Goodbye!")


if __name__ == "__main__":
    main()
