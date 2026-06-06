#!/usr/bin/env python3
"""Claude PermissionRequest Hook 的飞书桥接脚本。

这个文件只处理“Claude 请求本机授权”这一种事件。它会把待授权工具、目录、
风险说明和序号列表写进飞书，然后轮询共享 approvals 文件，等用户回复“同意/拒绝”
后再把结果回给 Claude CLI。
"""

from __future__ import annotations

import json
import os
import re
import site
import subprocess
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
        # Permission hook 要能在无人工干预时立刻发飞书消息，本地 vendor 目录优先于
        # 不稳定的用户级 site-packages，避免前台授权再次因为导包失败而静默回退。
        sys.path.append(vendor_site_packages)
    user_site_packages = site.getusersitepackages()
    if user_site_packages and user_site_packages not in sys.path:
        # 机器上的 Anaconda 解释器不会自动带上用户级 site-packages，这里补齐后
        # PermissionRequest hook 才能稳定 import 已装在用户目录里的 lark-oapi。
        sys.path.append(user_site_packages)
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody, CreateMessageResponse
except ImportError as exc:  # pragma: no cover - dependency bootstrap path
    raise SystemExit(
        "Missing dependency 'lark-oapi'. Install it with:\n"
        "python -m pip install lark-oapi"
    ) from exc


DEFAULT_CONFIG_PATH = INTEGRATION_ROOT / "config" / "feishu_claude_bot.v2.json"
DEFAULT_TIMEOUT_SECONDS = 8 * 60 * 60
# 首条飞书授权通知只展示当前批次，避免把同一 chat 里历史未清理的 pending 都发给用户。
CURRENT_BATCH_WINDOW_SECONDS = 10 * 60
DEFAULT_HOOK_LOG_PATH = CODEX_ROOT / "outputs" / "feishu-claude-v2" / "logs" / "feishu-claude-permission-hook.log"
LOG_ROTATE_MAX_BYTES = 512 * 1024
LOG_ROTATE_BACKUP_COUNT = 3


@dataclass
class HookConfig:
    """PermissionRequest hook 需要的最小配置集合。

    这个 hook 不负责启动 bot，也不负责执行任务，只负责把授权消息送回飞书并等结果。
    """

    # 飞书应用 App ID，用来创建消息客户端。
    app_id: str
    # 飞书应用 Secret，用于签发 OpenAPI 调用凭据。
    app_secret: str
    # 与 bot 共享的授权队列文件路径。
    approvals_path: str
    # 与 bot 共享的会话状态文件路径，用于在无环境变量时反查 chat_id。
    state_path: str
    # PowerShell 路径，供 hook 触发时拉起 bot 或辅助脚本。
    pwsh_path: str | None = None
    # Python 路径，适配当前机器上实际可用的解释器。
    python_path: str | None = None
    # Hook 专用日志路径，方便排障授权卡住、通知丢失或超时。
    hook_log_path: str | None = None

    @classmethod
    def load(cls, path: Path) -> "HookConfig":
        """Load routing settings from the shared bot JSON config."""

        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            app_id=data["app_id"],
            app_secret=data["app_secret"],
            approvals_path=data["approvals_path"],
            state_path=data["state_path"],
            pwsh_path=data.get("pwsh_path"),
            python_path=data.get("python_path"),
            hook_log_path=data.get("permission_hook_log_path"),
        )


