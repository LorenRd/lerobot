# Quest 2 → SO-101 Teleoperation

Control an SO-101 robot arm using a Meta Quest 2 VR right-hand controller with 6DOF tracking.

Two teleoperation modes are available:
- **Direct mode** (`teleoperate_direct.py`): Maps controller movements directly to joints. No IK or URDF needed. Works on Windows out of the box.
- **IK mode** (`teleoperate.py`): Uses inverse kinematics for end-effector control. Requires `placo` (Linux only — does not build on Windows).

## Hardware Requirements

- **Meta Quest 2** headset with right controller
- **USB-C cable** (Quest Link compatible) connecting Quest 2 to PC
- **SO-101 robot arm** connected via USB serial
- **Camera** (optional, one of):
  - **USB webcam** mounted on the arm for basic visual feedback
  - **Luxonis OAK-D Lite** mounted on the gripper for RGB + stereo depth (recommended for dataset recording)

## Software Requirements

### Windows (Tested ✅)
1. **SteamVR** with Quest Link:
   - Install **Steam** and **SteamVR** from the Steam Store
   - Install **Oculus PC app**: https://www.meta.com/quest/setup/
   - Enable **Quest Link** in Oculus app → Settings → General
   - SteamVR automatically registers as the OpenXR runtime
2. **Or** use the Oculus OpenXR runtime directly (set as active runtime in Oculus app)

### Linux
1. **Monado** OpenXR runtime: https://monado.freedesktop.org/
2. Configure Monado for Quest Link (requires additional setup)

### Python Dependencies
```bash
pip install lerobot[quest]
```

For **OAK-D Lite** camera support (RGB + depth):
```bash
pip install lerobot[quest,depthai]
```

This installs `pyopenxr>=1.1.0`, `PyOpenGL>=3.1.0`, and `glfw` alongside the base LeRobot dependencies.

> **Windows note**: The IK-based pipeline (`teleoperate.py`) requires `placo`, which cannot be built on Windows. Use `teleoperate_direct.py` instead, or run on Linux for full IK support.

## Setup

1. **Connect Quest 2** to PC via USB cable
2. **Put on the headset** and accept the Quest Link prompt
3. **Verify Quest Link** is active (you should see the Link home environment or SteamVR home)
4. **Connect SO-101 arm** via USB and note the serial port (e.g., `COM4` on Windows, `/dev/ttyACM0` on Linux)
5. **Download SO-101 URDF** from https://github.com/TheRobotStudio/SO-ARM100 (only needed for IK mode)

## Quick Start

### Direct Teleoperation (Recommended for Windows)
```bash
cd examples/quest_to_so101
python teleoperate_direct.py --robot-port COM4
```

Maps controller position to shoulder/elbow joints and rotation to wrist joints. No URDF or IK solver required.

### IK-Based Teleoperation (Linux)
```bash
pip install placo  # Linux only
cd examples/quest_to_so101
python teleoperate.py --robot-port COM4
```

### Teleoperation with Camera-to-VR Display
```bash
# USB webcam
python teleoperate_direct.py --robot-port COM4 --camera
python teleoperate.py --camera --camera-index 1  # Use second USB camera

# OAK-D Lite (RGB + depth side-by-side in VR)
python teleoperate_direct.py --robot-port COM4 --camera --camera-type depthai --show-depth
python teleoperate.py --camera --camera-type depthai --camera-device-id 18443010211F850E00
```

When `--camera` is enabled, the arm-mounted USB camera feed is rendered as a
floating 2D panel in the Quest 2 headset, giving the operator a first-person
view of the robot workspace.

Edit `teleoperate.py` to set your `ROBOT_PORT` and `URDF_PATH`.

### Recording Dataset
```bash
# Simple recording (legacy format, no OAK-D support)
python record.py

# Full LeRobotDataset recording with OAK-D Lite (recommended)
python record_dataset.py --repo-id user/my_dataset --robot-port COM4

# IK mode, custom FPS, no depth
python record_dataset.py --repo-id user/my_dataset --robot-port COM4 --mode ik --fps 15 --no-depth

# With VR preview + specific OAK-D device
python record_dataset.py --repo-id user/my_dataset --show-vr-preview --camera-device-id 18443010211F850E00
```

