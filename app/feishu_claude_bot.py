#!/usr/bin/env python3
"""飞书助手机器人主控入口。

这个文件负责把“飞书文本消息”转换为“本机 Claude Code 操作”，并把任务开始、
授权请求、阶段完成、截图、状态查询等结果再发回飞书。整体链路可以按下面理解：

1. 飞书长连接 SDK 收到消息，入口先做去重、空白字符清洗和控制命令预处理。
2. `handle_command()` 判断消息属于配置、授权、状态、截图、前台窗口还是后台任务。
3. 后台任务由 `_execute_claude_task()` 以 `--print` 方式执行，输出由 bot 汇总。
4. 前台任务由 `_launch_foreground_task()` 打开可接管窗口，后续命令优先注入同一窗口。
5. Claude 的 PermissionRequest / Stop 等 Hook 通过共享 state/approvals 文件回写状态。

维护时最重要的约束：一个飞书聊天默认只托管一个 Claude 会话；只要用户没有主动
“停止”，后续“继续/前台继续/普通文本”都应尽量复用原会话，不能静默另起并发任务。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import re
import site
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

INTEGRATION_ROOT = Path(__file__).resolve().parents[1]
CODEX_ROOT = INTEGRATION_ROOT.parents[1]
APP_ROOT = Path(__file__).resolve().parent
integrations_root = CODEX_ROOT / "integrations"
if str(integrations_root) not in sys.path:
    sys.path.insert(0, str(integrations_root))
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

try:
    vendor_site_packages = str(INTEGRATION_ROOT / "vendor")
    if os.path.isdir(vendor_site_packages) and vendor_site_packages not in sys.path:
        sys.path.append(vendor_site_packages)
    user_site_packages = site.getusersitepackages()
    if user_site_packages and user_site_packages not in sys.path:
        sys.path.append(user_site_packages)
    import lark_oapi as lark
except ImportError as exc:
    raise SystemExit(
        "Missing dependency 'lark-oapi'. Install it with:\n"
        "python -m pip install lark-oapi"
    ) from exc

from command_router import CommandIntent, CommandKind, CommandRouter
from feishu_gateway import FeishuGateway, FeishuGatewayConfig, IncomingTextMessage
from foreground_adapter import ForegroundAdapter
from session_manager import SessionManager


# 默认配置模板路径。
DEFAULT_CONFIG_PATH = INTEGRATION_ROOT / "config" / "feishu_claude_bot.v2.example.json"
# 运行态产物独立放到 outputs/feishu-claude-v2。
DEFAULT_OUTPUT_ROOT = CODEX_ROOT / "outputs" / "feishu-claude-v2"
# bot 与 hook 共享的会话状态文件，保存每个飞书 chat 当前绑定的 Claude 会话信息。
DEFAULT_STATE_PATH = DEFAULT_OUTPUT_ROOT / "state" / "feishu-claude-bot-state.json"
# bot 主日志路径，记录飞书消息、命令分流、异常和关键状态迁移。
DEFAULT_LOG_PATH = DEFAULT_OUTPUT_ROOT / "logs" / "feishu-claude-bot.log"
# PermissionRequest hook 与 bot 共享的授权队列，飞书回复“同意/拒绝”会写入这里。
DEFAULT_APPROVALS_PATH = DEFAULT_OUTPUT_ROOT / "state" / "feishu-claude-bot-approvals.json"
# 前台窗口默认使用 PowerShell 7，避免 Windows PowerShell 5.1 的编码和终端行为差异。
DEFAULT_PWSH_PATH = r"C:\Program Files\PowerShell\7\pwsh.exe"
# 前台 Stop hook 丢失时的兜底完成通知脚本。
DEFAULT_FOREGROUND_RETURN_HELPER_PATH = INTEGRATION_ROOT / "hooks" / "feishu_claude_foreground_return.py"
# 前台窗口观察器脚本，用于监控 claude.exe 子进程和 JSONL/transcript 摘要。
DEFAULT_FOREGROUND_WATCH_HELPER_PATH = INTEGRATION_ROOT / "hooks" / "feishu_claude_foreground_watch.py"
# Claude Code 默认日志根目录，读取 JSONL 摘要时会按 cwd 编码规则进入 projects 子目录。
DEFAULT_CLAUDE_HOME = Path.home() / ".claude"
# 临时 launcher、截图等产物只服务短期排障；默认保留 12 小时后自动清理。
TEMP_OUTPUT_RETENTION_SECONDS = 12 * 60 * 60
# 主日志轮转阈值，避免长连接机器人跑久后生成过大的单文件日志。
LOG_ROTATE_MAX_BYTES = 1024 * 1024
# 主日志保留份数；配合轮转用于追最近几次问题，不长期沉积。
LOG_ROTATE_BACKUP_COUNT = 3
# 飞书发送失败通常是临时网络/令牌波动，重试可以减少“任务完成但没通知”的体感问题。
FEISHU_SEND_RETRY_COUNT = 3
# 重试间隔不能太短，否则飞书 OpenAPI 短暂抖动期间会连续失败。
FEISHU_SEND_RETRY_DELAY_SECONDS = 1.5
# 读取 Claude JSONL 时只取尾部，避免大型长期会话状态查询时卡住机器人。
CLAUDE_JSONL_TAIL_BYTES = 1024 * 1024
# 用于识别“这段输出像阶段完成/轮次摘要”的关键词，状态查询和前台观察器都会复用。
CLAUDE_SUMMARY_ANCHORS = (
    "Round ",
    "进入 Round",
    "本轮迭代已完成",
    "进入下一轮检查",
    "任务执行完成",
    "任务已完成",
)

# Terminal process names that host Claude Code foreground sessions.
_TERMINAL_PROCESS_NAMES = frozenset({"windowsterminal", "openconsole", "pwsh", "powershell"})


def _find_visible_window_by_title(title_part: str, *, require_terminal_process: bool = True) -> int:
    """Return the HWND of a visible window whose title contains *title_part*.

    When *require_terminal_process* is True only windows owned by known terminal
    processes (WindowsTerminal, OpenConsole, pwsh, powershell) are considered.
    Returns 0 if no match is found.
    """

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    found = [0]
    title_lower = title_part.lower()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def enum_callback(hwnd_cb, _lparam):
        if not user32.IsWindowVisible(hwnd_cb):
            return True
        length = user32.GetWindowTextLengthW(hwnd_cb)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd_cb, buf, length + 1)
        if title_lower not in buf.value.lower():
            return True
        if require_terminal_process:
            proc_id = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd_cb, ctypes.byref(proc_id))
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            proc_handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, proc_id.value)
            if not proc_handle:
                return True
            try:
                exe_buf = ctypes.create_unicode_buffer(512)
                size = ctypes.wintypes.DWORD(512)
                # QueryFullProcessImageNameW returns the exe path.
                if not kernel32.QueryFullProcessImageNameW(proc_handle, 0, exe_buf, ctypes.byref(size)):
                    return True
                exe_name = os.path.basename(exe_buf.value).rsplit(".", 1)[0].lower()
                if exe_name not in _TERMINAL_PROCESS_NAMES:
                    return True
            finally:
                kernel32.CloseHandle(proc_handle)
        found[0] = hwnd_cb
        return False

    user32.EnumWindows(enum_callback, 0)
    return found[0]


@dataclass
class BotConfig:
    """飞书机器人运行配置。

    配置来自 `config/feishu_claude_bot.v2.json`，主程序、权限 Hook、完成 Hook 都会读取其中
    的共享路径。字段尽量保持显式，是为了让排障时能直接判断“消息发不出、状态不同步、
    Claude 启动参数不对”分别对应哪一类配置。
    """

    # 飞书开放平台应用 App ID，用于长连接收消息和 OpenAPI 发消息。
    app_id: str
    # 飞书开放平台应用 Secret；只在本机配置文件保存，不应写入文档或日志。
    app_secret: str
    # Claude Code 可执行文件路径，当前机器由 ccgui/codemoss SDK 安装目录提供。
    claude_path: str
    # 默认工作目录；当某个 chat 还没有执行“目录 <路径>”时使用它。
    default_cwd: str
    # 常用项目目录别名，例如“5a/主数据/数据治理”，降低手机端输入成本。
    cwd_aliases: dict[str, str] = field(default_factory=dict)
    # 允许使用机器人的飞书 chat_id 白名单；为空时通常表示不限制。
    allowed_chat_ids: list[str] = field(default_factory=list)
    # 全局默认授权模式；前台/后台未单独配置时使用该值。
    permission_mode: str = "bypassPermissions"
    # 后台任务专用授权模式；为空时回退到 permission_mode。
    background_permission_mode: str | None = None
    # 前台窗口专用授权模式；为空时回退到 permission_mode。
    foreground_permission_mode: str | None = None
    # 默认模型参数；为空时让 Claude CLI 使用自身默认模型。
    default_model: str = ""
    # 透传给 Claude CLI 的额外参数，适合放全局固定开关，不适合放一次性任务文本。
    additional_args: list[str] = field(default_factory=list)
    # 会话状态文件路径，bot 与 hook 通过它同步状态。
    state_path: str = str(DEFAULT_STATE_PATH)
    # bot 主日志路径。
    log_path: str = str(DEFAULT_LOG_PATH)
    # 授权队列文件路径，PermissionRequest hook 写入，bot 根据飞书回复更新。
    approvals_path: str = str(DEFAULT_APPROVALS_PATH)
    # 权限 Hook 日志路径；为空时 hook 使用默认输出目录。
    permission_hook_log_path: str = ""
    # Stop/StopFailure Hook 日志路径；为空时 hook 使用默认输出目录。
    turn_hook_log_path: str = ""
    # Hook 自动拉起 bot 的日志路径，排查“先开 Claude 后开机器人”场景。
    autostart_log_path: str = ""
    # 单条飞书回复最大字符数，避免长摘要在移动端难读或超过接口限制。
    reply_max_chars: int = 3500
    # 用于启动前台窗口和执行 Windows 进程探测的 PowerShell 路径。
    pwsh_path: str = DEFAULT_PWSH_PATH
    # 指定运行 Hook/辅助脚本的 Python；为空时使用当前解释器。
    python_path: str = ""

    @classmethod
    def load(cls, path: Path) -> "BotConfig":
        """Load JSON config from disk."""

        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)


class BotState:
    """按飞书 chat 持久化的会话状态，支持同一聊天内多个并发 Claude 会话。

    state 文件是整个方案的单一事实源：飞书命令、后台 worker、前台 watcher、
    PermissionRequest hook、Stop hook 都通过它判断同一个聊天当前处于什么阶段。

    多会话架构：每个 chat_id 下维护一个 sessions 字典，每个 session 有独立的状态；
    active_session 标记当前默认操作的会话。旧 state 文件（无 sessions 字段）会自动
    升级：将原有字段迁移到 session_id=s1 中，保持向后兼容。
    """

    DEFAULT_CHAT_STATE = {
        "cwd": "",
        "permission_mode": "",
        "model": "",
        "sessions": {},
        "active_session": "",
    }

    DEFAULT_SESSION_STATE = {
        "last_command": "",
        "last_result": "",
        "last_summary": "",
        "status": "idle",
        "started_at": None,
        "finished_at": None,
        "last_error": "",
        "active_pid": None,
        "foreground_pid": None,
        "pending_action": "",
        "pending_prompt": "",
        "last_exit_code": None,
        "managed_session": False,
        "runtime_permission_mode": "",
        "runtime_model": "",
        "runtime_settings_pending_restart": False,
        "live_output": "",
        "live_output_at": None,
        "foreground_transcript_path": "",
        "foreground_last_completion_marker": "",
        # 当前飞书会话选中的实时 Claude 窗口 HWND；必须进入默认 session 字段，否则 get_chat 合并视图会丢失窗口选择。
        "active_window_hwnd": None,
        # 当前选中窗口标题仅用于飞书展示，窗口实际路由以 active_window_hwnd 为准。
        "active_window_title": "",
        # 最近一次发送给飞书的窗口列表快照；切换窗口时按快照里的序号找 HWND，避免实时枚举顺序漂移导致串窗。
        "last_window_targets": [],
        # 窗口列表快照生成时间；过旧快照不再参与切换，防止用户按历史列表选择已关闭窗口。
        "last_window_targets_at": None,
        # 前台 watcher 从 Claude JSONL 提取的 AskUserQuestion 选项映射，用于飞书数字回复转成选项文本。
        "foreground_pending_question": {},
    }

    _SESSION_ID_COUNTER_KEY = "_next_session_seq"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            raw_text = self.path.read_text(encoding="utf-8", errors="replace").strip()
            if not raw_text:
                return {"chats": {}}
            return json.loads(raw_text)
        return {"chats": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _generate_session_id(self, existing_sessions=None):
        seq = self.data.get(self._SESSION_ID_COUNTER_KEY, 1)
        existing = existing_sessions or {}
        while f"s{seq}" in existing:
            seq += 1
        self.data[self._SESSION_ID_COUNTER_KEY] = seq + 1
        return f"s{seq}"

    def _migrate_legacy_chat_state(self, chat_state, default_cwd):
        if "sessions" in chat_state:
            return False
        session_state = {}
        for key, default_value in self.DEFAULT_SESSION_STATE.items():
            if key in chat_state:
                session_state[key] = chat_state.pop(key)
            else:
                session_state[key] = default_value
        if session_state.get("status") == "waiting_continue":
            session_state["status"] = "failed" if session_state.get("last_error") else "done"
            session_state["managed_session"] = True
            session_state["pending_action"] = session_state.get("pending_action") or "continue_session"
            session_state["pending_prompt"] = session_state.get("pending_prompt") or "继续"
        chat_state["sessions"] = {"s1": session_state}
        chat_state["active_session"] = "s1"
        for key, value in self.DEFAULT_CHAT_STATE.items():
            if key in ("sessions", "active_session"):
                continue
            chat_state.setdefault(key, default_cwd if key == "cwd" else value)
        if not chat_state.get("cwd"):
            chat_state["cwd"] = default_cwd
        return True

    def get_chat(self, chat_id, default_cwd):
        with self.lock:
            chat_state = self._get_or_create_chat(chat_id, default_cwd)
            return self._merge_chat_view(chat_state)

    def update_chat(self, chat_id, updates, default_cwd):
        with self.lock:
            chat_state = self._get_or_create_chat(chat_id, default_cwd)
            chat_level_keys = set(self.DEFAULT_CHAT_STATE.keys()) - {"sessions", "active_session"}
            session = self._get_active_session(chat_state)
            for key, value in updates.items():
                if key in chat_level_keys:
                    chat_state[key] = value
                else:
                    session[key] = value
            self.save()
            return self._merge_chat_view(chat_state)

    def _get_or_create_chat(self, chat_id, default_cwd):
        chats = self.data.setdefault("chats", {})
        chat_state = chats.setdefault(chat_id, {})
        mutated = self._migrate_legacy_chat_state(chat_state, default_cwd)
        for key, value in self.DEFAULT_CHAT_STATE.items():
            if key in ("sessions", "active_session"):
                continue
            if key not in chat_state:
                chat_state[key] = default_cwd if key == "cwd" else value
                mutated = True
        if not chat_state.get("cwd"):
            chat_state["cwd"] = default_cwd
            mutated = True
        sessions = chat_state.get("sessions", {})
        active = chat_state.get("active_session", "")
        if not active or active not in sessions:
            if sessions:
                chat_state["active_session"] = next(iter(sessions))
            else:
                new_session = dict(self.DEFAULT_SESSION_STATE)
                sessions["s1"] = new_session
                chat_state["sessions"] = sessions
                chat_state["active_session"] = "s1"
            mutated = True
        if mutated:
            self.save()
        return chat_state

    def _get_active_session(self, chat_state):
        sessions = chat_state.setdefault("sessions", {})
        active_id = chat_state.get("active_session", "")
        if active_id and active_id in sessions:
            return sessions[active_id]
        if sessions:
            first_id = next(iter(sessions))
            chat_state["active_session"] = first_id
            return sessions[first_id]
        new_session = dict(self.DEFAULT_SESSION_STATE)
        sessions["s1"] = new_session
        chat_state["active_session"] = "s1"
        return new_session

    def _merge_chat_view(self, chat_state):
        view = {}
        for key in self.DEFAULT_CHAT_STATE:
            if key in ("sessions", "active_session"):
                continue
            view[key] = chat_state.get(key, self.DEFAULT_CHAT_STATE.get(key))
        session = self._get_active_session(chat_state)
        for key, value in self.DEFAULT_SESSION_STATE.items():
            view[key] = session.get(key, value)
        return view

    def get_session(self, chat_id, session_id, default_cwd):
        with self.lock:
            chat_state = self._get_or_create_chat(chat_id, default_cwd)
            return chat_state.get("sessions", {}).get(session_id)

    def update_session(self, chat_id, session_id, updates, default_cwd):
        with self.lock:
            chat_state = self._get_or_create_chat(chat_id, default_cwd)
            session = chat_state.get("sessions", {}).get(session_id)
            if session is None:
                return None
            session.update(updates)
            self.save()
            return session

    def create_session(self, chat_id, default_cwd, label=""):
        with self.lock:
            chat_state = self._get_or_create_chat(chat_id, default_cwd)
            sessions = chat_state.setdefault("sessions", {})
            session_id = self._generate_session_id(sessions)
            new_session = dict(self.DEFAULT_SESSION_STATE)
            new_session["cwd"] = chat_state.get("cwd", default_cwd)
            if label:
                new_session["label"] = label
            sessions[session_id] = new_session
            chat_state["active_session"] = session_id
            self.save()
            return session_id, new_session

    def remove_session(self, chat_id, session_id, default_cwd):
        with self.lock:
            chat_state = self._get_or_create_chat(chat_id, default_cwd)
            sessions = chat_state.get("sessions", {})
            if session_id not in sessions:
                return False
            if len(sessions) <= 1:
                return False
            del sessions[session_id]
            if chat_state.get("active_session") == session_id:
                chat_state["active_session"] = next(iter(sessions))
            self.save()
            return True

    def list_sessions(self, chat_id, default_cwd):
        with self.lock:
            chat_state = self._get_or_create_chat(chat_id, default_cwd)
            return list(chat_state.get("sessions", {}).items())

    def set_active_session(self, chat_id, session_id, default_cwd):
        with self.lock:
            chat_state = self._get_or_create_chat(chat_id, default_cwd)
            if session_id not in chat_state.get("sessions", {}):
                return False
            chat_state["active_session"] = session_id
            self.save()
            return True

    def get_active_session_id(self, chat_id, default_cwd):
        with self.lock:
            chat_state = self._get_or_create_chat(chat_id, default_cwd)
            return chat_state.get("active_session", "s1")



class FeishuClaudeBot:
    """飞书到 Claude Code 的命令路由器。

    该类维护三类边界：
    1. 飞书入口边界：消息去重、白名单、文本归一化、控制命令硬拦截。
    2. Claude 运行边界：后台 `--print` 任务、前台可接管窗口、已有前台窗口注入。
    3. 状态同步边界：本进程 worker 与外部 Hook 共同读写 state/approvals。

    分支维护原则：截图、授权、状态、目录、模型、权限等“机器人控制命令”必须在进入
    Claude 之前消费掉；只有无法匹配控制命令的普通文本，才会被当作用户任务交给 Claude。
    """

    # 内部状态到中文展示文案的映射；新增状态时必须同步状态页、停止逻辑和 Hook 分支。
    STATUS_LABELS = {
        "idle": "空闲",
        "running": "后台执行中",
        "foreground_running": "前台拉起中",
        "foreground_opened": "前台已打开，等待接管",
        "foreground_busy": "前台执行中",
        "waiting_auth": "等待授权后继续",
        "done": "已完成",
        "failed": "已失败",
        "stopped": "已停止",
        "foreground_closed": "前台窗口已关闭",
        "waiting_continue": "等待继续下一轮",
    }

    # pending_action 用于解释“机器人在等用户做什么”，而不是替代 status。
    PENDING_ACTION_LABELS = {
        "approve_then_continue": "等待你在飞书里同意/拒绝授权",
        "continue_session": "等待你回复继续、前台继续或停止",
    }

    # 允许从飞书切换的授权模式集合；值必须能被 Claude CLI 的 --permission-mode 接受。
    ALLOWED_PERMISSION_MODES = {"acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"}

    def __init__(self, config: BotConfig) -> None:
        """Build the bot runtime, API client, and worker queue."""

        self.config = self._normalize_output_paths(config)
        # BotState 仍负责 JSON 兼容读写，v2 运行期只能通过 SessionManager 触达它。
        self.state_store = BotState(Path(self.config.state_path))
        self.session_manager = SessionManager(self.state_store, self.config.default_cwd)
        # 兼容旧大类里的会话管理调用点，但这里的 self.state 已经不是 BotState 裸对象。
        self.state = self.session_manager
        self.log_path = Path(self.config.log_path)
        self.client = (
            lark.Client.builder()
            .app_id(config.app_id)
            .app_secret(config.app_secret)
            .log_level(lark.LogLevel.INFO)
            .build()
        )
        self.active_chats: set[str] = set()
        # active_lock 只保护“同一个 chat 同时只能启动一个后台/前台启动动作”的进程内约束。
        self.active_lock = threading.Lock()
        # 飞书 SDK 客户端不是为多线程并发发送设计的；发送锁避免回复交错和 SDK 内部状态竞争。
        self.send_lock = threading.Lock()
        self.gateway = FeishuGateway(
            self.client,
            self.send_lock,
            FeishuGatewayConfig(
                app_id=self.config.app_id,
                app_secret=self.config.app_secret,
                reply_max_chars=self.config.reply_max_chars,
                retry_count=FEISHU_SEND_RETRY_COUNT,
                retry_delay_seconds=FEISHU_SEND_RETRY_DELAY_SECONDS,
            ),
            self.log,
        )
        self.command_router = CommandRouter()
        # jobs 保存后台 Claude Popen；停止命令和进程回收会通过它定位子进程。
        self.jobs: dict[str, subprocess.Popen[str]] = {}
        self.jobs_lock = threading.Lock()
        # 飞书移动端偶发会在极短时间内重复投递同一条控制命令；这里做进程内去重，
        # 避免截图/快捷键这类副作用操作被连续执行两遍。
        self.recent_control_commands: dict[tuple[str, str], float] = {}
        # 普通文本最终会直接注入 Claude 前台窗口；按内容短时去重可防止飞书重试把同一指令重复执行。
        self.recent_plain_text_commands: dict[tuple[str, str], float] = {}
        # 飞书长连接重连后偶尔会把同一条 message_id 再投一次；这里按消息 ID 再兜一层，
        # 避免“用户只点了一次按钮”却重复执行截图、前台继续之类有副作用的动作。
        # 持久化到磁盘，避免重启后重复处理旧消息。
        self._message_ids_path = Path(config.state_path).parent / "feishu-claude-bot-message-ids.json"
        self.recent_message_ids: dict[str, float] = self._load_message_ids()
        # 健康检查时间戳文件，自动重启机制通过它判断机器人是否真正在线。
        self._health_path = Path(config.state_path).parent / "feishu-claude-bot-health.json"
        # 进程内记录已启动过的前台观察器 chat_id:pid 组合，避免 PowerShell 查询失败时重复拉起 watcher。
        self._foreground_watch_spawned: set[str] = set()
        self.foreground_adapter = ForegroundAdapter(
            self.session_manager,
            self._process_exists,
            self._find_all_claude_terminal_hwnds,
            self._find_existing_foreground_launcher_pid,
            self._ensure_foreground_binding,
            self._find_realtime_claude_screenshot_target,
            self._send_command_to_foreground_window,
            self._send_hotkey_to_foreground_window,
            self.log,
        )
        self._cleanup_stale_runtime_state()
        # outputs 目录里的 launcher / screenshot 都是一次性中间产物；
        # 启动时顺手回收过期残留，避免长期运行后把 outputs 堆成排障垃圾桶。
        self._cleanup_temp_outputs()

    def _load_message_ids(self) -> dict[str, float]:
        """Load persisted message IDs from disk to survive restarts."""
        try:
            if self._message_ids_path.exists():
                data = json.loads(self._message_ids_path.read_text(encoding="utf-8"))
                cutoff = time.time() - 600  # 10 minutes
                return {mid: ts for mid, ts in data.items() if ts >= cutoff}
        except Exception:
            pass
        return {}

    def _save_message_ids(self) -> None:
        """Persist message IDs to disk for restart survival."""
        try:
            cutoff = time.time() - 600
            filtered = {mid: ts for mid, ts in self.recent_message_ids.items() if ts >= cutoff}
            self._message_ids_path.write_text(json.dumps(filtered), encoding="utf-8")
        except Exception:
            pass

    def _update_health_timestamp(self) -> None:
        """Write current timestamp to health file for watchdog detection."""
        try:
            self._health_path.write_text(json.dumps({
                "timestamp": time.time(),
                "pid": os.getpid(),
            }), encoding="utf-8")
        except Exception:
            pass

    def _start_health_reporter(self) -> None:
        """Start a background thread that periodically updates the health timestamp."""
        def _reporter():
            while True:
                self._update_health_timestamp()
                time.sleep(30)
        thread = threading.Thread(target=_reporter, daemon=True)
        thread.start()

    @staticmethod
    def _normalize_output_paths(config: BotConfig) -> BotConfig:
        """Canonicalize configured paths into the layered feishu-claude-v2 output tree."""

        state_path = Path(config.state_path).resolve()
        output_root = FeishuClaudeBot._resolve_output_root_from_state(state_path)
        # 无论用户之前把文件平铺在 outputs 根目录还是已经部分分层，这里都统一收敛到固定结构。
        config.state_path = str(output_root / "state" / "feishu-claude-bot-state.json")
        config.log_path = str(output_root / "logs" / "feishu-claude-bot.log")
        config.approvals_path = str(output_root / "state" / "feishu-claude-bot-approvals.json")
        config.permission_hook_log_path = str(output_root / "logs" / "feishu-claude-permission-hook.log")
        config.turn_hook_log_path = str(output_root / "logs" / "feishu-claude-turn-hook.log")
        config.autostart_log_path = str(output_root / "logs" / "feishu-claude-autostart.log")
        return config

    @staticmethod
    def _resolve_output_root_from_state(state_path: Path) -> Path:
        """Infer the feishu-claude-v2 output root from one configured state path."""

        if state_path.parent.name == "state" and state_path.parent.parent.name in {"feishu-claude-v2", "feishu-claude"}:
            return state_path.parent.parent
        if state_path.parent.name in {"feishu-claude-v2", "feishu-claude"}:
            return state_path.parent
        # 默认收敛到独立输出树。
        return state_path.parent / "feishu-claude-v2"

    def _get_output_root(self) -> Path:
        """Return the structured output root that owns state, logs, and temp artifacts."""

        return self._resolve_output_root_from_state(Path(self.config.state_path).resolve())

    def _get_temp_dir(self, category: str) -> Path:
        """Return one categorized temporary directory under outputs/feishu-claude-v2/temp."""

        temp_dir = self._get_output_root() / "temp" / category
        temp_dir.mkdir(parents=True, exist_ok=True)
        return temp_dir

    def _get_log_dir(self, category: str) -> Path:
        """Return one categorized diagnostic log directory under outputs/feishu-claude-v2/logs."""

        log_dir = self._get_output_root() / "logs" / category
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    def log(self, message: str) -> None:
        """Append diagnostics locally so channel failures can be traced later."""

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_log_if_needed(self.log_path)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}\n")

    def _rotate_log_if_needed(self, log_path: Path) -> None:
        """Rotate one growing log file before append so long-running bots do not keep a single giant log."""

        try:
            if not log_path.exists() or log_path.stat().st_size < LOG_ROTATE_MAX_BYTES:
                return
            # 只保留少量最近轮转副本，既方便排障回看，又避免 outputs 目录长期无上限膨胀。
            oldest_backup = log_path.with_name(f"{log_path.name}.{LOG_ROTATE_BACKUP_COUNT}")
            if oldest_backup.exists():
                oldest_backup.unlink(missing_ok=True)
            for index in range(LOG_ROTATE_BACKUP_COUNT - 1, 0, -1):
                source = log_path.with_name(f"{log_path.name}.{index}")
                target = log_path.with_name(f"{log_path.name}.{index + 1}")
                if source.exists():
                    source.replace(target)
            log_path.replace(log_path.with_name(f"{log_path.name}.1"))
        except OSError:
            # 日志轮转失败时不抛异常，避免“只是写日志”反过来中断飞书机器人主流程。
            return

    def send_text(self, chat_id: str, text: str) -> None:
        """Send a plain-text Feishu message back to the source chat."""

        # v2 将飞书发送细节收口到 FeishuGateway；保留本方法作为旧调用点的兼容壳。
        self.gateway.send_text(chat_id, text)

    def send_image(self, chat_id: str, image_path: Path) -> None:
        """Upload a local image and send it to the source Feishu chat."""

        # v2 将图片上传和发送重试收口到 FeishuGateway，避免业务流程直接依赖飞书 SDK 细节。
        self.gateway.send_image(chat_id, image_path)

    def _cleanup_path(self, path: Path, purpose: str) -> None:
        """Delete one temporary file or directory and log the failure instead of breaking the main flow."""

        try:
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            # 清理失败不应反过来打断主链路，否则“截图已发出”后还会因为删文件失败报错。
            self.log(f"cleanup {purpose} failed path={path} error={exc}")

    def _cleanup_temp_outputs(self) -> None:
        """Prune stale launcher and screenshot artifacts left by prior sessions or crashes."""

        cutoff_ts = time.time() - TEMP_OUTPUT_RETENTION_SECONDS
        outputs_root = self._get_output_root()
        temp_dirs = [
            outputs_root / "temp" / "launchers",
            outputs_root / "temp" / "screenshots",
        ]
        for temp_dir in temp_dirs:
            if not temp_dir.exists():
                continue
            for item in temp_dir.iterdir():
                try:
                    if item.is_file() and item.stat().st_mtime <= cutoff_ts:
                        # 只回收超时临时文件，避免误删当前仍在运行的前台 launcher。
                        item.unlink(missing_ok=True)
                except OSError as exc:
                    self.log(f"cleanup stale temp output failed path={item} error={exc}")

    def _format_duration(self, started_at: Any, finished_at: Any | None = None) -> str:
        """Format elapsed seconds into a concise Chinese duration string."""

        if not started_at:
            return "-"
        end_ts = float(finished_at) if finished_at else time.time()
        elapsed_seconds = max(0, int(end_ts - float(started_at)))
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

    def _format_status_label(self, status: Any) -> str:
        """Translate internal status codes into operator-friendly Chinese labels."""

        return self.STATUS_LABELS.get(str(status), str(status) or "-")

    def _format_pending_action_label(self, action: Any) -> str:
        """Translate pending follow-up actions into readable Chinese hints."""

        if not action:
            return ""
        return self.PENDING_ACTION_LABELS.get(str(action), str(action))

    def _get_display_status_label(self, chat_state: dict[str, Any]) -> str:
        """Render a human-facing status label without leaking internal session mechanics."""

        status = str(chat_state.get("status", "idle"))
        pending_action = str(chat_state.get("pending_action") or "")
        managed_session = bool(chat_state.get("managed_session"))
        active_pid = chat_state.get("active_pid")
        foreground_pid = chat_state.get("foreground_pid") or active_pid
        if status == "foreground_opened" and foreground_pid and self._process_exists(foreground_pid):
            # 前台窗口还活着时，用户可能已经在电脑里手动继续了任务；这里避免继续显示成
            # “等待接管”，否则手机端会误以为当前会话已经空闲。
            return "前台会话在线，可能正在人工操作"
        if status == "foreground_busy" and foreground_pid and self._process_exists(foreground_pid):
            # 飞书把命令送进前台窗口后拿不到标准输出流，状态上直接标记为执行中，
            # 方便用户在手机里区分“窗口已开”和“窗口里已有任务在跑”。
            return "前台执行中"
        if status == "done" and managed_session and pending_action == "continue_session":
            # 任务本轮已经完成，但会话仍托管可继续；对用户要突出“已完成”，而不是内部锁状态。
            return "本轮已完成，等待继续下一轮"
        if status == "failed" and managed_session and pending_action == "continue_session":
            # 失败后仍保留会话，方便用户继续修复；展示时区分“失败结果”和“还能继续”。
            return "本轮失败，等待继续处理"
        return self._format_status_label(status)

    def _build_sectioned_message(self, title: str, details: list[str], next_steps: list[str] | None = None) -> str:
        """Build a consistent Feishu text layout that stays readable on mobile."""

        lines = [title]
        lines.extend(line for line in details if line)
        if next_steps:
            # 统一把可执行动作放在末尾，方便用户直接在飞书里照着回复。
            lines.append("")
            lines.append("可直接回复：")
            lines.extend(next_steps)
        return "\n".join(lines)

    @staticmethod
    def _is_placeholder_status_summary(text: str) -> bool:
        """Tell whether one stored result is only a control/status placeholder, not a real task summary."""

        normalized = (text or "").strip()
        return normalized in {
            "前台会话窗口已打开，等待本机接管。",
            "前台会话窗口已关闭。",
            "已重新识别到当前会话对应的 Claude 前台窗口。",
        } or normalized.startswith("已将命令发送到前台会话窗口并开始执行：")

    @staticmethod
    def _is_fallback_completion_summary(text: str) -> bool:
        """Tell whether one stored summary is only the bot's fallback completion note, not Claude's own result."""

        normalized = (text or "").strip()
        return normalized.startswith("前台本轮已返回输入态，但当前会话没有收到 Claude Stop hook 摘要")

    def _capture_window_screenshot(self, pid: int) -> Path:
        """Capture a visible Windows window for a managed foreground Claude process.

        Uses PrintWindow with PW_RENDERFULLCONTENT so the capture works even when
        the screen is locked or the monitor is off.  Falls back to CopyFromScreen
        (screen DC BitBlt) if PrintWindow fails.
        """

        # Ensure we get real physical pixel coordinates on high-DPI displays.
        # SetProcessDpiAwareness can only be called once per process; ignore if already set.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (OSError, WindowsError):
            pass

        screenshot_dir = self._get_temp_dir("screenshots")
        screenshot_path = screenshot_dir / f"claude-window-{pid}-{int(time.time())}.bmp"

        hwnd = self._find_claude_terminal_hwnd(pid)
        if not hwnd:
            raise RuntimeError("没有找到可截图的 Claude Code 终端窗口，已取消截图，避免误发其他窗口。")

        user32 = ctypes.windll.user32
        # PrintWindow 对最小化窗口无法正确渲染，先恢复再截图。
        # SW_RESTORE(9) 确保窗口完全恢复。
        user32.ShowWindow(hwnd, 9)
        time.sleep(0.5)

        rect = ctypes.wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise RuntimeError("读取窗口位置失败")
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            raise RuntimeError("窗口尺寸无效，可能已最小化")

        img = self._print_window_to_image(hwnd, width, height)
        if img is None:
            # PrintWindow 失败时重试一次（窗口可能还在渲染中）。
            time.sleep(0.5)
            img = self._print_window_to_image(hwnd, width, height)
        if img is None:
            raise RuntimeError("窗口截图失败：PrintWindow 未成功")

        img.save(str(screenshot_path), "BMP")
        return screenshot_path

    # -- low-level screenshot helpers ----------------------------------------------------------

    @staticmethod
    def _build_process_tree() -> tuple[dict[int, int], dict[int, list[int]]]:
        """Snapshot the process table. Returns (parent_of, children_of)."""
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

    @staticmethod
    def _find_child_pids(parent_pid: int) -> list[int]:
        """Return all descendant PIDs of parent_pid (breadth-first)."""
        _, children_of = FeishuClaudeBot._build_process_tree()
        result: list[int] = []
        queue = [parent_pid]
        while queue:
            cur = queue.pop(0)
            for child in children_of.get(cur, []):
                result.append(child)
                queue.append(child)
        return result

    @staticmethod
    def _find_ancestor_pids(pid: int, max_depth: int = 5) -> list[int]:
        """Walk up the process tree from pid, returning ancestor PIDs."""
        parent_of, _ = FeishuClaudeBot._build_process_tree()
        ancestors: list[int] = []
        cur = pid
        for _ in range(max_depth):
            ppid = parent_of.get(cur)
            if not ppid or ppid == 0:
                break
            ancestors.append(ppid)
            cur = ppid
        return ancestors

    @staticmethod
    def _find_claude_terminal_hwnd(pid: int) -> int:
        """Find the Claude terminal window handle by PID or title matching."""

        user32 = ctypes.windll.user32

        def _find_visible_window_by_pid(target_pid: int) -> int:
            """Return HWND of a visible, non-zero-size window owned by target_pid."""
            result_hwnd = [0]

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
            def enum_by_pid(hwnd_cb, _lparam):
                proc_id = ctypes.wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd_cb, ctypes.byref(proc_id))
                if proc_id.value == target_pid and user32.IsWindowVisible(hwnd_cb):
                    r = ctypes.wintypes.RECT()
                    if user32.GetWindowRect(hwnd_cb, ctypes.byref(r)):
                        w = r.right - r.left
                        h = r.bottom - r.top
                        if w > 0 and h > 0:
                            result_hwnd[0] = hwnd_cb
                            return False
                return True

            user32.EnumWindows(enum_by_pid, 0)
            return result_hwnd[0]

        # 1. Try the launcher PID directly.
        if pid > 0:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                found = _find_visible_window_by_pid(pid)
                if found:
                    return found

            # 1b. Walk the process tree (children + ancestors).
            # When pwsh runs inside Windows Terminal, the visible window is
            # owned by WindowsTerminal.exe which is the PARENT of the launcher.
            parent_of, children_of = FeishuClaudeBot._build_process_tree()
            queue = [pid]
            while queue:
                cur = queue.pop(0)
                for child in children_of.get(cur, []):
                    found = _find_visible_window_by_pid(child)
                    if found:
                        return found
                    queue.append(child)
            cur = pid
            for _ in range(5):
                ppid = parent_of.get(cur)
                if not ppid or ppid == 0:
                    break
                found = _find_visible_window_by_pid(ppid)
                if found:
                    return found
                cur = ppid

        # 2. Search by window title.
        for title_part in ("Claude Code Assistant Front Session", "Claude Code"):
            found = _find_visible_window_by_title(title_part, require_terminal_process=True)
            if found:
                return found

        # 3. Last resort: any visible window with "Claude Code" in the title.
        return _find_visible_window_by_title("Claude Code", require_terminal_process=False) or 0

    @staticmethod
    def _find_all_claude_terminal_hwnds(pid: int) -> list[int]:
        """Return all visible Claude terminal window handles system-wide."""

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnds: list[int] = []
        seen: set[int] = set()
        parent_of, children_of = FeishuClaudeBot._build_process_tree()

        process_names: dict[int, str] = {}
        try:
            metadata_result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process | Select-Object ProcessId,Name | ConvertTo-Json -Compress",
                ],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            if metadata_result.returncode == 0 and (metadata_result.stdout or "").strip():
                rows = json.loads(metadata_result.stdout)
                if isinstance(rows, dict):
                    rows = [rows]
                for row in rows:
                    process_names[int(row.get("ProcessId") or 0)] = str(row.get("Name") or "").lower()
        except Exception:
            # 进程快照只用于实时兜底；失败时继续使用 PID/标题路径，避免截图命令整体失败。
            process_names = {}

        def _descendants(root_pid: int) -> set[int]:
            """Return descendant process ids from the live process tree."""

            result: set[int] = set()
            queue = list(children_of.get(root_pid, []))
            while queue:
                current = queue.pop(0)
                if current in result:
                    continue
                result.add(current)
                queue.extend(children_of.get(current, []))
            return result

        target_related: set[int] = set()
        if pid > 0:
            # state 里常保存 shell PID；Windows Terminal 的真实窗口通常在父进程上。
            target_related.add(pid)
            target_related.update(_descendants(pid))
            current_pid = pid
            for _ in range(8):
                parent_pid = parent_of.get(current_pid)
                if not parent_pid or parent_pid == 0:
                    break
                target_related.add(parent_pid)
                current_pid = parent_pid

        def _is_terminal_or_claude_window(hwnd_cb: int) -> bool:
            """Only include windows owned by known terminal processes or with 'Claude Code' in the title."""
            title = ""
            # Check window title first (cheaper than process lookup).
            length = user32.GetWindowTextLengthW(hwnd_cb)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd_cb, buf, length + 1)
                title = buf.value.lower()
                if "claude code" in title:
                    return True
            # Check whether the owning process is a known terminal.
            proc_id = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd_cb, ctypes.byref(proc_id))
            owner_pid = int(proc_id.value)
            owner_name = process_names.get(owner_pid, "").rsplit(".", 1)[0]
            owner_descendants = _descendants(owner_pid)
            has_claude_child = any(process_names.get(child_pid, "") == "claude.exe" for child_pid in owner_descendants)
            related_to_target = bool(
                target_related and (owner_pid in target_related or owner_descendants.intersection(target_related))
            )
            if owner_name in _TERMINAL_PROCESS_NAMES and (has_claude_child or related_to_target):
                return True
            # PowerShell 进程快照偶发不完整（超时、权限等）；fallback 到 API 直查可执行文件名。
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            proc_handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, proc_id.value)
            if not proc_handle:
                return False
            try:
                exe_buf = ctypes.create_unicode_buffer(512)
                size = ctypes.wintypes.DWORD(512)
                if not kernel32.QueryFullProcessImageNameW(proc_handle, 0, exe_buf, ctypes.byref(size)):
                    return False
                exe_name = os.path.basename(exe_buf.value).rsplit(".", 1)[0].lower()
                if exe_name in _TERMINAL_PROCESS_NAMES and related_to_target:
                    # 进程快照缺失时（process_names 为空），related_to_target 仍基于内核进程树；
                    # 只要窗口属于 target_related 里的终端进程就放行，避免因 PowerShell 延迟漏掉真实窗口。
                    return True
                return exe_name in _TERMINAL_PROCESS_NAMES and (has_claude_child or related_to_target or "claude" in title)
            finally:
                kernel32.CloseHandle(proc_handle)

        def _is_valid(hwnd_cb: int, *, allow_minimized: bool = False) -> bool:
            r = ctypes.wintypes.RECT()
            if not user32.GetWindowRect(hwnd_cb, ctypes.byref(r)):
                return False
            w = r.right - r.left
            h = r.bottom - r.top
            if allow_minimized:
                # Windows Terminal 会暴露 160x28 的标签页 HWND；这些不是可截图主窗口，不能误收集。
                # 锁屏时主窗口可能不再被 IsWindowVisible 视为可见；可信进程树内只按尺寸过滤，让 PrintWindow 有机会离屏渲染。
                return w > 100 and h > 100
            if not user32.IsWindowVisible(hwnd_cb):
                return False
            return w > 100 and h > 100

        def _is_terminal_window(hwnd_cb: int) -> bool:
            """Return whether one visible HWND is owned by a known terminal host."""

            proc_id = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd_cb, ctypes.byref(proc_id))
            owner_name = process_names.get(int(proc_id.value), "").rsplit(".", 1)[0]
            if owner_name in _TERMINAL_PROCESS_NAMES:
                return True
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            proc_handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, proc_id.value)
            if not proc_handle:
                return False
            try:
                exe_buf = ctypes.create_unicode_buffer(512)
                size = ctypes.wintypes.DWORD(512)
                if not kernel32.QueryFullProcessImageNameW(proc_handle, 0, exe_buf, ctypes.byref(size)):
                    return False
                exe_name = os.path.basename(exe_buf.value).rsplit(".", 1)[0].lower()
                return exe_name in _TERMINAL_PROCESS_NAMES
            finally:
                kernel32.CloseHandle(proc_handle)

        def _add(hwnd_cb: int, *, trusted_process_tree: bool = False) -> None:
            if hwnd_cb in seen or not _is_valid(hwnd_cb, allow_minimized=trusted_process_tree):
                return
            if not trusted_process_tree and not _is_terminal_or_claude_window(hwnd_cb):
                return
            if hwnd_cb not in seen:
                seen.add(hwnd_cb)
                hwnds.append(hwnd_cb)

        def _collect_by_pid(target_pid: int, *, trusted_process_tree: bool = False) -> None:
            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
            def _enum(hwnd_cb, _lparam):
                proc_id = ctypes.wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd_cb, ctypes.byref(proc_id))
                if proc_id.value == target_pid:
                    # 这里的 PID 来自已确认的 Claude 前台进程树；实时截图应信任进程树关系，
                    # 不再强制要求窗口标题包含 Claude，否则 Windows Terminal 标题为项目名时会漏掉。
                    _add(hwnd_cb, trusted_process_tree=trusted_process_tree)
                return True
            user32.EnumWindows(_enum, 0)

        # 1. All windows owned by launcher PID, its children, AND ancestors.
        if pid > 0:
            _collect_by_pid(pid, trusted_process_tree=True)
            queue = [pid]
            while queue:
                cur = queue.pop(0)
                for child in children_of.get(cur, []):
                    _collect_by_pid(child, trusted_process_tree=True)
                    queue.append(child)
            cur = pid
            for _ in range(5):
                ppid = parent_of.get(cur)
                if not ppid or ppid == 0:
                    break
                _collect_by_pid(ppid, trusted_process_tree=True)
                cur = ppid

        # 2. Any remaining window with "Claude Code" in the title.
        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def _enum_by_title(hwnd_cb, _lparam):
            if hwnd_cb in seen:
                return True
            if not user32.IsWindowVisible(hwnd_cb):
                return True
            length = user32.GetWindowTextLengthW(hwnd_cb)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd_cb, buf, length + 1)
            if "claude code" in buf.value.lower():
                _add(hwnd_cb)
            return True
        user32.EnumWindows(_enum_by_title, 0)

        # 3. If state PID is missing or points at a shell without HWND, scan live terminal
        # windows and only accept ones whose process tree currently contains claude.exe.
        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def _enum_live_claude_terminals(hwnd_cb, _lparam):
            if hwnd_cb not in seen:
                _add(hwnd_cb)
            return True
        user32.EnumWindows(_enum_live_claude_terminals, 0)

        if not hwnds:
            loose_terminal_hwnds: list[int] = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
            def _enum_single_visible_terminal(hwnd_cb, _lparam):
                if _is_valid(hwnd_cb) and _is_terminal_window(hwnd_cb):
                    loose_terminal_hwnds.append(hwnd_cb)
                return True

            user32.EnumWindows(_enum_single_visible_terminal, 0)
            if len(loose_terminal_hwnds) == 1:
                # Windows Terminal 的可见窗口有时不在 pwsh/claude 进程树上，且标题会变成任务标题；
                # 仅在全局只有一个可见终端时兜底收录，避免多终端场景误截其他窗口。
                hwnds.append(loose_terminal_hwnds[0])

        return hwnds

    @staticmethod
    def _print_window_to_image(hwnd: int, width: int, height: int):
        """Capture a window via PrintWindow (works when screen is locked).

        Returns a PIL Image on success, or None if PrintWindow failed.
        """

        try:
            from PIL import Image
        except ImportError:
            return None

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        PW_RENDERFULLCONTENT = 0x00000002

        hdc_window = user32.GetWindowDC(hwnd)
        if not hdc_window:
            return None
        hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
        hbitmap = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
        old_bmp = gdi32.SelectObject(hdc_mem, hbitmap)

        # Fill with white background first (in case the window has transparent regions).
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        fill_rect = RECT(0, 0, width, height)
        brush = gdi32.CreateSolidBrush(0x00FFFFFF)
        user32.FillRect(hdc_mem, ctypes.byref(fill_rect), brush)
        gdi32.DeleteObject(brush)

        result = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
        if not result:
            # PrintWindow returned 0 (failed).
            gdi32.SelectObject(hdc_mem, old_bmp)
            gdi32.DeleteObject(hbitmap)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(hwnd, hdc_window)
            return None

        # Extract bitmap bits.
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
        bmi.biWidth = width
        bmi.biHeight = -height  # top-down
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0  # BI_RGB

        buf = ctypes.create_string_buffer(width * height * 4)
        gdi32.GetDIBits(hdc_mem, hbitmap, 0, height, buf, ctypes.byref(bmi), 0)

        img = Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1)
        img = img.convert("RGB")

        # Cleanup GDI resources.
        gdi32.SelectObject(hdc_mem, old_bmp)
        gdi32.DeleteObject(hbitmap)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_window)
        return img

    @staticmethod
    def _copy_screen_region_to_image(left: int, top: int, width: int, height: int):
        """Fallback: capture a screen region via CopyFromScreen (BitBlt on screen DC).

        Returns a PIL Image on success, or None if Pillow is unavailable.
        This method does NOT work when the screen is locked.
        """

        try:
            from PIL import ImageGrab
        except ImportError:
            return None
        return ImageGrab.grab(bbox=(left, top, left + width, top + height))

    def _capture_desktop_screenshot(self) -> Path:
        """Capture the current primary desktop screen for remote troubleshooting."""

        screenshot_dir = self._get_temp_dir("screenshots")
        timestamp_ns = time.time_ns()
        screenshot_path = screenshot_dir / f"desktop-{timestamp_ns}.png"
        script_path = screenshot_dir / f"capture-desktop-{timestamp_ns}.ps1"
        # 截图文件名必须每次唯一；同秒重复截图时绝不能复用旧文件，否则飞书可能上传上一张历史图片。
        screenshot_path.unlink(missing_ok=True)
        script = r"""
