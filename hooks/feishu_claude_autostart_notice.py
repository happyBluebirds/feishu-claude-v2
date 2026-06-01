#!/usr/bin/env python3
"""Send Feishu notices for Claude SessionStart autostart checks."""

from __future__ import annotations

import json
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


INTEGRATION_ROOT = Path(__file__).resolve().parents[1]
CODEX_ROOT = INTEGRATION_ROOT.parents[1]
DEFAULT_LOG_PATH = CODEX_ROOT / "outputs" / "feishu-claude-v2" / "logs" / "feishu-claude-autostart.log"


@dataclass
class NoticeConfig:
    """Feishu credentials and local log settings for startup notices."""

    # Feishu app id from feishu_claude_bot.v2.json.
    app_id: str
    # Feishu app secret from feishu_claude_bot.v2.json.
    app_secret: str
    # Optional autostart diagnostic log path.
    log_path: str

    @classmethod
    def load(cls, path: Path) -> "NoticeConfig":
        """Load Feishu notice settings from the shared bot config."""

        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            app_id=data["app_id"],
            app_secret=data["app_secret"],
            log_path=data.get("autostart_log_path") or str(DEFAULT_LOG_PATH),
        )


def log_event(log_path: Path, message: str) -> None:
    """Append a timestamped diagnostic line for notification troubleshooting."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def send_feishu_text(config: NoticeConfig, chat_id: str, text: str) -> None:
    """Send one text message to a Feishu chat."""

    token_response = post_json(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {
            "app_id": config.app_id,
            "app_secret": config.app_secret,
        },
        headers={},
    )
    tenant_access_token = token_response.get("tenant_access_token")
    if not tenant_access_token:
        raise RuntimeError(f"tenant token missing: {token_response}")

    message_response = post_json(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        headers={"Authorization": f"Bearer {tenant_access_token}"},
    )
    if message_response.get("code") != 0:
        raise RuntimeError(f"send message failed: {message_response}")


def post_json(url: str, payload: dict[str, object], headers: dict[str, str]) -> dict[str, object]:
    """Post JSON with a short timeout so startup notices never hang forever."""

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            **headers,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    """Send a startup notice and write the outcome to local diagnostics."""

    if len(sys.argv) < 4:
        raise SystemExit("usage: feishu_claude_autostart_notice.py <config> <chat_id> <text>")

    config_path = Path(sys.argv[1])
    chat_id = sys.argv[2]
    text = sys.argv[3]
    config = NoticeConfig.load(config_path)
    log_path = Path(config.log_path)
    try:
        send_feishu_text(config, chat_id, text)
        log_event(log_path, f"sent SessionStart notice chat={chat_id}")
    except Exception as exc:
        log_event(log_path, f"failed to send SessionStart notice chat={chat_id or '-'} error={exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

