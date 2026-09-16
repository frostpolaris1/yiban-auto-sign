# -*- coding: utf-8 -*-
"""SMTP 邮箱通知模块（A 线：管理员告警邮件；B 线：用户签到结果邮件）。

通过 .env / 环境变量（YIBAN_MAIL_*）配置，仅用 Python 标准库 smtplib 发送，
零第三方依赖。设计原则：

- 不配置 = 不启用：YIBAN_MAIL_ENABLE 未开启或配置不完整时静默跳过，不影响现有功能；
- 静默失败：发送异常只记日志，绝不抛出（告警/通知失败不能拖累签到主流程）；
- 日志脱敏：授权码、完整发件地址不回显，只记录异常类型与打码后的邮箱。
"""

# 显式转发公共 API（依赖方向 config ← transport；转发的是同一对象）。
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