def log_event(log_path: Path, message: str) -> None:
    """Append a timestamped hook diagnostic line for approval troubleshooting."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    rotate_log_if_needed(log_path)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def rotate_log_if_needed(log_path: Path) -> None:
    """Rotate approval hook logs by size to keep long-lived authorization flows from accumulating one giant file."""

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
        # 授权 hook 日志只需要保留最近几轮授权过程，避免历史审批噪音无限堆积。
        log_path.replace(log_path.with_name(f"{log_path.name}.1"))
    except OSError:
        return


def is_bot_running(bot_script_path: Path) -> bool:
    """Check whether the Feishu long-connection bot process is already alive."""

    bot_script_for_ps = str(bot_script_path).replace("'", "''")
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-CimInstance Win32_Process | "
            f"Where-Object {{ ([string]$_.CommandLine).Contains('{bot_script_for_ps}') }} | "
            "Select-Object -First 1 -ExpandProperty ProcessId"
        ),
    ]
    result = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        # 授权 hook 每次收到 PermissionRequest 都会探测 bot；隐藏 PowerShell，避免本机出现闪烁控制台。
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    return bool((result.stdout or "").strip())


def ensure_bot_running(config_path: Path, config: HookConfig) -> None:
    """Start the Feishu long-connection bot in background when Claude hooks wake up first."""

    script_dir = INTEGRATION_ROOT
    bot_script_path = script_dir / "app" / "feishu_claude_bot.py"
    bootstrap_script = script_dir / "app" / "bootstrap_feishu_tool.py"
    if not bot_script_path.exists():
        return
    if not bootstrap_script.exists():
        return
    if is_bot_running(bot_script_path):
        return

    creation_flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    python_path = config.python_path or sys.executable
    # Hook-triggered lazy startup lets a local Claude session recover the Feishu
    # bridge automatically after a reboot or crash, without waiting for manual relaunch.
    subprocess.Popen(
        [
            python_path,
            str(bootstrap_script),
            str(bot_script_path),
            "--config",
            str(config_path),
        ],
        cwd=str(script_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # 权限 hook 自动拉起 bot 属于后台恢复动作，避免弹出空白 Python 控制台。
        creationflags=creation_flags,
    )
    time.sleep(2)


def load_request_state(path: Path) -> dict[str, Any]:
    """Read the shared approval request store written by the bot and hook."""

    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"requests": {}}


def load_bot_state(path: Path) -> dict[str, Any]:
    """Read the shared bot chat state so hooks can infer which Feishu chat owns a local session."""

    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"chats": {}}


def save_request_state(path: Path, state: dict[str, Any]) -> None:
    """Persist approval request updates so the waiting hook can see decisions."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def expire_stale_pending_requests(state: dict[str, Any], now: float) -> int:
    """Mark old pending approvals as expired before rendering the current list."""

    expired_count = 0
    for request in state.get("requests", {}).values():
        if request.get("status") != "pending":
            continue
        created_at = float(request.get("created_at") or 0)
        if created_at and now - created_at > DEFAULT_TIMEOUT_SECONDS:
            # 旧进程崩溃或用户长期未处理时，pending 会残留在共享文件里；渲染前先过期，
            # 避免首条通知把历史授权混进当前 Claude 窗口的授权列表。
            request["status"] = "expired"
            request["resolved_at"] = now
            expired_count += 1
    return expired_count


def summarize_tool_input(tool_input: Any) -> str:
    """Create a compact one-line summary of the requested tool action."""

    if isinstance(tool_input, dict):
        if "command" in tool_input:
            return str(tool_input["command"])
        if "file_path" in tool_input:
            return str(tool_input["file_path"])
        return json.dumps(tool_input, ensure_ascii=False, separators=(",", ": "))
    return str(tool_input)


def clip_text(text: Any, limit: int = 600) -> str:
    """Clip long tool text so a Feishu approval message stays readable on mobile."""

    cleaned = str(text or "").strip()
    if not cleaned:
        return "-"
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "\n...(内容已截断)"


