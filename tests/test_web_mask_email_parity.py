# -*- coding: utf-8 -*-
"""前端 `YB.maskEmail` 与后端 `_mask_email` 的脱敏口径对拍（真实行为，非静态扫描）。

## 为什么要对拍

两端各有一份邮箱脱敏：`web/services/accounts_data.py` 的 `_mask_email`（出站即脱敏，
经 `web.app._mask_email` 再导出）与 `web/static/js/core.js` 的 `maskEmail`（渲染层幂等
脱敏）。二者是同一口径的**两份实现**，
只要一边改动而另一边没跟上，就会出现「后端已脱敏、前端再脱一次」或反过来的错位，
表现为域名被吞或完整性泄漏。静态扫描只能证明函数存在，证明不了**行为一致**。

## 本文件怎么测

照 `tests/test_logs_by_date.py` 的做法：从源码里按花括号配对抽出
`function maskEmail(e) { ... }` 交给 node 执行；后端侧抽出 `def _mask_email(e):` 的
函数体后 `exec` 成可调用对象。对同一批输入逐例比对两端返回值，覆盖正常邮箱、
已含 `*`（幂等）、无 `@`、`@` 在首位、本地部长度 1/2/3/超长等边界。

node 不可用时跳过（本套件其余部分不引入硬性 node 依赖）。
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from _frontend_src import frontend_source

from yiban.infra import env_io

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_JS = os.path.join(BASE, "web", "static", "js", "core.js")
# 后端 `_mask_email` 的唯一真源（web.app 只再导出）：源码文本从它的新家抽取。
APP_PY = os.path.join(BASE, "web", "services", "accounts_data.py")
NODE = shutil.which("node")

# (说明, 输入) —— 两端对同一输入必须给出同一结果
CASES = (
    ("正常邮箱", "user@example.com"),
    ("本地部长度 1", "a@example.com"),
    ("本地部长度 2", "ab@example.com"),
    ("本地部长度 3", "abc@example.com"),
    ("本地部超长（仍只保留前 3）", "verylonglocalpart@example.com"),
    ("已含 *（幂等）", "use***@example.com"),
    ("星号在本地部中间", "a*b@example.com"),
    ("无 @", "not-an-email"),
    ("@ 在首位", "@example.com"),
    ("空串", ""),
    ("短域名", "x@q.io"),
)


def _extract_js_function(src, name):
    """按花括号配对抽出 `function <name>(...) { ... }` 整段。"""
    start = src.index("function " + name + "(")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError("JS 函数 %s 未找到匹配的右花括号" % name)


def _extract_python_function(src, name):
    """抽出 `def <name>(...):` 及其缩进函数体，exec 后返回可调用对象。

    后端函数体只含 str/find/min 等纯逻辑，无副作用，故可安全整体 exec。
    """
    lines = src.split("\n")
    start = None
    for idx, line in enumerate(lines):
        if line.startswith("def " + name + "("):
            start = idx
            break
    if start is None:
        raise AssertionError("Python 函数 %s 未找到" % name)
    body = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() == "" or line[:1] in (" ", "\t"):
            body.append(line)
        else:
            break
    namespace = {}
    exec("\n".join(body), namespace)
    return namespace[name]


@unittest.skipUnless(NODE, "node 不可用：跳过前后端邮箱脱敏口径对拍")
class MaskEmailParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CORE_JS, encoding="utf-8") as fh:
            js_src = fh.read()
        with open(APP_PY, encoding="utf-8") as fh:
            py_src = fh.read()
        cls.js_fn = _extract_js_function(js_src, "maskEmail")
        # staticmethod：否则函存为类属性后经 self.py_fn 访问会被当作绑定方法，多传一个 self
        cls.py_fn = staticmethod(_extract_python_function(py_src, "_mask_email"))

    def _run_js(self, inputs):
        """在 node 里对一批字符串执行抽出的 maskEmail，返回结果列表。"""
        script = (
            self.js_fn
            + "\nvar cases = " + json.dumps(inputs) + ";\n"
            + "console.log(JSON.stringify(cases.map(function (s) { return maskEmail(s); })));\n"
        )
        proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise AssertionError("node 执行失败：%s" % (proc.stderr or proc.stdout))
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_backend_and_frontend_agree_on_all_cases(self):
        """两端对全部边界输入返回同一脱敏结果。"""
        inputs = [s for _label, s in CASES]
        front = self._run_js(inputs)
        back = [self.py_fn(s) for s in inputs]
        problems = []
        for (label, value), f, b in zip(CASES, front, back, strict=True):
            if f != b:
                problems.append(
                    "  %s（输入 %r）：前端 %r != 后端 %r" % (label, value, f, b)
                )
        if problems:
            self.fail(
                "YB.maskEmail 与 _mask_email 脱敏口径不一致（单边改动会导致错位脱敏）：\n"
                + "\n".join(problems)
            )

    def test_mask_is_idempotent_on_both_sides(self):
        """两端对已脱敏值必须原样返回（幂等），否则会二次吞字符。"""
        masked = [self.py_fn(value) for _label, value in CASES]
        front = self._run_js(masked)
        for value, f in zip(masked, front, strict=True):
            self.assertEqual(f, value, "前端 maskEmail 对已脱敏值 %r 不幂等（返回 %r）" % (value, f))
            self.assertEqual(self.py_fn(value), value, "后端 _mask_email 对 %r 不幂等" % value)


TEST_KEY = "a" * 64


ADMIN_PASS = "MasterPass#2026"


_ENV_KEYS = (
    "YIBAN_MAIL_ENABLE", "YIBAN_MAIL_ADMIN_TO", "YIBAN_MAIL_ADMIN_NOTIFY",
    "YIBAN_MAIL_USER", "YIBAN_MAIL_PASS", "YIBAN_MAIL_SMTPS_ENC",
    "YIBAN_SITE_DESCRIPTION", "YIBAN_SITE_IMAGE",
)


def _load_webapp(tag):
    import db as _db
    spec = importlib.util.spec_from_file_location(f"webapp_{tag}", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"webapp_{tag}"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return _db, mod


class _Base(unittest.TestCase):
    """共享脚手架：临时 .env/DB + webapp 加载 + 主管理员登录（照抄 test_mailer.py 的 MailFailoverTest）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-admin-to-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls._old_env = {k: os.environ.get(k) for k in (*_ENV_KEYS, "YIBAN_ENV_FILE")}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        cls.db, cls.webapp = _load_webapp(cls.__name__)

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        if self.db._conn is not None:
            with contextlib.suppress(Exception):
                self.db._conn.close()
            self.db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        self.db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)

    def _reset_env_file(self, extra=""):
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n" + extra
            )

    def _master(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        t = c.get("/api/me").get_json()["csrf_token"]
        return c, {"X-CSRF-Token": t}

    def _env_admin_to(self):
        return env_io.parse_env_file(self.env_file).get("YIBAN_MAIL_ADMIN_TO")


class SiteDescriptionTest(_Base):
    """站点分享摘要：登录页与文档页含 description/og，可用 .env 覆盖。"""

    def setUp(self):
        # unittest 按方法名字母序执行，.env 覆盖会跨用例残留（且 site_description
        # 每次读文件）——每个用例都从干净基线起步。
        super().setUp()
        self._reset_env_file()

    def test_login_page_has_description_and_og(self):
        c = self.webapp.create_app().test_client()
        html = c.get("/login").get_data(as_text=True)
        self.assertIn('<meta name="description"', html)
        self.assertIn('<meta property="og:title"', html)
        self.assertIn('<meta property="og:description"', html)
        self.assertIn(self.webapp.SITE_DESCRIPTION_DEFAULT, html)

    def test_doc_pages_have_description(self):
        c = self.webapp.create_app().test_client()
        for path in ("/terms", "/privacy"):
            html = c.get(path).get_data(as_text=True)
            self.assertIn('<meta name="description"', html, f"{path} 缺摘要")
            self.assertIn('<meta property="og:description"', html, f"{path} 缺 og 摘要")

    def test_env_overrides_description(self):
        self._reset_env_file("YIBAN_SITE_DESCRIPTION=自定义站点简介\n")
        c = self.webapp.create_app().test_client()
        html = c.get("/login").get_data(as_text=True)
        self.assertIn("自定义站点简介", html)
        self.assertNotIn(self.webapp.SITE_DESCRIPTION_DEFAULT, html)

    def test_description_is_escaped(self):
        """摘要来自 .env，进 content 属性前必须转义（防属性注入）。"""
        self._reset_env_file('YIBAN_SITE_DESCRIPTION=x"><script>alert(1)</script>\n')
        c = self.webapp.create_app().test_client()
        html = c.get("/login").get_data(as_text=True)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn("&lt;script&gt;", html)

    def test_site_image_requires_https(self):
        """og:image 仅接受 https 绝对地址，其余一律忽略。"""
        self._reset_env_file("YIBAN_SITE_IMAGE=javascript:alert(1)\n")
        c = self.webapp.create_app().test_client()
        self.assertNotIn('property="og:image"', c.get("/login").get_data(as_text=True))
        self._reset_env_file("YIBAN_SITE_IMAGE=https://cdn.example.com/a.png\n")
        self.assertIn('content="https://cdn.example.com/a.png"',
                      c.get("/login").get_data(as_text=True))


class PlaceholderFontParityTest(_Base):
    """占位文字字号：三模板不得再把 placeholder 缩到 0.92em（与输入值不一致）。"""

    def _read(self, name):
        # A1/A3 起前端被拆分（index.html 的内联 CSS 外提为 static/css/app.css、
        # 设置区拆到 templates/tabs/settings.html）：改为聚合读取"模板 + include 片段 +
        # 外链自研静态资源"。否则本组的 assertNotIn 会因目标文件变空而**恒真**——
        # 占位字号缩放的防回流保护会静默失效。
        return frontend_source(name)

    def test_no_placeholder_font_shrink(self):
        # 载体换锚：user.html 已随用户端拆页退役、index.html 已无路由渲染；
        # 判据（有输入框的页面不得对 placeholder 缩字号）不变，改扫现役含输入框的页面。
        pages = ("login.html", os.path.join("pages", "work_settings.html"),
                 os.path.join("pages", "work_accounts.html"),
                 os.path.join("pages", "user_account.html"))
        for name in pages:
            src = self._read(name)
            self.assertNotIn("::placeholder { font-size", src, f"{name} 仍有 placeholder 字号缩放")
            self.assertNotIn("::placeholder{font-size", src, f"{name} 仍有 placeholder 字号缩放")

    def test_capacity_inputs_no_inline_shrink(self):
        """容量上限输入框不得再用内联 0.92em（与同卡其它输入框不一致）。"""
        src = self._read(os.path.join("pages", "work_settings.html"))
        self.assertNotIn('style="font-size:0.92em"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
