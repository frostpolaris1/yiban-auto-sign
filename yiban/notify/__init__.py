# -*- coding: utf-8 -*-
"""Webhook 推送组件（Server酱 / 自定义 URL）。

配置与仓库代码解耦（存 .env，gitignored；密钥字段 AES-GCM 加密）：
- YIBAN_NOTIFY_TYPE            : serverchan / custom / 空 = 不启用
- YIBAN_NOTIFY_SECRET_ENC      : 加密后的密文 JSON（serverchan=SendKey；custom=URL）
- YIBAN_NOTIFY_COOLDOWN        : 同类型告警节流秒数（默认 60，0=关闭）
- YIBAN_NOTIFY_URGENT_ONLY     : 1 = 仅推送重要（urgent）告警；0/空 = 全部推送
- YIBAN_NOTIFY_DAILY_MAX       : 每日推送条数上限，语义收窄为「非紧急（general）账」
                                  （0=不限；默认 5，匹配 Server酱 免费版 5 条/天；
                                  超限后当日不再推送，邮件通道不受影响）
- YIBAN_NOTIFY_URGENT_DAILY_MAX: 「紧急（urgent）账」每日上限（0=不限；默认 3）

兼容旧配置：未配置加密密文时回退明文 YIBAN_NOTIFY_URL（custom 语义），
旧部署迁移后无需手动改 .env。

防滥用（2026-08-29 被盗号滥用面加固）：
- 同类型告警节流：窗口内同标题只推一条（防盗号/异常反复触发刷爆 Server酱等
  第三方配额与管理员手机）；节流状态已持久化到磁盘
  （$YIBAN_STATE_DIR/notify-throttle.json，文件锁互斥）——web（常驻）与
  signin（cron 新进程）共享同一节流窗口，不再各持一份进程内节流表、各放行一条；
- 每日预算硬上限（拆成两本账）：非紧急推满 YIBAN_NOTIFY_DAILY_MAX、
  紧急推满 YIBAN_NOTIFY_URGENT_DAILY_MAX 后各自停手（邮件仍全量送达）。旧口径
  两类共用一份额度，未认证攻击者用「登录失败告警」（urgent）约 5 分钟即可烧掉
  当日全部额度，之后审计链异常等真告警在手机端全灭；
- 失败退还：额度在发送成功后才最终扣减，HTTP 异常 / 服务端非零 code / 白名单拒发
  一律退回，不浪费真告警的额度。退还按"占用时发出的凭证"执行——凭证带着账本标识与
  占用当日，因此跨日（23:59:59 占用、次日才失败）不会退到次日账上，管理员中途改
  上限也不会出现幻影退还 / 漏退；
- 耗尽可见：某本账耗尽时在**消耗动作内部当场**记 warning 并挂一次性告知标记
  （pop_exhaustion_notice，返回哪些账本耗尽；紧急 / 非紧急每日各一次），不等下一次
  尝试被拒才补——否则"最后一条恰好打满、当日再无新告警"时告知永远发不出去，那正是
  本档要治的静默失效。节流命中 / 额度耗尽 / 非紧急被过滤这三类静默跳过各有一行 info
  日志（同原因在窗口内只记一次），运维能区分"配置没生效"与"本来就没额度"；
- 仅重要告警：YIBAN_NOTIFY_URGENT_ONLY 开启后，非紧急（如用户日常改密、签到
  结果类）通知不推手机，把预算留给真正威胁系统/账号安全的事件；
- 检查服务端响应：Server酱 code!=0 / 非 JSON 一律记日志（配额耗尽、限频可见），
  不再"发出即成功"地静默失败；
- 自定义 URL 走 SSRF 白名单（https + 非回环/内网），与 web/signin 既有口径一致。

设计原则：不配置 = 不启用；发送异常只记日志、绝不抛出（不拖累签到主流程）。
"""

# 显式转发公共 API（依赖方向 config ← ledger ← transport；转发的是同一对象）。
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
