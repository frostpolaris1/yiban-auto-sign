# -*- coding: utf-8 -*-
"""兼容壳：`import mailer` 的旧导入路径继续可用（实现已拆入 `yiban/mail/`）。

两条转发纪律（都为"行为零变化"服务）：

1. **逐名转发**原模块的模块级名字全集（含下划线名），转发的是同一对象——函数、常量与
   一次性告警旗标，既有调用方（web/signin）对 `mailer.<名字>` 的引用无需改动。本文件
   所有导入都是再导出，故逐条标注 `noqa: F401`（`X as X` 的冗余别名形式会被 isort
   拆成一行一条，不可读）。
2. **属性写入转发**（`_ForwardingModule`）：既有测试以 `mailer.<名字> = 替身/复位值`
   打桩（如 `mailer._get` / `mailer.is_enabled` / `mailer._entry_port_warned = False`），
   若不转发，打桩只会落在壳模块上、实现模块内部仍调真名——即"打桩静默失效"。

新代码请直接 `from yiban.mail import ...`；本壳只为部署面与既有调用方保留。
"""
import json  # noqa: F401  # 转发原文的模块级别名（同一对象），供旧调用方与测试使用
import logging  # noqa: F401
import os
import smtplib  # noqa: F401
import ssl  # noqa: F401
import sys
import types
from email.header import Header  # noqa: F401
from email.mime.text import MIMEText  # noqa: F401

# 引导：以「文件路径」方式运行时 sys.path[0] 是 scripts/，仓库根不在其中——共享代码在
# yiban/ 下，故入口先补仓库根（与 scripts/signin.py 同口径；包化完成后统一收口）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yiban.infra import (
    account_crypto,  # noqa: F401  # 原文的裸模块名导入 → 包路径
    env_io,  # noqa: F401
)
from yiban.mail import config as _impl_config
from yiban.mail import transport as _impl_transport
from yiban.mail.config import (  # noqa: F401  # 逐名转发，见模块说明第 1 条
    _PREFIX,
    _get,
    _mask_addr,
    _port_warned,
    _read_env_file,
    _smtps_enc_decrypts_to_list,
    admin_notify_enabled,
    admin_recipients,
    get_config,
    is_enabled,
    logger,
    smtp_channel_state,
    smtp_list,
)
from yiban.mail.transport import (  # noqa: F401  # 逐名转发，见模块说明第 1 条
    _entry_port_warned,
    _send,
    send_admin_alert,
    send_user,
)


class _ForwardingModule(types.ModuleType):
    """壳模块：属性写入同时落到真正持有该名字的实现模块（见模块说明第 2 条）。

    模块级赋值走 `__dict__` 直写、不触发 `__setattr__`，故本转发只影响外部打桩
    （`setattr`），不影响本文件自身的名字绑定。
    """

    def __setattr__(self, name, value):
        if not name.startswith("__"):
            for mod in (_impl_config, _impl_transport):
                if hasattr(mod, name):
                    setattr(mod, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _ForwardingModule
