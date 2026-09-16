# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""兼容壳：`import db` 的旧导入路径继续可用（实现已迁入 `yiban/store/db.py`）。

两条转发纪律（都为"行为零变化"服务）：

1. **逐名转发**实现模块的模块级名字全集（含下划线名，如 `_conn_lock` / `_begin_immediate`），
   转发的是同一对象——函数、常量与可变状态字典，`db.get_conn is yiban.store.db.get_conn`
   这类身份断言仍成立，测试对内部名的引用无需改动。名字用程序化方式逐个拉取，不手抄
   ——手抄两百多个名字迟早与实现漂移。
2. **属性写入转发**（`_ForwardingModule`）：既有测试与调用方以 `db.<名字> = 替身` 打桩
   （如 `db.get_conn` / `db.audit` / `db._conn`），若不转发，打桩只会落在壳模块上、
   实现模块内部仍调真名——即"打桩静默失效"。

补充一条读取纪律：实现入包后，`web/app.py` 与 `yiban/**` 直接 `from yiban.store import db`，
于是同一进程里可能"一半读壳、一半读实现"。只转发写入挡不住这种不一致
（例如 `mock.patch.object(web.app.db, "audit")` 打在实现上，而壳里那份绑定仍是旧对象），
故本壳的**读取也回落到实现模块**（见 `_ForwardingModule.__getattribute__`）。

新代码请直接 `from yiban.store import db`；本壳只为部署面与既有调用方保留。
"""
import os
import sys
import types

# 引导：以「文件路径」方式运行时 sys.path[0] 是 scripts/，仓库根不在其中——共享代码在
# yiban/ 下，故入口先补仓库根（与 scripts/signin.py 同口径；包化完成后统一收口）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yiban.store import db as _impl


def _forward_all_names(impl):
    """把实现模块的模块级名字（含下划线名）逐个绑定到本壳，转发的是同一对象。

    跳过 dunder：`__doc__` / `__name__` / `__loader__` 等必须留在壳自己身上，
    否则 `import db` 拿到的模块元信息会指向实现模块的路径。
    """
    for name, value in list(vars(impl).items()):
        if name.startswith("__") and name.endswith("__"):
            continue
        globals()[name] = value


_forward_all_names(_impl)


class _ForwardingModule(types.ModuleType):
    """壳模块：读取与写入都落到真正持有该名字的实现模块（见模块说明第 2 条与补充条）。

    模块级赋值走 `__dict__` 直写、不触发 `__setattr__`，故本转发只影响外部打桩
    （`setattr`），不影响本文件自身的名字绑定。
    """

    def __getattribute__(self, name):
        if name.startswith("__") and name.endswith("__"):
            return types.ModuleType.__getattribute__(self, name)
        impl = types.ModuleType.__getattribute__(self, "_impl")
        try:
            return getattr(impl, name)
        except AttributeError:
            return types.ModuleType.__getattribute__(self, name)

    def __setattr__(self, name, value):
        if not name.startswith("__"):
            impl = types.ModuleType.__getattribute__(self, "_impl")
            if hasattr(impl, name):
                setattr(impl, name, value)
        types.ModuleType.__setattr__(self, name, value)


sys.modules[__name__].__class__ = _ForwardingModule
