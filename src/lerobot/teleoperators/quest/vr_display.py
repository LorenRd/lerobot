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
from dataclasses import dataclass, field

import cv2
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class HudStatus:
    """Dynamic state passed to the VR HUD overlay each frame."""

    is_tracking: bool = False
    is_recording: bool = False
    episode: int = 0
    frame_count: int = 0
    grip_value: float = 0.0  # 0.0 (open) to 1.0 (closed)


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
    # Side-by-side depth display mode
    show_depth: bool = False
    # OpenCV colormap for depth visualization (COLORMAP_TURBO, COLORMAP_JET, etc.)
    depth_colormap: int = cv2.COLORMAP_TURBO
    # Show controls & status HUD overlay on the camera feed
    show_controls: bool = True
    # Control labels to display (list of "ICON  Label" strings)
    control_labels: list[str] = field(default_factory=lambda: [
        "Trigger  Track Arm",
        "Grip     Close Gripper",
        "A Btn    Save Episode",
        "B Btn    Discard",
    ])


# ---------------------------------------------------------------------------
# Platform-specific OpenGL context creation
# ---------------------------------------------------------------------------


def create_gl_context():
    """
    Create a minimal offscreen OpenGL context for the current platform.

    Uses GLFW for cross-platform GL context creation. Returns platform-specific
    handles needed for the OpenXR graphics binding.

    On Windows: (glfw_window, hdc, hglrc)
    """
    try:
        return _create_glfw_context()
    except Exception as e:
        logger.warning(f"GLFW context creation failed: {e}")
        system = platform.system()
        if system == "Windows":
            return _create_wgl_context()
        raise RuntimeError(
            f"Failed to create OpenGL context: {e}\n"
            "Ensure your GPU drivers are up to date."
        )


def release_gl_context_from_thread():
    """Release the GL context from the calling thread (for transfer to another thread)."""
    try:
        import glfw
        glfw.make_context_current(None)
    except Exception:
        if platform.system() == "Windows":
            ctypes.windll.opengl32.wglMakeCurrent(None, None)


def make_gl_context_current(hdc_or_window, hglrc=None):
    """Make the GL context current on the calling thread."""
    if hglrc is None:
        # hdc_or_window is a GLFW window
        import glfw
        glfw.make_context_current(hdc_or_window)
    elif platform.system() == "Windows":
        if not ctypes.windll.opengl32.wglMakeCurrent(hdc_or_window, hglrc):
            raise RuntimeError("Failed to make WGL context current on thread")


def destroy_gl_context(window_or_hwnd, hdc=None, hglrc=None):
    """Destroy the GL context and associated window."""
    if hdc is None and hglrc is None:
        # GLFW window
        try:
            import glfw
            glfw.destroy_window(window_or_hwnd)
            glfw.terminate()
        except Exception:
            pass
    elif platform.system() == "Windows":
        opengl32 = ctypes.windll.opengl32
        user32 = ctypes.windll.user32
        opengl32.wglMakeCurrent(None, None)
        opengl32.wglDeleteContext(hglrc)
        user32.ReleaseDC(window_or_hwnd, hdc)
        user32.DestroyWindow(window_or_hwnd)


def _create_glfw_context():
    """Create an offscreen OpenGL context using GLFW (cross-platform)."""
    import glfw

    if not glfw.init():
        raise RuntimeError("Failed to initialize GLFW")

    glfw.window_hint(glfw.VISIBLE, False)
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 4)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 1)

    window = glfw.create_window(1, 1, "LeRobot VR Display", None, None)
    if not window:
        glfw.terminate()
        raise RuntimeError("Failed to create GLFW window")

    glfw.make_context_current(window)

    if platform.system() == "Windows":
        # Extract Win32 handles for OpenXR graphics binding
        native_window = glfw.get_win32_window(window)
        hdc = ctypes.windll.user32.GetDC(native_window)
        hglrc = ctypes.windll.opengl32.wglGetCurrentContext()
        logger.info(f"Created OpenGL context via GLFW (WGL hdc={hdc}, hglrc={hglrc})")
        # Return GLFW window as first element (for cleanup), plus Win32 handles
        return window, hdc, hglrc
    else:
        logger.info("Created OpenGL context via GLFW")
        return (window, None, None)


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
# Depth visualization utilities
# ---------------------------------------------------------------------------