param(
  [string]$OutputPath
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
if ($bounds.Width -le 0 -or $bounds.Height -le 0) { throw "桌面尺寸无效，无法截图" }
$bitmap = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
try {
  # 桌面截图用于远程排障，会包含当前主屏幕可见内容；发送前请注意隐私信息。
  $graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bitmap.Size)
  $stream = [System.IO.File]::Open($OutputPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
  try {
    $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
  } finally {
    $stream.Dispose()
  }
} finally {
  $graphics.Dispose()
  $bitmap.Dispose()
}
"""
        # 与窗口截图保持同样的 -File 传参方式，避免 -Command 参数绑定偶发失效。
        script_path.write_text(script, encoding="utf-8-sig")
        result = subprocess.run(
            [
                self.config.pwsh_path,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-OutputPath",
                str(screenshot_path),
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        try:
            script_path.unlink(missing_ok=True)
        except OSError as exc:
            self.log(f"cleanup desktop screenshot script failed path={script_path} error={exc}")
        if result.returncode != 0 or not screenshot_path.exists():
            raise RuntimeError((result.stderr or result.stdout or "桌面截图失败").strip())
        return screenshot_path

    def _capture_hwnd_screenshot(self, hwnd: int, tag: str = "") -> Path:
        """Capture a specific window handle. Returns the path to the BMP file."""

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (OSError, WindowsError):
            pass

        screenshot_dir = self._get_temp_dir("screenshots")
        suffix = f"-{tag}" if tag else ""
        screenshot_path = screenshot_dir / f"claude-window{suffix}-{time.time_ns()}.bmp"
        # 窗口截图可能被用户连续触发；写入前删除同名残留可避免截图失败后误发旧文件。
        screenshot_path.unlink(missing_ok=True)

        user32 = ctypes.windll.user32
        # 截图必须基于实时窗口状态；Windows Terminal 最小化时 PrintWindow 无法正确渲染
        # DirectX 内容，需要先恢复窗口再截图。SW_RESTORE(9) 确保窗口完全恢复。
        user32.ShowWindow(hwnd, 9)
        time.sleep(0.5)

        rect = ctypes.wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise RuntimeError("读取窗口位置失败")
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            raise RuntimeError("窗口尺寸无效，可能已最小化")

        img = self._print_window_to_image(hwnd, width, height)
        if img is None:
            # PrintWindow 失败时重试一次（窗口可能还在渲染中）。
            time.sleep(0.5)
            img = self._print_window_to_image(hwnd, width, height)
        if img is None:
            raise RuntimeError("窗口截图失败：PrintWindow 未成功")

        img.save(str(screenshot_path), "BMP")
        return screenshot_path

    def _get_window_title(self, hwnd: int) -> str:
        """Read a Windows window title for Feishu display and window selection.

        Args:
            hwnd: Native Windows HWND.

        Returns:
            Window title, or a fallback containing the HWND when no title is available.
        """

        title_len = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(title_len + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, title_buf, title_len + 1)
        title = title_buf.value.strip()
        if len(title) > 1 and ord(title[0]) > 0x2000:
            title = title[1:].lstrip()
        return title or f"HWND {hwnd}"

    def _resolve_realtime_window_targets(self) -> list[dict[str, Any]]:
        """Return realtime Claude terminal windows ordered for Feishu selection.

        Returns:
            A list of dictionaries containing 1-based index, HWND, owner PID, and title.
        """

        targets: list[dict[str, Any]] = []
        candidate_hwnds: list[int] = []
        for hwnd in self._find_all_claude_terminal_hwnds(0):
            if hwnd not in candidate_hwnds:
                candidate_hwnds.append(hwnd)
        for hwnd in self._find_windows_terminal_tab_hwnds():
            if hwnd not in candidate_hwnds:
                # Windows Terminal 多标签页只有 160x28 左右的 tab HWND；单独纳入飞书路由，不影响截图主窗口过滤。
                candidate_hwnds.append(hwnd)
        def _window_sort_key(hwnd: int) -> tuple[int, int]:
            """Return screen position for stable left-to-right Feishu window numbering.

            Args:
                hwnd: Native Windows HWND.

            Returns:
                A tuple of left and top coordinates; failed reads sort to the end.
            """

            rect = ctypes.wintypes.RECT()
            if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return (999999, 999999)
            return (rect.left, rect.top)

        # Windows Terminal 标签页的枚举顺序不稳定；按屏幕位置排序更接近用户在任务栏/标签栏看到的顺序。
        candidate_hwnds.sort(key=_window_sort_key)
        for hwnd in candidate_hwnds:
            proc_id = ctypes.wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
            title = self._get_window_title(hwnd)
            targets.append(
                {
                    # 窗口编号只代表当前实时枚举顺序；后续输入保存 HWND，避免标题变化导致误发。
                    "index": len(targets) + 1,
                    "hwnd": int(hwnd),
                    "pid": int(proc_id.value),
                    "title": title,
                }
            )
        return targets

    def _find_windows_terminal_tab_hwnds(self) -> list[int]:
        """Return visible Windows Terminal tab HWNDs for Feishu window selection.

        Args:
            None.

        Returns:
            Visible tab handles owned by WindowsTerminal.exe. These small HWNDs are
            useful for selecting a tab before paste, but should not be used as screenshot targets.
        """

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnds: list[int] = []
        seen: set[int] = set()
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def _enum(hwnd_cb, _lparam):
            if hwnd_cb in seen or not user32.IsWindowVisible(hwnd_cb):
                return True
            length = user32.GetWindowTextLengthW(hwnd_cb)
            if length <= 0:
                return True
            rect = ctypes.wintypes.RECT()
            if not user32.GetWindowRect(hwnd_cb, ctypes.byref(rect)):
                return True
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width <= 80 or not (20 <= height <= 80):
                return True
            proc_id = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd_cb, ctypes.byref(proc_id))
            proc_handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, proc_id.value)
            if not proc_handle:
                return True
            try:
                exe_buf = ctypes.create_unicode_buffer(512)
                size = ctypes.wintypes.DWORD(512)
                if not kernel32.QueryFullProcessImageNameW(proc_handle, 0, exe_buf, ctypes.byref(size)):
                    return True
                exe_name = os.path.basename(exe_buf.value).rsplit(".", 1)[0].lower()
                if exe_name != "windowsterminal":
                    return True
            finally:
                kernel32.CloseHandle(proc_handle)
            seen.add(hwnd_cb)
            hwnds.append(hwnd_cb)
            return True

        user32.EnumWindows(_enum, 0)
        return hwnds

    def _send_window_list(self, chat_id: str) -> None:
        """Send realtime selectable Claude windows to Feishu."""

        targets = self._resolve_realtime_window_targets()
        if not targets:
            self.send_text(chat_id, "当前没有可接管的 Claude 前台窗口。可发送：新窗口继续，或：新窗口运行 <任务>。")
            return
        chat_state = self.state.get_chat(chat_id, self.config.default_cwd)
        previous_snapshot = chat_state.get("last_window_targets") if isinstance(chat_state.get("last_window_targets"), list) else []
        previous_order = {
            int(item.get("hwnd")): int(item.get("index"))
            for item in previous_snapshot
            if item.get("hwnd") and item.get("index")
        }
        if previous_order:
            # 实时枚举顺序会漂移；展示时优先沿用上一次发给飞书的 HWND 顺序，只把新窗口追加到末尾。
            targets.sort(key=lambda item: (previous_order.get(int(item["hwnd"]), 999999), int(item["index"])))
            for display_index, target in enumerate(targets, start=1):
                # 展示序号必须和即将保存的快照一致，后续“切换到窗口N”才引用同一套映射。
                target["index"] = display_index
        active_hwnd = chat_state.get("active_window_hwnd")
        lines = []
        for target in targets:
            marker = " * " if active_hwnd and int(active_hwnd) == int(target["hwnd"]) else "   "
            lines.append(f"{marker}窗口{target['index']}：{target['title']}（HWND {target['hwnd']}）")
        window_snapshot = [
            {
                # 快照里的 index 对应飞书消息中用户看到的行号；后续切换必须按这个映射回 HWND。
                "index": int(target["index"]),
                "hwnd": int(target["hwnd"]),
                "pid": int(target.get("pid") or 0),
                "title": str(target.get("title") or ""),
            }
            for target in targets
        ]
        self.state.update_chat(
            chat_id,
            {
                # Windows Terminal 枚举顺序会随激活状态变化；保存快照能让“切换到窗口N”引用刚发出的列表。
                "last_window_targets": window_snapshot,
                "last_window_targets_at": time.time(),
            },
            self.config.default_cwd,
        )
        self.send_text(
            chat_id,
            self._build_sectioned_message(
                "当前可接管窗口",
                lines,
                # 窗口数量来自实时枚举；快捷项按实际数量生成，避免第三个窗口只能手输命令。
                [f"切换到窗口{target['index']}" for target in targets[:5]] + ["继续"],
            ),
        )

    def _select_window_by_index(self, chat_id: str, index: int) -> bool:
        """Select one realtime Claude window for subsequent Feishu text input.

        Args:
            chat_id: Feishu chat id.
            index: 1-based realtime window index.

        Returns:
            True when the window exists and was selected.
        """

        chat_state = self.state.get_chat(chat_id, self.config.default_cwd)
        snapshot_targets = chat_state.get("last_window_targets") if isinstance(chat_state.get("last_window_targets"), list) else []
        snapshot_at = float(chat_state.get("last_window_targets_at") or 0.0)
        snapshot_selected = None
        if snapshot_targets and time.time() - snapshot_at <= 300:
            # 用户通常会在看到窗口列表后立刻点快捷项；5 分钟内优先按快照序号解析，避免实时顺序变化。
            snapshot_selected = next((item for item in snapshot_targets if int(item.get("index") or 0) == index), None)
        targets = self._resolve_realtime_window_targets()
        if snapshot_selected:
            # 快照只提供稳定映射，真正发送前仍要用实时列表确认 HWND 当前可见可接管。
            selected = next((item for item in targets if int(item["hwnd"]) == int(snapshot_selected["hwnd"])), None)
        else:
            selected = next((item for item in targets if item["index"] == index), None)
        if not selected:
            if snapshot_selected:
                self.send_text(chat_id, f"窗口{index}已不在当前可接管列表中，已刷新窗口列表。")
            self._send_window_list(chat_id)
            return False
        self.state.update_chat(
            chat_id,
            {
                # active_window_hwnd 是飞书后续输入的路由目标；它独立于 foreground_pid，避免多窗口共用同一 PID 时串窗。
                "active_window_hwnd": selected["hwnd"],
                "active_window_title": selected["title"],
            },
            self.config.default_cwd,
        )
        self.send_text(chat_id, f"已切换到窗口{index}：{selected['title']}")
        return True

    def _resolve_active_window_target(self, chat_id: str) -> dict[str, Any] | None:
        """Resolve the current target window for plain Feishu text input.

        Args:
            chat_id: Feishu chat id.

        Returns:
            Selected or default realtime window target, or None when no window exists.
        """

        targets = self._resolve_realtime_window_targets()
        if not targets:
            self.send_text(chat_id, "当前没有可接管的 Claude 前台窗口。可发送：新窗口继续，或：新窗口运行 <任务>。")
            return None
        chat_state = self.state.get_chat(chat_id, self.config.default_cwd)
        active_hwnd = chat_state.get("active_window_hwnd")
        if active_hwnd:
            selected = next((item for item in targets if int(item["hwnd"]) == int(active_hwnd)), None)
            if selected:
                return selected
            # 窗口已关闭，清除失效 HWND 避免后续消息反复触发同一条错误提示。
            self.state.update_chat(
                chat_id,
                {
                    "active_window_hwnd": None,
                    "active_window_title": "",
                },
                self.config.default_cwd,
            )
            self._send_window_list(chat_id)
            return None
        selected = targets[0]
        self.state.update_chat(
            chat_id,
            {
                # 没有显式选择时，默认使用当前实时枚举的第一个窗口，通常也是最近活跃/最靠前的窗口。
                "active_window_hwnd": selected["hwnd"],
                "active_window_title": selected["title"],
            },
            self.config.default_cwd,
        )
        return selected

    def _send_command_to_foreground_hwnd(self, hwnd: int, command_text: str) -> None:
        """Activate one specific HWND and submit text into it.

        Args:
            hwnd: Native Windows HWND selected from realtime window list.
            command_text: Text to paste and submit.

        Returns:
            None. Raises RuntimeError when Windows refuses activation or paste.
        """

        launcher_dir = self._get_temp_dir("launchers")
        send_script_path = launcher_dir / f"send-window-{hwnd}-{int(time.time())}.ps1"
        send_script = "\n".join(
            [
                "param([long]$WindowHandle, [string]$CommandText)",
                "$ErrorActionPreference = 'Stop'",
                "Add-Type -AssemblyName System.Windows.Forms",
                "Add-Type -TypeDefinition @\"",
                "using System;",
                "using System.Runtime.InteropServices;",
                "public static class TargetWindow {",
                "  [DllImport(\"user32.dll\")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);",
                "  [DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr hWnd);",
                "  [DllImport(\"user32.dll\")] public static extern IntPtr GetForegroundWindow();",
                "  [DllImport(\"user32.dll\")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);",
                "  [DllImport(\"user32.dll\", EntryPoint=\"GetWindowThreadProcessId\")] public static extern uint GetWindowThreadId(IntPtr hWnd, IntPtr lpdwProcessId);",
                "  [DllImport(\"user32.dll\")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);",
                "  [DllImport(\"user32.dll\")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);",
                "  [DllImport(\"user32.dll\")] public static extern bool SetCursorPos(int x, int y);",
                "  [DllImport(\"user32.dll\")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);",
                "  [DllImport(\"user32.dll\")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);",
                "  [DllImport(\"kernel32.dll\")] public static extern uint GetCurrentThreadId();",
                "  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }",
                "  public static int GetOwnerProcessId(IntPtr hWnd) { uint processId; GetWindowThreadProcessId(hWnd, out processId); return (int)processId; }",
                "  public static bool IsSmallTab(IntPtr hWnd) { RECT rect; if (!GetWindowRect(hWnd, out rect)) { return false; } int width = rect.Right - rect.Left; int height = rect.Bottom - rect.Top; return width > 80 && height >= 20 && height <= 80; }",
                "  public static void ClickCenter(IntPtr hWnd) { RECT rect; if (!GetWindowRect(hWnd, out rect)) { return; } int x = (rect.Left + rect.Right) / 2; int y = (rect.Top + rect.Bottom) / 2; SetCursorPos(x, y); mouse_event(0x0002, 0, 0, 0, UIntPtr.Zero); mouse_event(0x0004, 0, 0, 0, UIntPtr.Zero); }",
                "  public static void PressPlainEnter() { keybd_event(0x10, 0, 0x0002, UIntPtr.Zero); keybd_event(0x11, 0, 0x0002, UIntPtr.Zero); keybd_event(0x12, 0, 0x0002, UIntPtr.Zero); keybd_event(0x0D, 0, 0, UIntPtr.Zero); keybd_event(0x0D, 0, 0x0002, UIntPtr.Zero); }",
                "  public static bool Activate(IntPtr hWnd) {",
                "    if (hWnd == IntPtr.Zero) { return false; }",
                "    ShowWindowAsync(hWnd, 9);",
                "    IntPtr fgHwnd = GetForegroundWindow();",
                "    if (fgHwnd != hWnd) {",
                "      uint fgTid = GetWindowThreadId(fgHwnd, IntPtr.Zero);",
                "      uint myTid = GetCurrentThreadId();",
                "      bool attached = false;",
                "      try {",
                "        if (fgTid != myTid) { attached = AttachThreadInput(myTid, fgTid, true); }",
                "      keybd_event(0x12, 0, 0, UIntPtr.Zero); keybd_event(0x12, 0, 0x0002, UIntPtr.Zero);  // Alt trick",
                "        SetForegroundWindow(hWnd);",
                "      } finally {",
                "        if (attached) { AttachThreadInput(myTid, fgTid, false); }",
                "      }",
                "    } else {",
                "      SetForegroundWindow(hWnd);",
                "    }",
                "    return GetForegroundWindow() == hWnd;",
                "  }",
                "}",
                "\"@",
                "$hwnd = [IntPtr]$WindowHandle",
                "if ($hwnd -eq [IntPtr]::Zero) { throw 'WINDOW_NOT_FOUND' }",
                "$wshell = New-Object -ComObject WScript.Shell",
                # HWND 路由也会遇到 Windows 前台锁；先用 AttachThreadInput 激活，失败后再按宿主 PID AppActivate。
                "$ownerPid = [TargetWindow]::GetOwnerProcessId($hwnd)",
                "$isSmallTab = [TargetWindow]::IsSmallTab($hwnd)",
                "$activated = [TargetWindow]::Activate($hwnd)",
                # ShowWindowAsync 对最小化/离屏窗口是异步恢复；这里等待窗口矩形稳定后再点击聚焦。
                "Start-Sleep -Milliseconds 300",
                "if (-not $activated) { $activated = $wshell.AppActivate($ownerPid); Start-Sleep -Milliseconds 200 }",
                "if ($isSmallTab) { [TargetWindow]::ClickCenter($hwnd); Start-Sleep -Milliseconds 200; $activated = $wshell.AppActivate($ownerPid) }",
                "if (-not $isSmallTab) {",
                # Windows Terminal 多窗口共用同一个 PID，AppActivate(pid) 可能激活到其他终端；发送前必须重新聚焦目标 HWND。
                "  [TargetWindow]::ClickCenter($hwnd)",
                "  Start-Sleep -Milliseconds 200",
                "  $activated = [TargetWindow]::GetForegroundWindow() -eq $hwnd",
                "  if (-not $activated) { $activated = [TargetWindow]::Activate($hwnd); Start-Sleep -Milliseconds 200 }",
                "  if (-not $activated) { $activated = [TargetWindow]::GetForegroundWindow() -eq $hwnd }",
                "}",
                # 非标签窗口只有当前台 HWND 确认等于目标 HWND 时才允许 SendKeys，避免误报“已发送”但文本落到别处。
                "if ((-not $isSmallTab) -and (-not $activated)) { throw 'WINDOW_ACTIVATE_FAILED' }",
                "if (-not $activated -and $isSmallTab) { throw 'WINDOW_TAB_ACTIVATE_FAILED' }",
                "Start-Sleep -Milliseconds 250",
                "$previousClipboard = $null",
                "try { $previousClipboard = Get-Clipboard -Raw -ErrorAction SilentlyContinue } catch {}",
                # Set-Clipboard -Value 可能写入尾随换行，Claude Code 会把它当成多行输入；SetText 保持精确文本。
                "[System.Windows.Forms.Clipboard]::SetText($CommandText)",
                "Start-Sleep -Milliseconds 250",
                # WScript.SendKeys 是异步投递，Claude Code 粘贴尚未完成时收到 Enter 会把它当成换行；SendWait 等待键序列处理完。
                "[System.Windows.Forms.SendKeys]::SendWait('^v')",
                "Start-Sleep -Milliseconds 700",
                # Claude Code 默认 Enter=提交、Ctrl+J=换行；提交前释放修饰键，避免被解释成 Shift/Ctrl+Enter 类换行。
                "[TargetWindow]::PressPlainEnter()",
                "Start-Sleep -Milliseconds 250",
                # 恢复剪贴板也走 SetText，避免把历史剪贴板文本额外补成多行。
                "if ($null -ne $previousClipboard) { try { [System.Windows.Forms.Clipboard]::SetText($previousClipboard) } catch {} }",
            ]
        )
        send_script_path.write_text(send_script, encoding="utf-8-sig")
        result = subprocess.run(
            [
                self.config.pwsh_path,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(send_script_path),
                "-WindowHandle",
                str(int(hwnd)),
                "-CommandText",
                command_text,
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        try:
            send_script_path.unlink(missing_ok=True)
        except OSError as exc:
            self.log(f"cleanup foreground hwnd send script failed path={send_script_path} error={exc}")
        if result.returncode != 0:
            raise RuntimeError(self._summarize_result(result.stderr or result.stdout or "WINDOW_SEND_FAILED", 500))

    def _send_text_to_managed_foreground_if_available(self, chat_id: str, text: str) -> bool:
        """Send text to the saved managed foreground session when it is still usable.

        Args:
            chat_id: Feishu chat id.
            text: Text to send into Claude.

        Returns:
            True when a managed foreground session accepted the text; otherwise False.
        """

        chat_state = self._refresh_chat_runtime_state(chat_id)
        foreground_pid = chat_state.get("foreground_pid") or chat_state.get("active_pid")
        if (
            not foreground_pid
            or not self._process_exists(foreground_pid)
            or chat_state.get("status") not in {"foreground_opened", "foreground_busy", "foreground_running"}
        ):
            return False
        if self._current_foreground_settings_stale(chat_state):
            # 当前前台窗口的启动参数无法热更新；继续复用窗口时只标记“下次新窗口生效”，避免误杀正在看的会话。
            self.state.update_chat(
                chat_id,
                {"runtime_settings_pending_restart": True},
                self.config.default_cwd,
            )
        # 已托管的前台窗口是飞书普通文本的主路径；它内部会重新定位真实终端窗口，降低 HWND 过期导致的发送失败。
        self._send_command_to_existing_foreground_session(chat_id, text, int(foreground_pid))
        return True

    def _is_stopped_configuration_state(self, chat_id: str) -> bool:
        """Tell whether plain text should be blocked after an explicit stop.

        Args:
            chat_id: Feishu chat id.

        Returns:
            True when the active session is stopped and no managed foreground process remains.
        """

        chat_state = self.state.get_chat(chat_id, self.config.default_cwd)
        # 停止后只允许目录/模型/权限/显式启动命令改变状态；普通自然语言不能再次隐式启动本机执行。
        return (
            chat_state.get("status") == "stopped"
            and not chat_state.get("managed_session")
            and not chat_state.get("active_pid")
            and not chat_state.get("foreground_pid")
        )

    def _send_text_to_active_window_or_prompt(self, chat_id: str, text: str) -> bool:
        """Send plain Feishu text to the currently selected realtime window.

        Args:
            chat_id: Feishu chat id.
            text: Text to send into Claude.

        Returns:
            True when the text was sent or a prompt was sent; callers should stop routing.
        """

        chat_state = self.state.get_chat(chat_id, self.config.default_cwd)
        active_hwnd = chat_state.get("active_window_hwnd")
        has_window_snapshot = bool(chat_state.get("last_window_targets"))
        if (not active_hwnd or not has_window_snapshot) and self._send_text_to_managed_foreground_if_available(chat_id, text):
            return True

        target = self._resolve_active_window_target(chat_id)
        if not target:
            if self._send_text_to_managed_foreground_if_available(chat_id, text):
                return True
            return True
        try:
            self._send_command_to_foreground_hwnd(int(target["hwnd"]), text)
            chat_state = self.state.get_chat(chat_id, self.config.default_cwd)
            managed_pid = int(chat_state.get("foreground_pid") or chat_state.get("active_pid") or target["pid"] or 0)
            started_at = time.time()
            target_title = str(target.get("title") or f"HWND {target['hwnd']}")
            self.state.update_chat(
                chat_id,
                {
                    # HWND 直连发送同样代表一轮 Claude 前台任务开始；写入 busy 后 watcher 才能发送完成/暂停通知。
                    "status": "foreground_busy",
                    "last_command": text,
                    # Windows Terminal tab 激活后实时序号可能重排；状态里用 HWND/标题表达稳定路由目标。
                    "last_result": f"已将命令发送到选中窗口：{target_title}",
                    "last_error": "",
                    "started_at": started_at,
                    "finished_at": None,
                    "active_pid": managed_pid if managed_pid else chat_state.get("active_pid"),
                    "foreground_pid": managed_pid if managed_pid else chat_state.get("foreground_pid"),
                    # 发送成功后刷新选中 HWND，避免后续普通文本因旧序号变化被误判到其他窗口。
                    "active_window_hwnd": target["hwnd"],
                    "active_window_title": target_title,
                    "managed_session": True,
                    "pending_action": "",
                    "pending_prompt": "",
                    # 命令已经送入前台窗口，本轮旧提问选项随之失效，避免后续数字回复再次命中旧选项。
                    "foreground_pending_question": {},
                    "last_exit_code": None,
                },
                self.config.default_cwd,
            )
            if managed_pid and self._process_exists(managed_pid):
                # 手动打开/重绑定窗口也要确保观察器在线，否则命令已送达但飞书端不会收到结束通知。
                self._start_foreground_watch(chat_id, str(chat_state.get("cwd") or self.config.default_cwd), managed_pid)
            self.send_text(chat_id, f"已发送到选中窗口：{target_title}")
        except Exception as exc:
            clean_error = self._normalize_foreground_send_error(str(exc))
            self.send_text(chat_id, f"前台窗口发送失败：{clean_error}\n可回复：窗口列表，切换到窗口1，新窗口继续")
        return True

    def _resolve_claude_screenshot_windows(self, chat_id: str, chat_state: dict[str, Any]) -> tuple[dict[str, Any], int, list[int]]:
        """Resolve the current foreground Claude PID and all screenshot-able terminal HWNDs.

        Args:
            chat_id: Feishu chat id whose active session owns the foreground Claude window.
            chat_state: Merged chat/session state read before screenshot routing.

        Returns:
            A tuple of refreshed chat state, selected foreground PID, and visible terminal window handles.
        """

        # 前台 PID/HWND 恢复策略归 ForegroundAdapter 管，主 bot 只保留截图发送编排。
        return self.foreground_adapter.resolve_screenshot_windows(chat_id, chat_state)

    def _send_claude_screenshot(self, chat_id: str, chat_state: dict[str, Any]) -> None:
        """Send screenshots of all managed foreground Claude windows."""

        chat_state, active_pid, hwnds = self._resolve_claude_screenshot_windows(chat_id, chat_state)
        if not active_pid:
            self.send_text(chat_id, "当前没有可截图的 Claude 前台窗口。请先回复“前台继续”或“前台运行”打开窗口。")
            return

        self.log(f"claude screenshot attempt chat={chat_id} status={chat_state.get('status')} pid={active_pid} hwnds={len(hwnds)}")

        if not hwnds:
            # PID 存活但没有可用 HWND 时通常是旧版 launcher、最小化或零尺寸控制台；提示用户走桌面截图或重开 v2 窗口。
            self.send_text(chat_id, self._normalize_window_screenshot_error(
                "当前 Claude 前台进程仍在，但没有暴露可截图的终端窗口。"
                "可回复“截图 桌面”查看当前屏幕，或回复“前台继续”重新打开 v2 托管窗口。"))
            return

        sent_count = 0
        errors: list[str] = []
        for i, hwnd in enumerate(hwnds):
            tag = f"{active_pid}-{i}" if len(hwnds) > 1 else str(active_pid)
            if len(hwnds) > 1:
                title_len = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                title_buf = ctypes.create_unicode_buffer(title_len + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, title_buf, title_len + 1)
                # Strip Windows Terminal status prefix (e.g. "⠁ ") and common stale suffixes.
                title = title_buf.value.strip()
                if len(title) > 1 and ord(title[0]) > 0x2000:
                    title = title[1:].lstrip()
                for suffix in ("终端截图发送失败", "截图发送失败"):
                    if title.endswith(suffix):
                        title = title[: -len(suffix)].rstrip()
                self.send_text(chat_id, f"[{i + 1}/{len(hwnds)}] {title}")
            screenshot_path: Path | None = None
            try:
                screenshot_path = self._capture_hwnd_screenshot(hwnd, tag=tag)
                self.send_image(chat_id, screenshot_path)
                sent_count += 1
                self.log(f"claude screenshot sent chat={chat_id} hwnd={hwnd} path={screenshot_path}")
            except Exception as exc:
                self.log(f"claude screenshot failed chat={chat_id} hwnd={hwnd} error={exc}")
                errors.append(str(exc))
            finally:
                if screenshot_path is not None:
                    self._cleanup_path(screenshot_path, "claude screenshot image")

        if sent_count == 0:
            self.send_text(chat_id, self._normalize_window_screenshot_error(errors[0] if errors else "截图失败"))
        elif errors:
            self.send_text(chat_id, f"已发送 {sent_count}/{len(hwnds)} 张截图，部分窗口截图失败。")

    def _send_claude_screenshot_by_index(self, chat_id: str, chat_state: dict[str, Any], index: int) -> None:
        """Send one Claude foreground screenshot by the 1-based window index shown in Feishu.

        Args:
            chat_id: Feishu chat id that receives the screenshot.
            chat_state: Merged chat/session state read before screenshot routing.
            index: User-facing 1-based window number, for example `截图 1`.

        Returns:
            None. The method sends either an image or a concise text error to Feishu.
        """

        if index <= 0:
            self.send_text(chat_id, "截图编号必须从 1 开始，例如：截图 1。")
            return

        chat_state, active_pid, hwnds = self._resolve_claude_screenshot_windows(chat_id, chat_state)
        self.log(
            f"claude screenshot attempt chat={chat_id} status={chat_state.get('status')} pid={active_pid} "
            f"hwnds={len(hwnds)} index={index}"
        )
        if not active_pid:
            self.send_text(chat_id, "当前没有可截图的 Claude 前台窗口。请先回复“前台继续”或“前台运行”打开窗口。")
            return
        if not hwnds:
            # PID 存活但没有可用 HWND 时通常是旧版 launcher、最小化或零尺寸控制台；提示用户走桌面截图或重开 v2 窗口。
            self.send_text(chat_id, self._normalize_window_screenshot_error(
                "当前 Claude 前台进程仍在，但没有暴露可截图的终端窗口。"
                "可回复“截图 桌面”查看当前屏幕，或回复“前台继续”重新打开 v2 托管窗口。"))
            return
        if index > len(hwnds):
            self.send_text(chat_id, f"当前只找到 {len(hwnds)} 个 Claude 前台窗口，请回复：截图 1。")
            return

        hwnd = hwnds[index - 1]
        screenshot_path: Path | None = None
        try:
            screenshot_path = self._capture_hwnd_screenshot(hwnd, tag=f"{active_pid}-{index - 1}")
            self.send_image(chat_id, screenshot_path)
            self.log(f"claude screenshot sent chat={chat_id} hwnd={hwnd} path={screenshot_path} index={index}")
        except Exception as exc:
            self.log(f"claude screenshot failed chat={chat_id} hwnd={hwnd} error={exc} index={index}")
            self.send_text(chat_id, self._normalize_window_screenshot_error(str(exc)))
        finally:
            if screenshot_path is not None:
                self._cleanup_path(screenshot_path, "claude screenshot image")

    def _send_desktop_screenshot(self, chat_id: str) -> None:
        """Send a screenshot of the primary desktop screen."""

        self.log(f"desktop screenshot attempt chat={chat_id}")
        screenshot_path: Path | None = None
        try:
            screenshot_path = self._capture_desktop_screenshot()
            self.send_image(chat_id, screenshot_path)
            self.log(f"desktop screenshot sent chat={chat_id} path={screenshot_path}")
        except Exception as exc:
            # 桌面截图比 Claude 窗口截图更依赖当前桌面环境，失败时把原因直接发回手机端。
            self.log(f"desktop screenshot failed chat={chat_id} error={exc}")
            self.send_text(chat_id, f"桌面截图发送失败：{self._summarize_result(str(exc), 500)}")
        finally:
            if screenshot_path is not None:
                # 桌面截图包含当前屏幕内容，上传后立即清理能降低遗留敏感画面的风险。
                self._cleanup_path(screenshot_path, "desktop screenshot image")

    def _summarize_result(self, text: str, limit: int = 800) -> str:
        """Trim long Claude output so notification cards stay readable on mobile."""

        # PowerShell/Claude 有时会返回 ANSI 颜色控制码；飞书文本消息不会解析这些控制码，
        # 不过滤会出现 [31;1m 这类乱码，影响手机端排障阅读。
        cleaned = self._strip_ansi_sequences(text or "").strip()
        if not cleaned:
            return "(Claude 未返回文本输出)"
        if len(cleaned) <= limit:
            return cleaned
        # Truncation keeps completion notices readable while the full raw output
        # remains available in the local state file for later troubleshooting.
        return cleaned[:limit].rstrip() + "\n...(结果已截断)"

    @staticmethod
    def _is_transcript_noise_line(line: str) -> bool:
        """Tell whether one PowerShell transcript line is metadata rather than Claude output."""

        if not line:
            return True
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

    def _read_foreground_transcript_summary(self, chat_state: dict[str, Any], limit: int = 900) -> str:
        """Read the visible foreground Claude transcript tail for status summaries."""

        transcript_value = str(chat_state.get("foreground_transcript_path") or "").strip()
        if not transcript_value:
            return ""
        transcript_path = Path(transcript_value)
        try:
            if not transcript_path.exists() or not transcript_path.is_file():
                return ""
            raw_text = transcript_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self.log(f"foreground transcript read failed path={transcript_path} error={exc}")
            return ""

        cleaned_lines: list[str] = []
        for raw_line in raw_text.splitlines():
            line = self._strip_ansi_sequences(raw_line).strip()
            if self._is_transcript_noise_line(line):
                continue
            cleaned_lines.append(line)
        if not cleaned_lines:
            return ""
        text = "\n".join(cleaned_lines)
        # 状态查询应该看最新进展；从尾部向前找最近的轮次/进度锚点，避免 transcript 头部旧内容占满摘要。
        round_index = max(text.rfind("Round "), text.rfind("进入 Round"))
        progress_index = max(text.rfind(anchor) for anchor in ("本轮迭代已完成", "Reading ", "Baked for", "Brewed for", "Leaving"))
        start_index = round_index if round_index >= 0 else progress_index
        if start_index >= 0:
            text = text[start_index:]
        elif len(text) > limit:
            text = text[-limit:]
        return self._summarize_result(text.strip(), limit)

    @staticmethod
    def _encode_claude_project_dir_name(cwd: str) -> str:
        """Convert a working directory into Claude's local project log directory name."""

        # Claude Code 在 Windows 上把 `D:\code\xxx` 写成 `D--code-xxx`；
        # 状态查询需要复用这个规则，才能从 Claude 自己的 JSONL 会话日志读取真实前台摘要。
        normalized = str(Path(cwd).resolve() if cwd else Path.cwd())
        return normalized.replace(":", "-").replace("\\", "-").replace("/", "-")

    def _get_claude_jsonl_candidates(self, chat_state: dict[str, Any]) -> list[Path]:
        """Find likely Claude JSONL transcript files for the current chat working directory."""

        candidates: list[Path] = []
        transcript_value = str(chat_state.get("foreground_transcript_path") or "").strip()
        if transcript_value.lower().endswith(".jsonl"):
            candidates.append(Path(transcript_value))
        cwd = str(chat_state.get("cwd") or self.config.default_cwd or "").strip()
        project_dir = DEFAULT_CLAUDE_HOME / "projects" / self._encode_claude_project_dir_name(cwd)
        if project_dir.exists():
            try:
                # 前台状态查询应该优先读取最近写入的 Claude 会话日志；旧会话只作为兜底。
                candidates.extend(sorted(project_dir.glob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True))
            except OSError as exc:
                self.log(f"claude jsonl scan failed dir={project_dir} error={exc}")
        seen: set[str] = set()
        unique_candidates: list[Path] = []
        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            unique_candidates.append(candidate)
        return unique_candidates

    @staticmethod
    def _read_text_file_tail(path: Path, max_bytes: int = CLAUDE_JSONL_TAIL_BYTES) -> str:
        """Read only the tail of a potentially large text file."""

        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            file_size = handle.tell()
            handle.seek(max(0, file_size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")

    @staticmethod
    def _extract_assistant_text_from_jsonl(record: dict[str, Any]) -> str:
        """Extract readable assistant text from one Claude JSONL record."""

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

    @staticmethod
    def _trim_summary_from_latest_anchor(text: str) -> str:
        """Trim a Claude summary from the newest known progress anchor when one exists."""

        if not text:
            return ""
        # Round 类任务优先从最近轮次标题截取；若不是轮次化任务，再从完成/进度标记截取；
        # 普通非循环任务没有标记时直接返回最近 assistant 文本。
        round_index = max(text.rfind("Round "), text.rfind("进入 Round"))
        progress_index = max(text.rfind(anchor) for anchor in CLAUDE_SUMMARY_ANCHORS[2:])
        start_index = round_index if round_index >= 0 else progress_index
        if start_index >= 0:
            return text[start_index:].strip()
        return text.strip()

    def _read_claude_jsonl_summary(self, chat_state: dict[str, Any], limit: int = 900) -> str:
        """Read the latest assistant summary from Claude's native JSONL logs."""

        for jsonl_path in self._get_claude_jsonl_candidates(chat_state):
            try:
                if not jsonl_path.exists() or not jsonl_path.is_file():
                    continue
                raw_tail = self._read_text_file_tail(jsonl_path)
            except OSError as exc:
                self.log(f"claude jsonl read failed path={jsonl_path} error={exc}")
                continue
            for raw_line in reversed(raw_tail.splitlines()):
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                assistant_text = self._extract_assistant_text_from_jsonl(record)
                if not assistant_text:
                    continue
                return self._summarize_result(self._trim_summary_from_latest_anchor(assistant_text), limit)
        return ""

    def _strip_ansi_sequences(self, text: str) -> str:
        """Remove terminal ANSI escape sequences before sending text to Feishu."""

        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text or "")

    def _normalize_command_key(self, text: str) -> str:
        """Normalize mobile command text for exact command dispatch."""

        # v2 统一由 CommandRouter 维护命令 key 规则，避免截图/权限/模型分支各自漂移。
        return self.command_router.normalize_command_key(text)

    @staticmethod
    def _parse_screenshot_index_key(command_key: str) -> int | None:
        """Parse a normalized `截图N` command into a 1-based screenshot index.

        Args:
            command_key: Text after mobile whitespace normalization, for example `截图1`.

        Returns:
            The requested 1-based window index, or None when the text is not an indexed screenshot command.
        """

        return CommandRouter().parse_screenshot_index_key(command_key)

    def _should_dedupe_control_command(self, chat_id: str, command_key: str, window_seconds: float = 2.5) -> bool:
        """Drop identical control commands repeated within a very short window."""

        now = time.time()
        dedupe_key = (chat_id, command_key)
        last_seen = self.recent_control_commands.get(dedupe_key, 0.0)
        self.recent_control_commands[dedupe_key] = now
        # 控制命令都是短文本且有明显副作用；在 2.5 秒内重复出现时，优先视作飞书重复投递。
        return last_seen > 0 and (now - last_seen) <= window_seconds

    def _should_dedupe_plain_text_command(self, chat_id: str, text: str, window_seconds: float = 12.0) -> bool:
        """Drop identical plain text commands repeated within a short retry window.

        Args:
            chat_id: Feishu chat id used to scope dedupe to one conversation.
            text: Plain text that would otherwise be injected into the selected Claude window.
            window_seconds: Maximum interval treated as a Feishu retry or duplicate event.

        Returns:
            True when the same chat has already routed the same text recently.
        """

        normalized_text = text.strip()
        if not normalized_text:
            return False
        now = time.time()
        text_hash = hashlib.sha1(normalized_text.encode("utf-8", errors="ignore")).hexdigest()
        dedupe_key = (chat_id, text_hash)
        last_seen = self.recent_plain_text_commands.get(dedupe_key, 0.0)
        self.recent_plain_text_commands[dedupe_key] = now
        if len(self.recent_plain_text_commands) > 256:
            cutoff = now - window_seconds
            # 普通文本去重只保护飞书短时间重复投递；过期记录清掉，避免长驻 bot 内存持续增长。
            self.recent_plain_text_commands = {
                existing_key: seen_at
                for existing_key, seen_at in self.recent_plain_text_commands.items()
                if seen_at >= cutoff
            }
        # 只有默认普通文本分支会调用这里；控制命令仍走各自的更短窗口去重策略。
        return last_seen > 0 and (now - last_seen) <= window_seconds

    def _should_dedupe_message_event(self, message_id: str, window_seconds: float = 600.0) -> bool:
        """Skip replayed Feishu events that carry the same message id."""

        if not message_id:
            return False
        now = time.time()
        last_seen = self.recent_message_ids.get(message_id, 0.0)
        self.recent_message_ids[message_id] = now
        if len(self.recent_message_ids) > 512:
            cutoff = now - window_seconds
            # message_id 去重只用于最近一小段时间内的重放保护；定期回收可避免长驻进程无限涨内存。
            self.recent_message_ids = {
                existing_id: seen_at for existing_id, seen_at in self.recent_message_ids.items() if seen_at >= cutoff
            }
        # 持久化到磁盘，避免重启后重复处理旧消息。
        self._save_message_ids()
        return last_seen > 0 and (now - last_seen) <= window_seconds

    def _normalize_window_screenshot_error(self, text: str) -> str:
        """Convert low-level screenshot failures into concise Chinese guidance."""

        cleaned = self._strip_ansi_sequences(text or "")
        lowered = cleaned.lower()
        if (
            "没有找到可截图的 claude code 终端窗口" in cleaned
            or "没有找到可激活的 claude code 终端窗口" in cleaned
            or "capture-window-" in lowered
        ):
            return "当前没有可截图的 Claude 前台窗口。请先回复“前台继续”或“前台运行”打开窗口后再试。"
        if "窗口尺寸无效" in cleaned:
            return "Claude 前台窗口当前尺寸无效，可能已最小化。请先让窗口可见后再试。"
        if "读取窗口位置失败" in cleaned or "无法激活" in cleaned:
            return "Claude 前台窗口暂时无法激活。请确认窗口仍存在且没有被系统隐藏，然后再试一次。"
        return f"Claude 窗口截图发送失败：{self._summarize_result(cleaned, 220)}"

    def _normalize_foreground_send_error(self, text: str) -> str:
        """Convert foreground injection failures into concise Chinese guidance."""

        cleaned = self._strip_ansi_sequences(text or "")
        lowered = cleaned.lower()
        if "appactivate" in lowered or "无法激活" in cleaned:
            return "Claude 前台窗口暂时无法激活。请确认该窗口仍存在、未被最小化，并且当前桌面可交互后再试。"
        if "没有找到可激活的 claude code 终端窗口" in cleaned:
            return "当前没有找到可接管的 Claude 前台窗口。请先回复“前台运行”或“前台继续”。"
        if "window_activate_failed" in lowered or "window_tab_activate_failed" in lowered:
            return "Claude 前台窗口无法激活，可能被其他窗口遮挡或最小化。可回复：窗口列表，切换到窗口1，或新窗口继续。"
        if "send-foreground" in lowered or ("claude code" in lowered and ("exception:" in lowered or "�" in cleaned)):
            # PowerShell stderr 会按控制台编码回传，中文 throw 可能变成乱码；飞书端只暴露可执行建议。
            return "Claude 前台窗口发送失败：当前窗口不是 v2 管理的可接管窗口，或系统拒绝激活。请回复“前台继续”打开 v2 管理窗口后再发送。"
        return self._summarize_result(cleaned, 500)

    def _is_failure_like_text(self, text: str) -> bool:
        """Treat known provider/runtime transport failures as task failures."""

        lowered = (text or "").lower()
        if self._is_auth_wait_text(text):
            # Claude 有时会以 0 退出码返回“需要编辑授权”类文本；这里先归类为受阻，后续再转成 waiting_auth。
            return True
        indicators = (
            "peer closed connection without sending complete message body",
            "incomplete chunked read",
            "provider api request failed",
            "api request failed",
            "request failed",
            "authentication failed",
            "permission denied",
            "rate limit",
            "timed out",
            "timeout",
            "connection refused",
            "network error",
            "internal server error",
            "service unavailable",
            "quota exceeded",
            "需要您授权",
            "需要授权",
            "没有权限",
            "无权",
            "permission required",
            "requires permission",
        )
        return any(indicator in lowered for indicator in indicators)

    def _is_auth_wait_text(self, text: str) -> bool:
        """Detect permission-blocked outputs that should keep the chat session managed."""

        lowered = (text or "").lower()
        indicators = (
            "需要您授权",
            "需要授权",
            "没有权限",
            "无权",
            "permission required",
            "requires permission",
            "approval required",
        )
        if any(indicator in lowered for indicator in indicators):
            return True
        auth_patterns = (
            r"需要[你您]?[^\n。；;]{0,60}授权",
            r"请批准[^\n。；;]{0,80}\bedit\b",
            r"批准上面的\s*edit",
            r"\ballow[^\n。；;]{0,40}\bedits?\b",
            r"\bdo you want to make this edit\b",
        )
        # 兼容 Claude 中文提示里把“需要”和“授权”拆开的说法，例如“需要你对文件编辑操作授权”。
        return any(re.search(pattern, lowered, re.IGNORECASE) for pattern in auth_patterns)

    def _is_managed_status(self, status: Any) -> bool:
        """Identify statuses that mean this chat still owns a Claude session."""

        return str(status) in {
            "running",
            "foreground_running",
            "foreground_opened",
            "foreground_busy",
            "waiting_auth",
            "waiting_continue",
        }

    def _should_resume_existing_session(self, chat_state: dict[str, Any], requested_continue: bool) -> bool:
        """Decide whether the next Claude invocation should reuse the existing session."""

        if requested_continue:
            return True
        status = str(chat_state.get("status", "idle"))
        managed_session = bool(chat_state.get("managed_session"))
        # 只要当前聊天还托管着 Claude 会话，并且不是首次 idle/stopped 状态，
        # 就把“运行/自然语言”也视为在同一会话里追加的新指令。
        return managed_session and status not in {"idle", "stopped"}

    def _should_block_plain_text_after_stop(self, chat_state: dict[str, Any]) -> bool:
        """Tell whether plain text should stay in config-only mode after an explicit stop."""

        status = str(chat_state.get("status", "idle"))
        managed_session = bool(chat_state.get("managed_session"))
        foreground_pid = chat_state.get("foreground_pid")
        # 用户显式停止后，当前聊天进入“仅配置、不自动执行”状态；
        # 这样后续发“权限/模型/目录”时不会被普通文本兜底重新拉起 Claude。
        if status == "stopped" and not managed_session:
            return True
        # 若 stop 后又被错误认回了一个仍存活的前台窗口，这里放行给前台窗口复用逻辑处理。
        return status == "stopped" and not foreground_pid

    def _is_v2_managed_foreground_state(self, chat_state: dict[str, Any]) -> bool:
        """Tell whether the saved foreground PID belongs to a v2-launched window.

        Args:
            chat_state: Current merged chat/session state.

        Returns:
            True only when the foreground session has v2 transcript metadata.
        """

        transcript_path = str(chat_state.get("foreground_transcript_path") or "")
        foreground_pid = chat_state.get("foreground_pid")
        # 旧/手工 Windows Terminal 可以截图，但不能作为飞书“继续”的注入目标；
        # 只有 v2 launcher 写入 transcript 后，文本注入和 watcher 状态机才是闭环的。
        return bool(foreground_pid and "feishu-claude-v2" in transcript_path.lower())

    def _ensure_continue_session_selected(self, chat_id: str) -> bool:
        """Ensure a valid active session is selected before running a continue command.

        Args:
            chat_id: Feishu chat id.

        Returns:
            True when the current active session can be continued; otherwise sends
            a selection prompt and returns False.
        """

        sessions = self.state.list_sessions(chat_id, self.config.default_cwd)
        active_id = self.state.get_active_session_id(chat_id, self.config.default_cwd)
        session_map = {sid: sstate for sid, sstate in sessions}
        active_state = session_map.get(active_id)
        if active_state is not None:
            return True

        if not sessions:
            self.send_text(
                chat_id,
                self._build_sectioned_message(
                    "当前没有可继续的会话",
                    ["说明：没有找到上一轮 Claude 会话。"],
                    ["新建会话", "运行 <任务>"],
                ),
            )
            return False

        lines = [f"当前 active_session={active_id or '(空)'} 不存在，请先选择一个会话。"]
        for sid, sstate in sessions:
            status = sstate.get("status", "idle")
            label = sstate.get("label", "")
            last_cmd = self._summarize_result(str(sstate.get("last_command") or sstate.get("last_result") or ""), 50)
            display = f"{sid}"
            if label:
                display += f"（{label}）"
            display += f" [{status}]"
            if last_cmd:
                display += f" - {last_cmd}"
            lines.append(display)
        self.send_text(
            chat_id,
            self._build_sectioned_message(
                "请选择要继续的会话",
                lines,
                ["切换会话 <id>", "会话列表", "新建会话"],
            ),
        )
        return False

    def _sanitize_file_stem(self, value: str) -> str:
        """Create a filesystem-safe launcher filename fragment from task text."""

        safe_text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", value).strip("-")
        return safe_text[:40] or "task"

    def _ps_quote(self, value: str) -> str:
        """Escape one string for safe embedding in a PowerShell single-quoted string."""

        return value.replace("'", "''")

    def _process_exists(self, pid: Any) -> bool:
        """Check whether a previously launched local process is still alive."""

        try:
            pid_value = int(pid)
        except (TypeError, ValueError):
            return False
        if pid_value <= 0:
            return False
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_value)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False

    def _find_existing_foreground_launcher_pid(self, chat_state: dict[str, Any]) -> int | None:
        """Find the existing managed foreground launcher window for the current chat."""

        cwd = str(chat_state.get("cwd") or self.config.default_cwd).strip()
        last_command = str(chat_state.get("last_command") or "").strip()
        # 当前前台 Claude 可能已经跑了很久，状态文件却因为旧逻辑丢失了 foreground_pid；
        # 这里先按 launcher 脚本特征识别，再兜底识别“带有 claude.exe 子进程的 pwsh 前台会话”，
        # 确保手工打开或脱离 launcher 的前台窗口也能重新绑定回来。
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                "$cwdTarget = @'\n"
                + cwd
                + "\n'@.Trim();"
                "$commandHint = @'\n"
                + last_command
                + "\n'@.Trim();"
                "$all = @(Get-CimInstance Win32_Process);"
                "$candidates = @();"
                "$launcherMatches = $all | "
                "Where-Object { $_.Name -eq 'pwsh.exe' -and ([string]$_.CommandLine).Contains('\\feishu-claude-v2\\temp\\launchers\\') } | "
                "ForEach-Object { "
                "  $line = [string]$_.CommandLine; "
                "  $score = 0; "
                "  if ($cwdTarget -and $line.Contains($cwdTarget)) { $score += 20 }; "
                "  if ($commandHint -and $line.Contains($commandHint)) { $score += 5 }; "
                "  if ($line.Contains('-NoExit')) { $score += 2 }; "
                "  [pscustomobject]@{ ProcessId = $_.ProcessId; Score = $score; CreationDate = $_.CreationDate } "
                "};"
                "$candidates += @($launcherMatches);"
                "$genericMatches = $all | "
                "Where-Object { $_.Name -eq 'pwsh.exe' } | "
                "ForEach-Object { "
                "  $pwsh = $_; "
                "  $children = @($all | Where-Object { $_.ParentProcessId -eq $pwsh.ProcessId -and $_.Name -eq 'claude.exe' }); "
                "  if (-not $children) { return }; "
                "  $score = 10; "
                "  $line = [string]$pwsh.CommandLine; "
                "  if ($cwdTarget -and $line.Contains($cwdTarget)) { $score += 5 }; "
                "  if ($commandHint -and $line.Contains($commandHint)) { $score += 2 }; "
                "  [pscustomobject]@{ ProcessId = $pwsh.ProcessId; Score = $score; CreationDate = $pwsh.CreationDate } "
                "};"
                "$candidates += @($genericMatches);"
                "$candidates | "
                "Where-Object { $_ -and $_.Score -gt 0 } | "
                "Sort-Object Score, CreationDate -Descending | "
                "Select-Object -First 1 -ExpandProperty ProcessId"
            ),
        ]
        result = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        pid_text = (result.stdout or "").strip()
        if result.returncode != 0 or not pid_text.isdigit():
            return None
        pid_value = int(pid_text)
        return pid_value if self._process_exists(pid_value) else None

    def _find_realtime_claude_screenshot_target(self, chat_state: dict[str, Any]) -> tuple[int, list[int]]:
        """Find a screenshot-able Claude terminal from the live process/window table.

        Args:
            chat_state: Current merged chat/session state used only for cwd/command scoring.

        Returns:
            A launcher PID and visible HWND list. Returns ``(0, [])`` when Claude
            exists but no safe, visible terminal window can be confirmed.
        """

        live_hwnds = self._find_all_claude_terminal_hwnds(0)
        if live_hwnds:
            # 标题或进程树已实时确认包含 Claude；没有可靠 PID 时用 -1 表示“实时窗口目标”。
            return -1, live_hwnds

        detected_pid = self._find_existing_foreground_launcher_pid(chat_state)
        if detected_pid and self._process_exists(detected_pid):
            detected_hwnds = self._find_all_claude_terminal_hwnds(detected_pid)
            if detected_hwnds:
                return detected_pid, detected_hwnds

        return 0, []

    def _ensure_foreground_binding(self, chat_id: str, chat_state: dict[str, Any]) -> dict[str, Any]:
        """Rebind the chat to an already running foreground launcher without restarting it."""

        foreground_pid = chat_state.get("foreground_pid")
        if foreground_pid and self._process_exists(foreground_pid):
            try:
                self._start_foreground_watch(chat_id, str(chat_state.get("cwd") or self.config.default_cwd), int(foreground_pid))
            except Exception as exc:
                self.log(f"foreground watch ensure failed chat={chat_id} pid={foreground_pid} error={exc}")
            return chat_state
        detected_pid = self._find_existing_foreground_launcher_pid(chat_state)
        if not detected_pid:
            return chat_state
        updated_state = self.session_manager.update_chat(
            chat_id,
            {
                # 这里只补状态关联，不创建新窗口也不结束旧窗口，避免干扰用户当前正在跑的前台任务。
                "foreground_pid": detected_pid,
                "managed_session": True,
                "status": "foreground_opened"
                if str(chat_state.get("status", "")) in {"idle", "done", "failed", "stopped", ""}
                else chat_state.get("status"),
                "last_result": chat_state.get("last_result") or "已重新识别到当前会话对应的 Claude 前台窗口。",
                "pending_action": chat_state.get("pending_action") or "continue_session",
                "pending_prompt": chat_state.get("pending_prompt") or "继续",
            },
        )
        self.log(
            f"foreground rebound chat={chat_id} pid={detected_pid} cwd={updated_state.get('cwd')} status={updated_state.get('status')}"
        )
        try:
            self._start_foreground_watch(chat_id, str(updated_state.get("cwd") or self.config.default_cwd), int(detected_pid))
        except Exception as exc:
            self.log(f"foreground watch ensure failed chat={chat_id} pid={detected_pid} error={exc}")
        return updated_state

    def _cleanup_stale_runtime_state(self) -> None:
        """Recover chats left in a running state after local process loss or bot restarts."""

        stale_statuses = {"running", "foreground_running", "foreground_opened", "foreground_busy"}
        chats = self.session_manager.iter_chat_states()
        for chat_id, chat_state in chats.items():
            chat_state = self._ensure_foreground_binding(chat_id, dict(chat_state))
            status = str(chat_state.get("status", "idle"))
            if status not in stale_statuses:
                continue
            runtime_pid = chat_state.get("active_pid")
            foreground_pid = chat_state.get("foreground_pid")
            is_foreground_status = status in {"foreground_running", "foreground_opened", "foreground_busy"}
            process_pid = foreground_pid if is_foreground_status else runtime_pid
            if process_pid and self._process_exists(process_pid):
                continue
            # Recover stale runtime state eagerly on startup so new tasks are not
            # blocked by a dead process that never got a chance to write final state.
            self.session_manager.update_chat(
                chat_id,
                {
                    # When a previously running process disappears, preserve the chat as
                    # a managed session so the user can still continue it from Feishu.
                    "status": "failed",
                    "finished_at": time.time(),
                    "last_error": "检测到旧任务状态残留，机器人已自动回收进程状态。可直接回复“继续”恢复下一轮。",
                    "active_pid": None,
                    # 后台任务残留回收不能清空仍可用的前台窗口登记，否则“前台继续”会重复开窗。
                    "foreground_pid": None if is_foreground_status else foreground_pid,
                    "pending_action": "continue_session",
                    "pending_prompt": "继续",
                    "managed_session": True,
                },
            )

    def _refresh_chat_runtime_state(self, chat_id: str) -> dict[str, Any]:
        """Reconcile one chat state against the actual local process table."""

        chat_state = self.session_manager.get_chat(chat_id)
        chat_state = self._ensure_foreground_binding(chat_id, chat_state)
        status = str(chat_state.get("status", "idle"))
        active_pid = chat_state.get("active_pid")
        foreground_pid = chat_state.get("foreground_pid")
        if foreground_pid and self._process_exists(foreground_pid) and status not in {"foreground_running", "foreground_opened", "foreground_busy"}:
            # 某些前台轮次会先落成 done/failed，但窗口其实还活着并且用户已经继续在里面操作；
            # 这里优先把状态恢复成“前台会话在线”，避免手机端长期显示过期的历史失败结果。
            return self.session_manager.update_chat(
                chat_id,
                {
                    "status": "foreground_opened",
                    "active_pid": foreground_pid,
                    "finished_at": None,
                    "last_error": "",
                    "managed_session": True,
                    "pending_action": "continue_session",
                    "pending_prompt": "继续",
                },
            )
        if status not in {"running", "foreground_running", "foreground_opened", "foreground_busy"}:
            return chat_state
        is_foreground_status = status in {"foreground_running", "foreground_opened", "foreground_busy"}
        process_pid = foreground_pid if is_foreground_status else active_pid
        if process_pid and self._process_exists(process_pid):
            return chat_state

        return self.session_manager.update_chat(
            chat_id,
            {
                # Process-level execution结束后仍保留托管会话，避免飞书端失去继续入口。
                "status": "failed",
                "finished_at": time.time(),
                "last_result": status in {"foreground_opened", "foreground_busy"} and "前台会话窗口已关闭。" or chat_state.get("last_result", ""),
                "last_error": "检测到任务进程已退出，机器人已自动回收进程状态。可直接回复“继续”或“前台继续”。",
                "active_pid": None,
                # 后台进程退出只回收 active_pid；前台窗口登记要保留给后续接管和截图使用。
                "foreground_pid": None if is_foreground_status else foreground_pid,
                "pending_action": "continue_session",
                "pending_prompt": "继续",
                "managed_session": True,
            },
        )

    def _normalize_permission_mode(self, raw_mode: str) -> str | None:
        """Normalize permission mode aliases from Feishu commands to Claude CLI values."""

        mode = (raw_mode or "").strip()
        aliases = {
            "默认": "default",
            "默认模式": "default",
            "接受编辑": "acceptEdits",
            "自动接受编辑": "acceptEdits",
            "自动": "auto",
            "绕过": "bypassPermissions",
            "跳过权限": "bypassPermissions",
            "计划": "plan",
            "计划模式": "plan",
            "不询问": "dontAsk",
        }
        mode = aliases.get(mode, mode)
        lowered_map = {item.lower(): item for item in self.ALLOWED_PERMISSION_MODES}
        return lowered_map.get(mode.lower())

    def _resolve_effective_permission_mode(
        self,
        foreground: bool,
        chat_state: dict[str, Any] | None = None,
        prefer_session_override: bool = True,
    ) -> str:
        """Pick the permission mode for background runs versus foreground takeovers."""

        if prefer_session_override and chat_state and chat_state.get("permission_mode"):
            # 飞书会话级授权模式优先于配置文件默认值，便于临时切换而不影响其他会话。
            return str(chat_state["permission_mode"])
        if foreground:
            # 前台窗口更强调可接管和授权交互体验，因此允许与后台任务使用不同默认模式。
            return self.config.foreground_permission_mode or self.config.permission_mode
        # 后台任务通常无人盯守，默认可以配置得更自动化；未配置时仍回到全局默认。
        return self.config.background_permission_mode or self.config.permission_mode

    def _resolve_effective_model(self, chat_state: dict[str, Any] | None = None) -> str:
        """Pick the Claude model alias for the next process invocation."""

        if chat_state and chat_state.get("model"):
            # 模型同样按飞书会话保存，避免一个聊天切模型影响其他聊天。
            return str(chat_state["model"])
        return self.config.default_model or ""

    def _foreground_restart_required(self, chat_state: dict[str, Any]) -> bool:
        """Check whether the managed foreground window is using stale runtime settings."""

        foreground_pid = chat_state.get("foreground_pid")
        if not foreground_pid or not self._process_exists(foreground_pid):
            return False
        configured_permission = self._resolve_effective_permission_mode(True, chat_state)
        configured_model = self._resolve_effective_model(chat_state)
        runtime_permission = str(chat_state.get("runtime_permission_mode") or "")
        runtime_model = str(chat_state.get("runtime_model") or "")
        # 前台窗口一旦打开，其进程参数无法热更新；当会话配置与窗口实际运行参数不一致时，
        # 显式“前台运行/前台继续”应重开窗口，避免飞书里看起来像“已切换但没生效”。
        return runtime_permission != configured_permission or runtime_model != configured_model

    def _current_foreground_settings_stale(self, chat_state: dict[str, Any]) -> bool:
        """Tell whether the currently bound foreground window is behind the saved chat settings."""

        foreground_pid = chat_state.get("foreground_pid")
        if not foreground_pid or not self._process_exists(foreground_pid):
            return False
        return self._foreground_restart_required(chat_state)

    def _build_claude_args(
        self,
        prompt: str,
        continue_mode: bool,
        print_mode: bool,
        foreground: bool,
        chat_state: dict[str, Any] | None = None,
        force_permission_mode: str | None = None,
    ) -> list[str]:
        """Build the Claude CLI argument list for background or foreground execution."""

        command = [self.config.claude_path]
        if print_mode:
            # 后台任务使用 --print 让 Claude 把最终内容写到 stdout，bot 才能汇总后发飞书。
            command.append("--print")
        permission_mode = force_permission_mode or self._resolve_effective_permission_mode(foreground, chat_state)
        # 所有新进程都显式带授权模式，避免 Claude CLI 默认值变更后行为漂移。
        command.extend(["--permission-mode", permission_mode])
        if permission_mode == "bypassPermissions":
            # Claude CLI 在 bypassPermissions 下仅传 --permission-mode 仍会弹出风险确认页；
            # 这里额外补齐官方要求的 --dangerously-skip-permissions，确保飞书里的前台继续
            # 和后台直跑都不会卡在本机等待人工确认。
            command.append("--dangerously-skip-permissions")
        model = self._resolve_effective_model(chat_state)
        if model:
            # Claude CLI 原生支持 --model，允许使用 opus/sonnet/haiku 或完整模型名。
            command.extend(["--model", model])
        # additional_args 放在模型/授权之后、-c 和 prompt 之前，保证全局开关不吞掉用户任务文本。
        command.extend(self.config.additional_args)
        if continue_mode:
            # -c 表示续用 Claude Code 当前项目最近会话，是“继续”能保留上下文的关键参数。
            command.append("-c")
        if prompt:
            # 单独“前台运行”现在允许只打开窗口；没有具体指令时不再给 Claude 强塞一个空参数。
            command.append(prompt)
        return command

    def _write_foreground_launcher(
        self,
        chat_id: str,
        prompt: str,
        cwd: str,
        continue_mode: bool,
        force_permission_mode: str | None = None,
    ) -> Path:
        """Generate a temporary PowerShell launcher that opens Claude in an interactive window."""

        launcher_dir = self._get_temp_dir("launchers")
        transcript_dir = self._get_log_dir("foreground")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        file_stem = self._sanitize_file_stem(prompt)
        launcher_path = launcher_dir / f"{timestamp}-{chat_id[-8:]}-{file_stem}.ps1"
        transcript_path = transcript_dir / f"{timestamp}-{chat_id[-8:]}-{file_stem}-transcript.log"
        chat_state = self.state.get_chat(chat_id, self.config.default_cwd)
        command = self._build_claude_args(
            prompt,
            continue_mode=continue_mode,
            print_mode=False,
            foreground=True,
            chat_state=chat_state,
            force_permission_mode=force_permission_mode,
        )
        started_at = float(chat_state.get("started_at") or time.time())
        python_path = self.config.python_path or sys.executable
        ps_command = ", ".join(f"'{self._ps_quote(part)}'" for part in command)
        launcher_text = "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                f"$chatId = '{self._ps_quote(chat_id)}'",
                f"$workDir = '{self._ps_quote(cwd)}'",
                f"$transcriptPath = '{self._ps_quote(str(transcript_path))}'",
                f"$command = @({ps_command})",
                "Set-Location -LiteralPath $workDir",
                # 前台 Claude 窗口里的后续 Stop/PermissionRequest hook 也要回到当前飞书聊天，
                # 因此前台启动脚本要把 chat_id、配置路径和执行模式一起注入子进程环境。
                "$env:FEISHU_CLAUDE_BOT_CHAT_ID = $chatId",
                f"$env:FEISHU_CLAUDE_BOT_APPROVALS_PATH = '{self._ps_quote(self.config.approvals_path)}'",
                f"$env:FEISHU_CLAUDE_BOT_CONFIG_PATH = '{self._ps_quote(str(INTEGRATION_ROOT / 'config' / 'feishu_claude_bot.v2.json'))}'",
                "$env:FEISHU_CLAUDE_BOT_EXECUTION_MODE = 'foreground'",
                "$Host.UI.RawUI.WindowTitle = 'Claude Code Assistant Front Session'",
                "New-Item -ItemType Directory -Force -Path (Split-Path -Parent $transcriptPath) | Out-Null",
                # Start-Transcript 是 Windows Terminal/PowerShell 场景下最稳的前台输出落盘方式；
                # 状态查询会读取这个文件的尾部，补齐 Stop hook 丢失时的前台摘要。
                "Start-Transcript -LiteralPath $transcriptPath -Append | Out-Null",
                "Write-Host 'Claude Code foreground session started.' -ForegroundColor Cyan",
                "Write-Host ('Chat ID: ' + $chatId)",
                "Write-Host ('Workdir: ' + $workDir)",
                "Write-Host ('Command: ' + ($command -join ' '))",
                "Write-Host ''",
                "$exe = $command[0]",
                "$args = @()",
                "if ($command.Length -gt 1) { $args = $command[1..($command.Length - 1)] }",
                "& $exe @args",
                # 某些国内代理模型/旧版 Claude Code 不会稳定触发 Stop hook；
                # 这里在命令返回提示符后补一次本机兜底通知，避免飞书端一直卡在 foreground_busy。
                "$claudeExitCode = if ($null -ne $LASTEXITCODE) { [int]$LASTEXITCODE } else { 0 }",
                f"& '{self._ps_quote(python_path)}' '{self._ps_quote(str(DEFAULT_FOREGROUND_RETURN_HELPER_PATH))}' "
                f"--chat-id $chatId --cwd $workDir --started-at '{started_at}' --exit-code $claudeExitCode --window-pid $PID",
                "Write-Host ''",
                "Write-Host 'Claude foreground session returned to the window. You can continue manually or close this window.' -ForegroundColor Yellow",
                "try { Stop-Transcript | Out-Null } catch {}",
                "try { Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue } catch {}",
            ]
        )
        # Persisting the launcher script keeps quoting predictable and leaves a trace
        # for troubleshooting when a foreground session fails to open as expected.
        launcher_path.write_text(launcher_text, encoding="utf-8-sig")
        self.state.update_chat(
            chat_id,
            {
                # 前台窗口输出没有 stdout 管道，改由 PowerShell transcript 落盘供“状态”读取最近摘要。
                "foreground_transcript_path": str(transcript_path),
            },
            self.config.default_cwd,
        )
        return launcher_path

    def _send_command_to_foreground_window(self, pid: int, command_text: str) -> None:
        """Activate the managed foreground window and paste one command into it."""

        launcher_dir = self._get_temp_dir("launchers")
        send_script_path = launcher_dir / f"send-foreground-{pid}-{int(time.time())}.ps1"
        send_script = "\n".join(
            [
                "param(",
                "  [int]$TargetPid,",
                "  [string]$CommandText",
                ")",
                "$ErrorActionPreference = 'Stop'",
                "Add-Type -AssemblyName System.Windows.Forms",
                "Add-Type -AssemblyName System.Management",
                # PowerShell 7 的 Add-Type 不会稳定复用上一行加载的程序集；
                # 内联 C# 显式引用窗口定位依赖，避免前台发送脚本在 Process/ManagementObjectSearcher 处编译失败。
                "Add-Type -ReferencedAssemblies @('System.Management','System.Diagnostics.Process','System.ComponentModel.Primitives') -TypeDefinition @\"",
                "using System;",
                "using System.Diagnostics;",
                "using System.Text;",
                "using System.Runtime.InteropServices;",
                "public static class ForegroundClaudeWindow {",
                "  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);",
                "  [DllImport(\"user32.dll\")] public static extern bool EnumWindows(EnumWindowsProc enumProc, IntPtr lParam);",
                "  [DllImport(\"user32.dll\")] public static extern bool IsWindowVisible(IntPtr hWnd);",
                "  [DllImport(\"user32.dll\", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);",
                "  [DllImport(\"user32.dll\")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);",
                "  [DllImport(\"user32.dll\", EntryPoint=\"GetWindowThreadProcessId\")] public static extern uint GetWindowThreadId(IntPtr hWnd, IntPtr lpdwProcessId);",
                "  [DllImport(\"user32.dll\")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);",
                "  [DllImport(\"user32.dll\")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);",
                "  [DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr hWnd);",
                "  [DllImport(\"user32.dll\")] public static extern IntPtr GetForegroundWindow();",
                "  [DllImport(\"user32.dll\")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);",
                "  [DllImport(\"kernel32.dll\")] public static extern uint GetCurrentThreadId();",
                "  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }",
                "  public static bool IsUsableTerminalWindow(IntPtr hWnd, string processName) {",
                "    if (!IsWindowVisible(hWnd)) { return false; }",
                "    if (!(processName.Equals(\"WindowsTerminal\", StringComparison.OrdinalIgnoreCase) || processName.Equals(\"OpenConsole\", StringComparison.OrdinalIgnoreCase) || processName.Equals(\"pwsh\", StringComparison.OrdinalIgnoreCase) || processName.Equals(\"powershell\", StringComparison.OrdinalIgnoreCase))) { return false; }",
                "    RECT rect;",
                "    if (!GetWindowRect(hWnd, out rect)) { return false; }",
                "    return (rect.Right - rect.Left) > 100 && (rect.Bottom - rect.Top) > 100;",
                "  }",
                "  public static IntPtr FindClaudeTerminalWindow(string titlePart) {",
                "    IntPtr matched = IntPtr.Zero;",
                "    EnumWindows(delegate(IntPtr hWnd, IntPtr lParam) {",
                "      if (!IsWindowVisible(hWnd)) { return true; }",
                "      StringBuilder sb = new StringBuilder(512);",
                "      GetWindowText(hWnd, sb, sb.Capacity);",
                "      string title = sb.ToString();",
                "      if (String.IsNullOrWhiteSpace(title) || title.IndexOf(titlePart, StringComparison.OrdinalIgnoreCase) < 0) { return true; }",
                "      uint processId;",
                "      GetWindowThreadProcessId(hWnd, out processId);",
                "      string processName = \"\";",
                "      try { processName = Process.GetProcessById((int)processId).ProcessName; } catch { return true; }",
                "      if (processName.Equals(\"WindowsTerminal\", StringComparison.OrdinalIgnoreCase) || processName.Equals(\"OpenConsole\", StringComparison.OrdinalIgnoreCase)) {",
                "        matched = hWnd;",
                "        return false;",
                "      }",
                "      return true;",
                "    }, IntPtr.Zero);",
                "    return matched;",
                "  }",
                "  public static IntPtr FindWindowByTargetPid(int targetPid) {",
                "    IntPtr matched = IntPtr.Zero;",
                "    EnumWindows(delegate(IntPtr hWnd, IntPtr lParam) {",
                "      if (!IsWindowVisible(hWnd)) { return true; }",
                "      uint processId;",
                "      GetWindowThreadProcessId(hWnd, out processId);",
                "      if ((int)processId == targetPid) {",
                "        matched = hWnd;",
                "        return false;",
                "      }",
                "      return true;",
                "    }, IntPtr.Zero);",
                "    return matched;",
                "  }",
                "  public static IntPtr FindWindowByProcessTree(int childPid) {",
                "    int parentPid = 0;",
                "    try {",
                "      var mos = new System.Management.ManagementObjectSearcher(\"SELECT ParentProcessId FROM Win32_Process WHERE ProcessId=\" + childPid);",
                "      foreach (var mo in mos.Get()) { parentPid = Convert.ToInt32(mo[\"ParentProcessId\"]); }",
                "    } catch {}",
                "    if (parentPid > 0) {",
                "      IntPtr h = FindWindowByTargetPid(parentPid);",
                "      if (h != IntPtr.Zero) return h;",
                "    }",
                "    return FindWindowByTargetPid(childPid);",
                "  }",
                "  public static IntPtr FindSingleVisibleTerminalWindow() {",
                "    IntPtr matched = IntPtr.Zero;",
                "    int count = 0;",
                "    EnumWindows(delegate(IntPtr hWnd, IntPtr lParam) {",
                "      uint processId;",
                "      GetWindowThreadProcessId(hWnd, out processId);",
                "      string processName = \"\";",
                "      try { processName = Process.GetProcessById((int)processId).ProcessName; } catch { return true; }",
                "      if (IsUsableTerminalWindow(hWnd, processName)) { matched = hWnd; count += 1; }",
                "      return true;",
                "    }, IntPtr.Zero);",
                "    return count == 1 ? matched : IntPtr.Zero;",
                "  }",
                "  public static bool ActivateClaudeWindow(IntPtr hWnd) {",
                "    if (hWnd == IntPtr.Zero) { return false; }",
                "    ShowWindowAsync(hWnd, 9);",
                "    // SetForegroundWindow 仅在调用进程拥有前台权限时才生效；",
                "    // 用 AttachThreadInput 临时合并输入队列，绕过 Windows 前台锁定。",
                "    IntPtr fgHwnd = GetForegroundWindow();",
                "    if (fgHwnd != hWnd) {",
                "      uint fgTid = GetWindowThreadId(fgHwnd, IntPtr.Zero);",
                "      uint myTid = GetCurrentThreadId();",
                "      bool attached = false;",
                "      try {",
                "        if (fgTid != myTid) { attached = AttachThreadInput(myTid, fgTid, true); }",
                "        SetForegroundWindow(hWnd);",
                "      } finally {",
                "        if (attached) { AttachThreadInput(myTid, fgTid, false); }",
                "      }",
                "    } else {",
                "      SetForegroundWindow(hWnd);",
                "    }",
                "    // 最终验证：窗口是否已成为前台。",
                "    return GetForegroundWindow() == hWnd;",
                "  }",
                "}",
                "\"@",
                "$wshell = New-Object -ComObject WScript.Shell",
                # Windows Terminal 下直接按 PID AppActivate 不稳定；这里改为先找真实窗口句柄再拉前台，
                # 减少“窗口明明在，但飞书继续/前台继续偶发失败”的情况。
                "$windowHandle = [ForegroundClaudeWindow]::FindClaudeTerminalWindow('Claude Code Assistant Front Session')",
                "if ($windowHandle -eq [IntPtr]::Zero) { $windowHandle = [ForegroundClaudeWindow]::FindClaudeTerminalWindow('Claude Code') }",
                "if ($windowHandle -eq [IntPtr]::Zero) { $windowHandle = [ForegroundClaudeWindow]::FindWindowByProcessTree($TargetPid) }",
                # Windows Terminal 手动窗口可能只有任务标题，且与 pwsh/claude 进程树脱钩；仅唯一可见终端时兜底激活。
                "if ($windowHandle -eq [IntPtr]::Zero) { $windowHandle = [ForegroundClaudeWindow]::FindSingleVisibleTerminalWindow() }",
                "if ($windowHandle -eq [IntPtr]::Zero) { throw '没有找到可激活的 Claude Code 终端窗口。' }",
                "$activated = [ForegroundClaudeWindow]::ActivateClaudeWindow($windowHandle)",
                "if (-not $activated) { $activated = $wshell.AppActivate('Claude Code') }",
                "if (-not $activated) { throw 'Claude Code 终端窗口无法激活。' }",
                "Start-Sleep -Milliseconds 250",
                # Windows Terminal 在不同配置下可能只接受 Ctrl+V；此前 Ctrl+Shift+V 会出现
                # “脚本返回成功但 Claude 输入框仍为空”的假成功，因此改用更通用的 Ctrl+V。
                "$previousClipboard = $null",
                "try { $previousClipboard = Get-Clipboard -Raw -ErrorAction SilentlyContinue } catch {}",
                # 命令文本通过参数进入脚本，避免用户指令里出现 here-string 边界字符时破坏脚本语法。
                "Set-Clipboard -Value $CommandText",
                "Start-Sleep -Milliseconds 250",
                "$wshell.SendKeys('^v')",
                "Start-Sleep -Milliseconds 250",
                "$wshell.SendKeys('{ENTER}')",
                "Start-Sleep -Milliseconds 350",
                # 还原剪贴板，避免远程发送命令后把用户本机剪贴板长期污染成“继续”。
                "if ($null -ne $previousClipboard) { try { Set-Clipboard -Value $previousClipboard } catch {} }",
            ]
        )
        # 这里也采用临时 ps1 + -File，和截图脚本保持一致，规避 -Command 解析 here-string 的兼容坑。
        send_script_path.write_text(send_script, encoding="utf-8-sig")
        result = subprocess.run(
            [
                self.config.pwsh_path,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(send_script_path),
                "-TargetPid",
                str(pid),
                "-CommandText",
                command_text,
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        try:
            send_script_path.unlink(missing_ok=True)
        except OSError as exc:
            self.log(f"cleanup foreground send script failed path={send_script_path} error={exc}")
        if result.returncode != 0:
            # PowerShell 失败文本常带 ANSI 颜色码，进入飞书前先清理，避免手机端看到控制字符。
            raise RuntimeError(self._summarize_result(result.stderr or result.stdout or "发送到前台窗口失败", 800))

    def _has_foreground_watch(self, chat_id: str, pid: int) -> bool:
        """Check whether a detached watcher is already bound to this chat and foreground window."""

        command = [
            self.config.pwsh_path,
            "-NoProfile",
            "-Command",
            (
                "Get-CimInstance Win32_Process | "
                "Where-Object { "
                "$_.Name -match '^python(\\.exe)?$' -and "
                f"([string]$_.CommandLine).Contains('feishu_claude_foreground_watch.py') -and "
                f"([string]$_.CommandLine).Contains('--chat-id {self._ps_quote(chat_id)}') -and "
                f"([string]$_.CommandLine).Contains('--window-pid {int(pid)}') "
                "} | Select-Object -First 1 -ExpandProperty ProcessId"
            ),
        ]
        result = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        return bool((result.stdout or "").strip())

    def _start_foreground_watch(self, chat_id: str, cwd: str, pid: int) -> None:
        """Start one persistent watcher for the managed foreground window if none is currently attached."""

        watch_key = f"{chat_id}:{pid}"
        if watch_key in self._foreground_watch_spawned:
            return
        python_path = self.config.python_path or sys.executable
        bootstrap_path = INTEGRATION_ROOT / "app" / "bootstrap_feishu_tool.py"
        if self._has_foreground_watch(chat_id, pid):
            self._foreground_watch_spawned.add(watch_key)
            return
        # 复用现有前台窗口时没有像“新开前台执行”那样的 launcher 收尾点；
        # 这里改成长期会话观察器，盯住 Claude 子进程每一轮的起落；
        # 这样无论是飞书送命令，还是用户直接在前台窗口手工继续，都能补齐完成通知。
        subprocess.Popen(
            [
                python_path,
                str(bootstrap_path),
                str(DEFAULT_FOREGROUND_WATCH_HELPER_PATH),
                "--chat-id",
                chat_id,
                "--cwd",
                cwd,
                "--window-pid",
                str(pid),
            ],
            cwd=str(INTEGRATION_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # 前台观察器是后台通知辅助进程，不能因为飞书发送完成通知而弹出空白终端窗口。
            creationflags=(
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            ),
        )
        self._foreground_watch_spawned.add(watch_key)

    def _send_hotkey_to_foreground_window(self, pid: int, hotkey: str) -> None:
        """Send one supported hotkey to the managed foreground Claude window."""

        hotkey_key = (hotkey or "").strip().lower()
        hotkey_map = {
            "shift+tab": "+{TAB}",
            "shift-tab": "+{TAB}",
            "tab": "{TAB}",
            "esc": "{ESC}",
            "enter": "{ENTER}",
            "ctrl+c": "^(c)",
            "ctrl+l": "^(l)",
        }
        send_keys = hotkey_map.get(hotkey_key)
        if not send_keys:
            raise RuntimeError("暂不支持该前台按键。当前支持：shift+tab、tab、esc、enter、ctrl+c、ctrl+l")

        launcher_dir = self._get_temp_dir("launchers")
        send_script_path = launcher_dir / f"send-hotkey-{pid}-{int(time.time())}.ps1"
        send_script = "\n".join(
            [
                "param(",
                "  [int]$TargetPid,",
                "  [string]$SendKeysText",
                ")",
                "$ErrorActionPreference = 'Stop'",
                "Add-Type -AssemblyName System.Windows.Forms",
                "Add-Type -AssemblyName System.Management",
                # 热键发送脚本与文本发送脚本复用同一段窗口定位 C#，同样需要显式编译引用。
                "Add-Type -ReferencedAssemblies @('System.Management','System.Diagnostics.Process','System.ComponentModel.Primitives') -TypeDefinition @\"",
                "using System;",
                "using System.Diagnostics;",
                "using System.Text;",
                "using System.Runtime.InteropServices;",
                "public static class ForegroundClaudeWindow {",
                "  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);",
                "  [DllImport(\"user32.dll\")] public static extern bool EnumWindows(EnumWindowsProc enumProc, IntPtr lParam);",
                "  [DllImport(\"user32.dll\")] public static extern bool IsWindowVisible(IntPtr hWnd);",
                "  [DllImport(\"user32.dll\", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);",
                "  [DllImport(\"user32.dll\")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);",
                "  [DllImport(\"user32.dll\", EntryPoint=\"GetWindowThreadProcessId\")] public static extern uint GetWindowThreadId(IntPtr hWnd, IntPtr lpdwProcessId);",
                "  [DllImport(\"user32.dll\")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);",
                "  [DllImport(\"user32.dll\")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);",
                "  [DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr hWnd);",
                "  [DllImport(\"user32.dll\")] public static extern IntPtr GetForegroundWindow();",
                "  [DllImport(\"user32.dll\")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);",
                "  [DllImport(\"kernel32.dll\")] public static extern uint GetCurrentThreadId();",
                "  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }",
                "  public static bool IsUsableTerminalWindow(IntPtr hWnd, string processName) {",
                "    if (!IsWindowVisible(hWnd)) { return false; }",
                "    if (!(processName.Equals(\"WindowsTerminal\", StringComparison.OrdinalIgnoreCase) || processName.Equals(\"OpenConsole\", StringComparison.OrdinalIgnoreCase) || processName.Equals(\"pwsh\", StringComparison.OrdinalIgnoreCase) || processName.Equals(\"powershell\", StringComparison.OrdinalIgnoreCase))) { return false; }",
                "    RECT rect;",
                "    if (!GetWindowRect(hWnd, out rect)) { return false; }",
                "    return (rect.Right - rect.Left) > 100 && (rect.Bottom - rect.Top) > 100;",
                "  }",
                "  public static IntPtr FindClaudeTerminalWindow(string titlePart) {",
                "    IntPtr matched = IntPtr.Zero;",
                "    EnumWindows(delegate(IntPtr hWnd, IntPtr lParam) {",
                "      if (!IsWindowVisible(hWnd)) { return true; }",
                "      StringBuilder sb = new StringBuilder(512);",
                "      GetWindowText(hWnd, sb, sb.Capacity);",
                "      string title = sb.ToString();",
                "      if (String.IsNullOrWhiteSpace(title) || title.IndexOf(titlePart, StringComparison.OrdinalIgnoreCase) < 0) { return true; }",
                "      uint processId;",
                "      GetWindowThreadProcessId(hWnd, out processId);",
                "      string processName = \"\";",
                "      try { processName = Process.GetProcessById((int)processId).ProcessName; } catch { return true; }",
                "      if (processName.Equals(\"WindowsTerminal\", StringComparison.OrdinalIgnoreCase) || processName.Equals(\"OpenConsole\", StringComparison.OrdinalIgnoreCase)) {",
                "        matched = hWnd;",
                "        return false;",
                "      }",
                "      return true;",
                "    }, IntPtr.Zero);",
                "    return matched;",
                "  }",
                "  public static IntPtr FindWindowByTargetPid(int targetPid) {",
                "    IntPtr matched = IntPtr.Zero;",
                "    EnumWindows(delegate(IntPtr hWnd, IntPtr lParam) {",
                "      if (!IsWindowVisible(hWnd)) { return true; }",
                "      uint processId;",
                "      GetWindowThreadProcessId(hWnd, out processId);",
                "      if ((int)processId == targetPid) {",
                "        matched = hWnd;",
                "        return false;",
                "      }",
                "      return true;",
                "    }, IntPtr.Zero);",
                "    return matched;",
                "  }",
                "  public static IntPtr FindWindowByProcessTree(int childPid) {",
                "    int parentPid = 0;",
                "    try {",
                "      var mos = new System.Management.ManagementObjectSearcher(\"SELECT ParentProcessId FROM Win32_Process WHERE ProcessId=\" + childPid);",
                "      foreach (var mo in mos.Get()) { parentPid = Convert.ToInt32(mo[\"ParentProcessId\"]); }",
                "    } catch {}",
                "    if (parentPid > 0) {",
                "      IntPtr h = FindWindowByTargetPid(parentPid);",
                "      if (h != IntPtr.Zero) return h;",
                "    }",
                "    return FindWindowByTargetPid(childPid);",
                "  }",
                "  public static IntPtr FindSingleVisibleTerminalWindow() {",
                "    IntPtr matched = IntPtr.Zero;",
                "    int count = 0;",
                "    EnumWindows(delegate(IntPtr hWnd, IntPtr lParam) {",
                "      uint processId;",
                "      GetWindowThreadProcessId(hWnd, out processId);",
                "      string processName = \"\";",
                "      try { processName = Process.GetProcessById((int)processId).ProcessName; } catch { return true; }",
                "      if (IsUsableTerminalWindow(hWnd, processName)) { matched = hWnd; count += 1; }",
                "      return true;",
                "    }, IntPtr.Zero);",
                "    return count == 1 ? matched : IntPtr.Zero;",
                "  }",
                "  public static bool ActivateClaudeWindow(IntPtr hWnd) {",
                "    if (hWnd == IntPtr.Zero) { return false; }",
                "    ShowWindowAsync(hWnd, 9);",
                "    // SetForegroundWindow 仅在调用进程拥有前台权限时才生效；",
                "    // 用 AttachThreadInput 临时合并输入队列，绕过 Windows 前台锁定。",
                "    IntPtr fgHwnd = GetForegroundWindow();",
                "    if (fgHwnd != hWnd) {",
                "      uint fgTid = GetWindowThreadId(fgHwnd, IntPtr.Zero);",
                "      uint myTid = GetCurrentThreadId();",
                "      bool attached = false;",
                "      try {",
                "        if (fgTid != myTid) { attached = AttachThreadInput(myTid, fgTid, true); }",
                "        SetForegroundWindow(hWnd);",
                "      } finally {",
                "        if (attached) { AttachThreadInput(myTid, fgTid, false); }",
                "      }",
                "    } else {",
                "      SetForegroundWindow(hWnd);",
                "    }",
                "    // 最终验证：窗口是否已成为前台。",
                "    return GetForegroundWindow() == hWnd;",
                "  }",
                "}",
                "\"@",
                "$wshell = New-Object -ComObject WScript.Shell",
                # 热键注入与文本注入保持同一套“窗口句柄拉前台”逻辑，减少 Windows Terminal 下的激活失败。
                "$activated = $false",
                "$windowHandle = [ForegroundClaudeWindow]::FindClaudeTerminalWindow('Claude Code Assistant Front Session')",
                "if ($windowHandle -eq [IntPtr]::Zero) { $windowHandle = [ForegroundClaudeWindow]::FindClaudeTerminalWindow('Claude Code') }",
                "if ($windowHandle -eq [IntPtr]::Zero) { $windowHandle = [ForegroundClaudeWindow]::FindWindowByProcessTree($TargetPid) }",
                # Windows Terminal 手动窗口可能只有任务标题，且与 pwsh/claude 进程树脱钩；仅唯一可见终端时兜底激活。
                "if ($windowHandle -eq [IntPtr]::Zero) { $windowHandle = [ForegroundClaudeWindow]::FindSingleVisibleTerminalWindow() }",
                "if ($windowHandle -ne [IntPtr]::Zero -and [ForegroundClaudeWindow]::ActivateClaudeWindow($windowHandle)) { $activated = $true }",
                "if (-not $activated) {",
                "  if (-not $wshell.AppActivate('Claude Code')) {",
                "    if (-not $wshell.AppActivate($TargetPid)) { throw 'Claude Code 终端窗口无法激活。' }",
                "  }",
                "}",
                "Start-Sleep -Milliseconds 250",
                "[System.Windows.Forms.SendKeys]::SendWait($SendKeysText)",
            ]
        )
        # 前台快捷键是对已打开窗口的直接接管动作，单独落成脚本便于定位激活失败或键值映射错误。
        send_script_path.write_text(send_script, encoding="utf-8-sig")
        result = subprocess.run(
            [
                self.config.pwsh_path,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(send_script_path),
                "-TargetPid",
                str(pid),
                "-SendKeysText",
                send_keys,
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        try:
            send_script_path.unlink(missing_ok=True)
        except OSError as exc:
            self.log(f"cleanup foreground hotkey script failed path={send_script_path} error={exc}")
        if result.returncode != 0:
            raise RuntimeError(self._summarize_result(result.stderr or result.stdout or "发送前台快捷键失败", 500))

    def _resolve_cwd_input(self, raw_value: str) -> tuple[str | None, str | None]:
        """Resolve a user-supplied directory alias or absolute path."""

        normalized_value = raw_value.strip()
        if not normalized_value:
            return None, "请提供目录路径，例如：目录 D:\\code\\5a\\unimis-ry-cloud"

        # Alias mapping keeps mobile commands short while still routing Claude to
        # the intended project root configured on this machine.
        mapped_value = self.config.cwd_aliases.get(normalized_value, normalized_value)
        if not Path(mapped_value).exists():
            return None, f"目录不存在：{normalized_value}"
        return mapped_value, None

    def _load_approvals_state(self) -> dict[str, Any]:
        """Read the shared approval state used by the PermissionRequest hook."""

        approvals_path = Path(self.config.approvals_path)
        if approvals_path.exists():
            return json.loads(approvals_path.read_text(encoding="utf-8"))
        return {"requests": {}}

    def _save_approvals_state(self, state: dict[str, Any]) -> None:
        """Persist approval decisions for the waiting PermissionRequest hook."""

        approvals_path = Path(self.config.approvals_path)
        approvals_path.parent.mkdir(parents=True, exist_ok=True)
        approvals_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _find_pending_approvals(self, chat_id: str) -> list[tuple[str, dict[str, Any]]]:
        """Return pending approval requests for one chat, newest first."""

        state = self._load_approvals_state()
        requests = state.get("requests", {})
        matches = [
            (request_id, request)
            for request_id, request in requests.items()
            if request.get("chat_id") == chat_id and request.get("status") == "pending"
        ]
        matches.sort(key=lambda item: float(item[1].get("created_at", 0)), reverse=True)
        return matches

    @staticmethod
    def _clip_approval_text(value: Any, limit: int = 220) -> str:
        """Clip approval detail text for mobile-friendly pending-item lists."""

        cleaned = str(value or "").strip()
        if not cleaned:
            return "-"
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[:limit].rstrip() + "\n...(内容已截断)"

    @staticmethod
    def _describe_bash_approval(command: Any, description: Any = "") -> tuple[str, str]:
        """Return a Chinese action label and risk hint for a Bash approval request."""

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
            # 飞书审批页需要把可能写文件/改 Git 状态的命令标出来，避免“同意”批量放行时看不出风险。
            risk = "风险：可能修改文件、移动/删除内容、安装依赖或改变 Git 状态，请确认命令范围。"
        elif any(re.search(pattern, normalized_command) for pattern in readonly_patterns):
            risk = "风险：只读查询，通常不会修改文件。"
        else:
            risk = "风险：会在本机执行 shell 命令，请确认目录和命令内容。"
        return action, risk

    def _get_approval_risk_hint(self, tool_name: str, tool_input: Any) -> str:
        """Return a Chinese risk hint for one approval request."""

        normalized_tool = tool_name.lower()
        if normalized_tool == "askuserquestion":
            # AskUserQuestion 本质是让 Claude 先征询下一步意见，飞书卡片只需要把问题和选项讲清楚，
            # 避免用户误以为这是文件写入类授权。
            return "风险：这是 Claude 发起的提问，不会直接修改文件。"
        if normalized_tool == "bash" and isinstance(tool_input, dict):
            _, risk = self._describe_bash_approval(tool_input.get("command"), tool_input.get("description"))
            return risk
        if normalized_tool in {"edit", "multiedit", "write", "notebookedit"}:
            return "风险：会修改文件内容，请确认文件路径和变更片段。"
        if normalized_tool in {"read", "glob", "grep", "ls"}:
            return "风险：只读查看，通常不会修改文件。"
        return "风险：需要 Claude 调用本机工具，请确认参数内容。"

    def _get_ask_user_question_action_label(self, tool_input: Any) -> str:
        """Return a short Chinese label for an AskUserQuestion request."""

        if not isinstance(tool_input, dict):
            return "用户提问"
        questions = tool_input.get("questions")
        if not isinstance(questions, list) or not questions:
            return "用户提问"
        first_question = questions[0]
        if not isinstance(first_question, dict):
            return "用户提问"
        header = str(first_question.get("header") or "").strip()
        if header:
            return header
        question_text = str(first_question.get("question") or "").strip()
        if question_text:
            return self._clip_approval_text(question_text, 48)
        return "用户提问"

    def _summarize_ask_user_question(self, tool_input: Any) -> list[str]:
        """Render AskUserQuestion payloads as a readable Chinese question card."""

        lines = [self._get_approval_risk_hint("AskUserQuestion", tool_input)]
        if not isinstance(tool_input, dict):
            lines.extend(["提问内容：", self._clip_approval_text(tool_input, 1000)])
            return lines

        questions = tool_input.get("questions")
        if not isinstance(questions, list) or not questions:
            lines.extend(["提问内容：", self._clip_approval_text(json.dumps(tool_input, ensure_ascii=False, indent=2), 1000)])
            return lines

        for question_index, question_item in enumerate(questions, start=1):
            if not isinstance(question_item, dict):
                lines.extend([f"问题 {question_index}：", self._clip_approval_text(question_item, 800)])
                continue
            header = str(question_item.get("header") or f"问题 {question_index}").strip()
            question_text = str(question_item.get("question") or "").strip()
            if len(questions) == 1:
                lines.append(f"主题：{header}")
                lines.append(f"问题：{question_text or '-'}")
            else:
                lines.append(f"问题 {question_index}：{header}")
                lines.append(f"问题：{question_text or '-'}")

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
            if question_index < len(questions):
                lines.append("")

        return lines

    def _summarize_approval_request(self, request: dict[str, Any], index: int | None = None) -> list[str]:
        """Render one pending approval as a Chinese numbered item."""

        tool_name = str(request.get("tool_name") or "-")
        tool_input = request.get("tool_input")
        prefix = f"{index}. " if index is not None else ""
        lines = [f"{prefix}{tool_name}"]
        cwd = str(request.get("cwd") or "").strip()
        if cwd:
            lines.append(f"目录：{cwd}")
        lines.append(f"动作：{self._get_approval_action_label(request)}")
        if tool_name.lower() == "askuserquestion":
            # 提问类授权的核心信息是“问题 + 选项”，直接复用专用渲染，避免清单页只显示原始 JSON。
            lines.extend(self._summarize_ask_user_question(tool_input))
            return lines
        lines.append(self._get_approval_risk_hint(tool_name, tool_input))
        if isinstance(tool_input, dict):
            file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
            if file_path:
                lines.append(f"文件：{file_path}")
            if tool_name.lower() in {"edit", "multiedit"}:
                # 待授权清单需要展示“改前/改后”，否则用户只能看到文件名，无法判断是否应授权。
                if "old_string" in tool_input:
                    lines.extend(["原内容：", self._clip_approval_text(tool_input.get("old_string"))])
                if "new_string" in tool_input:
                    lines.extend(["新内容：", self._clip_approval_text(tool_input.get("new_string"))])
            elif tool_name.lower() in {"write", "notebookedit"}:
                lines.extend(["写入内容预览：", self._clip_approval_text(tool_input.get("content") or tool_input.get("new_source"))])
            elif tool_name.lower() == "bash":
                # Bash 授权保留 Claude 卡片里的“命令正文”，同时补中文动作和风险，
                # 这样“授权”列表不再只是英文卡片，手机上也能判断是否该批量同意。
                lines.extend(["Bash 命令：", self._clip_approval_text(tool_input.get("command")), "执行方式：shell 命令"])
                if tool_input.get("description"):
                    lines.append(f"说明：{tool_input.get('description')}")
            else:
                lines.extend(["请求参数：", self._clip_approval_text(json.dumps(tool_input, ensure_ascii=False, indent=2))])
        else:
            lines.extend(["请求内容：", self._clip_approval_text(tool_input)])
        return lines

    def _get_approval_action_label(self, request: dict[str, Any]) -> str:
        """Return a short Chinese action label for one approval request."""

        tool_name = str(request.get("tool_name") or "-")
        tool_input = request.get("tool_input")
        if tool_name.lower() == "askuserquestion":
            return self._get_ask_user_question_action_label(tool_input)
        if isinstance(tool_input, dict):
            if tool_name.lower() == "bash":
                action, _ = self._describe_bash_approval(tool_input.get("command"), tool_input.get("description"))
                return action
            file_path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "").strip()
            if file_path:
                return f"{tool_name} {file_path}"
        return tool_name

    def _render_approval_choices(self, chat_id: str) -> list[str]:
        """Render explicit approval commands for the current pending list."""

        pending_matches = self._find_pending_approvals(chat_id)
        if not pending_matches:
            return []
        lines = [
            "可直接回复：",
            "同意 = 同意全部或当前推荐项",
            "拒绝 = 拒绝全部或当前问题",
            "同意 1 = 同意/选择第 1 条",
            "拒绝 1 = 拒绝第 1 条",
            "同意 1 3 = 同意/选择第 1、3 条",
            "同意 1,3 = 同意/选择第 1、3 条",
            "全部授权 = 同意全部",
            "授权 = 重新查看清单",
        ]
        if len(pending_matches) > 1:
            lines.append("全部拒绝 = 拒绝全部")
        lines.append("说明：列表里的编号统一使用，同意 1 / 拒绝 1 都表示处理第 1 条。")
        return lines

    def _send_pending_approvals(self, chat_id: str) -> None:
        """Send the current pending approval list with Chinese numeric choices."""

        pending_matches = self._find_pending_approvals(chat_id)
        if not pending_matches:
            # 用户常从“Claude 已暂停”兜底卡片点进来；无 pending 时明确区分暂停通知和授权卡片。
            self.send_text(chat_id, "当前没有待处理的授权请求。若刚才收到的是“Claude 已暂停”，它不是授权卡片，不需要回复“同意 1”。")
            return
        lines = [f"待授权项目：{len(pending_matches)} 条"]
        for index, (_, request) in enumerate(pending_matches, start=1):
            lines.extend(self._summarize_approval_request(request, index))
            lines.append("")
        lines.extend(self._render_approval_choices(chat_id))
        self.send_text(chat_id, "\n".join(line for line in lines if line is not None).strip())

    @staticmethod
    def _normalize_approval_request_id(value: str | None) -> str:
        """Normalize optional approval identifiers pasted from Feishu text."""

        # 仍兼容历史长编号，但新交互不再要求用户复制编号；这里去掉尖括号和多余空白，
        # 避免手机端把“同意 <编号>”里的符号一起传进来后匹配失败。
        return re.sub(r"^[<《【\\[]|[>》】\\]]$", "", (value or "").strip())

    @staticmethod
    def _parse_chinese_approval_index(value: str) -> int | None:
        """Parse Chinese-friendly approval item selectors into a 1-based index."""

        normalized = re.sub(r"[\s\u00a0\u200b\u200c\u200d\ufeff<>《》【】\\[\\]]+", "", value or "")
        normalized = normalized.replace("第", "").replace("项", "").replace("个", "").replace("条", "")
        aliases = {
            "一": 1,
            "第一": 1,
            "二": 2,
            "第二": 2,
            "三": 3,
            "第三": 3,
            "四": 4,
            "第四": 4,
            "五": 5,
            "第五": 5,
            "六": 6,
            "第六": 6,
            "七": 7,
            "第七": 7,
            "八": 8,
            "第八": 8,
            "九": 9,
            "第九": 9,
            "十": 10,
            "第十": 10,
        }
        if normalized.isdigit():
            return int(normalized)
        return aliases.get(normalized)

    def _parse_approval_indexes(self, value: str | None) -> list[int]:
        """Parse one or more Chinese-friendly approval indexes from Feishu text."""

        raw_value = value or ""
        # 多选允许空格、英文逗号、中文顿号/逗号、分号和斜杠混用，方便手机端输入。
        raw_parts = re.split(r"[\s\u00a0\u3000,，、;；/]+", raw_value.strip())
        indexes: list[int] = []
        for raw_part in raw_parts:
            if not raw_part:
                continue
            selected_index = self._parse_chinese_approval_index(raw_part)
            if selected_index is None:
                return []
            if selected_index not in indexes:
                indexes.append(selected_index)
        return indexes

    def _build_ask_user_question_updated_input(
        self,
        tool_input: Any,
        option_indexes: list[int],
    ) -> tuple[dict[str, Any] | None, str, str]:
        """Convert selected option numbers into AskUserQuestion updatedInput answers."""

        if not isinstance(tool_input, dict):
            return None, "", "这条提问没有可解析的问题参数，无法按选项作答。"
        questions = tool_input.get("questions")
        if not isinstance(questions, list) or not questions:
            return None, "", "这条提问没有问题列表，无法按选项作答。"
        if not option_indexes:
            return None, "", "请回复选项序号，例如“同意 1”或“同意 1 3”。"

        updated_input = dict(tool_input)
        answers: dict[str, str] = dict(tool_input.get("answers") or {})
        handled_lines: list[str] = []
        for question_index, question_item in enumerate(questions, start=1):
            if not isinstance(question_item, dict):
                return None, "", f"第 {question_index} 个问题格式异常，无法作答。"
            options = question_item.get("options")
            if not isinstance(options, list) or not options:
                return None, "", f"第 {question_index} 个问题没有可选方案。"
            multi_select = bool(question_item.get("multiSelect"))
            if len(questions) == 1:
                selected_indexes = option_indexes
            else:
                if question_index > len(option_indexes):
                    return None, "", f"共有 {len(questions)} 个问题，请为每个问题都提供选项序号，例如“同意 1 2”。"
                selected_indexes = [option_indexes[question_index - 1]]
            if not multi_select and len(selected_indexes) > 1 and len(questions) == 1:
                return None, "", "这是单选问题，请只回复一个选项序号，例如“同意 1”。"

            answer_labels: list[str] = []
            for selected_index in selected_indexes:
                if selected_index < 1 or selected_index > len(options):
                    return None, "", f"第 {question_index} 个问题没有第 {selected_index} 个选项。"
                selected_option = options[selected_index - 1]
                if isinstance(selected_option, dict):
                    answer_labels.append(str(selected_option.get("label") or f"选项 {selected_index}").strip())
                else:
                    answer_labels.append(str(selected_option).strip())

            question_text = str(question_item.get("question") or question_item.get("header") or f"问题 {question_index}").strip()
            # Claude 的 AskUserQuestion 输出约定是“问题文本 -> 答案字符串”；多选用逗号拼接。
            answers[question_text] = ", ".join(answer_labels)
            handled_lines.append(f"问题 {question_index}：{answers[question_text]}")

        updated_input["answers"] = answers
        return updated_input, "\n".join(handled_lines), ""

    def _try_handle_ask_user_question_selection(
        self,
        chat_id: str,
        decision: str,
        request_ids_text: str,
    ) -> bool:
        """Handle Feishu option replies for AskUserQuestion pending requests."""

        if decision != "同意":
            return False
        indexes = self._parse_approval_indexes(request_ids_text)
        if not indexes:
            return False

        state = self._load_approvals_state()
        pending_matches = self._find_pending_approvals(chat_id)
        ask_matches = [
            (request_id, request)
            for request_id, request in pending_matches
            if str(request.get("tool_name") or "").lower() == "askuserquestion"
        ]
        if not ask_matches:
            return False

        selected_request_id = ""
        selected_request: dict[str, Any] | None = None
        selected_option_indexes = indexes
        if len(indexes) >= 2:
            approval_index = indexes[0]
            if 1 <= approval_index <= len(pending_matches):
                candidate_id, candidate_request = pending_matches[approval_index - 1]
                if str(candidate_request.get("tool_name") or "").lower() == "askuserquestion":
                    # 多待办清单里允许“同意 5 1”表达“第 5 条提问选择第 1 个方案”。
                    # 这条优先于单个提问快捷语义，避免列表页选择跑偏。
                    selected_request_id = candidate_id
                    selected_request = candidate_request
                    selected_option_indexes = indexes[1:]
        if not selected_request and len(ask_matches) == 1:
            # 只有一个提问时，“同意 1”就是选择第 1 个方案；这是手机端最自然的用法。
            selected_request_id, selected_request = ask_matches[0]
        if not selected_request:
            return False

        updated_input, handled_text, error = self._build_ask_user_question_updated_input(
            selected_request.get("tool_input"),
            selected_option_indexes,
        )
        if error:
            self.send_text(chat_id, error)
            return True
        selected_request["updated_input"] = updated_input
        self._apply_approval_decision(selected_request, decision)
        self._save_approvals_state(state)
        remaining_count = len(self._find_pending_approvals(chat_id))
        suffix = f"\n剩余待授权：{remaining_count} 条，可回复“授权”查看清单后继续处理。" if remaining_count else ""
        self.send_text(
            chat_id,
            "\n".join(
                [
                    "已选择提问方案并同意继续。",
                    handled_text,
                    suffix.strip(),
                ]
            ).strip(),
        )
        return bool(selected_request_id)

    def _apply_default_ask_user_question_answer(self, request: dict[str, Any]) -> str:
        """Fill AskUserQuestion answers with the first option when user replies plain agree."""

        if str(request.get("tool_name") or "").lower() != "askuserquestion":
            return ""
        tool_input = request.get("tool_input")
        if not isinstance(tool_input, dict):
            return ""
        questions = tool_input.get("questions")
        if not isinstance(questions, list) or not questions:
            return ""
        default_indexes = [1 for _ in questions]
        updated_input, handled_text, error = self._build_ask_user_question_updated_input(tool_input, default_indexes)
        if error or not updated_input:
            return ""
        # 单独回复“同意”按帮助里的“当前推荐项”处理；Claude 生成的问题通常把推荐项放在第一项。
        request["updated_input"] = updated_input
        return handled_text

    def _build_foreground_question_answer_text(
        self,
        pending_question: dict[str, Any],
        option_indexes: list[int],
    ) -> tuple[str, str]:
        """Convert foreground AskUserQuestion option numbers into text sent to Claude.

        Args:
            pending_question: Question state persisted by the foreground watcher.
            option_indexes: 1-based option indexes parsed from Feishu text.

        Returns:
            A tuple of answer text and error text. Exactly one item is non-empty.
        """

        tool_input = pending_question.get("tool_input")
        if not isinstance(tool_input, dict):
            return "", "当前暂停问题没有可解析的参数，请直接回复文字指令。"
        questions = tool_input.get("questions")
        if not isinstance(questions, list) or not questions:
            return "", "当前暂停问题没有问题列表，请直接回复文字指令。"
        if not option_indexes:
            return "", "请回复选项序号，例如：1，或：同意 1。"
        if len(questions) == 1:
            question_item = questions[0]
            if not isinstance(question_item, dict):
                return "", "当前暂停问题格式异常，请直接回复文字指令。"
            options = question_item.get("options")
            if not isinstance(options, list) or not options:
                return "", "当前暂停问题没有可选方案，请直接回复文字指令。"
            multi_select = bool(question_item.get("multiSelect"))
            if not multi_select and len(option_indexes) > 1:
                return "", "这是单选问题，请只回复一个选项序号，例如：1。"
            selected_labels: list[str] = []
            for selected_index in option_indexes:
                if selected_index < 1 or selected_index > len(options):
                    return "", f"当前暂停问题没有第 {selected_index} 个选项。"
                option = options[selected_index - 1]
                if isinstance(option, dict):
                    selected_labels.append(str(option.get("label") or f"选项 {selected_index}").strip())
                else:
                    selected_labels.append(str(option).strip())
            # 前台窗口没有 PermissionRequest 回调可写 updatedInput，只能把用户选中的自然语言答案送回 Claude 输入框。
            return ", ".join(label for label in selected_labels if label), ""

        updated_input, _, error = self._build_ask_user_question_updated_input(tool_input, option_indexes)
        if error:
            return "", error
        answers = updated_input.get("answers") if isinstance(updated_input, dict) else None
        if not isinstance(answers, dict) or not answers:
            return "", "没有解析到可发送的选项答案，请直接回复文字指令。"
        # 多问题场景用“问题：答案”逐行发送，保留上下文，避免 Claude 把多个答案顺序理解错。
        return "\n".join(f"{question}: {answer}" for question, answer in answers.items()), ""

    def _handle_foreground_question_reply(self, chat_id: str, raw_text: str) -> bool:
        """Route Feishu option-number replies to the current foreground AskUserQuestion.

        Args:
            chat_id: Feishu chat id.
            raw_text: User message after normalization.

        Returns:
            True when the message was consumed as a foreground-question reply.
        """

        chat_state = self._refresh_chat_runtime_state(chat_id)
        pending_question = chat_state.get("foreground_pending_question")
        if not isinstance(pending_question, dict) or not pending_question.get("tool_input"):
            return False

        text = (raw_text or "").strip()
        option_indexes: list[int] = []
        if text == "同意":
            # 暂停问题里的“同意”不是授权动作；没有 hook 授权队列时按推荐的第一个选项继续。
            option_indexes = [1]
        elif text.startswith("同意 "):
            option_indexes = self._parse_approval_indexes(text[3:].strip())
        else:
            option_indexes = self._parse_approval_indexes(text)
        if not option_indexes:
            return False

        answer_text, error = self._build_foreground_question_answer_text(pending_question, option_indexes)
        if error:
            self.send_text(chat_id, error)
            return True
        if not answer_text:
            self.send_text(chat_id, "没有解析到可发送的选项答案，请直接回复文字指令。")
            return True
        self._send_text_to_active_window_or_prompt(chat_id, answer_text)
        return True

    def _resolve_approval_request(
        self,
        requests: dict[str, Any],
        chat_id: str,
        request_id: str | None,
    ) -> tuple[str | None, dict[str, Any] | None, str]:
        """Resolve a pending approval by optional full/partial id or newest request."""

        pending_matches = self._find_pending_approvals(chat_id)
        if not pending_matches:
            return None, None, ""
        normalized_id = self._normalize_approval_request_id(request_id)
        if not normalized_id:
            # 单独回复“同意/拒绝”视为处理列表第 1 项；文案里不再使用“最新”，避免列表顺序歧义。
            selected_id, selected_request = pending_matches[0]
            return selected_id, selected_request, ""
        selected_index = self._parse_chinese_approval_index(normalized_id)
        if selected_index is not None:
            if 1 <= selected_index <= len(pending_matches):
                selected_id, selected_request = pending_matches[selected_index - 1]
                return selected_id, selected_request, ""
            return None, None, f"没有第 {selected_index} 条待授权；请回复“授权”查看当前清单。"
        exact_request = requests.get(normalized_id)
        if exact_request and exact_request.get("chat_id") == chat_id:
            return normalized_id, exact_request, ""
        partial_matches = [
            (candidate_id, request)
            for candidate_id, request in pending_matches
            if candidate_id.endswith(normalized_id) or normalized_id in candidate_id
        ]
        if len(partial_matches) == 1:
            selected_id, selected_request = partial_matches[0]
            return selected_id, selected_request, ""
        if len(partial_matches) > 1:
            return None, None, "匹配到多条授权，请回复“授权”查看清单，再用“同意 1 / 拒绝 1”处理。"
        return None, None, "没有找到对应的待授权；请回复“授权”查看当前清单。"

    def _resolve_approval_requests_by_indexes(
        self,
        chat_id: str,
        indexes: list[int],
    ) -> tuple[list[tuple[int, str, dict[str, Any]]], str]:
        """Resolve multiple pending approval requests by 1-based list indexes."""

        pending_matches = self._find_pending_approvals(chat_id)
        if not pending_matches:
            return [], ""
        selected_items: list[tuple[int, str, dict[str, Any]]] = []
        missing_indexes: list[int] = []
        for selected_index in indexes:
            if 1 <= selected_index <= len(pending_matches):
                selected_id, selected_request = pending_matches[selected_index - 1]
                selected_items.append((selected_index, selected_id, selected_request))
            else:
                missing_indexes.append(selected_index)
        if missing_indexes:
            missing_text = "、".join(str(item) for item in missing_indexes)
            return [], f"没有第 {missing_text} 条待授权；请回复“授权”查看当前清单。"
        return selected_items, ""

    def _apply_approval_decision(self, request: dict[str, Any], decision: str) -> None:
        """Write one approval decision into the shared request object."""

        # PermissionRequest hook 会轮询这个状态字段；写成 approved/denied 后，前台 Claude 会继续或中断。
        request["status"] = "approved" if decision == "同意" else "denied"
        request["resolved_at"] = time.time()

    def _handle_all_approval_replies(self, chat_id: str, decision: str) -> bool:
        """Apply the same approval decision to every pending request in this chat."""

        state = self._load_approvals_state()
        pending_matches = self._find_pending_approvals(chat_id)
        if not pending_matches:
            self.send_text(chat_id, "当前没有待处理的授权请求。")
            return True
        handled_lines: list[str] = []
        for _, request in pending_matches:
            default_answer = self._apply_default_ask_user_question_answer(request) if decision == "同意" else ""
            if default_answer:
                handled_lines.append(default_answer)
            self._apply_approval_decision(request, decision)
        self._save_approvals_state(state)
        extra = "\n" + "\n".join(handled_lines) if handled_lines else ""
        self.send_text(chat_id, f"已{decision}全部待授权请求。\n数量：{len(pending_matches)}{extra}")
        return True

    def _handle_approval_replies_by_indexes(self, chat_id: str, decision: str, request_ids_text: str) -> bool:
        """Apply one decision to multiple pending approvals selected by list indexes."""

        indexes = self._parse_approval_indexes(request_ids_text)
        if len(indexes) <= 1:
            return False
        state = self._load_approvals_state()
        selected_items, resolve_error = self._resolve_approval_requests_by_indexes(chat_id, indexes)
        if not selected_items:
            self.send_text(chat_id, resolve_error or "当前没有待处理的授权请求。")
            return True
        handled_lines: list[str] = []
        for selected_index, _, request in selected_items:
            if request.get("status") != "pending":
                handled_lines.append(f"第 {selected_index} 项已是 {request.get('status')}，已跳过")
                continue
            self._apply_approval_decision(request, decision)
            handled_lines.append(f"第 {selected_index} 项：{request.get('tool_name') or '-'}")
        self._save_approvals_state(state)
        remaining_count = len(self._find_pending_approvals(chat_id))
        suffix = f"\n剩余待授权：{remaining_count} 条，可回复“授权”查看清单后继续处理。" if remaining_count else ""
        self.send_text(
            chat_id,
            "\n".join(
                [
                    f"已{decision} {len(selected_items)} 条授权请求",
                    *handled_lines,
                    suffix.strip(),
                ]
            ).strip(),
        )
        return True

    def _handle_approval_reply(self, chat_id: str, decision: str, request_id: str | None = None) -> bool:
        """Apply an approval/denial reply from Feishu to a waiting PermissionRequest hook."""

        if request_id and self._try_handle_ask_user_question_selection(chat_id, decision, request_id):
            return True
        if request_id and self._handle_approval_replies_by_indexes(chat_id, decision, request_id):
            return True

        state = self._load_approvals_state()
        requests = state.get("requests", {})
        selected_id, request, resolve_error = self._resolve_approval_request(requests, chat_id, request_id)
        if not request:
            chat_state = self._refresh_chat_runtime_state(chat_id)
            text_waiting_auth = self._is_auth_wait_text(
                "\n".join(
                    [
                        str(chat_state.get("last_result") or ""),
                        str(chat_state.get("last_error") or ""),
                    ]
                )
            )
            if not request_id and (chat_state.get("status") == "waiting_auth" or text_waiting_auth):
                if decision == "同意":
                    # Some Claude providers exit with a plain “需要授权”文本而不会挂起
                    # 原进程；这里转为托管续跑，保证飞书里的授权动作仍然能接上上下文。
                    self.state.update_chat(
                        chat_id,
                        {
                            # 旧版本可能已把授权等待误写成 done，这里统一恢复为可续跑的托管状态。
                            "status": "done",
                            "finished_at": time.time(),
                            "pending_action": "continue_session",
                            "pending_prompt": str(chat_state.get("pending_prompt") or "继续"),
                            "managed_session": True,
                        },
                        self.config.default_cwd,
                    )
                    self.send_text(
                        chat_id,
                        "已收到授权同意，准备在后台继续当前会话。原会话上下文会保留，本轮会自动续跑。",
                    )
                    prompt = str(chat_state.get("pending_prompt") or "继续")
                    self._queue_claude_task(chat_id, prompt, continue_mode=True, foreground=False)
                    return True

                chat_state = self.state.update_chat(
                    chat_id,
                    {
                        # 拒绝本次授权后不销毁托管会话，方便用户稍后继续或改走前台接管。
                        "status": "failed",
                        "finished_at": time.time(),
                        "last_error": "本次授权已在飞书中拒绝。当前会话仍保留，可回复“继续”或“前台继续”。",
                        "pending_action": "continue_session",
                        "pending_prompt": "继续",
                        "managed_session": True,
                    },
                    self.config.default_cwd,
                )
                self.send_text(
                    chat_id,
                    "\n".join(
                        [
                            "已拒绝本次授权",
                            f"目录：{chat_state.get('cwd')}",
                            f"结束时间：{self._format_ts(chat_state.get('finished_at'))}",
                            "说明：当前 Claude 会话未释放，你之后仍可回复“继续”或“前台继续”。",
                        ]
                    ),
                )
                return True

            self.send_text(chat_id, resolve_error or "当前没有待处理的授权请求。")
            return True
        if request.get("status") != "pending":
            self.send_text(chat_id, f"这条授权当前状态为 {request.get('status')}，无需重复处理。")
            return True

        self._apply_approval_decision(request, decision)
        self._save_approvals_state(state)
        tool_name = request.get("tool_name") or "-"
        selected_index = next(
            (
                index
                for index, (candidate_id, _) in enumerate(self._find_pending_approvals(chat_id), start=1)
                if candidate_id == selected_id
            ),
            1,
        )
        remaining_count = len(self._find_pending_approvals(chat_id))
        suffix = f"\n剩余待授权：{remaining_count} 条，可回复“授权”查看清单后继续处理。" if remaining_count else ""
        self.send_text(chat_id, f"已{decision}第 {selected_index} 项授权请求。\n工具：{tool_name}{suffix}")
        return True

    def _send_runtime_settings(self, chat_id: str) -> None:
        """Send current per-chat model and permission mode settings."""

        chat_state = self.state.get_chat(chat_id, self.config.default_cwd)
        permission_mode = chat_state.get("permission_mode") or "(跟随配置)"
        effective_permission = self._resolve_effective_permission_mode(False, chat_state)
        model = chat_state.get("model") or "(跟随配置)"
        effective_model = self._resolve_effective_model(chat_state) or "(Claude 默认)"
        lines = [
            f"当前授权模式：{permission_mode}",
            f"后台实际授权模式：{effective_permission}",
            f"当前模型：{model}",
            f"实际模型：{effective_model}",
            "",
            "可直接回复：",
            "权限 default",
            "权限 acceptEdits",
            "权限 跟随配置",
            "模型 opus",
            "模型 sonnet",
            "模型 跟随配置",
        ]
        self.send_text(chat_id, "\n".join(lines))

    def _set_permission_mode(self, chat_id: str, raw_mode: str) -> None:
        """Persist the permission mode override for this Feishu chat."""

        if raw_mode in {"", "跟随配置", "清空", "恢复默认配置"}:
            # 空值表示取消会话级覆盖，下一轮重新使用配置文件里的前台/后台默认值。
            updated_state = self.state.update_chat(chat_id, {"permission_mode": ""}, self.config.default_cwd)
            self.send_text(
                chat_id,
                f"已取消当前会话的授权模式覆盖。\n后台实际授权模式：{self._resolve_effective_permission_mode(False, updated_state)}",
            )
            return
        normalized = self._normalize_permission_mode(raw_mode)
        if not normalized:
            allowed = ", ".join(sorted(self.ALLOWED_PERMISSION_MODES))
            self.send_text(chat_id, f"不支持的授权模式：{raw_mode}\n可选值：{allowed}\n也可以回复：权限 跟随配置")
            return
        updated_state = self.state.update_chat(
            chat_id,
            {
                # 会话级权限模式只影响后续新启动的 Claude 进程，已打开的前台窗口不会被强制改写。
                "permission_mode": normalized,
                # Claude CLI 运行时没有对外暴露“热切换 permission mode”的接口；
                # 这里仅记录配置已变更，便于状态页明确提示当前前台窗口仍在跑旧参数。
                "runtime_settings_pending_restart": self._current_foreground_settings_stale(
                    {
                        **self.state.get_chat(chat_id, self.config.default_cwd),
                        "permission_mode": normalized,
                    }
                ),
            },
            self.config.default_cwd,
        )
        next_hint = "后台运行/继续会立即按新模式生效。"
        if updated_state.get("runtime_settings_pending_restart"):
            next_hint = "当前前台窗口仍在使用旧模式，且不会被热修改。你仍可继续操作这个窗口；新模式只会在下次新开 Claude 窗口时生效。"
        self.send_text(chat_id, "\n".join(["已切换当前会话授权模式", f"授权模式：{updated_state.get('permission_mode')}", f"说明：{next_hint}"]))

    def _set_model(self, chat_id: str, raw_model: str) -> None:
        """Persist the model override for this Feishu chat."""

        model = (raw_model or "").strip()
        if model in {"", "跟随配置", "清空", "恢复默认配置"}:
            # 空值表示取消会话级模型覆盖，下一轮重新使用配置文件或 Claude 自身默认模型。
            updated_state = self.state.update_chat(chat_id, {"model": ""}, self.config.default_cwd)
            self.send_text(
                chat_id,
                f"已取消当前会话的模型覆盖。\n实际模型：{self._resolve_effective_model(updated_state) or '(Claude 默认)'}",
            )
            return
        if re.search(r"\s", model):
            self.send_text(chat_id, "模型名不能包含空白字符。示例：模型 opus、模型 sonnet、模型 claude-sonnet-4-6")
            return
        updated_state = self.state.update_chat(
            chat_id,
            {
                # 模型覆盖按聊天保存，方便同一机器人在不同会话里使用不同模型。
                "model": model,
                # Claude 已启动进程的模型同样不能在原窗口内热切换，只能在后续新进程上应用。
                "runtime_settings_pending_restart": self._current_foreground_settings_stale(
                    {
                        **self.state.get_chat(chat_id, self.config.default_cwd),
                        "model": model,
                    }
                ),
            },
            self.config.default_cwd,
        )
        next_hint = "后台运行/继续会立即按新模型生效。"
        if updated_state.get("runtime_settings_pending_restart"):
            next_hint = "当前前台窗口仍在使用旧模型，且不会被热修改。你仍可继续操作这个窗口；新模型只会在下次新开 Claude 窗口时生效。"
        self.send_text(chat_id, "\n".join(["已切换当前会话模型", f"模型：{updated_state.get('model')}", f"说明：{next_hint}"]))

    def is_allowed_chat(self, chat_id: str) -> bool:
        """Limit bot access to configured chats when an allow-list is present."""

        if not self.config.allowed_chat_ids:
            return True
        return chat_id in self.config.allowed_chat_ids

    def run(self) -> None:
        """Start the Feishu long-connection listener and keep processing events."""

        self.log("bot starting")
        self._start_health_reporter()

        def handle_text_message(message: IncomingTextMessage) -> None:
            """Route one normalized Feishu text message from the gateway."""

            chat_id = message.chat_id
            if not self.is_allowed_chat(chat_id):
                # 白名单属于业务访问控制，Gateway 只负责把飞书消息交付到这里。
                self.log(f"blocked chat={chat_id} reason=allow_list")
                self.send_text(chat_id, "当前聊天未加入允许列表，请先在配置中添加 chat_id。")
                return
            if self._should_dedupe_message_event(message.message_id):
                # 这里按飞书原始 message_id 去重，避免同一条手机端消息在 SDK 重连后再次落入业务分支。
                self.log(f"dedupe message chat={chat_id} message_id={message.message_id} text={message.text}")
                return
            self.log(
                f"recv chat={chat_id} message_id={message.message_id} text={message.text} "
                f"raw_repr={message.text!r} content_repr={message.raw_content!r}"
            )
            if self._preprocess_control_command(chat_id, message.text):
                return
            self.handle_command(chat_id, message.text)

        def handle_non_text_message(chat_id: str, message_type: str) -> None:
            """Reply to unsupported Feishu message types after allow-list checks."""

            if not self.is_allowed_chat(chat_id):
                self.log(f"blocked chat={chat_id} reason=allow_list message_type={message_type}")
                self.send_text(chat_id, "当前聊天未加入允许列表，请先在配置中添加 chat_id。")
                return
            self.send_text(chat_id, "当前最小版仅支持文本消息。发送 /help 查看命令。")

        def handle_empty_text_message(chat_id: str) -> None:
            """Reply to empty Feishu text messages after allow-list checks."""

            if not self.is_allowed_chat(chat_id):
                self.log(f"blocked chat={chat_id} reason=allow_list empty_text=true")
                self.send_text(chat_id, "当前聊天未加入允许列表，请先在配置中添加 chat_id。")
                return
            self.send_text(chat_id, "收到空消息。发送 /help 查看命令。")

        # 飞书收消息和重连策略已收口到 FeishuGateway，bot 只处理业务路由回调。
        self.gateway.start_text_listener(handle_text_message, handle_non_text_message, handle_empty_text_message)

    def _normalize_incoming_text(self, text: str) -> str:
        """Normalize Feishu mobile text before any command routing decisions."""

        # 文本清洗归 CommandRouter 统一维护，避免入口预处理和通用路由行为不一致。
        return self.command_router.normalize_incoming_text(text)

    def _preprocess_control_command(self, chat_id: str, raw_text: str) -> bool:
        """Hard-intercept screenshot/model/permission commands before general routing."""

        intent = self.command_router.parse(raw_text)
        text = intent.text
        command_key = intent.key
        self.log(f"preprocess enter chat={chat_id} text={text!r} key={command_key!r}")

        # 截图类命令副作用很强，而且用户已经反馈过“一次截图执行两遍/跑进 Claude”；
        # 因此这里在通用 handle_command 之前先拦截，成功处理后直接返回 True。
        if intent.kind in {
            CommandKind.SCREENSHOT_DESKTOP,
            CommandKind.SCREENSHOT_CLAUDE,
            CommandKind.SCREENSHOT_INDEX,
            CommandKind.SCREENSHOT_HELP,
        }:
            if self._should_dedupe_control_command(chat_id, f"screenshot:{command_key}", window_seconds=30.0):
                self.log(f"dedupe screenshot chat={chat_id} key={command_key}")
                return True
            if intent.kind == CommandKind.SCREENSHOT_DESKTOP:
                self.log(f"preprocess screenshot kind=desktop chat={chat_id} text={text} key={command_key}")
                self.send_text(chat_id, "正在截取当前桌面主屏幕...")
                self._send_desktop_screenshot(chat_id)
                return True
            if intent.kind == CommandKind.SCREENSHOT_CLAUDE:
                self.log(f"preprocess screenshot kind=claude chat={chat_id} text={text} key={command_key}")
                chat_state = self._refresh_chat_runtime_state(chat_id)
                self.send_text(chat_id, "正在截取当前 Claude 前台窗口...")
                self._send_claude_screenshot(chat_id, chat_state)
                return True
            if intent.kind == CommandKind.SCREENSHOT_INDEX and intent.index is not None:
                self.log(
                    f"preprocess screenshot kind=index chat={chat_id} text={text} "
                    f"key={command_key} index={intent.index}"
                )
                chat_state = self._refresh_chat_runtime_state(chat_id)
                self._send_claude_screenshot_by_index(chat_id, chat_state, intent.index)
                return True
            self.log(f"preprocess screenshot fallback chat={chat_id} text={text} key={command_key}")
            self.send_text(chat_id, "截图命令请使用：截图 claude，或：截图 桌面。")
            return True

        if intent.kind == CommandKind.PERMISSION_MODE:
            self.log(f"preprocess permission chat={chat_id} text={text} key={command_key}")
            self._set_permission_mode(chat_id, intent.value)
            return True

        if intent.kind == CommandKind.MODEL:
            self.log(f"preprocess model chat={chat_id} text={text} key={command_key}")
            self._set_model(chat_id, intent.value)
            return True

        return False

    @staticmethod
    def _is_screenshot_control_key(command_key: str) -> bool:
        """Tell whether a normalized key is a bot screenshot control command."""

        return CommandRouter().is_screenshot_control(command_key)

    def handle_command(self, chat_id: str, text: str) -> None:
        """Parse one chat command and dispatch it to the appropriate handler."""

        # 统一复用入口层清洗逻辑，避免同一条飞书文本在不同函数里被不同方式处理。
        intent = self.command_router.parse(text)
        text = intent.text
        command_key = intent.key

        # 第一优先级：机器人控制命令。只要命中截图，就必须在进入 Claude 之前结束。
        if intent.kind in {
            CommandKind.SCREENSHOT_DESKTOP,
            CommandKind.SCREENSHOT_CLAUDE,
            CommandKind.SCREENSHOT_INDEX,
            CommandKind.SCREENSHOT_HELP,
        }:
            # 截图是机器人控制命令，绝不允许漏到“前台窗口自然语言”分支里被 Claude 当成用户任务执行。
            if intent.kind == CommandKind.SCREENSHOT_DESKTOP:
                self.log(f"screenshot command matched kind=desktop chat={chat_id} text={text} key={command_key}")
                self.send_text(chat_id, "正在截取当前桌面主屏幕...")
                self._send_desktop_screenshot(chat_id)
                return
            if intent.kind == CommandKind.SCREENSHOT_CLAUDE:
                self.log(f"screenshot command matched kind=claude chat={chat_id} text={text} key={command_key}")
                chat_state = self._refresh_chat_runtime_state(chat_id)
                self.send_text(chat_id, "正在截取当前 Claude 前台窗口...")
                self._send_claude_screenshot(chat_id, chat_state)
                return
            # "截图N" -- 截取指定索引的窗口。
            if intent.kind == CommandKind.SCREENSHOT_INDEX and intent.index is not None:
                self.log(f"screenshot command matched kind=index chat={chat_id} text={text} key={command_key} index={intent.index}")
                chat_state = self._refresh_chat_runtime_state(chat_id)
                self._send_claude_screenshot_by_index(chat_id, chat_state, intent.index)
                return
            # 单独“截图/截图窗口”等只给用法，不执行，也不能掉到 Claude 前台窗口。
            self.log(f"screenshot command fallback chat={chat_id} text={text} key={command_key}")
            self.send_text(chat_id, "截图命令请使用：截图 claude，或：截图 桌面。")
            return

        # 第二优先级：帮助、身份和纯查询命令。这类命令不应改变任务状态。
        if text == "/help":
            self.send_text(
                chat_id,
                "\n".join(
                    [
                        "可用命令：",
                        "帮助",
                        "我是谁",
                        "新窗口继续 [补充指令]",
                        "新窗口运行 <任务>",
                        "窗口列表",
                        "切换到窗口1",
                        "继续",
                        "直接发送任意文本：发到当前选中窗口",
                        "状态",
                        "",
                        "多会话管理：",
                        "新建会话 [标签]：创建并切换到新会话",
                        "会话列表：查看所有会话及状态",
                        "切换会话 <id>：切换到指定会话",
                        "关闭会话 [id]：关闭指定会话",
                        "",
                        "截图 claude",
                        "截图 1",
                        "截图 桌面",
                        "前台上一个",
                        "前台按键 shift+tab",
                        "停止",
                        "目录 <绝对路径>",
                        "权限 <模式>",
                        "模型 <模型>",
                        "",
                        "目录别名：",
                        "目录 5a",
                        "目录 主数据",
                        "目录 数据治理",
                        "目录 消息总线",
                        "",
                        "授权模式示例：",
                        "权限 default",
                        "权限 acceptEdits",
                        "权限 bypassPermissions",
                        "权限 跟随配置",
                        "说明：bypassPermissions 会为新开的 Claude 前台/后台进程追加危险跳过参数，只能在可信聊天和本机环境中使用。",
                        "",
                        "飞书授权回复：",
                        "授权：查看所有待授权项目（会展开问题和可选方案）",
                        "只有看到授权清单时，才需要回复“同意 1 / 拒绝 1”；普通暂停通知可直接回复下一步指令。",
                        "同意：同意全部或当前推荐项",
                        "拒绝：拒绝全部或当前问题",
                        "全部授权：同意全部待授权",
                        "同意 1：同意/选择第 1 条",
                        "拒绝 1：拒绝第 1 条",
                        "同意 1 3：同意/选择第 1、3 条",
                        "同意 1,3：同意/选择第 1、3 条",
                        "全部拒绝：拒绝全部待授权",
                        "",
                        "模型示例：",
                        "模型 opus",
                        "模型 sonnet",
                        "模型 haiku",
                        "模型 跟随配置",
                        "",
                        "默认行为：",
                        "未匹配到控制命令时，整条消息会直接发送到当前选中的实时 Claude 窗口。",
                        "如果没有选中窗口，会默认使用实时枚举到的第一个 Claude 窗口；也可以先发“切换到窗口1”。",
                        "“新窗口继续”会新开前台窗口并复用上一会话上下文；“新窗口运行”会新开前台窗口并使用新上下文。",
                        "后台运行命令仅保留兼容入口，日常使用建议直接走前台窗口。",
                    ]
                ),
            )
            return

        if text == "帮助":
            self.handle_command(chat_id, "/help")
            return

        if text == "/whoami":
            self.send_text(chat_id, f"当前聊天 ID：{chat_id}")
            return

        if text == "我是谁":
            self.send_text(chat_id, f"当前聊天 ID：{chat_id}")
            return

        # 第三优先级：会话配置。目录、权限、模型都会影响后续 Claude 的启动参数。
        if text in {"/permission", "权限", "授权模式", "权限模式"}:
            self._send_runtime_settings(chat_id)
            return

        for compact_prefix in ("权限", "授权模式", "权限模式", "切换授权模式"):
            if command_key.startswith(compact_prefix) and len(command_key) > len(compact_prefix):
                # 飞书菜单有时会把“权限 绕过”发成带零宽字符/特殊空格的文本；
                # 先用压平后的 command_key 解析，避免配置命令掉入自然语言兜底并启动 Claude。
                self._set_permission_mode(chat_id, command_key[len(compact_prefix):].strip())
                return

        if text.startswith("/permission "):
            self._set_permission_mode(chat_id, text[12:].strip())
            return

        for prefix in ("权限 ", "授权模式 ", "权限模式 ", "切换授权模式 "):
            if text.startswith(prefix):
                self._set_permission_mode(chat_id, text[len(prefix):].strip())
                return

        if text in {"/model", "模型", "模型模式"}:
            self._send_runtime_settings(chat_id)
            return

        for compact_prefix in ("模型", "切换模型", "模型模式"):
            if command_key.startswith(compact_prefix) and len(command_key) > len(compact_prefix):
                # 与授权模式一致，模型快捷词也要兼容飞书移动端混入的不可见空白。
                self._set_model(chat_id, command_key[len(compact_prefix):].strip())
                return

        if text.startswith("/model "):
            self._set_model(chat_id, text[7:].strip())
            return

        for prefix in ("模型 ", "切换模型 ", "模型模式 "):
            if text.startswith(prefix):
                self._set_model(chat_id, text[len(prefix):].strip())
                return

        if text == "同意":
            pending_matches = self._find_pending_approvals(chat_id)
            if pending_matches and self._handle_all_approval_replies(chat_id, "同意"):
                # 用户已明确约定“同意”就是全部授权；无 pending hook 时仍走旧的文本授权续跑兜底。
                return
            if self._handle_foreground_question_reply(chat_id, text):
                return
            if self._handle_approval_reply(chat_id, "同意"):
                return

        if text == "拒绝":
            pending_matches = self._find_pending_approvals(chat_id)
            if pending_matches and self._handle_all_approval_replies(chat_id, "拒绝"):
                # 与“同意=全部”保持对称；没有 pending hook 时仍走旧的文本拒绝续跑兜底。
                return
            if self._handle_approval_reply(chat_id, "拒绝"):
                return

        if text in {"全部同意", "同意全部", "全同意", "全部授权", "授权全部", "全授权", "同意所有", "全部允许", "全部统一", "统一全部"}:
            if self._handle_all_approval_replies(chat_id, "同意"):
                return

        if text in {"全部拒绝", "拒绝全部", "全拒绝"}:
            if self._handle_all_approval_replies(chat_id, "拒绝"):
                return

        if text in {"授权", "待授权", "授权列表", "授权清单", "查看授权"}:
            # 这里只是展开待授权队列给用户看，不修改任何授权结果。
            self._send_pending_approvals(chat_id)
            return

        if text.startswith("同意 "):
            approval_target = text[3:].strip()
            if approval_target in {"全部", "所有", "全量", "全部授权"} and self._handle_all_approval_replies(chat_id, "同意"):
                # 兼容“同意 全部”这类手机语音/联想输入，避免误当成授权编号查找。
                return
            pending_matches = self._find_pending_approvals(chat_id)
            if pending_matches and self._handle_approval_reply(chat_id, "同意", approval_target):
                return
            if self._handle_foreground_question_reply(chat_id, text):
                return
            if self._handle_approval_reply(chat_id, "同意", approval_target):
                return

        if text.startswith("拒绝 "):
            if self._handle_approval_reply(chat_id, "拒绝", text[3:].strip()):
                return

        if self._handle_foreground_question_reply(chat_id, text):
            return

        # 第四优先级：目录切换。它只改变后续 Claude 的工作目录，不会立即发起执行。
        # Single-word aliases from the mobile quick-reply bar should return guidance
        # instead of being forwarded as raw Claude tasks.
        if text in {"/cwd", "目录"}:
            self.send_text(chat_id, "请提供目录路径，例如：目录 D:\\code\\5a\\unimis-ry-cloud")
            return

        stopped_launch_commands = {"/run", "运行", "/fgrun", "前台运行", "/fgcontinue", "前台继续"}
        if (
            self._is_stopped_configuration_state(chat_id)
            and text not in stopped_launch_commands
            and not text.startswith(("新窗口继续", "/newcontinue", "新窗口运行", "/newrun"))
        ):
            self.send_text(
                chat_id,
                self._build_sectioned_message(
                    "当前处于已停止后的配置态",
                    [
                        "说明：普通文本不会自动启动 Claude，避免停止后误触继续执行。",
                        "如需重新接管本机 Claude，请使用显式启动命令。",
                    ],
                    ["运行", "前台运行", "新窗口继续", "状态"],
                ),
            )
            return

        # v2 日常入口改为窗口驱动；裸“运行”作为显式启动入口，不再把普通文本误判成后台任务。
        if text in {"/run", "运行"}:
            chat_state = self._refresh_chat_runtime_state(chat_id)
            foreground_pid = chat_state.get("foreground_pid") or chat_state.get("active_pid")
            if foreground_pid and self._process_exists(foreground_pid):
                # 用户点“运行”时若已有可接管窗口，只提示复用，避免重复新开前台 Claude。
                self.send_text(
                    chat_id,
                    self._build_sectioned_message(
                        "当前已有可接管前台窗口",
                        [
                            f"目录：{chat_state.get('cwd')}",
                            f"窗口进程：{foreground_pid}",
                            "说明：请直接发送任务文本，或回复“继续”把指令送入当前窗口。",
                        ],
                        ["继续", "窗口列表", "新窗口运行 <任务>", "状态"],
                    ),
                )
                return
            self._queue_claude_task(chat_id, "继续", continue_mode=True, foreground=True)
            return

        if text in {"/bgrun", "后台运行"}:
            self.send_text(chat_id, "后台运行已不作为主路径使用。请改用：新窗口运行 <任务>，或直接把内容发送到当前窗口。")
            return

        # “继续”是面向当前窗口的快捷输入，不能再走后台队列或旧 foreground_pid。
        if text in {"/continue", "继续"}:
            self._send_text_to_active_window_or_prompt(chat_id, "继续")
            return

        # 兼容旧按钮：前台运行/前台继续现在等价于新窗口运行/新窗口继续。
        if text in {"/fgrun", "前台运行"}:
            self._queue_claude_task(chat_id, "", continue_mode=False, foreground=True, open_foreground_only=True)
            return

        if text in {"/fgcontinue", "前台继续"}:
            # “新窗口继续”才负责强制重开；“前台继续”应复用当前可见窗口，避免用户刚接管的会话被新窗口打断。
            self._send_text_to_active_window_or_prompt(chat_id, "继续")
            return

        if text in {"/bgcontinue", "后台继续"}:
            self.send_text(chat_id, "后台继续已不作为主路径使用。请改用：继续，或：新窗口继续。")
            return

        if text in {"前台上一个", "前台 shift+tab", "前台 shift tab", "前台切回"}:
            if self._should_dedupe_control_command(chat_id, "hotkey:shift+tab"):
                self.log(f"dedupe foreground hotkey chat={chat_id} key=shift+tab")
                return
            chat_state = self._refresh_chat_runtime_state(chat_id)
            foreground_pid = chat_state.get("foreground_pid") or chat_state.get("active_pid")
            if not foreground_pid or not self._process_exists(foreground_pid):
                self.send_text(chat_id, "当前没有可接管的 Claude 前台窗口。请先回复“前台继续”或“前台运行”。")
                return
            try:
                # 热键发送保留主类包装入口，便于回归测试和后续 Windows 兼容补丁替换底层实现。
                self._send_hotkey_to_foreground_window(int(foreground_pid), "shift+tab")
                self.send_text(chat_id, "已向当前 Claude 前台窗口发送 Shift+Tab。")
            except Exception as exc:
                self.log(f"foreground hotkey failed chat={chat_id} pid={foreground_pid} key=shift+tab error={exc}")
                self.send_text(chat_id, f"前台快捷键发送失败：{self._summarize_result(str(exc), 220)}")
            return

        if text.startswith("前台按键 "):
            hotkey = text[5:].strip()
            if self._should_dedupe_control_command(chat_id, f"hotkey:{self._normalize_command_key(hotkey)}"):
                self.log(f"dedupe foreground hotkey chat={chat_id} key={hotkey}")
                return
            chat_state = self._refresh_chat_runtime_state(chat_id)
            foreground_pid = chat_state.get("foreground_pid") or chat_state.get("active_pid")
            if not foreground_pid or not self._process_exists(foreground_pid):
                self.send_text(chat_id, "当前没有可接管的 Claude 前台窗口。请先回复“前台继续”或“前台运行”。")
                return
            try:
                # 自定义热键同样走主类包装入口，保证测试桩和真实发送链路保持同一层边界。
                self._send_hotkey_to_foreground_window(int(foreground_pid), hotkey)
                self.send_text(chat_id, f"已向当前 Claude 前台窗口发送按键：{hotkey}")
            except Exception as exc:
                self.log(f"foreground hotkey failed chat={chat_id} pid={foreground_pid} key={hotkey} error={exc}")
                self.send_text(chat_id, f"前台快捷键发送失败：{self._summarize_result(str(exc), 220)}")
            return

        if text.startswith("/cwd "):
            new_cwd, error_text = self._resolve_cwd_input(text[5:])
            if error_text:
                self.send_text(chat_id, error_text)
                return
            # Directory switching affects where Claude sees files and session history.
            self.state.update_chat(chat_id, {"cwd": new_cwd}, self.config.default_cwd)
            self.send_text(chat_id, f"已切换工作目录：{new_cwd}")
            return

        if text.startswith("目录 "):
            new_cwd, error_text = self._resolve_cwd_input(text[3:])
            if error_text:
                self.send_text(chat_id, error_text)
                return
            # Directory switching affects where Claude sees files and session history.
            self.state.update_chat(chat_id, {"cwd": new_cwd}, self.config.default_cwd)
            self.send_text(chat_id, f"已切换工作目录：{new_cwd}")
            return

        # 多会话管理命令
        if text in {"/new", "新建会话", "新会话", "创建会话"}:
            session_id, session_state = self.state.create_session(chat_id, self.config.default_cwd)
            sessions = self.state.list_sessions(chat_id, self.config.default_cwd)
            self.send_text(
                chat_id,
                self._build_sectioned_message(
                    f"已创建新会话：{session_id}",
                    [
                        f"当前共 {len(sessions)} 个会话",
                        f"工作目录：{session_state.get('cwd', self.config.default_cwd)}",
                    ],
                    [f"运行 <任务>", f"切换会话 {session_id}", "会话列表"],
                ),
            )
            return

        if text.startswith("/new "):
            label = text[5:].strip()
            session_id, session_state = self.state.create_session(chat_id, self.config.default_cwd, label=label)
            sessions = self.state.list_sessions(chat_id, self.config.default_cwd)
            self.send_text(
                chat_id,
                self._build_sectioned_message(
                    f"已创建新会话：{session_id}（{label}）",
                    [
                        f"当前共 {len(sessions)} 个会话",
                        f"工作目录：{session_state.get('cwd', self.config.default_cwd)}",
                    ],
                    [f"运行 <任务>", f"切换会话 {session_id}", "会话列表"],
                ),
            )
            return

        for prefix in ("新建会话 ", "新会话 ", "创建会话 "):
            if text.startswith(prefix):
                label = text[len(prefix):].strip()
                session_id, session_state = self.state.create_session(chat_id, self.config.default_cwd, label=label)
                sessions = self.state.list_sessions(chat_id, self.config.default_cwd)
                self.send_text(
                    chat_id,
                    self._build_sectioned_message(
                        f"已创建新会话：{session_id}（{label}）",
                        [
                            f"当前共 {len(sessions)} 个会话",
                            f"工作目录：{session_state.get('cwd', self.config.default_cwd)}",
                        ],
                        [f"运行 <任务>", f"切换会话 {session_id}", "会话列表"],
                    ),
                )
                return

        if text in {"/sessions", "会话列表", "会话"}:
            sessions = self.state.list_sessions(chat_id, self.config.default_cwd)
            active_id = self.state.get_active_session_id(chat_id, self.config.default_cwd)
            if not sessions:
                self.send_text(chat_id, "当前没有活跃会话。发送新建会话创建一个。")
                return
            lines = [f"会话列表（共 {len(sessions)} 个，当前活跃：{active_id}）"]
            for sid, sstate in sessions:
                status = sstate.get("status", "idle")
                label = sstate.get("label", "")
                last_cmd = sstate.get("last_command", "")
                marker = " * " if sid == active_id else "   "
                display = f"{marker}{sid}"
                if label:
                    display += f"（{label}）"
                display += f" [{status}]"
                if last_cmd:
                    display += f" - {self._summarize_result(last_cmd, 40)}"
                lines.append(display)
            self.send_text(
                chat_id,
                self._build_sectioned_message(
                    lines[0],
                    lines[1:],
                    ["新建会话", "切换会话 <id>", "状态"],
                ),
            )
            return

        for prefix in ("/switch ", "切换会话 "):
            if text.startswith(prefix):
                target_id = text[len(prefix):].strip()
                if self.state.set_active_session(chat_id, target_id, self.config.default_cwd):
                    session = self.state.get_session(chat_id, target_id, self.config.default_cwd)
                    label = session.get("label", "") if session else ""
                    display = f"{target_id}（{label}）" if label else target_id
                    self.send_text(
                        chat_id,
                        self._build_sectioned_message(
                            f"已切换到会话：{display}",
                            [
                                f"状态：{session.get('status', 'idle') if session else '未知'}",
                                f"目录：{session.get('cwd', self.config.default_cwd) if session else self.config.default_cwd}",
                            ],
                            ["运行 <任务>", "继续", "状态"],
                        ),
                    )
                else:
                    sessions = self.state.list_sessions(chat_id, self.config.default_cwd)
                    valid_ids = ", ".join(sid for sid, _ in sessions) if sessions else "无"
                    self.send_text(chat_id, f"会话 {target_id} 不存在。当前会话：{valid_ids}")
                return

        for prefix in ("/close ", "关闭会话 "):
            if text.startswith(prefix):
                target_id = text[len(prefix):].strip()
                if self.state.remove_session(chat_id, target_id, self.config.default_cwd):
                    sessions = self.state.list_sessions(chat_id, self.config.default_cwd)
                    active_id = self.state.get_active_session_id(chat_id, self.config.default_cwd)
                    self.send_text(
                        chat_id,
                        self._build_sectioned_message(
                            f"已关闭会话：{target_id}",
                            [
                                f"剩余 {len(sessions)} 个会话",
                                f"当前活跃：{active_id}",
                            ],
                            ["会话列表", "状态"],
                        ),
                    )
                else:
                    sessions = self.state.list_sessions(chat_id, self.config.default_cwd)
                    if len(sessions) <= 1:
                        self.send_text(chat_id, "不能关闭最后一个会话。至少需要保留一个会话。")
                    else:
                        self.send_text(chat_id, f"会话 {target_id} 不存在或无法关闭。")
                return

        if text in {"关闭会话"}:
            sessions = self.state.list_sessions(chat_id, self.config.default_cwd)
            if len(sessions) <= 1:
                self.send_text(chat_id, "当前只有一个会话，无法关闭。")
                return
            active_id = self.state.get_active_session_id(chat_id, self.config.default_cwd)
            lines = [f"当前会话列表（活跃：{active_id}）"]
            for sid, sstate in sessions:
                label = sstate.get("label", "")
                display = f"  {sid}"
                if label:
                    display += f"（{label}）"
                lines.append(display)
            lines.append("")
            lines.append("请指定要关闭的会话 ID，例如：关闭会话 s2")
            self.send_text(chat_id, "\n".join(lines))
            return

        if text in {"/status", "状态"}:
            chat_state = self._refresh_chat_runtime_state(chat_id)
            started_at = chat_state.get("started_at")
            finished_at = chat_state.get("finished_at")
            live_output = str(chat_state.get("live_output") or "").strip()
            last_result = str(chat_state.get("last_result") or "").strip()
            last_summary = str(chat_state.get("last_summary") or "").strip()
            fallback_summary = last_summary if self._is_fallback_completion_summary(last_summary) else ""
            effective_last_summary = "" if fallback_summary else last_summary
            meaningful_last_result = last_result if not self._is_placeholder_status_summary(last_result) else ""
            claude_jsonl_summary = self._read_claude_jsonl_summary(chat_state, 900)
            foreground_transcript_summary = self._read_foreground_transcript_summary(chat_state, 900)
            active_session_id = self.state.get_active_session_id(chat_id, self.config.default_cwd)
            session_count = len(self.state.list_sessions(chat_id, self.config.default_cwd))
            lines = [
                f"会话：{active_session_id}" + (f"（共 {session_count} 个）" if session_count > 1 else ""),
                f"状态：{self._get_display_status_label(chat_state)}",
                f"目录：{chat_state.get('cwd')}",
                f"上次命令：{chat_state.get('last_command') or '-'}",
                f"授权模式：{chat_state.get('runtime_permission_mode') or self._resolve_effective_permission_mode(False, chat_state)}",
                f"模型：{chat_state.get('runtime_model') or self._resolve_effective_model(chat_state) or '(Claude 默认)'}",
                f"开始时间：{self._format_ts(started_at)}",
                f"结束时间：{self._format_ts(finished_at)}",
                f"总耗时：{self._format_duration(started_at, finished_at if finished_at else None)}",
            ]
            if chat_state.get("status") == "running":
                if live_output:
                    lines.extend(["运行摘要：", self._summarize_result(live_output, 700)])
                else:
                    # 后台 Claude 只有在 CLI 实际吐出 stdout/stderr 后才能抓取片段；没有输出时不能伪造进度。
                    lines.append("运行摘要：后台任务仍在执行，当前还没有可抓取的输出片段。")
            elif live_output:
                # 后台任务完成、失败后仍优先展示 live_output，避免这次改动影响原本可用的后台摘要体验。
                lines.extend(["结果摘要：", self._summarize_result(live_output, 700)])
            elif effective_last_summary:
                # 前台会话在线时拿不到实时 stdout；这里优先展示最近一次 hook 摘要，
                # 让“状态”能稳定回看上一轮真正完成了什么，而不是显示开窗/发命令占位文案。
                lines.extend(["结果摘要：", self._summarize_result(effective_last_summary, 700)])
            elif claude_jsonl_summary:
                # PowerShell transcript 对 Claude TUI 不稳定，Claude 自己的 JSONL 日志才是前台摘要主来源；
                # 没有 Round 标记时也会返回最近 assistant 文本，避免普通任务被误判为无法摘要。
                lines.extend(["前台输出摘要：", self._summarize_result(claude_jsonl_summary, 700)])
            elif foreground_transcript_summary:
                # 前台窗口没有 stdout 管道，但 Start-Transcript 会记录可见输出；
                # Stop hook 丢失时，状态页用 transcript 尾部补齐阶段性摘要。
                lines.extend(["前台输出摘要：", self._summarize_result(foreground_transcript_summary, 700)])
            elif meaningful_last_result:
                # 前台开窗、前台发送命令等场景通常只有 last_result，没有 stdout/stderr；
                # 这里补充兜底展示，确保状态查询始终能返回一段最近结果或最近动作说明。
                lines.extend(["结果摘要：", self._summarize_result(meaningful_last_result, 700)])
            foreground_pid = chat_state.get("foreground_pid") or chat_state.get("active_pid")
            if chat_state.get("status") == "foreground_opened" and foreground_pid:
                # 机器人无法读取用户在前台 Claude 窗口里手动敲入的细节，只能确认窗口仍在线；
                # 这里明确说明能力边界，避免“等待接管”被误读成当前没有在执行。
                if (
                    not live_output
                    and not effective_last_summary
                    and not claude_jsonl_summary
                    and not foreground_transcript_summary
                    and not meaningful_last_result
                ):
                    lines.append("结果摘要：当前前台窗口暂无可直接读取的摘要。")
                if claude_jsonl_summary:
                    lines.append("说明：前台 Claude 窗口仍在线，已从 Claude 会话日志读取最近输出。")
                elif foreground_transcript_summary:
                    lines.append("说明：前台 Claude 窗口仍在线，已从 transcript 兜底读取最近输出。")
                else:
                    lines.append("说明：前台 Claude 窗口仍在线。状态会优先读取 Claude JSONL 会话日志，读取不到时再用 transcript 兜底。")
                lines.append("截图：如需画面，请单独回复“截图 claude”或“截图 桌面”。")
            if chat_state.get("status") == "foreground_busy" and foreground_pid:
                # 前台执行中的输出通过 transcript 兜底读取；如果还没有写出内容，再提示用户用截图确认画面。
                if (
                    not live_output
                    and not effective_last_summary
                    and not claude_jsonl_summary
                    and not foreground_transcript_summary
                    and not meaningful_last_result
                ):
                    lines.append("结果摘要：当前前台窗口暂无可直接读取的摘要。")
                if claude_jsonl_summary:
                    lines.append("说明：这一轮正在前台窗口执行，已从 Claude 会话日志读取最近输出。")
                elif foreground_transcript_summary:
                    lines.append("说明：这一轮正在前台窗口执行，已从 transcript 兜底读取最近输出。")
                else:
                    lines.append("说明：这一轮正在前台窗口执行。如需画面，请单独回复“截图 claude”或“截图 桌面”。")
            if fallback_summary:
                # 兜底通知说明的是“机器人如何判断这轮结束”，不是 Claude 实际产出；
                # 单独列成收尾说明，避免状态页把它误读成文档修复/检查结果本身。
                lines.append(f"收尾说明：{self._summarize_result(fallback_summary, 220)}")
            if chat_state.get("runtime_settings_pending_restart"):
                # 单窗口模式下不会强制重开当前 Claude；这里明确提示新权限/模型只是“已保存”，
                # 避免用户在飞书里误以为当前前台窗口已经被热更新。
                lines.append("说明：你已修改会话的授权模式或模型，但当前前台窗口仍在运行旧参数。新设置会在下次新开 Claude 窗口时生效。")
            if chat_state.get("pending_action"):
                lines.append(f"待处理动作：{self._format_pending_action_label(chat_state.get('pending_action'))}")
            if chat_state.get("last_exit_code") is not None:
                lines.append(f"上次退出码：{chat_state.get('last_exit_code')}")
            if chat_state.get("last_error"):
                # 状态页面向手机查看，优先返回归一化后的中文错误，避免旧 PowerShell 堆栈把关键信息淹没。
                lines.append(f"上次错误：{self._normalize_foreground_send_error(str(chat_state['last_error']))}")
            next_steps: list[str] = []
            if chat_state.get("status") == "waiting_auth":
                next_steps = ["同意", "拒绝", "停止"]
            elif chat_state.get("managed_session"):
                next_steps = ["继续", "前台继续", "停止"]
            self.send_text(chat_id, self._build_sectioned_message("任务状态", lines, next_steps))
            return

        if text in {"窗口", "窗口列表", "前台窗口", "切换窗口"}:
            self._send_window_list(chat_id)
            return

        switch_match = re.fullmatch(r"切换到?窗口\s*(\d+)", text)
        if switch_match:
            # 窗口编号来自实时枚举列表；保存 HWND 而不是 PID，因为多个 Windows Terminal 标签页可能共用同一宿主 PID。
            self._select_window_by_index(chat_id, int(switch_match.group(1)))
            return

        for prefix in ("新窗口继续", "/newcontinue"):
            if text == prefix or text.startswith(prefix + " "):
                prompt = text[len(prefix):].strip() or "继续"
                # 新窗口继续显式要求开新前台窗口，但 Claude 参数用 continue 模式复用上一个项目会话上下文。
                self._queue_claude_task(chat_id, prompt, continue_mode=True, foreground=True, route_to_existing_foreground=False)
                return

        for prefix in ("新窗口运行", "/newrun"):
            if text == prefix or text.startswith(prefix + " "):
                prompt = text[len(prefix):].strip()
                if not prompt:
                    self.send_text(chat_id, "请提供任务内容，例如：新窗口运行 检查当前项目。")
                    return
                # 新窗口运行是新的上下文入口，不能复用已有前台窗口，也不能自动 continue。
                self._queue_claude_task(chat_id, prompt, continue_mode=False, foreground=True, route_to_existing_foreground=False)
                return


        # "暂停"发送 Ctrl+C 中断当前操作，保留窗口不关闭。
        if text in {"暂停", "暂停任务", "中断", "中断任务", "取消当前操作"}:
            self._pause_foreground_task(chat_id)
            return
        # “停止”在窗口驱动模式下默认发送给选中窗口，让 Claude/TUI 自己处理；强杀进程保留给 /stop。
        if text in {"停止", "停止当前任务", "停止任务", "结束当前任务", "终止当前任务"}:
            self._send_text_to_active_window_or_prompt(chat_id, "停止")
            return

        if text == "/stop":
            self._stop_task(chat_id)
            return

        for prefix in ("/run ", "运行 "):
            if text.startswith(prefix):
                prompt = text[len(prefix):].strip()
                if not prompt:
                    self.send_text(chat_id, "请直接发送要给 Claude 的内容，或使用：新窗口运行 <任务>。")
                    return
                # 运行前缀只做兼容剥离，不再触发后台任务；实际输入仍进入当前选中窗口。
                self._send_text_to_active_window_or_prompt(chat_id, prompt)
                return

        for prefix in ("/continue ", "继续 "):
            if text.startswith(prefix):
                prompt = text[len(prefix):].strip() or "继续"
                # 带补充说明的继续也只是发到当前窗口，保持与用户当前看到的 Claude 会话一致。
                self._send_text_to_active_window_or_prompt(chat_id, prompt)
                return

        for prefix in ("/fgrun ", "前台运行 "):
            if text.startswith(prefix):
                prompt = text[len(prefix):].strip()
                if not prompt:
                    self.send_text(chat_id, "请提供任务内容，例如：新窗口运行 检查当前项目。")
                    return
                self._queue_claude_task(chat_id, prompt, continue_mode=False, foreground=True, route_to_existing_foreground=False)
                return

        for prefix in ("/fgcontinue ", "前台继续 "):
            if text.startswith(prefix):
                prompt = text[len(prefix):].strip() or "继续"
                # 带补充说明的“前台继续”仍是当前窗口输入；显式开新窗口请使用“新窗口继续”。
                self._send_text_to_active_window_or_prompt(chat_id, prompt)
                return

        for prefix in ("/bgrun ", "后台运行 ", "/bgcontinue ", "后台继续 "):
            if text.startswith(prefix):
                self.send_text(chat_id, "后台命令已不作为主路径使用。请改用：新窗口运行、新窗口继续，或直接发送内容到当前窗口。")
                return

        # 兜底分支：所有普通飞书文本都发送到当前选中/实时默认窗口，不再静默创建后台 Claude 任务。
        if self._should_dedupe_plain_text_command(chat_id, text):
            # 飞书偶发重复投递时不再回发提示，避免在群里制造额外噪声；日志保留哈希便于排查。
            text_hash = hashlib.sha1(text.strip().encode("utf-8", errors="ignore")).hexdigest()
            self.log(f"dedupe plain text command chat={chat_id} sha1={text_hash}")
            return
        self._send_text_to_active_window_or_prompt(chat_id, text)

    def _queue_claude_task(
        self,
        chat_id: str,
        prompt: str,
        continue_mode: bool,
        foreground: bool,
        route_to_existing_foreground: bool = True,
        open_foreground_only: bool = False,
    ) -> None:
        """Start one Claude execution in the background and report lifecycle updates."""

        command_key = self._normalize_command_key(self._normalize_incoming_text(prompt))
        if self._is_screenshot_control_key(command_key):
            # 截图命令属于机器人本地控制，不应进入后台 Claude 或前台 Claude 会话。
            self.log(f"blocked screenshot command from queue chat={chat_id} prompt={prompt!r}")
            self._preprocess_control_command(chat_id, prompt)
            return

        chat_state = self._refresh_chat_runtime_state(chat_id)
        # effective_continue_mode 表示“这次启动是否应该复用项目最近会话”；
        # 它由当前状态、显式 continue 标志和前台/后台语义共同决定。
        effective_continue_mode = self._should_resume_existing_session(chat_state, continue_mode)
        if open_foreground_only:
            # 单独“前台运行”只负责拉起可接管窗口，不应自动续跑上一轮。
            effective_continue_mode = False
        active_pid = chat_state.get("active_pid")
        foreground_pid = chat_state.get("foreground_pid") or active_pid
        foreground_hwnds = self._find_all_claude_terminal_hwnds(int(foreground_pid)) if foreground_pid else []
        foreground_is_v2_managed = self._is_v2_managed_foreground_state(chat_state)
        if open_foreground_only and foreground_pid and self._process_exists(foreground_pid) and foreground_hwnds and foreground_is_v2_managed:
            # 这里不再新开窗口，是因为用户已经要求“只打开窗口，不直接执行”；
            # 重复拉起只会让前台会话和状态机分叉。
            self.send_text(
                chat_id,
                self._build_sectioned_message(
                    "当前已有可接管前台窗口",
                    [
                        f"目录：{chat_state.get('cwd')}",
                        f"窗口进程：{foreground_pid}",
                        "说明：当前聊天已经有一个 Claude 前台窗口，无需再新开。",
                        "如需把指令送进去，请直接发送：前台继续、继续，或：运行 <任务>。",
                    ],
                    ["前台继续", "继续", "状态"],
                ),
            )
            return
        if open_foreground_only and foreground_pid and self._process_exists(foreground_pid) and not foreground_hwnds:
            # PID 存活但实时没有可截图/可接管 HWND，通常是切 v2 后绑定到了旧 shell 或最小化标签页；
            # 允许重新开一个受 v2 管理的前台窗口，不再被旧 state 卡住。
            self.log(f"foreground pid has no live hwnd, opening new window chat={chat_id} pid={foreground_pid}")
        if (
            route_to_existing_foreground
            and (
                chat_state.get("status") in {"foreground_opened", "foreground_busy"}
                or foreground
            )
            and foreground_pid
            and self._process_exists(foreground_pid)
            and foreground_hwnds
            and foreground_is_v2_managed
        ):
            # 前台窗口 PID 与后台任务 PID 分开保存；即使后台任务结束清空 active_pid，
            # 用户再次发“前台继续”也应优先复用 foreground_pid，避免重复新开 Claude 窗口。
            updated_fields: dict[str, Any] = {}
            if self._current_foreground_settings_stale(chat_state):
                # 单飞书会话固定绑定一个前台窗口时，切权限/模型后也优先保持原窗口；
                # 这里只把“等待重开新窗口后生效”的状态写回，避免机器人擅自把当前窗口关掉。
                updated_fields["runtime_settings_pending_restart"] = True
            if updated_fields:
                chat_state = self.state.update_chat(chat_id, updated_fields, self.config.default_cwd)
            self._send_command_to_existing_foreground_session(chat_id, prompt, int(foreground_pid))
            return
        with self.active_lock:
            # 同一个 chat 不允许同时发起两个启动动作，否则状态文件、授权队列和飞书回复会彼此覆盖。
            if chat_id in self.active_chats:
                chat_state = self._refresh_chat_runtime_state(chat_id)
                active_pid = chat_state.get("active_pid")
                if active_pid and not self._process_exists(active_pid):
                    self.active_chats.discard(chat_id)
                    self.state.update_chat(
                        chat_id,
                        {
                            "status": "failed",
                            "finished_at": time.time(),
                            "last_error": "检测到旧任务进程已退出，已自动回收进程状态。可直接回复“继续”。",
                            "active_pid": None,
                            # 这里只处理正在执行的后台进程锁，不能顺手清掉仍存在的前台窗口。
                            "foreground_pid": chat_state.get("foreground_pid"),
                            "pending_action": "continue_session",
                            "pending_prompt": "继续",
                            "managed_session": True,
                        },
                        self.config.default_cwd,
                    )
                else:
                    self.send_text(chat_id, "当前聊天已有任务在执行，请先等待完成后再发起新任务。")
                    return
            if chat_state.get("status") == "waiting_auth":
                self.send_text(chat_id, "当前会话正在等待授权，请先回复“同意”或“拒绝”，也可以回复“停止”结束本次托管。")
                return
            self.active_chats.add(chat_id)

        cwd = chat_state.get("cwd") or self.config.default_cwd
        runtime_permission_mode = self._resolve_effective_permission_mode(
            foreground,
            chat_state,
            prefer_session_override=True,
        )
        runtime_model = self._resolve_effective_model(chat_state)
        if foreground:
            # 前台启动的状态文字要区分“开空窗口”和“开窗口并执行”，否则飞书里很难判断是否可人工接管。
            mode_label = "前台打开空窗口" if open_foreground_only else (
                "前台直接继续当前会话" if effective_continue_mode and prompt == "继续" else (
                    "前台在当前会话追加指令" if effective_continue_mode else "前台在当前会话执行指令"
                )
            )
            status_value = "foreground_running"
        else:
            # 后台运行只区分是否续用当前项目会话，不区分窗口，因为本身就是无界面执行。
            mode_label = "后台直接继续当前会话" if effective_continue_mode and prompt == "继续" else (
                "后台在当前会话追加指令" if effective_continue_mode else "后台在当前会话执行指令"
            )
            status_value = "running"
        self.state.update_chat(
            chat_id,
            {
                "status": status_value,
                # 开始时间在任务真正进入执行态时写入，这样状态页和完成通知才会显示同一轮耗时。
                "started_at": time.time(),
                "finished_at": None,
                "last_command": prompt,
                "last_result": "",
                "last_error": "",
                "active_pid": None,
                # active_pid 只表示当前后台/启动进程；foreground_pid 表示已打开的可接管窗口，
                # 后台任务启动时不能清空它，否则“前台继续”会误判没有窗口并重复新开。
                "foreground_pid": chat_state.get("foreground_pid"),
                # 进入执行态时显式清空待办动作，避免上一轮授权/继续提示残留到新进程。
                "pending_action": "",
                "pending_prompt": "",
                # 新任务开始后旧 AskUserQuestion 选项不可再复用，否则“1”会被误路由到历史问题。
                "foreground_pending_question": {},
                "last_exit_code": None,
                "managed_session": True,
                "runtime_permission_mode": runtime_permission_mode,
                "runtime_model": runtime_model,
                # 新进程启动时即成为当前聊天唯一受管窗口，之前积累的“待重开生效”提示可以清空。
                "runtime_settings_pending_restart": False,
                "live_output": "",
                "live_output_at": None,
            },
            self.config.default_cwd,
        )
        started_at = self.state.get_chat(chat_id, self.config.default_cwd).get("started_at")
        lines = [
            f"类型：{mode_label}",
            f"目录：{cwd}",
            f"开始时间：{self._format_ts(started_at)}",
            f"授权模式：{runtime_permission_mode}",
            f"模型：{runtime_model or '(Claude 默认)'}",
            f"指令：{prompt or '(无，当前仅打开前台窗口)'}",
        ]
        if foreground:
            lines.append("说明：将打开可接管的 Claude 前台窗口。")
        next_steps = ["状态", "停止"] if not foreground else ["状态", "前台继续", "停止"]
        self.send_text(chat_id, self._build_sectioned_message("任务已开始", lines, next_steps))

        worker = threading.Thread(
            target=self._launch_foreground_task if foreground else self._execute_claude_task,
            args=(chat_id, prompt, cwd, effective_continue_mode, None),
            daemon=True,
        )
        worker.start()

    def _launch_foreground_task(
        self,
        chat_id: str,
        prompt: str,
        cwd: str,
        continue_mode: bool,
        force_permission_mode: str | None = None,
    ) -> None:
        """Open a visible Claude session window so the user can take over locally."""

        try:
            launcher_path = self._write_foreground_launcher(
                chat_id,
                prompt,
                cwd,
                continue_mode,
                force_permission_mode=force_permission_mode,
            )
            start_command = (
                f"$p = Start-Process -FilePath '{self._ps_quote(self.config.pwsh_path)}' "
                f"-ArgumentList @('-NoExit','-ExecutionPolicy','Bypass','-File','{self._ps_quote(str(launcher_path))}') "
                f"-WorkingDirectory '{self._ps_quote(cwd)}' -WindowStyle Normal -PassThru; "
                "$p.Id"
            )
            result = subprocess.run(
                [
                    self.config.pwsh_path,
                    "-NoProfile",
                    "-Command",
                    start_command,
                ],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "前台窗口启动失败")

            pid_text = (result.stdout or "").strip().splitlines()
            launched_pid = int(pid_text[-1]) if pid_text and pid_text[-1].strip().isdigit() else None
            chat_state = self.state.update_chat(
                chat_id,
                {
                    # Keeping the session open in state reflects that the user may
                    # continue iterating manually inside the visible Claude window.
                    "status": "foreground_opened",
                    "finished_at": None,
                    "last_result": "前台会话窗口已打开，等待本机接管。",
                    "active_pid": launched_pid,
                    # foreground_pid 专门记录可复用的前台终端窗口，避免后台任务结束后清空 active_pid 导致重复开窗。
                    "foreground_pid": launched_pid,
                    "last_error": "",
                    "pending_action": "",
                    "pending_prompt": "",
                    # 新前台窗口没有继承上一窗口的提问上下文，必须清空旧选项映射。
                    "foreground_pending_question": {},
                    "last_exit_code": None,
                    "managed_session": True,
                },
                self.config.default_cwd,
            )
            if launched_pid:
                try:
                    self._start_foreground_watch(chat_id, cwd, int(launched_pid))
                except Exception as exc:
                    self.log(f"foreground watch launch failed chat={chat_id} pid={launched_pid} error={exc}")
            lines = [
                f"目录：{cwd}",
                f"开始时间：{self._format_ts(chat_state.get('started_at'))}",
                f"窗口进程：{launched_pid or '-'}",
                "说明：现在可以回到电脑前直接接管该 Claude 窗口，并在窗口中继续下一轮执行。",
            ]
            self.send_text(
                chat_id,
                self._build_sectioned_message("前台会话已打开", lines, ["状态", "停止"]),
            )
        except Exception as exc:  # pragma: no cover - operational safety branch
            finished_at = time.time()
            chat_state = self.state.update_chat(
                chat_id,
                {
                    "status": "failed",
                    "finished_at": finished_at,
                    "last_error": str(exc),
                    "pending_action": "",
                    "pending_prompt": "",
                    "last_exit_code": None,
                    "managed_session": False,
                },
                self.config.default_cwd,
            )
            lines = [
                f"目录：{cwd}",
                f"开始时间：{self._format_ts(chat_state.get('started_at'))}",
                f"结束时间：{self._format_ts(finished_at)}",
                f"总耗时：{self._format_duration(chat_state.get('started_at'), finished_at)}",
                f"异常信息：{exc}",
            ]
            self.send_text(chat_id, self._build_sectioned_message("前台会话启动失败", lines, ["状态", "运行 <任务指令>"]))
        finally:
            with self.active_lock:
                self.active_chats.discard(chat_id)

    def _send_command_to_existing_foreground_session(self, chat_id: str, prompt: str, pid: int) -> None:
        """Route a new instruction into the already-open managed foreground Claude window."""

        command_key = self._normalize_command_key(self._normalize_incoming_text(prompt))
        if self._is_screenshot_control_key(command_key):
            # 双保险：即使入口层因为旧进程/测试路径漏掉了截图命令，这里也禁止把“截图 claude”
            # 这类机器人控制命令粘贴进 Claude 前台窗口。
            self.log(f"blocked screenshot command from foreground send chat={chat_id} pid={pid} prompt={prompt!r}")
            self._preprocess_control_command(chat_id, prompt)
            return

        chat_state = self.state.get_chat(chat_id, self.config.default_cwd)
        try:
            # When a managed foreground session is already open, sending the next
            # command into that exact window preserves the live conversation instead
            # of silently falling back to a background worker.
            self.foreground_adapter.send_command(pid, prompt)
            started_at = time.time()
            try:
                self._start_foreground_watch(chat_id, str(chat_state.get("cwd") or self.config.default_cwd), pid)
            except Exception as exc:
                # 前台命令已经成功送达时，不应因为观察器拉起失败就把本轮直接判成失败；
                # 这里只落日志，至少保留原有 Stop hook 路径继续工作。
                self.log(f"foreground watch launch failed chat={chat_id} pid={pid} error={exc}")
            updated_state = self.state.update_chat(
                chat_id,
                {
                    # 命令已经被送入现有前台窗口，这一轮应改成前台执行中，避免状态页仍显示等待接管。
                    "status": "foreground_busy",
                    "last_command": prompt,
                    "last_result": f"已将命令发送到前台会话窗口并开始执行：{prompt}",
                    "last_error": "",
                    # 每次前台追加指令都重新记录本轮起始时间，方便飞书端查看当前这轮耗时。
                    "started_at": started_at,
                    "finished_at": None,
                    "active_pid": pid,
                    # 前台追加指令成功后继续保留窗口 PID，后续“前台继续”和“截图 claude”都复用同一个窗口。
                    "foreground_pid": pid,
                    "managed_session": True,
                    "pending_action": "",
                    "pending_prompt": "",
                    "last_exit_code": None,
                },
                self.config.default_cwd,
            )
            lines = [
                f"状态：{self._get_display_status_label(updated_state)}",
                f"目录：{updated_state.get('cwd')}",
                f"开始时间：{self._format_ts(started_at)}",
                f"窗口进程：{pid}",
                f"指令：{prompt}",
                "说明：命令已发送到当前托管的 Claude 前台窗口，请回到电脑前查看执行过程。",
            ]
            self.send_text(chat_id, self._build_sectioned_message("前台窗口已接收命令", lines, ["状态", "停止"]))
        except Exception as exc:
            finished_at = time.time()
            # 前台注入失败常来自窗口激活链路；这里先归一化成中文提示，避免状态页长期保留乱码堆栈。
            clean_error = self._normalize_foreground_send_error(str(exc))
            updated_state = self.state.update_chat(
                chat_id,
                {
                    "status": "failed",
                    "finished_at": finished_at,
                    # 前台发送失败常来自 PowerShell/Windows Terminal，写入状态前先清理控制码。
                    "last_error": clean_error,
                    "active_pid": pid,
                    # 即使发送失败，也保留前台窗口 PID，用户修正窗口焦点后可再次尝试，不必重新开窗。
                    "foreground_pid": pid,
                    "managed_session": True,
                },
                self.config.default_cwd,
            )
            lines = [
                f"目录：{updated_state.get('cwd')}",
                f"窗口进程：{pid}",
                f"异常信息：{clean_error}",
                "说明：这次没有成功把命令送进前台窗口，你可以先回到电脑前确认窗口仍在最前台。",
            ]
            self.send_text(chat_id, self._build_sectioned_message("前台窗口发送失败", lines, ["窗口列表", "新窗口继续", "状态", "停止"]))

    def _execute_claude_task(self, chat_id: str, prompt: str, cwd: str, continue_mode: bool) -> None:
        """Run Claude Code for one chat command and push the final result back to Feishu."""

        try:
            command = self._build_claude_args(
                prompt,
                continue_mode=continue_mode,
                print_mode=True,
                foreground=False,
                chat_state=self.state.get_chat(chat_id, self.config.default_cwd),
            )
            # 后台任务需要继承当前进程环境，否则 Claude 的 Hook、授权路由和路径配置会断链。
            env = os.environ.copy()
            # 把当前飞书 chat_id 写入环境变量，PermissionRequest / Stop hook 才知道该把消息回给谁。
            env["FEISHU_CLAUDE_BOT_CHAT_ID"] = chat_id
            env["FEISHU_CLAUDE_BOT_APPROVALS_PATH"] = self.config.approvals_path
            env["FEISHU_CLAUDE_BOT_CONFIG_PATH"] = str(INTEGRATION_ROOT / "config" / "feishu_claude_bot.v2.json")
            # 后台 print 模式由 bot 自己汇总最终输出；Stop hook 只在前台场景负责补通知。
            env["FEISHU_CLAUDE_BOT_EXECUTION_MODE"] = "background"
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
            with self.jobs_lock:
                self.jobs[chat_id] = process
            self.state.update_chat(
                chat_id,
                {
                    # 后台进程 PID 只用于 stop/status 回收，不代表前台窗口；不要覆盖 foreground_pid。
                    "active_pid": process.pid,
                },
                self.config.default_cwd,
            )
            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            output_lock = threading.Lock()
            last_live_update = {"ts": 0.0}

            def capture_stream(stream: Any, target: list[str]) -> None:
                """Capture Claude output incrementally so status can show a live summary."""

                for line in iter(stream.readline, ""):
                    with output_lock:
                        target.append(line)
                        combined = "".join(stdout_chunks + stderr_chunks).strip()
                    now = time.time()
                    if combined and now - last_live_update["ts"] >= 2:
                        # live_output 只保存最近一小段可读摘要，不把整条长输出都塞进状态文件。
                        last_live_update["ts"] = now
                        self.state.update_chat(
                            chat_id,
                            {
                                # 保存最近可见输出，供“状态”命令主动返回当前执行摘要。
                                "live_output": self._summarize_result(combined, 1200),
                                "live_output_at": now,
                            },
                            self.config.default_cwd,
                        )

            stdout_thread = threading.Thread(target=capture_stream, args=(process.stdout, stdout_chunks), daemon=True)
            stderr_thread = threading.Thread(target=capture_stream, args=(process.stderr, stderr_chunks), daemon=True)
            stdout_thread.start()
            stderr_thread.start()
            process.wait()
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)
            stdout = "".join(stdout_chunks).strip()
            stderr = "".join(stderr_chunks).strip()
            result_text = stdout or stderr or "(Claude 未返回文本输出)"
            # 先看退出码，再看文本里有没有“看起来像失败”的语义，因为某些模型/提供方错误会 exit 0。
            status = "done"
            failure_text = ""
            if process.returncode != 0:
                status = "failed"
                failure_text = stderr or result_text
            elif self._is_failure_like_text(result_text):
                # Some provider/network failures still exit 0, so classify them from text.
                status = "failed"
                failure_text = result_text
            finished_at = time.time()
            existing_state = self.state.get_chat(chat_id, self.config.default_cwd)
            if existing_state.get("status") == "stopped":
                # /stop 已经给出最终反馈时，不要再让后台线程把状态覆盖回等待继续。
                return
            final_status = "done"
            pending_action = "continue_session"
            pending_prompt = "继续"
            title = "任务执行完成"
            if status == "failed":
                title = "任务执行失败"
                if self._is_auth_wait_text(result_text):
                    # 当 Claude 直接退出并返回“需要授权”文本时，我们保留会话并等待
                    # 飞书里的“同意/拒绝”，从而避免上下文在移动端丢失。
                    final_status = "waiting_auth"
                    pending_action = "approve_then_continue"
                    title = "任务需要授权"
                else:
                    # 真正的失败才把状态落成 failed；这样“状态”里能区分授权等待和执行失败。
                    final_status = "failed"

            chat_state = self.state.update_chat(
                chat_id,
                {
                    "status": final_status,
                    "finished_at": finished_at,
                    "last_result": result_text,
                    "last_error": failure_text,
                    "active_pid": None,
                    # 后台任务完成后仍保留前台窗口登记，保证后续“前台继续”可以回到原窗口。
                    "foreground_pid": existing_state.get("foreground_pid"),
                    "pending_action": pending_action,
                    "pending_prompt": pending_prompt,
                    "last_exit_code": process.returncode,
                    "managed_session": True,
                    # 最终结果同时刷新 live_output，方便状态页第一屏就看到本轮输出摘要。
                    "live_output": self._summarize_result(result_text, 1200),
                    "live_output_at": finished_at,
                },
                self.config.default_cwd,
            )
            summary_text = self._summarize_result(result_text)
            lines = [
                f"目录：{cwd}",
                f"开始时间：{self._format_ts(chat_state.get('started_at'))}",
                f"结束时间：{self._format_ts(finished_at)}",
                f"总耗时：{self._format_duration(chat_state.get('started_at'), finished_at)}",
                f"退出码：{process.returncode}",
                "结果摘要：",
                summary_text,
            ]
            if final_status == "waiting_auth":
                next_steps = ["同意", "拒绝", "停止"]
            else:
                next_steps = ["继续", "前台继续", "停止"]
            self.send_text(chat_id, self._build_sectioned_message(title, lines, next_steps))
        except Exception as exc:  # pragma: no cover - operational safety branch
            finished_at = time.time()
            chat_state = self.state.update_chat(
                chat_id,
                {
                    "status": "failed",
                    "finished_at": finished_at,
                    "last_error": str(exc),
                    "pending_action": "",
                    "pending_prompt": "",
                    "last_exit_code": None,
                    "managed_session": False,
                },
                self.config.default_cwd,
            )
            lines = [
                f"目录：{cwd}",
                f"开始时间：{self._format_ts(chat_state.get('started_at'))}",
                f"结束时间：{self._format_ts(finished_at)}",
                f"总耗时：{self._format_duration(chat_state.get('started_at'), finished_at)}",
                f"异常信息：{exc}",
            ]
            self.send_text(chat_id, self._build_sectioned_message("任务执行异常", lines, ["状态", "继续", "停止"]))
        finally:
            with self.jobs_lock:
                self.jobs.pop(chat_id, None)
            with self.active_lock:
                self.active_chats.discard(chat_id)


    def _pause_foreground_task(self, chat_id: str) -> None:
        chat_state = self.state.get_chat(chat_id, self.config.default_cwd)
        active_hwnd = chat_state.get("active_window_hwnd")
        foreground_pid = chat_state.get("foreground_pid")
        active_pid = chat_state.get("active_pid")
        target_pid = foreground_pid or active_pid
        if not target_pid and not active_hwnd:
            self.send_text(chat_id, "当前没有正在运行的前台任务。")
            return
        target_hwnd = int(active_hwnd) if active_hwnd else int(target_pid)
        try:
            self._send_hotkey_to_foreground_window(target_hwnd, "ctrl+c")
            self.send_text(chat_id, self._build_sectioned_message(
                "已暂停",
                [f"窗口进程：{target_hwnd}", "说明：已发送 Ctrl+C 中断当前操作。"],
                ["继续", "前台继续", "状态", "停止"],
            ))
        except Exception as exc:
            clean_error = self._normalize_foreground_send_error(str(exc))
            self.send_text(chat_id, self._build_sectioned_message(
                "暂停失败",
                [f"窗口进程：{target_hwnd}", f"异常：{clean_error}"],
                ["状态", "停止"],
            ))

    def _stop_task(self, chat_id: str) -> None:
        """Terminate the currently running Claude process for one chat if present."""

        with self.jobs_lock:
            process = self.jobs.get(chat_id)
        chat_state = self.state.get_chat(chat_id, self.config.default_cwd)
        active_pid = chat_state.get("active_pid")
        foreground_pid = chat_state.get("foreground_pid")
        target_pid = None
        if process is not None and process.poll() is None:
            target_pid = process.pid
        elif active_pid:
            target_pid = int(active_pid)
        elif foreground_pid:
            target_pid = int(foreground_pid)

        if target_pid is not None:
            # Windows taskkill is the simplest way to terminate either a background Claude
            # child process or the visible foreground PowerShell window tree.
            subprocess.run(
                ["taskkill", "/PID", str(target_pid), "/T", "/F"],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
        elif not self._is_managed_status(chat_state.get("status")) and not chat_state.get("managed_session"):
            self.send_text(chat_id, "当前聊天没有正在执行或托管中的任务。")
            return

        chat_state = self.state.update_chat(
            chat_id,
            {
                "status": "stopped",
                # Persist stop time so the chat can immediately see a final duration
                # after a manual cancellation from Feishu.
                "finished_at": time.time(),
                "last_error": "任务已由 /stop 终止",
                "active_pid": None,
                "foreground_pid": None,
                "pending_action": "",
                "pending_prompt": "",
                "managed_session": False,
            },
            self.config.default_cwd,
        )
        with self.active_lock:
            self.active_chats.discard(chat_id)
        lines = [
            f"目录：{chat_state.get('cwd')}",
            f"开始时间：{self._format_ts(chat_state.get('started_at'))}",
            f"结束时间：{self._format_ts(chat_state.get('finished_at'))}",
            f"总耗时：{self._format_duration(chat_state.get('started_at'), chat_state.get('finished_at'))}",
            "说明：任务已由 /stop 主动终止，当前托管会话也已释放。",
        ]
        self.send_text(chat_id, self._build_sectioned_message("任务已停止", lines, ["运行 <任务指令>", "目录 <别名或路径>"]))

    @staticmethod
    def _format_ts(timestamp: Any) -> str:
        """Format a Unix timestamp for operator-friendly status output."""

        if not timestamp:
            return "-"
        return datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M:%S")

    def _split_text(self, text: str) -> list[str]:
        """Split long replies into multiple Feishu-friendly chunks."""

        # 兼容旧测试入口；实际发送分片由 FeishuGateway 统一负责。
        return self.gateway.split_text(text)


def main() -> int:
    """Parse CLI arguments and start the long-connection bot runtime."""

    parser = argparse.ArgumentParser(description="Minimal Feishu Claude bot controller")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to the bot JSON config file",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        raise SystemExit(f"Config file not found: {config_path}")

    config = BotConfig.load(config_path)
    bot = FeishuClaudeBot(config)
    bot.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


