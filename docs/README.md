# 飞书 Claude 机器人

## 1. 作用

这是当前生效的飞书控制器，用于：

- 在飞书里发命令控制本机 Claude Code
- 支持 `新窗口继续`、`新窗口运行`、`窗口列表`、`切换到窗口1`、`继续`、`状态`、`截图 claude`、`目录`
- 支持在飞书里切换当前会话的授权模式和模型
- 支持在 `状态` 中返回后台任务最近可抓取的输出摘要
- 支持前台窗口通过 Claude JSONL 会话日志返回最近 assistant 摘要，PowerShell transcript 仅作为兜底
- 支持按需发送 Claude 窗口截图或当前桌面截图
- 通过飞书长连接模式接收消息，不要求公网回调地址

当前项目已经按职责整理目录：主程序在 `app/`，Claude hook 在 `hooks/`，配置在 `config/`，启动脚本在 `scripts/`，回归脚本在 `tests/`，维护文档在 `docs/`。根目录只保留 `INDEX.md`，不再维护旧路径兼容入口。

## 2. 前置条件

### 飞书应用

- 已创建企业自建应用
- 已拿到 `App ID` 和 `App Secret`
- 已给机器人开通收发消息权限
- 已把机器人拉进目标群聊或允许私聊

### 飞书应用权限与事件

至少建议确认以下配置已经打开：

- 机器人能力：允许机器人在会话中接收消息
- 消息权限：允许发送文本消息
- 事件订阅：订阅 `im.message.receive_v1`
- 连接方式：使用长连接模式即可，不要求公网回调地址

### 本机 Claude

- 本机 `claude.exe` 可直接执行
- 前台可接管模式默认通过 `C:\Program Files\PowerShell\7\pwsh.exe` 拉起可见窗口
- 推荐先手动验证：

```powershell
& "C:\Users\YOUR_USER\.codemoss\dependencies\claude-sdk\node_modules\@anthropic-ai\claude-agent-sdk-win32-x64\claude.exe" --version
```

### Python 依赖

- 需要安装飞书官方 Python SDK：

```powershell
python -m pip install -r "D:\code\codex\integrations\feishu-claude-v2\requirements.txt"
```

- `vendor/` 目录只作为本机离线兜底，不提交到 Git；恢复环境时优先通过 `requirements.txt` 安装依赖。

## 3. 配置

1. 复制示例配置文件：

```powershell
Copy-Item "D:\code\codex\integrations\feishu-claude-v2\config\feishu_claude_bot.example.json" "D:\code\codex\integrations\feishu-claude-v2\config\feishu_claude_bot.v2.json"
```

2. 修改以下字段：

- `app_id`
- `app_secret`
- `claude_path`
- `default_cwd`
- `allowed_chat_ids`
- `default_model`

## 4. 启动

```powershell
python "D:\code\codex\integrations\feishu-claude-v2\app\bootstrap_feishu_tool.py" "D:\code\codex\integrations\feishu-claude-v2\app\feishu_claude_bot.py" --config "D:\code\codex\integrations\feishu-claude-v2\config\feishu_claude_bot.v2.json"
```

### 4.1 使用启动脚本

前台启动：

```powershell
powershell -ExecutionPolicy Bypass -File "D:\code\codex\integrations\feishu-claude-v2\scripts\start-feishu-claude-bot.ps1"
```

后台启动：

```powershell
Start-Process -FilePath "powershell.exe" -ArgumentList "-ExecutionPolicy","Bypass","-File","D:\code\codex\integrations\feishu-claude-v2\scripts\start-feishu-claude-bot.ps1" -WorkingDirectory "D:\code\codex\integrations\feishu-claude" -WindowStyle Hidden
```

说明：

- 如果本机 Claude 先触发了 `PermissionRequest` hook，而飞书机器人尚未启动，hook 会自动尝试在后台拉起飞书长连接机器人。
- 但如果电脑刚开机、机器人完全没启动、且你是先从飞书发消息给机器人，这种场景下仍然需要至少启动过一次机器人，因为飞书长连接本身必须先有本地进程在线。

### 4.2 停止

停止所有 bot 进程：

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'feishu_claude_bot\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

### 4.3 重启