def describe_bash_approval(command: Any, description: Any = "") -> tuple[str, str]:
    """Return a Chinese action label and risk hint for a Bash PermissionRequest."""

    raw_command = str(command or "").strip()
    raw_description = str(description or "").strip()
    normalized_command = re.sub(r"\s+", " ", raw_command.lower())
    if raw_description:
        action = raw_description
    elif re.search(r"\bgit\s+diff\b.*\b--stat\b", normalized_command):
        action = "查看 Git 变更统计"
    elif re.search(r"\bgit\s+diff\b", normalized_command):
        action = "查看 Git 变更内容"
    elif re.search(r"\bgit\s+status\b", normalized_command):
        action = "查看 Git 工作区状态"
    elif re.search(r"\b(grep|find|select-string)\b", normalized_command):
        action = "搜索或读取文件内容"
    elif re.search(r"\b(ls|dir|get-childitem)\b", normalized_command):
        action = "查看目录内容"
    elif re.search(r"\b(cat|type|get-content|head|tail)\b", normalized_command):
        action = "读取文件内容"
    else:
        action = "执行 Bash 命令"

    destructive_patterns = [
        r"\b(rm|del|erase|rmdir|remove-item|move-item|mv|copy|cp)\b",
        r"\b(git\s+reset|git\s+checkout|git\s+clean|git\s+restore)\b",
        r"\b(npm|pnpm|yarn|pip|uv|mvn|gradle)\s+(install|add|remove|uninstall)\b",
        r"\b(set-content|out-file|tee-object)\b",
        r"(^|[^>])>([^>]|$)",
    ]
    readonly_patterns = [
        r"\bgit\s+(diff|status|show|log)\b",
        r"\b(grep|find|select-string|ls|dir|get-childitem|cat|type|get-content|head|tail|pwd)\b",
    ]
    if any(re.search(pattern, normalized_command) for pattern in destructive_patterns):
        # Hook 首条授权通知也要展示风险等级，因为用户可能直接回复“同意”批量放行。
        risk = "风险：可能修改文件、移动/删除内容、安装依赖或改变 Git 状态，请确认命令范围。"
    elif any(re.search(pattern, normalized_command) for pattern in readonly_patterns):
        risk = "风险：只读查询，通常不会修改文件。"
    else:
        risk = "风险：会在本机执行 shell 命令，请确认目录和命令内容。"
    return action, risk


def build_tool_risk_hint(tool_name: str, tool_input: Any) -> str:
    """Build a Chinese risk hint for a tool approval request."""

    normalized_tool = tool_name.lower()
    if normalized_tool == "askuserquestion":
        return "风险：Claude 想向你提问以确认下一步，本工具本身不会修改文件。"
    if normalized_tool == "bash" and isinstance(tool_input, dict):
        _, risk = describe_bash_approval(tool_input.get("command"), tool_input.get("description"))
        return risk
    if normalized_tool in {"edit", "multiedit", "write", "notebookedit"}:
        return "风险：会修改文件内容，请确认文件路径和变更片段。"
    if normalized_tool in {"read", "glob", "grep", "ls"}:
        return "风险：只读查看，通常不会修改文件。"
    return "风险：需要 Claude 调用本机工具，请确认参数内容。"


def build_ask_user_question_details(tool_input: dict[str, Any]) -> list[str]:
    """Format Claude AskUserQuestion payload into a readable mobile approval card."""

    lines = [build_tool_risk_hint("AskUserQuestion", tool_input)]
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not questions:
        lines.extend(["提问内容：", clip_text(json.dumps(tool_input, ensure_ascii=False, indent=2), 1000)])
        return lines

    for question_index, question_item in enumerate(questions, start=1):
        if not isinstance(question_item, dict):
            lines.extend([f"问题 {question_index}：", clip_text(question_item, 800)])
            continue
        header = str(question_item.get("header") or f"问题 {question_index}").strip()
        question_text = str(question_item.get("question") or "").strip()
        if len(questions) == 1:
            lines.append(f"主题：{header}")
            lines.append(f"问题：{question_text or '-'}")
        else:
            lines.append(f"问题 {question_index}：{header}")
            lines.append(question_text or "-")
        options = question_item.get("options")
        if isinstance(options, list) and options:
            lines.append("可选方案：")
            for option_index, option in enumerate(options, start=1):
                if isinstance(option, dict):
                    label = str(option.get("label") or f"选项 {option_index}").strip()
                    description = str(option.get("description") or "").strip()
                    lines.append(f"{option_index}. {label}")
                    if description:
                        lines.append(f"   说明：{description}")
                else:
                    lines.append(f"{option_index}. {option}")
        multi_select = bool(question_item.get("multiSelect"))
        lines.append(f"选择方式：{'可多选' if multi_select else '单选'}")

    lines.extend(
        [
            "选择方式：",
            "同意 1 = 选择第 1 个方案",
            "拒绝 1 = 拒绝第 1 个方案",
            "同意 = 同意当前推荐/全部可执行项",
            "拒绝 = 拒绝当前问题/全部可执行项",
        ]
    )
    return lines


