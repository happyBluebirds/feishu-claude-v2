#!/usr/bin/env python3
"""Integration tests: simulate Feishu messages calling the bot.

Each test creates a fresh bot with stubbed I/O, sends commands through
handle_command, and asserts on captured replies and state changes.
"""

import json
import sys
import threading
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

MODULE_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(MODULE_DIR))

if "lark_oapi" not in sys.modules:
    # 单元测试会完全替换飞书发送入口；这里仅提供导入桩，避免离线环境缺 SDK 时无法收集测试。
    lark_stub = types.ModuleType("lark_oapi")
    lark_stub.LogLevel = types.SimpleNamespace(INFO="INFO")
    lark_stub.Client = types.SimpleNamespace(builder=lambda: MagicMock())
    sys.modules["lark_oapi"] = lark_stub

from feishu_claude_bot import BotConfig, BotState, CommandRouter, FeishuClaudeBot

DEFAULT_CWD = "D:\\code\\test"
CHAT_ID = "oc_test_chat_001"


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

@dataclass
class CapturedMessage:
    chat_id: str
    text: str


@dataclass
class CapturedTask:
    chat_id: str
    prompt: str
    continue_mode: bool
    foreground: bool
    open_foreground_only: bool = False
    route_to_existing_foreground: bool = True


class TestableBot(FeishuClaudeBot):
    """FeishuClaudeBot with all external I/O stubbed out for testing."""

    def __init__(self, tmp_path: Path) -> None:
        self._captured_messages: list[CapturedMessage] = []
        self._captured_tasks: list[CapturedTask] = []
        self._stub_process_exists: bool = False

        # Build config pointing to temp directory
        state_dir = tmp_path / "state"
        logs_dir = tmp_path / "logs"
        state_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)

        config = BotConfig(
            app_id="test_app_id",
            app_secret="test_app_secret",
            claude_path="claude",
            default_cwd=DEFAULT_CWD,
            state_path=str(state_dir / "feishu-claude-bot-state.json"),
            log_path=str(logs_dir / "feishu-claude-bot.log"),
            approvals_path=str(state_dir / "feishu-claude-bot-approvals.json"),
            permission_hook_log_path=str(logs_dir / "permission-hook.log"),
            turn_hook_log_path=str(logs_dir / "turn-hook.log"),
            autostart_log_path=str(logs_dir / "autostart.log"),
        )

        # Bypass __init__ to avoid creating a real Feishu client
        self.config = self._normalize_output_paths(config)
        self.state = BotState(Path(self.config.state_path))
        self.log_path = Path(self.config.log_path)
        self.client = MagicMock()
        self.active_chats: set[str] = set()
        self.active_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.jobs: dict[str, Any] = {}
        self.jobs_lock = threading.Lock()
        self.recent_control_commands: dict[tuple[str, str], float] = {}
        self.recent_plain_text_commands: dict[tuple[str, str], float] = {}
        self.recent_message_ids: dict[str, float] = {}
        self.command_router = CommandRouter()

    # --- Stubbed methods ---

    def send_text(self, chat_id: str, text: str) -> None:
        self._captured_messages.append(CapturedMessage(chat_id=chat_id, text=text))

    def send_image(self, chat_id: str, image_path: Path) -> None:
        self._captured_messages.append(CapturedMessage(chat_id=chat_id, text=f"[image:{image_path}]"))

    def _queue_claude_task(
        self,
        chat_id: str,
        prompt: str,
        continue_mode: bool,
        foreground: bool,
        route_to_existing_foreground: bool = True,
        open_foreground_only: bool = False,
    ) -> None:
        self._captured_tasks.append(CapturedTask(
            chat_id=chat_id,
            prompt=prompt,
            continue_mode=continue_mode,
            foreground=foreground,
            open_foreground_only=open_foreground_only,
            route_to_existing_foreground=route_to_existing_foreground,
        ))

    def _process_exists(self, pid: Any) -> bool:
        return self._stub_process_exists

    def _refresh_chat_runtime_state(self, chat_id: str) -> dict[str, Any]:
        return self.state.get_chat(chat_id, self.config.default_cwd)

    def _resolve_realtime_window_targets(self) -> list[dict[str, Any]]:
        return []

    def log(self, message: str) -> None:
        pass  # silent in tests

    # --- Helpers ---

    def last_reply(self) -> str | None:
        return self._captured_messages[-1].text if self._captured_messages else None

    def all_replies(self) -> list[str]:
        return [m.text for m in self._captured_messages]

    def last_task(self) -> CapturedTask | None:
        return self._captured_tasks[-1] if self._captured_tasks else None

    def clear_captures(self) -> None:
        self._captured_messages.clear()
        self._captured_tasks.clear()

    def send(self, text: str) -> None:
        """Shortcut: simulate a Feishu user sending text."""
        self.handle_command(CHAT_ID, text)

    def get_state(self) -> dict[str, Any]:
        """Get current merged chat state."""
        return self.state.get_chat(CHAT_ID, self.config.default_cwd)

    def get_raw_state(self) -> dict[str, Any]:
        """Get raw internal chat state (with sessions)."""
        with self.state.lock:
            return self.state._get_or_create_chat(CHAT_ID, self.config.default_cwd)


