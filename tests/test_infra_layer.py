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


if __name__ == "__main__":
    unittest.main(verbosity=2)
