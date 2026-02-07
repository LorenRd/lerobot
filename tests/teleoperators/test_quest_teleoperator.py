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

"""Tests for the Quest VR controller teleoperator."""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from lerobot.teleoperators.quest.config_quest import (
    ControllerHand,
    QuestTeleoperatorConfig,
)
from lerobot.teleoperators.quest.coordinate_transform import (
    vr_to_robot_position,
    vr_to_robot_rotation,
    vr_to_robot_rotvec,
)
from lerobot.teleoperators.quest.openxr_session import ControllerState
from lerobot.teleoperators.quest.vr_display import VRDisplayConfig
from lerobot.utils.rotation import Rotation


# ========================
# Config Tests
# ========================


class TestQuestTeleoperatorConfig:
    def test_default_config(self):
        config = QuestTeleoperatorConfig()
        assert config.controller_hand == ControllerHand.RIGHT
        assert config.position_scale == 1.0
        assert config.rotation_scale == 1.0
        assert config.noise_threshold == 0.001
        assert config.smoothing_alpha == 0.2
        assert config.gripper_threshold == 0.5
        assert config.clutch_threshold == 0.3
        assert config.polling_rate_hz == 90.0

    def test_custom_config(self):
        config = QuestTeleoperatorConfig(
            id="test_quest",
            controller_hand=ControllerHand.LEFT,
            position_scale=2.0,
            smoothing_alpha=0.5,
        )
        assert config.id == "test_quest"
        assert config.controller_hand == ControllerHand.LEFT
        assert config.position_scale == 2.0
        assert config.smoothing_alpha == 0.5

    def test_config_type(self):
        config = QuestTeleoperatorConfig()
        assert config.type == "quest"

    def test_camera_display_config_defaults(self):
        config = QuestTeleoperatorConfig()
        assert config.enable_camera_display is False
        assert config.camera_index == 0
        assert config.camera_width == 640
        assert config.camera_height == 480
        assert config.camera_fps == 30
        assert config.vr_display_width == 0.6
        assert config.vr_display_height == 0.45
        assert config.vr_display_distance == 1.0
        assert config.vr_display_offset_y == -0.2

    def test_camera_display_config_custom(self):
        config = QuestTeleoperatorConfig(
            enable_camera_display=True,
            camera_index=1,
            camera_width=1280,
            camera_height=720,
            vr_display_distance=1.5,
        )
        assert config.enable_camera_display is True
        assert config.camera_index == 1
        assert config.camera_width == 1280
        assert config.camera_height == 720
        assert config.vr_display_distance == 1.5


# ========================
# VR Display Config Tests
# ========================


class TestVRDisplayConfig:
    def test_default_config(self):
        config = VRDisplayConfig()
        assert config.enabled is False
        assert config.display_width == 0.6
        assert config.display_height == 0.45
        assert config.display_distance == 1.0
        assert config.display_offset_y == -0.2
        assert config.texture_width == 640
        assert config.texture_height == 480

    def test_custom_config(self):
        config = VRDisplayConfig(
            enabled=True,
            display_width=0.8,
            display_distance=1.5,
            texture_width=1280,
            texture_height=720,
        )
        assert config.enabled is True
        assert config.display_width == 0.8
        assert config.display_distance == 1.5
        assert config.texture_width == 1280
        assert config.texture_height == 720


# ========================
# Coordinate Transform Tests
# ========================


class TestCoordinateTransform:
    def test_vr_to_robot_position_identity(self):
        """Origin maps to origin."""
        vr_pos = np.array([0.0, 0.0, 0.0])
        robot_pos = vr_to_robot_position(vr_pos)
        np.testing.assert_array_almost_equal(robot_pos, [0.0, 0.0, 0.0])

    def test_vr_to_robot_position_axes(self):
        """
        VR: X=right, Y=up, Z=backward
        Robot: X=forward, Y=left, Z=up
        Mapping: robot_x = -vr_z, robot_y = -vr_x, robot_z = vr_y
        """
        # VR right → Robot -Y (left)
        vr_pos = np.array([1.0, 0.0, 0.0])
        robot_pos = vr_to_robot_position(vr_pos)
        np.testing.assert_array_almost_equal(robot_pos, [0.0, -1.0, 0.0])

        # VR up → Robot Z (up)
        vr_pos = np.array([0.0, 1.0, 0.0])
        robot_pos = vr_to_robot_position(vr_pos)
        np.testing.assert_array_almost_equal(robot_pos, [0.0, 0.0, 1.0])

        # VR backward → Robot -X (forward when negated)
        vr_pos = np.array([0.0, 0.0, 1.0])
        robot_pos = vr_to_robot_position(vr_pos)
        np.testing.assert_array_almost_equal(robot_pos, [-1.0, 0.0, 0.0])

    def test_vr_to_robot_rotation_identity(self):
        """Identity rotation stays identity."""
        vr_rot = Rotation.from_rotvec(np.zeros(3))
        robot_rot = vr_to_robot_rotation(vr_rot)
        np.testing.assert_array_almost_equal(
            robot_rot.as_matrix(), np.eye(3), decimal=10
        )

    def test_vr_to_robot_rotvec_zero(self):
        """Zero rotation vector stays zero."""
        vr_rotvec = np.array([0.0, 0.0, 0.0])
        robot_rotvec = vr_to_robot_rotvec(vr_rotvec)
        np.testing.assert_array_almost_equal(robot_rotvec, [0.0, 0.0, 0.0])


