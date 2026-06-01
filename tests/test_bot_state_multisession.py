#!/usr/bin/env python3
"""Unit tests for BotState multi-session architecture."""

import json
import tempfile
from pathlib import Path

import pytest

import sys

# Import BotState directly from the module
MODULE_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(MODULE_DIR))

from feishu_claude_bot import BotState


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "state.json"


@pytest.fixture
def state(state_path):
    return BotState(state_path)


DEFAULT_CWD = "D:\\code\\test"


class TestLegacyMigration:
    """Old flat state files should auto-migrate into sessions structure."""

    def test_migrate_flat_state(self, state_path):
        state_path.write_text(json.dumps({
            "chats": {
                "chat1": {
                    "cwd": "D:\\code\\proj",
                    "status": "running",
                    "last_command": "run something",
                    "last_result": "",
                    "last_summary": "",
                    "active_pid": 1234,
                    "foreground_pid": None,
                    "pending_action": "",
                    "pending_prompt": "",
                    "last_exit_code": None,
                    "managed_session": True,
                    "last_error": "",
                    "started_at": 1000,
                    "finished_at": None,
                    "permission_mode": "",
                    "model": "",
                    "runtime_permission_mode": "bypassPermissions",
                    "runtime_model": "opus",
                    "runtime_settings_pending_restart": False,
                    "live_output": "some output",
                    "live_output_at": 2000,
                    "foreground_transcript_path": "",
                    "foreground_last_completion_marker": "",
                }
            }
        }, ensure_ascii=False), encoding="utf-8")

        s = BotState(state_path)
        chat = s.get_chat("chat1", DEFAULT_CWD)

        # Chat-level fields preserved
        assert chat["cwd"] == "D:\\code\\proj"
        assert chat["permission_mode"] == ""
        assert chat["model"] == ""

        # Session-level fields migrated into active session
        assert chat["status"] == "running"
        assert chat["last_command"] == "run something"
        assert chat["active_pid"] == 1234

        # Sessions structure created
        assert "sessions" in s.data["chats"]["chat1"]
        assert "s1" in s.data["chats"]["chat1"]["sessions"]
        assert s.data["chats"]["chat1"]["active_session"] == "s1"

    def test_migrate_waiting_continue_status(self, state_path):
        state_path.write_text(json.dumps({
            "chats": {
                "chat1": {
                    "cwd": DEFAULT_CWD,
                    "status": "waiting_continue",
                    "last_command": "do stuff",
                    "last_result": "",
                    "last_summary": "",
                    "active_pid": None,
                    "foreground_pid": None,
                    "pending_action": "",
                    "pending_prompt": "",
                    "last_exit_code": None,
                    "managed_session": False,
                    "last_error": "",
                    "started_at": None,
                    "finished_at": None,
                    "permission_mode": "",
                    "model": "",
                    "runtime_permission_mode": "",
                    "runtime_model": "",
                    "runtime_settings_pending_restart": False,
                    "live_output": "",
                    "live_output_at": None,
                    "foreground_transcript_path": "",
                    "foreground_last_completion_marker": "",
                }
            }
        }, ensure_ascii=False), encoding="utf-8")

        s = BotState(state_path)
        chat = s.get_chat("chat1", DEFAULT_CWD)

        # waiting_continue should be migrated to done/failed
        assert chat["status"] in ("done", "failed")
        assert chat["managed_session"] is True
        assert chat["pending_action"] == "continue_session"
        assert chat["pending_prompt"] == "继续"

    def test_migrate_preserves_error_in_waiting_continue(self, state_path):
        state_path.write_text(json.dumps({
            "chats": {
                "chat1": {
                    "cwd": DEFAULT_CWD,
                    "status": "waiting_continue",
                    "last_error": "some error",
                    "last_command": "",
                    "last_result": "",
                    "last_summary": "",
                    "active_pid": None,
                    "foreground_pid": None,
                    "pending_action": "",
                    "pending_prompt": "",
                    "last_exit_code": None,
                    "managed_session": False,
                    "started_at": None,
                    "finished_at": None,
                    "permission_mode": "",
                    "model": "",
                    "runtime_permission_mode": "",
                    "runtime_model": "",
                    "runtime_settings_pending_restart": False,
                    "live_output": "",
                    "live_output_at": None,
                    "foreground_transcript_path": "",
                    "foreground_last_completion_marker": "",
                }
            }
        }, ensure_ascii=False), encoding="utf-8")

        s = BotState(state_path)
        chat = s.get_chat("chat1", DEFAULT_CWD)
        assert chat["status"] == "failed"

    def test_no_double_migration(self, state_path):
        """Already-migrated state should not be migrated again."""
        state_path.write_text(json.dumps({
            "chats": {
                "chat1": {
                    "cwd": DEFAULT_CWD,
                    "sessions": {
                        "s1": {"status": "idle", "last_command": ""}
                    },
                    "active_session": "s1",
                }
            }
        }, ensure_ascii=False), encoding="utf-8")

        s = BotState(state_path)
        chat = s.get_chat("chat1", DEFAULT_CWD)
        # Should NOT create s1 inside s1
        assert "sessions" not in s.data["chats"]["chat1"]["sessions"]["s1"]


