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
OpenXR session management for Meta Quest 2 controller tracking.

This module wraps the pyopenxr library to provide a clean interface for:
- Initializing an OpenXR session (headless, no rendering required for controller-only)
- Reading 6DOF controller pose (position + orientation)
- Reading trigger/button states
- Background polling thread for low-latency updates

Requirements:
- pyopenxr >= 1.1.0
- An active OpenXR runtime (Oculus on Windows, Monado on Linux)
- Meta Quest 2 connected via Quest Link (USB or Air Link)
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

logger = logging.getLogger(__name__)


class ControllerButton(Enum):
    """Quest 2 controller button identifiers."""
    A = "a"
    B = "b"
    X = "x"
    Y = "y"
    THUMBSTICK_CLICK = "thumbstick_click"
    MENU = "menu"


@dataclass
class ControllerState:
    """Snapshot of a Quest 2 controller's current state."""
    position: np.ndarray = field(default_factory=lambda: np.zeros(3, dtype=np.float64))
    orientation: np.ndarray = field(default_factory=lambda: np.array([0, 0, 0, 1], dtype=np.float64))  # xyzw
    grip_trigger: float = 0.0       # 0.0 (open) to 1.0 (squeezed)
    index_trigger: float = 0.0      # 0.0 (released) to 1.0 (pulled)
    thumbstick_x: float = 0.0       # -1.0 (left) to 1.0 (right)
    thumbstick_y: float = 0.0       # -1.0 (down) to 1.0 (up)
    button_a: bool = False
    button_b: bool = False
    is_tracking: bool = False
    timestamp: float = 0.0


