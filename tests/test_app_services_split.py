# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`web/app.py` 收尾契约：routes 的 `m.*` 名字面总账 + 别名加载安全。

`web/app.py` 已把实现按域拆入 `web/routes/`、`web/services/`、`web/render.py`、
`web/security.py`，本模块只剩「应用工厂 + 跨域中间件 + 名字面（再导出与转发包装）」。
本文件钉住三件破了会**静默**失效的事：

1. **名字面完整（总账）**：`web/routes/*.py` 里 `m.<名字>` 取用的名字必须在 app 模块上
   全部可达。计数硬钉：命中总数 198、兼容名面 197，多一个少一个都红——新增名字必须同步
   审视它是不是该出现在 app 模块上。
   扫描规则：正则 `\\bm\\.([A-Za-z_]\\w*)` 逐行扫 `web/routes/*.py`（`__pycache__` 不在面内）。
   排除规则：`__file__`（唯一命中的模块自省名，`settings_api.py` / `signin_api.py` 用它
   反推仓库根）不属于兼容名面，单列计数；其余命中全部来自 `m = appmod()` 绑定后的模块
   属性读取——routes 里 `m` 没有第二个绑定（无 `for m in`、无局部 `m = <非模块>`）。
2. **别名加载安全**：`web.services.*` / `web.render` / `web.security` 不得导入 `web.app`。
   测试把 app.py 以别名加载（`webapp` / `webapp_xxx`），此时普通 `import web.app` 会再执行
   **一份副本**：副本的模块级状态（被打桩的 `ENV_FILE`、`WEB_VERSION` 等）与在跑的那份
   不是同一个，路由体读到副本上，打桩静默失效。故双钉：源文件正则 + 全新解释器实测
   `web.app not in sys.modules`。
3. **晚查找机制**：routes 经 `web.routes.appmod()` 按 `current_app.import_name` 取模块对象。
   在别名加载的 app 上，它必须指回那一份（而不是另执行一份 `web.app`），且对 app 模块属性的
   打桩/赋值立即可见——否则打桩面与路由体看到两个世界。