@pytest.fixture
def bot(tmp_path):
    return TestableBot(tmp_path)


# ---------------------------------------------------------------------------
# Help command
# ---------------------------------------------------------------------------

class TestHelpCommand:
    def test_help_shows_session_commands(self, bot):
        bot.send("/help")
        reply = bot.last_reply()
        assert "新建会话" in reply
        assert "会话列表" in reply
        assert "切换会话" in reply
        assert "关闭会话" in reply

    def test_help_alias(self, bot):
        bot.send("帮助")
        assert "新建会话" in bot.last_reply()


# ---------------------------------------------------------------------------
# Create session
# ---------------------------------------------------------------------------

class TestCreateSession:
    def test_new_session_command(self, bot):
        bot.send("新建会话")
        reply = bot.last_reply()
        assert "已创建新会话" in reply
        # First create_session skips auto-created s1, returns s2
        assert "s2" in reply

    def test_new_session_slash(self, bot):
        bot.send("/new")
        assert "已创建新会话" in bot.last_reply()

    def test_new_session_with_label(self, bot):
        bot.send("新建会话 bugfix")
        reply = bot.last_reply()
        assert "bugfix" in reply

    def test_new_session_with_label_slash(self, bot):
        bot.send("/new feature-x")
        reply = bot.last_reply()
        assert "feature-x" in reply

    def test_new_session_becomes_active(self, bot):
        bot.send("新建会话")
        active = bot.state.get_active_session_id(CHAT_ID, bot.config.default_cwd)
        # The newly created session should be active
        assert active is not None and active.startswith("s")

    def test_create_multiple_sessions(self, bot):
        bot.send("新建会话")  # s2
        bot.clear_captures()
        bot.send("新建会话")  # s3
        reply = bot.last_reply()
        assert "3 个会话" in reply or "共 3" in reply


# ---------------------------------------------------------------------------
# List sessions
# ---------------------------------------------------------------------------

class TestListSessions:
    def test_list_sessions_empty(self, bot):
        bot.send("会话列表")
        reply = bot.last_reply()
        # get_chat auto-creates s1, so there should be 1 session
        assert "s1" in reply

    def test_list_sessions_shows_active_marker(self, bot):
        bot.send("新建会话")
        bot.clear_captures()
        bot.send("会话列表")
        reply = bot.last_reply()
        assert "*" in reply  # active session marker

    def test_list_sessions_slash(self, bot):
        bot.send("/sessions")
        assert "s1" in bot.last_reply()

    def test_list_sessions_shows_status(self, bot):
        bot.send("会话列表")
        reply = bot.last_reply()
        assert "idle" in reply


# ---------------------------------------------------------------------------
# Switch session
# ---------------------------------------------------------------------------