class OpenXRSession:
    """
    Manages an OpenXR session for reading Quest 2 controller data.

    Uses a background thread to poll the OpenXR runtime at the configured rate,
    storing the latest controller state in a thread-safe manner.
    """

    def __init__(self, polling_rate_hz: float = 90.0):
        self._polling_rate_hz = polling_rate_hz
        self._running = False
        self._poll_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._left_state = ControllerState()
        self._right_state = ControllerState()

        # OpenXR handles (initialized in connect())
        self._instance = None
        self._session = None
        self._space = None
        self._action_set = None
        self._hand_paths = {}
        self._pose_actions = {}
        self._trigger_actions = {}
        self._grip_actions = {}
        self._thumbstick_actions = {}
        self._button_actions = {}

    @property
    def is_connected(self) -> bool:
        return self._session is not None and self._running

    def connect(self) -> None:
        """Initialize OpenXR instance, session, and action bindings."""
        try:
            import xr
        except ImportError:
            raise ImportError(
                "pyopenxr is required for Quest teleoperator. "
                "Install with: pip install pyopenxr>=1.1.0\n"
                "Also ensure an OpenXR runtime is active (Oculus app on Windows, Monado on Linux)."
            )

        logger.info("Initializing OpenXR session for Quest 2 controller tracking...")

        # Create OpenXR instance
        self._instance = xr.create_instance(
            create_info=xr.InstanceCreateInfo(
                application_info=xr.ApplicationInfo(
                    application_name="LeRobot Quest Teleop",
                    application_version=xr.Version(1, 0, 0),
                ),
                enabled_extension_names=[],
            )
        )

        # Get the system (HMD)
        system_id = xr.get_system(
            self._instance,
            xr.SystemGetInfo(form_factor=xr.FormFactor.HEAD_MOUNTED_DISPLAY),
        )

        # Create session
        self._session = xr.create_session(
            self._instance,
            xr.SessionCreateInfo(system_id=system_id),
        )

        # Create reference space (LOCAL = seated, STAGE = room-scale)
        self._space = xr.create_reference_space(
            self._session,
            xr.ReferenceSpaceCreateInfo(
                reference_space_type=xr.ReferenceSpaceType.LOCAL,
                pose_in_reference_space=xr.Posef(),
            ),
        )

        # Set up input actions
        self._setup_actions()

        # Start background polling
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

        logger.info("OpenXR session connected. Controller polling started.")

    def _setup_actions(self) -> None:
        """Create OpenXR action set and bind controller inputs."""
        import xr

        # Create action set
        self._action_set = xr.create_action_set(
            self._instance,
            xr.ActionSetCreateInfo(
                action_set_name="teleop",
                localized_action_set_name="Teleoperation",
            ),
        )

        # Define hand paths
        for hand in ("left", "right"):
            self._hand_paths[hand] = xr.string_to_path(
                self._instance, f"/user/hand/{hand}"
            )

        # Create pose actions for each hand
        for hand in ("left", "right"):
            self._pose_actions[hand] = xr.create_action(
                self._action_set,
                xr.ActionCreateInfo(
                    action_type=xr.ActionType.POSE_INPUT,
                    action_name=f"{hand}_hand_pose",
                    localized_action_name=f"{hand.capitalize()} Hand Pose",
                    subaction_paths=[self._hand_paths[hand]],
                ),
            )

        # Create trigger/grip/thumbstick actions
        for hand in ("left", "right"):
            self._trigger_actions[hand] = xr.create_action(
                self._action_set,
                xr.ActionCreateInfo(
                    action_type=xr.ActionType.FLOAT_INPUT,
                    action_name=f"{hand}_trigger",
                    localized_action_name=f"{hand.capitalize()} Trigger",
                    subaction_paths=[self._hand_paths[hand]],
                ),
            )
            self._grip_actions[hand] = xr.create_action(
                self._action_set,
                xr.ActionCreateInfo(
                    action_type=xr.ActionType.FLOAT_INPUT,
                    action_name=f"{hand}_grip",
                    localized_action_name=f"{hand.capitalize()} Grip",
                    subaction_paths=[self._hand_paths[hand]],
                ),
            )
            self._thumbstick_actions[hand] = xr.create_action(
                self._action_set,
                xr.ActionCreateInfo(
                    action_type=xr.ActionType.VECTOR2F_INPUT,
                    action_name=f"{hand}_thumbstick",
                    localized_action_name=f"{hand.capitalize()} Thumbstick",
                    subaction_paths=[self._hand_paths[hand]],
                ),
            )

        # Create button actions (A/B for right, X/Y for left)
        for hand, buttons in [("right", ["a", "b"]), ("left", ["x", "y"])]:
            self._button_actions[hand] = {}
            for btn in buttons:
                self._button_actions[hand][btn] = xr.create_action(
                    self._action_set,
                    xr.ActionCreateInfo(
                        action_type=xr.ActionType.BOOLEAN_INPUT,
                        action_name=f"{hand}_{btn}_button",
                        localized_action_name=f"{hand.capitalize()} {btn.upper()} Button",
                        subaction_paths=[self._hand_paths[hand]],
                    ),
                )

        # Suggest bindings for Oculus Touch controllers
        bindings = []
        for hand in ("left", "right"):
            prefix = f"/user/hand/{hand}/input"
            bindings.extend([
                xr.ActionSuggestedBinding(
                    self._pose_actions[hand],
                    xr.string_to_path(self._instance, f"{prefix}/grip/pose"),
                ),
                xr.ActionSuggestedBinding(
                    self._trigger_actions[hand],
                    xr.string_to_path(self._instance, f"{prefix}/trigger/value"),
                ),
                xr.ActionSuggestedBinding(
                    self._grip_actions[hand],
                    xr.string_to_path(self._instance, f"{prefix}/squeeze/value"),
                ),
                xr.ActionSuggestedBinding(
                    self._thumbstick_actions[hand],
                    xr.string_to_path(self._instance, f"{prefix}/thumbstick"),
                ),
            ])

        # Button bindings
        for hand, buttons in [("right", ["a", "b"]), ("left", ["x", "y"])]:
            prefix = f"/user/hand/{hand}/input"
            for btn in buttons:
                bindings.append(
                    xr.ActionSuggestedBinding(
                        self._button_actions[hand][btn],
                        xr.string_to_path(self._instance, f"{prefix}/{btn}/click"),
                    )
                )

        xr.suggest_interaction_profile_bindings(
            self._instance,
            xr.InteractionProfileSuggestedBinding(
                interaction_profile=xr.string_to_path(
                    self._instance, "/interaction_profiles/oculus/touch_controller"
                ),
                suggested_bindings=bindings,
            ),
        )

        # Attach action set to session
        xr.attach_session_action_sets(
            self._session,
            xr.SessionActionSetsAttachInfo(action_sets=[self._action_set]),
        )

        # Create action spaces for pose tracking
        self._action_spaces = {}
        for hand in ("left", "right"):
            self._action_spaces[hand] = xr.create_action_space(
                self._session,
                xr.ActionSpaceCreateInfo(
                    action=self._pose_actions[hand],
                    subaction_path=self._hand_paths[hand],
                ),
            )

    def _poll_loop(self) -> None:
        """Background thread that continuously polls controller state."""
        import xr

        interval = 1.0 / self._polling_rate_hz

        while self._running:
            t0 = time.perf_counter()
            try:
                # Poll OpenXR events
                while True:
                    event_buffer = xr.EventDataBuffer()
                    result = xr.poll_event(self._instance, event_buffer)
                    if result == xr.Result.EVENT_UNAVAILABLE:
                        break
                    # Handle session state changes if needed
                    if event_buffer.type == xr.StructureType.EVENT_DATA_SESSION_STATE_CHANGED:
                        state_event = xr.cast(event_buffer, xr.EventDataSessionStateChanged)
                        if state_event.state == xr.SessionState.READY:
                            xr.begin_session(
                                self._session,
                                xr.SessionBeginInfo(
                                    primary_view_configuration_type=xr.ViewConfigurationType.PRIMARY_STEREO,
                                ),
                            )

                # Wait for frame (required by OpenXR even if we're not rendering)
                frame_state = xr.wait_frame(self._session, xr.FrameWaitInfo())
                xr.begin_frame(self._session, xr.FrameBeginInfo())

                # Sync actions
                xr.sync_actions(
                    self._session,
                    xr.ActionsSyncInfo(active_action_sets=[
                        xr.ActiveActionSet(action_set=self._action_set),
                    ]),
                )

                # Read controller states
                now = time.perf_counter()
                for hand, state_attr in [("left", "_left_state"), ("right", "_right_state")]:
                    state = self._read_controller(hand, frame_state.predicted_display_time)
                    state.timestamp = now
                    with self._lock:
                        setattr(self, state_attr, state)

                # End frame
                xr.end_frame(
                    self._session,
                    xr.FrameEndInfo(
                        display_time=frame_state.predicted_display_time,
                        environment_blend_mode=xr.EnvironmentBlendMode.OPAQUE,
                        layers=[],
                    ),
                )

            except Exception as e:
                logger.debug(f"OpenXR poll error: {e}")

            elapsed = time.perf_counter() - t0
            sleep_time = interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _read_controller(self, hand: str, predicted_time) -> ControllerState:
        """Read all inputs from one controller."""
        import xr

        state = ControllerState()

        # Read pose
        space_location = xr.locate_space(
            self._action_spaces[hand],
            self._space,
            predicted_time,
        )
        pose_valid = bool(
            space_location.location_flags
            & (xr.SpaceLocationFlags.POSITION_VALID_BIT | xr.SpaceLocationFlags.ORIENTATION_VALID_BIT)
        )
        state.is_tracking = pose_valid
        if pose_valid:
            pos = space_location.pose.position
            state.position = np.array([pos.x, pos.y, pos.z], dtype=np.float64)
            ori = space_location.pose.orientation
            # OpenXR quaternion is (x, y, z, w)
            state.orientation = np.array([ori.x, ori.y, ori.z, ori.w], dtype=np.float64)

        # Read index trigger
        trigger_state = xr.get_action_state_float(
            self._session,
            xr.ActionStateGetInfo(
                action=self._trigger_actions[hand],
                subaction_path=self._hand_paths[hand],
            ),
        )
        if trigger_state.is_active:
            state.index_trigger = trigger_state.current_state

        # Read grip trigger
        grip_state = xr.get_action_state_float(
            self._session,
            xr.ActionStateGetInfo(
                action=self._grip_actions[hand],
                subaction_path=self._hand_paths[hand],
            ),
        )
        if grip_state.is_active:
            state.grip_trigger = grip_state.current_state

        # Read thumbstick
        thumb_state = xr.get_action_state_vector2f(
            self._session,
            xr.ActionStateGetInfo(
                action=self._thumbstick_actions[hand],
                subaction_path=self._hand_paths[hand],
            ),
        )
        if thumb_state.is_active:
            state.thumbstick_x = thumb_state.current_state.x
            state.thumbstick_y = thumb_state.current_state.y

        # Read buttons
        if hand in self._button_actions:
            for btn_name, btn_action in self._button_actions[hand].items():
                btn_state = xr.get_action_state_boolean(
                    self._session,
                    xr.ActionStateGetInfo(
                        action=btn_action,
                        subaction_path=self._hand_paths[hand],
                    ),
                )
                if btn_state.is_active:
                    if btn_name in ("a", "x"):
                        state.button_a = btn_state.current_state
                    elif btn_name in ("b", "y"):
                        state.button_b = btn_state.current_state

        return state

    def get_controller_state(self, hand: str = "right") -> ControllerState:
        """Get the latest controller state (thread-safe)."""
        with self._lock:
            if hand == "left":
                return ControllerState(
                    position=self._left_state.position.copy(),
                    orientation=self._left_state.orientation.copy(),
                    grip_trigger=self._left_state.grip_trigger,
                    index_trigger=self._left_state.index_trigger,
                    thumbstick_x=self._left_state.thumbstick_x,
                    thumbstick_y=self._left_state.thumbstick_y,
                    button_a=self._left_state.button_a,
                    button_b=self._left_state.button_b,
                    is_tracking=self._left_state.is_tracking,
                    timestamp=self._left_state.timestamp,
                )
            else:
                return ControllerState(
                    position=self._right_state.position.copy(),
                    orientation=self._right_state.orientation.copy(),
                    grip_trigger=self._right_state.grip_trigger,
                    index_trigger=self._right_state.index_trigger,
                    thumbstick_x=self._right_state.thumbstick_x,
                    thumbstick_y=self._right_state.thumbstick_y,
                    button_a=self._right_state.button_a,
                    button_b=self._right_state.button_b,
                    is_tracking=self._right_state.is_tracking,
                    timestamp=self._right_state.timestamp,
                )

    def disconnect(self) -> None:
        """Stop polling and destroy OpenXR session."""
        import xr

        self._running = False
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=2.0)
            self._poll_thread = None

        # Clean up OpenXR resources
        if self._session is not None:
            try:
                xr.end_session(self._session)
            except Exception:
                pass
            try:
                xr.destroy_session(self._session)
            except Exception:
                pass
            self._session = None

        if self._instance is not None:
            try:
                xr.destroy_instance(self._instance)
            except Exception:
                pass
            self._instance = None

        logger.info("OpenXR session disconnected.")
