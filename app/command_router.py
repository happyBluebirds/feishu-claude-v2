#!/usr/bin/env python3
"""Pure text-to-command router for the v2 Feishu Claude bot."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class CommandKind(str, Enum):
    """Supported high-priority bot control command kinds."""

    UNKNOWN = "unknown"
    SCREENSHOT_DESKTOP = "screenshot_desktop"
    SCREENSHOT_CLAUDE = "screenshot_claude"
    SCREENSHOT_INDEX = "screenshot_index"
    SCREENSHOT_HELP = "screenshot_help"
    PERMISSION_MODE = "permission_mode"
    MODEL = "model"


@dataclass(frozen=True)
class CommandIntent:
    """Structured command parse result.

    Attributes:
        kind: Parsed command kind.
        text: Normalized display text after Feishu mobile whitespace cleanup.
        key: Compact command key used for exact matching.
        value: Optional command value, such as permission mode or model name.
        index: Optional 1-based screenshot index.
    """

    kind: CommandKind
    text: str
    key: str
    value: str = ""
    index: int | None = None


class CommandRouter:
    """Convert incoming Feishu text into structured bot control commands.

    This class deliberately has no side effects. It does not send messages,
    read state, start Claude, or touch the file system.
    """

    SCREENSHOT_DESKTOP_KEYS = {"/screenshotdesktop", "截图桌面", "截图desktop", "截图全屏", "截图屏幕", "桌面截图"}
    SCREENSHOT_CLAUDE_KEYS = {"/screenshot", "截图claude", "截图cloude", "截图cluade", "claude截图"}
    SCREENSHOT_HELP_KEYS = {"截图", "终端截图", "截图窗口", "截图终端"}
    PERMISSION_PREFIXES = ("权限", "授权模式", "权限模式", "切换授权模式")
    MODEL_PREFIXES = ("模型", "切换模型", "模型模式")

    def normalize_incoming_text(self, text: str) -> str:
        """Normalize Feishu mobile text before routing.

        Args:
            text: Raw message text.

        Returns:
            Text stripped of common mobile-only invisible spacing artifacts.
        """

        return text.replace("\u00a0", " ").replace("\u3000", " ").replace("\u200b", "").strip()

    def normalize_command_key(self, text: str) -> str:
        """Normalize control text for exact command dispatch.

        Args:
            text: Already-cleaned message text.

        Returns:
            Lowercase compact key with whitespace and zero-width characters removed.
        """

        cleaned = re.sub(r"[\s\u00a0\u200b\u200c\u200d\ufeff]+", "", text or "")
        return cleaned.lower()

    def parse_screenshot_index_key(self, command_key: str) -> int | None:
        """Parse a normalized `截图N` command into a 1-based index.

        Args:
            command_key: Compact command key, for example `截图1`.

        Returns:
            The requested window index, or None when not an indexed screenshot command.
        """

        match = re.match(r"^截图(\d+)$", command_key or "")
        return int(match.group(1)) if match else None

    def parse(self, raw_text: str) -> CommandIntent:
        """Parse one Feishu text message into a control command intent.

        Args:
            raw_text: Raw incoming Feishu text.

        Returns:
            Structured command intent. Unknown text remains side-effect free.
        """

        text = self.normalize_incoming_text(raw_text)
        key = self.normalize_command_key(text)
        screenshot_index = self.parse_screenshot_index_key(key)
        if key in self.SCREENSHOT_DESKTOP_KEYS:
            return CommandIntent(CommandKind.SCREENSHOT_DESKTOP, text, key)
        if key in self.SCREENSHOT_CLAUDE_KEYS:
            return CommandIntent(CommandKind.SCREENSHOT_CLAUDE, text, key)
        if screenshot_index is not None:
            return CommandIntent(CommandKind.SCREENSHOT_INDEX, text, key, index=screenshot_index)
        if key in self.SCREENSHOT_HELP_KEYS:
            return CommandIntent(CommandKind.SCREENSHOT_HELP, text, key)

        for prefix in self.PERMISSION_PREFIXES:
            if key.startswith(prefix) and len(key) > len(prefix):
                return CommandIntent(CommandKind.PERMISSION_MODE, text, key, value=key[len(prefix):].strip())

        for prefix in self.MODEL_PREFIXES:
            if key.startswith(prefix) and len(key) > len(prefix):
                return CommandIntent(CommandKind.MODEL, text, key, value=key[len(prefix):].strip())

        return CommandIntent(CommandKind.UNKNOWN, text, key)

    def is_screenshot_control(self, command_key: str) -> bool:
        """Tell whether one compact key is a screenshot control command.

        Args:
            command_key: Compact command key.

        Returns:
            True when the key belongs to a screenshot control command.
        """

        return (
            command_key in self.SCREENSHOT_DESKTOP_KEYS
            or command_key in self.SCREENSHOT_CLAUDE_KEYS
            or command_key in self.SCREENSHOT_HELP_KEYS
            or self.parse_screenshot_index_key(command_key) is not None
        )
