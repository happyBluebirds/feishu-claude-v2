#!/usr/bin/env python3
"""本机回归测试：模拟飞书消息，验证单窗口模式下的机器人核心行为。"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "app" / "feishu_claude_bot.py"
CONFIG_PATH = ROOT / "config" / "feishu_claude_bot.v2.json"
TEST_CHAT_ID = "regression-test-chat"


def load_bot_module():
    """按文件路径加载机器人模块，便于直接调用内部方法做本机回归测试。"""

    spec = importlib.util.spec_from_file_location("feishu_bot_regression_module", str(MODULE_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 feishu_claude_bot.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def assert_true(condition: bool, message: str) -> None:
    """统一断言输出，失败时直接抛异常中断回归。"""

    if not condition:
        raise AssertionError(message)


def main() -> None:
    """执行单窗口模式的核心消息链路回归。"""

    module = load_bot_module()
    config = module.BotConfig.load(CONFIG_PATH)
    bot = module.FeishuClaudeBot(config)

    sent_messages: list[tuple[str, str]] = []
    foreground_commands: list[tuple[int, str]] = []
    foreground_hotkeys: list[tuple[int, str]] = []
    screenshot_calls: list[int] = []
    screenshot_texts: list[str] = []
    queued_tasks: list[tuple[str, str, bool, bool, bool, bool]] = []

    # 回归脚本只验证路由与状态，不真的发飞书消息或操作现有前台窗口。
    bot.send_text = lambda chat_id, text: sent_messages.append((chat_id, text))
    bot.send_image = lambda chat_id, image_path: sent_messages.append((chat_id, f"IMAGE:{image_path.name}"))
    bot._process_exists = lambda pid: int(pid) > 0
    bot._ensure_foreground_binding = lambda chat_id, chat_state: chat_state  # type: ignore[assignment]
    bot._resolve_claude_screenshot_windows = (  # type: ignore[assignment]
        lambda chat_id, chat_state: (chat_state, int(chat_state.get("foreground_pid") or 0), [int(chat_state.get("foreground_pid") or 0)])
    )
    bot._capture_hwnd_screenshot = lambda hwnd, tag="": screenshot_calls.append(hwnd) or (  # type: ignore[assignment]
        Path(config.state_path).resolve().parent.parent / "temp" / "screenshots" / f"mock-{hwnd}.png"
    )
    bot._send_command_to_existing_foreground_session = (
        lambda chat_id, prompt, pid: foreground_commands.append((pid, prompt))
    )
    bot._send_hotkey_to_foreground_window = lambda pid, hotkey: foreground_hotkeys.append((pid, hotkey))

    temp_dir = tempfile.TemporaryDirectory(prefix="feishu-bot-regression-")
    temp_root = Path(temp_dir.name)
    transcript_path = temp_root / "foreground-transcript.log"
    transcript_path.write_text(
        "\n".join(
            [
                "Windows PowerShell transcript start",
                "Chat ID: regression-test-chat",
                "Claude Code foreground session started.",
                "Round 043 摘要",
                "旧轮次内容不应该出现在最新状态摘要里。",
                "Round 044 摘要",
                "本轮修复 3 个问题，报告已写入 docs/check/round-044.md。",
                "本轮迭代已完成，进入下一轮检查。",
            ]
        ),
        encoding="utf-8",
    )
    module.DEFAULT_CLAUDE_HOME = temp_root / ".claude"
    project_dir = module.DEFAULT_CLAUDE_HOME / "projects" / module.FeishuClaudeBot._encode_claude_project_dir_name(config.default_cwd)
    project_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = project_dir / "regression-latest.jsonl"
    jsonl_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Round 040 摘要\n旧 JSONL 摘要不应展示。"}],
                        },
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "前置说明\nRound 047 摘要\n本轮检查 2 个问题，已完成修复。\n本轮迭代已完成，进入下一轮检查。",
                                }
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
            ]
        ),
        encoding="utf-8",
    )

    base_state = {
        "cwd": config.default_cwd,
        "last_command": "继续",
        "last_result": "前台会话窗口已打开，等待本机接管。",
        "last_summary": "上一轮已完成 5 个问题修复，并进入下一轮检查。",
        "status": "foreground_opened",
        "started_at": None,
        "finished_at": None,
        "last_error": "",
        "active_pid": 31636,
        "foreground_pid": 31636,
        "pending_action": "continue_session",
        "pending_prompt": "继续",
        "last_exit_code": None,
        "managed_session": True,
        "permission_mode": "",
        "model": "",
        "runtime_permission_mode": "default",
        "runtime_model": "",
        "runtime_settings_pending_restart": False,
        "live_output": "",
        "live_output_at": None,
        "foreground_transcript_path": str(transcript_path),
    }
    bot.state.update_chat(TEST_CHAT_ID, base_state, config.default_cwd)

    approval_state = {
        "requests": {
            "old-request": {
                "chat_id": TEST_CHAT_ID,
                "tool_name": "Bash",
                "tool_input": {
                    "command": "ls //a",
                    "description": "加载目录 //a",
                },
                "status": "pending",
                "created_at": 1,
            },
            "new-request": {
                "chat_id": TEST_CHAT_ID,
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": r"D:\code\demo-project\cas-server\Demo.java",
                    "old_string": "select 1 from dual",
                    "new_string": "SELECT 1",
                },
                "status": "pending",
                "created_at": 2,
            },
        }
    }
    bot._load_approvals_state = lambda: approval_state  # type: ignore[assignment]
    bot._save_approvals_state = lambda state: approval_state.update(state)  # type: ignore[assignment]
    bot.handle_command(TEST_CHAT_ID, "同意")
    assert_true(
        approval_state["requests"]["new-request"]["status"] == "approved",
        "单独回复“同意”没有处理第 1 项授权",
    )
    assert_true(
        approval_state["requests"]["old-request"]["status"] == "approved",
        "单独回复“同意”应按用户约定同意全部待授权",
    )
    approval_state["requests"]["new-request"]["status"] = "pending"
    approval_state["requests"]["old-request"]["status"] = "pending"
    bot.handle_command(TEST_CHAT_ID, "授权")
    approval_list_text = sent_messages[-1][1]
    assert_true("1. Edit" in approval_list_text, "授权清单缺少第 1 项")
    assert_true("同意 = 同意全部或当前推荐项" in approval_list_text, "授权清单缺少同意提示")
    assert_true("拒绝 = 拒绝全部或当前问题" in approval_list_text, "授权清单缺少拒绝提示")
    assert_true("同意 1 = 同意/选择第 1 条" in approval_list_text, "授权清单缺少同意序号提示")
    assert_true("拒绝 1 = 拒绝第 1 条" in approval_list_text, "授权清单缺少拒绝序号提示")
    assert_true("同意 1 3 = 同意/选择第 1、3 条" in approval_list_text, "授权清单缺少空格多选提示")
    assert_true("同意 1,3 = 同意/选择第 1、3 条" in approval_list_text, "授权清单缺少逗号多选提示")
    assert_true("全部授权 = 同意全部" in approval_list_text, "授权清单缺少全部授权提示")
    assert_true("同意最新" not in approval_list_text, "授权清单不应再出现“最新”概念")
    assert_true("风险：" in approval_list_text, "授权清单缺少风险提示")
    assert_true("加载目录 //a" in approval_list_text, "授权清单没有展示 Bash 中文说明")
    assert_true("Bash 命令：" in approval_list_text, "授权清单没有展示 Bash 命令标题")
    assert_true("执行方式：shell 命令" in approval_list_text, "授权清单没有展示 Bash 执行方式")
    approval_state["requests"]["ask-user-question-request"] = {
        "chat_id": TEST_CHAT_ID,
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {
                    "question": "这个拆分方案可以接受吗？还是要在本会话一次性完成两阶段？",
                    "header": "执行节奏",
                    "options": [
                        {
                            "label": "Phase 1 先走（推荐）",
                            "description": "本会话先完成 kernel 迁移，Phase 2 下轮单独处理。",
                        },
                        {
                            "label": "一次性两阶段都做",
                            "description": "本会话一次性完成两阶段，但风险和耗时更高。",
                        },
                    ],
                    "multiSelect": False,
                }
            ]
        },
        "status": "pending",
        "created_at": 4,
    }
    bot.handle_command(TEST_CHAT_ID, "授权")
    ask_user_approval_text = sent_messages[-1][1]
    assert_true("AskUserQuestion" in ask_user_approval_text, "授权清单缺少 AskUserQuestion 项")
    assert_true("主题：执行节奏" in ask_user_approval_text, "授权清单没有展示 AskUserQuestion 主题")
    assert_true("问题：这个拆分方案可以接受吗" in ask_user_approval_text, "授权清单没有展示 AskUserQuestion 问题")
    assert_true("可选方案：" in ask_user_approval_text, "授权清单没有展示 AskUserQuestion 选项标题")
    assert_true("1. Phase 1 先走（推荐）" in ask_user_approval_text, "授权清单没有展示 AskUserQuestion 第 1 个选项")
    assert_true("2. 一次性两阶段都做" in ask_user_approval_text, "授权清单没有展示 AskUserQuestion 第 2 个选项")
    assert_true("选择方式：单选" in ask_user_approval_text, "授权清单没有展示 AskUserQuestion 选择方式")
    del approval_state["requests"]["ask-user-question-request"]
    approval_state["requests"]["old-request"]["status"] = "approved"
    approval_state["requests"]["new-request"]["status"] = "approved"
    approval_state["requests"]["ask-user-question-request"] = {
        "chat_id": TEST_CHAT_ID,
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {
                    "question": "这个拆分方案可以接受吗？",
                    "header": "执行节奏",
                    "options": [
                        {"label": "Phase 1 先走（推荐）", "description": "先完成低风险部分。"},
                        {"label": "一次性两阶段都做", "description": "一次做完但风险更高。"},
                    ],
                    "multiSelect": False,
                }
            ]
        },
        "status": "pending",
        "created_at": 5,
    }
    bot.handle_command(TEST_CHAT_ID, "同意 1")
    ask_user_request = approval_state["requests"]["ask-user-question-request"]
    assert_true(ask_user_request["status"] == "approved", "同意 1 没有同意 AskUserQuestion")
    assert_true(
        ask_user_request["updated_input"]["answers"]["这个拆分方案可以接受吗？"] == "Phase 1 先走（推荐）",
        "同意 1 没有把第 1 个方案写入 AskUserQuestion answers",
    )
    approval_state["requests"]["ask-user-question-request"]["status"] = "approved"
    approval_state["requests"]["old-request"]["status"] = "pending"
    approval_state["requests"]["new-request"]["status"] = "pending"
    bot.handle_command(TEST_CHAT_ID, "同意 1")
    assert_true(
        approval_state["requests"]["new-request"]["status"] == "approved",
        "同意 1 没有按清单处理第 1 项授权",
    )
    approval_index_reply_text = sent_messages[-1][1]
    assert_true("已同意第 1 项授权请求" in approval_index_reply_text, "授权处理回复没有使用中文序号提示")
    approval_state["requests"]["ask-user-question-default-request"] = {
        "chat_id": TEST_CHAT_ID,
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {
                    "question": "默认是否先走推荐方案？",
                    "header": "默认选择",
                    "options": [
                        {"label": "推荐方案", "description": "默认选项。"},
                        {"label": "备选方案", "description": "备用选项。"},
                    ],
                    "multiSelect": False,
                }
            ]
        },
        "status": "pending",
        "created_at": 7,
    }
    bot.handle_command(TEST_CHAT_ID, "同意")
    default_request = approval_state["requests"]["ask-user-question-default-request"]
    assert_true(default_request["status"] == "approved", "同意没有批量同意 AskUserQuestion")
    assert_true(
        default_request["updated_input"]["answers"]["默认是否先走推荐方案？"] == "推荐方案",
        "同意没有给 AskUserQuestion 写入默认推荐选项",
    )
    approval_state["requests"]["ask-user-question-default-request"]["status"] = "approved"
    bot.handle_command(TEST_CHAT_ID, "全部授权")
    assert_true(
        approval_state["requests"]["old-request"]["status"] == "approved",
        "“全部授权”没有处理剩余待授权",
    )
    approval_state["requests"]["old-request"]["status"] = "pending"
    bot.handle_command(TEST_CHAT_ID, "全部统一")
    assert_true(
        approval_state["requests"]["old-request"]["status"] == "approved",
        "“全部统一”没有兼容成全部同意",
    )
    approval_state["requests"]["old-request"]["status"] = "pending"
    approval_state["requests"]["new-request"]["status"] = "pending"
    approval_state["requests"]["third-request"] = {
        "chat_id": TEST_CHAT_ID,
        "tool_name": "Bash",
        "tool_input": {
            "command": "pwd",
            "description": "查看当前目录",
        },
        "status": "pending",
        "created_at": 3,
    }
    bot.handle_command(TEST_CHAT_ID, "同意 1,3")
    assert_true(
        approval_state["requests"]["third-request"]["status"] == "approved"
        and approval_state["requests"]["old-request"]["status"] == "approved"
        and approval_state["requests"]["new-request"]["status"] == "pending",
        "同意 1,3 没有按清单批量处理第 1、3 项授权",
    )
    approval_state["requests"]["ask-user-question-request"] = {
        "chat_id": TEST_CHAT_ID,
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {
                    "question": "多租户 Hook 怎么设计？",
                    "header": "Hook 接口",
                    "options": [
                        {"label": "默认 no-op + 可覆盖", "description": "推荐方案。"},
                        {"label": "必填接口", "description": "更严格但接入成本高。"},
                    ],
                    "multiSelect": False,
                }
            ]
        },
        "status": "pending",
        "created_at": 6,
    }
    bot.handle_command(TEST_CHAT_ID, "同意 1 2")
    ask_user_request = approval_state["requests"]["ask-user-question-request"]
    assert_true(ask_user_request["status"] == "approved", "同意 1 2 没有选择清单第 1 条 AskUserQuestion")
    assert_true(
        ask_user_request["updated_input"]["answers"]["多租户 Hook 怎么设计？"] == "必填接口",
        "同意 1 2 没有把第 2 个方案写入 AskUserQuestion answers",
    )
    approval_state["requests"]["ask-user-question-request"]["status"] = "approved"
    approval_state["requests"]["old-request"]["status"] = "pending"
    approval_state["requests"]["new-request"]["status"] = "pending"
    approval_state["requests"]["third-request"]["status"] = "pending"
    bot.handle_command(TEST_CHAT_ID, "拒绝 1 3")
    assert_true(
        approval_state["requests"]["third-request"]["status"] == "denied"
        and approval_state["requests"]["old-request"]["status"] == "denied"
        and approval_state["requests"]["new-request"]["status"] == "pending",
        "拒绝 1 3 没有按清单批量处理第 1、3 项授权",
    )
    approval_state["requests"]["third-request"]["status"] = "approved"
    bot.handle_command(TEST_CHAT_ID, "拒绝")
    assert_true(
        approval_state["requests"]["old-request"]["status"] == "denied"
        and approval_state["requests"]["new-request"]["status"] == "denied",
        "单独回复“拒绝”没有按用户约定拒绝全部待授权",
    )

    # 1. 前台窗口已存在时，自然语言消息应进入同一个窗口，而不是悄悄开新任务。
    bot.handle_command(TEST_CHAT_ID, "继续修复第2个问题")
    assert_true(foreground_commands[-1] == (31636, "继续修复第2个问题"), "自然语言消息没有路由到现有前台窗口")

    # 2. 前台快捷键命令应直接发给同一个窗口。
    bot.handle_command(TEST_CHAT_ID, "前台上一个")
    assert_true(foreground_hotkeys[-1] == (31636, "shift+tab"), "前台 Shift+Tab 没有发送到当前窗口")
    first_hotkey_count = len(foreground_hotkeys)
    bot.handle_command(TEST_CHAT_ID, "前台上一个")
    assert_true(len(foreground_hotkeys) == first_hotkey_count, "前台 Shift+Tab 重复消息没有被去重")

    # 3. 截图命令应复用当前窗口 PID。
    bot._send_claude_screenshot(TEST_CHAT_ID, bot.state.get_chat(TEST_CHAT_ID, config.default_cwd))
    assert_true(screenshot_calls[-1] == 31636, "截图没有使用当前已绑定的前台窗口 PID")
    original_send_claude_screenshot = bot._send_claude_screenshot
    bot._send_claude_screenshot = lambda chat_id, state: screenshot_texts.append("shot")  # type: ignore[assignment]
    bot._preprocess_control_command(TEST_CHAT_ID, "截图 claude")
    first_screenshot_count = len(screenshot_texts)
    bot._preprocess_control_command(TEST_CHAT_ID, "截图 claude")
    assert_true(len(screenshot_texts) == first_screenshot_count, "截图重复消息没有被去重")
    bot._send_claude_screenshot = original_send_claude_screenshot  # type: ignore[assignment]
    bot.recent_control_commands.clear()
    before_foreground_count = len(foreground_commands)
    bot._send_claude_screenshot = lambda chat_id, state: screenshot_texts.append("shot-from-handle")  # type: ignore[assignment]
    bot.handle_command(TEST_CHAT_ID, "截图 claude")
    assert_true(len(foreground_commands) == before_foreground_count, "截图命令被错误发送进了 Claude 前台窗口")
    assert_true(screenshot_texts[-1] == "shot-from-handle", "截图命令没有在机器人本地被拦截处理")
    bot._send_claude_screenshot = original_send_claude_screenshot  # type: ignore[assignment]

    # 4. 切权限后不应自动重开当前窗口，而是标记“下次新窗口生效”。
    bot._set_permission_mode(TEST_CHAT_ID, "绕过")
    updated_state = bot.state.get_chat(TEST_CHAT_ID, config.default_cwd)
    assert_true(updated_state.get("runtime_settings_pending_restart") is True, "切换权限后没有标记待下次新窗口生效")
    bot.handle_command(TEST_CHAT_ID, "前台继续")
    assert_true(foreground_commands[-1] == (31636, "继续"), "前台继续没有继续使用同一个前台窗口")

    # 5. 帮助命令应包含新增命令，避免飞书端功能已支持但帮助文案缺失。
    bot.handle_command(TEST_CHAT_ID, "/help")
    help_text = sent_messages[-1][1]
    assert_true("前台上一个" in help_text, "帮助命令缺少“前台上一个”")
    assert_true("截图 claude" in help_text, "帮助命令缺少“截图 claude”")
    assert_true("前台按键 shift+tab" in help_text, "帮助命令缺少“前台按键 shift+tab”")
    assert_true("飞书授权回复：" in help_text, "帮助命令缺少飞书授权回复说明")
    assert_true("同意：同意全部或当前推荐项" in help_text, "帮助命令缺少“同意”说明")
    assert_true("授权：查看所有待授权项目（会展开问题和可选方案）" in help_text, "帮助命令缺少授权清单展开说明")
    assert_true("拒绝：拒绝全部或当前问题" in help_text, "帮助命令缺少“拒绝”说明")
    assert_true("全部授权：同意全部待授权" in help_text, "帮助命令缺少“全部授权”说明")
    assert_true("同意 1：同意/选择第 1 条" in help_text, "帮助命令缺少单项同意说明")
    assert_true("拒绝 1：拒绝第 1 条" in help_text, "帮助命令缺少单项拒绝说明")
    assert_true("同意 1 3：同意/选择第 1、3 条" in help_text, "帮助命令缺少空格多选说明")
    assert_true("同意 1,3：同意/选择第 1、3 条" in help_text, "帮助命令缺少逗号多选说明")
    assert_true("危险跳过参数" in help_text, "帮助命令缺少 bypassPermissions 前台运行说明")

    # 6. 状态页应明确提示“当前窗口仍是旧参数，新设置下次新窗口生效”。
    bot.handle_command(TEST_CHAT_ID, "状态")
    status_text = sent_messages[-1][1]
    assert_true("当前前台窗口仍在运行旧参数" in status_text, "状态页没有提示当前窗口仍在使用旧参数")
    assert_true("上一轮已完成 5 个问题修复，并进入下一轮检查。" in status_text, "状态页没有优先显示最近一次 hook 摘要")
    assert_true("前台会话窗口已打开，等待本机接管。" not in status_text, "状态页错误回退到了前台占位文案")

    # 6.1 Stop hook 摘要缺失时，状态页应优先从 Claude JSONL 读取最近 assistant 摘要。
    bot.state.update_chat(
        TEST_CHAT_ID,
        {
            "last_summary": "",
            "last_result": "已将命令发送到前台会话窗口并开始执行：继续",
            "status": "foreground_busy",
            "foreground_transcript_path": str(transcript_path),
        },
        config.default_cwd,
    )
    bot.handle_command(TEST_CHAT_ID, "状态")
    jsonl_status_text = sent_messages[-1][1]
    assert_true("前台输出摘要" in jsonl_status_text, "状态页没有展示前台 JSONL 摘要")
    assert_true("Round 047 摘要" in jsonl_status_text, "状态页没有读取到 Claude JSONL 最近 assistant 摘要")
    assert_true("旧 JSONL 摘要不应展示" not in jsonl_status_text, "状态页没有从 JSONL 尾部读取最新摘要")
    assert_true("已从 Claude 会话日志读取最近输出" in jsonl_status_text, "状态页没有说明 JSONL 摘要来源")

    # 6.2 普通非 Round 任务没有进度标记时，也应返回最近 assistant 文本，而不是报暂无摘要。
    jsonl_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "我已经完成这次普通问题排查，重点是修复 hook stdin 传递。"}],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    bot.handle_command(TEST_CHAT_ID, "状态")
    plain_status_text = sent_messages[-1][1]
    assert_true("普通问题排查" in plain_status_text, "无 Round 标记时没有回退到最近 assistant 文本")

    # 7. 显式停止后，普通文本不应再自动启动 Claude，而应进入仅配置态。
    bot.state.update_chat(
        TEST_CHAT_ID,
        {
            "status": "stopped",
            "managed_session": False,
            "active_pid": None,
            "foreground_pid": None,
        },
        config.default_cwd,
    )
    before_command_count = len(foreground_commands)
    before_message_count = len(sent_messages)
    bot.handle_command(TEST_CHAT_ID, "帮我继续修文档")
    assert_true(len(foreground_commands) == before_command_count, "停止后普通文本仍被错误转发到前台窗口")
    assert_true(len(sent_messages) == before_message_count + 1, "停止后普通文本没有返回配置态提示")
    assert_true("已停止后的配置态" in sent_messages[-1][1], "停止后没有提示当前已进入配置态")

    # 8. 当前没有活动窗口时，单独“运行”应只打开前台窗口，不直接要求任务参数。
    original_queue_claude_task = bot._queue_claude_task
    bot._queue_claude_task = lambda chat_id, prompt, continue_mode, foreground, route_to_existing_foreground=True, open_foreground_only=False: queued_tasks.append(  # type: ignore[assignment]
        (chat_id, prompt, continue_mode, foreground, route_to_existing_foreground, open_foreground_only)
    )
    bot.handle_command(TEST_CHAT_ID, "运行")
    assert_true(
        queued_tasks[-1] == (TEST_CHAT_ID, "继续", True, True, True, False),
        "单独“运行”在无活动窗口时没有按“只打开前台窗口”处理",
    )

    # 9. 已有前台窗口时，单独“运行”不应重复新开窗口，而应提示复用当前窗口。
    bot.state.update_chat(
        TEST_CHAT_ID,
        {
            "status": "foreground_opened",
            "managed_session": True,
            "active_pid": 31636,
            "foreground_pid": 31636,
        },
        config.default_cwd,
    )
    before_queue_count = len(queued_tasks)
    bot.handle_command(TEST_CHAT_ID, "运行")
    assert_true(len(queued_tasks) == before_queue_count, "已有前台窗口时，单独“运行”仍错误触发了新任务")
    assert_true("当前已有可接管前台窗口" in sent_messages[-1][1], "已有前台窗口时，单独“运行”没有返回复用提示")

    # 10. 单独“前台运行”在非 bypass 模式下应只打开空窗口，不携带“继续”。
    bot.state.update_chat(
        TEST_CHAT_ID,
        {
            "status": "stopped",
            "managed_session": False,
            "active_pid": None,
            "foreground_pid": None,
            "permission_mode": "default",
        },
        config.default_cwd,
    )
    bot.handle_command(TEST_CHAT_ID, "前台运行")
    assert_true(
        queued_tasks[-1] == (TEST_CHAT_ID, "", False, True, True, True),
        "单独“前台运行”没有按“只打开空窗口”处理",
    )

    # 11. 单独“前台运行”在 bypassPermissions 模式下也应允许开窗，并由命令参数跳过本机确认页。
    bot.state.update_chat(
        TEST_CHAT_ID,
        {
            "status": "stopped",
            "managed_session": False,
            "active_pid": None,
            "foreground_pid": None,
            "permission_mode": "bypassPermissions",
        },
        config.default_cwd,
    )
    before_queue_count = len(queued_tasks)
    bot.handle_command(TEST_CHAT_ID, "前台运行")
    assert_true(len(queued_tasks) == before_queue_count + 1, "bypassPermissions 模式下，前台运行没有触发开窗任务")
    bypass_state = bot.state.get_chat(TEST_CHAT_ID, config.default_cwd)
    bypass_args = bot._build_claude_args("", False, False, True, bypass_state)
    assert_true("--permission-mode" in bypass_args, "bypassPermissions 前台启动缺少权限模式参数")
    assert_true("bypassPermissions" in bypass_args, "bypassPermissions 前台启动没有使用绕过权限模式")
    assert_true(
        "--dangerously-skip-permissions" in bypass_args,
        "bypassPermissions 前台启动缺少跳过本机危险确认的参数",
    )

    bot._queue_claude_task = original_queue_claude_task  # type: ignore[assignment]

    print(
        json.dumps(
            {
                "ok": True,
                "checks": [
                    "natural_language_reuses_same_window",
                    "hotkey_reuses_same_window",
                    "hotkey_dedupes_duplicate_messages",
                    "screenshot_reuses_same_window",
                    "screenshot_dedupes_duplicate_messages",
                    "permission_change_marks_pending_restart_only",
                    "help_contains_new_commands",
                    "status_shows_pending_restart_hint",
                    "status_prefers_last_hook_summary",
                    "status_reads_foreground_jsonl_summary",
                    "status_reads_plain_jsonl_assistant_text",
                    "approval_plain_agree_approves_all",
                    "approval_list_renders_numbered_items",
                    "approval_reply_selects_item_by_index",
                    "approval_reply_supports_multiple_indexes",
                    "approval_reply_supports_all_decision",
                    "approval_reply_supports_homophone_all_alias",
                    "approval_plain_reject_denies_all",
                    "plain_text_blocked_after_stop",
                    "run_without_prompt_opens_foreground_window",
                    "run_without_prompt_reuses_existing_window",
                    "foreground_run_opens_blank_window_only",
                    "foreground_run_allows_bypass_mode",
                    "foreground_bypass_adds_dangerous_skip_flag",
                ],
            },
            ensure_ascii=False,
        )
    )
    temp_dir.cleanup()


if __name__ == "__main__":
    main()

