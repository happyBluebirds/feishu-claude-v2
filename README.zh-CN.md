# Feishu/Lark Bot Bridge for Local Claude Code

中文 | [English](README.md)

Feishu/Lark Bot Bridge for Local Claude Code 是一个本机 Claude Code 桥接工具：通过飞书或 Lark 机器人接收聊天命令，控制运行在本机 Windows 环境里的 Claude Code，并把授权请求、执行完成、失败通知、状态摘要和截图回传到聊天窗口。

它的核心定位是：通过飞书/Lark 控制本机 Claude Code 编码代理，同时让源码和执行状态继续保留在自己的机器上。

它适合“代码和工具都留在本机，飞书/Lark 作为远程控制入口”的工作方式。

## 主要能力

- 在飞书/Lark 私聊或群聊中控制本机 Claude Code。
- 支持新开 Claude Code 窗口、继续已有会话、管理和切换托管窗口。
- 将 Claude Code 的 `PermissionRequest`、`Stop`、`StopFailure` hook 事件转发到飞书/Lark。
- 在聊天中回复授权指令，无需回到本机操作。
- 支持查询状态摘要、发送 Claude 窗口截图或当前桌面截图。
- 使用飞书/Lark 长连接模式，不需要公网回调地址。
- 运行态 state、log、approval 和临时截图均放在仓库外或被 Git 忽略，避免误提交私有上下文。

## 目录结构

```text
app/       机器人主进程、命令路由、会话管理、飞书/Lark 网关
hooks/     Claude Code 授权、完成通知、自动拉起等 hook 脚本
scripts/   启动机器人和配置 hook 的 PowerShell/CMD 入口
config/    脱敏示例配置与本机配置说明
docs/      中文详细操作说明和维护说明
tests/     本机模拟回归脚本
```

## 快速开始

1. 创建飞书/Lark 企业自建应用，并开启机器人收发消息能力。
2. 安装 Python 依赖：

```powershell
python -m pip install -r requirements.txt
```

3. 复制示例配置：

```powershell
Copy-Item config\feishu_claude_bot.v2.example.json config\feishu_claude_bot.v2.json
```

4. 修改 `config/feishu_claude_bot.v2.json`：

- `app_id`
- `app_secret`
- `claude_path`
- `default_cwd`
- `allowed_chat_ids`
- `python_path`
- state 和 log 输出路径

5. 校验配置：

```powershell
.\scripts\start-feishu-claude-bot.ps1 -ValidateOnly
```

6. 启动桥接服务：

```powershell
.\scripts\start-feishu-claude-bot.ps1
```

## 常用命令

常用命令包括：

- `帮助`
- `新窗口运行 <任务>`
- `新窗口继续`
- `窗口列表`
- `切换到窗口1`
- `继续`
- `状态`
- `截图 claude`
- `截图 桌面`
- `目录 <路径>`
- `权限 default`

也支持英文命令别名。完整命令和运行说明见 [docs/README.md](docs/README.md)。

## 安全说明

- 不要提交 `config/feishu_claude_bot.v2.json`，它包含真实飞书/Lark 凭据和本机路径。
- 仓库内的示例配置已经脱敏，只保留占位值。
- 运行日志、状态文件、审批队列和截图可能包含私有项目上下文，已通过 `.gitignore` 排除。
- 在共享工作空间使用前，请先限制 `allowed_chat_ids`。

## 文档入口

- [English README](README.md)
- [中文详细说明](docs/README.md)
- [维护说明](docs/MAINTENANCE.md)
- [项目索引](INDEX.md)