def build_tool_approval_details(tool_name: str, tool_input: Any) -> list[str]:
    """Build Chinese approval details that explain what Claude is asking to do."""

    if not isinstance(tool_input, dict):
        return ["请求内容：", clip_text(tool_input, 800)]

    normalized_tool = tool_name.lower()
    lines = [build_tool_risk_hint(tool_name, tool_input)]
    if normalized_tool == "askuserquestion":
        return build_ask_user_question_details(tool_input)

    if normalized_tool in {"edit", "multiedit"}:
        lines.append(f"文件：{tool_input.get('file_path') or '-'}")
        if "old_string" in tool_input:
            # Edit 的关键风险在“删掉什么、换成什么”；手机审批时必须展示差异内容，而不是只给文件名。
            lines.extend(["原内容：", clip_text(tool_input.get("old_string"), 800)])
        if "new_string" in tool_input:
            lines.extend(["新内容：", clip_text(tool_input.get("new_string"), 800)])
        if "edits" in tool_input:
            lines.append(f"批量编辑数量：{len(tool_input.get('edits') or [])}")
        return lines

    if normalized_tool in {"write", "notebookedit"}:
        lines.extend(
            [
                f"文件：{tool_input.get('file_path') or tool_input.get('notebook_path') or '-'}",
                "写入内容预览：",
                clip_text(tool_input.get("content") or tool_input.get("new_source"), 1000),
            ]
        )
        return lines

    if normalized_tool == "bash":
        action, _ = describe_bash_approval(tool_input.get("command"), tool_input.get("description"))
        lines.extend(
            [
                f"动作：{action}",
                "Bash 命令：",
                clip_text(tool_input.get("command"), 1000),
                "执行方式：shell 命令",
            ]
        )
        if tool_input.get("description"):
            # Claude 的本机授权卡片只有英文动作；飞书端补中文说明，便于手机端判断是否授权。
            lines.append(f"说明：{tool_input.get('description')}")
        return lines

    if normalized_tool in {"read", "glob", "grep", "ls"}:
        lines.append(f"读取范围：{clip_text(tool_input, 800)}")
        return lines

    lines.extend(["请求参数：", clip_text(json.dumps(tool_input, ensure_ascii=False, indent=2), 1000)])
    return lines


def build_pending_approval_hint() -> list[str]:
    """Return Chinese instructions for approving one item by list index."""

    return [
        "可直接回复：",
        "同意 = 同意全部或当前推荐项",
        "拒绝 = 拒绝全部或当前问题",
        "同意 1 = 同意/选择第 1 条",
        "拒绝 1 = 拒绝第 1 条",
        "同意 1 3 = 同意/选择第 1、3 条",
        "同意 1,3 = 同意/选择第 1、3 条",
        "全部授权 = 同意全部",
        "授权 = 查看所有待授权项目",
        "说明：列表里的编号统一使用，同意 1 / 拒绝 1 都表示处理第 1 条。",
    ]


