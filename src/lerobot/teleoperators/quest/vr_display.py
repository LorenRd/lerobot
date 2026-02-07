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
VR camera display for Meta Quest 2 headset via OpenXR quad composition layer.

Renders camera frames from an arm-mounted USB camera as a floating 2D panel
in the Quest 2 headset's field of view. This provides visual feedback to the
operator during teleoperation.

The display uses an OpenXR swapchain backed by OpenGL textures. Camera frames
are uploaded to the swapchain each render cycle and submitted as a quad
composition layer positioned in front of the user.

Requirements:
- pyopenxr >= 1.1.0
- PyOpenGL >= 3.1.0
- An active OpenXR session with OpenGL graphics binding
- Windows (Quest Link) or Linux (Monado)
"""

import ctypes
import logging
import platform
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class VRDisplayConfig:
    """Configuration for the VR camera display quad layer."""

    # Whether to enable the camera-to-VR display
    enabled: bool = False
    # Size of the virtual display panel in VR space (meters)
    display_width: float = 0.6
    display_height: float = 0.45
    # Distance from the user's head (meters, placed along -Z in view space)
    display_distance: float = 1.0
    # Vertical offset from eye level (meters, positive = up)
    display_offset_y: float = -0.2
    # Texture resolution (should match camera resolution for best quality)
    texture_width: int = 640
    texture_height: int = 480


# ---------------------------------------------------------------------------
# Platform-specific OpenGL context creation
# ---------------------------------------------------------------------------


def create_gl_context():
    """
    Create a minimal offscreen OpenGL context for the current platform.

    Returns platform-specific handles needed for OpenXR graphics binding.
    On Windows: (hwnd, hdc, hglrc)
    """
    system = platform.system()
    if system == "Windows":
        return _create_wgl_context()
    else:
        raise RuntimeError(
            f"VR display is not yet supported on {system}. "
            "Use Windows with Quest Link for camera-to-VR display."
        )


def release_gl_context_from_thread():
    """Release the GL context from the calling thread (for transfer to another thread)."""
    if platform.system() == "Windows":
        ctypes.windll.opengl32.wglMakeCurrent(None, None)


def make_gl_context_current(hdc, hglrc):
    """Make the GL context current on the calling thread."""
    if platform.system() == "Windows":
        if not ctypes.windll.opengl32.wglMakeCurrent(hdc, hglrc):
            raise RuntimeError("Failed to make WGL context current on thread")


def destroy_gl_context(hwnd, hdc, hglrc):
    """Destroy the GL context and associated window."""
    if platform.system() == "Windows":
        opengl32 = ctypes.windll.opengl32
        user32 = ctypes.windll.user32
        opengl32.wglMakeCurrent(None, None)
        opengl32.wglDeleteContext(hglrc)
        user32.ReleaseDC(hwnd, hdc)
        user32.DestroyWindow(hwnd)


class _PIXELFORMATDESCRIPTOR(ctypes.Structure):
    """Win32 PIXELFORMATDESCRIPTOR for WGL context creation."""

    _fields_ = [
        ("nSize", ctypes.c_ushort),
        ("nVersion", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("iPixelType", ctypes.c_ubyte),
        ("cColorBits", ctypes.c_ubyte),
        ("cRedBits", ctypes.c_ubyte),
        ("cRedShift", ctypes.c_ubyte),
        ("cGreenBits", ctypes.c_ubyte),
        ("cGreenShift", ctypes.c_ubyte),
        ("cBlueBits", ctypes.c_ubyte),
        ("cBlueShift", ctypes.c_ubyte),
        ("cAlphaBits", ctypes.c_ubyte),
        ("cAlphaShift", ctypes.c_ubyte),
        ("cAccumBits", ctypes.c_ubyte),
        ("cAccumRedBits", ctypes.c_ubyte),
        ("cAccumGreenBits", ctypes.c_ubyte),
        ("cAccumBlueBits", ctypes.c_ubyte),
        ("cAccumAlphaBits", ctypes.c_ubyte),
        ("cDepthBits", ctypes.c_ubyte),
        ("cStencilBits", ctypes.c_ubyte),
        ("cAuxBuffers", ctypes.c_ubyte),
        ("iLayerType", ctypes.c_ubyte),
        ("bReserved", ctypes.c_ubyte),
        ("dwLayerMask", ctypes.c_ulong),
        ("dwVisibleMask", ctypes.c_ulong),
        ("dwDamageMask", ctypes.c_ulong),
    ]


def _create_wgl_context():
    """Create an offscreen OpenGL context using WGL on Windows."""
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    opengl32 = ctypes.windll.opengl32

    PFD_DRAW_TO_WINDOW = 0x00000004
    PFD_SUPPORT_OPENGL = 0x00000020
    PFD_DOUBLEBUFFER = 0x00000001
    PFD_TYPE_RGBA = 0

    # Create a 1x1 hidden window for the GL context
    hwnd = user32.CreateWindowExW(
        0, "STATIC", "LeRobot VR Display", 0,
        0, 0, 1, 1, None, None, None, None,
    )
    if not hwnd:
        raise RuntimeError("Failed to create hidden window for GL context")

    hdc = user32.GetDC(hwnd)
    if not hdc:
        user32.DestroyWindow(hwnd)
        raise RuntimeError("Failed to get device context for GL window")

    pfd = _PIXELFORMATDESCRIPTOR()
    pfd.nSize = ctypes.sizeof(_PIXELFORMATDESCRIPTOR)
    pfd.nVersion = 1
    pfd.dwFlags = PFD_DRAW_TO_WINDOW | PFD_SUPPORT_OPENGL | PFD_DOUBLEBUFFER
    pfd.iPixelType = PFD_TYPE_RGBA
    pfd.cColorBits = 32
    pfd.cDepthBits = 24

    pixel_format = gdi32.ChoosePixelFormat(hdc, ctypes.byref(pfd))
    if not pixel_format:
        raise RuntimeError("Failed to choose pixel format for GL context")

    if not gdi32.SetPixelFormat(hdc, pixel_format, ctypes.byref(pfd)):
        raise RuntimeError("Failed to set pixel format for GL context")

    hglrc = opengl32.wglCreateContext(hdc)
    if not hglrc:
        raise RuntimeError("Failed to create WGL OpenGL context")

    if not opengl32.wglMakeCurrent(hdc, hglrc):
        opengl32.wglDeleteContext(hglrc)
        raise RuntimeError("Failed to activate WGL OpenGL context")

    logger.info("Created offscreen OpenGL context (WGL)")
    return hwnd, hdc, hglrc


# ---------------------------------------------------------------------------
# VR Camera Display
# ---------------------------------------------------------------------------


class VRCameraDisplay:
    """
    Renders camera frames as an OpenXR quad composition layer in VR space.

    Creates a floating 2D panel in front of the user's view showing the camera
    feed from an arm-mounted USB camera. Uses an OpenXR swapchain backed by
    OpenGL textures for efficient GPU-side frame upload and display.

    Usage:
        display = VRCameraDisplay(config)
        display.setup(xr_session, xr_space)
        # In render loop:
        layer = display.render_frame(camera_frame, predicted_display_time)
        # Pass layer to xr.end_frame(layers=[...])
        display.destroy()
    """

    def __init__(self, config: VRDisplayConfig | None = None):
        self.config = config or VRDisplayConfig()
        self._swapchain = None
        self._swapchain_images = []
        self._space = None
        self._is_setup = False

    @property
    def is_setup(self) -> bool:
        return self._is_setup

    def setup(self, xr_session, xr_space) -> None:
        """
        Create OpenXR swapchain for the camera display quad layer.

        Must be called after the OpenXR session with an OpenGL graphics binding
        is created, and with the GL context current on the calling thread.
        """
        try:
            import xr
        except ImportError:
            raise ImportError(
                "pyopenxr is required for VR display. "
                "Install with: pip install pyopenxr>=1.1.0"
            )

        try:
            from OpenGL import GL
        except ImportError:
            raise ImportError(
                "PyOpenGL is required for VR camera display. "
                "Install with: pip install PyOpenGL>=3.1.0"
            )

        self._space = xr_space

        # Standard OpenGL internal formats
        GL_RGBA8 = 0x8058
        GL_SRGB8_ALPHA8 = 0x8C43

        formats = xr.enumerate_swapchain_formats(xr_session)
        if GL_SRGB8_ALPHA8 in formats:
            chosen_format = GL_SRGB8_ALPHA8
        elif GL_RGBA8 in formats:
            chosen_format = GL_RGBA8
        elif formats:
            chosen_format = formats[0]
            logger.warning(f"Standard RGBA formats unavailable, using {chosen_format:#x}")
        else:
            raise RuntimeError("No swapchain formats available from OpenXR runtime")

        self._swapchain = xr.create_swapchain(
            xr_session,
            xr.SwapchainCreateInfo(
                usage_flags=(
                    xr.SwapchainUsageFlags.COLOR_ATTACHMENT_BIT
                    | xr.SwapchainUsageFlags.SAMPLED_BIT
                    | xr.SwapchainUsageFlags.TRANSFER_DST_BIT
                ),
                format=chosen_format,
                sample_count=1,
                width=self.config.texture_width,
                height=self.config.texture_height,
                face_count=1,
                array_size=1,
                mip_count=1,
            ),
        )

        # Get swapchain images (OpenGL texture names)
        self._swapchain_images = xr.enumerate_swapchain_images(
            self._swapchain, xr.SwapchainImageOpenGLKHR
        )

        # Initialize texture parameters for each swapchain image
        for img in self._swapchain_images:
            GL.glBindTexture(GL.GL_TEXTURE_2D, img.image)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)

        self._is_setup = True
        logger.info(
            f"VR camera display: {self.config.texture_width}x{self.config.texture_height}, "
            f"{len(self._swapchain_images)} swapchain images, format={chosen_format:#x}"
        )

    def render_frame(self, frame: np.ndarray, display_time) -> "xr.CompositionLayerQuad":
        """
        Upload a camera frame to the swapchain and return a quad composition layer.

        Args:
            frame: Camera frame as numpy array (H, W, 3) in BGR format.
            display_time: Predicted display time from xr.wait_frame().

        Returns:
            An xr.CompositionLayerQuad to submit in xr.end_frame() layers.
        """
        import xr
        from OpenGL import GL

        if not self._is_setup:
            raise RuntimeError("VR display not set up. Call setup() first.")

        # Acquire the next swapchain image
        index = xr.acquire_swapchain_image(
            self._swapchain, xr.SwapchainImageAcquireInfo()
        )
        xr.wait_swapchain_image(
            self._swapchain,
            xr.SwapchainImageWaitInfo(timeout=xr.INFINITE_DURATION),
        )

        tex_w = self.config.texture_width
        tex_h = self.config.texture_height

        # Resize if frame dimensions don't match texture
        if frame.shape[1] != tex_w or frame.shape[0] != tex_h:
            frame = cv2.resize(frame, (tex_w, tex_h))

        # Convert BGR → RGBA for OpenGL
        frame_rgba = cv2.cvtColor(frame, cv2.COLOR_BGR2RGBA)
        # Flip vertically (OpenGL textures are bottom-up, camera frames are top-down)
        frame_rgba = np.ascontiguousarray(np.flipud(frame_rgba))

        # Upload pixel data to the swapchain texture
        tex_id = self._swapchain_images[index].image
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex_id)
        GL.glTexSubImage2D(
            GL.GL_TEXTURE_2D, 0, 0, 0,
            tex_w, tex_h,
            GL.GL_RGBA, GL.GL_UNSIGNED_BYTE,
            frame_rgba,
        )
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)

        # Release the swapchain image back to the runtime
        xr.release_swapchain_image(
            self._swapchain, xr.SwapchainImageReleaseInfo()
        )

        # Build the quad composition layer
        layer = xr.CompositionLayerQuad(
            layer_flags=xr.CompositionLayerFlags.BLEND_TEXTURE_SOURCE_ALPHA_BIT,
            space=self._space,
            eye_visibility=xr.EyeVisibility.BOTH,
            sub_image=xr.SwapchainSubImage(
                swapchain=self._swapchain,
                image_rect=xr.Rect2Di(
                    offset=xr.Offset2Di(0, 0),
                    extent=xr.Extent2Di(tex_w, tex_h),
                ),
                image_array_index=0,
            ),
            pose=xr.Posef(
                orientation=xr.Quaternionf(0, 0, 0, 1),
                position=xr.Vector3f(
                    0,
                    self.config.display_offset_y,
                    -self.config.display_distance,
                ),
            ),
            size=xr.Extent2Df(
                self.config.display_width,
                self.config.display_height,
            ),
        )

        return layer

    def destroy(self) -> None:
        """Release OpenXR swapchain resources."""
        if self._swapchain is not None:
            try:
                import xr
                xr.destroy_swapchain(self._swapchain)
            except Exception:
                pass
            self._swapchain = None
        self._swapchain_images = []
        self._is_setup = False
        logger.info("VR camera display destroyed.")
