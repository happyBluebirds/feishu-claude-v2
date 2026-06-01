#!/usr/bin/env python3
"""Test screenshot on a REAL Claude window — bypasses the bot entirely.

Loads the bot module, finds an actual Claude terminal window via
_find_claude_terminal_hwnd, and calls _capture_window_screenshot.
"""

import importlib.util
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "app" / "feishu_claude_bot.py"
CONFIG_PATH = ROOT / "config" / "feishu_claude_bot.v2.json"


def main():
    print("=== Real Claude window screenshot test ===\n")

    # Load bot module
    print("1. Loading bot module...")
    spec = importlib.util.spec_from_file_location("feishu_claude_bot", str(MODULE_PATH))
    module = importlib.util.module_from_spec(spec)
    sys.modules["feishu_claude_bot"] = module
    spec.loader.exec_module(module)
    print("   OK")

    # Create bot
    print("\n2. Creating bot...")
    config = module.BotConfig.load(CONFIG_PATH)
    bot = module.FeishuClaudeBot(config)
    print("   OK")

    # Find a real Claude window
    print("\n3. Finding Claude terminal window...")
    import ctypes
    # Try each known Claude PID
    for pid in [36688, 29968, 39032]:
        hwnd = bot._find_claude_terminal_hwnd(pid)
        if hwnd:
            # Check window state
            user32 = ctypes.windll.user32
            is_visible = bool(user32.IsWindowVisible(hwnd))
            is_iconic = bool(user32.IsIconic(hwnd))
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            print(f"   Found hwnd={hwnd} for pid={pid}: visible={is_visible} iconic={is_iconic} size=({w}x{h})")

            if w > 0 and h > 0:
                print(f"\n4. Taking screenshot of pid={pid} hwnd={hwnd}...")
                try:
                    path = bot._capture_window_screenshot(pid)
                    size = Path(path).stat().st_size
                    print(f"   SUCCESS: {path} ({size} bytes)")
                    return 0
                except Exception as e:
                    print(f"   FAIL: {e}")
            else:
                print(f"   Window has 0x0 size, trying anyway...")
                try:
                    path = bot._capture_window_screenshot(pid)
                    size = Path(path).stat().st_size
                    print(f"   SUCCESS (recovered): {path} ({size} bytes)")
                    return 0
                except Exception as e:
                    print(f"   FAIL: {e}")
        else:
            print(f"   No window found for pid={pid}")

    # Also try the stale PID from state to test the new error message
    print("\n5. Testing stale PID 36044 (should give clear error)...")
    try:
        path = bot._capture_window_screenshot(36044)
        print(f"   UNEXPECTED SUCCESS: {path}")
    except RuntimeError as e:
        print(f"   Got expected error: {e}")
        if "已退出" in str(e):
            print("   PASS: stale PID detected correctly")
        else:
            print("   WARN: error message could be clearer")

    return 1


if __name__ == "__main__":
    sys.exit(main())