The `record_dataset.py` script records episodes in the standard LeRobot dataset format with:
- `observation.images.wrist_rgb` — RGB frames from OAK-D Lite (480×640×3)
- `observation.images.wrist_depth` — Stereo depth maps (480×640×1, uint8 normalized)
- `observation.state` — 6 joint positions (shoulder_pan, shoulder_lift, elbow_flex, wrist_flex, wrist_roll, gripper)
- `action` — 6 joint commands

Episode controls: **A button** = save episode, **B button** = discard & re-record, **Ctrl+C** = end session.

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
| `enable_camera_display` | False | Enable camera-to-VR headset display |
| `camera_index` | 0 | USB camera device index or path |
| `camera_type` | "opencv" | Camera backend: `"opencv"` (webcam) or `"depthai"` (OAK-D) |
| `camera_device_id` | "" | OAK-D MxID for specific device (depthai only) |
| `show_depth_in_vr` | False | Side-by-side RGB+depth panel in VR (depthai only) |
| `vr_display_distance` | 1.0 | Distance of VR display panel from user (meters) |
| `vr_display_width` | 0.6 | Width of VR display panel (meters) |
| `vr_display_height` | 0.45 | Height of VR display panel (meters) |

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

--- Camera-to-VR Display (optional, --camera flag) ---

USB Camera (arm-mounted) → CameraStream (background capture thread)
    │                        or
OAK-D Lite (gripper-mounted) → DepthAICameraStream
    │   ├── RGB stream  → left panel
    │   └── Depth stream → right panel (colorized)
    ▼
VRCameraDisplay → OpenXR swapchain texture upload
    │
    ▼
OpenXR quad composition layer → Quest 2 headset display
    (floating 2D panel in front of user)

--- Dataset Recording (record_dataset.py) ---

OAK-D Lite → RGB + Depth frames  ─┐
Quest 2    → Joint commands       ─┤→ LeRobotDataset.add_frame()
SO-101     → Joint positions      ─┘    → .save_episode() → disk
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "pyopenxr not found" | Run `pip install pyopenxr>=1.1.0` |
| "PyOpenGL not found" (camera mode) | Run `pip install PyOpenGL>=3.1.0` |
| "No OpenXR runtime" | Install SteamVR + Oculus app (Windows) or Monado (Linux) |
| `GraphicsDeviceInvalidError` | SteamVR requires OpenGL binding — ensure `glfw` and `PyOpenGL` are installed |
| `GraphicsRequirementsCallMissingError` | Internal bug — ensure latest Quest module version |
| Session stuck at READY (state 2) | Put on the headset — SteamVR needs proximity sensor active to reach FOCUSED |
| Controller not tracking | Ensure Quest Link is active, headset is on, session reaches FOCUSED (state 5) |
| Arm moves too fast/slow | Adjust `position_scale` in config or `--speed` in direct mode |
| Arm shakes/jitters | Increase `smoothing_alpha` (e.g., 0.3-0.5) |
| IK solver fails | Reduce `MAX_EE_STEP_M`, check URDF path |
| `placo` won't install (Windows) | Use `teleoperate_direct.py` instead — placo requires Linux |
| Robot doesn't move | Check serial port, verify calibration completed |
| Robot connection hangs | Ensure arm is powered (external power, not just USB), correct COM port |
| Camera display not showing | Check USB camera connection, try `--camera-index 1` |
| Camera display laggy | Reduce `camera_width`/`camera_height` or `camera_fps` |
| OAK-D not detected | Run `python -c "import depthai; print(depthai.Device.getAllAvailableDevices())"` to verify USB connection |
| OAK-D depth is noisy | Normal at close range (<20cm). Ensure stereo cameras are clean |
| Depth panel all black | Check `--show-depth` flag and `use_depth=True` in camera config |
| "No module 'scservo_sdk'" | Run `pip install feetech-servo-sdk` |
