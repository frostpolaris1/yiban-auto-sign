# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""兼容壳：`yiban.mail` → `yiban.adapters.notifier.mail`（仓库结构整理批 A4）。

**为什么有本件**
SMTP 邮件组件在批 A4 搬进适配器层
（目标架构契约 `docs/dev/architecture-target.md` 第 3 节）。
读者面里有 `web/`（`web/app.py`、`web/security.py`、`web/services/capacity.py`、
`web/services/channel_health.py`、`web/services/env_io.py`、`web/services/notify_mail.py`）。
`web/` 本批**不动**（该契约第 6 节的本批边界）。故旧坐标留转发壳。

**本件不是第二份实现**
本件不复制任何判据。属性访问与子模块都指向 `yiban.adapters.notifier.mail`
的**同一对象**。判据是对象同一，验证者＝`tests/test_notifier_relocation.py`。

**子模块为什么登记进 `sys.modules`**
同 `yiban/notify/__init__.py`：读者面里有 `import yiban.mail.layout` 与
`mock.patch("yiban.mail.transport.X")` 两种写法，登记同一对象后两者都落到位。

**退役条件**
读者面（`web/**`、`scripts/backup_sentinel.py`、`tests/**`）改到新坐标后删除本件。
工单见本批交付报告。
"""
import importlib as _importlib
import sys as _sys

from yiban.adapters.notifier import mail as _impl

_SUBMODULES = ("config", "layout", "transport")

for _name in _SUBMODULES:
    _sys.modules[__name__ + "." + _name] = _importlib.import_module(
        "yiban.adapters.notifier.mail." + _name)


def __getattr__(name):
    """把属性访问转发给实现模块（同一个对象）。"""
    try:
        return getattr(_impl, name)
    except AttributeError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
            f"（本件是转发壳，实现里也没有这个名字）") from None


def __dir__():
    """`dir()` 覆盖实现模块的全部名字。

    并集里另有本壳自己的私有名（`_impl` / `_sys` / `_importlib`）。
    故口径是"同面（外加壳自身私有名）"，不是逐名相等。
    """
    return sorted(set(globals()) | set(dir(_impl)))
