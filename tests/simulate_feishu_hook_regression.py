#!/usr/bin/env python3
"""本机回归测试：验证 PermissionRequest / Stop / StopFailure Hook 的关键路由逻辑。"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PERMISSION_HOOK_PATH = ROOT / "hooks" / "feishu_claude_permission_hook.py"
TURN_HOOK_PATH = ROOT / "hooks" / "feishu_claude_turn_hook.py"
FOREGROUND_RETURN_HELPER_PATH = ROOT / "hooks" / "feishu_claude_foreground_return.py"
FOREGROUND_WATCH_PATH = ROOT / "hooks" / "feishu_claude_foreground_watch.py"
HOOKS_DIR = ROOT / "hooks"
if str(HOOKS_DIR) not in sys.path:
    # foreground_return 会导入同目录的 turn_hook；整理目录后测试进程需要显式补 hooks 路径。
    sys.path.insert(0, str(HOOKS_DIR))


def load_module(module_name: str, module_path: Path):
    """按文件路径加载模块，便于直接调用 Hook 内部函数做本机验证。"""

    spec = importlib.util.spec_from_file_location(module_name, str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块：{module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def assert_true(condition: bool, message: str) -> None:
    """统一断言输出，失败时直接抛异常中断整轮回归。"""

    if not condition:
        raise AssertionError(message)


def main() -> None:
    """执行 Hook 相关的本机回归测试。"""

    permission_hook = load_module("feishu_permission_hook_regression", PERMISSION_HOOK_PATH)
    turn_hook = load_module("feishu_turn_hook_regression", TURN_HOOK_PATH)
    foreground_return = load_module("feishu_foreground_return_regression", FOREGROUND_RETURN_HELPER_PATH)
    foreground_watch = load_module("feishu_foreground_watch_regression", FOREGROUND_WATCH_PATH)

    with tempfile.TemporaryDirectory(prefix="feishu-hook-regression-") as temp_dir:
        temp_root = Path(temp_dir)
        log_path = temp_root / "hook.log"
        state_path = temp_root / "state.json"

        # 模拟一个“手动前台窗口已被机器人托管”的聊天状态，验证 hook 能在缺少环境变量时
        # 仍按 cwd 和托管状态把通知路由回正确的飞书会话。
        bot_state = {
            "chats": {
                "chat-foreground": {
                    "cwd": r"D:\code\demo-project",
                    "status": "foreground_opened",
                    "started_at": 1779294000,
                    "finished_at": None,
                    "managed_session": True,
                    "active_pid": 31636,
                    "foreground_pid": 31636,
                },
                "chat-other": {
                    "cwd": r"D:\code\other-project",
                    "status": "done",
                    "started_at": 1779293000,
                    "finished_at": 1779293600,
                    "managed_session": True,
                    "active_pid": None,
                    "foreground_pid": None,
                },
            }
        }
        state_path.write_text(json.dumps(bot_state, ensure_ascii=False, indent=2), encoding="utf-8")

        permission_config = permission_hook.HookConfig(
            app_id="app-id",
            app_secret="app-secret",
            approvals_path=str(temp_root / "approvals.json"),
            state_path=str(state_path),
            pwsh_path=None,
            python_path=None,
            hook_log_path=str(log_path),
        )

        resolved_permission_chat = permission_hook.resolve_chat_id_from_state(
            permission_config,
            r"D:\code\demo-project",
            log_path,
        )
        assert_true(
            resolved_permission_chat == "chat-foreground",
            "PermissionRequest hook 没有把手动前台窗口路由回正确飞书会话",
        )

        turn_state = turn_hook.load_state(state_path)
        resolved_turn_chat = turn_hook.resolve_chat_id_from_state(
            turn_state,
            r"D:\code\demo-project",
            log_path,
        )
        assert_true(
            resolved_turn_chat == "chat-foreground",
            "Stop/StopFailure hook 没有把手动前台窗口路由回正确飞书会话",
        )

        # 验证完成通知落状态时，托管会话不会被释放，方便继续下一轮。
        updated_done = turn_hook.update_chat_for_stop(
            state_path,
            "chat-foreground",
            "本轮检查已完成。",
            1779294600.0,
        )
        assert_true(updated_done.get("status") == "done", "Stop hook 没有把状态更新为 done")
        assert_true(updated_done.get("pending_action") == "continue_session", "Stop hook 没有保留继续动作")
        assert_true(updated_done.get("managed_session") is True, "Stop hook 不应释放托管会话")
        reloaded_done = turn_hook.load_state(state_path)["chats"]["chat-foreground"]
        assert_true(reloaded_done.get("last_result") == "本轮检查已完成。", "Stop hook 没有写入结果摘要")
        assert_true(reloaded_done.get("last_summary") == "本轮检查已完成。", "Stop hook 没有写入最近 hook 摘要")

        # 验证失败通知落状态时，同样保留会话，便于飞书继续恢复。
        updated_failed = turn_hook.update_chat_for_failure(
            state_path,
            "chat-foreground",
            "API Error: 400 invalid_request_error",
            1779294700.0,
        )
        assert_true(updated_failed.get("status") == "failed", "StopFailure hook 没有把状态更新为 failed")
        assert_true(updated_failed.get("pending_action") == "continue_session", "StopFailure hook 没有保留继续动作")
        assert_true(updated_failed.get("managed_session") is True, "StopFailure hook 不应释放托管会话")
        reloaded_failed = turn_hook.load_state(state_path)["chats"]["chat-foreground"]
        assert_true(
            reloaded_failed.get("last_error") == "API Error: 400 invalid_request_error",
            "StopFailure hook 没有写入失败摘要",
        )
        assert_true(
            reloaded_failed.get("last_summary") == "API Error: 400 invalid_request_error",
            "StopFailure hook 没有保留失败 hook 摘要",
        )

        # 验证授权请求的允许/拒绝响应结构，避免后续 Claude Hook 解析失败。
        allow_response = permission_hook.build_allow_response()
        allow_response_with_updated_input = permission_hook.build_allow_response(
            {
                "questions": [
                    {
                        "question": "这个拆分方案可以接受吗？",
                        "header": "执行节奏",
                        "options": [{"label": "Phase 1", "description": "先做第一阶段"}],
                        "multiSelect": False,
                    }
                ],
                "answers": {"这个拆分方案可以接受吗？": "Phase 1"},
            }
        )
        deny_response = permission_hook.build_deny_response("飞书已拒绝本次授权请求。")
        assert_true(
            allow_response["hookSpecificOutput"]["decision"]["behavior"] == "allow",
            "PermissionRequest allow 响应结构不正确",
        )
        assert_true(
            allow_response_with_updated_input["hookSpecificOutput"]["decision"]["updatedInput"]["answers"]["这个拆分方案可以接受吗？"]
            == "Phase 1",
            "PermissionRequest allow 响应没有携带 AskUserQuestion updatedInput",
        )
        assert_true(
            deny_response["hookSpecificOutput"]["decision"]["behavior"] == "deny",
            "PermissionRequest deny 响应结构不正确",
        )
        assert_true(
            deny_response["hookSpecificOutput"]["decision"]["interrupt"] is True,
            "PermissionRequest deny 响应缺少 interrupt 标记",
        )
        edit_details = "\n".join(
            permission_hook.build_tool_approval_details(
                "Edit",
                {
                    "file_path": r"D:\code\demo-project\cas-server\src\main\java\Demo.java",
                    "old_string": "private static final String CONNECTION_TEST_QUERY = \"select 1 from dual\";",
                    "new_string": "private static final String CONNECTION_TEST_QUERY_MYSQL = \"SELECT 1\";",
                },
            )
        )
        assert_true("原内容：" in edit_details, "Edit 授权详情缺少原内容")
        assert_true("新内容：" in edit_details, "Edit 授权详情缺少新内容")
        assert_true("CONNECTION_TEST_QUERY_MYSQL" in edit_details, "Edit 授权详情没有展示具体变更")
        assert_true("风险：" in edit_details, "Edit 授权详情缺少风险提示")
        pending_hint = "\n".join(permission_hook.build_pending_approval_hint())
        assert_true("同意 = 同意全部或当前推荐项" in pending_hint, "权限请求提示缺少同意语义")
        assert_true("拒绝 = 拒绝全部或当前问题" in pending_hint, "权限请求提示缺少拒绝语义")
        assert_true("同意 1 = 同意/选择第 1 条" in pending_hint, "权限请求提示缺少同意序号命令")
        assert_true("拒绝 1 = 拒绝第 1 条" in pending_hint, "权限请求提示缺少拒绝序号命令")
        assert_true("全部授权 = 同意全部" in pending_hint, "权限请求提示缺少全部授权命令")
        assert_true("授权 = 查看所有待授权项目" in pending_hint, "权限请求提示缺少授权清单入口")
        bash_details = "\n".join(
            permission_hook.build_tool_approval_details(
                "Bash",
                {
                    "command": "grep -A 2 'jdbcType=' /d/code/5a/demo.xml | head -120",
                    "description": "读取 mapper 字段类型",
                },
            )
        )
        assert_true("Bash 命令：" in bash_details, "Bash 授权详情缺少中文命令标题")
        assert_true("grep -A 2" in bash_details, "Bash 授权详情缺少命令正文")
        assert_true("执行方式：shell 命令" in bash_details, "Bash 授权详情缺少执行方式")
        assert_true("风险：只读查询" in bash_details, "Bash 授权详情缺少只读风险提示")
        ask_user_details = "\n".join(
            permission_hook.build_tool_approval_details(
                "AskUserQuestion",
                {
                    "questions": [
                        {
                            "question": "这个拆分方案可以接受吗？还是要在本会话一次性完成两阶段？",
                            "header": "执行节奏",
                            "options": [
                                {
                                    "label": "Phase 1 先走（推荐）",
                                    "description": "本会话完成 Phase 1，Phase 2 下轮记独立任务",
                                },
                                {
                                    "label": "一次性两阶段都做",
                                    "description": "工作量 6-10 小时，错误面更大",
                                },
                            ],
                            "multiSelect": False,
                        }
                    ]
                },
            )
        )
        assert_true("主题：执行节奏" in ask_user_details, "AskUserQuestion 授权详情缺少中文主题")
        assert_true("问题：这个拆分方案可以接受吗" in ask_user_details, "AskUserQuestion 授权详情缺少问题正文")
        assert_true("可选方案：" in ask_user_details, "AskUserQuestion 授权详情缺少选项标题")
        assert_true("1. Phase 1 先走（推荐）" in ask_user_details, "AskUserQuestion 授权详情缺少选项序号")
        assert_true("同意 1 = 选择第 1 个方案" in ask_user_details, "AskUserQuestion 授权详情缺少同意选项提示")
        assert_true("拒绝 1 = 拒绝第 1 个方案" in ask_user_details, "AskUserQuestion 授权详情缺少拒绝选项提示")
        pending_message_state = {
            "requests": {
                "older-bash": {
                    "chat_id": "chat-foreground",
                    "session_id": "session-current",
                    "cwd": r"D:\code\demo-project",
                    "tool_name": "Bash",
                    "tool_input": {"command": "git status", "description": "查看 Git 状态"},
                    "status": "pending",
                    "created_at": 101,
                },
                "newer-question": {
                    "chat_id": "chat-foreground",
                    "session_id": "session-current",
                    "cwd": r"D:\code\demo-project",
                    "tool_name": "AskUserQuestion",
                    "tool_input": {
                        "questions": [
                            {
                                "question": "这个拆分方案可以接受吗？",
                                "header": "执行节奏",
                                "options": [
                                    {"label": "Phase 1 先走（推荐）", "description": "先做第一阶段。"},
                                    {"label": "一次性两阶段都做", "description": "一次完成。"},
                                ],
                                "multiSelect": False,
                            }
                        ]
                    },
                    "status": "pending",
                    "created_at": 102,
                },
                "history-same-chat": {
                    "chat_id": "chat-foreground",
                    "session_id": "session-old",
                    "cwd": r"D:\code\demo-project",
                    "tool_name": "Edit",
                    "tool_input": {"file_path": r"D:\old.java", "old_string": "a", "new_string": "b"},
                    "status": "pending",
                    "created_at": 1,
                },
            }
        }
        pending_message = permission_hook.build_pending_approvals_message(
            pending_message_state,
            "chat-foreground",
            "newer-question",
        )
        assert_true("待授权项目：2 条" in pending_message, "首条授权通知没有带待授权清单数量")
        assert_true("1. AskUserQuestion" in pending_message, "首条授权通知没有按最新在前展示提问项")
        assert_true("2. Bash" in pending_message, "首条授权通知没有展示已有 Bash 待办")
        assert_true("history-same-chat" not in pending_message and r"D:\old.java" not in pending_message, "首条授权通知不应混入历史会话 pending")
        assert_true("可选方案：" in pending_message, "首条授权通知没有展开 AskUserQuestion 选项")
        assert_true("同意 1,3 = 同意/选择第 1、3 条" in pending_message, "首条授权通知缺少多选回复提示")
        stale_state = {
            "requests": {
                "stale": {"chat_id": "chat-foreground", "status": "pending", "created_at": 1},
                "fresh": {"chat_id": "chat-foreground", "status": "pending", "created_at": 1779294700.0},
            }
        }
        expired_count = permission_hook.expire_stale_pending_requests(stale_state, 1779294700.0)
        assert_true(expired_count == 1, "历史 pending 没有被自动过期")
        assert_true(stale_state["requests"]["stale"]["status"] == "expired", "历史 pending 状态没有写成 expired")
        assert_true(stale_state["requests"]["fresh"]["status"] == "pending", "新 pending 不应被误过期")

        # 验证 SessionEnd 去重判断，避免 Stop 已发完成通知后又收到一条重复 SessionEnd。
        assert_true(
            turn_hook.should_skip_session_end(
                {
                    "status": "done",
                    "finished_at": 1779294700.0,
                }
            )
            is True,
            "SessionEnd 去重判断未识别已完成状态",
        )
        assert_true(
            turn_hook.should_skip_session_end(
                {
                    "status": "foreground_busy",
                    "finished_at": None,
                }
            )
            is False,
            "SessionEnd 去重判断误伤仍在执行的前台会话",
        )

        # 验证前台命令返回但 hook 缺失时，兜底逻辑会补写完成状态且保留继续入口。
        fallback_state = {
            "chats": {
                "chat-foreground": {
                    "cwd": r"D:\code\demo-project",
                    "status": "foreground_busy",
                    "started_at": 1779294800.0,
                    "finished_at": None,
                    "managed_session": True,
                    "active_pid": 31636,
                    "foreground_pid": 31636,
                }
            }
        }
        state_path.write_text(json.dumps(fallback_state, ensure_ascii=False, indent=2), encoding="utf-8")
        updated_fallback = foreground_return.update_chat_for_foreground_return(
            state_path,
            "chat-foreground",
            31636,
            1779294800.0,
            1779294860.0,
            0,
            "前台本轮已返回输入态，但当前会话没有收到 Claude Stop hook 摘要；已按兜底逻辑通知飞书。",
        )
        assert_true(updated_fallback.get("status") == "done", "前台返回兜底没有把状态更新为 done")
        assert_true(updated_fallback.get("pending_action") == "continue_session", "前台返回兜底没有保留继续动作")
        assert_true(updated_fallback.get("foreground_pid") == 31636, "前台返回兜底错误清空了窗口 PID")
        assert_true(
            updated_fallback.get("last_summary") == "前台本轮已返回输入态，但当前会话没有收到 Claude Stop hook 摘要；已按兜底逻辑通知飞书。",
            "前台返回兜底没有写入结果摘要",
        )
        assert_true(
            foreground_return.should_skip_fallback(
                {
                    "status": "done",
                    "finished_at": 1779294860.0,
                },
                1779294800.0,
            )
            is True,
            "前台返回兜底未识别已由 hook 落库的完成状态",
        )
        assert_true(
            foreground_return.should_skip_fallback(
                {
                    "status": "done",
                    "finished_at": 1779294860.0,
                },
                1779294800.0,
                "stopped",
            )
            is False,
            "前台停止通知不应被旧 done 状态吞掉",
        )
        transcript_path = temp_root / "foreground-transcript.log"
        transcript_path.write_text(
            "\n".join(
                [
                    "Windows PowerShell transcript start",
                    "Round 044 摘要",
                    "旧轮次内容不应作为完成通知摘要。",
                    "Round 045 摘要",
                    "本轮修复 3 个问题。",
                    "本轮迭代已完成，进入下一轮检查。",
                ]
            ),
            encoding="utf-8",
        )
        marker, transcript_summary = foreground_watch.read_transcript_summary(transcript_path)
        assert_true(bool(marker), "前台观察器没有识别 transcript 完成标记")
        assert_true("Round 045 摘要" in transcript_summary, "前台观察器没有提取本轮摘要")
        assert_true("旧轮次内容不应作为完成通知摘要" not in transcript_summary, "前台观察器没有从 transcript 尾部提取最新轮次")
        assert_true("Windows PowerShell transcript" not in transcript_summary, "前台观察器没有过滤 transcript 噪音")

        foreground_watch.CLAUDE_HOME = temp_root / ".claude"
        claude_project_dir = (
            foreground_watch.CLAUDE_HOME
            / "projects"
            / foreground_watch.encode_claude_project_dir_name(r"D:\code\demo-project")
        )
        claude_project_dir.mkdir(parents=True, exist_ok=True)
        claude_jsonl_path = claude_project_dir / "session.jsonl"
        claude_jsonl_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "Round 046 摘要\n旧 JSONL 完成摘要不应命中。"}],
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
                                        "text": "Round 047 摘要\n本轮修复 hook stdin 和前台摘要。\n本轮迭代已完成，进入下一轮检查。",
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
        jsonl_marker, jsonl_summary = foreground_watch.read_claude_jsonl_completion_summary(r"D:\code\demo-project")
        assert_true(bool(jsonl_marker), "前台观察器没有识别 Claude JSONL 完成标记")
        assert_true("Round 047 摘要" in jsonl_summary, "前台观察器没有读取 Claude JSONL 最新完成摘要")
        assert_true("旧 JSONL 完成摘要不应命中" not in jsonl_summary, "前台观察器没有从 JSONL 尾部读取最新完成摘要")

        claude_jsonl_path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "普通任务已经处理完，但没有固定轮次标记。"}],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        no_marker, no_marker_summary = foreground_watch.read_claude_jsonl_completion_summary(r"D:\code\demo-project")
        assert_true(no_marker == "" and no_marker_summary == "", "观察器不应把无完成标记的普通文本当作完成通知触发")
        generic_tail = foreground_watch.read_claude_jsonl_tail_summary(r"D:\code\demo-project")
        assert_true("普通任务已经处理完" in generic_tail, "前台停止通知应能读取无完成标记的最新输出尾部")
        generic_marker, generic_marker_summary = foreground_watch.read_claude_jsonl_tail_marker(r"D:\code\demo-project")
        assert_true(bool(generic_marker), "长驻前台兜底应能为普通 assistant 输出生成去重标记")
        assert_true("普通任务已经处理完" in generic_marker_summary, "长驻前台兜底没有读取普通 assistant 输出摘要")

        child_project_dir = (
            foreground_watch.CLAUDE_HOME
            / "projects"
            / foreground_watch.encode_claude_project_dir_name(r"D:\code\demo-project\docs\migration-check")
        )
        child_project_dir.mkdir(parents=True, exist_ok=True)
        child_jsonl_path = child_project_dir / "child-session.jsonl"
        child_jsonl_path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "子目录会话里出现的选项摘要，应从父项目 watcher 推送。"}],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        child_marker, child_summary = foreground_watch.read_claude_jsonl_tail_marker(r"D:\code\demo-project")
        assert_true(bool(child_marker), "父项目 watcher 没有扫描子目录 Claude JSONL")
        assert_true("子目录会话里出现的选项摘要" in child_summary, "父项目 watcher 没有优先读取子目录最新摘要")

        stopped_transcript_path = temp_root / "foreground-stopped-transcript.log"
        stopped_transcript_path.write_text(
            "\n".join(
                [
                    "Windows PowerShell transcript start",
                    "主要权衡：",
                    "路径 A：加配置和批量分支。",
                    "路径 B：只替换默认规则。",
                    "要不要按路径 A 执行？如果同意我就创建任务清单。",
                ]
            ),
            encoding="utf-8",
        )
        stopped_transcript_tail = foreground_watch.read_transcript_tail_summary(stopped_transcript_path)
        assert_true("路径 A：加配置和批量分支。" in stopped_transcript_tail, "前台停止通知没有读取 transcript 最近输出")
        assert_true("要不要按路径 A 执行" in stopped_transcript_tail, "前台停止通知没有保留决策问题原文")

        print(
            json.dumps(
                {
                    "ok": True,
                    "checks": [
                        "permission_hook_resolves_manual_foreground_chat",
                        "turn_hook_resolves_manual_foreground_chat",
                        "stop_hook_preserves_managed_session",
                        "stop_failure_hook_preserves_managed_session",
                        "turn_hook_persists_last_summary",
                        "foreground_return_fallback_preserves_session",
                        "foreground_watch_reads_transcript_completion",
                        "foreground_watch_reads_claude_jsonl_completion",
                        "foreground_watch_ignores_jsonl_without_completion_marker",
                        "foreground_watch_reads_generic_stopped_tail",
                        "foreground_watch_marks_generic_assistant_tail",
                        "foreground_watch_reads_child_project_jsonl",
                        "permission_hook_response_shapes_valid",
                        "permission_hook_edit_details_readable",
                        "permission_hook_numbered_hint_readable",
                        "permission_hook_bash_card_readable",
                        "permission_hook_ask_user_question_readable",
                        "session_end_skip_logic_valid",
                    ],
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