class TestCreateSession:
    def test_create_first_session(self, state):
        sid, sstate = state.create_session("chat1", DEFAULT_CWD)
        # First create_session skips auto-created s1, returns s2
        assert sid == "s2"
        assert sstate["status"] == "idle"

    def test_create_multiple_sessions(self, state):
        s1, _ = state.create_session("chat1", DEFAULT_CWD)
        s2, _ = state.create_session("chat1", DEFAULT_CWD)
        s3, _ = state.create_session("chat1", DEFAULT_CWD)
        # Auto-created s1 exists, so create_session returns s2, s3, s4
        assert s1 == "s2"
        assert s2 == "s3"
        assert s3 == "s4"

    def test_create_session_with_label(self, state):
        sid, sstate = state.create_session("chat1", DEFAULT_CWD, label="bugfix")
        assert sstate["label"] == "bugfix"

    def test_create_session_sets_active(self, state):
        s1, _ = state.create_session("chat1", DEFAULT_CWD)
        s2, _ = state.create_session("chat1", DEFAULT_CWD)
        assert state.get_active_session_id("chat1", DEFAULT_CWD) == s2

    def test_create_session_inherits_cwd(self, state):
        state.get_chat("chat1", DEFAULT_CWD)
        state.update_chat("chat1", {"cwd": "D:\\other"}, DEFAULT_CWD)
        sid, sstate = state.create_session("chat1", DEFAULT_CWD)
        assert sstate["cwd"] == "D:\\other"

    def test_session_id_increments_across_chats(self, state):
        s1, _ = state.create_session("chat1", DEFAULT_CWD)
        s2, _ = state.create_session("chat2", DEFAULT_CWD)
        # Each chat auto-creates s1, so create_session returns s2 for each
        assert s1 == "s2"
        assert s2 == "s3"


class TestListSessions:
    def test_list_empty(self, state):
        # get_chat auto-creates s1
        sessions = state.list_sessions("chat1", DEFAULT_CWD)
        assert len(sessions) == 1
        assert sessions[0][0] == "s1"

    def test_list_multiple(self, state):
        state.create_session("chat1", DEFAULT_CWD)
        state.create_session("chat1", DEFAULT_CWD)
        sessions = state.list_sessions("chat1", DEFAULT_CWD)
        # s1 (auto-created) + s2 + s3 from create_session
        assert len(sessions) == 3
        ids = [s[0] for s in sessions]
        assert "s1" in ids
        assert "s2" in ids
        assert "s3" in ids


class TestSwitchSession:
    def test_switch_valid(self, state):
        s1, _ = state.create_session("chat1", DEFAULT_CWD)
        s2, _ = state.create_session("chat1", DEFAULT_CWD)
        assert state.set_active_session("chat1", s1, DEFAULT_CWD) is True
        assert state.get_active_session_id("chat1", DEFAULT_CWD) == s1

    def test_switch_invalid(self, state):
        assert state.set_active_session("chat1", "s99", DEFAULT_CWD) is False


