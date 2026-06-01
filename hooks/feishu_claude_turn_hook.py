#!/usr/bin/env python3
"""Claude Stop / StopFailure / SessionEnd Hook 的飞书通知脚本。

这个脚本只负责把 Claude 本轮完成、失败或会话结束的信号翻译成飞书消息，并把
结果同步回共享 state 文件。它不启动 Claude，不做任务调度，也不接收普通飞书指令。
"""

from __future__ import annotations

import json
import os
import site
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INTEGRATION_ROOT = Path(__file__).resolve().parents[1]
CODEX_ROOT = INTEGRATION_ROOT.parents[1]

try:
    vendor_site_packages = str(INTEGRATION_ROOT / "vendor")
    if os.path.isdir(vendor_site_packages) and vendor_site_packages not in sys.path:
        # Stop/StopFailure hook 需要在前台会话完成时立刻可用，本地 vendor 目录能规避
        # 当前机器 Anaconda 无法稳定读取用户级包目录的问题。
        sys.path.append(vendor_site_packages)
    user_site_packages = site.getusersitepackages()
    if user_site_packages and user_site_packages not in sys.path:
        # 当前这台机器的 Claude hook 会走 Anaconda Python；显式加入用户级包目录，
        # 才能复用已经安装好的 lark-oapi，而不是要求再改全局解释器环境。
        sys.path.append(user_site_packages)
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody, CreateMessageResponse
except ImportError as exc:  # pragma: no cover - dependency bootstrap path
    raise SystemExit(
        "Missing dependency 'lark-oapi'. Install it with:\n"
        "python -m pip install lark-oapi"
    ) from exc


DEFAULT_CONFIG_PATH = INTEGRATION_ROOT / "config" / "feishu_claude_bot.v2.json"
DEFAULT_HOOK_LOG_PATH = CODEX_ROOT / "outputs" / "feishu-claude-v2" / "logs" / "feishu-claude-turn-hook.log"
LOG_ROTATE_MAX_BYTES = 512 * 1024
LOG_ROTATE_BACKUP_COUNT = 3
FEISHU_SEND_RETRY_COUNT = 3
FEISHU_SEND_RETRY_DELAY_SECONDS = 1.5


@dataclass
class TurnHookConfig:
    """Turn hook 运行所需的最小共享配置。"""

    # 飞书应用 App ID，用于创建消息客户端。
    app_id: str
    # 飞书应用 Secret，用于调用 OpenAPI。
    app_secret: str
    # 共享状态文件路径，用于读写当前 chat 的完成状态。
    state_path: str
    # Turn hook 自己的日志路径。
    hook_log_path: str | None = None

    @classmethod
    def load(cls, path: Path) -> "TurnHookConfig":
        """Load the subset of JSON config required by the turn hook."""

        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            app_id=data["app_id"],
            app_secret=data["app_secret"],
            state_path=data["state_path"],
            hook_log_path=data.get("turn_hook_log_path"),
        )


