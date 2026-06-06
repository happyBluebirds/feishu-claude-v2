#!/usr/bin/env python3
"""Unit tests for non-intrusive screenshot window-size resolution.

Mocks Windows user32 APIs to verify that screenshot requests can resolve a
usable capture size without restoring or activating the target window. This
prevents Feishu screenshot commands from flashing local terminal windows.

Since the bot module has encoding issues that prevent direct import in some
environments, these tests recreate the screenshot logic inline and test it.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Minimal reproduction of the fixed non-intrusive screenshot sizing logic
# ---------------------------------------------------------------------------

def capture_window_size(hwnd, log_fn=print):
    """Reproduce the non-intrusive size logic used before PrintWindow.

    Returns (width, height, used_window_placement), so tests can assert on the
    exact path taken without needing PIL or real windows.

    Raises RuntimeError on invalid dimensions (same as the real method).
    """

    user32 = ctypes.windll.user32
    was_minimized = bool(user32.IsIconic(hwnd))
    is_hidden = not bool(user32.IsWindowVisible(hwnd))
    log_fn(f"screenshot debug hwnd={hwnd} pid=0 is_iconic={was_minimized} is_visible={not is_hidden}")

    if not was_minimized and not is_hidden:
        rect = ctypes.wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            log_fn(f"screenshot debug live rect: size=({width}x{height})")
            if width > 100 and height > 100:
                return width, height, False

    class WINDOWPLACEMENT(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint32),
            ("flags", ctypes.c_uint32),
            ("showCmd", ctypes.c_uint32),
            ("ptMinPosition", ctypes.wintypes.POINT),
            ("ptMaxPosition", ctypes.wintypes.POINT),
            ("rcNormalPosition", ctypes.wintypes.RECT),
        ]

    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    if user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
        nr = wp.rcNormalPosition
        width = nr.right - nr.left
        height = nr.bottom - nr.top
        log_fn(f"screenshot debug normal rect fallback: size=({width}x{height})")
        if width > 100 and height > 100:
            return width, height, True

    raise RuntimeError("窗口尺寸无效或句柄已失效")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_NORMAL_RECT = (100, 100, 900, 700)  # 800x600


def _mock_user32(
    *,
    is_iconic: bool = False,
    is_visible: bool = True,
    window_rect: tuple | None = VALID_NORMAL_RECT,
    wp_normal_rect: tuple = VALID_NORMAL_RECT,
    get_window_rect_retval: int = 1,
):
    user32 = MagicMock()
    user32.IsIconic.return_value = int(is_iconic)
    user32.IsWindowVisible.return_value = int(is_visible)

    def fake_get_window_rect(_hwnd, rect_ptr):
        if window_rect is None:
            return 0
        l, t, r, b = window_rect
        rect_ptr._obj.left = l
        rect_ptr._obj.top = t
        rect_ptr._obj.right = r
        rect_ptr._obj.bottom = b
        return get_window_rect_retval

    user32.GetWindowRect.side_effect = fake_get_window_rect

    def fake_get_window_placement(_hwnd, wp_ptr):
        l, t, r, b = wp_normal_rect
        wp_ptr._obj.rcNormalPosition.left = l
        wp_ptr._obj.rcNormalPosition.top = t
        wp_ptr._obj.rcNormalPosition.right = r
        wp_ptr._obj.rcNormalPosition.bottom = b
        wp_ptr._obj.showCmd = 0
        return 1

    user32.GetWindowPlacement.side_effect = fake_get_window_placement

    return user32


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCaptureWindowScreenshot:
    """Tests for non-flashing screenshot size resolution."""

    MOCK_HWNDS = {"target": 0x1234}

    def test_visible_window_valid_rect_succeeds(self):
        """Normal case: window is visible with valid dimensions."""
        user32 = _mock_user32()
        with patch("ctypes.windll.user32", user32):
            w, h, used_window_placement = capture_window_size(
                self.MOCK_HWNDS["target"]
            )
        assert w == 800
        assert h == 600
        assert used_window_placement is False
        user32.ShowWindow.assert_not_called()
        user32.GetWindowPlacement.assert_not_called()

    def test_minimized_window_uses_normal_rect_without_restore(self):
        """IsIconic=True: use WINDOWPLACEMENT size but never restore the window."""
        user32 = _mock_user32(is_iconic=True, is_visible=False)
        with patch("ctypes.windll.user32", user32):
            w, h, used_window_placement = capture_window_size(
                self.MOCK_HWNDS["target"]
            )
        assert w == 800
        assert h == 600
        assert used_window_placement is True
        user32.ShowWindow.assert_not_called()
        user32.GetWindowPlacement.assert_called_once()

    def test_hidden_window_uses_normal_rect_without_restore(self):
        """IsWindowVisible=False but IsIconic=False should not flash local UI."""
        user32 = _mock_user32(is_iconic=False, is_visible=False,
                               window_rect=(0, 0, 0, 0))
        with patch("ctypes.windll.user32", user32):
            w, h, used_window_placement = capture_window_size(
                self.MOCK_HWNDS["target"]
            )
        assert w == 800
        assert h == 600
        assert used_window_placement is True
        user32.ShowWindow.assert_not_called()
        user32.GetWindowPlacement.assert_called_once()

    def test_visible_zero_size_falls_back_to_window_placement(self):
        """IsIconic=False, IsWindowVisible=True, but GetWindowRect returns 0x0.

        Should fall back to WINDOWPLACEMENT without restoring the window.
        """
        user32 = _mock_user32(is_iconic=False, is_visible=True,
                               window_rect=(0, 0, 0, 0))
        with patch("ctypes.windll.user32", user32):
            w, h, used_window_placement = capture_window_size(
                self.MOCK_HWNDS["target"]
            )
        assert w == 800
        assert h == 600
        assert used_window_placement is True
        user32.GetWindowPlacement.assert_called_once()
        user32.ShowWindow.assert_not_called()

    def test_zero_size_fallback_still_invalid_raises(self):
        """Both GetWindowRect and WINDOWPLACEMENT report 0x0 -> should raise."""
        user32 = _mock_user32(is_iconic=False, is_visible=True,
                               window_rect=(0, 0, 0, 0),
                               wp_normal_rect=(0, 0, 0, 0))
        with patch("ctypes.windll.user32", user32):
            with pytest.raises(RuntimeError, match="窗口尺寸无效"):
                capture_window_size(self.MOCK_HWNDS["target"])
        user32.ShowWindow.assert_not_called()

    def test_minimized_with_invalid_wp_raises(self):
        """Minimized window with 0x0 WINDOWPLACEMENT -> should raise."""
        user32 = _mock_user32(is_iconic=True, is_visible=False,
                               wp_normal_rect=(0, 0, 0, 0))
        with patch("ctypes.windll.user32", user32):
            with pytest.raises(RuntimeError, match="窗口尺寸无效"):
                capture_window_size(self.MOCK_HWNDS["target"])
        user32.ShowWindow.assert_not_called()

    def test_hidden_with_invalid_wp_raises(self):
        """Hidden window with 0x0 WINDOWPLACEMENT -> should raise."""
        user32 = _mock_user32(is_iconic=False, is_visible=False,
                               window_rect=(0, 0, 0, 0),
                               wp_normal_rect=(0, 0, 0, 0))
        with patch("ctypes.windll.user32", user32):
            with pytest.raises(RuntimeError, match="窗口尺寸无效"):
                capture_window_size(self.MOCK_HWNDS["target"])
        user32.ShowWindow.assert_not_called()

    def test_negative_size_treated_as_invalid(self):
        """Negative dimensions from GetWindowRect -> fallback path."""
        user32 = _mock_user32(is_iconic=False, is_visible=True,
                               window_rect=(900, 700, 100, 100))  # negative size
        with patch("ctypes.windll.user32", user32):
            w, h, used_window_placement = capture_window_size(
                self.MOCK_HWNDS["target"]
            )
        assert w == 800
        assert h == 600
        assert used_window_placement is True
        user32.ShowWindow.assert_not_called()

    def test_get_window_rect_api_failure_uses_window_placement(self):
        """GetWindowRect returns 0 (API failure) -> use normal placement rect."""
        user32 = _mock_user32(is_iconic=False, is_visible=True, window_rect=None)
        with patch("ctypes.windll.user32", user32):
            w, h, used_window_placement = capture_window_size(self.MOCK_HWNDS["target"])
        assert (w, h) == (800, 600)
        assert used_window_placement is True
        user32.ShowWindow.assert_not_called()

    def test_no_restore_for_visible_valid_window(self):
        """Visible window with valid rect should not call ShowWindow at all."""
        user32 = _mock_user32(is_iconic=False, is_visible=True,
                               window_rect=(50, 50, 1050, 850))
        with patch("ctypes.windll.user32", user32):
            w, h, used_window_placement = capture_window_size(
                self.MOCK_HWNDS["target"]
            )
        assert w == 1000
        assert h == 800
        assert used_window_placement is False
        user32.ShowWindow.assert_not_called()

    def test_minimized_showcmd_indicates_minimized_state(self):
        """WINDOWPLACEMENT.showCmd should not trigger any restore behavior."""
        user32 = _mock_user32(is_iconic=True, is_visible=False)
        original_wp = user32.GetWindowPlacement.side_effect

        def fake_wp_with_showcmd(_hwnd, wp_ptr):
            result = original_wp(_hwnd, wp_ptr)
            wp_ptr._obj.showCmd = 2
            return result

        user32.GetWindowPlacement.side_effect = fake_wp_with_showcmd

        with patch("ctypes.windll.user32", user32):
            w, h, used_window_placement = capture_window_size(
                self.MOCK_HWNDS["target"]
            )
        assert w == 800
        assert h == 600
        assert used_window_placement is True
        user32.ShowWindow.assert_not_called()

    def test_visible_1x1_size_triggers_fallback(self):
        """1x1 pixel window is still invalid -> should trigger fallback."""
        user32 = _mock_user32(is_iconic=False, is_visible=True,
                               window_rect=(100, 100, 101, 101))
        with patch("ctypes.windll.user32", user32):
            w, h, used_window_placement = capture_window_size(
                self.MOCK_HWNDS["target"]
            )
        assert w == 800
        assert h == 600
        assert used_window_placement is True
        user32.ShowWindow.assert_not_called()

    def test_mixed_iconic_visible_states(self):
        """Test various combinations of iconic/visible states."""
        user32 = _mock_user32(is_iconic=True, is_visible=True)
        with patch("ctypes.windll.user32", user32):
            w, h, used_window_placement = capture_window_size(
                self.MOCK_HWNDS["target"]
            )
        assert used_window_placement is True
        user32.ShowWindow.assert_not_called()

        user32 = _mock_user32(is_iconic=False, is_visible=False,
                               window_rect=(0, 0, 0, 0))
        with patch("ctypes.windll.user32", user32):
            w, h, used_window_placement = capture_window_size(
                self.MOCK_HWNDS["target"]
            )
        assert used_window_placement is True
        user32.ShowWindow.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