# ========================
# ControllerState Tests
# ========================


class TestControllerState:
    def test_default_state(self):
        state = ControllerState()
        np.testing.assert_array_equal(state.position, [0, 0, 0])
        np.testing.assert_array_equal(state.orientation, [0, 0, 0, 1])
        assert state.grip_trigger == 0.0
        assert state.index_trigger == 0.0
        assert state.button_a is False
        assert state.button_b is False
        assert state.is_tracking is False

    def test_custom_state(self):
        state = ControllerState(
            position=np.array([1.0, 2.0, 3.0]),
            grip_trigger=0.8,
            index_trigger=0.6,
            button_a=True,
            is_tracking=True,
        )
        np.testing.assert_array_equal(state.position, [1.0, 2.0, 3.0])
        assert state.grip_trigger == 0.8
        assert state.index_trigger == 0.6
        assert state.button_a is True
        assert state.is_tracking is True


# ========================
# QuestTeleoperator Tests (with mocked OpenXR)
# ========================


class TestQuestTeleoperator:
    def _make_mock_session(self):
        """Create a mock OpenXR session that returns controllable states."""
        mock_session = MagicMock()
        mock_session.is_connected = True
        mock_session.get_controller_state.return_value = ControllerState(
            position=np.array([0.1, 0.5, -0.3]),
            orientation=np.array([0.0, 0.0, 0.0, 1.0]),
            grip_trigger=0.0,
            index_trigger=0.0,
            button_a=False,
            button_b=False,
            is_tracking=True,
            timestamp=1.0,
        )
        return mock_session

    @patch("lerobot.teleoperators.quest.quest_teleop.OpenXRSession")
    def test_connect_disconnect(self, MockSession):
        MockSession.return_value = self._make_mock_session()

        config = QuestTeleoperatorConfig(id="test")
        teleop = _create_teleop_with_mock(config, MockSession.return_value)

        assert teleop.is_connected
        teleop.disconnect()
        assert not teleop.is_connected

    @patch("lerobot.teleoperators.quest.quest_teleop.OpenXRSession")
    def test_get_action_not_tracking(self, MockSession):
        mock_session = self._make_mock_session()
        mock_session.get_controller_state.return_value = ControllerState(is_tracking=False)
        MockSession.return_value = mock_session

        config = QuestTeleoperatorConfig(id="test")
        teleop = _create_teleop_with_mock(config, mock_session)

        action = teleop.get_action()
        assert action["quest.enabled"] is False

    @patch("lerobot.teleoperators.quest.quest_teleop.OpenXRSession")
    def test_get_action_clutch_disabled(self, MockSession):
        """When index trigger is not pulled, tracking should be disabled."""
        mock_session = self._make_mock_session()
        # index_trigger = 0 (below threshold)
        MockSession.return_value = mock_session

        config = QuestTeleoperatorConfig(id="test")
        teleop = _create_teleop_with_mock(config, mock_session)

        action = teleop.get_action()
        assert action["quest.enabled"] is False

    @patch("lerobot.teleoperators.quest.quest_teleop.OpenXRSession")
    def test_get_action_clutch_enabled(self, MockSession):
        """When index trigger is pulled, tracking should be enabled."""
        mock_session = self._make_mock_session()
        mock_session.get_controller_state.return_value = ControllerState(
            position=np.array([0.1, 0.5, -0.3]),
            orientation=np.array([0.0, 0.0, 0.0, 1.0]),
            grip_trigger=0.0,
            index_trigger=0.8,  # Above clutch_threshold
            is_tracking=True,
            timestamp=1.0,
        )
        MockSession.return_value = mock_session

        config = QuestTeleoperatorConfig(id="test")
        teleop = _create_teleop_with_mock(config, mock_session)

        action = teleop.get_action()
        assert action["quest.enabled"] is True

    @patch("lerobot.teleoperators.quest.quest_teleop.OpenXRSession")
    def test_action_features(self, MockSession):
        config = QuestTeleoperatorConfig(id="test")
        from lerobot.teleoperators.quest.quest_teleop import QuestTeleoperator

        teleop = QuestTeleoperator(config)
        features = teleop.action_features
        assert "quest.pos" in features
        assert "quest.rot" in features
        assert "quest.grip" in features
        assert "quest.enabled" in features
        assert "quest.buttons" in features

    @patch("lerobot.teleoperators.quest.quest_teleop.OpenXRSession")
    def test_teleop_events_default(self, MockSession):
        """Teleop events should return safe defaults when not connected."""
        from lerobot.teleoperators.quest.quest_teleop import QuestTeleoperator
        from lerobot.teleoperators.utils import TeleopEvents

        config = QuestTeleoperatorConfig(id="test")
        teleop = QuestTeleoperator(config)

        events = teleop.get_teleop_events()
        assert events[TeleopEvents.IS_INTERVENTION] is False
        assert events[TeleopEvents.SUCCESS] is False
        assert events[TeleopEvents.RERECORD_EPISODE] is False


