# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""双路径命名空间校验者（128-P2a）。

标签：E · Web：认证/权限/API
覆盖：`yiban/web/` 再导出命名空间与旧 `web/` 的**身份等价**，与生产入口经该
    命名空间仍可建 app。
对应实现：`yiban/web/__init__.py`（shim 骨架）。
关键断言：`yiban.web.X is web.X`（同一模块对象，不是拷贝）——只有身份等价，
    `mock.patch` 才在两侧互通，"两套并存"才不成立；且
    `yiban.web.app.create_app()` 建出的 app 其 `url_map` 与 `web.app` 侧一致。
    本文件只证身份与可达，不证业务行为（业务行为由既有 web 用例覆盖）。

为什么必须用 `is` 而不是"名字能解析"
    `from web.app import create_app` 这类**名字再导出**会让 `yiban.web.app.create_app`
    解析出同一个函数对象，但 `yiban.web.app` 是**另一个模块对象**。此时
    `mock.patch("web.app.create_app")` 只改 `web.app` 的属性，别名模块仍指旧函数——
    旧路径 1901 处打桩与将来新路径的打桩就不再互通，"双路径"是两套并行真相。
    故 shim 必须让 `sys.modules["yiban.web.X"]` 就是 `web.X` 本体。

依赖：Flask test client 不参与；只建 app 读 url_map，不触网不发请求。
"""

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 进入 shim 命名空间的 web 子模块。app/security/render/routes 是 128-P2a 点名面；
#: services 同族（web 的 Python 模块面除去 `__main__` 只有一个入口壳），一并钉住，
#: 防止 shim 只覆盖一半（§13 同族必齐）。
MODULES = ("app", "security", "render", "routes", "services")


class DualPathIdentityTest(unittest.TestCase):
    """静态身份：`yiban.web.X` 与 `web.X` 是同一模块对象。"""

    def test_submodule_objects_are_identical(self):
        for name in MODULES:
            with self.subTest(module=name):
                real = importlib.import_module("web." + name)
                alias = importlib.import_module("yiban.web." + name)
                self.assertIs(
                    alias, real,
                    f"yiban.web.{name} 必须是 web.{name} 本体（同一对象），"
                    "不是再导出拷贝——否则两侧 mock.patch 不互通")

    def test_nested_submodules_are_identical(self):
        """嵌套子模块同法别名。

        只做顶层映射会让 `import yiban.web.routes.pages` 新建一个副本模块，import
        机制随后把副本写回 `web.routes.pages` 属性——旧路径被污染。故必须钉住嵌套身份。
        """
        for dotted in ("routes.pages", "routes.auth", "services.env_io", "services.locks"):
            with self.subTest(dotted=dotted):
                real = importlib.import_module("web." + dotted)
                alias = importlib.import_module("yiban.web." + dotted)
                self.assertIs(alias, real,
                              f"yiban.web.{dotted} 必须是 web.{dotted} 本体")

    def test_named_objects_are_identical(self):
        # 逐模块点名若干真实名字：模块对象相同即名字对象相同，这里把它显式钉住，
        # 防止将来有人把 shim 改成"只搬名字"的实现而测试仍绿。
        cases = {
            "app": ("create_app", "main", "SIGN_MIN_INTERVAL"),
            "security": ("verify_admin",),
            "render": ("site_description",),
            "routes": ("register_all",),
        }
        for mod_name, names in cases.items():
            real = importlib.import_module("web." + mod_name)
            alias = importlib.import_module("yiban.web." + mod_name)
            for name in names:
                with self.subTest(module=mod_name, attr=name):
                    self.assertTrue(hasattr(real, name), f"web.{mod_name} 缺 {name}（底本不符）")
                    self.assertIs(
                        getattr(alias, name), getattr(real, name),
                        f"yiban.web.{mod_name}.{name} 必须是 web.{mod_name}.{name} 同一对象")

    def test_patch_on_web_is_visible_through_alias(self):
        """打桩互通：改 `web.app` 的属性，经 `yiban.web.app` 必须看得见。"""
        web_app = importlib.import_module("web.app")
        importlib.import_module("yiban.web.app")  # 导入即建立别名，下面断言依赖它
        with mock.patch.object(web_app, "SIGN_MIN_INTERVAL", 424242):
            self.assertEqual(importlib.import_module("yiban.web.app").SIGN_MIN_INTERVAL,
                             424242,
                             "在 web.app 上打桩，yiban.web.app 侧必须看到同一值")


class DualPathEntrypointTest(unittest.TestCase):
    """生产入口可经 `yiban.web` 走通：建 app 且 url_map 与 `web.app` 侧一致。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-dualpath-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + "b" * 64 + "\n")
            f.write("YIBAN_ADMIN_USER=admin\n")
            f.write("YIBAN_ADMIN_PASSWORD=MasterPass#2026\n")
        cls._old_env = {k: os.environ.get(k) for k in (
            "YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
            "YIBAN_STATE_DIR", "YIBAN_LOG_FILE", "YIBAN_DISABLE_PURGE_LOOP")}
        os.environ["YIBAN_ACCOUNTS_KEY"] = "b" * 64
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        if BASE not in sys.path:
            sys.path.insert(0, BASE)
        import db as db_mod
        db_mod.init_db(os.environ["YIBAN_DB_FILE"], env_file=cls.env_file)
        cls.web_app = importlib.import_module("web.app")
        cls.alias_app = importlib.import_module("yiban.web.app")
        cls.app_via_web = cls.web_app.create_app()
        cls.app_via_alias = cls.alias_app.create_app()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_alias_module_is_the_entrypoint_module(self):
        self.assertIs(self.alias_app, self.web_app,
                      "yiban.web.app 必须是 web.app 本体，生产入口才真正同一条路径")

    def test_create_app_via_alias_matches_via_web(self):
        def rules(app):
            return sorted(
                (r.rule, tuple(sorted(m for m in r.methods if m in
                                      ("GET", "POST", "PUT", "DELETE", "PATCH"))))
                for r in app.url_map.iter_rules())

        self.assertTrue(self.app_via_alias.url_map.iter_rules(),
                        "不变量前提：app 至少注册了若干路由")
        self.assertEqual(
            rules(self.app_via_alias), rules(self.app_via_web),
            "经 yiban.web.app 建出的 app，其 url_map 必须与 web.app 侧完全一致")


class RootPackageLayoutTest(unittest.TestCase):
    """`web/` 真实代码零移动：旧路径的模块文件仍在 web/ 下。"""

    def test_real_modules_still_live_under_web(self):
        for rel in ("web/app.py", "web/security.py", "web/render.py"):
            with self.subTest(path=rel):
                self.assertTrue(os.path.isfile(os.path.join(BASE, rel)),
                                f"P2a 不许搬真实代码：{rel} 必须仍在 web/ 下")


if __name__ == "__main__":
    unittest.main(verbosity=2)
