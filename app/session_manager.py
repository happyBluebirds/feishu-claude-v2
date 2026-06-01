#!/usr/bin/env python3
"""Session state boundary for the v2 Feishu Claude bot."""

from __future__ import annotations

from typing import Any


class SessionManager:
    """Single bot-side writer facade for chat/session state.

    The current v2 code still carries the legacy `BotState` implementation.
    This facade is the migration point: new v2 code should call this class
    instead of writing `BotState` directly.
    """

    def __init__(self, state_store: Any, default_cwd: str) -> None:
        """Create a state manager around the legacy store.

        Args:
            state_store: Existing BotState-compatible object.
            default_cwd: Default working directory used for new chats.
        """

        self.state_store = state_store
        self.default_cwd = default_cwd

    def _default_cwd(self, override: str | None = None) -> str:
        """Return the effective default cwd for legacy BotState calls.

        Args:
            override: Optional cwd supplied by old call sites during migration.

        Returns:
            The cwd value that should be passed into the wrapped state store.
        """

        return override or self.default_cwd

    def iter_chat_states(self) -> dict[str, dict[str, Any]]:
        """Return a snapshot of all raw chat states for startup reconciliation.

        Returns:
            A shallow copy keyed by Feishu chat id. Callers must route any writes
            back through SessionManager so BotState remains hidden behind this
            boundary.
        """

        return dict(self.state_store.data.get("chats", {}))

    def get_chat(self, chat_id: str, default_cwd: str | None = None) -> dict[str, Any]:
        """Read the merged active chat/session view.

        Args:
            chat_id: Feishu chat id.
            default_cwd: Optional legacy cwd override while old call sites are migrated.

        Returns:
            Merged chat/session state for the active session.
        """

        return self.state_store.get_chat(chat_id, self._default_cwd(default_cwd))

    def update_chat(
        self,
        chat_id: str,
        updates: dict[str, Any],
        default_cwd: str | None = None,
    ) -> dict[str, Any]:
        """Apply updates to the active chat/session state.

        Args:
            chat_id: Feishu chat id.
            updates: State fields to write.
            default_cwd: Optional legacy cwd override while old call sites are migrated.

        Returns:
            Merged chat/session state after the update.
        """

        return self.state_store.update_chat(chat_id, updates, self._default_cwd(default_cwd))

    def create_session(
        self,
        chat_id: str,
        default_cwd: str | None = None,
        label: str = "",
    ) -> tuple[str, dict[str, Any]]:
        """Create and activate a new Claude session for one chat.

        Args:
            chat_id: Feishu chat id.
            default_cwd: Optional legacy cwd override while old call sites are migrated.
            label: Optional human label saved on the session.

        Returns:
            New session id and raw session state.
        """

        return self.state_store.create_session(chat_id, self._default_cwd(default_cwd), label=label)

    def list_sessions(
        self,
        chat_id: str,
        default_cwd: str | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        """List all sessions belonging to one chat.

        Args:
            chat_id: Feishu chat id.
            default_cwd: Optional legacy cwd override while old call sites are migrated.

        Returns:
            Ordered session id/state pairs from the wrapped store.
        """

        return self.state_store.list_sessions(chat_id, self._default_cwd(default_cwd))

    def get_active_session_id(self, chat_id: str, default_cwd: str | None = None) -> str:
        """Return the active session id for one chat.

        Args:
            chat_id: Feishu chat id.
            default_cwd: Optional legacy cwd override while old call sites are migrated.

        Returns:
            Active session id, creating a default session through BotState if needed.
        """

        return self.state_store.get_active_session_id(chat_id, self._default_cwd(default_cwd))

    def set_active_session(self, chat_id: str, session_id: str, default_cwd: str | None = None) -> bool:
        """Switch the active session for one chat.

        Args:
            chat_id: Feishu chat id.
            session_id: Existing session id to activate.
            default_cwd: Optional legacy cwd override while old call sites are migrated.

        Returns:
            True when the session exists and became active.
        """

        return self.state_store.set_active_session(chat_id, session_id, self._default_cwd(default_cwd))

    def get_session(
        self,
        chat_id: str,
        session_id: str,
        default_cwd: str | None = None,
    ) -> dict[str, Any] | None:
        """Read one raw session state.

        Args:
            chat_id: Feishu chat id.
            session_id: Session id to read.
            default_cwd: Optional legacy cwd override while old call sites are migrated.

        Returns:
            Raw session state or None when it does not exist.
        """

        return self.state_store.get_session(chat_id, session_id, self._default_cwd(default_cwd))

    def remove_session(self, chat_id: str, session_id: str, default_cwd: str | None = None) -> bool:
        """Remove one inactive session from a chat.

        Args:
            chat_id: Feishu chat id.
            session_id: Session id to remove.
            default_cwd: Optional legacy cwd override while old call sites are migrated.

        Returns:
            True when the session existed and was removed.
        """

        return self.state_store.remove_session(chat_id, session_id, self._default_cwd(default_cwd))

    def update_foreground_binding(self, chat_id: str, pid: int, status: str = "foreground_opened") -> dict[str, Any]:
        """Bind one chat to a foreground Claude launcher process.

        Args:
            chat_id: Feishu chat id.
            pid: Foreground PowerShell process id.
            status: Runtime status to expose after rebinding.

        Returns:
            Merged chat/session state after rebinding.
        """

        return self.update_chat(
            chat_id,
            {
                # 前台 PID 是截图、热键、文本注入共用的运行时锚点；集中写入可减少状态漂移。
                "foreground_pid": pid,
                "active_pid": pid,
                "status": status,
                "finished_at": None,
                "last_error": "",
                "managed_session": True,
                "pending_action": "continue_session",
                "pending_prompt": "继续",
            },
        )
