#!/usr/bin/env python

"""
Quest 2 → SO-101 Direct Teleoperation (no IK required)

Maps the Quest 2 right controller position/rotation directly to SO-101 joints.
Uses simple proportional mapping — no inverse kinematics or URDF needed.

Controls:
  - Index trigger (hold): Enable tracking (clutch). Release to reposition hand.
  - Grip trigger (squeeze): Close gripper. Release to open.
  - A button: Exit program

Mapping (after VR→Robot frame transform):
  - Hand forward/back (robot X) → elbow_flex
  - Hand left/right (robot Y) → shoulder_pan
  - Hand up/down (robot Z) → shoulder_lift
  - Wrist pitch (about robot Y) → wrist_flex
  - Wrist roll (about robot X) → wrist_roll

Usage:
  python teleoperate_direct.py --robot-port COM4
  python teleoperate_direct.py --robot-port COM4 --camera  # USB webcam in VR
  python teleoperate_direct.py --robot-port COM4 --camera --camera-type depthai --show-depth  # OAK-D
"""

import argparse
import logging
import time

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(message)s")


def _rotation_to_euler_xyz(rot) -> np.ndarray:
    """Extract extrinsic XYZ Euler angles (roll, pitch, yaw) from Rotation.

    Returns array [roll_about_X, pitch_about_Y, yaw_about_Z] in radians.
    Unlike rotvec components, these are independent single-axis rotations
    that correctly decompose combined wrist orientations.
    """
    R = rot.as_matrix()
    cy = np.sqrt(R[0, 0] ** 2 + R[1, 0] ** 2)
    if cy > 1e-6:
        roll = np.arctan2(R[2, 1], R[2, 2])
        pitch = np.arctan2(-R[2, 0], cy)
        yaw = np.arctan2(R[1, 0], R[0, 0])
    else:
        # Gimbal lock
        roll = np.arctan2(-R[1, 2], R[1, 1])
        pitch = np.pi / 2 if R[2, 0] < 0 else -np.pi / 2
        yaw = 0.0
    return np.array([roll, pitch, yaw])


