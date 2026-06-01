#!/usr/bin/env python3
"""前台 Claude 窗口观察器。

这个脚本会盯住一个已托管的前台 PowerShell 窗口，判断 Claude 子进程是否开始/结束，
并从 JSONL 或 transcript 中提取阶段摘要，再调用兜底返回脚本补齐飞书通知。
它只用于前台窗口链路，目的是让"窗口还在但本轮已结束"的情况也能被状态页感知。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from feishu_claude_turn_hook import DEFAULT_CONFIG_PATH, apply_chat_runtime_updates, load_state, log_event, save_state


POLL_INTERVAL_SECONDS = 3
WATCH_TIMEOUT_SECONDS = 24 * 60 * 60
GENERIC_ASSISTANT_IDLE_SECONDS = 15
# 飞书命令注入长驻 claude.exe 时子进程不会重启；用 state.started_at 识别新一轮需容忍毫秒级写入误差。
ROUND_STARTED_AT_SKEW_SECONDS = 0.5
COMPLETION_MARKERS = (
    "本轮迭代已完成",
    "进入下一轮检查",
    "任务执行完成",
    "Round ",
)
CLAUDE_HOME = Path.home() / ".claude"
CLAUDE_JSONL_TAIL_BYTES = 1024 * 1024

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def load_runtime_paths(config_path: Path) -> tuple[str, str, Path, Path]:
    """Load the minimal paths needed by the detached foreground watcher from the shared bot config."""

    data = json.loads(config_path.read_text(encoding="utf-8"))
    pwsh_path = str(data.get("pwsh_path") or "powershell")
    python_path = str(data.get("python_path") or sys.executable)
    state_path = Path(str(data["state_path"]))
    hook_log_raw = str(data.get("turn_hook_log_path") or "").strip()
    if hook_log_raw:
        log_path = Path(hook_log_raw)
    else:
        log_path = state_path.resolve().parent.parent / "logs" / "feishu-claude-turn-hook.log"
    return pwsh_path, python_path, state_path, log_path


def process_exists(pid: int, pwsh_path: str) -> bool:
    """Check whether the managed foreground pwsh window process is still alive."""

    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def _build_process_tree() -> tuple[dict[int, int], dict[int, list[int]]]:
    """Snapshot the process table using CreateToolhelp32Snapshot (no visible window)."""

    TH32CS_SNAPPROCESS = 0x00000002
    snapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == -1:
        return {}, {}

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ProcessID", ctypes.c_uint32),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", ctypes.c_uint32),
            ("cntThreads", ctypes.c_uint32),
            ("th32ParentProcessID", ctypes.c_uint32),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_uint32),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    pe = PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    parent_of: dict[int, int] = {}
    children_of: dict[int, list[int]] = {}
    try:
        if ctypes.windll.kernel32.Process32FirstW(snapshot, ctypes.byref(pe)):
            while True:
                pid = pe.th32ProcessID
                ppid = pe.th32ParentProcessID
                parent_of[pid] = ppid
                children_of.setdefault(ppid, []).append(pid)
                if not ctypes.windll.kernel32.Process32NextW(snapshot, ctypes.byref(pe)):
                    break
    finally:
        ctypes.windll.kernel32.CloseHandle(snapshot)
    return parent_of, children_of


def has_claude_child_process(parent_pid: int, pwsh_path: str) -> bool:
    """Check whether a Claude CLI child process currently exists under the managed foreground pwsh session."""

    # 前台窗口本身会长期存活，真正代表"一轮 Claude 正在跑"的是它下面的 claude.exe 子进程。
    _, children_of = _build_process_tree()
    queue = [int(parent_pid)]
    while queue:
        cur = queue.pop(0)
        for child in children_of.get(cur, []):
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, child)
            if not handle:
                continue
            try:
                exe_buf = ctypes.create_unicode_buffer(512)
                size = ctypes.wintypes.DWORD(512)
                if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, exe_buf, ctypes.byref(size)):
                    exe_name = os.path.basename(exe_buf.value).rsplit(".", 1)[0].lower()
                    if exe_name.startswith("claude"):
                        return True
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
            queue.append(child)
    return False


def get_chat_state(state_path: Path, chat_id: str) -> dict[str, Any]:
    """Read one chat's persisted state for watcher decisions."""

    chat_state = load_state(state_path).get("chats", {}).get(chat_id, {})
    sessions = chat_state.get("sessions")
    if isinstance(sessions, dict):
        active_session_id = str(chat_state.get("active_session") or "")
        session_state = sessions.get(active_session_id) if active_session_id else None
        if isinstance(session_state, dict):
            # BotState 的 v2 读取视图会用 active session 覆盖运行态字段；watcher 必须保持同样视角，
            # 否则顶层 legacy PID 残留会误触发 rebound-to-old-pid 并停止通知。
            return {**chat_state, **session_state}
    return chat_state