def find_pending_approvals(
    state: dict[str, Any],
    chat_id: str,
    session_id: str | None = None,
    created_at: float | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Return current pending approval requests in the same order as the bot list."""

    requests = state.get("requests", {})
    matches = [
        (request_id, request)
        for request_id, request in requests.items()
        if request.get("chat_id") == chat_id
        and request.get("status") == "pending"
        and (not session_id or request.get("session_id") == session_id)
        and (
            created_at is None
            # 首条通知只展示当前批次附近生成的授权，避免历史 pending 和当前 Claude 屏幕不一致。
            or abs(float(request.get("created_at") or 0) - created_at) <= CURRENT_BATCH_WINDOW_SECONDS
        )
    ]
    # 飞书里“同意 1”按列表第 1 条处理；这里与 bot 保持“最新在前”，避免首条通知和授权清单排序不一致。
    matches.sort(key=lambda item: float(item[1].get("created_at", 0)), reverse=True)
    return matches


def build_approval_action_label(tool_name: str, tool_input: Any) -> str:
    """Return a short Chinese action label for one approval list item."""

    normalized_tool = tool_name.lower()
    if normalized_tool == "askuserquestion" and isinstance(tool_input, dict):
        questions = tool_input.get("questions")
        if isinstance(questions, list) and questions and isinstance(questions[0], dict):
            # 提问类授权优先展示主题，手机端扫列表时能先看出是在问什么方向。
            return str(questions[0].get("header") or questions[0].get("question") or "用户提问").strip()
        return "用户提问"
    if normalized_tool == "bash" and isinstance(tool_input, dict):
        action, _ = describe_bash_approval(tool_input.get("command"), tool_input.get("description"))
        return action
    if isinstance(tool_input, dict):
        file_path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "").strip()
        if file_path:
            return f"{tool_name} {file_path}"
    return tool_name


def summarize_approval_request(request: dict[str, Any], index: int) -> list[str]:
    """Render one pending approval request as a numbered Chinese list item."""

    tool_name = str(request.get("tool_name") or "-")
    tool_input = request.get("tool_input")
    lines = [f"{index}. {tool_name}"]
    cwd = str(request.get("cwd") or "").strip()
    if cwd:
        lines.append(f"目录：{cwd}")
    lines.append(f"动作：{build_approval_action_label(tool_name, tool_input)}")
    # 首条授权通知直接带完整清单，避免用户还要回复“授权”二次查看后才能决策。
    lines.extend(build_tool_approval_details(tool_name, tool_input))
    return lines


def build_pending_approvals_message(state: dict[str, Any], chat_id: str, request_id: str) -> str:
    """Build the first Feishu approval message with the full pending approval list included."""

    current_request = state.get("requests", {}).get(request_id, {})
    session_id = str(current_request.get("session_id") or "")
    created_at = float(current_request.get("created_at") or 0)
    pending_matches = find_pending_approvals(state, chat_id, session_id=session_id, created_at=created_at)
    lines = ["权限请求", f"本次新增：{request_id}", f"待授权项目：{len(pending_matches)} 条"]
    for index, (_, request) in enumerate(pending_matches, start=1):
        lines.extend(summarize_approval_request(request, index))
        lines.append("")
    lines.extend(build_pending_approval_hint())
    return "\n".join(line for line in lines if line is not None).strip()


def resolve_chat_id_from_state(config: HookConfig, cwd: str, log_path: Path) -> str:
    """Infer the Feishu chat for a local foreground Claude session when env metadata is missing."""

    state = load_bot_state(Path(config.state_path))
    chats: dict[str, Any] = state.get("chats", {})
    if not chats:
        log_event(log_path, "resolve chat: state file has no chats")
        return ""

    scored_matches: list[tuple[int, float, str]] = []
    for candidate_chat_id, chat_state in chats.items():
        status = str(chat_state.get("status", ""))
        candidate_cwd = str(chat_state.get("cwd", ""))
        started_at = float(chat_state.get("started_at") or 0)
        managed_session = bool(chat_state.get("managed_session"))
        active_pid = chat_state.get("active_pid")
        score = 0
        # 优先绑定仍在托管中的前台会话；这类会话最可能是用户当前手动操作的 Claude 窗口。
        if managed_session and status in {"foreground_opened", "foreground_busy", "foreground_running"}:
            score += 10
        # 工作目录一致时，说明这个 hook 与飞书里选定的项目上下文高度相关。
        if cwd and candidate_cwd and cwd.lower() == candidate_cwd.lower():
            score += 5
        # 仍有活动进程的聊天更可信，避免把审批误发给已经失活的旧会话。
        if active_pid:
            score += 2
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


def send_feishu_text(app_id: str, app_secret: str, chat_id: str, text: str) -> None:
    """Send a text message into the assistant chat via Feishu OpenAPI."""

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
    response: CreateMessageResponse = client.im.v1.message.create(request)
    if not response.success():
        raise RuntimeError(f"send message failed: code={response.code} msg={response.msg}")


def build_allow_response(updated_input: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the hook JSON that grants the waiting permission request."""

    decision: dict[str, Any] = {"behavior": "allow"}
    if updated_input:
        # AskUserQuestion 这类工具允许 hook 在放行时补充用户选择；飞书端会把
        # “同意 1”转换成 answers，再通过 updatedInput 交回 Claude。
        decision["updatedInput"] = updated_input
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }
    }


def build_deny_response(message: str) -> dict[str, Any]:
    """Return the hook JSON that denies the waiting permission request."""

    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": message,
                "interrupt": True,
            },
        }
    }


