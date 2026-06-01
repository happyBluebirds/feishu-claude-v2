"""重导出公共网关模块，保持向后兼容。"""

from feishu_bot_common.feishu_gateway import (
    FeishuGateway,
    FeishuGatewayConfig,
    IncomingTextMessage,
    create_gateway,
)