"""
import contextlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import flask

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64

#: `m.<名字>` 扫描正则（与报告里的复核命令同一表达式）
M_ATTR_RE = re.compile(r"\bm\.([A-Za-z_]\w*)")
#: 命中总数（含下方排除项）——新增/删除即红
M_ROUTE_NAMES_TOTAL = 198
#: 兼容名面计数（总数减去排除项）
M_ROUTE_COMPAT_NAMES = 197
#: 不算兼容名面的模块自省名（见模块 docstring 的排除规则）
M_ROUTE_NAME_EXCLUDED = frozenset({"__file__"})

#: 不得把 web.app 拉进 sys.modules 的拆分模块（按包路径导入）
SPLIT_MODULES = (
    "web.services.accounts_data",
    "web.services.capacity",
    "web.services.channel_health",
    "web.services.env_io",
    "web.services.executor_env",
    "web.services.locks",
    "web.services.logs",
    "web.services.manual_sign",
    "web.services.measure",
    "web.services.notify_mail",
    "web.services.signstatus",
    "web.services.verify_queue",
    "web.render",
    "web.security",
)

#: 拆分模块的源文件（正则禁 import web.app 的扫描面）：由 SPLIT_MODULES 派生，避免两处清单漂移
SPLIT_SOURCES = tuple(m.replace(".", "/") + ".py" for m in SPLIT_MODULES)


def scan_m_route_names():
    """扫 `web/routes/*.py` 的 `m.<名字>` 命中集（排序列表，供断言与报错用）。"""
    names = set()
    routes_dir = os.path.join(BASE, "web", "routes")
    for name in sorted(os.listdir(routes_dir)):
        if not name.endswith(".py"):
            continue
        with io.open(os.path.join(routes_dir, name), encoding="utf-8") as f:
            names.update(M_ATTR_RE.findall(f.read()))
    return sorted(names)


class AppNameSurfaceContractTest(unittest.TestCase):
    """以别名加载的 app 副本为靶子（routes 与测试实际看到的那一份）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-appsplit-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._old_env = {k: os.environ.get(k) for k in
                        ("YIBAN_ENV_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                         "YIBAN_ACCOUNTS_FILE", "YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE")}
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        spec = importlib.util.spec_from_file_location(
            "webapp_appsplit", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_appsplit"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        cls.flask_app = flask.Flask("webapp_appsplit")
        cls.flask_app.secret_key = "appsplit-test-secret"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ------------------------------------------------------------------
    # 1. 名字面总账
    # ------------------------------------------------------------------
    def test_scan_counts_are_pinned(self):
        """命中总数与兼容名面数硬钉：名字面增删必须被看见，不能静默漂移。"""
        names = scan_m_route_names()
        self.assertEqual(len(names), M_ROUTE_NAMES_TOTAL,
                         f"routes 的 m.* 命中数变了（现 {len(names)}）；"
                         "若确为新增兼容名，请同步更新本文件的钉死计数并复核 app 名字面")
        compat = [n for n in names if n not in M_ROUTE_NAME_EXCLUDED]
        self.assertEqual(len(compat), M_ROUTE_COMPAT_NAMES,
                         f"兼容名面计数变了（现 {len(compat)}，含 {len(names) - len(compat)} 个排除名）")
        excluded = sorted(set(names) & M_ROUTE_NAME_EXCLUDED)
        self.assertEqual(set(names) & M_ROUTE_NAME_EXCLUDED, set(M_ROUTE_NAME_EXCLUDED),
                         f"排除集与实测不符：命中 {excluded}，排除集 {sorted(M_ROUTE_NAME_EXCLUDED)}")

    def test_every_m_route_name_reachable_on_app_module(self):
        """routes 经 `m.*` 取用的每个名字都必须在 app 模块上可达（别名加载的那一份）。"""
        missing = [n for n in scan_m_route_names() if not hasattr(self.webapp, n)]
        self.assertEqual(missing, [],
                         f"web.app 名字面缺失 routes 取用的名字（路由体在请求期会 AttributeError）：{missing}")

    def test_route_modules_do_not_import_web_app(self):
        """正则钉：拆分模块的源文件不得 `import web.app`（别名加载会执行副本模块）。"""
        bad = []
        for rel in SPLIT_SOURCES:
            with io.open(os.path.join(BASE, rel), encoding="utf-8") as f:
                src = f.read()
            if re.search(r"^\s*(?:import|from)\s+web\.app\b", src, re.M):
                bad.append(rel)
        self.assertEqual(bad, [], f"以下模块禁止 import web.app：{bad}")

    # ------------------------------------------------------------------
    # 2. 全新解释器实测：只 import 拆分模块，不得把 web.app 拉进来
    # ------------------------------------------------------------------
    def test_importing_split_modules_does_not_execute_web_app(self):
        """子进程双钉：导入全部拆分模块后 web.app 不在 sys.modules，且 app 名字面仍齐。"""
        code = (
            "import sys, re, pathlib, importlib;"
            f"sys.path[:0]=[{os.path.join(BASE, 'scripts')!r}, {BASE!r}];"
            f"[importlib.import_module(m) for m in {list(SPLIT_MODULES)!r}];"
            "print('APP_LOADED_BY_SPLIT' if 'web.app' in sys.modules else 'SPLIT_ONLY_OK');"
            "import web.app as m;"
            "names=set();"
            "[names.update(re.findall(r'\\bm\\.([A-Za-z_]\\w*)', p.read_text(encoding='utf-8')))"
            " for p in pathlib.Path('web/routes').glob('*.py')];"
            "print('NAMES', len(names));"
            "print('MISSING', ','.join(sorted(n for n in names if not hasattr(m, n))) or 'NONE')"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["YIBAN_STATE_DIR"] = self.tmp
        env["YIBAN_LOG_FILE"] = os.path.join(self.tmp, "sign.log")
        env["YIBAN_DB_FILE"] = os.path.join(self.tmp, "probe.db")
        env["YIBAN_ENV_FILE"] = self.env_file
        r = subprocess.run([sys.executable, "-c", code], cwd=BASE, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertIn("SPLIT_ONLY_OK", r.stdout, r.stderr[-800:])
        self.assertNotIn("APP_LOADED_BY_SPLIT", r.stdout, r.stderr[-800:])
        self.assertIn(f"NAMES {M_ROUTE_NAMES_TOTAL}", r.stdout,
                      f"独立进程改扫出的命中数与本文件钉死值不符：{r.stdout!r} {r.stderr[-400:]}")
        self.assertIn("MISSING NONE", r.stdout,
                      f"独立进程里 web.app 名字面缺名：{r.stdout!r} {r.stderr[-400:]}")
        self.assertEqual(r.returncode, 0, r.stderr[-800:])

    # ------------------------------------------------------------------
    # 3. 晚查找机制（routes 取的就是这一份，打桩立即可见）
    # ------------------------------------------------------------------
    def test_appmod_resolves_alias_loaded_module(self):
        """`appmod()` 必须指回别名加载的那一份 app，而不是另执行一份 `web.app`。"""
        from web.routes import appmod
        with self.flask_app.test_request_context():
            self.assertIs(appmod(), self.webapp,
                          "routes 的晚查找没取到在跑的那一份 app 模块")

    def test_patched_app_attribute_is_visible_through_late_lookup(self):
        """代表性往返：patch app 模块属性 → routes 晚查找取到的是同一个被改过的值。"""
        from web.routes import appmod
        with self.flask_app.test_request_context(), \
                mock.patch.object(self.webapp, "ENV_FILE", os.path.join(self.tmp, "x.env")):
            self.assertEqual(appmod().ENV_FILE, os.path.join(self.tmp, "x.env"))
            self.assertEqual(appmod(), self.webapp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
