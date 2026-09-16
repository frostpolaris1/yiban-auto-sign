# -*- coding: utf-8 -*-
"""兼容壳：`import notify` 的旧导入路径继续可用（实现已拆入 `yiban/notify/`）。

两条转发纪律（都为"行为零变化"服务）：

1. **逐名转发**原模块的模块级名字全集（含下划线名），转发的是同一对象——函数、常量与
   可变状态字典，`notify._general_daily is yiban.notify.ledger._general_daily` 这类
   身份断言仍成立，测试对内部名的引用无需改动。本文件所有导入都是再导出，故逐条标注
   `noqa: F401`（`X as X` 的冗余别名形式会被 isort 拆成一行一条，不可读）。
2. **属性写入转发**（`_ForwardingModule`）：既有测试以 `notify.<名字> = 替身` 打桩
   （如 `notify._daily_today` / `notify._send_custom`），若不转发，打桩只会落在壳模块上、
   实现模块内部仍调真名——即"打桩静默失效"。

新代码请直接 `from yiban.notify import ...`；本壳只为部署面与既有调用方保留。
"""
import ipaddress  # noqa: F401  # 转发原文的模块级别名（同一对象），供旧调用方与测试使用
import json  # noqa: F401
import logging  # noqa: F401
import os
import sys
import threading  # noqa: F401
import time  # noqa: F401
import types
from contextlib import (
    contextmanager,  # noqa: F401
    suppress,  # noqa: F401
)
from urllib.parse import urlparse  # noqa: F401

import requests  # noqa: F401

# 引导：以「文件路径」方式运行时 sys.path[0] 是 scripts/，仓库根不在其中——共享代码在
# yiban/ 下，故入口先补仓库根（与 scripts/signin.py 同口径；包化完成后统一收口）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yiban.infra import (
    account_crypto,  # noqa: F401  # 原文的裸模块名导入 → 包路径
    env_io,  # noqa: F401
    locks,  # noqa: F401
)
from yiban.notify import config as _impl_config
from yiban.notify import ledger as _impl_ledger
from yiban.notify import transport as _impl_transport
from yiban.notify.config import (  # noqa: F401  # 逐名转发，见模块说明第 1 条
    _PREFIX,
    DEFAULT_COOLDOWN,
    DEFAULT_DAILY_MAX,
    DEFAULT_LOGINFAIL_DAILY_MAX,
    DEFAULT_URGENT_DAILY_MAX,
    LOGINFAIL_DAILY_MAX_KEY,
    _env_int,
    _env_path,
    _env_str,
    _host_of,
    _mask_secret,
    _read_env_file,
    get_config,
    get_secret,
    is_configured,
    is_safe_url,
    logger,
)
from yiban.notify.ledger import (  # noqa: F401  # 逐名转发，见模块说明第 1 条
    _LEDGER_IDS,
    _LEDGERS,
    BudgetTicket,
    _archive_corrupt_state_file,
    _consume_budget_locked,
    _consume_daily_budget,
    _daily_limit,
    _daily_remaining,
    _daily_today,
    _ensure_ledger_structure,
    _general_daily,
    _ledger,
    _ledger_file_lock,
    _ledger_path,
    _load_ledger_file,
    _load_throttle_file,
    _log_exhaustion_warning,
    _loginfail_daily,
    _loginfail_daily_limit,
    _mark_exhausted_locked,
    _merge_ledger_into_disk,
    _pop_notice_locked,
    _prune_throttle_entries,
    _refund_budget_locked,
    _refund_daily_budget,
    _roll_locked_inner,
    _save_ledger_file,
    _save_throttle_file,
    _skip_log_lock,
    _skip_logged,
    _state_dir,
    _state_file_lock,
    _throttle_lock,
    _throttle_path,
    _throttle_ts,
    _unmark_exhausted_locked,
    _urgent_daily,
    _with_ledger_locked,
    budget_exhausted_today,
    pop_exhaustion_notice,
)
from yiban.notify.transport import (  # noqa: F401  # 逐名转发，见模块说明第 1 条
    DEFAULT_URL_TIMEOUT,
    MAX_TITLE_CHARS,
    SERVERCHAN_TURBO_HOST,
    SKIP_LOG_WINDOW,
    _log_skip,
    _send_custom,
    _send_serverchan,
    _throttle_due,
    send,
    send_test,
)


class _ForwardingModule(types.ModuleType):
    """壳模块：属性写入同时落到真正持有该名字的实现模块（见模块说明第 2 条）。

    模块级赋值走 `__dict__` 直写、不触发 `__setattr__`，故本转发只影响外部打桩
    （`setattr`），不影响本文件自身的名字绑定。
    """

    def __setattr__(self, name, value):
        if not name.startswith("__"):
            for mod in (_impl_config, _impl_ledger, _impl_transport):
                if hasattr(mod, name):
                    setattr(mod, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _ForwardingModule
