# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""通知适配器搬家后的守卫（仓库结构整理批 A4）。

标签：M · 架构分层与基础设施
覆盖：本批把 `yiban/mail/` 与 `yiban/notify/` 搬到 `yiban/adapters/notifier/`
    （目标架构契约 `docs/dev/architecture-target.md` 第 3 节的演进映射表：
    `mail/` `notify/` → `adapters/notifier/`）。旧坐标留转发壳。本文件钉四条不变式：
    ① **新坐标在位**：两个组件本体在 `yiban/adapters/notifier/` 下。
    ② **旧坐标是壳，不是第二份实现**：旧坐标的每个公共名与新坐标是**同一对象**
       （`is`），子模块也同一（`yiban.notify.ledger is
       yiban.adapters.notifier.notify.ledger`）。判据是对象同一，不是"输出看着一样"。
    ③ **新坐标真能用**：从新坐标取配置与通道状态，判据与旧坐标逐值相同。
    ④ **告警编排没跟着搬**：`yiban/adapters/notifier/report.py` 留在原地
       （执行核冻结名单里的 `round` / `executor_v3` 都 import 它）。
对应实现：`yiban/adapters/notifier/mail/`、`yiban/adapters/notifier/notify/`、
    `yiban/mail/__init__.py` 与 `yiban/notify/__init__.py`（转发壳）。
关键断言：① ④ 读文件系统；② ③ 在进程内比对象同一与逐值相等。
依赖：只导入包，不发网络请求、不发邮件（配置缺失时两个组件都"不启用"）。
    公开面的**完整**清单由 `tests/test_infra_layer.py` 钉住，本文件不抄第二份。
"""
import importlib
import os
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW_MAIL = "yiban/adapters/notifier/mail"
NEW_NOTIFY = "yiban/adapters/notifier/notify"

#: 两个组件的子模块（旧坐标下必须也能 import，且指向同一对象）。
MAIL_SUBMODULES = ("config", "layout", "transport")
NOTIFY_SUBMODULES = ("config", "ledger", "transport")

#: 读者面用到的公共名（完整清单见 `tests/test_infra_layer.py`）。
MAIL_PUBLIC = ("send_admin_alert", "send_user", "get_config", "is_enabled",
               "smtp_list", "smtp_channel_state", "admin_recipients",
               "admin_notify_enabled", "logger")
NOTIFY_PUBLIC = ("send", "send_test", "get_config", "get_secret", "is_configured",
                 "is_safe_url", "budget_exhausted_today", "pop_exhaustion_notice",
                 "BudgetTicket", "logger")


class NotifierRelocationTest(unittest.TestCase):
    """① 新坐标在位；④ 告警编排没搬。"""

    def test_bodies_live_at_the_new_coordinate(self):
        for rel in (f"{NEW_MAIL}/__init__.py", f"{NEW_MAIL}/config.py",
                    f"{NEW_MAIL}/layout.py", f"{NEW_MAIL}/transport.py",
                    f"{NEW_NOTIFY}/__init__.py", f"{NEW_NOTIFY}/config.py",
                    f"{NEW_NOTIFY}/ledger.py", f"{NEW_NOTIFY}/transport.py"):
            with self.subTest(rel=rel):
                self.assertTrue(os.path.isfile(os.path.join(BASE, rel)), f"缺 {rel}")

    def test_bodies_are_not_still_at_the_old_coordinate(self):
        """旧坐标只许有 `__init__.py`（壳），不许还有子模块文件。"""
        for old, subs in (("yiban/mail", MAIL_SUBMODULES),
                          ("yiban/notify", NOTIFY_SUBMODULES)):
            for sub in subs:
                with self.subTest(rel=f"{old}/{sub}.py"):
                    self.assertFalse(
                        os.path.isfile(os.path.join(BASE, old, sub + ".py")),
                        f"{old}/{sub}.py 仍在：那是第二份实现（改一处不会同步另一处）")

    def test_alert_orchestration_has_one_implementation(self):
        """告警编排只有一份实现，旧坐标是转发壳（仓库结构整理批 B 重锚）。

        批 A4 的边界是"告警编排留在 `yiban/engine/alerts.py`"（执行核冻结期
        不许改冻结文件的 import 行）。批 B 时冻结令已解除，编排搬进
        `yiban/adapters/notifier/report.py`，旧坐标只剩壳。
        不变式不变：**只有一份实现**；旧坐标取到的必须是实现本身（同一对象）。
        """
        import yiban.adapters.notifier.report as new
        import yiban.engine.alerts as old
        self.assertIs(old, new,
                      "yiban/engine/alerts.py 必须是转发壳（同一对象），不是第二份实现")
        self.assertFalse(os.path.exists(os.path.join(BASE, NEW_MAIL, "alerts.py")),
                         "告警编排不得住进 notifier/mail")


class NotifierShellTest(unittest.TestCase):
    """② 旧坐标是壳：公共名与子模块都是同一对象。"""

    def test_public_names_are_the_same_objects(self):
        pairs = (("yiban.mail", "yiban.adapters.notifier.mail", MAIL_PUBLIC),
                 ("yiban.notify", "yiban.adapters.notifier.notify", NOTIFY_PUBLIC))
        for old_name, new_name, names in pairs:
            old = importlib.import_module(old_name)
            new = importlib.import_module(new_name)
            self.assertIsNot(old, new, f"{old_name} 与 {new_name} 必须是两个模块对象")
            for name in names:
                with self.subTest(module=old_name, name=name):
                    self.assertTrue(hasattr(new, name), f"{new_name} 缺 {name}")
                    self.assertIs(getattr(old, name), getattr(new, name),
                                  f"{old_name}.{name} 不是新实现的同一对象："
                                  "那是第二份实现，打桩会落空")

    def test_submodules_resolve_to_the_same_objects(self):
        pairs = (("yiban.mail", "yiban.adapters.notifier.mail", MAIL_SUBMODULES),
                 ("yiban.notify", "yiban.adapters.notifier.notify", NOTIFY_SUBMODULES))
        for old_pkg, new_pkg, subs in pairs:
            for sub in subs:
                with self.subTest(module=f"{old_pkg}.{sub}"):
                    old_mod = importlib.import_module(f"{old_pkg}.{sub}")
                    new_mod = importlib.import_module(f"{new_pkg}.{sub}")
                    self.assertIs(old_mod, new_mod,
                                  f"{old_pkg}.{sub} 与新坐标的不是同一模块："
                                  "`mock.patch('<旧坐标>.…')` 会静默打空")


class NotifierBehaviorTest(unittest.TestCase):
    """③ 从新坐标取配置与通道状态，逐值与旧坐标相同。"""

    def test_mail_config_matches_across_coordinates(self):
        old = importlib.import_module("yiban.mail")
        new = importlib.import_module("yiban.adapters.notifier.mail")
        self.assertEqual(new.get_config(), old.get_config())
        self.assertEqual(new.smtp_list(), old.smtp_list())
        self.assertEqual(new.smtp_channel_state(), old.smtp_channel_state())
        self.assertEqual(new.is_enabled(), old.is_enabled())

    def test_notify_config_matches_across_coordinates(self):
        old = importlib.import_module("yiban.notify")
        new = importlib.import_module("yiban.adapters.notifier.notify")
        self.assertEqual(new.get_config(), old.get_config())
        self.assertEqual(new.is_configured(), old.is_configured())
        self.assertEqual(new.budget_exhausted_today(), old.budget_exhausted_today())


if __name__ == "__main__":
    unittest.main(verbosity=2)