# ========================
# Processor Tests
# ========================


class TestMapQuestActionToRobotAction:
    def test_disabled_action(self):
        from lerobot.teleoperators.quest.quest_processor import MapQuestActionToRobotAction

        processor = MapQuestActionToRobotAction()
        action = {
            "quest.pos": np.zeros(3),
            "quest.rot": Rotation.from_rotvec(np.zeros(3)),
            "quest.grip": 0.0,
            "quest.enabled": False,
            "quest.buttons": {},
        }
        result = processor.action(action)
        assert result["enabled"] is False
        assert result["target_x"] == 0.0
        assert result["target_y"] == 0.0
        assert result["target_z"] == 0.0

    def test_enabled_action(self):
        from lerobot.teleoperators.quest.quest_processor import MapQuestActionToRobotAction

        processor = MapQuestActionToRobotAction(apply_coordinate_transform=False)
        action = {
            "quest.pos": np.array([0.1, 0.2, 0.3]),
            "quest.rot": Rotation.from_rotvec(np.zeros(3)),
            "quest.grip": 0.0,
            "quest.enabled": True,
            "quest.buttons": {},
        }
        result = processor.action(action)
        assert result["enabled"] is True
        assert abs(result["target_x"] - 0.1) < 1e-6
        assert abs(result["target_y"] - 0.2) < 1e-6
        assert abs(result["target_z"] - 0.3) < 1e-6

    def test_gripper_close(self):
        from lerobot.teleoperators.quest.quest_processor import MapQuestActionToRobotAction

        processor = MapQuestActionToRobotAction(gripper_threshold=0.5)
        action = {
            "quest.pos": np.zeros(3),
            "quest.rot": Rotation.from_rotvec(np.zeros(3)),
            "quest.grip": 0.9,  # Above threshold
            "quest.enabled": True,
            "quest.buttons": {},
        }
        result = processor.action(action)
        assert result["gripper_vel"] == -1.0  # Close

    def test_gripper_open(self):
        from lerobot.teleoperators.quest.quest_processor import MapQuestActionToRobotAction

        processor = MapQuestActionToRobotAction(gripper_threshold=0.5)
        action = {
            "quest.pos": np.zeros(3),
            "quest.rot": Rotation.from_rotvec(np.zeros(3)),
            "quest.grip": 0.1,  # Below threshold
            "quest.enabled": True,
            "quest.buttons": {},
        }
        result = processor.action(action)
        assert result["gripper_vel"] == 1.0  # Open

    def test_quest_keys_removed(self):
        from lerobot.teleoperators.quest.quest_processor import MapQuestActionToRobotAction

        processor = MapQuestActionToRobotAction()
        action = {
            "quest.pos": np.zeros(3),
            "quest.rot": Rotation.from_rotvec(np.zeros(3)),
            "quest.grip": 0.0,
            "quest.enabled": True,
            "quest.buttons": {},
        }
        result = processor.action(action)
        # Quest-specific keys should be consumed
        assert "quest.pos" not in result
        assert "quest.rot" not in result
        assert "quest.grip" not in result
        assert "quest.enabled" not in result
        # Standard keys should be present
        assert "enabled" in result
        assert "target_x" in result
        assert "gripper_vel" in result


# ========================
# Helpers
# ========================


def _create_teleop_with_mock(config, mock_session):
    """Create a QuestTeleoperator with a mocked OpenXR session (skip actual connect)."""
    from lerobot.teleoperators.quest.quest_teleop import QuestTeleoperator

    teleop = QuestTeleoperator(config)
    teleop._session = mock_session
    # Set calibration to pass is_calibrated check
    teleop._calib_pos = np.zeros(3)
    teleop._calib_rot_inv = Rotation.from_rotvec(np.zeros(3))
    return teleop
