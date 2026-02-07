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

Requires:
- pyopenxr >= 1.1.0
- Meta Quest 2 connected via Quest Link (USB)
- Active OpenXR runtime (Oculus app on Windows, Monado on Linux)
"""

import logging
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

        # Calibration state
        self._calib_pos: np.ndarray | None = None
        self._calib_rot_inv: Rotation | None = None

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
        return self._calib_pos is not None and self._calib_rot_inv is not None

    @check_if_already_connected
    def connect(self, calibrate: bool = True) -> None:
        """Initialize OpenXR session and begin controller tracking."""
        enable_display = self.config.enable_camera_display
        self._session = OpenXRSession(
            polling_rate_hz=self.config.polling_rate_hz,
            enable_display=enable_display,
        )

        # Set up camera-to-VR display if enabled
        if enable_display:
            from .camera_stream import CameraStream, CameraStreamConfig
            from .vr_display import VRCameraDisplay, VRDisplayConfig

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
            )
            self._vr_display = VRCameraDisplay(display_config)
            self._session.attach_camera_display(self._vr_display, self._camera_stream)

        self._session.connect()

        logger.info(
            f"Quest teleoperator connected. Using {self._hand} controller. "
            "Pull index trigger to enable tracking."
        )

        if calibrate:
            self.calibrate()

    def calibrate(self) -> None:
        """
        Calibrate the controller reference pose.

        Instructs the user to hold the controller in a neutral position
        (pointing forward, at a comfortable height) and pull the index trigger
        to capture the reference pose.
        """
        if not self.is_connected:
            raise RuntimeError("Must be connected before calibrating")

        print(
            "\n=== Quest Controller Calibration ===\n"
            "Hold the controller in a neutral position:\n"
            "  - Arm extended forward at a comfortable height\n"
            "  - Controller pointing in the direction the robot faces (robot +X)\n"
            "\n"
            "Pull and hold the INDEX TRIGGER to capture this reference pose..."
        )

        # Wait for index trigger pull
        while True:
            state = self._session.get_controller_state(self._hand)
            if state.is_tracking and state.index_trigger > self.config.clutch_threshold:
                break
            import time
            time.sleep(0.01)

        # Capture reference pose
        self._calib_pos = state.position.copy()
        quat = state.orientation
        self._calib_rot_inv = Rotation.from_quat(quat).inv()

        # Reset smoothing
        self._smoothed_pos = None
        self._smoothed_rot = None
        self._clutch_engaged = False

        print("Calibration captured! Release trigger, then pull again to start teleoperating.\n")

    @check_if_not_connected
    def get_action(self) -> RobotAction:
        """
        Get the current action from the Quest controller.

        Returns a dict with calibrated position/rotation in robot frame,
        grip trigger value, and enabled status.
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
            # Re-capture position reference on clutch re-engage to prevent jumps
            self._clutch_reference_pos = raw_pos.copy()
            self._clutch_reference_rot = raw_rot
            # Reset smoothing on re-engage
            self._smoothed_pos = None
            self._smoothed_rot = None

        self._clutch_engaged = clutch_now
        enabled = clutch_now

        if enabled and self._clutch_reference_pos is not None:
            # Compute delta from clutch reference, then apply calibration rotation
            delta_pos = raw_pos - self._clutch_reference_pos
            pos_cal = self._calib_rot_inv.apply(delta_pos)

            # Rotation relative to clutch reference, then calibrated
            delta_rot = self._clutch_reference_rot.inv() * raw_rot
            rot_cal = self._calib_rot_inv * delta_rot

            # Apply EMA smoothing
            pos_cal = self._apply_position_smoothing(pos_cal)
            rot_cal = self._apply_rotation_smoothing(rot_cal)
        else:
            pos_cal = np.zeros(3, dtype=np.float64)
            rot_cal = Rotation.from_rotvec(np.zeros(3))

        # Scale
        pos_cal = pos_cal * self.config.position_scale

        # Gripper: grip trigger value
        grip_value = state.grip_trigger

        # Button states for episode control
        buttons = {
            "a": state.button_a,
            "b": state.button_b,
            "thumbstick_click": state.thumbstick_x != 0 or state.thumbstick_y != 0,
        }

        return {
            "quest.pos": pos_cal,
            "quest.rot": rot_cal,
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