class TestSwitchSession:
    def test_switch_session(self, bot):
        bot.send("新建会话")
        bot.clear_captures()
        bot.send("切换会话 s1")
        reply = bot.last_reply()
        assert "已切换到会话" in reply
        assert "s1" in reply

    def test_switch_session_slash(self, bot):
        bot.send("新建会话")
        bot.clear_captures()
        bot.send("/switch s1")
        assert "已切换到会话" in bot.last_reply()

    def test_switch_nonexistent(self, bot):
        bot.send("切换会话 s99")
        reply = bot.last_reply()
        assert "不存在" in reply

    def test_switch_updates_active(self, bot):
        bot.send("新建会话")
        bot.send("新建会话")
        bot.send("切换会话 s1")
        assert bot.state.get_active_session_id(CHAT_ID, bot.config.default_cwd) == "s1"


# ---------------------------------------------------------------------------
# Close session
# ---------------------------------------------------------------------------

class TestCloseSession:
    def test_close_session(self, bot):
        bot.send("新建会话")  # s1
        bot.send("新建会话")  # s2
        bot.clear_captures()
        bot.send("关闭会话 s1")
        reply = bot.last_reply()
        assert "已关闭会话" in reply
        assert "s1" in reply

    def test_close_session_slash(self, bot):
        bot.send("新建会话")  # s1
        bot.send("新建会话")  # s2
        bot.clear_captures()
        bot.send("/close s1")
        assert "已关闭会话" in bot.last_reply()

    def test_cannot_close_last_session(self, bot):
        bot.send("关闭会话 s1")
        reply = bot.last_reply()
        assert "不能关闭" in reply or "最后一个" in reply

    def test_close_nonexistent(self, bot):
        bot.send("新建会话")  # s1
        bot.send("新建会话")  # s2
        bot.clear_captures()
        bot.send("关闭会话 s99")
        reply = bot.last_reply()
        assert "不存在" in reply or "无法关闭" in reply

    def test_close_active_switches_to_another(self, bot):
        bot.send("新建会话")  # s2 (active)
        bot.send("新建会话")  # s3 (active)
        bot.send("关闭会话 s3")
        active = bot.state.get_active_session_id(CHAT_ID, bot.config.default_cwd)
        # Should fall back to s1 (auto) or s2
        assert active != "s3"

    def test_close_without_id_shows_list(self, bot):
        bot.send("新建会话")  # s1
        bot.send("新建会话")  # s2
        bot.clear_captures()
        bot.send("关闭会话")
        reply = bot.last_reply()
        assert "s1" in reply or "s2" in reply


# ---------------------------------------------------------------------------
# Backward compat: existing commands route to active session
# ---------------------------------------------------------------------------

class TestBackwardCompatRouting:
    def test_run_prefixed_text_requires_window(self, bot):
        bot.send("运行 fix the bug")
        assert bot.last_task() is None
        assert "当前没有可接管的 Claude 前台窗口" in bot.last_reply()

    def test_continue_requires_window(self, bot):
        bot.send("继续")
        assert bot.last_task() is None
        assert "当前没有可接管的 Claude 前台窗口" in bot.last_reply()

    def test_status_shows_active_session(self, bot):
        bot.send("状态")
        reply = bot.last_reply()
        assert "会话" in reply
        assert "状态" in reply

    def test_permission_update_goes_to_active_session(self, bot):
        bot.send("权限 bypassPermissions")
        state = bot.get_state()
        assert state["permission_mode"] == "bypassPermissions"

    def test_model_update_goes_to_active_session(self, bot):
        bot.send("模型 sonnet")
        state = bot.get_state()
        assert state["model"] == "sonnet"

    def test_cwd_update(self, bot, tmp_path):
        new_dir = tmp_path / "project"
        new_dir.mkdir()
        bot.send(f"目录 {new_dir}")
        state = bot.get_state()
        assert state["cwd"] == str(new_dir)

    def test_stop_command(self, bot):
        bot.send("停止")
        # Should not crash, may send a reply
        assert bot.last_reply() is not None


