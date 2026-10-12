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
3. **属性删除同样转发**（`__delattr__`）：`mock.patch.object(壳, 名字, X)` 与
   `monkeypatch.delattr(壳, 名字)` 的撤销都走 `delattr`，且"删完 `hasattr` 是否还为真"
   决定要不要把原值 `setattr` 回来。壳的读取回落到实现模块，只删壳自己的条目**不够**
   ——回落会让 `hasattr` 仍为真、原值永不恢复，打桩静默残留（`_conn` 这类已移出实现
   模块 `__dict__` 的名字尤其如此）。故壳另记一份"已摘名"清单（`_shell_deleted`），
   删除后读取立刻失败，随后的恢复 `setattr` 移出清单并照常转发到实现模块。

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

# 经 `__delattr__` 从壳上摘掉的名字（下次 `setattr` 原值即移出）。不放在实现模块上：
# 壳是视图，"删除"的语义是从视图上摘掉，实现模块的名字不受损。
_shell_deleted = set()


class _ForwardingModule(types.ModuleType):
    """壳模块：读取、写入与删除都落到真正持有该名字的实现模块（见模块说明第 2/3 条与补充条）。

    模块级赋值/删除默认直写 `__dict__`、不触发魔术方法，故本转发只影响外部打桩
    （`setattr` / `delattr`），不影响本文件自身的名字绑定。
    """

    def __getattribute__(self, name):
        if name.startswith("__") and name.endswith("__"):
            return types.ModuleType.__getattribute__(self, name)
        if name in types.ModuleType.__getattribute__(self, "_shell_deleted"):
            raise AttributeError(
                f"module {types.ModuleType.__getattribute__(self, '__name__')!r} "
                f"has no attribute {name!r}"
            )
        impl = types.ModuleType.__getattribute__(self, "_impl")
        try:
            return getattr(impl, name)
        except AttributeError:
            return types.ModuleType.__getattribute__(self, name)

    def __setattr__(self, name, value):
        if not name.startswith("__"):
            types.ModuleType.__getattribute__(self, "_shell_deleted").discard(name)
            impl = types.ModuleType.__getattribute__(self, "_impl")
            if hasattr(impl, name):
                # 转发名只在实现模块上落地，壳**不留自己的副本**：副本会让
                # `mock.patch.object(壳, 名字, X)` 的 `get_original` 把该名字当成
                # "局部名"（读壳 `__dict__`、local=True），退出时按副本恢复——副本
                # 是上一次外部写入的值（可能是别的用例的连接），恢复即写错。
                setattr(impl, name, value)
                return
        types.ModuleType.__setattr__(self, name, value)

    def __delattr__(self, name):
        if name.startswith("__"):
            return types.ModuleType.__delattr__(self, name)
        impl = types.ModuleType.__getattribute__(self, "_impl")
        if not hasattr(impl, name):
            return types.ModuleType.__delattr__(self, name)
        types.ModuleType.__getattribute__(self, "_shell_deleted").add(name)
        self.__dict__.pop(name, None)


sys.modules[__name__].__class__ = _ForwardingModule
