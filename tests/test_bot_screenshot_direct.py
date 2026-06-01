#!/usr/bin/env python3
"""Direct test: load the actual bot module and call _capture_window_screenshot
on a real hidden Win32 window to verify the fix works end-to-end.
"""

import ctypes
import ctypes.wintypes
import importlib.util
import sys
import time
from pathlib import Path

# Create a real hidden window first, before importing the bot module
# (the bot module sets DPI awareness which affects window coordinates).
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
CW_USEDEFAULT = 0x80000000
SW_HIDE = 0
SW_SHOW = 5

WNDPROCTYPE = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.wintypes.HWND, ctypes.c_uint,
    ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM
)

_defwndproc = ctypes.cast(user32.DefWindowProcW, ctypes.c_void_p).value

def create_test_window(visible=True):
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

    class_name = f"BotTestWnd_{int(time.time()*1000)}"
    wc = WNDCLASSEX()
    wc.cbSize = ctypes.sizeof(WNDCLASSEX)
    wc.lpfnWndProc = ctypes.cast(_defwndproc, WNDPROCTYPE)
    wc.hInstance = hinstance
    wc.lpszClassName = class_name
    user32.RegisterClassExW(ctypes.byref(wc))

    style = WS_OVERLAPPEDWINDOW
    if visible:
        style |= WS_VISIBLE

    hwnd = user32.CreateWindowExW(
        0, class_name, "Test Window", style,
        CW_USEDEFAULT, CW_USEDEFAULT, 800, 600,
        0, 0, hinstance, None
    )
    return hwnd


def main():
    print("=== Direct bot screenshot test ===\n")

    # Step 1: Create a hidden window (simulates the bug scenario)
    print("1. Creating test window...")
    hwnd = create_test_window(visible=True)
    assert hwnd, "Failed to create window"
    time.sleep(0.3)

    # Hide it
    user32.ShowWindow(hwnd, SW_HIDE)
    time.sleep(0.2)

    is_visible = bool(user32.IsWindowVisible(hwnd))
    is_iconic = bool(user32.IsIconic(hwnd))
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    print(f"   is_visible={is_visible}, is_iconic={is_iconic}, rect=({rect.left},{rect.top},{rect.right},{rect.bottom}), size=({w}x{h})")

    # Step 2: Load the bot module
    print("\n2. Loading bot module...")
    ROOT = Path(__file__).resolve().parents[1]
    MODULE_PATH = ROOT / "app" / "feishu_claude_bot.py"
    CONFIG_PATH = ROOT / "config" / "feishu_claude_bot.v2.json"

    spec = importlib.util.spec_from_file_location("feishu_bot_test", str(MODULE_PATH))
    if spec is None or spec.loader is None:
        print("   FAIL: cannot load module")
        return 1
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    print("   Module loaded OK")

    # Step 3: Create a bot instance
    print("\n3. Creating bot instance...")
    config = module.BotConfig.load(CONFIG_PATH)
    bot = module.FeishuClaudeBot(config)
    print("   Bot created OK")

    # Step 4: Monkey-patch _find_claude_terminal_hwnd to return our test window
    print("\n4. Patching _find_claude_terminal_hwnd to return test hwnd...")
    bot._find_claude_terminal_hwnd = lambda pid: hwnd
    print(f"   Patched, hwnd={hwnd}")

    # Step 5: Call _capture_window_screenshot
    print("\n5. Calling _capture_window_screenshot...")
    try:
        screenshot_path = bot._capture_window_screenshot(9999)
        print(f"   SUCCESS: screenshot saved to {screenshot_path}")

        # Verify the file exists and has content
        path = Path(screenshot_path)
        if path.exists() and path.stat().st_size > 0:
            print(f"   File size: {path.stat().st_size} bytes")
            print("\n=== TEST PASSED ===")
        else:
            print("   FAIL: screenshot file is empty or missing")
            return 1
    except RuntimeError as e:
        print(f"   FAIL: RuntimeError: {e}")
        return 1
    except Exception as e:
        print(f"   FAIL: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        user32.DestroyWindow(hwnd)

    return 0


if __name__ == "__main__":
    sys.exit(main())

