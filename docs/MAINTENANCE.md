# 飞书 Claude 机器人维护说明

本文档面向后续排障和功能维护，重点说明目录分层、修改入口、回归步骤和常见故障定位顺序。

## 1. 修改入口

- 修改飞书命令解析、状态查询、截图、前台窗口控制：
  - `app/feishu_claude_bot.py`

- 修改 Python 启动路径、`vendor` 依赖加载：
  - `app/bootstrap_feishu_tool.py`

- 修改飞书授权逻辑：
  - `hooks/feishu_claude_permission_hook.py`

- 修改任务完成、失败通知和摘要回传：
  - `hooks/feishu_claude_turn_hook.py`

- 修改前台窗口返回兜底通知：
  - `hooks/feishu_claude_foreground_return.py`

- 修改前台窗口长期观察：
  - `hooks/feishu_claude_foreground_watch.py`

- 修改 Claude 启动时自动拉起机器人：
  - `hooks/feishu_claude_bot_autostart.py`
  - `hooks/feishu_claude_autostart_notice.py`

- 修改本机配置：
  - `config/feishu_claude_bot.v2.json`

## 2. 根目录规范

根目录只保留 `INDEX.md`。人工启动应使用 `scripts/start-feishu-claude-bot.ps1`。

## 3. 启动、停止、重启

启动：

```powershell
powershell -ExecutionPolicy Bypass -File "D:\code\codex\integrations\feishu-claude-v2\scripts\start-feishu-claude-bot.ps1"
```

查看进程：

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'feishu_claude_bot\.py' } | Select-Object ProcessId, Name, CommandLine | Format-List
```

停止机器人主进程：

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'feishu_claude_bot\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

重启：

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'feishu_claude_bot\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
powershell -ExecutionPolicy Bypass -File "D:\code\codex\integrations\feishu-claude-v2\scripts\start-feishu-claude-bot.ps1"
```

## 4. 回归测试

修改 `app/feishu_claude_bot.py` 后至少执行：

```powershell
python "D:\code\codex\integrations\feishu-claude-v2\tests\simulate_feishu_bot_regression.py"
```

修改 `hooks/` 下脚本后至少执行：

```powershell
python "D:\code\codex\integrations\feishu-claude-v2\tests\simulate_feishu_hook_regression.py"
```

修改路径、启动脚本或目录结构后建议同时执行：

```powershell
python -m py_compile "D:\code\codex\integrations\feishu-claude-v2\app\feishu_claude_bot.py" "D:\code\codex\integrations\feishu-claude-v2\app\bootstrap_feishu_tool.py" "D:\code\codex\integrations\feishu-claude-v2\hooks\feishu_claude_permission_hook.py" "D:\code\codex\integrations\feishu-claude-v2\hooks\feishu_claude_turn_hook.py" "D:\code\codex\integrations\feishu-claude-v2\hooks\feishu_claude_foreground_return.py" "D:\code\codex\integrations\feishu-claude-v2\hooks\feishu_claude_foreground_watch.py" "D:\code\codex\integrations\feishu-claude-v2\hooks\feishu_claude_bot_autostart.py" "D:\code\codex\integrations\feishu-claude-v2\hooks\feishu_claude_autostart_notice.py"
```

## 5. 日志分层

- `outputs/feishu-claude-v2/state/`
  - 跨重启保留的状态文件。
  - 不建议在有活动会话时删除。

- `outputs/feishu-claude-v2/logs/`
  - 排障日志。
  - 可按需要清理历史轮转文件。

- `outputs/feishu-claude-v2/logs/foreground/`
  - 新版前台窗口的 PowerShell transcript。
  - 该目录现在只是兜底来源；Claude TUI 输出不一定会被 transcript 完整捕获。
  - `状态` 的主摘要来源是 Claude JSONL 会话日志：`%USERPROFILE%\.claude\projects\<项目目录编码>\*.jsonl`。
  - 前台观察器也会优先识别 Claude JSONL 里的完成标记，并在真实 Stop hook 缺失时补发完成通知。
  - 对长驻交互式 `claude.exe`，前台观察器还会等待最新 assistant JSONL 摘要稳定 15 秒后补发兜底通知；这是为了覆盖进程不退出且 Stop hook 偶发缺失的代理/旧版 CLI 场景。
  - 已经在旧配置下打开的 Claude 窗口可能缓存旧 hook 命令；如果完成通知继续缺失，需要关闭后重新通过飞书打开。

- `outputs/feishu-claude-v2/temp/launchers/`
  - 一次性前台启动和前台控制脚本。
  - 机器人会自动清理过期文件。

- `outputs/feishu-claude-v2/temp/screenshots/`
  - 一次性截图和截图辅助脚本。
  - 发送成功后会尽量自动删除。

## 6. 常见问题定位顺序

- 飞书发消息无回复：
  - 看 `feishu-claude-bot.log` 是否有 `recv`。
  - 没有 `recv` 时优先检查飞书长连接进程、应用事件订阅和机器人权限。

