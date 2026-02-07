# Quest 2 → SO-101 Teleoperation

Control an SO-101 robot arm using a Meta Quest 2 VR right-hand controller with 6DOF end-effector tracking via inverse kinematics.

## Hardware Requirements

- **Meta Quest 2** headset with right controller
- **USB-C cable** (Quest Link compatible) connecting Quest 2 to PC
- **SO-101 robot arm** connected via USB serial
- **USB camera** (optional) mounted on the arm for visual feedback in VR

## Software Requirements

### Windows
1. **Oculus PC app** (provides OpenXR runtime): https://www.meta.com/quest/setup/
2. In Oculus app: Enable **Quest Link** under Settings → General
3. Set Oculus as the active OpenXR runtime (usually automatic)

### Linux
1. **Monado** OpenXR runtime: https://monado.freedesktop.org/
2. Configure Monado for Quest Link (requires additional setup)

### Python Dependencies
```bash
pip install lerobot[quest]
```

This installs `pyopenxr>=1.1.0` alongside the base LeRobot dependencies.

## Setup

1. **Connect Quest 2** to PC via USB cable
2. **Put on the headset** and accept the Quest Link prompt
3. **Verify Quest Link** is active (you should see the Link home environment)
4. **Connect SO-101 arm** via USB and note the serial port (e.g., `COM5` on Windows, `/dev/ttyACM0` on Linux)
5. **Download SO-101 URDF** from https://github.com/TheRobotStudio/SO-ARM100

## Quick Start

### Teleoperation
```bash
cd examples/quest_to_so101
python teleoperate.py
```

Edit `teleoperate.py` to set your `ROBOT_PORT` and `URDF_PATH`.

### Recording Dataset
```bash
python record.py
```

## Controls

| Input | Action |
|-------|--------|
| **Index trigger** (hold) | Enable arm tracking (clutch) |
| **Index trigger** (release) | Pause tracking (reposition hand) |
| **Grip trigger** (squeeze) | Close gripper |
| **Grip trigger** (release) | Open gripper |
| **A button** | Save episode / mark success |
| **B button** | Discard episode / re-record |

## Calibration

On startup, the system asks you to hold the controller in a neutral pose:
1. Extend your arm forward at a comfortable height
2. Point the controller in the direction the robot faces (robot +X axis)
3. Pull and hold the **index trigger** to capture the reference pose
4. Release the trigger, then pull again to start teleoperating

## Configuration

Key parameters in `QuestTeleoperatorConfig`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `position_scale` | 1.0 | Scale factor for controller → arm position mapping |
| `rotation_scale` | 1.0 | Scale factor for orientation mapping |
| `smoothing_alpha` | 0.2 | EMA smoothing (0=none, 1=max) |
| `clutch_threshold` | 0.3 | Index trigger threshold for clutch activation |
| `gripper_threshold` | 0.5 | Grip trigger threshold for gripper close |

## Architecture

```
Quest 2 Controller (6DOF + triggers)
    │
    ▼
QuestTeleoperator.get_action()
    │  → {quest.pos, quest.rot, quest.grip, quest.enabled}
    ▼
MapQuestActionToRobotAction (VR→Robot frame transform)
    │  → {target_x/y/z, target_wx/wy/wz, gripper_vel, enabled}
    ▼
EEReferenceAndDelta (forward kinematics + delta computation)
    │  → {ee.x/y/z, ee.wx/wy/wz}
    ▼
EEBoundsAndSafety (workspace limits + jump prevention)
    ▼
InverseKinematicsEEToJoints (IK solver via placo)
    │  → {shoulder_pan.pos, shoulder_lift.pos, ..., gripper.pos}
    ▼
SO-101 Follower (serial motor commands)
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "pyopenxr not found" | Run `pip install pyopenxr>=1.1.0` |
| "No OpenXR runtime" | Install Oculus app (Windows) or Monado (Linux) |
| Controller not tracking | Ensure Quest Link is active, headset is on |
| Arm moves too fast/slow | Adjust `position_scale` in config |
| Arm shakes/jitters | Increase `smoothing_alpha` (e.g., 0.3-0.5) |
| IK solver fails | Reduce `MAX_EE_STEP_M`, check URDF path |
| Robot doesn't move | Check serial port, verify calibration |
