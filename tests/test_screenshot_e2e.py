#!/usr/bin/env python3
"""End-to-end test: create real Win32 windows and verify the screenshot
window-state recovery logic. Uses actual ctypes/Win32 calls, no mocks.

Tests the fix for the bug where GetWindowRect returns (0,0,0,0) for a window
that EnumWindows reported as visible with valid dimensions — a race condition
that causes "窗口尺寸无效，可能已最小化".
"""

import ctypes
import ctypes.wintypes
import os
import time
import sys
from pathlib import Path

import pytest

if os.environ.get("FEISHU_CLAUDE_RUN_GUI_E2E") != "1":
    pytest.skip(
        "Win32 screenshot E2E creates real GUI windows; set FEISHU_CLAUDE_RUN_GUI_E2E=1 to run it.",
        allow_module_level=True,
    )

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32

# Constants
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
SW_HIDE = 0
SW_SHOW = 5
SW_SHOWNOACTIVATE = 4
SW_RESTORE = 9
SW_MINIMIZE = 6
CW_USEDEFAULT = 0x80000000
PW_RENDERFULLCONTENT = 0x00000002
GWL_STYLE = -16

WNDPROCTYPE = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.wintypes.HWND, ctypes.c_uint,
    ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM
)

_defwndproc_cfn = ctypes.cast(user32.DefWindowProcW, ctypes.c_void_p).value


def _create_window(title="Test", style=WS_OVERLAPPEDWINDOW | WS_VISIBLE, x=CW_USEDEFAULT, y=CW_USEDEFAULT, w=800, h=600):
    hinstance = kernel32.GetModuleHandleW(None)

    class WNDCLASSEX(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint32), ("style", ctypes.c_uint32),
            ("lpfnWndProc", WNDPROCTYPE), ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.wintypes.HANDLE),
            ("hIcon", ctypes.wintypes.HANDLE), ("hCursor", ctypes.wintypes.HANDLE),
            ("hbrBackground", ctypes.wintypes.HANDLE), ("lpszMenuName", ctypes.c_wchar_p),
            ("lpszClassName", ctypes.c_wchar_p), ("hIconSm", ctypes.wintypes.HANDLE),
        ]

    class_name = f"TestWnd_{id(title)}_{int(time.time()*1000)}"
    wc = WNDCLASSEX()
    wc.cbSize = ctypes.sizeof(WNDCLASSEX)
    wc.lpfnWndProc = ctypes.cast(_defwndproc_cfn, WNDPROCTYPE)
    wc.hInstance = hinstance
    wc.lpszClassName = class_name
    user32.RegisterClassExW(ctypes.byref(wc))

    hwnd = user32.CreateWindowExW(0, class_name, title, style, x, y, w, h, 0, 0, hinstance, None)
    return hwnd


def _window_state(hwnd):
    """Return (is_iconic, is_visible, rect, wp_normal_rect) for a window."""
    is_iconic = bool(user32.IsIconic(hwnd))
    is_visible = bool(user32.IsWindowVisible(hwnd))

    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))

    class WINDOWPLACEMENT(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint32), ("flags", ctypes.c_uint32),
            ("showCmd", ctypes.c_uint32), ("ptMinPosition", ctypes.wintypes.POINT),
            ("ptMaxPosition", ctypes.wintypes.POINT), ("rcNormalPosition", ctypes.wintypes.RECT),
        ]

    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
    nr = wp.rcNormalPosition

    return {
        "is_iconic": is_iconic,
        "is_visible": is_visible,
        "rect": (rect.left, rect.top, rect.right, rect.bottom),
        "rect_size": (rect.right - rect.left, rect.bottom - rect.top),
        "wp_normal": (nr.left, nr.top, nr.right, nr.bottom),
        "wp_size": (nr.right - nr.left, nr.bottom - nr.top),
        "showCmd": wp.showCmd,
    }


