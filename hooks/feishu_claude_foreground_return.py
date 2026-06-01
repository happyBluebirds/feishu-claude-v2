#!/usr/bin/env python3
"""前台 Claude 会话的兜底完成通知脚本。

当前台窗口返回输入态，但 Claude 没有触发标准 Stop Hook 时，这个脚本负责补发
一条飞书完成通知，并把该轮状态写回共享 state。它只解决“本轮结束但 hook 缺失”
这一类场景，不替代正常 Stop/StopFailure。
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from typing import Any

from feishu_claude_turn_hook import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_HOOK_LOG_PATH,
    TurnHookConfig,
    apply_chat_runtime_updates,
    build_message,
    format_duration,
    format_ts,
    load_state,
    log_event,
    save_state,
    send_feishu_text,
)


def should_skip_fallback(chat_state: dict[str, Any], started_at: float, notice_kind: str = "completion") -> bool:
    """Decide whether the foreground return fallback should stay silent because a real hook already landed."""

    status = str(chat_state.get("status", ""))
    finished_at = chat_state.get("finished_at")
    if notice_kind == "stopped":
        # stopped 通知的语义是“前台 Claude 已经停住，把最后几行给用户决策”；
        # 不能被旧的 done/failed 状态吞掉，否则用户会继续不知道终端停在了哪里。
        return status == "stopped" and bool(finished_at) and float(finished_at) >= started_at
    if status in {"done", "failed", "waiting_auth", "stopped"} and finished_at:
        return float(finished_at) >= started_at
    return False


def update_chat_for_foreground_return(
    state_path: Path,
    chat_id: str,
    window_pid: int,
    started_at: float,
    finished_at: float,
    exit_code: int,
    summary_text: str,
    notice_kind: str = "completion",
) -> dict[str, Any]:
    """Persist a fallback completion state when the foreground Claude turn returned without any hook callback."""

    state = load_state(state_path)
    chats = state.setdefault("chats", {})
    chat_state = chats.setdefault(chat_id, {"cwd": "", "managed_session": True})
    # 前台命令只是本轮结束，窗口仍保留；因此这里把状态落成 done/failed，
    # 同时继续保留 foreground_pid 和可继续入口，保证飞书后续还能直接续跑。
    # 前台停止也写成 done，是因为 Claude 已经回到输入态；区别放在摘要和通知标题里表达。
    apply_chat_runtime_updates(
        chat_state,
        {
            "status": "done" if exit_code == 0 else "failed",
            "started_at": started_at,
            "finished_at": finished_at,
            "last_result": summary_text,
            "last_summary": summary_text,
            "last_error": "" if exit_code == 0 else summary_text,
            "active_pid": window_pid,
            "foreground_pid": window_pid,
            "pending_action": "continue_session",
            "pending_prompt": "继续",
            "managed_session": True,
            "last_exit_code": exit_code,
        },
    )
    save_state(state_path, state)
    return chat_state


def main() -> int:
    """Send a fallback completion notice when a foreground Claude command returns without any Stop hook."""

    parser = argparse.ArgumentParser(description="Foreground fallback notifier for Feishu Claude bot")
    parser.add_argument("--chat-id", required=True, help="Feishu chat id bound to the foreground Claude session")
    parser.add_argument("--cwd", default="", help="Working directory of the foreground Claude session")
    parser.add_argument("--started-at", required=True, type=float, help="Unix timestamp recorded when the turn started")
    parser.add_argument("--exit-code", default=0, type=int, help="Exit code returned by Claude for this foreground turn")
    parser.add_argument("--window-pid", default=0, type=int, help="Pwsh window pid that remains open after the turn")
    parser.add_argument("--summary", default="", help="Optional foreground transcript summary to send instead of generic fallback text")
    parser.add_argument("--notice-kind", default="completion", choices=("completion", "stopped"), help="Whether this notice is a normal completion or a foreground pause")
    args = parser.parse_args()

    config_path = DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return 0

    config = TurnHookConfig.load(config_path)
    log_path = Path(config.hook_log_path or DEFAULT_HOOK_LOG_PATH)
    state_path = Path(config.state_path)

    # 给真正的 Stop/StopFailure hook 留一个短窗口先落状态，避免同一轮出现重复通知。
    time.sleep(2.5)
    state = load_state(state_path)
    existing_chat_state = state.get("chats", {}).get(args.chat_id, {})
    if should_skip_fallback(existing_chat_state, args.started_at, args.notice_kind):
        log_event(log_path, f"skip foreground fallback chat={args.chat_id} started_at={args.started_at}")
        return 0

    finished_at = time.time()
    # 没拿到 Claude 的真实摘要时，fallback 文案只说明“前台返回了，但标准 Stop hook 没到”。
    summary_text = args.summary.strip() or (
        "前台本轮已返回输入态，但当前会话没有收到 Claude Stop hook 摘要；"
        "已按兜底逻辑通知飞书。"
        if args.exit_code == 0
        else "前台本轮已返回输入态，但当前会话没有收到 Claude Stop hook 摘要，且退出码非 0，请关注窗口内容。"
    )
    updated_state = update_chat_for_foreground_return(
        state_path,
        args.chat_id,
        args.window_pid,
        args.started_at,
        finished_at,
        args.exit_code,
        summary_text,
        args.notice_kind,
    )
    is_stopped_notice = args.notice_kind == "stopped"
    lines = [
        f"目录：{updated_state.get('cwd') or args.cwd or '-'}",
        f"开始时间：{format_ts(args.started_at)}",
        f"结束时间：{format_ts(finished_at)}",
        f"总耗时：{format_duration(args.started_at, finished_at)}",
        f"退出码：{args.exit_code}",
        "结果摘要：",
        summary_text,
    ]
    if is_stopped_notice:
        # 这种通知不猜测 Claude 是完成、提问还是等待决策，只说明“终端已停住并回到输入态”。
        lines.append("说明：Claude 已回到输入态。请根据上面的最近输出，直接回复下一步指令，机器人会送入当前前台窗口。")
    else:
        lines.append("说明：Claude 已回到输入态，但这一轮没有触发 Stop hook；机器人已用前台返回兜底补发完成通知。")
    next_steps = ["继续", "前台继续", "停止"]
    if is_stopped_notice:
        next_steps = ["继续", "停止"]
        option_numbers = re.findall(r"(?m)^(\d+)\.\s+", summary_text)
        if option_numbers:
            # 摘要里出现 AskUserQuestion 选项时，底部快捷回复优先给数字；授权说明留给 help，避免暂停通知过长。
            next_steps = [*option_numbers[:5], "同意 1", "停止"]

    message_id = send_feishu_text(
        config.app_id,
        config.app_secret,
        args.chat_id,
        build_message("Claude 已暂停" if is_stopped_notice else "任务执行完成", lines, next_steps),
    )
    log_event(
        log_path,
        f"sent foreground fallback chat={args.chat_id} cwd={args.cwd or '-'} exit_code={args.exit_code} notice_kind={args.notice_kind} message_id={message_id or '-'}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