- 飞书提示已发送到前台，但 Claude 窗口没执行：
  - v2 当前以实时窗口为主：先在飞书发 `窗口列表`，再发 `切换到窗口1` 或 `切换到窗口2` 固定后续输入目标。
  - 后续普通文本、`继续`、`运行 <文本>` 都应发送到选中的实时 HWND；不要再把 `foreground_pid` 当成唯一窗口身份，因为多个 Windows Terminal 标签页可能共用同一个宿主 PID。
  - 看 bot 日志里的前台发送脚本是否执行失败。
  - 新的 `send-window-*.ps1` 会先确认目标 HWND 已经成为前台窗口，再粘贴文本；如果失败返回 `WINDOW_ACTIVATE_FAILED`，说明 Windows 拒绝激活，机器人不会继续盲目粘贴。
  - 如果错误来自 `send-foreground-*.ps1` 的 `Add-Type`，优先检查内联 C# 是否显式引用 `System.Management`、`System.Diagnostics.Process`、`System.ComponentModel.Primitives`；PowerShell 7 不一定复用前一行 `Add-Type -AssemblyName` 加载的程序集。
  - 如果 state 里残留 `regression-test-chat` 或 `foreground_pid` 指向已退出窗口，应先清理测试会话并把主 chat 重新绑定到真实前台 `pwsh` PID。
  - 再检查窗口是否最小化、锁屏或焦点不在可交互桌面。

- 飞书每次发送通知时桌面弹出空白 Python/PowerShell 窗口：
  - 优先检查 `feishu_claude_foreground_watch.py`、`feishu_claude_bot_autostart.py`、`feishu_claude_permission_hook.py` 里后台 `subprocess.run/Popen` 是否带 `CREATE_NO_WINDOW`。
  - 前台 Claude 窗口本身必须可见，但 watcher、return helper、autostart notice、hook 自动拉起 bot 都是后台辅助进程，不能弹出可见控制台。
  - 修复后需要重启 v2 bot，并清掉旧的 `feishu_claude_foreground_watch.py` 进程；否则旧 watcher 仍可能继续用旧启动方式发通知。

- 飞书命令进入了不符合预期的后台任务：
  - 日常入口已经改为窗口驱动：`新窗口继续` 负责新开前台窗口并复用上一会话上下文，`新窗口运行 <任务>` 负责新开前台窗口并使用新上下文。
  - 裸 `继续` 只向当前选中窗口输入 `继续`；普通文本默认发到当前选中窗口。
  - `运行 <文本>` 只作为兼容前缀，会剥掉“运行”后把 `<文本>` 发到当前选中窗口，不再代表后台运行。
  - 后台相关命令仅保留兼容提示和内部能力，排障时不要把它作为主路径验证。

- 飞书截图不好用：
  - `截图 claude` 会先枚举当前 `foreground_pid` 对应的终端窗口；若进程仍存在但 `hwnds=0`，代码会尝试重新扫描当前带 `claude.exe` 子进程的 `pwsh` 并写入 `claude screenshot rebound` 日志。
  - `截图 1`、`截图2` 这类编号截图必须在 `_preprocess_control_command` 阶段拦截；如果日志只有 `preprocess enter` 而没有 `preprocess screenshot kind=index`，说明命令归一化或截图路由又漏了。
  - 排障测试不要直接跑会写真实 state 的 bot 回归脚本；如需构造临时 bot，必须避免触发 `_ensure_foreground_binding` 启动测试 watcher，测试后清理非真实 chat 和临时 watcher 进程。

- 授权消息没有到飞书：
  - 看 `feishu-claude-permission-hook.log`。
  - 确认 Claude 当前权限模式不是完全绕过授权。
  - 授权消息应展示中文动作、风险提示、具体命令/文件详情，并在首条消息里直接带当前待授权清单；回复 `授权` 可重新查看清单。
  - 单独回复 `同意` 会同意全部待授权；只想处理单项时回复 `同意 1`、`拒绝 1`、`同意 2`、`拒绝 2`。
  - `全部授权`、`全部同意` 都表示同意全部，`全部拒绝` 表示拒绝全部。

- 完成通知没有到飞书：
  - 看 `feishu-claude-turn-hook.log`。
  - 新日志里的 `message_id` 是飞书 OpenAPI 返回的消息 ID；如果脚本显示已发送但手机端没看到，优先用它区分“发送成功但客户端未展示”和“实际发送失败”。
  - 看 `%USERPROFILE%\.claude\settings.json` 里的 `PermissionRequest` / `Stop` / `StopFailure` 是否调用 `scripts\run_permission_hook.cmd` 和 `scripts\run_turn_hook.cmd`。
  - 不要把 hook 命令写成 `cmd /c "python.exe" "hook.py"`；这种写法会让 stdin JSON 被 cmd 当成命令执行，表现为 Claude 认为 hook success，但飞书没有收到授权或完成通知。
  - 如果前台会话没有触发真实 `Stop` hook，再看前台观察器是否启动。
  - 如果真实 hook 缺失但窗口是新版机器人打开的，再看 Claude JSONL 是否有最新 assistant 消息。
  - 如果 Claude JSONL 可读但仍无通知，再看 `logs/foreground/` 下 transcript 兜底是否有最近输出。

- 状态显示和真实窗口不一致：
  - 看 `feishu-claude-bot-state.json` 里的 `status`、`foreground_pid`、`active_pid`。
  - 如状态残留，优先通过飞书发送 `状态` 触发刷新，再决定是否 `停止` 后重新拉起。