def strip_ansi(text: str) -> str:
    """Remove terminal color/control sequences before matching transcript content."""

    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text or "")


def is_transcript_noise_line(line: str) -> bool:
    """Tell whether one transcript line is PowerShell metadata rather than Claude output."""

    noise_prefixes = (
        "**********************",
        "Windows PowerShell transcript",
        "PowerShell transcript",
        "Transcript started",
        "Transcript stopped",
        "Start time:",
        "End time:",
        "Username:",
        "RunAs User:",
        "Machine:",
        "Host Application:",
        "Process ID:",
        "PSVersion:",
        "PSEdition:",
        "GitCommitId:",
        "OS:",
        "Platform:",
        "PSCompatibleVersions:",
        "PSRemotingProtocolVersion:",
        "SerializationVersion:",
        "WSManStackVersion:",
        "Configuration Name:",
        "Command:",
        "Chat ID:",
        "Workdir:",
        "Claude Code foreground session started",
        "Claude foreground session returned",
    )
    return any(line.startswith(prefix) for prefix in noise_prefixes)


def read_transcript_summary(transcript_path: Path, limit: int = 1200) -> tuple[str, str]:
    """Return a completion marker hash and readable tail summary from a foreground transcript."""

    if not transcript_path.exists() or not transcript_path.is_file():
        return "", ""
    raw_text = transcript_path.read_text(encoding="utf-8", errors="replace")
    cleaned_lines: list[str] = []
    for raw_line in raw_text.splitlines():
        line = strip_ansi(raw_line).strip()
        if is_transcript_noise_line(line):
            continue
        cleaned_lines.append(line)
    if not cleaned_lines:
        return "", ""
    text = "\n".join(cleaned_lines)
    if not any(marker in text for marker in COMPLETION_MARKERS):
        return "", ""
    # 以最近一次 Round/完成标记之后的文本作为本轮摘要，避免旧轮次内容反复触发。
    round_index = max(text.rfind("Round "), text.rfind("进入 Round"))
    start_index = round_index if round_index >= 0 else max(text.rfind("本轮迭代已完成"), text.rfind("任务执行完成"))
    summary = text[start_index:] if start_index >= 0 else text
    summary = summary.strip()
    if len(summary) > limit:
        summary = summary[-limit:].lstrip()
    marker = hashlib.sha1(summary.encode("utf-8", errors="ignore")).hexdigest()
    return marker, summary


def extract_transcript_text(transcript_path: Path) -> str:
    """Read foreground transcript text after removing PowerShell metadata and ANSI noise."""

    if not transcript_path.exists() or not transcript_path.is_file():
        return ""
    raw_text = transcript_path.read_text(encoding="utf-8", errors="replace")
    cleaned_lines: list[str] = []
    for raw_line in raw_text.splitlines():
        line = strip_ansi(raw_line).strip()
        if is_transcript_noise_line(line):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def trim_recent_lines(text: str, limit: int = 1200, line_count: int = 14) -> str:
    """Return the latest readable terminal lines without relying on Claude wording."""

    cleaned_lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not cleaned_lines:
        return ""
    # 前台停止时用户真正需要的是终端最后一屏，保留尾部若干行比关键词判断更稳。
    summary = "\n".join(cleaned_lines[-line_count:]).strip()
    if len(summary) > limit:
        summary = summary[-limit:].lstrip()
    return summary