def log_event(log_path: Path, message: str) -> None:
    """Append one timestamped diagnostic line for Stop/StopFailure troubleshooting."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    rotate_log_if_needed(log_path)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def rotate_log_if_needed(log_path: Path) -> None:
    """Rotate hook logs by size so abnormal hook storms do not leave one ever-growing file."""

    try:
        if not log_path.exists() or log_path.stat().st_size < LOG_ROTATE_MAX_BYTES:
            return
        oldest_backup = log_path.with_name(f"{log_path.name}.{LOG_ROTATE_BACKUP_COUNT}")
        if oldest_backup.exists():
            oldest_backup.unlink(missing_ok=True)
        for index in range(LOG_ROTATE_BACKUP_COUNT - 1, 0, -1):
            source = log_path.with_name(f"{log_path.name}.{index}")
            target = log_path.with_name(f"{log_path.name}.{index + 1}")
            if source.exists():
                source.replace(target)
        # Hook 日志主要用于近几轮排障，轮转后保留最近 3 份即可。
        log_path.replace(log_path.with_name(f"{log_path.name}.1"))
    except OSError:
        return


def load_state(path: Path) -> dict[str, Any]:
    """Read bot state so hooks can update the right Feishu chat runtime record."""

    if path.exists():
        raw_text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not raw_text:
            # 上一次写状态若被异常中断，可能留下 0 字节文件；这里回退到空结构，
            # 避免后续完成/失败通知因为读不到 state 而继续整条失败。
            return {"chats": {}}
        return json.loads(raw_text)
    return {"chats": {}}


def _sanitize_text(value: Any) -> str:
    """Normalize arbitrary hook text into safe UTF-8 JSON/string content."""

    text = str(value or "")
    # Claude 返回的最后消息里偶发会带半个 surrogate 字符；先通过 replace 丢弃
    # 非法码位，确保后续写状态和发飞书都不会再触发 UnicodeEncodeError。
    return text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def _sanitize_json_value(value: Any) -> Any:
    """Recursively sanitize state values before writing them to disk."""

    if isinstance(value, dict):
        return {str(key): _sanitize_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_json_value(item) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value)
    return value


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Persist bot state updates made by Stop/StopFailure hooks."""

    path.parent.mkdir(parents=True, exist_ok=True)
    safe_state = _sanitize_json_value(state)
    payload = json.dumps(safe_state, ensure_ascii=False, indent=2)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    # 原子替换避免写一半时把主状态文件截成 0 字节，导致 bot/status 一起失真。
    temp_path.write_text(payload, encoding="utf-8")
    temp_path.replace(path)


def resolve_chat_id_from_state(state: dict[str, Any], cwd: str, log_path: Path) -> str:
    """Infer the owning Feishu chat when the foreground Claude window lacks env metadata."""

    chats: dict[str, Any] = state.get("chats", {})
    if not chats:
        log_event(log_path, "resolve chat: no chats found in bot state")
        return ""

    scored_matches: list[tuple[int, float, str]] = []
    for candidate_chat_id, chat_state in chats.items():
        status = str(chat_state.get("status", ""))
        candidate_cwd = str(chat_state.get("cwd", ""))
        started_at = float(chat_state.get("started_at") or 0)
        managed_session = bool(chat_state.get("managed_session"))
        score = 0
        # 优先匹配仍托管中的前台会话，这样前台人工执行的完成通知能尽量回到正确会话。
        if managed_session and status in {"foreground_opened", "foreground_busy", "foreground_running", "done", "failed"}:
            score += 10
        # 工作目录一致时，通常就是当前这一条 Claude 会话正在处理的项目。
        if cwd and candidate_cwd and cwd.lower() == candidate_cwd.lower():
            score += 5
        if score > 0:
            scored_matches.append((score, started_at, str(candidate_chat_id)))

    if not scored_matches:
        latest_chat_id = ""
        latest_ts = -1.0
        for candidate_chat_id, chat_state in chats.items():
            candidate_ts = float(chat_state.get("started_at") or chat_state.get("finished_at") or 0)
            if candidate_ts >= latest_ts:
                latest_ts = candidate_ts
                latest_chat_id = str(candidate_chat_id)
        if latest_chat_id:
            log_event(log_path, f"resolve chat fallback to latest chat={latest_chat_id} cwd={cwd or '-'}")
        return latest_chat_id

    scored_matches.sort(reverse=True)
    selected_chat_id = scored_matches[0][2]
    log_event(log_path, f"resolve chat from state chat={selected_chat_id} cwd={cwd or '-'} score={scored_matches[0][0]}")
    return selected_chat_id


def _extract_message_id(response: CreateMessageResponse) -> str:
    """Extract Feishu message_id from SDK response for delivery troubleshooting."""

    data = getattr(response, "data", None)
    if data is None:
        return ""
    message_id = getattr(data, "message_id", "")
    return str(message_id or "")


