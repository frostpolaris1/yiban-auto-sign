# -*- coding: utf-8 -*-
"""`yiban/infra/` 层的边界守卫（依赖单向、无重复实现）。

标签：M · 架构分层与基础设施
覆盖：基础设施层的依赖单向与无重复实现：旧 scripts
   路径已消失且新路径在场、四个模块可按包导入且有
   docstring、不导入业务层与上层、包内互引不走裸名；两个兼容壳（notify /
   mailer）已删除、运行时目录不再裸名导入它们、包暴露的公共面与壳曾转发的公共名一致。
对应实现：yiban/infra/（locks.py、env_io.py、env_lock.py、account_crypto.py）、yiban/notify
   与 yiban/mail 的包公共面。
关键断言：这是结构断言而非行为断言：被测的是「谁导入了谁」与「某个文件还在不在」。裸名
   import 依赖 sys.path[0]=scripts/，迁移后必错，且在缺 scripts/
   的部署形态下当场炸——所以宁可写正则守卫，也不要留一份「看着无害」的兼容壳（壳存在就等于允许第二份实现存在）。测试代码不列入裸名守卫（那里出现裸名会直接
   ImportError，无需文本兜底）。
依赖：只读仓库源码文本与
   os.path.exists；不导入业务模块、不建库、不联网。整文件在本机执行，无 skip。

同时锁住"只有一份实现"：四个模块原先散在 `scripts/` 下，迁移后旧路径必须消失
（留同名文件会让人 import 到另一份，正是本项目反复踩过的"两份实现"坑）。
"""
import io
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INFRA_MODULES = ("locks", "env_io", "env_lock", "account_crypto") # 清单加一项，下面几组守卫自动覆盖它；新模块漏登记才是真正的漏洞

# 基础设施层不得导入的东西：业务模块与上层（web/signin/db/notify/mailer/child_env）
FORBIDDEN = (
    "db", "signin", "notify", "mailer", "child_env", "web",
    "yiban.store", "yiban.client", "yiban.schedule", "yiban.alerting", # 点号前缀按包名判：禁的是这些上层，不禁 yiban.infra 自己
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
RUNTIME_DIRS = ("web", "yiban", "scripts", "docker") # 测试目录不在列：那里的裸名会直接 ImportError，不需要文本守卫


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