def main() -> int:
    """Wait for a Feishu approval reply and answer Claude's PermissionRequest hook."""

    # Claude hook payloads are UTF-8 JSON. Reading raw bytes avoids Windows locale
    # decoding corrupting Chinese paths or tool descriptions before json.loads.
    raw_input = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    if not raw_input.strip():
        return 0

    hook_input = json.loads(raw_input)
    if hook_input.get("hook_event_name") != "PermissionRequest":
        return 0

    config_path = Path(os.environ.get("FEISHU_CLAUDE_BOT_CONFIG_PATH", str(DEFAULT_CONFIG_PATH)))
    approvals_path_value = os.environ.get("FEISHU_CLAUDE_BOT_APPROVALS_PATH", "").strip()
    if not config_path.exists():
        return 0

    config = HookConfig.load(config_path)
    log_path = Path(config.hook_log_path or DEFAULT_HOOK_LOG_PATH)
    cwd = str(hook_input.get("cwd", ""))
    # 优先使用环境变量里的 chat_id；如果前台窗口是用户手动打开的，再回查 state 反推所属飞书会话。
    chat_id = os.environ.get("FEISHU_CLAUDE_BOT_CHAT_ID", "").strip()
    if not chat_id:
        # 前台手动会话不会天然带上飞书 chat_id；这里回查 bot state，把权限请求尽量路由回
        # 当前托管的飞书聊天，而不是直接退回本地弹框导致手机端完全无感。
        chat_id = resolve_chat_id_from_state(config, cwd, log_path)
    if not chat_id:
        log_event(log_path, f"skip PermissionRequest: missing chat id cwd={cwd or '-'}")
        # Without bot routing metadata, fall back to Claude's native local prompt.
        return 0

    ensure_bot_running(config_path, config)
    approvals_path = Path(approvals_path_value or config.approvals_path)
    session_id = str(hook_input.get("session_id", "unknown"))
    created_at = time.time()
    request_id = f"{session_id}-{int(created_at)}"
    tool_name = str(hook_input.get("tool_name", "-"))
    tool_input = hook_input.get("tool_input")
    state = load_request_state(approvals_path)
    expired_count = expire_stale_pending_requests(state, created_at)
    if expired_count:
        # 自动过期历史残留后立即落盘，bot 侧“授权”命令也不会再看到这些旧请求。
        log_event(log_path, f"expired stale approvals count={expired_count}")
    state.setdefault("requests", {})[request_id] = {
        "chat_id": chat_id,
        "session_id": session_id,
        "cwd": cwd,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "status": "pending",
        "created_at": created_at,
    }
    save_request_state(approvals_path, state)
    # 先发飞书，再开始轮询审批状态；这样手机端能第一时间看到授权卡片。
    log_event(log_path, f"queued approval request_id={request_id} chat={chat_id} tool={tool_name} cwd={cwd or '-'}")

    send_feishu_text(
        config.app_id,
        config.app_secret,
        chat_id,
        build_pending_approvals_message(state, chat_id, request_id),
    )

    deadline = created_at + DEFAULT_TIMEOUT_SECONDS
    while time.time() < deadline:
        state = load_request_state(approvals_path)
        request = state.get("requests", {}).get(request_id, {})
        status = request.get("status")
        if status == "approved":
            # 飞书里回复同意后，只返回 allow，不再附带额外失败信息。
            log_event(log_path, f"approval granted request_id={request_id}")
            updated_input = request.get("updated_input")
            print(json.dumps(build_allow_response(updated_input if isinstance(updated_input, dict) else None), ensure_ascii=False))
            return 0
        if status == "denied":
            # 拒绝时返回 deny，并带上中文说明，方便 Claude 直接中止本次工具调用。
            log_event(log_path, f"approval denied request_id={request_id}")
            print(json.dumps(build_deny_response("飞书已拒绝本次授权请求。"), ensure_ascii=False))
            return 0
        time.sleep(2)

    state = load_request_state(approvals_path)
    request = state.get("requests", {}).get(request_id)
    if request and request.get("status") == "pending":
        # 超时后把请求标成 expired，避免下次状态查询还把它当成活跃待办。
        request["status"] = "expired"
        request["resolved_at"] = time.time()
        save_request_state(approvals_path, state)
    log_event(log_path, f"approval timeout request_id={request_id}")
    print(json.dumps(build_deny_response("飞书授权等待超时，已拒绝本次操作。"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
