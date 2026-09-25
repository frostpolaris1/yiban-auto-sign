# -*- coding: utf-8 -*-
"""SMTP 邮箱通知模块（A 线：管理员告警邮件；B 线：用户签到结果邮件）。

通过 .env / 环境变量（YIBAN_MAIL_*）配置，仅用 Python 标准库 smtplib 发送，零第三方
依赖。分层：`config` 读配置与判定通道三态，`layout` 排版正文（一份声明出纯文本/HTML/
推送三版；它不判通道、不决定发不发，但**在发送路径上**——`transport._send` 每发一封都
现调 `layout.as_body` 排版），`transport` 投递（主备 failover，并 import 上面两层）。
正文入参接受普通字符串（行为与改版前逐字一致）或 `layout.Mail`（改发 multipart/alternative）。设计原则：

- 不配置 = 不启用：YIBAN_MAIL_ENABLE 未开启或配置不完整时静默跳过，不影响现有功能；
- 静默失败：发送异常只记日志，绝不抛出（告警/通知失败不能拖累签到主流程）；
- 日志脱敏：授权码、完整发件地址不回显，收件人只出打码串；SMTP 异常**只记粗分类**
  （认证失败 / 发送被拒 / 连接失败 / 其他失败），刻意不回显 `type(e).__name__`——原始类型名
  会把"端口是否开放、是否超时、域名能否解析"写进管理员可读日志（细节见 `transport._classify_send_error`）。
"""

# 公共 API 显式转发（转发的是同一对象）。
# 包内的 import 边**只有两条**，都出自 `transport.py` 的 `from . import config, layout`：
#   transport → config   （`is_enabled` / `smtp_list` / `check_smtp_host`，另在两处
#                         发送日志里直呼 config 的私有名 `_mask_addr`）
#   transport → layout   （`_send` 里 `layout.as_body(text)` 排版正文）
# `config` 与 `layout` 都不 import 包内其它模块（config 只取 yiban.infra /
# yiban.notify.config，layout 只取 yiban.clock），故三层无环；transport 是唯一的汇聚点。
from .config import (  # noqa: F401
    admin_notify_enabled,
    admin_recipients,
    get_config,
    is_enabled,
    logger,
    smtp_channel_state,
    smtp_list,
)
from .transport import send_admin_alert, send_user  # noqa: F401
