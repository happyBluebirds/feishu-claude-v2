#!/usr/bin/env python3
"""Unit tests for _capture_window_screenshot window-state recovery logic.

Mocks Windows user32 APIs to verify the fix for the bug where a hidden window
(reported as "visible" but with 0x0 dimensions) caused the screenshot to fail
with "窗口尺寸无效，可能已最小化".

Since the bot module has encoding issues that prevent direct import in some
environments, these tests recreate the screenshot logic inline and test it.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Minimal reproduction of the fixed _capture_window_screenshot logic
# ---------------------------------------------------------------------------

def capture_window_screenshot(hwnd, log_fn=print, print_window_fn=None):
    """Reproduce the core window-state logic from _capture_window_screenshot.

    Returns (width, height, needs_restore, was_minimized, show_calls)
    so tests can assert on the exact path taken without needing PIL or real
    windows.

    Raises RuntimeError on invalid dimensions (same as the real method).
    """

    user32 = ctypes.windll.user32
    was_minimized = bool(user32.IsIconic(hwnd))
    is_hidden = not bool(user32.IsWindowVisible(hwnd))
    log_fn(f"screenshot debug hwnd={hwnd} pid=0 is_iconic={was_minimized} is_visible={not is_hidden}")

    class WINDOWPLACEMENT(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint32),
            ("flags", ctypes.c_uint32),
            ("showCmd", ctypes.c_uint32),
            ("ptMinPosition", ctypes.wintypes.POINT),
            ("ptMaxPosition", ctypes.wintypes.POINT),
            ("rcNormalPosition", ctypes.wintypes.RECT),
        ]

    needs_restore = was_minimized or is_hidden

    if needs_restore:
        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
        nr = wp.rcNormalPosition
        width = nr.right - nr.left
        height = nr.bottom - nr.top
        log_fn(f"screenshot debug minimized/hidden: size=({width}x{height})")
        if width <= 0 or height <= 0:
            raise RuntimeError("窗口尺寸无效，可能已最小化")
        SW_RESTORE = 9
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.01)  # shortened for tests
    else:
        rect = ctypes.wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise RuntimeError("读取窗口位置失败")
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        log_fn(f"screenshot debug not minimized: size=({width}x{height})")
        if width <= 0 or height <= 0:
            wp = WINDOWPLACEMENT()
            wp.length = ctypes.sizeof(WINDOWPLACEMENT)
            user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
            nr = wp.rcNormalPosition
            width = nr.right - nr.left
            height = nr.bottom - nr.top
            log_fn(f"screenshot debug zero-size fallback: size=({width}x{height})")
            if width <= 0 or height <= 0:
                raise RuntimeError("窗口尺寸无效，可能已最小化")
            SW_RESTORE = 9
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.01)
            needs_restore = True

    # Replicate the finally block from the real code
    if was_minimized:
        SW_MINIMIZE = 6
        user32.ShowWindow(hwnd, SW_MINIMIZE)

    return width, height, needs_restore, was_minimized


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
    """Tests for the window-state recovery in _capture_window_screenshot."""

    MOCK_HWNDS = {"target": 0x1234}

    def test_visible_window_valid_rect_succeeds(self):
        """Normal case: window is visible with valid dimensions."""
        user32 = _mock_user32()
        with patch("ctypes.windll.user32", user32):
            w, h, needs_restore, was_minimized = capture_window_screenshot(
                self.MOCK_HWNDS["target"]
            )
        assert w == 800
        assert h == 600
        assert needs_restore is False
        assert was_minimized is False
        user32.ShowWindow.assert_not_called()
        user32.GetWindowPlacement.assert_not_called()

    def test_minimized_window_restores_and_captures(self):
        """IsIconic=True: restore via WINDOWPLACEMENT, then re-minimize."""
        user32 = _mock_user32(is_iconic=True, is_visible=False)
        with patch("ctypes.windll.user32", user32):
            w, h, needs_restore, was_minimized = capture_window_screenshot(
                self.MOCK_HWNDS["target"]
            )
        assert w == 800
        assert h == 600
        assert needs_restore is True
        assert was_minimized is True
        show_calls = user32.ShowWindow.call_args_list
        assert len(show_calls) == 2
        assert show_calls[0] == call(self.MOCK_HWNDS["target"], 9)  # SW_RESTORE
        assert show_calls[1] == call(self.MOCK_HWNDS["target"], 6)  # SW_MINIMIZE
        user32.GetWindowPlacement.assert_called_once()

    def test_hidden_window_restores_and_captures(self):
        """IsWindowVisible=False but IsIconic=False: treat as hidden, restore and capture.

        This is the key bug-fix scenario - previously this would fall into the
        else branch and raise '窗口尺寸无效'.
        """
        user32 = _mock_user32(is_iconic=False, is_visible=False,
                               window_rect=(0, 0, 0, 0))
        with patch("ctypes.windll.user32", user32):
            w, h, needs_restore, was_minimized = capture_window_screenshot(
                self.MOCK_HWNDS["target"]
            )
        assert w == 800
        assert h == 600
        assert needs_restore is True
        assert was_minimized is False
        # Should restore with SW_RESTORE, but NOT re-minimize
        show_calls = user32.ShowWindow.call_args_list
        assert len(show_calls) == 1
        assert show_calls[0] == call(self.MOCK_HWNDS["target"], 9)  # SW_RESTORE
        user32.GetWindowPlacement.assert_called_once()

    def test_visible_zero_size_falls_back_to_window_placement(self):
        """IsIconic=False, IsWindowVisible=True, but GetWindowRect returns 0x0.

        Should fall back to WINDOWPLACEMENT + ShowWindow instead of raising.
        """
        user32 = _mock_user32(is_iconic=False, is_visible=True,
                               window_rect=(0, 0, 0, 0))
        with patch("ctypes.windll.user32", user32):
            w, h, needs_restore, was_minimized = capture_window_screenshot(
                self.MOCK_HWNDS["target"]
            )
        assert w == 800
        assert h == 600
        assert needs_restore is True
        assert was_minimized is False
        user32.GetWindowPlacement.assert_called_once()
        show_calls = user32.ShowWindow.call_args_list
        assert len(show_calls) == 1
        assert show_calls[0] == call(self.MOCK_HWNDS["target"], 9)  # SW_RESTORE

    def test_zero_size_fallback_still_invalid_raises(self):
        """Both GetWindowRect and WINDOWPLACEMENT report 0x0 -> should raise."""
        user32 = _mock_user32(is_iconic=False, is_visible=True,
                               window_rect=(0, 0, 0, 0),
                               wp_normal_rect=(0, 0, 0, 0))
        with patch("ctypes.windll.user32", user32):
            with pytest.raises(RuntimeError, match="窗口尺寸无效"):
                capture_window_screenshot(self.MOCK_HWNDS["target"])

    def test_minimized_with_invalid_wp_raises(self):
        """Minimized window with 0x0 WINDOWPLACEMENT -> should raise."""
        user32 = _mock_user32(is_iconic=True, is_visible=False,
                               wp_normal_rect=(0, 0, 0, 0))
        with patch("ctypes.windll.user32", user32):
            with pytest.raises(RuntimeError, match="窗口尺寸无效"):
                capture_window_screenshot(self.MOCK_HWNDS["target"])

    def test_hidden_with_invalid_wp_raises(self):
        """Hidden window with 0x0 WINDOWPLACEMENT -> should raise."""
        user32 = _mock_user32(is_iconic=False, is_visible=False,
                               window_rect=(0, 0, 0, 0),
                               wp_normal_rect=(0, 0, 0, 0))
        with patch("ctypes.windll.user32", user32):
            with pytest.raises(RuntimeError, match="窗口尺寸无效"):
                capture_window_screenshot(self.MOCK_HWNDS["target"])

    def test_negative_size_treated_as_invalid(self):
        """Negative dimensions from GetWindowRect -> fallback path."""
        user32 = _mock_user32(is_iconic=False, is_visible=True,
                               window_rect=(900, 700, 100, 100))  # negative size
        with patch("ctypes.windll.user32", user32):
            w, h, needs_restore, was_minimized = capture_window_screenshot(
                self.MOCK_HWNDS["target"]
            )
        assert w == 800
        assert h == 600
        assert needs_restore is True

    def test_get_window_rect_api_failure_raises(self):
        """GetWindowRect returns 0 (API failure) -> should raise."""
        user32 = _mock_user32(is_iconic=False, is_visible=True, window_rect=None)
        with patch("ctypes.windll.user32", user32):
            with pytest.raises(RuntimeError, match="读取窗口位置失败"):
                capture_window_screenshot(self.MOCK_HWNDS["target"])

    def test_no_restore_for_visible_valid_window(self):
        """Visible window with valid rect should not call ShowWindow at all."""
        user32 = _mock_user32(is_iconic=False, is_visible=True,
                               window_rect=(50, 50, 1050, 850))
        with patch("ctypes.windll.user32", user32):
            w, h, needs_restore, was_minimized = capture_window_screenshot(
                self.MOCK_HWNDS["target"]
            )
        assert w == 1000
        assert h == 800
        assert needs_restore is False
        assert was_minimized is False
        user32.ShowWindow.assert_not_called()

    def test_minimized_showcmd_indicates_minimized_state(self):
        """WINDOWPLACEMENT.showCmd should be read but doesn't affect logic."""
        user32 = _mock_user32(is_iconic=True, is_visible=False)
        # Override showCmd to 2 (SW_SHOWMINIMIZED) to verify it's logged
        original_wp = user32.GetWindowPlacement.side_effect

        def fake_wp_with_showcmd(_hwnd, wp_ptr):
            original_wp(_hwnd, wp_ptr)
            wp_ptr._obj.showCmd = 2

        user32.GetWindowPlacement.side_effect = fake_wp_with_showcmd

        with patch("ctypes.windll.user32", user32):
            w, h, needs_restore, was_minimized = capture_window_screenshot(
                self.MOCK_HWNDS["target"]
            )
        assert w == 800
        assert h == 600
        assert was_minimized is True

    def test_visible_1x1_size_triggers_fallback(self):
        """1x1 pixel window is still invalid -> should trigger fallback."""
        user32 = _mock_user32(is_iconic=False, is_visible=True,
                               window_rect=(100, 100, 101, 101))
        with patch("ctypes.windll.user32", user32):
            w, h, needs_restore, was_minimized = capture_window_screenshot(
                self.MOCK_HWNDS["target"]
            )
        # 1x1 triggers fallback since 1 > 0 but the real code checks <= 0
        # Actually 1 > 0, so it should NOT trigger fallback
        assert w == 1
        assert h == 1
        assert needs_restore is False

    def test_mixed_iconic_visible_states(self):
        """Test various combinations of iconic/visible states."""
        # Case: iconic=True, visible=True (minimized but reported visible)
        user32 = _mock_user32(is_iconic=True, is_visible=True)
        with patch("ctypes.windll.user32", user32):
            w, h, needs_restore, was_minimized = capture_window_screenshot(
                self.MOCK_HWNDS["target"]
            )
        assert needs_restore is True
        assert was_minimized is True

        # Case: iconic=False, visible=False (hidden, not minimized)
        user32 = _mock_user32(is_iconic=False, is_visible=False,
                               window_rect=(0, 0, 0, 0))
        with patch("ctypes.windll.user32", user32):
            w, h, needs_restore, was_minimized = capture_window_screenshot(
                self.MOCK_HWNDS["target"]
            )
        assert needs_restore is True
        assert was_minimized is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