先停止，再重新后台启动：

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'feishu_claude_bot\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Process -FilePath "powershell.exe" -ArgumentList "-ExecutionPolicy","Bypass","-File","D:\code\codex\integrations\feishu-claude-v2\scripts\start-feishu-claude-bot.ps1" -WorkingDirectory "D:\code\codex\integrations\feishu-claude" -WindowStyle Hidden
```

### 4.4 查看当前是否已启动

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'feishu_claude_bot\.py' } | Select-Object ProcessId, Name, CommandLine | Format-List
```

## 5. 命令

- `/help`
- `/whoami`
- `/run 帮我检查 docs`
- `/fgrun 优化信创迁移文档`
- `/bgrun 帮我检查 docs`
- `/continue [继续修复剩余问题]`
- `/fgcontinue [继续修复剩余问题]`
- `/bgcontinue [继续修复剩余问题]`
- `/status`
- `/screenshot`
- `/screenshot desktop`
- `/stop`
- `/cwd D:\code\your-project`
- `/permission default`
- `/permission acceptEdits`
- `/permission 跟随配置`
- `/model opus`
- `/model sonnet`
- `/model 跟随配置`
- `帮助`
- `我是谁`
- `新窗口运行 检查当前项目`
- `新窗口继续`
- `窗口列表`
- `切换到窗口1`
- `继续`
- `运行 帮我检查 docs`（兼容前缀，会把后面的文本发送到当前选中窗口）
- `前台运行 优化信创迁移文档`（兼容旧入口，等价于新开前台窗口）
- `前台继续 [修复剩余问题]`（兼容旧入口，等价于新开前台窗口并继续）
- `前台上一个`
- `前台按键 shift+tab`
- `后台继续 [修复剩余问题]`（兼容提示，不作为主路径）
- `状态`
- `截图`
- `截图 claude`
- `截图 桌面`
- `截图桌面`
- `停止`
- `目录 D:\code\your-project`
- `权限 default`
- `权限 acceptEdits`
- `权限 跟随配置`
- `模型 opus`
- `模型 sonnet`
- `模型 跟随配置`
- `目录 5a`
- `目录 主数据`
- `目录 数据治理`
- `目录 消息总线`
- 直接发送自然语言任务，例如：`帮我检查 docs 目录的问题`

说明：

