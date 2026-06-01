# Feishu/Lark Bot Bridge for Local Claude Code

[中文](README.zh-CN.md) | English

Feishu/Lark Bot Bridge for Local Claude Code lets you control a Claude Code instance running on your own Windows machine from a Feishu or Lark chat. It receives chat commands through a Feishu/Lark bot, starts or resumes local Claude Code sessions, forwards permission requests back to chat, and sends completion updates, status summaries, and screenshots.

Control a local Claude Code coding agent from Feishu/Lark while keeping source code and execution state on your own machine.

This project is designed for local-first Claude Code workflows where the source code and tools stay on your machine, while Feishu/Lark acts as the remote control surface.

## What It Does

- Control local Claude Code from Feishu/Lark private chats or group chats.
- Start a new Claude Code window, continue an existing session, or switch between managed windows.
- Forward Claude Code `PermissionRequest`, `Stop`, and `StopFailure` hook events to Feishu/Lark.
- Reply to permission prompts from chat without touching the local machine.
- Request status summaries, Claude window screenshots, or desktop screenshots.
- Use Feishu/Lark long connection mode, so no public callback URL is required.
- Keep runtime state, logs, approvals, and temporary screenshots outside the public repository.

## Repository Layout

```text
app/       Main bot process, command routing, session management, Feishu/Lark gateway
hooks/     Claude Code hook scripts for permissions, completion events, and autostart
scripts/   PowerShell and CMD entrypoints for starting the bot and wiring hooks
config/    Sanitized example config and local config instructions
docs/      Detailed Chinese operation and maintenance docs
tests/     Local regression and simulation scripts
```

## Quick Start

1. Create a Feishu/Lark custom app and enable bot messaging.
2. Install Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

3. Copy the example config:

```powershell
Copy-Item config\feishu_claude_bot.v2.example.json config\feishu_claude_bot.v2.json
```

4. Edit `config/feishu_claude_bot.v2.json`:

- `app_id`
- `app_secret`
- `claude_path`
- `default_cwd`
- `allowed_chat_ids`
- `python_path`
- output paths for state and logs

5. Validate the config:

```powershell
.\scripts\start-feishu-claude-bot.ps1 -ValidateOnly
```

6. Start the bridge:

```powershell
.\scripts\start-feishu-claude-bot.ps1
```

## Chat Commands

Common commands include:

- `help`
- `new window run <task>`
- `new window continue`
- `windows`
- `switch to window 1`
- `continue`
- `status`
- `screenshot claude`
- `screenshot desktop`
- `cwd <path>`
- `permission default`

Chinese command aliases are also supported. See [docs/README.md](docs/README.md) for the full command list and operational notes.

## Security Notes

- Do not commit `config/feishu_claude_bot.v2.json`; it contains real Feishu/Lark credentials and local paths.
- The committed example config is sanitized and uses placeholders.
- Runtime logs, state files, approval queues, and screenshots may contain private project context and are ignored by Git.
- Restrict `allowed_chat_ids` before using the bridge in a shared workspace.

## Documentation

- [中文 README](README.zh-CN.md)
- [Detailed Chinese guide](docs/README.md)
- [Maintenance notes](docs/MAINTENANCE.md)
- [Project index](INDEX.md)
