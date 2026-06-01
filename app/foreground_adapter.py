#!/usr/bin/env python3
"""Foreground window adapter for the v2 Feishu Claude bot."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from session_manager import SessionManager


class ForegroundAdapter:
    """Resolve foreground Claude process/window state for UI automation.

    The first v2 migration keeps low-level Win32 functions in the legacy bot
    class, but moves the policy for stale PID recovery into this adapter.
    """

    def __init__(
        self,
        session_manager: SessionManager,
        process_exists: Callable[[Any], bool],
        find_all_hwnds: Callable[[int], list[int]],
        find_existing_launcher_pid: Callable[[dict[str, Any]], int | None],
        ensure_binding: Callable[[str, dict[str, Any]], dict[str, Any]],
        find_realtime_screenshot_target: Callable[[dict[str, Any]], tuple[int, list[int]]],
        send_command_fn: Callable[[int, str], None],
        send_hotkey_fn: Callable[[int, str], None],
        log_fn: Callable[[str], None],
    ) -> None:
        """Create an adapter around legacy Win32 primitives.

        Args:
            session_manager: State writer facade used when rebinding foreground PID.
            process_exists: Callback that checks whether a PID is still alive.
            find_all_hwnds: Callback returning visible terminal HWNDs for one PID.
            find_existing_launcher_pid: Callback that scans current processes for a Claude launcher.
            ensure_binding: Callback that restores watcher binding for a known PID.
            find_realtime_screenshot_target: Callback that finds a visible Claude window without trusting saved state.
            send_command_fn: Low-level callback that activates a window and pastes text.
            send_hotkey_fn: Low-level callback that activates a window and sends one hotkey.
            log_fn: Diagnostic logger supplied by the host bot.
        """

        self.session_manager = session_manager
        self.process_exists = process_exists
        self.find_all_hwnds = find_all_hwnds
        self.find_existing_launcher_pid = find_existing_launcher_pid
        self.ensure_binding = ensure_binding
        self.find_realtime_screenshot_target = find_realtime_screenshot_target
        self.send_command_fn = send_command_fn
        self.send_hotkey_fn = send_hotkey_fn
        self.log = log_fn

    def send_command(self, pid: int, command_text: str) -> None:
        """Send one text command into an already managed foreground window.

        Args:
            pid: Managed foreground launcher/window process id.
            command_text: Text that should be pasted and submitted.

        Returns:
            None. Any activation or paste failure is raised by the low-level callback.
        """

        # 前台文本注入统一经过适配层，后续替换 Win32 实现时不再牵动业务状态机。
        self.send_command_fn(pid, command_text)

    def send_hotkey(self, pid: int, hotkey: str) -> None:
        """Send one supported hotkey into an already managed foreground window.

        Args:
            pid: Managed foreground launcher/window process id.
            hotkey: Human-readable hotkey name, such as `shift+tab`.

        Returns:
            None. Any activation or send failure is raised by the low-level callback.
        """

        # 热键和文本注入共享同一前台边界，避免命令路由层直接依赖 Windows 细节。
        self.send_hotkey_fn(pid, hotkey)

    def resolve_screenshot_windows(
        self,
        chat_id: str,
        chat_state: dict[str, Any],
    ) -> tuple[dict[str, Any], int, list[int]]:
        """Resolve the foreground PID and visible terminal windows for screenshots.

        Args:
            chat_id: Feishu chat id.
            chat_state: Current merged chat/session state.

        Returns:
            Refreshed chat state, selected PID, and screenshot-able window handles.
        """

        chat_state = self.ensure_binding(chat_id, chat_state)
        active_pid = int(chat_state.get("foreground_pid") or chat_state.get("active_pid") or 0)
        if not active_pid or not self.process_exists(active_pid):
            live_hwnds = self.find_all_hwnds(0)
            if live_hwnds:
                # state 可能刚切 v2 或被旧版本写坏；截图以实时窗口为准，避免有窗口却误报找不到。
                return chat_state, -1, live_hwnds
            return chat_state, 0, []

        hwnds = self.find_all_hwnds(active_pid)
        if hwnds:
            return chat_state, active_pid, hwnds

        realtime_pid, realtime_hwnds = self.find_realtime_screenshot_target(chat_state)
        if realtime_pid and realtime_hwnds:
            # 截图前以实时可见窗口为准；state 里的 PID 只能作为候选，不能作为截图事实源。
            updated_state = (
                self.session_manager.update_foreground_binding(chat_id, realtime_pid)
                if realtime_pid > 0
                else chat_state
            )
            self.log(
                f"foreground adapter realtime screenshot target chat={chat_id} "
                f"old_pid={active_pid} pid={realtime_pid} hwnds={len(realtime_hwnds)}"
            )
            return updated_state, realtime_pid, realtime_hwnds

        live_hwnds = self.find_all_hwnds(0)
        if live_hwnds:
            # 已保存 PID 没有 HWND 时，回退到实时 Claude 终端扫描，兼容 Windows Terminal 父子进程变化。
            return chat_state, active_pid, live_hwnds

        detected_pid = self.find_existing_launcher_pid(chat_state)
        if not detected_pid or detected_pid == active_pid or not self.process_exists(detected_pid):
            return chat_state, active_pid, []

        rebound_hwnds = self.find_all_hwnds(detected_pid)
        if not rebound_hwnds:
            return chat_state, active_pid, []

        # 旧 PID 存活但没有窗口句柄时，截图/热键都会失败；这里由前台适配层统一重绑。
        updated_state = self.session_manager.update_foreground_binding(chat_id, detected_pid)
        self.log(
            f"foreground adapter rebound chat={chat_id} old_pid={active_pid} pid={detected_pid} hwnds={len(rebound_hwnds)}"
        )
        return updated_state, detected_pid, rebound_hwnds