# ---------------------------------------------------------------------------
# Session isolation
# ---------------------------------------------------------------------------

class TestSessionIsolation:
    def test_sessions_have_independent_status(self, bot):
        """Session-level fields (status, last_command) are independent per session."""
        # s1 is auto-created, update its status
        bot.state.update_session(CHAT_ID, "s1", {"status": "running", "last_command": "task1"}, bot.config.default_cwd)
        # Create s2
        bot.send("新建会话")
        # s2 should be idle
        raw = bot.get_raw_state()
        sessions = raw["sessions"]
        assert sessions["s1"]["status"] == "running"
        assert sessions["s1"]["last_command"] == "task1"
        assert sessions["s2"]["status"] == "idle"

    def test_model_is_chat_level_shared(self, bot):
        """model/permission_mode are chat-level, shared across sessions."""
        bot.send("模型 opus")
        bot.send("新建会话")  # creates s2, active = s2
        bot.send("模型 sonnet")

        raw = bot.get_raw_state()
        # model is chat-level, not per-session
        assert raw["model"] == "sonnet"
        # sessions don't have model field
        assert "model" not in raw["sessions"]["s1"]
        assert "model" not in raw["sessions"]["s2"]


# ---------------------------------------------------------------------------
# Status command with session info
# ---------------------------------------------------------------------------

class TestStatusWithSessions:
    def test_status_single_session(self, bot):
        bot.send("状态")
        reply = bot.last_reply()
        # Single session, may or may not show session count
        assert "状态" in reply

    def test_status_multiple_sessions(self, bot):
        bot.send("新建会话")  # s2
        bot.send("新建会话")  # s3
        bot.clear_captures()
        bot.send("状态")
        reply = bot.last_reply()
        assert "3 个" in reply

    def test_status_shows_session_id(self, bot):
        bot.send("状态")
        reply = bot.last_reply()
        assert "会话" in reply


# ---------------------------------------------------------------------------
# Command parsing edge cases
# ---------------------------------------------------------------------------

class TestCommandParsing:
    def test_whitespace_handling(self, bot):
        bot.send("  新建会话  ")
        assert "已创建新会话" in bot.last_reply()

    def test_unknown_command_falls_through(self, bot):
        bot.send("do something unrelated")
        assert bot.last_task() is None
        assert "当前没有可接管的 Claude 前台窗口" in bot.last_reply()

    def test_screenshot_not_forwarded_to_claude(self, bot):
        bot.send("截图 桌面")
        # Should be handled by screenshot logic, not forwarded as task
        # (may fail due to no real display, but should not create a task)
        tasks = bot._captured_tasks
        assert len(tasks) == 0 or all(t.prompt != "截图 桌面" for t in tasks)


# ---------------------------------------------------------------------------
# State persistence across commands
# ---------------------------------------------------------------------------

class TestStatePersistence:
    def test_state_survives_multiple_commands(self, bot):
        bot.send("模型 opus")       # chat-level model = opus
        bot.send("权限 bypassPermissions")  # chat-level permission = bypassPermissions
        bot.send("新建会话")        # create s2
        bot.send("模型 sonnet")     # chat-level model = sonnet (overrides)

        # Reload state from disk
        reloaded = BotState(Path(bot.config.state_path))
        sessions = reloaded.list_sessions(CHAT_ID, bot.config.default_cwd)
        assert len(sessions) >= 2

        # chat-level model should be sonnet (last write wins)
        chat = reloaded.get_chat(CHAT_ID, bot.config.default_cwd)
        assert chat["model"] == "sonnet"
        assert chat["permission_mode"] == "bypassPermissions"


# ---------------------------------------------------------------------------
# Stop state blocks plain text
# ---------------------------------------------------------------------------

