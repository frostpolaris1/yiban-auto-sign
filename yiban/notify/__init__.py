# -*- coding: utf-8 -*-
"""Webhook 推送组件（Server酱 / 自定义 URL）。

配置与仓库代码解耦（存 .env，gitignored；密钥字段 AES-GCM 加密），键名与含义见
`.env.example` 的 YIBAN_NOTIFY_* 段。分三层：`config` 读配置并判定通道可用性、
`ledger` 管每日额度与节流状态（磁盘 + 文件锁，跨 web/signin 进程共享）、
`transport` 负责发送与失败退还。

设计原则：不配置 = 不启用；发送异常只记日志、绝不抛出（不拖累签到主流程）；每日额度
按 general / urgent / login_fail 三本独立账互不挤占；额度只在发送成功后才最终扣减，
失败凭占用凭证退还；自定义 URL 走 SSRF 白名单。
"""

# 公共 API 显式转发（依赖方向 config ← ledger ← transport；转发的是同一对象）。
from .config import (  # noqa: F401
    DEFAULT_COOLDOWN,
    DEFAULT_DAILY_MAX,
    DEFAULT_LOGINFAIL_DAILY_MAX,
    DEFAULT_URGENT_DAILY_MAX,
    LOGINFAIL_DAILY_MAX_KEY,
    get_config,
    get_secret,
    is_configured,
    is_safe_url,
    logger,
)
from .ledger import BudgetTicket, budget_exhausted_today, pop_exhaustion_notice  # noqa: F401
from .transport import (  # noqa: F401
    DEFAULT_URL_TIMEOUT,
    MAX_TITLE_CHARS,
    SERVERCHAN_TURBO_HOST,
    SKIP_LOG_WINDOW,
    send,
    send_test,
)
