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
Meta Quest 2 VR controller teleoperator for LeRobot.

Provides 6DOF end-effector control of a robot arm using a Quest 2 controller.
Uses the index trigger as a clutch (press to enable tracking, release to reposition)
and the grip trigger for gripper open/close control.

Includes an empirical 5-pose calibration system that captures the actual mapping
between VR controller movements and robot joint directions, eliminating hardcoded
axis assumptions.

Requires:
- pyopenxr >= 1.1.0
- Meta Quest 2 connected via Quest Link (USB)
- Active OpenXR runtime (Oculus app on Windows, Monado on Linux)
"""

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from lerobot.processor import RobotAction
from lerobot.teleoperators.teleoperator import Teleoperator
from lerobot.teleoperators.utils import TeleopEvents
from lerobot.utils.decorators import check_if_already_connected, check_if_not_connected
from lerobot.utils.rotation import Rotation

from .config_quest import ControllerHand, QuestTeleoperatorConfig
from .openxr_session import ControllerState, OpenXRSession

logger = logging.getLogger(__name__)

# Default calibration file path
CALIBRATION_DIR = Path.home() / ".lerobot"
CALIBRATION_FILE = CALIBRATION_DIR / "quest_calibration.json"

# Pose labels and instructions for the 5-pose calibration
CALIBRATION_POSES = [
    {
        "name": "NEUTRAL",
        "hud": "NEUTRAL: Hold controller still, arm relaxed",
        "console": "Pose 1/5 — NEUTRAL: Hold controller still at a comfortable position.\n"
                   "           This is your zero reference. Pull INDEX TRIGGER when ready.",
    },
    {
        "name": "FORWARD",
        "hud": "FORWARD: Move hand ~20cm FORWARD",
        "console": "Pose 2/5 — FORWARD: Move your hand ~20cm FORWARD from the neutral pose.\n"
                   "           Keep holding the trigger.",
    },
    {
        "name": "UP",
        "hud": "UP: Move hand ~20cm UP",
        "console": "Pose 3/5 — UP: Move your hand ~20cm UP from the neutral pose.\n"
                   "           Keep holding the trigger.",
    },
    {
        "name": "RIGHT",
        "hud": "RIGHT: Move hand ~20cm to the RIGHT",
        "console": "Pose 4/5 — RIGHT: Move your hand ~20cm to the RIGHT from neutral.\n"
                   "           Keep holding the trigger.",
    },
    {
        "name": "PITCH_DOWN",
        "hud": "PITCH: Tilt controller nose DOWN ~45 deg",
        "console": "Pose 5/5 — PITCH DOWN: Tilt the controller nose ~45° DOWN.\n"
                   "           Keep holding the trigger.",
    },
]


@dataclass
class CalibrationData:
    """Empirical calibration data from the 5-pose procedure.

    Stores a 3×3 position mapping matrix and rotation axis info so that
    ``get_action()`` can return correctly-mapped deltas without relying on
    hardcoded VR→Robot axis transforms.
    """

    # 3×3 matrix: robot_pos_delta = pos_mapping @ vr_pos_delta
    pos_mapping: list[list[float]] = field(default_factory=lambda: [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ])
    # Rotation mapping: [roll_axis_index, pitch_axis_index] into the VR rotvec
    # and the corresponding signs.  After calibration these are filled with the
    # empirical mapping so that euler_robot = rot_signs * euler_vr[rot_axes].
    rot_pitch_sign: float = 1.0
    rot_roll_sign: float = 1.0
    # The VR rotvec component index that maps to robot pitch (wrist flex)
    rot_pitch_vr_axis: int = 0  # default: VR rotvec[0]
    # The VR rotvec component index that maps to robot roll (wrist roll)
    rot_roll_vr_axis: int = 2   # default: VR rotvec[2]
    # Reference orientation quaternion [x,y,z,w] captured at neutral pose
    neutral_orientation: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 1.0])
    # Timestamp of calibration
    timestamp: str = ""

    def as_pos_matrix(self) -> np.ndarray:
        return np.array(self.pos_mapping, dtype=np.float64)

    def save(self, path: Path | None = None) -> None:
        path = path or CALIBRATION_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)
        logger.info(f"Calibration saved to {path}")

    @classmethod
    def load(cls, path: Path | None = None) -> "CalibrationData | None":
        path = path or CALIBRATION_FILE
        if not path.exists():
            return None
        try:
            with open(path) as f:
                data = json.load(f)
            logger.info(f"Calibration loaded from {path}")
            return cls(**data)
        except Exception as e:
            logger.warning(f"Failed to load calibration from {path}: {e}")
            return None


class QuestTeleoperator(Teleoperator):
    """
    Teleoperator using a Meta Quest 2 VR controller for 6DOF end-effector control.

    Control mapping:
    - Controller position/orientation → End-effector pose (relative/delta mode)
    - Index trigger → Clutch (hold to enable tracking, release to reposition)
    - Grip trigger → Gripper (squeeze to close, release to open)
    - A button → Mark success / end episode
    - B button → Discard and re-record episode
    - Thumbstick click → Terminate recording session
    """

    config_class = QuestTeleoperatorConfig
    name = "quest"

    def __init__(self, config: QuestTeleoperatorConfig):
        super().__init__(config)
        self.config = config

        self._session: OpenXRSession | None = None
        self._hand = config.controller_hand.value

        # Empirical calibration data (replaces old _calib_rot_inv)
        self._calibration: CalibrationData | None = None
        self._pos_mapping: np.ndarray | None = None  # cached 3x3 matrix

        # Clutch state
        self._clutch_engaged = False
        self._clutch_reference_pos: np.ndarray | None = None
        self._clutch_reference_rot: Rotation | None = None

        # Smoothing state (EMA)
        self._smoothed_pos: np.ndarray | None = None
        self._smoothed_rot: Rotation | None = None

        # Camera-to-VR display components (initialized in connect() if enabled)
        self._camera_stream = None
        self._vr_display = None

    @property
    def action_features(self) -> dict[str, type]:
        return {
            "quest.pos": np.ndarray,      # Calibrated 3D position in robot frame
            "quest.rot": Rotation,        # Calibrated rotation in robot frame
            "quest.grip": float,          # Grip trigger value (0.0-1.0)
            "quest.enabled": bool,        # Whether clutch is engaged (tracking active)
            "quest.buttons": dict,        # Button states for episode control
        }

    @property
    def feedback_features(self) -> dict[str, type]:
        # Future: haptic feedback via controller vibration
        return {}

    @property
    def is_connected(self) -> bool:
        return self._session is not None and self._session.is_connected

    @property
    def is_calibrated(self) -> bool:
        return self._calibration is not None and self._pos_mapping is not None

    @check_if_already_connected
    def connect(self, calibrate: bool = True, recalibrate: bool = False) -> None:
        """Initialize OpenXR session and begin controller tracking.

        Args:
            calibrate: Whether to calibrate at startup. If a saved calibration
                exists and ``recalibrate`` is False, it will be loaded instead.
            recalibrate: Force a fresh 5-pose calibration even if a saved one exists.
        """
        enable_display = self.config.enable_camera_display
        self._session = OpenXRSession(
            polling_rate_hz=self.config.polling_rate_hz,
            enable_display=enable_display,
        )

        # Set up camera-to-VR display if enabled
        if enable_display:
            from .vr_display import VRCameraDisplay, VRDisplayConfig

            show_depth = self.config.show_depth_in_vr and self.config.camera_type == "depthai"

            if self.config.camera_type == "depthai":
                from .camera_stream import DepthAICameraStream

                self._camera_stream = DepthAICameraStream(
                    device_id=self.config.camera_device_id,
                    width=self.config.camera_width,
                    height=self.config.camera_height,
                    fps=self.config.camera_fps,
                )
            else:
                from .camera_stream import CameraStream, CameraStreamConfig

                cam_config = CameraStreamConfig(
                    camera_index=self.config.camera_index,
                    capture_width=self.config.camera_width,
                    capture_height=self.config.camera_height,
                    capture_fps=self.config.camera_fps,
                )
                self._camera_stream = CameraStream(cam_config)

            self._camera_stream.connect()

            display_config = VRDisplayConfig(
                enabled=True,
                display_width=self.config.vr_display_width,
                display_height=self.config.vr_display_height,
                display_distance=self.config.vr_display_distance,
                display_offset_y=self.config.vr_display_offset_y,
                texture_width=self.config.camera_width,
                texture_height=self.config.camera_height,
                show_depth=show_depth,
            )
            self._vr_display = VRCameraDisplay(display_config)
            self._session.attach_camera_display(self._vr_display, self._camera_stream)

        self._session.connect()

        logger.info(
            f"Quest teleoperator connected. Using {self._hand} controller. "
            "Pull index trigger to enable tracking."
        )

        if calibrate:
            if not recalibrate:
                saved = CalibrationData.load()
                if saved is not None:
                    self._calibration = saved
                    self._pos_mapping = saved.as_pos_matrix()
                    logger.info("Loaded saved calibration. Use --recalibrate to redo.")
                    return
            self.calibrate()

    def calibrate(self) -> None:
        """
        Run the 5-pose empirical calibration procedure.

        Guides the user through 5 poses via console + VR HUD:
        1. Neutral (zero reference)
        2. Forward (~20 cm)
        3. Up (~20 cm)
        4. Right (~20 cm)
        5. Pitch down (~45°)

        From these samples, computes a 3×3 position mapping matrix and rotation
        sign/axis info. Saves the result to ``~/.lerobot/quest_calibration.json``.
        """
        if not self.is_connected:
            raise RuntimeError("Must be connected before calibrating")

        HOLD_SECONDS = 3.0  # time to hold each pose for averaging

        print(
            "\n╔══════════════════════════════════════════╗\n"
            "║   5-Pose Empirical Calibration           ║\n"
            "╠══════════════════════════════════════════╣\n"
            "║ You will be guided through 5 poses.      ║\n"
            "║ Pull & HOLD the INDEX TRIGGER, then move  ║\n"
            "║ to each pose when prompted.               ║\n"
            "╚══════════════════════════════════════════╝\n"
        )

        # --- Pose 1: NEUTRAL (zero reference) ---
        pose = CALIBRATION_POSES[0]
        print(pose["console"])
        self._set_hud_calibration_text(pose["hud"])

        # Wait for trigger pull
        state = self._wait_for_trigger()
        neutral_pos = state.position.copy()
        neutral_rot = Rotation.from_quat(state.orientation)
        neutral_quat = state.orientation.copy()
        print(f"  ✓ Neutral captured. Hold still for {HOLD_SECONDS:.0f}s...")
        self._set_hud_calibration_text(f"NEUTRAL: Hold still... {HOLD_SECONDS:.0f}s")
        neutral_pos = self._average_position(HOLD_SECONDS)
        print("  ✓ Neutral averaged.\n")

        # --- Poses 2-4: FORWARD, UP, RIGHT (position deltas) ---
        vr_deltas = []
        for i in range(1, 4):
            pose = CALIBRATION_POSES[i]
            print(pose["console"])
            self._set_hud_calibration_text(pose["hud"])
            time.sleep(0.5)  # brief pause for user to read

            # Wait for user to settle, then average
            print(f"  Move now... averaging for {HOLD_SECONDS:.0f}s")
            for countdown in range(int(HOLD_SECONDS), 0, -1):
                self._set_hud_calibration_text(f"{pose['hud']}  [{countdown}s]")
                time.sleep(1.0)

            avg_pos = self._average_position(1.0)  # 1s of averaging
            delta = avg_pos - neutral_pos
            vr_deltas.append(delta)
            mag = np.linalg.norm(delta) * 100  # cm
            print(f"  ✓ {pose['name']} captured: delta={np.round(delta, 4)} ({mag:.1f} cm)\n")

        # --- Pose 5: PITCH DOWN (rotation delta) ---
        pose = CALIBRATION_POSES[4]
        print(pose["console"])
        self._set_hud_calibration_text(pose["hud"])
        time.sleep(0.5)

        print(f"  Tilt now... averaging for {HOLD_SECONDS:.0f}s")
        for countdown in range(int(HOLD_SECONDS), 0, -1):
            self._set_hud_calibration_text(f"{pose['hud']}  [{countdown}s]")
            time.sleep(1.0)

        state = self._session.get_controller_state(self._hand)
        pitch_rot = Rotation.from_quat(state.orientation)
        pitch_delta_rot = neutral_rot.inv() * pitch_rot
        pitch_rotvec = pitch_delta_rot.as_rotvec()
        print(f"  ✓ PITCH captured: rotvec={np.round(np.degrees(pitch_rotvec), 1)} deg\n")

        # --- Compute mapping matrix ---
        # V = 3x3 matrix where columns are the VR deltas for forward/up/right
        V = np.column_stack(vr_deltas)  # (3, 3)

        # J = desired robot-frame directions for each pose
        # Forward → robot +X = [1,0,0], Up → robot +Z = [0,0,1], Right → robot -Y = [0,-1,0]
        J = np.array([
            [1.0, 0.0, 0.0],   # forward → robot +X
            [0.0, 0.0, 1.0],   # up → robot +Z
            [0.0, -1.0, 0.0],  # right → robot -Y
        ]).T  # (3, 3) where columns are robot directions

        try:
            # M = J @ V^-1  so that  M @ vr_delta = robot_delta
            # Normalize columns of V to unit length first, then scale M
            # to preserve magnitude
            V_norms = np.linalg.norm(V, axis=0, keepdims=True)
            V_unit = V / np.maximum(V_norms, 1e-6)
            M = J @ np.linalg.inv(V_unit)
            # Now M maps unit-VR-delta to robot direction, preserving robot magnitudes
        except np.linalg.LinAlgError:
            logger.warning("Calibration matrix is singular — poses too similar. Using fallback.")
            M = np.eye(3)

        # --- Compute rotation mapping ---
        # Find which VR rotvec component has the largest magnitude → that's the pitch axis
        abs_rv = np.abs(pitch_rotvec)
        pitch_vr_axis = int(np.argmax(abs_rv))
        # Sign: pitch down should produce positive wrist_flex (from Jacobian: +flex = down)
        # The user pitched down, so the captured rotvec component should be mapped to positive
        pitch_sign = 1.0 if pitch_rotvec[pitch_vr_axis] > 0 else -1.0

        # Roll axis: the remaining axis with largest magnitude (orthogonal to pitch)
        remaining = [i for i in range(3) if i != pitch_vr_axis]
        roll_vr_axis = remaining[0]  # heuristic: take the first remaining axis
        roll_sign = 1.0  # default; user can tune with --rot-scale

        # --- Build calibration data ---
        cal = CalibrationData(
            pos_mapping=M.tolist(),
            rot_pitch_sign=pitch_sign,
            rot_roll_sign=roll_sign,
            rot_pitch_vr_axis=pitch_vr_axis,
            rot_roll_vr_axis=roll_vr_axis,
            neutral_orientation=neutral_quat.tolist(),
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        cal.save()

        self._calibration = cal
        self._pos_mapping = cal.as_pos_matrix()

        # Reset clutch/smoothing
        self._smoothed_pos = None
        self._smoothed_rot = None
        self._clutch_engaged = False

        self._set_hud_calibration_text("")  # clear HUD

        print(
            "╔══════════════════════════════════════════╗\n"
            "║   Calibration complete!                  ║\n"
            "╚══════════════════════════════════════════╝\n"
            f"  Position mapping matrix:\n{np.array_str(M, precision=3)}\n"
            f"  Pitch: VR axis {pitch_vr_axis}, sign {pitch_sign:+.0f}\n"
            f"  Roll:  VR axis {roll_vr_axis}, sign {roll_sign:+.0f}\n"
            "\nRelease trigger, then pull again to start teleoperating.\n"
        )

    # -- Calibration helpers --------------------------------------------------

    def _wait_for_trigger(self) -> ControllerState:
        """Block until the index trigger is pulled while tracking."""
        while True:
            state = self._session.get_controller_state(self._hand)
            if state.is_tracking and state.index_trigger > self.config.clutch_threshold:
                return state
            time.sleep(0.01)

    def _average_position(self, duration: float) -> np.ndarray:
        """Average controller position over ``duration`` seconds."""
        samples = []
        t_end = time.perf_counter() + duration
        while time.perf_counter() < t_end:
            state = self._session.get_controller_state(self._hand)
            if state.is_tracking:
                samples.append(state.position.copy())
            time.sleep(0.005)
        if not samples:
            return np.zeros(3, dtype=np.float64)
        return np.mean(samples, axis=0)

    def _set_hud_calibration_text(self, text: str) -> None:
        """Push a calibration instruction line to the VR HUD (if available)."""
        if self._session is not None:
            self._session.set_hud_calibration_text(text)

    @check_if_not_connected
    def get_action(self) -> RobotAction:
        """
        Get the current action from the Quest controller.

        Returns a dict with empirically-mapped position/rotation in robot frame,
        grip trigger value, and enabled status.  The position is mapped through
        the 3×3 calibration matrix so that forward/up/right hand movements
        correspond to the correct robot-frame axes.
        """
        state = self._session.get_controller_state(self._hand)

        if not state.is_tracking or not self.is_calibrated:
            return {
                "quest.pos": np.zeros(3, dtype=np.float64),
                "quest.rot": Rotation.from_rotvec(np.zeros(3)),
                "quest.grip": 0.0,
                "quest.enabled": False,
                "quest.buttons": {"a": False, "b": False, "thumbstick_click": False},
            }

        # Current raw pose
        raw_pos = state.position
        raw_rot = Rotation.from_quat(state.orientation)

        # Check clutch state (index trigger)
        clutch_now = state.index_trigger > self.config.clutch_threshold
        rising_edge = clutch_now and not self._clutch_engaged

        if rising_edge:
            # Re-capture references on clutch re-engage to prevent jumps
            self._clutch_reference_pos = raw_pos.copy()
            self._clutch_reference_rot = raw_rot
            self._smoothed_pos = None
            self._smoothed_rot = None

        self._clutch_engaged = clutch_now
        enabled = clutch_now

        if enabled and self._clutch_reference_pos is not None:
            # Position: delta in VR world frame, then map through calibration matrix
            delta_pos = raw_pos - self._clutch_reference_pos
            pos_mapped = self._pos_mapping @ delta_pos  # (3,) in robot frame

            # Rotation: delta from clutch reference, expressed as rotvec
            delta_rot = self._clutch_reference_rot.inv() * raw_rot

            # Apply EMA smoothing
            pos_mapped = self._apply_position_smoothing(pos_mapped)
            delta_rot = self._apply_rotation_smoothing(delta_rot)
        else:
            pos_mapped = np.zeros(3, dtype=np.float64)
            delta_rot = Rotation.from_rotvec(np.zeros(3))

        # Scale
        pos_mapped = pos_mapped * self.config.position_scale

        # Gripper: grip trigger value
        grip_value = state.grip_trigger

        # Button states for episode control
        buttons = {
            "a": state.button_a,
            "b": state.button_b,
            "thumbstick_click": state.thumbstick_x != 0 or state.thumbstick_y != 0,
        }

        return {
            "quest.pos": pos_mapped,
            "quest.rot": delta_rot,
            "quest.grip": grip_value,
            "quest.enabled": enabled,
            "quest.buttons": buttons,
        }

    def get_teleop_events(self) -> dict[str, Any]:
        """
        Get episode control events from Quest controller buttons.

        - A button: Mark success / end episode
        - B button: Discard and re-record episode
        """
        if not self.is_connected:
            return {
                TeleopEvents.IS_INTERVENTION: False,
                TeleopEvents.TERMINATE_EPISODE: False,
                TeleopEvents.SUCCESS: False,
                TeleopEvents.RERECORD_EPISODE: False,
            }

        state = self._session.get_controller_state(self._hand)

        return {
            TeleopEvents.IS_INTERVENTION: self._clutch_engaged,
            TeleopEvents.TERMINATE_EPISODE: state.button_b,
            TeleopEvents.SUCCESS: state.button_a,
            TeleopEvents.RERECORD_EPISODE: state.button_b,
        }

    def _apply_position_smoothing(self, pos: np.ndarray) -> np.ndarray:
        """Apply exponential moving average smoothing to position."""
        alpha = self.config.smoothing_alpha
        if alpha <= 0 or self._smoothed_pos is None:
            self._smoothed_pos = pos.copy()
            return pos
        self._smoothed_pos = alpha * self._smoothed_pos + (1 - alpha) * pos
        return self._smoothed_pos.copy()

    def _apply_rotation_smoothing(self, rot: Rotation) -> Rotation:
        """Apply quaternion LERP-based smoothing to rotation."""
        alpha = self.config.smoothing_alpha
        if alpha <= 0 or self._smoothed_rot is None:
            self._smoothed_rot = rot
            return rot
        # Simple quaternion interpolation (NLERP) between previous and current
        q_prev = self._smoothed_rot.as_quat()
        q_curr = rot.as_quat()
        # Ensure shortest path (dot product positive)
        if np.dot(q_prev, q_curr) < 0:
            q_curr = -q_curr
        q_interp = alpha * q_prev + (1 - alpha) * q_curr
        # Normalize
        q_interp = q_interp / np.linalg.norm(q_interp)
        self._smoothed_rot = Rotation.from_quat(q_interp)
        return self._smoothed_rot

    def configure(self) -> None:
        """No additional configuration needed for Quest controller."""
        pass

    def update_hud_status(self, is_recording: bool = False, episode: int = 0, frame_count: int = 0) -> None:
        """Update the VR HUD overlay with recording status (for use by recording scripts)."""
        if self._session is not None:
            self._session.update_hud_status(is_recording, episode, frame_count)

    def get_monitor_frame(self) -> np.ndarray | None:
        """Return the latest composited camera frame (BGR) for desktop monitor display.

        Returns None if camera display is not enabled or no frame has been rendered yet.
        """
        if self._vr_display is not None:
            return self._vr_display.get_latest_display_frame()
        # Fallback: return raw camera frame if display not set up but camera exists
        if self._camera_stream is not None:
            return self._camera_stream.get_latest_frame()
        return None

    def send_feedback(self, feedback: dict[str, Any]) -> None:
        """
        Send feedback to the Quest controller.
        Future: haptic vibration feedback.
        """
        # TODO: Implement haptic feedback via OpenXR haptic API
        pass

    @check_if_not_connected
    def disconnect(self) -> None:
        """Disconnect from the Quest controller and clean up all resources."""
        if self._camera_stream is not None:
            self._camera_stream.disconnect()
            self._camera_stream = None

        self._vr_display = None  # Cleaned up by OpenXRSession.disconnect()

        if self._session is not None:
            self._session.disconnect()
            self._session = None

        self._clutch_engaged = False
        self._smoothed_pos = None
        self._smoothed_rot = None
        logger.info("Quest teleoperator disconnected.")
