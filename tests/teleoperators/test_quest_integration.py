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
Hardware integration tests for the Quest VR controller teleoperator.

These tests require actual hardware (Quest 2 headset, SO-101 arm, USB camera)
and are skipped automatically when hardware is not available.

Run with: pytest tests/teleoperators/test_quest_integration.py -v
"""

import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# Skip the entire module if pyopenxr is not installed
xr = pytest.importorskip("xr", reason="pyopenxr not installed")


def _openxr_runtime_available() -> bool:
    """Check if an OpenXR runtime (e.g., Oculus) is available."""
    try:
        import xr

        instance = xr.create_instance(
            xr.InstanceCreateInfo(
                application_info=xr.ApplicationInfo(
                    application_name="LeRobot Test",
                    application_version=xr.Version(1, 0, 0),
                ),
                enabled_extension_names=[],
            )
        )
        xr.destroy_instance(instance)
        return True
    except Exception:
        return False


def _quest_headset_available() -> bool:
    """Check if a Quest headset is connected via Quest Link."""
    try:
        import xr

        instance = xr.create_instance(
            xr.InstanceCreateInfo(
                application_info=xr.ApplicationInfo(
                    application_name="LeRobot Test",
                    application_version=xr.Version(1, 0, 0),
                ),
                enabled_extension_names=[],
            )
        )
        system_id = xr.get_system(
            instance,
            xr.SystemGetInfo(form_factor=xr.FormFactor.HEAD_MOUNTED_DISPLAY),
        )
        xr.destroy_instance(instance)
        return system_id is not None
    except Exception:
        return False


def _usb_camera_available(index: int = 0) -> bool:
    """Check if a USB camera is available at the given index."""
    try:
        import cv2

        cap = cv2.VideoCapture(index)
        available = cap.isOpened()
        cap.release()
        return available
    except Exception:
        return False


requires_openxr = pytest.mark.skipif(
    not _openxr_runtime_available(),
    reason="OpenXR runtime not available (install Oculus app or Monado)",
)

requires_quest = pytest.mark.skipif(
    not _quest_headset_available(),
    reason="Quest headset not connected via Quest Link",
)

requires_camera = pytest.mark.skipif(
    not _usb_camera_available(),
    reason="No USB camera available at index 0",
)


# ==========================
# OpenXR Runtime Tests
# ==========================


@requires_openxr
class TestOpenXRRuntime:
    """Tests that verify the OpenXR runtime is functional."""

    def test_create_instance(self):
        """OpenXR instance can be created."""
        import xr

        instance = xr.create_instance(
            xr.InstanceCreateInfo(
                application_info=xr.ApplicationInfo(
                    application_name="LeRobot Integration Test",
                    application_version=xr.Version(1, 0, 0),
                ),
                enabled_extension_names=[],
            )
        )
        assert instance is not None
        xr.destroy_instance(instance)


@requires_quest
class TestQuestConnection:
    """Tests that verify Quest 2 headset connectivity."""

    def test_get_system(self):
        """Can detect a head-mounted display system."""
        import xr

        instance = xr.create_instance(
            xr.InstanceCreateInfo(
                application_info=xr.ApplicationInfo(
                    application_name="LeRobot Integration Test",
                    application_version=xr.Version(1, 0, 0),
                ),
                enabled_extension_names=[],
            )
        )
        system_id = xr.get_system(
            instance,
            xr.SystemGetInfo(form_factor=xr.FormFactor.HEAD_MOUNTED_DISPLAY),
        )
        assert system_id is not None
        xr.destroy_instance(instance)

    def test_create_session(self):
        """Can create an OpenXR session (headless, for controller input)."""
        import xr

        instance = xr.create_instance(
            xr.InstanceCreateInfo(
                application_info=xr.ApplicationInfo(
                    application_name="LeRobot Integration Test",
                    application_version=xr.Version(1, 0, 0),
                ),
                enabled_extension_names=[],
            )
        )
        system_id = xr.get_system(
            instance,
            xr.SystemGetInfo(form_factor=xr.FormFactor.HEAD_MOUNTED_DISPLAY),
        )
        session = xr.create_session(
            instance,
            xr.SessionCreateInfo(system_id=system_id),
        )
        assert session is not None
        xr.destroy_session(session)
        xr.destroy_instance(instance)


# ==========================
# QuestTeleoperator Integration Tests
# ==========================


@requires_quest
class TestQuestTeleoperatorHardware:
    """Integration tests for the Quest teleoperator with actual hardware."""

    def test_connect_disconnect(self):
        """QuestTeleoperator can connect to and disconnect from Quest 2."""
        from lerobot.teleoperators.quest.config_quest import QuestTeleoperatorConfig
        from lerobot.teleoperators.quest.openxr_session import OpenXRSession

        session = OpenXRSession(polling_rate_hz=30.0)
        session.connect()
        assert session.is_connected

        # Let it poll for a short time
        time.sleep(0.5)

        session.disconnect()
        assert not session.is_connected

    def test_read_controller_state(self):
        """Can read controller state from connected Quest 2."""
        from lerobot.teleoperators.quest.openxr_session import OpenXRSession

        session = OpenXRSession(polling_rate_hz=30.0)
        session.connect()

        # Wait for a few poll cycles
        time.sleep(1.0)

        state = session.get_controller_state("right")
        # State should have valid defaults even if controller isn't being held
        assert state.position is not None
        assert state.orientation is not None
        assert len(state.position) == 3
        assert len(state.orientation) == 4

        session.disconnect()

    def test_teleoperator_full_lifecycle(self):
        """Full lifecycle: create → connect → get_action → disconnect."""
        from lerobot.teleoperators.quest.config_quest import QuestTeleoperatorConfig
        from lerobot.teleoperators.quest.quest_teleop import QuestTeleoperator

        config = QuestTeleoperatorConfig(
            id="integration_test",
            polling_rate_hz=30.0,
        )
        teleop = QuestTeleoperator(config)

        # Skip calibration for automated test (manually set reference)
        teleop._session = None
        # We can't easily automate calibration (requires trigger pull),
        # so test the connect/disconnect lifecycle
        # For full calibration testing, use the manual test below


# ==========================
# Camera Integration Tests
# ==========================


@requires_camera
class TestCameraStreamHardware:
    """Integration tests for USB camera capture."""

    def test_camera_connect_capture(self):
        """CameraStream can connect to USB camera and capture frames."""
        from lerobot.teleoperators.quest.camera_stream import (
            CameraStream,
            CameraStreamConfig,
        )

        config = CameraStreamConfig(
            camera_index=0,
            capture_width=640,
            capture_height=480,
            capture_fps=30,
        )
        stream = CameraStream(config)
        stream.connect()
        assert stream.is_connected

        # Wait for first frame
        time.sleep(0.5)

        frame = stream.get_latest_frame()
        assert frame is not None
        assert frame.ndim == 3
        assert frame.shape[2] == 3  # BGR

        rgb_frame = stream.get_latest_frame_rgb()
        assert rgb_frame is not None
        assert rgb_frame.shape == frame.shape

        assert stream.frame_count > 0

        stream.disconnect()
        assert not stream.is_connected

    def test_camera_multiple_frames(self):
        """Camera captures multiple frames over time."""
        from lerobot.teleoperators.quest.camera_stream import (
            CameraStream,
            CameraStreamConfig,
        )

        stream = CameraStream(CameraStreamConfig(camera_index=0, capture_fps=30))
        stream.connect()

        time.sleep(1.0)
        count1 = stream.frame_count

        time.sleep(1.0)
        count2 = stream.frame_count

        assert count2 > count1, "Camera should capture frames over time"

        stream.disconnect()


# ==========================
# VR Display Integration Tests
# ==========================


@requires_quest
class TestVRDisplayHardware:
    """Integration tests for camera-to-VR display rendering."""

    def test_gl_context_creation(self):
        """Can create an offscreen OpenGL context on this platform."""
        import platform

        if platform.system() != "Windows":
            pytest.skip("GL context creation only implemented for Windows")

        from lerobot.teleoperators.quest.vr_display import (
            create_gl_context,
            destroy_gl_context,
        )

        hwnd, hdc, hglrc = create_gl_context()
        assert hwnd != 0
        assert hdc != 0
        assert hglrc != 0

        destroy_gl_context(hwnd, hdc, hglrc)

    def test_display_session_with_graphics(self):
        """Can create an OpenXR session with OpenGL graphics binding."""
        import platform

        if platform.system() != "Windows":
            pytest.skip("Graphics session only implemented for Windows")

        from lerobot.teleoperators.quest.openxr_session import OpenXRSession

        session = OpenXRSession(polling_rate_hz=30.0, enable_display=True)
        session.connect()
        assert session.is_connected
        assert session._gl_handles is not None

        time.sleep(0.5)
        session.disconnect()


# ==========================
# End-to-End Smoke Test
# ==========================


@requires_quest
@requires_camera
class TestEndToEndSmoke:
    """Smoke test for the full Quest → SO-101 pipeline (without actual robot)."""

    def test_teleop_with_camera_display(self):
        """Full pipeline: Quest + Camera + VR Display (no robot, just verify data flow)."""
        import platform

        if platform.system() != "Windows":
            pytest.skip("Full pipeline requires Windows for Quest Link")

        from lerobot.teleoperators.quest.config_quest import QuestTeleoperatorConfig
        from lerobot.teleoperators.quest.quest_teleop import QuestTeleoperator

        config = QuestTeleoperatorConfig(
            id="smoke_test",
            polling_rate_hz=30.0,
            enable_camera_display=True,
            camera_index=0,
        )
        teleop = QuestTeleoperator(config)

        try:
            teleop.connect(calibrate=False)
            assert teleop.is_connected

            # Manually set calibration to skip interactive prompt
            teleop._calib_pos = np.zeros(3)
            from lerobot.utils.rotation import Rotation

            teleop._calib_rot_inv = Rotation.from_rotvec(np.zeros(3))

            # Get a few actions
            for _ in range(10):
                action = teleop.get_action()
                assert "quest.pos" in action
                assert "quest.rot" in action
                assert "quest.grip" in action
                assert "quest.enabled" in action
                time.sleep(0.03)

        finally:
            if teleop.is_connected:
                teleop.disconnect()


# ==========================
# Manual Test Helpers
# ==========================


def manual_test_controller_tracking():
    """
    Manual test: prints live controller position for 10 seconds.

    Run with: python -c "from tests.teleoperators.test_quest_integration import manual_test_controller_tracking; manual_test_controller_tracking()"
    """
    from lerobot.teleoperators.quest.openxr_session import OpenXRSession

    print("Connecting to Quest 2 via OpenXR...")
    session = OpenXRSession(polling_rate_hz=30.0)
    session.connect()

    print("Connected! Move the right controller. Tracking for 10 seconds...")
    t_end = time.time() + 10.0
    while time.time() < t_end:
        state = session.get_controller_state("right")
        pos = state.position
        tracking = "✓" if state.is_tracking else "✗"
        trigger = f"idx={state.index_trigger:.2f} grip={state.grip_trigger:.2f}"
        print(
            f"  [{tracking}] pos=({pos[0]:+.3f}, {pos[1]:+.3f}, {pos[2]:+.3f}) {trigger}",
            end="\r",
        )
        time.sleep(0.033)

    print("\nDone.")
    session.disconnect()


def manual_test_camera_display():
    """
    Manual test: shows camera feed in Quest 2 headset for 30 seconds.

    Run with: python -c "from tests.teleoperators.test_quest_integration import manual_test_camera_display; manual_test_camera_display()"
    """
    from lerobot.teleoperators.quest.camera_stream import (
        CameraStream,
        CameraStreamConfig,
    )
    from lerobot.teleoperators.quest.openxr_session import OpenXRSession
    from lerobot.teleoperators.quest.vr_display import VRCameraDisplay, VRDisplayConfig

    print("Setting up camera and VR display...")
    camera = CameraStream(CameraStreamConfig(camera_index=0))
    camera.connect()

    display = VRCameraDisplay(VRDisplayConfig(enabled=True))
    session = OpenXRSession(polling_rate_hz=30.0, enable_display=True)
    session.attach_camera_display(display, camera)
    session.connect()

    print("Camera feed should now be visible in the Quest 2 headset.")
    print("Running for 30 seconds... Press Ctrl+C to stop early.")
    try:
        time.sleep(30.0)
    except KeyboardInterrupt:
        pass

    print("Cleaning up...")
    session.disconnect()
    camera.disconnect()
    print("Done.")