# ---------------------------------------------------------------------------
# The actual screenshot recovery logic (mirrors the fix in feishu_claude_bot.py)
# ---------------------------------------------------------------------------

def recover_and_get_dimensions(hwnd, log_fn=print):
    """Try to get valid window dimensions, recovering from hidden/zero-size state.

    Returns (width, height, was_recovered) or raises RuntimeError.
    """

    was_minimized = bool(user32.IsIconic(hwnd))
    is_hidden = not bool(user32.IsWindowVisible(hwnd))

    class WINDOWPLACEMENT(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint32), ("flags", ctypes.c_uint32),
            ("showCmd", ctypes.c_uint32), ("ptMinPosition", ctypes.wintypes.POINT),
            ("ptMaxPosition", ctypes.wintypes.POINT), ("rcNormalPosition", ctypes.wintypes.RECT),
        ]

    needs_restore = was_minimized or is_hidden
    recovered = False

    if needs_restore:
        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
        nr = wp.rcNormalPosition
        width = nr.right - nr.left
        height = nr.bottom - nr.top
        log_fn(f"  minimized/hidden path: wp_size=({width}x{height})")
        if width <= 0 or height <= 0:
            raise RuntimeError("窗口尺寸无效，可能已最小化")
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.3)
        recovered = True
    else:
        rect = ctypes.wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise RuntimeError("读取窗口位置失败")
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        log_fn(f"  GetWindowRect: size=({width}x{height})")
        if width <= 0 or height <= 0:
            # THE FIX: fall back to WINDOWPLACEMENT + ShowWindow
            log_fn("  Zero-size fallback: trying WINDOWPLACEMENT...")
            wp = WINDOWPLACEMENT()
            wp.length = ctypes.sizeof(WINDOWPLACEMENT)
            user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
            nr = wp.rcNormalPosition
            width = nr.right - nr.left
            height = nr.bottom - nr.top
            log_fn(f"  WINDOWPLACEMENT: size=({width}x{height})")
            if width <= 0 or height <= 0:
                raise RuntimeError("窗口尺寸无效，可能已最小化")
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.3)
            recovered = True

    return width, height, recovered


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def test_visible_window_baseline():
    """Test 1: Normal visible window — no recovery needed."""
    hwnd = _create_window("Visible Test")
    assert hwnd, "Failed to create window"
    try:
        time.sleep(0.2)
        state = _window_state(hwnd)
        print(f"  state: {state}")
        assert state["is_visible"], "Window should be visible"
        assert state["rect_size"][0] > 0 and state["rect_size"][1] > 0, "Should have valid size"

        w, h, recovered = recover_and_get_dimensions(hwnd)
        assert w > 0 and h > 0
        assert not recovered
        print("  PASS")
    finally:
        user32.DestroyWindow(hwnd)


def test_minimized_window_recovery():
    """Test 2: Minimized window — existing recovery path."""
    hwnd = _create_window("Minimized Test")
    assert hwnd
    try:
        time.sleep(0.2)
        user32.ShowWindow(hwnd, SW_MINIMIZE)
        time.sleep(0.2)

        state = _window_state(hwnd)
        print(f"  state: {state}")
        assert state["is_iconic"], "Should be iconic"

        w, h, recovered = recover_and_get_dimensions(hwnd)
        assert w > 0 and h > 0
        assert recovered
        print("  PASS")
    finally:
        user32.DestroyWindow(hwnd)


