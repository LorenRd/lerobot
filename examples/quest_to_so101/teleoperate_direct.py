#!/usr/bin/env python

"""
Quest 2 → SO-101 Direct Teleoperation (no IK required)

Maps the Quest 2 right controller position/rotation directly to SO-101 joints.
Uses simple proportional mapping — no inverse kinematics or URDF needed.

Controls:
  - Index trigger (hold): Enable tracking (clutch). Release to reposition hand.
  - Grip trigger (squeeze): Close gripper. Release to open.
  - A button: Exit program

Mapping:
  - Controller X (left/right) → shoulder_pan
  - Controller Y (up/down) → shoulder_lift
  - Controller Z (forward/back) → elbow_flex
  - Controller pitch → wrist_flex
  - Controller yaw → wrist_roll

Usage:
  python teleoperate_direct.py --robot-port COM4
"""

import argparse
import logging
import time

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(message)s")


def main():
    parser = argparse.ArgumentParser(description="Quest 2 → SO-101 Direct Teleoperation")
    parser.add_argument("--robot-port", type=str, default="COM4", help="Serial port for SO-101")
    parser.add_argument("--speed", type=float, default=40.0, help="Degrees per movement unit")
    parser.add_argument("--fps", type=int, default=30, help="Control loop FPS")
    args = parser.parse_args()

    from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig
    from lerobot.teleoperators.quest import QuestTeleoperator, QuestTeleoperatorConfig
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

    speed = args.speed
    interval = 1.0 / args.fps

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
                # Extract rotation vector (radians) — convert to degrees for joint mapping
                rotvec = rot.as_rotvec()
                rot_deg = np.degrees(rotvec)

                # Map controller movements to joint deltas
                deltas = {
                    "shoulder_pan": pos[0] * speed,    # left/right
                    "shoulder_lift": pos[1] * speed,   # up/down
                    "elbow_flex": -pos[2] * speed,     # forward/back
                    "wrist_flex": rot_deg[0] * 0.5,    # pitch
                    "wrist_roll": rot_deg[2] * 0.5,    # yaw
                }

                # Apply deltas to current joints, clamp to limits
                for motor, delta in deltas.items():
                    lo, hi = joint_limits[motor]
                    current_joints[motor] = np.clip(
                        current_joints[motor] + delta, lo, hi
                    )

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