def read_transcript_tail_summary(transcript_path: Path, limit: int = 1200) -> str:
    """Read the latest foreground transcript tail for a generic stopped notice."""

    return trim_recent_lines(extract_transcript_text(transcript_path), limit)


def encode_claude_project_dir_name(cwd: str) -> str:
    """Convert a Windows working directory into Claude's project log directory name."""

    # Claude Code 的 JSONL 日志按工作目录编码存储；观察器用同一规则定位前台会话真实输出。
    normalized = str(Path(cwd).resolve() if cwd else Path.cwd())
    return normalized.replace(":", "-").replace("\\", "-").replace("/", "-")


def get_claude_jsonl_candidates(cwd: str) -> list[Path]:
    """Return Claude JSONL files for one working directory, newest first."""

    project_root = CLAUDE_HOME / "projects"
    encoded_cwd = encode_claude_project_dir_name(cwd)
    project_dirs = [project_root / encoded_cwd]
    if project_root.exists():
        try:
            # 前台 Claude 会话里用户/模型可能 `cd` 到项目子目录；Claude JSONL 会落到子目录编码名下。
            # 观察器以配置 cwd 为锚点额外纳入子目录 project，避免选项/摘要写在子目录时飞书收不到。
            project_dirs.extend(
                item for item in project_root.iterdir() if item.is_dir() and item.name.startswith(f"{encoded_cwd}-")
            )
        except OSError:
            pass
    candidates: list[Path] = []
    for project_dir in project_dirs:
        if not project_dir.exists():
            continue
        try:
            candidates.extend(project_dir.glob("*.jsonl"))
        except OSError:
            continue
    if not candidates:
        return []
    try:
        return sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return []


def read_text_file_tail(path: Path, max_bytes: int = CLAUDE_JSONL_TAIL_BYTES) -> str:
    """Read the tail of a large Claude JSONL file without loading old rounds into memory."""

    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        file_size = handle.tell()
        handle.seek(max(0, file_size - max_bytes))
        return handle.read().decode("utf-8", errors="replace")


def extract_assistant_text_from_jsonl(record: dict[str, Any]) -> str:
    """Extract assistant text content from one Claude JSONL record."""

    if record.get("type") != "assistant":
        return ""
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    text_parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text") or "").strip())
    return "\n".join(part for part in text_parts if part).strip()


def parse_claude_record_timestamp(record: dict[str, Any]) -> float:
    """Parse a Claude JSONL record timestamp into Unix seconds.

    Args:
        record: One parsed Claude JSONL record.

    Returns:
        Unix timestamp seconds, or 0.0 when the record has no parseable timestamp.
    """

    raw_timestamp = str(record.get("timestamp") or "").strip()
    if not raw_timestamp:
        return 0.0
    try:
        normalized = raw_timestamp.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).astimezone(timezone.utc).timestamp()
    except ValueError:
        return 0.0


