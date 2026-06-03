#!/usr/bin/env python3
"""Ensure the Feishu Claude long-connection bot is running when Claude hooks fire."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


INTEGRATION_ROOT = Path(__file__).resolve().parents[1]
CODEX_ROOT = INTEGRATION_ROOT.parents[1]
DEFAULT_CONFIG_PATH = INTEGRATION_ROOT / "config" / "feishu_claude_bot.v2.json"
DEFAULT_LOG_PATH = CODEX_ROOT / "outputs" / "feishu-claude-v2" / "logs" / "feishu-claude-autostart.log"


@dataclass
class AutoStartConfig:
    """Subset of bot config needed for startup checks and Feishu notices."""

    # Bot state path, used to find the most recently active chat for notifications.
    state_path: str
    # Python executable used to launch the bot and detached notice sender.
    python_path: str
    # Local autostart log path; defaults to outputs/feishu-claude-v2/logs/feishu-claude-autostart.log.
    log_path: str

    # Loaded from feishu_claude_bot.v2.json; PowerShell 7 avoids Windows PowerShell 5
    # startup/profile differences during Claude SessionStart hooks.
    pwsh_path: str

    @classmethod
    def load(cls, path: Path) -> "AutoStartConfig":
        """Load startup settings from the shared bot JSON config."""

        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            state_path=data["state_path"],
            python_path=data.get("python_path") or sys.executable,
            log_path=data.get("autostart_log_path") or str(DEFAULT_LOG_PATH),
            pwsh_path=data.get("pwsh_path") or "powershell",
        )


def log_event(log_path: Path, message: str) -> None:
    """Append a timestamped diagnostic line for SessionStart troubleshooting."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def is_bot_running(bot_script_path: Path, pwsh_path: str) -> bool:
    """Check whether the Feishu long-connection bot process is already alive."""

    bot_script_for_ps = str(bot_script_path).replace("'", "''")
    # 排除当前 hook 进程及其父进程，避免 CommandLine.Contains 自身路径导致假阳性。
    current_pid = os.getpid()
    command = [
        pwsh_path,
        "-NoProfile",
        "-Command",
        (
            f"$selfPid = {current_pid}; "
            "$parentPid = (Get-CimInstance Win32_Process -Filter \"ProcessId=$selfPid\").ParentProcessId; "
            "Get-CimInstance Win32_Process | "
            f"Where-Object {{ ([string]$_.CommandLine).Contains('{bot_script_for_ps}') -and "
            "$_.ProcessId -ne $selfPid -and $_.ProcessId -ne $parentPid } | "
            "Select-Object -First 1 -ExpandProperty ProcessId"
        ),
    ]
    result = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        # SessionStart 健康检查会在打开 Claude 窗口时执行；隐藏 pwsh 探测进程，避免桌面出现一闪而过的控制台窗口。
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    return bool((result.stdout or "").strip())


def is_bot_healthy(state_path: Path) -> bool:
    """Check if the bot process is actually responsive by reading its health timestamp."""

    health_path = Path(state_path).parent / "feishu-claude-bot-health.json"
    if not health_path.exists():
        return False
    try:
        data = json.loads(health_path.read_text(encoding="utf-8"))
        timestamp = float(data.get("timestamp", 0))
        # 健康检查时间戳超过 90 秒未更新，认为机器人已失去响应。
        return (time.time() - timestamp) < 90
    except Exception:
        return False


def get_latest_chat_id(state_path: Path) -> str:
    """Return the most recently active Feishu chat id from bot state."""

    if not state_path.exists():
        return ""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return ""

    latest_chat_id = ""
    latest_ts = -1.0
    for chat_id, chat_state in data.get("chats", {}).items():
        candidate_ts = float(chat_state.get("finished_at") or chat_state.get("started_at") or 0)
        if candidate_ts >= latest_ts:
            latest_ts = candidate_ts
            latest_chat_id = str(chat_id)
    return latest_chat_id


def launch_notice_sender(config_path: Path, chat_id: str, text: str, log_path: Path, python_path: str) -> None:
    """Send a startup notice through a helper process with a bounded timeout."""

    if not chat_id:
        log_event(log_path, "skip SessionStart notice: missing latest chat id")
        return
    notice_script = INTEGRATION_ROOT / "hooks" / "feishu_claude_autostart_notice.py"
    bootstrap_script = INTEGRATION_ROOT / "app" / "bootstrap_feishu_tool.py"
    if not notice_script.exists():
        log_event(log_path, f"skip SessionStart notice: missing script {notice_script}")
        return
    if not bootstrap_script.exists():
        log_event(log_path, f"skip SessionStart notice: missing bootstrap {bootstrap_script}")
        return

    result = subprocess.run(
        [python_path, str(bootstrap_script), str(notice_script), str(config_path), chat_id, text],
        cwd=str(INTEGRATION_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=12,
        # SessionStart notice 是后台飞书通知，不能在用户桌面弹出 Python 控制台窗口。
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    if result.returncode == 0:
        log_event(log_path, f"SessionStart notice helper completed chat={chat_id}")
    else:
        log_event(
            log_path,
            f"SessionStart notice helper failed chat={chat_id} code={result.returncode} stderr={(result.stderr or '').strip()}",
        )


def ensure_bot_running(config_path: Path) -> None:
    """Start the Feishu bot lazily so Claude startup can recover the bridge automatically."""

    script_dir = INTEGRATION_ROOT
    bot_script_path = script_dir / "app" / "feishu_claude_bot.py"
    bootstrap_script = script_dir / "app" / "bootstrap_feishu_tool.py"
    if not bot_script_path.exists():
        return
    if not bootstrap_script.exists():
        return
    config = AutoStartConfig.load(config_path)
    log_path = Path(config.log_path)
    state_path = Path(config.state_path)
    bot_was_running = is_bot_running(bot_script_path, config.pwsh_path)
    bot_is_healthy = bot_was_running and is_bot_healthy(state_path)

    if not bot_is_healthy:
        # 进程不存在或健康检查超时，都需要重新拉起。
        if bot_was_running:
            log_event(log_path, "bot process running but unhealthy; restarting")
        creation_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        # SessionStart runs on Claude window creation; keep bot startup detached so
        # the Claude UI is not blocked by a long-running Feishu connection process.
        subprocess.Popen(
            [
                config.python_path,
                str(bootstrap_script),
                str(bot_script_path),
                "--config",
                str(config_path),
            ],
            cwd=str(script_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        log_event(log_path, "bot process was not running or unhealthy; attempted detached startup")
    else:
        log_event(log_path, "bot process already running and healthy")

    latest_chat_id = get_latest_chat_id(state_path)
    notice = "Claude 本地会话已启动，飞书助手已在线。" if bot_is_healthy else "Claude 本地会话已启动，飞书助手已自动拉起。"
    launch_notice_sender(config_path, latest_chat_id, notice, log_path, config.python_path)


def main() -> int:
    """Run as a quiet hook: do the startup check, print nothing, and exit 0."""

    config_path = DEFAULT_CONFIG_PATH
    if config_path.exists():
        ensure_bot_running(config_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

