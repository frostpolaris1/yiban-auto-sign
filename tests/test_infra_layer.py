# -*- coding: utf-8 -*-
"""`yiban/infra/` 层的边界守卫（依赖单向、无重复实现）。

基础设施层的价值在于"与业务无关、可被任何层导入且不被任何业务拖住"——一旦有人从
管理层里 `import db` 或 `import signin`，这层就会把业务逻辑卷进来，单独替换/测试
都要拉起整个应用。故用结构断言把这条边界钉住。

同时锁住"只有一份实现"：四个模块原先散在 `scripts/` 下，迁移后旧路径必须消失
（留同名文件会让人 import 到另一份，正是本项目反复踩过的"两份实现"坑）。
"""
import io
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INFRA_MODULES = ("locks", "env_io", "env_lock", "account_crypto")

# 基础设施层不得导入的东西：业务模块与上层（web/signin/db/notify/mailer/child_env）
FORBIDDEN = (
    "db", "signin", "notify", "mailer", "child_env", "web",
    "yiban.store", "yiban.client", "yiban.schedule", "yiban.alerting",
)


class InfraLayerTest(unittest.TestCase):
    def _src(self, name):
        with io.open(os.path.join(BASE, "yiban", "infra", name + ".py"),
                     encoding="utf-8") as f:
            return f.read()

    def test_old_paths_are_gone(self):
        for name in INFRA_MODULES:
            with self.subTest(module=name):
                self.assertFalse(
                    os.path.exists(os.path.join(BASE, "scripts", name + ".py")),
                    f"scripts/{name}.py 仍在：会出现第二份实现",
                )
                self.assertTrue(
                    os.path.exists(os.path.join(BASE, "yiban", "infra", name + ".py")),
                    f"yiban/infra/{name}.py 缺失",
                )

    def test_importable_as_package(self):
        import importlib
        for name in INFRA_MODULES:
            with self.subTest(module=name):
                mod = importlib.import_module(f"yiban.infra.{name}")
                self.assertTrue(hasattr(mod, "__doc__") and mod.__doc__,
                                "模块需有文档字符串说明职责")

    def test_no_business_imports(self):
        bad = []
        for name in INFRA_MODULES:
            src = self._src(name)
            for match in re.finditer(r"(?m)^\s*(?:import|from)\s+([\w.]+)", src):
                mod = match.group(1).split(".")[0]
                dotted = match.group(1)
                if mod in ("db", "signin", "notify", "mailer", "child_env", "web"):
                    bad.append(f"{name}.py → {dotted}")
                if dotted.startswith(("yiban.store", "yiban.client", "yiban.schedule")):
                    bad.append(f"{name}.py → {dotted}")
        self.assertEqual(bad, [], "基础设施层不得依赖业务层：" + "; ".join(bad))

    def test_infra_modules_do_not_import_each_other_by_bare_name(self):
        """包内互引必须走 `yiban.infra.X`（裸名依赖 sys.path[0]=scripts/，迁移后必错）。"""
        for name in INFRA_MODULES:
            src = self._src(name)
            for other in INFRA_MODULES:
                if other == name:
                    continue
                with self.subTest(module=name, other=other):
                    self.assertNotRegex(
                        src, rf"(?m)^\s*import {other}\s*$",
                        f"{name}.py 仍以裸名导入 {other}（应为 from yiban.infra import {other}）",
                    )


#: 推送/邮件两个组件的裸名壳（曾让 `import notify` / `import mailer` 继续可用）
REMOVED_SHELLS = ("scripts/notify.py", "scripts/mailer.py")

#: 裸名导入：`import notify` / `from mailer import x` / `import mailer as m` 都算
BARE_NOTIFY_MAILER_RE = re.compile(
    r"(?m)^\s*(?:import|from)\s+(?:notify|mailer)(?:\s|\.|,|$)")

#: 运行时目录（测试代码不在此列：测试里出现裸名会直接 ImportError，无需文本守卫）
RUNTIME_DIRS = ("web", "yiban", "scripts", "docker")


class LegacyShellRemovalTest(unittest.TestCase):
    """收口守卫：两个兼容壳必须消失，且运行时模块不得再裸名导入它们。

    壳删除后 `import notify` 会直接 ModuleNotFoundError，故**保留壳**没有意义；
    真正要防的是"有人图省事又加回一份壳"或"新代码写成裸名导入"——那两件事都会
    让"两份实现/路径依赖 scripts/"的老问题回来（`yiban/` 里出现裸名还会让包
    在缺 scripts/ 的部署形态下当场炸）。调用方一律 `from yiban import notify` /
    `from yiban import mail as mailer`；下划线名走子模块。
    """

    def _iter_runtime_py(self):
        for root in RUNTIME_DIRS:
            base = os.path.join(BASE, root)
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for name in sorted(filenames):
                    if name.endswith(".py"):
                        yield os.path.join(dirpath, name)

    def test_shells_are_gone(self):
        for rel in REMOVED_SHELLS:
            with self.subTest(path=rel):
                self.assertFalse(
                    os.path.exists(os.path.join(BASE, rel)),
                    f"{rel} 还在：旧导入路径会继续被使用，收口未完成",
                )

    def test_no_bare_notify_mailer_imports_in_runtime_dirs(self):
        bad = []
        for path in self._iter_runtime_py():
            with io.open(path, encoding="utf-8") as f:
                src = f.read()
            for m in BARE_NOTIFY_MAILER_RE.finditer(src):
                bad.append(f"{os.path.relpath(path, BASE).replace(os.sep, '/')}: "
                           f"{m.group(0).strip()}")
        self.assertEqual(
            bad, [],
            "运行时模块不得裸名导入 notify/mailer（应为 `from yiban import notify` / "
            "`from yiban import mail as mailer`）：" + "; ".join(bad),
        )

    def test_packages_expose_the_public_surface(self):
        """壳曾转发的**公共名**必须能从包直接拿到（下划线名走子模块）。"""
        import importlib

        notify_pkg = importlib.import_module("yiban.notify")
        mail_pkg = importlib.import_module("yiban.mail")
        for name in ("send", "send_test", "get_config", "get_secret", "is_configured",
                     "is_safe_url", "budget_exhausted_today", "pop_exhaustion_notice",
                     "BudgetTicket", "logger"):
            with self.subTest(pkg="yiban.notify", name=name):
                self.assertTrue(hasattr(notify_pkg, name), f"yiban.notify 缺公共名 {name}")
        for name in ("send_admin_alert", "send_user", "get_config", "is_enabled",
                     "smtp_list", "smtp_channel_state", "admin_recipients",
                     "admin_notify_enabled", "logger"):
            with self.subTest(pkg="yiban.mail", name=name):
                self.assertTrue(hasattr(mail_pkg, name), f"yiban.mail 缺公共名 {name}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