class TestRemoveSession:
    def test_remove_session(self, state):
        s1, _ = state.create_session("chat1", DEFAULT_CWD)  # s2
        s2, _ = state.create_session("chat1", DEFAULT_CWD)  # s3
        # Remove s2, should leave s1 (auto) and s3
        assert state.remove_session("chat1", s1, DEFAULT_CWD) is True
        sessions = state.list_sessions("chat1", DEFAULT_CWD)
        assert len(sessions) == 2
        ids = [s[0] for s in sessions]
        assert s1 not in ids
        assert "s1" in ids  # auto-created still there

    def test_cannot_remove_last_session(self, state):
        # get_chat auto-creates s1
        assert state.remove_session("chat1", "s1", DEFAULT_CWD) is False

    def test_remove_nonexistent(self, state):
        state.create_session("chat1", DEFAULT_CWD)
        assert state.remove_session("chat1", "s99", DEFAULT_CWD) is False

    def test_remove_active_switches_to_another(self, state):
        s1, _ = state.create_session("chat1", DEFAULT_CWD)  # s2
        s2, _ = state.create_session("chat1", DEFAULT_CWD)  # s3
        # s3 is active, remove it
        state.remove_session("chat1", s2, DEFAULT_CWD)
        active = state.get_active_session_id("chat1", DEFAULT_CWD)
        # Should fall back to s1 (auto-created) or s2
        assert active != s2


class TestGetSession:
    def test_get_existing(self, state):
        s1, _ = state.create_session("chat1", DEFAULT_CWD)
        session = state.get_session("chat1", s1, DEFAULT_CWD)
        assert session is not None
        assert session["status"] == "idle"

    def test_get_nonexistent(self, state):
        state.create_session("chat1", DEFAULT_CWD)
        assert state.get_session("chat1", "s99", DEFAULT_CWD) is None


class TestUpdateSession:
    def test_update_specific_session(self, state):
        s1, _ = state.create_session("chat1", DEFAULT_CWD)
        s2, _ = state.create_session("chat1", DEFAULT_CWD)

        state.update_session("chat1", s1, {"status": "running"}, DEFAULT_CWD)
        state.update_session("chat1", s2, {"status": "done"}, DEFAULT_CWD)

        assert state.get_session("chat1", s1, DEFAULT_CWD)["status"] == "running"
        assert state.get_session("chat1", s2, DEFAULT_CWD)["status"] == "done"

    def test_update_nonexistent_returns_none(self, state):
        state.create_session("chat1", DEFAULT_CWD)
        result = state.update_session("chat1", "s99", {"status": "x"}, DEFAULT_CWD)
        assert result is None


class TestBackwardCompatGetChat:
    """get_chat should return a merged flat view of chat-level + active session fields."""

    def test_flat_view_includes_session_fields(self, state):
        chat = state.get_chat("chat1", DEFAULT_CWD)
        # Session-level fields
        assert "status" in chat
        assert "last_command" in chat
        assert "active_pid" in chat
        # Chat-level fields
        assert "cwd" in chat
        assert "permission_mode" in chat
        assert "model" in chat
        # Internal fields NOT exposed
        assert "sessions" not in chat
        assert "active_session" not in chat

    def test_flat_view_reflects_active_session(self, state):
        s1, _ = state.create_session("chat1", DEFAULT_CWD)
        s2, _ = state.create_session("chat1", DEFAULT_CWD)

        state.update_session("chat1", s1, {"status": "running", "last_command": "task1"}, DEFAULT_CWD)
        state.update_session("chat1", s2, {"status": "done", "last_command": "task2"}, DEFAULT_CWD)

        # Active is s2
        chat = state.get_chat("chat1", DEFAULT_CWD)
        assert chat["status"] == "done"
        assert chat["last_command"] == "task2"

        # Switch to s1
        state.set_active_session("chat1", s1, DEFAULT_CWD)
        chat = state.get_chat("chat1", DEFAULT_CWD)
        assert chat["status"] == "running"
        assert chat["last_command"] == "task1"


