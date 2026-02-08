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

from dataclasses import dataclass

from ..configs import CameraConfig, ColorMode, Cv2Rotation


@CameraConfig.register_subclass("depthai")
@dataclass
class DepthAICameraConfig(CameraConfig):
    """Configuration class for Luxonis DepthAI cameras (OAK-D, OAK-D Lite, etc.).

    This class provides specialized configuration options for DepthAI cameras,
    including support for stereo depth sensing and device identification via MxID.

    Example configurations for OAK-D Lite:
    ```python
    # Basic RGB-only
    DepthAICameraConfig("18443010211F850E00", 30, 640, 480)

    # With depth sensing
    DepthAICameraConfig("18443010211F850E00", 30, 640, 480, use_depth=True)

    # Auto-detect single device
    DepthAICameraConfig("", 30, 640, 480)
    ```

    Attributes:
        fps: Requested frames per second for the color stream.
        width: Requested frame width in pixels for the color stream.
        height: Requested frame height in pixels for the color stream.
        device_id: MxID or IP address to identify the device. Empty string for auto-detect.
        color_mode: Color mode for image output (RGB or BGR). Defaults to RGB.
        use_depth: Whether to enable stereo depth stream. Defaults to False.
        rotation: Image rotation setting (0°, 90°, 180°, or 270°). Defaults to no rotation.
        warmup_s: Time reading frames before returning from connect (in seconds).
        depth_align_to_color: Whether to align depth to the RGB camera frame. Defaults to True.
        stereo_extended_disparity: Enable extended disparity for close-range depth. Defaults to True.
        stereo_lr_check: Enable left-right consistency check for better occlusion handling.
    """

    device_id: str = ""
    color_mode: ColorMode = ColorMode.RGB
    use_depth: bool = False
    rotation: Cv2Rotation = Cv2Rotation.NO_ROTATION
    warmup_s: int = 1
    depth_align_to_color: bool = True
    stereo_extended_disparity: bool = True
    stereo_lr_check: bool = True

    def __post_init__(self) -> None:
        if self.color_mode not in (ColorMode.RGB, ColorMode.BGR):
            raise ValueError(
                f"`color_mode` is expected to be {ColorMode.RGB.value} or {ColorMode.BGR.value}, "
                f"but {self.color_mode} is provided."
            )

        if self.rotation not in (
            Cv2Rotation.NO_ROTATION,
            Cv2Rotation.ROTATE_90,
            Cv2Rotation.ROTATE_180,
            Cv2Rotation.ROTATE_270,
        ):
            raise ValueError(
                f"`rotation` is expected to be in "
                f"{(Cv2Rotation.NO_ROTATION, Cv2Rotation.ROTATE_90, Cv2Rotation.ROTATE_180, Cv2Rotation.ROTATE_270)}, "
                f"but {self.rotation} is provided."
            )

        values = (self.fps, self.width, self.height)
        if any(v is not None for v in values) and any(v is None for v in values):
            raise ValueError(
                "For `fps`, `width` and `height`, either all of them need to be set, or none of them."
            )