def extract_ask_user_question_input_from_jsonl(record: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the latest AskUserQuestion tool input from one assistant JSONL record.

    Args:
        record: One parsed Claude JSONL record.

    Returns:
        The AskUserQuestion input dictionary, or None when the record is unrelated.
    """

    if record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for block in reversed(content):
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use" or str(block.get("name") or "").lower() != "askuserquestion":
            continue
        tool_input = block.get("input")
        if isinstance(tool_input, dict):
            return tool_input
    return None


def read_latest_ask_user_question(cwd: str, since_timestamp: float = 0.0) -> dict[str, Any] | None:
    """Read the newest AskUserQuestion payload written by Claude for this working directory.

    Args:
        cwd: Foreground Claude working directory.
        since_timestamp: Optional lower bound for record timestamps; older questions are ignored.

    Returns:
        A serializable pending-question state dictionary, or None when no current question exists.
    """

    for jsonl_path in get_claude_jsonl_candidates(cwd):
        try:
            raw_tail = read_text_file_tail(jsonl_path)
        except OSError:
            continue
        for raw_line in reversed(raw_tail.splitlines()):
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            record_timestamp = parse_claude_record_timestamp(record)
            if since_timestamp and record_timestamp and record_timestamp < since_timestamp - 5:
                # 只展示本轮开始后的提问，防止旧 JSONL 里的历史选项在新暂停通知里复活。
                continue
            tool_input = extract_ask_user_question_input_from_jsonl(record)
            if not tool_input:
                if extract_assistant_text_from_jsonl(record):
                    # 倒序扫描时先遇到更新的 assistant 文本，说明旧 AskUserQuestion 已被后续结果覆盖，不再展示旧选项。
                    return None
                continue
            marker_source = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
            return {
                "tool_name": "AskUserQuestion",
                "tool_input": tool_input,
                "source_path": str(jsonl_path),
                "source_timestamp": record_timestamp,
                "source_marker": hashlib.sha1(marker_source.encode("utf-8", errors="ignore")).hexdigest(),
            }
    return None


def format_ask_user_question_lines(pending_question: dict[str, Any]) -> list[str]:
    """Render one foreground AskUserQuestion payload as Feishu-readable option lines.

    Args:
        pending_question: State dictionary returned by read_latest_ask_user_question().

    Returns:
        Lines ready to append into a foreground pause summary.
    """

    tool_input = pending_question.get("tool_input")
    lines = ["待选择问题："]
    if not isinstance(tool_input, dict):
        return lines
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not questions:
        return lines
    for question_index, question_item in enumerate(questions, start=1):
        if not isinstance(question_item, dict):
            lines.append(str(question_item))
            continue
        header = str(question_item.get("header") or f"问题 {question_index}").strip()
        question_text = str(question_item.get("question") or "").strip()
        lines.append(header)
        if question_text:
            lines.append(question_text)
        options = question_item.get("options")
        if isinstance(options, list) and options:
            for option_index, option in enumerate(options, start=1):
                if isinstance(option, dict):
                    label = str(option.get("label") or f"选项 {option_index}").strip()
                    description = str(option.get("description") or "").strip()
                    lines.append(f"{option_index}. {label}")
                    if description:
                        lines.append(f"   说明：{description}")
                else:
                    lines.append(f"{option_index}. {option}")
        lines.append(f"选择方式：{'可多选' if question_item.get('multiSelect') else '单选'}")
        if question_index < len(questions):
            lines.append("")
    return lines


def append_latest_ask_user_question_summary(cwd: str, summary: str, since_timestamp: float) -> tuple[str, dict[str, Any] | None]:
    """Append the latest foreground AskUserQuestion options to a pause summary.

    Args:
        cwd: Foreground Claude working directory.
        summary: Existing terminal/assistant summary.
        since_timestamp: Start time of the current foreground round.

    Returns:
        A tuple of enriched summary and the pending-question state to persist.
    """

    pending_question = read_latest_ask_user_question(cwd, since_timestamp)
    if not pending_question:
        return summary, None
    question_block = "\n".join(format_ask_user_question_lines(pending_question)).strip()
    enriched_parts = [part for part in (summary.strip(), question_block) if part]
    return "\n\n".join(enriched_parts), pending_question


def trim_summary_from_latest_marker(text: str, limit: int = 1200) -> tuple[str, bool]:
    """Return a readable summary and whether it contains a known completion/progress marker."""

    if not text:
        return "", False
    # 轮次化任务优先保留最近 Round 标题；否则再从最近完成/进度标记截取。
    marker_index = max(text.rfind("Round "), text.rfind("进入 Round"))
    if marker_index < 0:
        marker_index = max(text.rfind(marker) for marker in COMPLETION_MARKERS[:3])
    summary = text[marker_index:] if marker_index >= 0 else text
    summary = summary.strip()
    if len(summary) > limit:
        summary = summary[-limit:].lstrip()
    return summary, marker_index >= 0


def build_watch_marker(source: str, notice_kind: str, summary: str) -> str:
    """Build a stable de-dup marker for one foreground watcher notification."""

    # 同一段摘要无论来自 JSONL 还是 transcript，都要带 notice_kind，避免决策通知和完成通知互相吞掉。
    return hashlib.sha1(f"{source}:{notice_kind}:{summary}".encode("utf-8", errors="ignore")).hexdigest()


def coerce_float(value: Any) -> float:
    """Convert a state timestamp into float seconds.

    Args:
        value: Raw value read from the shared JSON state file.

    Returns:
        Parsed timestamp, or 0.0 when the state field is missing or malformed.
    """

    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def read_transcript_watch_summary(transcript_path: Path, limit: int = 1200) -> tuple[str, str, str]:
    """Return marker, summary, and notice kind from a foreground transcript."""

    text = extract_transcript_text(transcript_path)
    if not text:
        return "", "", ""
    completion_summary, has_completion = trim_summary_from_latest_marker(text, limit)
    if has_completion:
        return build_watch_marker(str(transcript_path), "completion", completion_summary), completion_summary, "completion"
    return "", "", ""


def read_claude_jsonl_completion_summary(cwd: str, limit: int = 1200) -> tuple[str, str]:
    """Read the latest Claude assistant message and return it when it has a completion marker."""

    for jsonl_path in get_claude_jsonl_candidates(cwd):
        try:
            raw_tail = read_text_file_tail(jsonl_path)
        except OSError:
            continue
        for raw_line in reversed(raw_tail.splitlines()):
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            assistant_text = extract_assistant_text_from_jsonl(record)
            if not assistant_text:
                continue
            summary, has_marker = trim_summary_from_latest_marker(assistant_text, limit)
            if not has_marker:
                return "", ""
            marker = hashlib.sha1(f"{jsonl_path}:{summary}".encode("utf-8", errors="ignore")).hexdigest()
            return marker, summary
    return "", ""


def read_claude_jsonl_tail_summary(cwd: str, limit: int = 1200) -> str:
    """Read the latest assistant text tail without requiring a completion marker."""

    for jsonl_path in get_claude_jsonl_candidates(cwd):
        try:
            raw_tail = read_text_file_tail(jsonl_path)
        except OSError:
            continue
        for raw_line in reversed(raw_tail.splitlines()):
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            assistant_text = extract_assistant_text_from_jsonl(record)
            if assistant_text:
                return trim_recent_lines(assistant_text, limit)
    return ""


def read_claude_jsonl_tail_marker(cwd: str, limit: int = 1200) -> tuple[str, str]:
    """Return a stable marker and tail summary for the latest Claude assistant text.

    This is the foreground fallback for long-lived interactive Claude processes. Some
    Claude/proxy builds keep `claude.exe` alive and occasionally miss Stop hooks, so
    the watcher needs a content-level signal instead of waiting for process exit.
    """

    for jsonl_path in get_claude_jsonl_candidates(cwd):
        try:
            raw_tail = read_text_file_tail(jsonl_path)
        except OSError:
            continue
        for raw_line in reversed(raw_tail.splitlines()):
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            assistant_text = extract_assistant_text_from_jsonl(record)
            if assistant_text:
                summary = trim_recent_lines(assistant_text, limit)
                return build_watch_marker(str(jsonl_path), "assistant-tail", summary), summary
    return "", ""


def read_claude_jsonl_watch_summary(cwd: str, limit: int = 1200) -> tuple[str, str, str]:
    """Read the latest Claude assistant message for completion or decision-wait notices."""

    for jsonl_path in get_claude_jsonl_candidates(cwd):
        try:
            raw_tail = read_text_file_tail(jsonl_path)
        except OSError:
            continue
        for raw_line in reversed(raw_tail.splitlines()):
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            assistant_text = extract_assistant_text_from_jsonl(record)
            if not assistant_text:
                continue
            completion_summary, has_completion = trim_summary_from_latest_marker(assistant_text, limit)
            if has_completion:
                return build_watch_marker(str(jsonl_path), "completion", completion_summary), completion_summary, "completion"
            return "", "", ""
    return "", "", ""


def update_chat_for_detected_round_start(state_path: Path, chat_id: str, cwd: str, window_pid: int, started_at: float) -> None:
    """Mark the chat as foreground busy when the watcher detects a fresh Claude child process."""

    state = load_state(state_path)
    chats = state.setdefault("chats", {})
    chat_state = chats.setdefault(chat_id, {"cwd": cwd, "managed_session": True})
    # 手工在窗口里继续执行时，机器人没有外部命令入口可更新 started_at；
    # 这里在观察到 claude.exe 新起时主动补一轮起始状态，方便后续完成通知和耗时统计。
    chat_state["cwd"] = chat_state.get("cwd") or cwd
    apply_chat_runtime_updates(
        chat_state,
        {
            "status": "foreground_busy",
            "started_at": started_at,
            "finished_at": None,
            "active_pid": window_pid,
            "foreground_pid": window_pid,
            "last_error": "",
            "pending_action": "",
            "pending_prompt": "",
            "managed_session": True,
            # 新一轮前台执行开始后，旧 AskUserQuestion 选项已经失效，必须清空以免飞书数字回复选中历史问题。
            "foreground_pending_question": {},
        },
    )
    save_state(state_path, state)


def update_chat_completion_marker(state_path: Path, chat_id: str, marker: str) -> None:
    """Persist the last transcript completion marker so one completed round is not notified twice."""

    state = load_state(state_path)
    chats = state.setdefault("chats", {})
    chat_state = chats.setdefault(chat_id, {"managed_session": True})
    # 完成标记必须同步 active session，否则 bot 读取合并视图时会重复通知同一段输出。
    apply_chat_runtime_updates(chat_state, {"foreground_last_completion_marker": marker})
    save_state(state_path, state)


def update_chat_pending_question(state_path: Path, chat_id: str, pending_question: dict[str, Any] | None) -> None:
    """Persist the latest foreground AskUserQuestion options for Feishu reply routing.

    Args:
        state_path: Shared bot state file.
        chat_id: Feishu chat id bound to the foreground window.
        pending_question: Question state to store; None clears the field.

    Returns:
        None. The main bot will read this field when users reply with an option number.
    """

    state = load_state(state_path)
    chats = state.setdefault("chats", {})
    chat_state = chats.setdefault(chat_id, {"managed_session": True})
    apply_chat_runtime_updates(
        chat_state,
        {
            # 这个字段不是 PermissionRequest 授权队列，只是前台窗口里 AskUserQuestion 的选项映射。
            "foreground_pending_question": pending_question or {},
        },
    )
    save_state(state_path, state)


def invoke_foreground_return_helper(
    python_path: str,
    config_path: Path,
    chat_id: str,
    cwd: str,
    started_at: float,
    window_pid: int,
    summary: str = "",
    notice_kind: str = "completion",
) -> int:
    """Delegate the final state write and Feishu completion notice to the shared fallback helper."""

    integration_root = config_path.parents[1]
    bootstrap_path = integration_root / "app" / "bootstrap_feishu_tool.py"
    helper_path = integration_root / "hooks" / "feishu_claude_foreground_return.py"
    command = [
        python_path,
        str(bootstrap_path),
        str(helper_path),
        "--chat-id",
        chat_id,
        "--cwd",
        cwd,
        "--started-at",
        str(started_at),
        "--exit-code",
        "0",
        "--window-pid",
        str(window_pid),
    ]
    if summary:
        command.extend(["--summary", summary])
    command.extend(["--notice-kind", notice_kind])
    result = subprocess.run(
        command,
        cwd=str(integration_root),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        # return helper 只负责写状态和发飞书通知；显式禁用控制台窗口，避免每次通知弹空白终端。
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.returncode


def read_latest_stopped_summary(cwd: str, transcript_path_value: str, limit: int = 1200) -> str:
    """Read the most useful recent output when the foreground Claude process stops."""

    # 前台停止通知要模拟"看终端最后几行"，所以优先用 transcript；没有窗口输出时再退回 JSONL。
    if transcript_path_value:
        transcript_summary = read_transcript_tail_summary(Path(transcript_path_value), limit)
        if transcript_summary:
            return transcript_summary
    return read_claude_jsonl_tail_summary(cwd, limit)


def main() -> int:
    """Continuously watch one Claude foreground window and emit fallback completion notices for each detected round."""

    parser = argparse.ArgumentParser(description="Persistent watcher for one Claude foreground window")
    parser.add_argument("--chat-id", required=True, help="Feishu chat id bound to the foreground Claude session")
    parser.add_argument("--cwd", default="", help="Working directory of the foreground Claude session")
    parser.add_argument("--window-pid", required=True, type=int, help="Managed pwsh pid that hosts the Claude session")
    args = parser.parse_args()

    config_path = DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return 0
    pwsh_path, python_path, state_path, log_path = load_runtime_paths(config_path)

    active_round_started_at: float | None = None
    claude_child_seen = False
    baseline_assistant_marker = ""
    pending_assistant_marker = ""
    pending_assistant_summary = ""
    pending_assistant_since = 0.0
    deadline = time.time() + WATCH_TIMEOUT_SECONDS
    log_event(log_path, f"foreground session watch started chat={args.chat_id} window_pid={args.window_pid}")

    while time.time() < deadline:
        if not process_exists(args.window_pid, pwsh_path):
            log_event(log_path, f"foreground session watch stopped chat={args.chat_id} window_pid={args.window_pid} reason=window-exited")
            return 0

        current_chat_state = get_chat_state(state_path, args.chat_id)
        current_foreground_pid = int(current_chat_state.get("foreground_pid") or 0)
        if current_foreground_pid and current_foreground_pid != args.window_pid:
            log_event(
                log_path,
                f"foreground session watch stopped chat={args.chat_id} window_pid={args.window_pid} reason=rebound-to-{current_foreground_pid}",
            )
            return 0

        child_running = has_claude_child_process(args.window_pid, pwsh_path)
        transcript_value = str(current_chat_state.get("foreground_transcript_path") or "").strip()
        state_status = str(current_chat_state.get("status") or "")
        state_started_at = coerce_float(current_chat_state.get("started_at"))
        state_finished_at = coerce_float(current_chat_state.get("finished_at"))
        state_is_active_foreground_round = state_status in {"foreground_busy", "foreground_running", "foreground_opened"} and (
            not state_finished_at or state_finished_at < state_started_at
        )
        state_has_new_round = (
            child_running
            and state_status == "foreground_busy"
            and (
                active_round_started_at is None
                or state_started_at > active_round_started_at + ROUND_STARTED_AT_SKEW_SECONDS
            )
        )
        if child_running and ((not claude_child_seen and state_is_active_foreground_round) or state_has_new_round):
            claude_child_seen = True
            # 飞书入口已写入本轮 started_at 时直接复用，保证后续暂停通知耗时与状态页一致。
            active_round_started_at = state_started_at or time.time()
            baseline_assistant_marker, _ = read_claude_jsonl_tail_marker(args.cwd)
            pending_assistant_marker = ""
            pending_assistant_summary = ""
            pending_assistant_since = 0.0
            update_chat_pending_question(state_path, args.chat_id, None)
            if not state_has_new_round:
                # 手工在终端里继续时没有飞书入口写状态，观察器需要补一轮 foreground_busy。
                update_chat_for_detected_round_start(
                    state_path,
                    args.chat_id,
                    args.cwd,
                    args.window_pid,
                    active_round_started_at,
                )
            log_event(
                log_path,
                f"foreground session watch detected round start chat={args.chat_id} window_pid={args.window_pid} started_at={active_round_started_at}",
            )
        elif child_running and claude_child_seen and active_round_started_at is not None:
            assistant_marker, assistant_summary = read_claude_jsonl_tail_marker(args.cwd)
            last_marker = str(get_chat_state(state_path, args.chat_id).get("foreground_last_completion_marker") or "")
            if assistant_marker and assistant_marker not in {baseline_assistant_marker, last_marker}:
                if assistant_marker != pending_assistant_marker:
                    # 长驻交互进程不会退出；先等待最新 assistant JSONL 稳定一小段时间，
                    # 避免 Claude 仍在追加工具调用/后续回复时过早通知飞书。
                    pending_assistant_marker = assistant_marker
                    pending_assistant_summary = assistant_summary
                    pending_assistant_since = time.time()
                elif time.time() - pending_assistant_since >= GENERIC_ASSISTANT_IDLE_SECONDS:
                    notice_summary, pending_question = append_latest_ask_user_question_summary(
                        args.cwd,
                        pending_assistant_summary,
                        active_round_started_at,
                    )
                    if pending_question:
                        update_chat_pending_question(state_path, args.chat_id, pending_question)
                    else:
                        update_chat_pending_question(state_path, args.chat_id, None)
                    helper_code = invoke_foreground_return_helper(
                        python_path,
                        config_path,
                        args.chat_id,
                        args.cwd,
                        active_round_started_at,
                        args.window_pid,
                        notice_summary,
                        "stopped",
                    )
                    update_chat_completion_marker(state_path, args.chat_id, pending_assistant_marker)
                    log_event(
                        log_path,
                        f"foreground session watch detected stable assistant tail chat={args.chat_id} window_pid={args.window_pid} started_at={active_round_started_at} helper_code={helper_code}",
                    )
                    # 长驻 claude.exe 回到输入态时子进程仍存在；清空 active_round_started_at
                    # 避免同一轮已通知的稳定尾部继续变化时反复刷飞书。
                    active_round_started_at = None
                    baseline_assistant_marker = pending_assistant_marker
                    pending_assistant_marker = ""
                    pending_assistant_summary = ""
                    pending_assistant_since = 0.0
        elif (not child_running) and claude_child_seen and active_round_started_at is not None:
            # 子进程从有到无，说明当前这一轮已经停住；不再猜测完成/决策类型，直接回传最近输出尾部。
            try:
                stopped_summary = read_latest_stopped_summary(args.cwd, transcript_value)
            except OSError as exc:
                log_event(log_path, f"foreground stopped summary read failed chat={args.chat_id} cwd={args.cwd or '-'} error={exc}")
                stopped_summary = ""
            stopped_summary, pending_question = append_latest_ask_user_question_summary(
                args.cwd,
                stopped_summary,
                active_round_started_at,
            )
            if pending_question:
                update_chat_pending_question(state_path, args.chat_id, pending_question)
            else:
                update_chat_pending_question(state_path, args.chat_id, None)
            marker = build_watch_marker(args.cwd or transcript_value or str(args.window_pid), "stopped", stopped_summary or str(active_round_started_at))
            last_marker = str(get_chat_state(state_path, args.chat_id).get("foreground_last_completion_marker") or "")
            if marker == last_marker:
                claude_child_seen = False
                active_round_started_at = None
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            helper_code = invoke_foreground_return_helper(
                python_path,
                config_path,
                args.chat_id,
                args.cwd,
                active_round_started_at,
                args.window_pid,
                stopped_summary,
                "stopped",
            )
            update_chat_completion_marker(state_path, args.chat_id, marker)
            log_event(
                log_path,
                f"foreground session watch detected round end chat={args.chat_id} window_pid={args.window_pid} started_at={active_round_started_at} helper_code={helper_code}",
            )
            claude_child_seen = False
            active_round_started_at = None
            baseline_assistant_marker = ""
            pending_assistant_marker = ""
            pending_assistant_summary = ""
            pending_assistant_since = 0.0

        time.sleep(POLL_INTERVAL_SECONDS)

    log_event(log_path, f"foreground session watch stopped chat={args.chat_id} window_pid={args.window_pid} reason=timeout")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
