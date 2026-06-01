# v2 配置说明

v2 是当前生效版本。旧版 v1 配置目录已失效并冻结，不要再从 v1 启动或修改真实运行配置。

`feishu_claude_bot.v2.example.json` 是模板，不包含真实飞书凭据。

实际运行时使用：

```text
config/feishu_claude_bot.v2.json
```

注意事项：

- 如果从 v1 配置复制字段，只能复制必要凭据和 Claude 路径；复制后必须检查输出路径是否全部指向当前项目约定的本机输出目录。
- v1 长连接机器人不应再运行；如果发现 v1 进程或 hook 配置仍存在，应迁移到 v2。
- 修改配置后先运行 `scripts/start-feishu-claude-bot.ps1 -ValidateOnly`。