class TestBackwardCompatUpdateChat:
    """update_chat should route fields to the correct level (chat vs session)."""

    def test_session_field_goes_to_active_session(self, state):
        s1, _ = state.create_session("chat1", DEFAULT_CWD)
        state.update_chat("chat1", {"status": "running", "last_command": "do stuff"}, DEFAULT_CWD)

        session = state.get_session("chat1", s1, DEFAULT_CWD)
        assert session["status"] == "running"
        assert session["last_command"] == "do stuff"

    def test_chat_field_stays_at_chat_level(self, state):
        state.update_chat("chat1", {"cwd": "D:\\new", "permission_mode": "bypass"}, DEFAULT_CWD)
        raw = state.data["chats"]["chat1"]
        assert raw["cwd"] == "D:\\new"
        assert raw["permission_mode"] == "bypass"
        # Session should NOT have these
        session = raw["sessions"]["s1"]
        assert "cwd" not in session
        assert "permission_mode" not in session

    def test_mixed_update(self, state):
        state.update_chat("chat1", {
            "cwd": "D:\\proj",
            "status": "running",
            "model": "sonnet",
            "last_command": "test",
        }, DEFAULT_CWD)

        raw = state.data["chats"]["chat1"]
        assert raw["cwd"] == "D:\\proj"
        assert raw["model"] == "sonnet"
        session = raw["sessions"]["s1"]
        assert session["status"] == "running"
        assert session["last_command"] == "test"


class TestPersistence:
    """State should survive save/reload."""

    def test_sessions_survive_reload(self, state_path):
        s1 = BotState(state_path)
        s1.create_session("chat1", DEFAULT_CWD)  # s2
        s1.create_session("chat1", DEFAULT_CWD)  # s3
        s1.update_session("chat1", "s1", {"status": "running"}, DEFAULT_CWD)

        # Reload
        s2 = BotState(state_path)
        sessions = s2.list_sessions("chat1", DEFAULT_CWD)
        # s1 (auto) + s2 + s3 = 3 sessions
        assert len(sessions) == 3
        assert s2.get_session("chat1", "s1", DEFAULT_CWD)["status"] == "running"
        assert s2.get_active_session_id("chat1", DEFAULT_CWD) == "s3"


class TestEdgeCases:
    def test_empty_state_file(self, state_path):
        state_path.write_text("", encoding="utf-8")
        s = BotState(state_path)
        chat = s.get_chat("chat1", DEFAULT_CWD)
        assert chat["status"] == "idle"

    def test_empty_json_object(self, state_path):
        state_path.write_text("{}", encoding="utf-8")
        s = BotState(state_path)
        chat = s.get_chat("chat1", DEFAULT_CWD)
        assert chat["status"] == "idle"

    def test_multiple_chats_independent(self, state):
        state.create_session("chat1", DEFAULT_CWD)  # chat1: s1(auto), s2
        state.create_session("chat2", DEFAULT_CWD)  # chat2: s1(auto), s3

        state.update_session("chat1", "s1", {"status": "running"}, DEFAULT_CWD)
        state.update_session("chat2", "s1", {"status": "done"}, DEFAULT_CWD)

        # Same session ID "s1" in different chats have independent state
        assert state.get_session("chat1", "s1", DEFAULT_CWD)["status"] == "running"
        assert state.get_session("chat2", "s1", DEFAULT_CWD)["status"] == "done"

    def test_get_or_create_preserves_existing_sessions(self, state):
        state.create_session("chat1", DEFAULT_CWD)  # s2
        state.create_session("chat1", DEFAULT_CWD)  # s3
        # Calling get_chat again should NOT overwrite sessions
        state.get_chat("chat1", DEFAULT_CWD)
        sessions = state.list_sessions("chat1", DEFAULT_CWD)
        # s1 (auto) + s2 + s3 = 3
        assert len(sessions) == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
