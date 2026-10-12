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

# 仓库根入 sys.path：本文件在 pytest 外直接跑时也能解析 `yiban` / `web`。
# 必须**先于**任何 yiban 导入（tests/test_deploy_entry_imports.py 按出现位置钉死顺序）。
sys.path.insert(0, BASE)

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

    def test_alias_import_keeps_real_spec_and_loader(self):
        """别名导入不得改写真实模块的 `__spec__` 与 `__loader__`（审查 M-1）。

        import 机制会无条件把别名 spec 写进被返回的真模块。不还原时真模块的
        `__spec__.name` 变成 `yiban.web.*`，且 loader 变成别名的 no-op loader——
        `find_spec` 与 `reload` 随之失准。
        """
        importlib.import_module("yiban.web.app")
        importlib.import_module("yiban.web.routes")
        importlib.import_module("yiban.web.services.measure")
        for dotted in ("web.app", "web.routes", "web.services.measure"):
            mod = importlib.import_module(dotted)
            with self.subTest(module=dotted):
                self.assertEqual(mod.__spec__.name, dotted,
                                 "别名导入不得改写真实模块的 __spec__.name")
        routes = importlib.import_module("web.routes")
        sssl = routes.__spec__.submodule_search_locations
        self.assertIsNotNone(sssl, "web.routes 的 spec 必须仍是包 spec（带 __path__）")
        self.assertTrue(sssl[0].replace("\\", "/").endswith("web/routes"),
                        f"web.routes 的 spec 必须指回真实目录，实得 {sssl!r}")
        self.assertEqual(type(routes.__loader__).__name__, "SourceFileLoader",
                         "真模块的 loader 不得被别名 loader 顶替")

    def test_find_spec_is_not_corrupted_by_alias_import(self):
        """`importlib.util.find_spec("web.X")` 必须返回真实 spec（审查 M-1）。"""
        importlib.import_module("yiban.web.routes")
        spec = importlib.util.find_spec("web.routes")
        self.assertEqual(spec.name, "web.routes",
                         "别名导入后 find_spec 不得返回别名 spec")
        sssl = spec.submodule_search_locations
        self.assertIsNotNone(sssl, "find_spec 必须带回包 spec 的 submodule_search_locations")
        self.assertTrue(sssl[0].replace("\\", "/").endswith("web/routes"),
                        f"find_spec 必须指回真实 routes 目录，实得 {sssl!r}")

    def test_alias_loader_restores_spec_so_reload_really_reloads(self):
        """`importlib.reload` 必须真重载（审查 M-1）。

        复现法（审查同法）：把模块体定义的 `VALUE` 改成 99 再 reload——真重载把它还原。
        用合成模块对，避免 reload 真 `web.*` 影响进程内其他用例；走的是 shim 的真
        `_AliasLoader`。
        """
        from yiban.web import _AliasLoader
        tmp = tempfile.mkdtemp(prefix="yiban-alias-reload-")
        real_name = "_p2a_real_target"
        sys.path.insert(0, tmp)  # reload 按名字重找 spec，故合成模块所在目录须在 sys.path 上
        try:
            path = os.path.join(tmp, real_name + ".py")
            with open(path, "w", encoding="utf-8") as f:
                f.write("VALUE = 1\n")
            real_spec = importlib.util.spec_from_file_location(real_name, path)
            real_mod = importlib.util.module_from_spec(real_spec)
            sys.modules[real_name] = real_mod
            real_spec.loader.exec_module(real_mod)
            self.assertEqual(real_mod.VALUE, 1, "不变量前提：合成模块初值 1")

            # 走与 shim 同一条机制：别名 spec 的 loader 是 _AliasLoader。
            alias_spec = importlib.util.spec_from_loader(
                "_p2a_alias_name", _AliasLoader(real_name))
            alias_mod = importlib.util.module_from_spec(alias_spec)  # create_module＋spec 补写
            alias_spec.loader.exec_module(alias_mod)                 # 还原真 spec
            self.assertIs(alias_mod, real_mod, "别名必须指向同一模块对象")
            self.assertEqual(real_mod.__spec__.name, real_name,
                             "别名加载后真模块的 __spec__.name 必须保真")

            real_mod.VALUE = 99
            importlib.reload(real_mod)
            self.assertEqual(real_mod.VALUE, 1,
                             "reload 必须真重载（修复前 __spec__ 指向别名 no-op loader，静默不重载）")
        finally:
            sys.modules.pop(real_name, None)
            if tmp in sys.path:
                sys.path.remove(tmp)
            shutil.rmtree(tmp, ignore_errors=True)


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
