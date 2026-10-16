# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""批 B 搬迁的等价性守卫：旧坐标是转发壳，取到的是**同一实现对象**。

标签：M · 架构分层与基础设施
覆盖：批 B 把下列模块搬进目标层（目标架构契约 `docs/dev/architecture-target.md`
    第 3 节），旧坐标留转发壳。本文件钉两条不变式：
    ① **一份实现**：旧坐标与新坐标取到的是**同一个模块对象**（`is`）。
       不是"输出看着一样"——那会漏掉"两份实现，改一处不同步另一处"。
    ② **旧坐标可打桩**：`mock.patch("yiban.store.db.X")` 与
       `mock.patch("yiban.adapters.persistence.db.X")` 打到同一个对象上。
       这是本批最大的技术风险面（`tests/` 里约 1,491 处打桩，搬调用点后按旧模块名
       打桩会**静默失效**）。
对应实现：`yiban/clock.py` 等 14 个模块壳 ＋ `yiban/store/__init__.py` 包壳。
关键断言：进程内 `importlib.import_module(旧)` `is` `importlib.import_module(新)`；
    `mock.patch` 经旧坐标后，实现模块的属性就是那个替身。
依赖：只做 import 与 mock；不碰网络、不碰 DB、不读磁盘。
已知边界：只在**进程内**判对象同一；`web/**`、`scripts/**`、`tests/**` 的旧坐标读者
    不重测（它们的用例各自覆盖行为）。本文件不重测被搬模块的行为。
"""
import importlib
import unittest
import unittest.mock as mock

#: 旧坐标 → 新坐标（批 B 全部模块级壳）。
SHELLS = (
    ("yiban.clock", "yiban.core.clock"),
    ("yiban.status", "yiban.core.status"),
    ("yiban.window", "yiban.core.window"),
    ("yiban.masking", "yiban.core.masking"),
    ("yiban.engine.hrw", "yiban.core.hrw"),
    ("yiban.engine.round", "yiban.app.round"),
    ("yiban.engine.runner", "yiban.app.runner"),
    ("yiban.engine.probe", "yiban.app.probe"),
    ("yiban.engine.alerts", "yiban.adapters.notifier.report"),
    ("yiban.config_loader", "yiban.adapters.config.loader"),
    ("yiban.state_gc", "yiban.adapters.statefiles.state_gc"),
    ("yiban.cred_state", "yiban.adapters.statefiles.cred_state"),
    ("yiban.infra.env_io", "yiban.adapters.config.env_io"),
    ("yiban.infra.account_crypto", "yiban.adapters.config.account_crypto"),
)

#: 包壳：旧包坐标 → 新包坐标。子模块也要落到同一对象。
STORE_SUBMODULES = (
    "accounts", "audit_chain", "claims", "cleanup", "clock_meta", "connection",
    "db", "events", "migrations", "purge_guard", "queue_store", "run_events",
    "session_cache", "time_prefs", "tracking", "users", "verify_jobs",
)

#: 旧坐标（字符串式打桩面）→ 必须能被打桩的属性名（都在实现里存在）。
PATCH_PROBES = (
    ("yiban.core.clock", "__name__"),
    ("yiban.store.db", "__name__"),
    ("yiban.store.queue_store", "__name__"),
    ("yiban.engine.round", "__name__"),
    ("yiban.infra.env_io", "__name__"),
)


class SingleImplementationTest(unittest.TestCase):
    """① 旧坐标 = 实现（同一对象）。"""

    def test_module_shells_are_the_same_object(self):
        for old, new in SHELLS:
            with self.subTest(old=old, new=new):
                o = importlib.import_module(old)
                n = importlib.import_module(new)
                self.assertIs(
                    o, n,
                    f"{old} 不是 {new} 的同一对象——那是第二份实现"
                    f"（改一处不会同步另一处；旧坐标的打桩会静默失效）")

    def test_store_package_shell_maps_every_submodule(self):
        """`yiban.store.<sub>` 与 `yiban.adapters.persistence.<sub>` 是同一对象。"""
        for sub in STORE_SUBMODULES:
            with self.subTest(sub=sub):
                o = importlib.import_module(f"yiban.store.{sub}")
                n = importlib.import_module(f"yiban.adapters.persistence.{sub}")
                self.assertIs(o, n, f"yiban.store.{sub} 不是实现模块的同一对象")

    def test_store_package_attributes_resolve(self):
        """`from yiban.store import db` 与属性访问都要取到实现。"""
        import yiban.adapters.persistence.db as impl_db
        import yiban.store as store
        self.assertIs(store.db, impl_db)

    def test_shells_live_at_the_old_path_but_point_at_the_impl(self):
        """壳文件在旧路径；实现在新路径（证明"搬家"而不是"复制"）。"""
        import os
        for old, new in SHELLS:
            with self.subTest(old=old):
                impl = importlib.import_module(new)
                self.assertTrue(os.path.isfile(impl.__file__))
                self.assertIn(os.path.join(*new.split(".")), impl.__file__,
                              f"{new} 的 __file__ 不在新坐标下（{impl.__file__}）")


class OldCoordinatePatchingTest(unittest.TestCase):
    """② 经旧坐标打桩，命中实现——否则搬调用点后打桩静默失效。"""

    def test_patch_through_old_coordinate_hits_the_implementation(self):
        for old in (o for o, _ in PATCH_PROBES):
            with self.subTest(old=old):
                impl = importlib.import_module(old)
                sentinel = object()
                with mock.patch(old + ".__name__", sentinel):
                    self.assertIs(
                        impl.__name__, sentinel,
                        f"经 {old} 打桩没有命中实现——旧坐标的打桩会静默失效"
                        "（这正是'搬家后打桩脱靶'的缺陷形状）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