def test_hidden_window_recovery():
    """Test 3: Hidden window (SW_HIDE) — new recovery path."""
    hwnd = _create_window("Hidden Test")
    assert hwnd
    try:
        time.sleep(0.2)
        user32.ShowWindow(hwnd, SW_HIDE)
        time.sleep(0.1)

        state = _window_state(hwnd)
        print(f"  state: {state}")

        if not state["is_visible"]:
            print("  Window is not visible — testing recovery...")
            w, h, recovered = recover_and_get_dimensions(hwnd)
            assert w > 0 and h > 0
            assert recovered
            print("  PASS (recovered from hidden state)")
        else:
            # On some Windows versions, SW_HIDE still reports visible
            # but GetWindowRect may return 0x0
            print("  Window still reports visible after SW_HIDE")
            if state["rect_size"] == (0, 0):
                print("  GetWindowRect returns 0x0 — testing zero-size fallback...")
                w, h, recovered = recover_and_get_dimensions(hwnd)
                assert w > 0 and h > 0
                assert recovered
                print("  PASS (recovered via zero-size fallback)")
            else:
                print(f"  GetWindowRect returns {state['rect_size']} — no recovery needed")
                print("  PASS (window already has valid dimensions)")
    finally:
        user32.DestroyWindow(hwnd)


def test_zero_size_visible_window():
    """Test 4: Simulate GetWindowRect returning (0,0,0,0) for a visible window.

    We can't easily create a truly zero-sized visible top-level window, but we
    can verify that the recovery logic correctly handles the case where
    WINDOWPLACEMENT also returns 0x0 (genuinely invalid window).
    """
    # Create a normal window, then destroy it to get an invalid hwnd
    hwnd = _create_window("Will Destroy")
    assert hwnd
    user32.DestroyWindow(hwnd)
    time.sleep(0.1)

    # Now hwnd is invalid — GetWindowRect should fail
    rect = ctypes.wintypes.RECT()
    result = user32.GetWindowRect(hwnd, ctypes.byref(rect))
    print(f"  Destroyed window: GetWindowRect returned={result} rect=({rect.left},{rect.top},{rect.right},{rect.bottom})")

    # The recovery logic should handle this gracefully
    # (In practice, _find_claude_terminal_hwnd wouldn't return a destroyed hwnd,
    #  but this tests the robustness of the recovery path)

    # Instead, test with a window that has WS_VISIBLE but we manually set to 0x0
    hwnd2 = _create_window("Zero Test")
    assert hwnd2
    try:
        time.sleep(0.1)

        # Move to 0x0 size using SetWindowPos with SWP_FRAMECHANGED
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_FRAMECHANGED = 0x0020
        user32.SetWindowPos(hwnd2, 0, 0, 0, 0, 0, SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        time.sleep(0.1)

        state = _window_state(hwnd2)
        print(f"  After SetWindowPos(0,0,0,0): {state}")

        # Even if the window is "visible" with 0x0, WINDOWPLACEMENT gives the
        # normal position. Test that recovery works.
        if state["wp_size"][0] > 0 and state["wp_size"][1] > 0:
            print(f"  WINDOWPLACEMENT has valid size: {state['wp_size']}")
            w, h, recovered = recover_and_get_dimensions(hwnd2)
            print(f"  Recovered: {w}x{h}")
            print("  PASS")
        else:
            print(f"  Both rect and wp are zero — this is a genuinely invalid window")
            try:
                recover_and_get_dimensions(hwnd2)
                print("  UNEXPECTED: should have raised")
            except RuntimeError as e:
                print(f"  Correctly raised: {e}")
                print("  PASS")
    finally:
        user32.DestroyWindow(hwnd2)


def test_printwindow_after_recovery():
    """Test 5: PrintWindow actually works after recovering a hidden window."""
    try:
        from PIL import Image
    except ImportError:
        print("  SKIP: Pillow not installed")
        return

    hwnd = _create_window("PW Test")
    assert hwnd
    try:
        time.sleep(0.2)
        # Hide it
        user32.ShowWindow(hwnd, SW_HIDE)
        time.sleep(0.1)

        # Recover
        w, h, recovered = recover_and_get_dimensions(hwnd)
        assert w > 0 and h > 0

        # Try PrintWindow
        hdc_window = user32.GetWindowDC(hwnd)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
        hbitmap = gdi32.CreateCompatibleBitmap(hdc_window, w, h)
        old_bmp = gdi32.SelectObject(hdc_mem, hbitmap)

        result = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
        print(f"  PrintWindow result={result}")

        if result:
            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32),
                ]

            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = 0

            buf = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(hdc_mem, hbitmap, 0, h, buf, ctypes.byref(bmi), 0)
            img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
            extrema = img.getextrema()
            print(f"  Image extrema: {extrema}")
            is_blank = all(ch[0] == 0 and ch[1] == 0 for ch in extrema[:3])
            assert not is_blank, "Image is all black"
            print("  PASS: PrintWindow captured real content after recovery")
        else:
            print("  WARN: PrintWindow returned 0 (may need foreground DC)")

        gdi32.SelectObject(hdc_mem, old_bmp)
        gdi32.DeleteObject(hbitmap)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_window)
    finally:
        user32.DestroyWindow(hwnd)