class TestStoppedState:
    def test_plain_text_blocked_after_stop(self, bot):
        # First put the bot into a managed session state
        bot.state.update_chat(CHAT_ID, {
            "status": "done",
            "managed_session": True,
        }, bot.config.default_cwd)
        bot.send("停止")
        bot.clear_captures()
        bot.send("some random text")
        # Should get config guidance, not a task
        reply = bot.last_reply()
        assert reply is not None
        assert "配置态" in reply or "运行" in reply
        # Should NOT have queued a new task for "some random text"
        task = bot.last_task()
        if task is not None:
            assert task.prompt != "some random text"


# ---------------------------------------------------------------------------
# Foreground launch shortcuts
# ---------------------------------------------------------------------------

class TestForegroundLaunchShortcuts:
    def test_new_run_without_prompt_opens_blank_foreground_window(self, bot):
        """`新窗口运行` without a prompt should open a blank Claude foreground window."""

        bot.send("新窗口运行")

        task = bot.last_task()
        assert task is not None
        assert task.prompt == ""
        assert task.continue_mode is False
        assert task.foreground is True
        assert task.route_to_existing_foreground is False
        assert task.open_foreground_only is True

    def test_newrun_without_prompt_opens_blank_foreground_window(self, bot):
        """`/newrun` without a prompt should match the Chinese shortcut behavior."""

        bot.send("/newrun")

        task = bot.last_task()
        assert task is not None
        assert task.prompt == ""
        assert task.continue_mode is False
        assert task.foreground is True
        assert task.route_to_existing_foreground is False
        assert task.open_foreground_only is True


# ---------------------------------------------------------------------------
# Approval persistence
# ---------------------------------------------------------------------------

def write_approval_state(bot: TestableBot, requests: dict[str, Any]) -> None:
    """Persist approval requests through the real JSON file used by the bot."""

    approvals_path = Path(bot.config.approvals_path)
    approvals_path.parent.mkdir(parents=True, exist_ok=True)
    approvals_path.write_text(
        json.dumps({"requests": requests}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_approval_state(bot: TestableBot) -> dict[str, Any]:
    """Read the approval JSON file after a simulated Feishu command."""

    return json.loads(Path(bot.config.approvals_path).read_text(encoding="utf-8"))


class TestApprovalPersistence:
    def test_ask_user_question_selection_is_saved_to_disk(self, bot):
        """Replying `同意 1` to a single AskUserQuestion should approve the stored request."""

        request_id = "session-100"
        question_text = "您希望如何调整前端布局？"
        write_approval_state(
            bot,
            {
                request_id: {
                    "chat_id": CHAT_ID,
                    "session_id": "session",
                    "cwd": DEFAULT_CWD,
                    "tool_name": "AskUserQuestion",
                    "tool_input": {
                        "questions": [
                            {
                                "question": question_text,
                                "header": "布局方式",
                                "options": [
                                    {"label": "修改数据库配置", "description": "显示传统布局"},
                                    {"label": "修改前端代码", "description": "强制传统布局"},
                                ],
                                "multiSelect": False,
                            }
                        ]
                    },
                    "status": "pending",
                    "created_at": 100.0,
                }
            },
        )

        bot.send("同意 1")

        saved_request = read_approval_state(bot)["requests"][request_id]
        assert saved_request["status"] == "approved"
        assert saved_request["updated_input"]["answers"][question_text] == "修改数据库配置"
        assert "剩余待授权" not in bot.last_reply()

    def test_indexed_non_question_approval_is_saved_to_disk(self, bot):
        """Replying `拒绝 1` should persist the indexed approval decision."""

        request_id = "session-200"
        write_approval_state(
            bot,
            {
                request_id: {
                    "chat_id": CHAT_ID,
                    "session_id": "session",
                    "cwd": DEFAULT_CWD,
                    "tool_name": "Bash",
                    "tool_input": {"command": "git status", "description": "查看 Git 状态"},
                    "status": "pending",
                    "created_at": 200.0,
                }
            },
        )

        bot.send("拒绝 1")

        saved_request = read_approval_state(bot)["requests"][request_id]
        assert saved_request["status"] == "denied"
        assert "已拒绝" in bot.last_reply()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