- 单独发送 `目录` 这类不带参数的快捷词时，bot 会返回中文提示，不会误启动空任务。
- 单独发送 `运行` 不再代表后台运行；请直接发送要给 Claude 的内容，或使用 `新窗口运行 <任务>`。
- `运行 <文本>` 只做兼容剥离，会把 `<文本>` 发送到当前选中窗口。
- 单独发送 `继续` 时，bot 会向当前选中窗口输入 `继续`，不再创建后台任务。
- `窗口列表` 会实时枚举 Claude 前台窗口；`切换到窗口1` 会把后续 `继续`、`停止` 和普通文本固定发送到该窗口。
- `新窗口继续` 会新开前台窗口并复用上一会话上下文；`新窗口运行 <任务>` 会新开前台窗口并使用新上下文。
- 单独发送 `前台运行` 时，bot 会只打开一个可接管的 Claude 前台窗口，不再默认附带 `继续` 指令。
- `前台运行` 会严格沿用当前飞书会话的权限模式；如果当前模式是 `bypassPermissions`，机器人会自动补齐 `--dangerously-skip-permissions`，避免前台窗口停在本机危险模式确认页。
- 单独发送 `前台继续` 时，bot 会把当前会话直接带到前台并继续上一轮。
- `前台继续` / `前台运行` 仅作为旧入口兼容；日常使用优先发送 `新窗口继续` / `新窗口运行 <任务>`。
- 如果你刚切了 `权限 <模式>` 或 `模型 <模型名>`，机器人会记住新配置，但不会强制关闭当前窗口；当前窗口继续按旧参数运行，新配置只会在下次新开 Claude 窗口时生效。
- 后台相关命令仅保留兼容提示和内部能力，日常验证不要再把后台运行作为主路径。
- `前台上一个` 或 `前台按键 shift+tab` 会直接把 `Shift+Tab` 送进当前 Claude 前台窗口，适合你人在手机上时远程切回 Claude TUI 的上一个交互区域。
- 为避免飞书移动端偶发的重复投递，`截图 claude`、`截图 桌面`、`前台上一个`、`前台按键 ...` 这类控制命令现在带有短时间去重；同一条命令在极短时间内重复到达时，机器人只执行一次。
- 后台执行触发真实 `PermissionRequest` 时，助手机器人会发送中文授权消息，并在首条消息里直接带上当前待授权项目清单；回复 `授权` 可重新查看清单。
- 单独回复 `同意` 会按当前约定同意全部待授权；如需只处理某一项，回复 `同意 1`、`拒绝 1`、`同意 2`、`拒绝 2`。
- 多条待审批时也可以回复 `全部授权` / `全部同意` / `全部拒绝`；其中 `全部授权` 和 `全部同意` 都表示放行当前全部待授权。
- 授权消息会展示具体动作内容，例如 Edit 的文件、原内容、新内容；Bash 会展示中文动作、风险提示、`Bash 命令`、命令正文和执行方式；AskUserQuestion 会展示问题、选项和单选/多选方式；不会要求你复制长编号。
- 前台可接管窗口里如果是你手动继续操作，`PermissionRequest` 现在也会优先按工作目录和托管会话状态反推回当前飞书聊天，不再因为缺少环境变量就只弹本地授权框。
- 前台会话完成一轮时，Claude 的 `Stop` hook 会把 `last_assistant_message` 作为“结果摘要”回发到飞书，并把状态切到“本轮已完成，等待继续下一轮”。
- 如果前台会话这一轮因 Claude/API 错误结束，`StopFailure` hook 会把失败摘要回发到飞书，避免手机端一直停留在“前台执行中”。
- 如果 Claude 没有挂起原进程、而是直接返回“需要授权”文本，bot 会把会话状态切到 `waiting_auth`，此时你仍然可以直接回复 `同意` / `拒绝`，机器人会保留上下文并自动续跑。
- 任务开始、完成、失败、停止时，bot 会在当前飞书会话内主动返回时间信息，包括开始时间、结束时间和总耗时。
- `状态` / `/status` 会优先展示中文状态，例如“等待授权后继续”“等待继续下一轮”，同时保留待处理动作提示，便于在手机上直接判断下一步该回什么。
- 如果前台 Claude 窗口还在线，`状态` 不会再把它误显示成空闲；当机器人无法读取你在电脑里手动输入的细节时，会明确提示“前台会话在线，可能正在人工操作”。
- 如果状态文件因为旧逻辑或异常重启丢失了 `foreground_pid`，机器人现在会按工作目录和 launcher 特征自动认回仍在运行的前台 Claude 窗口，避免 `前台继续`、`截图 claude`、`状态` 再误开新窗口或误报空闲。
- 任务开始、结束、授权等待、停止等通知会统一附带“可直接回复”提示，尽量减少你手动回忆命令词。
- `目录 <别名>` 支持先走 `cwd_aliases` 映射，再切换到对应的本机目录，适合在手机上快速切项目。
- `运行 <任务>` / `继续 <指令>` 现在默认都走后台执行，并由助手机器人自动回传结果摘要。
- 只要你没有主动回复 `停止`，当前聊天就会一直沿用同一个 Claude 会话；`运行 <任务>` 不是新建会话，而是给当前会话下达一条明确执行指令。
- 当当前聊天里已经托管了 Claude 会话时，`运行 <任务>` 和未匹配前缀的自然语言会自动沿用这个会话继续执行；如果前台窗口已经打开，这些命令会优先发送到该前台窗口。显式 `后台运行 <任务>` / `后台继续 <指令>` 仍然保留后台执行。
- 如果命令是机器人送进前台窗口的，状态会显示为“前台执行中”；如果你直接回到电脑前手动继续，机器人会把会话展示为“前台会话在线，可能正在人工操作”，但不会伪造窗口内的实时进度。
- `任务已开始` 通知里的“类型”会区分成“当前会话执行指令”“当前会话追加指令”“当前会话直接继续”，方便在飞书里快速判断这一轮的触发方式。
- `前台运行 [指令]` / `前台继续 [指令]` 会在本机弹出或复用可见 Claude 窗口；如果当前会话已经登记了可接管窗口，后续 `前台继续` 会优先把命令送入原窗口，不会重复新开。
- 除非你主动回复 `停止`，否则任务完成、失败、授权等待后，当前聊天的 Claude 会话都不会自动释放。
- `继续` 更适合“无需补充说明，直接沿着上一轮往下做”的场景；`运行 <任务>` 更适合“仍在同一会话里，但我要明确指定本轮要执行什么”的场景。
- `权限 <模式>` 会切换当前飞书会话后续 Claude 进程的授权模式，支持 `acceptEdits`、`auto`、`bypassPermissions`、`default`、`dontAsk`、`plan`，也支持中文别名如 `权限 接受编辑`、`权限 计划`。
- `模型 <模型名>` 会切换当前飞书会话后续 Claude 进程的模型，可以使用 `opus`、`sonnet`、`haiku` 或完整模型名；回复 `模型 跟随配置` 可取消会话级覆盖。
- `状态` 会尝试返回后台任务最近输出摘要；如果 Claude CLI 当前还没有吐出 stdout/stderr，机器人会明确提示“当前还没有可抓取的输出片段”，不会伪造进度。
- `状态` 会优先读取 Claude 自己写入的 JSONL 会话日志，例如 `%USERPROFILE%\.claude\projects\<项目目录编码>\*.jsonl`，从最新 assistant 消息中提取摘要。
- 如果最近 assistant 文本包含 `Round `、`本轮迭代已完成`、`进入下一轮检查` 等标记，机器人会从最近标记处截取，避免旧轮次内容占满状态。
- 如果不是循环式 Round 任务、没有这些标记，机器人会直接返回最近 assistant 文本，不会因为缺少标记就报“暂无摘要”。
- 通过新版机器人打开的前台窗口仍会把 PowerShell transcript 写入 `outputs\feishu-claude-v2\logs\foreground\`，但 transcript 只作为 Claude JSONL 不可读时的兜底来源。
- 前台观察器会优先读取 Claude JSONL 中的完成标记，在真实 Stop hook 缺失时主动补发完成通知；transcript 完成标记识别仅作为兜底。
- 已经在旧配置下打开的 Claude 前台窗口可能仍缓存旧 hook 命令；如果完成通知仍缺失，建议关闭旧 Claude 窗口后通过飞书重新打开。
- `状态` 只返回文字状态，不再自动追加图片，避免普通状态查询刷出大图。
- `截图` / `截图 claude` / `截图claude` / `/screenshot` 会截取前台 Claude 窗口，适合查看 Claude 当前卡在哪一步。
- 如果当前没有 Claude 前台窗口，`截图 claude` 会直接返回中文提示，不再把底层 PowerShell 异常和乱码堆栈发回飞书。
- `截图 桌面` / `截图桌面` / `/screenshot desktop` 会截取当前主屏幕桌面，适合远程确认电脑真实画面；该截图可能包含其他窗口和隐私信息，使用前请留意当前屏幕内容。

## 5.1 单窗口约束

- 当前设计目标是：一个飞书会话优先绑定一个 Claude 前台窗口。
- 只要这个前台窗口还活着，飞书里的自然语言、`前台继续`、`前台上一个`、`截图 claude` 都会优先操作这同一个窗口。
- 机器人不会因为收到普通消息就悄悄再开第二个前台 Claude 窗口。
- `权限 <模式>`、`模型 <模型>` 属于 Claude 进程启动参数，不能在已运行窗口里热切换；因此机器人只记录配置变更，不会强制把当前窗口关掉重开。

## 5.2 本机回归测试

可用下面命令执行本机模拟回归：

```powershell
python "D:\code\codex\integrations\feishu-claude-v2\tests\simulate_feishu_bot_regression.py"
```

说明：

- 该脚本不会真实给飞书发消息，也不会真的操作你当前窗口。
- 它会在本机模拟飞书消息链路，验证：
  - 自然语言是否复用同一个前台窗口
  - `前台上一个` 是否发往同一个窗口
  - `截图 claude` 是否复用同一个窗口 PID
  - 切权限后是否只标记“下次新窗口生效”
  - `帮助` 是否包含新增命令
  - `状态` 是否正确提示“当前窗口仍在使用旧参数”

如需验证 Claude Hook 相关通知链路，可执行：

```powershell
python "D:\code\codex\integrations\feishu-claude-v2\tests\simulate_feishu_hook_regression.py"
```

说明：

- 该脚本不会真实调用飞书接口，也不会阻塞等待真实授权回复。
- 它会在本机模拟并验证：
  - `PermissionRequest` 能否把手动前台窗口路由回正确飞书会话
  - `Stop` 完成通知是否保留托管会话，便于继续下一轮
  - `StopFailure` 失败通知是否保留托管会话，便于继续恢复
  - 授权允许/拒绝的 Hook JSON 结构是否正确

## 6. 状态与日志

- 根目录：
  - `D:\code\codex\outputs\feishu-claude-v2`
- 状态文件：
  - `D:\code\codex\outputs\feishu-claude-v2\state\feishu-claude-bot-state.json`
  - `D:\code\codex\outputs\feishu-claude-v2\state\feishu-claude-bot-approvals.json`
- 日志文件：
  - `D:\code\codex\outputs\feishu-claude-v2\logs\feishu-claude-bot.log`
  - `D:\code\codex\outputs\feishu-claude-v2\logs\feishu-claude-autostart.log`
  - `D:\code\codex\outputs\feishu-claude-v2\logs\feishu-claude-permission-hook.log`
  - `D:\code\codex\outputs\feishu-claude-v2\logs\feishu-claude-turn-hook.log`
  - `D:\code\codex\outputs\feishu-claude-v2\logs\foreground\`
- 临时产物目录：
  - `D:\code\codex\outputs\feishu-claude-v2\temp\launchers`
  - `D:\code\codex\outputs\feishu-claude-v2\temp\screenshots`

说明：

- 当前这套输出目录遵循“自动生成产物按主题分层、按用途分目录、按命名语义有序排放”的用户级规则。
- `temp\launchers` 和 `temp\screenshots` 现在属于自动清理目录。
- 一次性的前台 launcher 会在本轮前台命令返回后尽量自删。
- `截图 claude` / `截图 桌面` 生成的本地 PNG 在成功发送到飞书后会立即删除。
- 机器人启动时还会顺手清理这两个目录里超过 12 小时的遗留临时文件。
- `feishu-claude-bot.log` 会在超过 1 MB 时自动轮转，保留最近 3 份备份。
- `feishu-claude-permission-hook.log`、`feishu-claude-turn-hook.log` 会在超过 512 KB 时自动轮转，保留最近 3 份备份。
- `feishu-claude-bot-state.json`、`feishu-claude-bot-approvals.json`、各类 `.log` 仍会保留，用于会话续跑和排障。
- `logs\foreground\` 下的 transcript 用于前台摘要和排障，可能包含 Claude 窗口可见内容；如涉及敏感信息，可在确认不需要排障后清理旧文件。

快速查看日志：

```powershell
Get-Content -LiteralPath "D:\code\codex\outputs\feishu-claude-v2\logs\feishu-claude-bot.log" -Tail 100
```

如果要排查“截图/权限/前台继续”为什么走错分支，优先看以下新日志关键词：

- `recv ... raw_repr=... content_repr=...`
  - 用于确认飞书实际发来的原文里是否混入了零宽字符、特殊空格或菜单附加内容。
- `preprocess enter ... key=...`
  - 用于确认消息是否进入了机器人入口层预分流。
- `preprocess screenshot kind=...`
  - 用于确认截图命令是否在入口层被成功拦截。
- `foreground rebound ...`
  - 用于确认机器人是否已经自动认回当前仍在运行的前台 Claude 窗口。

如果日志里只出现了：

- `recv ...`

但没有出现：

- `preprocess enter ...`

同时本机又真的新开了 `claude.exe --print ...`，这通常说明不是当前飞书机器人主进程在执行，而是机器上还残留了另一条旧的本地桥接/测试进程在偷跑命令。此时应优先排查异常的 `python.exe -` 常驻进程，而不是继续只盯 `feishu_claude_bot.py`。

查看状态：

```powershell
Get-Content -LiteralPath "D:\code\codex\outputs\feishu-claude-v2\state\feishu-claude-bot-state.json" -Raw
```

## 7. 配置项说明

- `app_id`
  - 飞书应用的 App ID。
- `app_secret`
  - 飞书应用的 App Secret。
- `claude_path`
  - 本机 `claude.exe` 的绝对路径。
- `default_cwd`
  - 默认工作目录。
  - 新聊天或未切换目录时，Claude 会在这个目录下执行。
- `cwd_aliases`
  - 目录别名映射表。
  - 例如 `目录 5a`、`目录 主数据` 会先映射到配置里的真实绝对路径，再切换工作目录。
- `allowed_chat_ids`
  - 允许访问的聊天 ID 列表。
  - 设为 `[]` 表示不限制，任何能给机器人发消息的会话都可调用。
- `permission_mode`
  - 传给 Claude 的权限模式。
  - 兼容旧配置保留字段。
- `background_permission_mode`
  - 后台任务使用的权限模式。
  - 建议设为 `default`，这样权限请求才能通过飞书审批。
- `foreground_permission_mode`
  - 前台可接管任务使用的权限模式。
  - 设为 `bypassPermissions` 时，脚本现在会自动补齐 `--dangerously-skip-permissions`，避免前台继续卡在本机风险确认页。
  - 如果希望所有工具调用都改成飞书里授权，建议设为 `default`、`acceptEdits` 或其他非绕过模式。
- `default_model`
  - 默认模型别名或完整模型名。
  - 留空表示使用 Claude Code 自身默认模型；飞书里可用 `模型 <模型名>` 对当前会话临时覆盖。
- `additional_args`
  - 追加传给 Claude CLI 的额外参数列表。
  - 留空时表示只使用脚本内置参数。
- `state_path`
  - bot 运行状态文件路径。
- `log_path`
  - bot 本地日志文件路径。
- `approvals_path`
  - 飞书授权请求状态文件路径。
  - `PermissionRequest` hook 会等待这个文件里的 `approved/denied` 决定。
- `permission_hook_log_path`
  - `PermissionRequest` hook 的本地诊断日志路径。
  - 当前台手动会话没有收到飞书授权通知时，可优先查看这里确认 hook 是否成功反推出 chat_id。
- `turn_hook_log_path`
  - `Stop` / `StopFailure` hook 的本地诊断日志路径。
  - 当前台任务已在电脑上完成、但飞书没收到摘要时，可优先查看这里确认完成事件是否成功回传。
  - Claude hook 已配置为调用 `scripts\run_permission_hook.cmd` / `scripts\run_turn_hook.cmd` 包装脚本；不要改成 `cmd /c "python.exe" "hook.py"`，否则 stdin JSON 可能被 cmd 当作命令执行，导致飞书收不到授权或完成通知。
- `reply_max_chars`
  - 单条飞书回复最大字符数。
  - 超过后脚本会自动拆分多条发送。
- `pwsh_path`
  - 前台可接管模式使用的 PowerShell 7 可执行文件路径。
- `python_path`
  - Claude hook 自动拉起机器人和发送启动通知时使用的 Python 路径。
  - 建议指向已安装 `lark-oapi` 且能正常运行 bot 的 Python，例如 `python` 或你的虚拟环境解释器路径。

## 7.1 自动启动与诊断

- Claude `SessionStart` hook 会执行 `feishu_claude_bot_autostart.py`。
- 如果飞书机器人未启动，脚本会尝试后台拉起机器人。
- 每次 Claude 本地会话启动时，脚本会向最近一次活跃的飞书会话发送启动提示。
- 自动启动诊断日志：
  - `D:\code\codex\outputs\feishu-claude-v2\logs\feishu-claude-autostart.log`

## 8. 常见运维动作

修改配置文件后：

1. 保存 [feishu_claude_bot.v2.json](D:\code\codex\integrations\feishu-claude-v2\config\feishu_claude_bot.v2.json)
2. 执行“重启”命令
3. 查看日志确认出现新的 `bot starting`
4. 在飞书里发送 `我是谁` 或 `状态` 进行联调

如果飞书里发消息没有响应：

1. 先确认 bot 进程仍在运行
2. 查看日志里是否有新的 `recv chat=...`
3. 如果只有 `bot starting` 没有 `recv`
4. 优先检查飞书事件订阅、机器人消息权限、会话入口是否正确

## 9. 当前限制

- 当前最小版只支持文本消息
- `/continue` 依赖 Claude 在同一目录下的当前会话
- 当前一条聊天同一时刻只允许跑一个进程，但会额外保留一个“托管会话”
- 如果底层 Claude 会话本身已经损坏，bot 仍会保留飞书侧托管状态；这时通常需要回复 `停止` 后再用 `运行 <任务>` 重新进入新的执行链
- 当前未实现多任务并发队列和更细粒度的流式输出
- 未匹配到命令前缀的文本会默认按“当前会话中的执行指令”发送给 Claude
- Claude 窗口截图依赖 Windows 当前桌面可见窗口；机器人会优先使用当前会话登记的前台窗口，若窗口已关闭或电脑锁屏/休眠，则可能失败或截到黑屏。
- 桌面截图截取的是当前主屏幕，不限定 Claude 窗口。