def colorize_depth(depth_frame: np.ndarray, colormap: int = cv2.COLORMAP_TURBO) -> np.ndarray:
    """
    Colorize a depth map for visualization.

    Args:
        depth_frame: Depth map as numpy array (H, W) uint16 in millimeters.
        colormap: OpenCV colormap constant (default: COLORMAP_TURBO).

    Returns:
        Colorized depth image as BGR numpy array (H, W, 3) uint8.
    """
    if depth_frame.ndim == 3 and depth_frame.shape[2] == 1:
        depth_frame = depth_frame[:, :, 0]

    invalid_mask = depth_frame == 0
    if depth_frame.dtype == np.uint16:
        # Normalize to 0-255 using percentile-based range for better contrast
        valid = depth_frame[~invalid_mask]
        if valid.size > 0:
            min_d = np.percentile(valid, 3)
            max_d = np.percentile(valid, 97)
            normalized = np.clip((depth_frame.astype(np.float32) - min_d) / max(max_d - min_d, 1), 0, 1)
            normalized = (normalized * 255).astype(np.uint8)
        else:
            normalized = np.zeros(depth_frame.shape, dtype=np.uint8)
    else:
        normalized = depth_frame.astype(np.uint8) if depth_frame.dtype != np.uint8 else depth_frame

    colorized = cv2.applyColorMap(normalized, colormap)
    colorized[invalid_mask] = 0  # Black for invalid/zero depth
    return colorized


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

    def render_frame(self, frame: np.ndarray, display_time, depth_frame: np.ndarray | None = None, hud_status: HudStatus | None = None) -> "xr.CompositionLayerQuad":
        """
        Upload a camera frame to the swapchain and return a quad composition layer.

        Args:
            frame: Camera frame as numpy array (H, W, 3) in BGR format.
            display_time: Predicted display time from xr.wait_frame().
            depth_frame: Optional depth map as numpy array (H, W) uint16 in millimeters.
                         When provided and show_depth is enabled, creates a side-by-side
                         RGB + colorized depth display.

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

        # Build the display frame (side-by-side if depth is available)
        if self.config.show_depth and depth_frame is not None:
            display_frame = self._compose_side_by_side(frame, depth_frame, tex_w, tex_h)
        else:
            # Resize if frame dimensions don't match texture
            if frame.shape[1] != tex_w or frame.shape[0] != tex_h:
                display_frame = cv2.resize(frame, (tex_w, tex_h))
            else:
                display_frame = frame

        # Draw HUD overlay (controls + status) on top of the display frame
        if self.config.show_controls:
            display_frame = self._draw_hud_overlay(display_frame, hud_status)

        # Convert BGR → RGBA for OpenGL
        frame_rgba = cv2.cvtColor(display_frame, cv2.COLOR_BGR2RGBA)
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

        # Determine display width (wider for side-by-side mode)
        actual_display_width = self.config.display_width
        if self.config.show_depth:
            actual_display_width = self.config.display_width * 2.0

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
                actual_display_width,
                self.config.display_height,
            ),
        )

        return layer

    def _draw_hud_overlay(
        self, frame: np.ndarray, status: HudStatus | None = None
    ) -> np.ndarray:
        """Draw a semi-transparent controls & status HUD overlay on the frame."""
        out = frame.copy()
        h, w = out.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX

        # --- Status bar (top) ---
        bar_h = 32
        overlay = out[:bar_h, :].copy()
        cv2.rectangle(out, (0, 0), (w, bar_h), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.3, out[:bar_h, :], 0.7, 0, out[:bar_h, :])

        if status is not None:
            # Tracking indicator
            track_color = (0, 255, 0) if status.is_tracking else (80, 80, 80)
            track_text = "TRACKING" if status.is_tracking else "PAUSED"
            cv2.circle(out, (16, bar_h // 2), 6, track_color, -1)
            cv2.putText(out, track_text, (28, bar_h - 10), font, 0.45, track_color, 1, cv2.LINE_AA)

            # Recording indicator
            if status.is_recording:
                cv2.circle(out, (w // 2 - 50, bar_h // 2), 6, (0, 0, 255), -1)
                cv2.putText(out, "REC", (w // 2 - 38, bar_h - 10), font, 0.45, (0, 0, 255), 1, cv2.LINE_AA)

            # Episode / frame count
            info = f"Ep {status.episode}  F {status.frame_count}"
            cv2.putText(out, info, (w - 180, bar_h - 10), font, 0.4, (200, 200, 200), 1, cv2.LINE_AA)

            # Grip bar
            grip_x = w // 2 + 30
            grip_bar_w = 60
            cv2.rectangle(out, (grip_x, 8), (grip_x + grip_bar_w, bar_h - 8), (80, 80, 80), 1)
            fill_w = int(grip_bar_w * status.grip_value)
            if fill_w > 0:
                cv2.rectangle(out, (grip_x, 8), (grip_x + fill_w, bar_h - 8), (0, 180, 255), -1)
            cv2.putText(out, "Grip", (grip_x + grip_bar_w + 4, bar_h - 10), font, 0.35, (180, 180, 180), 1, cv2.LINE_AA)
        else:
            cv2.putText(out, "LeRobot Quest Teleop", (8, bar_h - 10), font, 0.45, (200, 200, 200), 1, cv2.LINE_AA)

        # --- Control labels (bottom-left) ---
        labels = self.config.control_labels
        line_h = 20
        panel_h = len(labels) * line_h + 12
        panel_w = 220
        y0 = h - panel_h
        overlay_bot = out[y0:h, 0:panel_w].copy()
        cv2.rectangle(out, (0, y0), (panel_w, h), (0, 0, 0), -1)
        cv2.addWeighted(overlay_bot, 0.3, out[y0:h, 0:panel_w], 0.7, 0, out[y0:h, 0:panel_w])

        for i, label in enumerate(labels):
            y = y0 + 16 + i * line_h
            cv2.putText(out, label, (8, y), font, 0.35, (220, 220, 220), 1, cv2.LINE_AA)

        return out

    def _compose_side_by_side(
        self, rgb_frame: np.ndarray, depth_frame: np.ndarray, tex_w: int, tex_h: int
    ) -> np.ndarray:
        """Compose a side-by-side RGB + colorized depth frame for VR display."""
        half_w = tex_w // 2

        # Resize RGB to fit left half
        rgb_resized = cv2.resize(rgb_frame, (half_w, tex_h))

        # Colorize depth map
        depth_colorized = colorize_depth(depth_frame, self.config.depth_colormap)
        depth_resized = cv2.resize(depth_colorized, (half_w, tex_h))

        # Stack horizontally
        return np.hstack([rgb_resized, depth_resized])

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