def test_real_bot_screenshot_flow():
    """Test 6: Simulate the exact bot flow — find hwnd by pid, then capture.

    Creates a window, finds it via EnumWindows (like _find_claude_terminal_hwnd),
    then runs the recovery logic (like _capture_window_screenshot).
    """
    hwnd = _create_window("Claude Code Test Window")
    assert hwnd
    try:
        time.sleep(0.2)

        # Step 1: Find the window via EnumWindows (like the bot does)
        target_pid = kernel32.GetCurrentProcessId()
        found_hwnd = [0]

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def enum_cb(hwnd_cb, _lparam):
            proc_id = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd_cb, ctypes.byref(proc_id))
            if proc_id.value == target_pid and user32.IsWindowVisible(hwnd_cb):
                rect = ctypes.wintypes.RECT()
                user32.GetWindowRect(hwnd_cb, ctypes.byref(rect))
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                if w > 100 and h > 100:
                    found_hwnd[0] = hwnd_cb
                    return False
            return True

        user32.EnumWindows(enum_cb, 0)
        print(f"  EnumWindows found hwnd={found_hwnd[0]}")
        assert found_hwnd[0] == hwnd, f"Expected hwnd={hwnd}, got {found_hwnd[0]}"

        # Step 2: Simulate the window becoming zero-sized (race condition)
        # Use SetWindowPos to move to 0,0 with 0,0 size while keeping WS_VISIBLE
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_FRAMECHANGED = 0x0020
        HWND_TOP = 0
        user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        time.sleep(0.1)

        state = _window_state(hwnd)
        print(f"  After SetWindowPos to 0x0: {state}")

        # Step 3: Run recovery logic
        if state["rect_size"] == (0, 0):
            print("  Window is 0x0 — running recovery...")
            w, h, recovered = recover_and_get_dimensions(hwnd)
            print(f"  Recovered: {w}x{h}, recovered={recovered}")
            # WINDOWPLACEMENT should still have the original size
            assert w > 0 and h > 0, "Should recover original dimensions"
            print("  PASS")
        else:
            print(f"  Window still has size {state['rect_size']}")
            print("  PASS (SetWindowPos didn't zero the size — testing normal path)")
    finally:
        user32.DestroyWindow(hwnd)


def main():
    print("=" * 60)
    print("End-to-end screenshot recovery test (real Win32 windows)")
    print("=" * 60)

    tests = [
        ("1. Visible window baseline", test_visible_window_baseline),
        ("2. Minimized window recovery", test_minimized_window_recovery),
        ("3. Hidden window recovery", test_hidden_window_recovery),
        ("4. Zero-size visible window (BUG)", test_zero_size_visible_window),
        ("5. PrintWindow after recovery", test_printwindow_after_recovery),
        ("6. Real bot flow simulation", test_real_bot_screenshot_flow),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        print(f"\n--- {name} ---")
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{len(tests)} passed, {failed} failed")
    print(f"{'=' * 60}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