def main():
    parser = argparse.ArgumentParser(description="Quest 2 → SO-101 Direct Teleoperation")
    parser.add_argument("--robot-port", type=str, default="COM4", help="Serial port for SO-101")
    parser.add_argument("--pos-scale", type=float, default=300.0,
                        help="Position sensitivity: degrees per meter of hand displacement")
    parser.add_argument("--rot-scale", type=float, default=0.7,
                        help="Rotation sensitivity: multiplier on wrist rotation (0.1-2.0)")
    parser.add_argument("--fps", type=int, default=30, help="Control loop FPS")
    parser.add_argument("--camera", action="store_true",
                        help="Enable camera-to-VR display in Quest headset")
    parser.add_argument("--camera-index", type=int, default=0,
                        help="Camera device index for OpenCV backend (default: 0)")
    parser.add_argument("--camera-type", type=str, default="opencv", choices=["opencv", "depthai"],
                        help="Camera backend: opencv (USB webcam) or depthai (OAK-D Lite)")
    parser.add_argument("--camera-device-id", type=str, default="",
                        help="OAK-D device MxID (empty for auto-detect, depthai only)")
    parser.add_argument("--show-depth", action="store_true",
                        help="Show side-by-side RGB+depth in VR (depthai only)")
    args = parser.parse_args()

    from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig
    from lerobot.teleoperators.quest import QuestTeleoperator, QuestTeleoperatorConfig
    from lerobot.teleoperators.quest.coordinate_transform import (
        vr_to_robot_position,
        vr_to_robot_rotation,
    )
    from lerobot.utils.rotation import Rotation

    # Initialize robot
    robot_config = SO101FollowerConfig(port=args.robot_port, id="follower", use_degrees=True)
    robot = SO101Follower(robot_config)

    # Initialize Quest teleoperator
    teleop_config = QuestTeleoperatorConfig(
        id="quest_right",
        position_scale=1.0,
        rotation_scale=1.0,
        smoothing_alpha=0.2,
        enable_camera_display=args.camera,
        camera_index=args.camera_index,
        camera_type=args.camera_type,
        camera_device_id=args.camera_device_id,
        show_depth_in_vr=args.show_depth,
    )
    teleop = QuestTeleoperator(teleop_config)

    # Connect
    print("Connecting robot...")
    robot.connect()
    print(f"Robot connected. Motors: {list(robot.bus.motors.keys())}")

    print("\nConnecting Quest 2...")
    teleop.connect()
    print("Quest 2 connected!")
    time.sleep(3)  # Wait for OpenXR session state

    # Get initial joint positions
    obs = robot.get_observation()
    motor_names = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]
    current_joints = {m: obs[f"{m}.pos"] for m in motor_names}

    # Joint limits (degrees)
    joint_limits = {
        "shoulder_pan": (-150, 150),
        "shoulder_lift": (-90, 90),
        "elbow_flex": (-90, 90),
        "wrist_flex": (-90, 90),
        "wrist_roll": (-150, 150),
        "gripper": (0, 100),
    }

    print(
        "\n=== Direct Teleoperation ===\n"
        "Controls:\n"
        "  HOLD index trigger  = arm follows your hand\n"
        "  RELEASE trigger     = arm freezes, reposition freely\n"
        "  GRIP trigger        = close gripper\n"
        "  A button            = EXIT\n"
        "\nMove your hand to control the arm!\n"
    )

    pos_scale = args.pos_scale
    rot_scale = args.rot_scale
    interval = 1.0 / args.fps

    # Home joints: recorded when tracking first engages so arm moves relative to its starting pose
    home_joints = None
    was_enabled = False

    try:
        while True:
            t0 = time.perf_counter()

            action = teleop.get_action()
            enabled = action.get("quest.enabled", False)
            pos = action.get("quest.pos", np.zeros(3))
            rot = action.get("quest.rot", Rotation.from_rotvec(np.zeros(3)))
            grip = action.get("quest.grip", 0.0)
            buttons = action.get("quest.buttons", {})
            if buttons.get("a", False):
                print("\nA button pressed — exiting.")
                break

            if enabled:
                # First frame of tracking: snapshot current joints as "home"
                if not was_enabled:
                    home_joints = {m: current_joints[m] for m in motor_names}

                # Transform from VR (Y-up) frame to robot (Z-up) frame
                pos_robot = vr_to_robot_position(pos)
                rot_robot = vr_to_robot_rotation(rot)

                # Extract independent Euler angles in robot frame (radians → degrees)
                # [0]=roll (about X/forward), [1]=pitch (about Y/left), [2]=yaw (about Z/up)
                euler_deg = np.degrees(_rotation_to_euler_xyz(rot_robot))

                # Noise deadzone: ignore sub-2mm hand tremor
                if np.linalg.norm(pos_robot) < 0.002:
                    pos_robot = np.zeros(3)

                # Proportional mapping: joint = home + displacement * scale
                # Robot X (forward/back) → elbow_flex (reach)
                # Robot Y (left/right) → shoulder_pan (sweep)
                # Robot Z (up/down) → shoulder_lift (height)
                mapped = {
                    "shoulder_pan":  home_joints["shoulder_pan"]  - pos_robot[1] * pos_scale,
                    "shoulder_lift": home_joints["shoulder_lift"] - pos_robot[2] * pos_scale,
                    "elbow_flex":    home_joints["elbow_flex"]    + pos_robot[0] * pos_scale,
                    "wrist_flex":    home_joints["wrist_flex"]    + euler_deg[1] * rot_scale,
                    "wrist_roll":    home_joints["wrist_roll"]    + euler_deg[0] * rot_scale,
                }

                # Clamp to limits
                for motor, value in mapped.items():
                    lo, hi = joint_limits[motor]
                    current_joints[motor] = np.clip(value, lo, hi)

            was_enabled = enabled

            # Gripper: map grip trigger to joint position
            current_joints["gripper"] = grip * 100.0

            # Send to robot
            joint_action = {f"{m}.pos": float(current_joints[m]) for m in motor_names}
            robot.send_action(joint_action)

            # Status display
            status = "TRACK" if enabled else "PAUSE"
            j = " ".join(f"{m[:4]}={current_joints[m]:+6.1f}" for m in motor_names[:5])
            print(f"\r  [{status}] {j} grip={current_joints['gripper']:5.1f}", end="", flush=True)

            elapsed = time.perf_counter() - t0
            if elapsed < interval:
                time.sleep(interval - elapsed)

    except KeyboardInterrupt:
        print("\n\nStopped by user.")
    finally:
        teleop.disconnect()
        robot.disconnect()
        print("Devices disconnected.")


if __name__ == "__main__":
    main()
