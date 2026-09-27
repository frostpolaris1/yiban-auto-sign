# -*- coding: utf-8 -*-
"""高危门禁"清单与执行同源"回归（MF-58 ③）。

标签：E · Web：认证/权限/API
覆盖：`web.app.create_app` 里 `_high_risk_gate` docstring 的覆盖面清单（`METHOD /path`
    行）与真实路由**双向自动比对**——
    ①清单里每个 `METHOD /path` 必须在 url_map 上真实注册（防"`/api/accounts/<idx>/delete`
      这类注册面上不存在的幻影条目"再次混进清单）；
    ②源码里真的调用了 `high_risk_gate()(` 的每个视图，其 `METHOD /path` 必须在清单上
      （防新挂门禁的端点不登清单、清单外的"暗门"）。
    清单是"必须当次输口令"落点表，不是审计清单——登记原报案曾定性偏了，本测试只钉
    清单↔路由同源这一件事。
对应实现：`web/app.py::_high_risk_gate.__doc__`（覆盖面段）+ 各域视图调用点。
关键断言：清单↔路由双向全等——幻影条目（清单有、注册面无）与暗门条目（视图过门、
    清单不登）各红一侧。
依赖：只建一个 app 实例读 url_map/view_functions/inspect.getsource，不触网不发请求。
"""
import contextlib
import importlib.util
import inspect
import os
import re
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MANIFEST_LINE = re.compile(r"^\s*-\s+(GET|POST|PUT|DELETE)\s+(/[^\s（(]+)", re.M)
GATE_CALL = re.compile(r"high_risk_gate\(\)\(")


class GateManifestSyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-manifest4a-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                "YIBAN_ACCOUNTS_KEY=" + "c" * 64 + "\n"
                "YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD=MasterPass#2026\n"
            )
        os.environ["YIBAN_ACCOUNTS_KEY"] = "c" * 64
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp_manifest4a", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_manifest4a"] = cls.webapp
        spec.loader.exec_module(cls.webapp)
        db.init_db(cls.db_file, env_file=cls.env_file)
        cls.app = cls.webapp.create_app()

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        sys.modules.pop("webapp_manifest4a", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_gate_manifest_bidirectional_sync(self):
        manifest = set(MANIFEST_LINE.findall(
            self.app.extensions["yiban_high_risk_gate"].__doc__))
        self.assertGreaterEqual(len(manifest), 8,
                                "覆盖面段必须已转成 `METHOD /path` 清单形态")
        # ① 幻影检测：清单条目必须真实注册
        registered = {(m, r.rule) for r in self.app.url_map.iter_rules()
                      for m in (r.methods - {"HEAD", "OPTIONS"})}
        phantoms = manifest - registered
        self.assertFalse(phantoms,
                         f"清单列了注册面上不存在的端点（清单腐化的复现形态）: {phantoms}")
        # ② 暗门检测：调了门禁的视图必须在清单上
        actual = set()
        for rule in self.app.url_map.iter_rules():
            view = self.app.view_functions.get(rule.endpoint)
            if view is None:
                continue
            try:
                src = inspect.getsource(view)
            except (OSError, TypeError):
                continue
            if GATE_CALL.search(src):
                for method in rule.methods - {"HEAD", "OPTIONS"}:
                    actual.add((method, rule.rule))
        missing = actual - manifest
        self.assertFalse(missing,
                         f"以下端点实际调用了高危门禁却不在清单上: {missing}")


if __name__ == "__main__":
    unittest.main()