def send_feishu_text(app_id: str, app_secret: str, chat_id: str, text: str) -> str:
    """Send one text message into the target Feishu chat and return its message_id."""

    client = (
        lark.Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .log_level(lark.LogLevel.INFO)
        .build()
    )
    request = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )
    last_error = ""
    for attempt in range(1, FEISHU_SEND_RETRY_COUNT + 1):
        response: CreateMessageResponse = client.im.v1.message.create(request)
        if response.success():
            return _extract_message_id(response)
        last_error = f"code={response.code} msg={response.msg}"
        if attempt < FEISHU_SEND_RETRY_COUNT:
            # 完成通知是用户感知最强的链路，飞书 OpenAPI 偶发抖动时短重试能减少“已完成但没通知”。
            time.sleep(FEISHU_SEND_RETRY_DELAY_SECONDS)
    raise RuntimeError(f"send message failed after retries: {last_error}")


def format_ts(value: Any) -> str:
    """Format a unix timestamp into local readable time."""

    if not value:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))


def format_duration(started_at: Any, finished_at: Any) -> str:
    """Format elapsed seconds into concise Chinese duration text."""

    if not started_at or not finished_at:
        return "-"
    elapsed_seconds = max(0, int(float(finished_at) - float(started_at)))
    minutes, seconds = divmod(elapsed_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分钟")
    if seconds or not parts:
        parts.append(f"{seconds}秒")
    return "".join(parts)


def summarize_text(text: str, limit: int = 1200) -> str:
    """Trim long Claude final messages so Feishu summaries stay readable on mobile."""

    cleaned = _sanitize_text(text).strip()
    if not cleaned:
        return "(Claude 未返回可展示摘要)"
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "\n...(结果已截断)"


def get_active_window_title(chat_state: dict[str, Any]) -> str:
    """从 chat state 的活跃 session 里取窗口标题。"""
    sessions = chat_state.get("sessions", {})
    active_sid = chat_state.get("active_session", "")
    session = sessions.get(active_sid) if active_sid else None
    if session:
        return str(session.get("active_window_title") or "")
    return ""


def should_skip_session_end(existing_chat_state: dict[str, Any]) -> bool:
    """Avoid sending a second completion notice when Stop/StopFailure already updated the chat."""

    status = str(existing_chat_state.get("status", ""))
    finished_at = existing_chat_state.get("finished_at")
    # SessionEnd 常与 Stop / StopFailure 接近出现；如果当前聊天状态已经被前一条 hook
    # 落成 done/failed，就不再重复发一次“完成通知”，避免飞书里出现双份收尾消息。
    return status in {"done", "failed"} and bool(finished_at)


def apply_chat_runtime_updates(chat_state: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """Write hook runtime updates to both legacy chat fields and the active v2 session.

    Args:
        chat_state: Raw chat state loaded from the shared JSON state file.
        updates: Runtime fields produced by Stop/StopFailure/foreground fallback hooks.

    Returns:
        The same chat state after compatibility updates.
    """

    chat_state.update(updates)
    sessions = chat_state.get("sessions")
    if isinstance(sessions, dict):
        active_session_id = str(chat_state.get("active_session") or "")
        session_state = sessions.get(active_session_id) if active_session_id else None
        if session_state is None:
            active_session_id = next(iter(sessions), "s1")
            session_state = sessions.setdefault(active_session_id, {})
            chat_state["active_session"] = active_session_id
        # BotState 读取状态时优先合并 active session；hook 必须同步这里，否则通知已发但 bot 仍看到旧 PID/旧状态。
        session_state.update(updates)
    return chat_state


def build_message(title: str, details: list[str], next_steps: list[str]) -> str:
    """Build the same mobile-friendly message layout used by the main bot."""

    lines = [title]
    lines.extend(line for line in details if line)
    if next_steps:
        lines.append("")
        lines.append("可直接回复：")
        lines.extend(next_steps)
    return "\n".join(lines)


def read_hook_input() -> str:
    """Read Claude hook JSON as UTF-8 bytes to avoid Windows locale mojibake."""

    # Claude writes hook payloads as UTF-8 JSON. Reading through sys.stdin.read()
    # lets Windows choose a console/codepage decoder first, which can corrupt Chinese
    # before json.loads ever sees it. Reading bytes keeps the boundary honest.
    return sys.stdin.buffer.read().decode("utf-8", errors="replace")


def update_chat_for_stop(
    state_path: Path,
    chat_id: str,
    summary_text: str,
    finished_at: float,
) -> dict[str, Any]:
    """Persist a completed-turn state so Feishu /status reflects foreground completion."""

    state = load_state(state_path)
    chats = state.setdefault("chats", {})
    chat_state = chats.setdefault(chat_id, {"cwd": "", "managed_session": True})
    # 前台窗口在一轮完成后通常仍保持打开，因此这里只结束“本轮状态”，不释放托管会话。
    apply_chat_runtime_updates(
        chat_state,
        {
            "status": "done",
            "finished_at": finished_at,
            "last_result": summary_text,
            # last_summary 单独保留最近一次 hook 摘要，供“状态”在前台场景优先展示真实完成结果。
            "last_summary": summary_text,
            "last_error": "",
            "pending_action": "continue_session",
            "pending_prompt": "继续",
            "managed_session": True,
            "last_exit_code": 0,
        },
    )
    save_state(state_path, state)
    return chat_state


def update_chat_for_failure(
    state_path: Path,
    chat_id: str,
    error_text: str,
    finished_at: float,
) -> dict[str, Any]:
    """Persist a failed-turn state so Feishu can guide the next recovery action."""

    state = load_state(state_path)
    chats = state.setdefault("chats", {})
    chat_state = chats.setdefault(chat_id, {"cwd": "", "managed_session": True})
    # StopFailure 结束时通常仍保留可继续的会话入口，因此这里保留托管状态供飞书恢复。
    apply_chat_runtime_updates(
        chat_state,
        {
            "status": "failed",
            "finished_at": finished_at,
            "last_result": "",
            # 失败场景也要保留最近一次 hook 摘要，否则状态页只能看到错误标记，看不到失败内容。
            "last_summary": error_text,
            "last_error": error_text,
            "pending_action": "continue_session",
            "pending_prompt": "继续",
            "managed_session": True,
            "last_exit_code": 1,
        },
    )
    save_state(state_path, state)
    return chat_state


def main() -> int:
    """Route Stop/StopFailure hook events back to the active Feishu conversation."""

    raw_input = read_hook_input()
    if not raw_input.strip():
        return 0

    hook_input = json.loads(raw_input)
    hook_event_name = str(hook_input.get("hook_event_name", ""))
    if hook_event_name not in {"Stop", "StopFailure", "SessionEnd"}:
        return 0

    # 后台 `--print` 任务本身已经由 bot worker 汇总结果；这里跳过，避免飞书收到双份完成消息。
    if os.environ.get("FEISHU_CLAUDE_BOT_EXECUTION_MODE", "").strip().lower() == "background":
        return 0

    config_path = Path(os.environ.get("FEISHU_CLAUDE_BOT_CONFIG_PATH", str(DEFAULT_CONFIG_PATH)))
    if not config_path.exists():
        return 0

    config = TurnHookConfig.load(config_path)
    log_path = Path(config.hook_log_path or DEFAULT_HOOK_LOG_PATH)
    cwd = str(hook_input.get("cwd", ""))
    state_path = Path(config.state_path)
    state = load_state(state_path)
    log_event(log_path, f"received hook={hook_event_name} cwd={cwd or '-'}")

    # 环境变量优先；若前台窗口是用户手动接管的，环境里可能没有 chat_id，就回查 state。
    chat_id = os.environ.get("FEISHU_CLAUDE_BOT_CHAT_ID", "").strip()
    if not chat_id:
        chat_id = resolve_chat_id_from_state(state, cwd, log_path)
    if not chat_id:
        log_event(log_path, f"skip {hook_event_name}: missing chat id cwd={cwd or '-'}")
        return 0

    finished_at = time.time()
    existing_chat_state = state.get("chats", {}).get(chat_id, {})
    started_at = existing_chat_state.get("started_at")

    if hook_event_name == "SessionEnd":
        if should_skip_session_end(existing_chat_state):
            log_event(log_path, f"skip SessionEnd duplicate chat={chat_id} cwd={cwd or '-'}")
            return 0
        # SessionEnd 用于前台自然返回输入态的兜底完成通知，通常和 Stop 同类但触发更晚。
        summary_text = summarize_text(hook_input.get("last_assistant_message", ""))
        lines = [
            f"窗口：{get_active_window_title(existing_chat_state) or existing_chat_state.get('cwd') or cwd or '-'}",
            f"开始时间：{format_ts(started_at)}",
            f"结束时间：{format_ts(finished_at)}",
            f"总耗时：{format_duration(started_at, finished_at)}",
            "结果摘要：",
            summary_text,
            "说明：Claude 前台会话已结束。如需继续，请重新发起运行或前台运行。",
        ]
        message_id = send_feishu_text(
            config.app_id,
            config.app_secret,
            chat_id,
            build_message("前台会话已结束", lines, ["运行 <任务>", "前台运行", "状态"]),
        )
        update_chat_for_stop(state_path, chat_id, summary_text, finished_at)
        log_event(log_path, f"sent SessionEnd summary chat={chat_id} cwd={cwd or '-'} message_id={message_id or '-'}")
        return 0

    if hook_event_name == "Stop":
        # Stop 是标准完成路径，优先展示最近一轮摘要，再给出继续入口。
        summary_text = summarize_text(hook_input.get("last_assistant_message", ""))
        lines = [
            f"窗口：{get_active_window_title(existing_chat_state) or existing_chat_state.get('cwd') or cwd or '-'}",
            f"开始时间：{format_ts(started_at)}",
            f"结束时间：{format_ts(finished_at)}",
            f"总耗时：{format_duration(started_at, finished_at)}",
            "结果摘要：",
            summary_text,
        ]
        # 前台 Claude 窗口完成一轮后仍保持打开，因此要明确告诉用户会话还能继续，而不是误以为彻底结束。
        lines.append("说明：本轮前台执行已完成，Claude 窗口通常仍保持打开，可直接继续下一轮。")
        # 先发飞书再写状态，确保即便状态持久化失败，手机端也能先看到完成摘要。
        message_id = send_feishu_text(
            config.app_id,
            config.app_secret,
            chat_id,
            build_message("任务执行完成", lines, ["继续", "前台继续", "停止"]),
        )
        updated_state = update_chat_for_stop(state_path, chat_id, summary_text, finished_at)
        lines = [
            f"窗口：{get_active_window_title(updated_state) or updated_state.get('cwd') or cwd or '-'}",
            f"开始时间：{format_ts(started_at)}",
            f"结束时间：{format_ts(finished_at)}",
            f"总耗时：{format_duration(started_at, finished_at)}",
            "结果摘要：",
            summary_text,
        ]
        log_event(log_path, f"sent Stop summary chat={chat_id} cwd={cwd or '-'} message_id={message_id or '-'}")
        return 0

    # StopFailure 保留失败摘要和错误类型，方便用户决定继续、前台继续还是停止。
    error_text = summarize_text(
        hook_input.get("error_details") or hook_input.get("last_assistant_message") or hook_input.get("error") or "未知错误"
    )
    lines = [
        f"窗口：{get_active_window_title(existing_chat_state) or existing_chat_state.get('cwd') or cwd or '-'}",
        f"开始时间：{format_ts(started_at)}",
        f"结束时间：{format_ts(finished_at)}",
        f"总耗时：{format_duration(started_at, finished_at)}",
        f"异常类型：{_sanitize_text(hook_input.get('error') or '-')}",
        "异常摘要：",
        error_text,
        "说明：这一轮前台执行已因 Claude/API 错误结束，但当前会话仍保留，可继续或切到前台处理。",
    ]
    message_id = send_feishu_text(
        config.app_id,
        config.app_secret,
        chat_id,
        build_message("任务执行失败", lines, ["继续", "前台继续", "停止"]),
    )
    update_chat_for_failure(state_path, chat_id, error_text, finished_at)
    log_event(
        log_path,
        f"sent StopFailure summary chat={chat_id} cwd={cwd or '-'} error={hook_input.get('error') or '-'} message_id={message_id or '-'}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
