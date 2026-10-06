# -*- coding: utf-8 -*-
"""XFF / 转发头信任判定：逐部署形态对拍 + 显式开关 + 单一谓词守卫。

标签：E · Web：认证/权限/API
覆盖：`_client_ip` 的取值域（回环段语义 + IPv4-mapped + glibc 缩写 IPv4）、逐部署形态
的信任门对拍、`YIBAN_TRUST_FORWARDED_HEADERS` 显式开关的三态（缺省/显式开/显式关）、
歧义形态的一次性告警、`X-Forwarded-Proto` 与 XFF 共用同一判据、以及"回环谓词只有一个
定义点"的结构守卫。
对应实现：`web/security.py` 的 `_is_loopback_addr` / `_forwarded_trust_enabled` /
`_client_ip`，`web/app.py` 的 `_forwarded_proto_is_https` 与 `_report_ambiguous_forwarded_trust`。
关键断言：①取值域按 `ipaddress` 段语义（127.0.0.0/8 与 ::1），并显式判 `ipv4_mapped`；
②默认（键未设）与非法值都保持"回环即可信"的现行为；③显式关时任何转发头都不采信；
④缺省且出现"回环来源 + XFF"时只告警一次；⑤`web/` 下回环谓词只有一个定义点。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite，不联网、不访问真实易班接口。
"""

import ast
import importlib.util
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_KEY = "f" * 64

#: 显式开关的键名（须同时进 `.env.example` 与 `README.md`）。
TRUST_ENV_KEY = "YIBAN_TRUST_FORWARDED_HEADERS"


def _read(rel):
    with io.open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


def _read_env_lines(path):
    with io.open(path, encoding="utf-8") as f:
        return [ln.rstrip("\n") for ln in f if ln.strip()]


#: 逐部署形态对拍表（默认档 = 键未设 ⇒ 保持"回环即可信"的现行为）。
#: 每行 = (形态名, REMOTE_ADDR, X-Forwarded-For 或 None, 期望桶, 备注)。
#: `期望桶` 是本版落地后的行为；"误开"行的期望值仍是客户端自报的 XFF——本版
#: 按工单评论的 PM 裁定**不翻转默认**，翻转留给 B 半，届时这些行必须改成 REMOTE_ADDR。
FORMS = (
    ("nginx 反代（systemd）", "127.0.0.1", "203.0.113.9", "203.0.113.9",
     "nginx 覆盖式写 XFF：读到真实客户端 IP"),
    ("compose healthcheck（容器内直连，不带 XFF）", "127.0.0.1", None, "127.0.0.1",
     "healthcheck 自身不发 XFF，桶即回环地址"),
    ("compose 容器内直连（自报 XFF）", "127.0.0.1", "198.51.100.7", "198.51.100.7",
     "误开：本版保留（PM 裁定不翻转默认）"),
    ("--host 0.0.0.0 显式直连", "203.0.113.9", "198.51.100.7", "203.0.113.9",
     "首跳非回环：XFF 被丢弃、退回 remote_addr"),
    ("回环直连（默认监听，本机调试/冒烟）", "127.0.0.1", "198.51.100.7", "198.51.100.7",
     "误开：本版保留（PM 裁定不翻转默认）"),
    ("ssh -L 隧道", "127.0.0.1", "198.51.100.7", "198.51.100.7",
     "误开：本版保留（PM 裁定不翻转默认）"),
    ("双栈 IPv6 监听（v4 客户端）", "::ffff:127.0.0.1", "203.0.113.9", "203.0.113.9",
     "误关：本版起认 IPv4-mapped 回环"),
    ("绑 127.0.0.2（非 .1 回环）", "127.0.0.2", "203.0.113.9", "203.0.113.9",
     "反向：本版起与监听侧判据同源"),
)

#: 取值域单元表：地址串 → 是否回环。`ipaddress` 段语义 + 显式 `ipv4_mapped`
#: + glibc 缩写 IPv4（`socket.inet_aton` 归一）。
DOMAIN = (
    ("127.0.0.1", True),
    ("::1", True),
    ("127.0.0.2", True),
    ("127.0.0.255", True),
    ("::ffff:127.0.0.1", True),
    ("::ffff:127.0.0.2", True),
    ("127.1", True),
    ("0x7f000001", True),
    ("2130706433", True),
    ("localhost", True),
    ("LOCALHOST", True),
    ("127.0.0.1 ", True),
    ("203.0.113.5", False),
    ("::ffff:203.0.113.5", False),
    ("::2", False),
    ("0.0.0.0", False),
    ("127", False),
    ("example.com", False),
    ("", False),
    (None, False),
)


class _LogCapture(logging.Handler):
    """把 `web` 通道的日志行收进列表（用于"只告警一次"这类计数断言）。"""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


def _capture_web_log(fn):
    cap = _LogCapture()
    logging.getLogger("web").addHandler(cap)
    try:
        fn()
    finally:
        logging.getLogger("web").removeHandler(cap)
    return cap.lines


class ForwardedTrustFormTableTest(unittest.TestCase):
    """默认档（键未设）下的逐形态对拍：用裸 Flask 应用 + test_request_context。"""

    @classmethod
    def setUpClass(cls):
        import flask
        spec = importlib.util.spec_from_file_location(
            "webapp_fwdtrust_forms", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_fwdtrust_forms"] = cls.webapp
        spec.loader.exec_module(cls.webapp)
        cls.flask_app = flask.Flask("fwdtrust-forms")
        cls.flask_app.secret_key = "fwdtrust-forms-secret"

    def _bucket(self, remote_addr, xff):
        environ = {} if remote_addr is None else {"REMOTE_ADDR": remote_addr}
        headers = {} if xff is None else {"X-Forwarded-For": xff}
        with self.flask_app.test_request_context("/", environ_base=environ,
                                                headers=headers):
            return self.webapp._client_ip()

    def test_逐形态桶对拍(self):
        for form, remote_addr, xff, want, note in FORMS:
            with self.subTest(form=form):
                self.assertEqual(self._bucket(remote_addr, xff), want,
                                 f"{form}：{note}")

    def test_裸应用未配开关时按信任执行(self):
        """裸 Flask 应用没有本键 ⇒ 回退默认（信任），不得因此把回环首跳判成不可信。"""
        self.assertEqual(self._bucket("127.0.0.1", "203.0.113.9"), "203.0.113.9")


class LoopbackPredicateDomainTest(unittest.TestCase):
    """`_is_loopback_addr` 的取值域：段语义 + IPv4-mapped + glibc 缩写 IPv4。"""

    @classmethod
    def setUpClass(cls):
        import web.security as sec
        cls.sec = sec

    def test_取值域逐枚(self):
        for raw, want in DOMAIN:
            with self.subTest(raw=raw):
                self.assertIs(self.sec._is_loopback_addr(raw), want,
                              f"{raw!r} 的回环判定应为 {want}")

    def test_入站与出站判据不混用(self):
        """入站信任门与出站 SSRF 判据不得互换：两边共用的只有"是否回环"这一层事实。"""
        from yiban.notify import config as notify_config
        self.assertIs(self.sec._is_loopback_addr("127.0.0.2"), True)
        # 出站判据对同一地址给的是"不可路由 ⇒ 拒"，两者不是同一个函数、也不互换
        self.assertIsNot(self.sec._is_loopback_addr, notify_config._is_nonroutable_target)


class _AppBase(unittest.TestCase):
    """建临时 `.env` + SQLite，并以别名加载 `web/app.py`（与既有 Web 测试同形）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-fwdtrust-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        with io.open(cls.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        with io.open(cls.env_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls._old_env = {k: os.environ.get(k) for k in
                        ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                         "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE",
                         TRUST_ENV_KEY)}
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ.pop(TRUST_ENV_KEY, None)
        spec = importlib.util.spec_from_file_location(
            "webapp_fwdtrust", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_fwdtrust"] = cls.webapp
        spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        os.environ.pop(TRUST_ENV_KEY, None)
        self._write_env(None)

    def _write_env(self, trust_value, cookie_secure=""):
        lines = [ln for ln in _read_env_lines(self.env_file)
                 if not ln.startswith(TRUST_ENV_KEY + "=")
                 and not ln.startswith("YIBAN_COOKIE_SECURE=")]
        lines.append(f"YIBAN_COOKIE_SECURE={cookie_secure}")
        if trust_value is not None:
            lines.append(f"{TRUST_ENV_KEY}={trust_value}")
        with io.open(self.env_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n" + "\n".join(lines) + "\n")

    def _app(self, trust_value, cookie_secure=""):
        """按给定 `.env` 建应用（开关在 create_app 期解析 ⇒ 每档要新建）。"""
        self._write_env(trust_value, cookie_secure)
        app = self.webapp.create_app()
        return app, app.test_client()

    def _bucket(self, app, remote_addr, xff):
        headers = {} if xff is None else {"X-Forwarded-For": xff}
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": remote_addr},
                                      headers=headers):
            return self.webapp._client_ip()


class ForwardedTrustSwitchTest(_AppBase):
    """显式开关 `YIBAN_TRUST_FORWARDED_HEADERS` 的三态。"""

    def test_键未设时保持信任回环首跳(self):
        app, _c = self._app(None)
        self.assertEqual(self._bucket(app, "127.0.0.1", "203.0.113.9"), "203.0.113.9")

    def test_显式关时任何转发头都不采信(self):
        app, _c = self._app("0")
        for form, remote_addr, xff, _want, _note in FORMS:
            with self.subTest(form=form):
                self.assertEqual(self._bucket(app, remote_addr, xff or ""), remote_addr,
                                 f"{form}：显式关后桶必须是 remote_addr")

    def test_显式开时与缺省同行为(self):
        app, _c = self._app("1")
        for form, remote_addr, xff, want, _note in FORMS:
            with self.subTest(form=form):
                self.assertEqual(self._bucket(app, remote_addr, xff or ""), want)

    def test_非法值回退信任并告警(self):
        """非法值：解析发生在 create_app，故采集必须包住建应用这一步。"""
        seen = []

        def _run():
            app, _c = self._app("maybe")
            seen.append(self._bucket(app, "127.0.0.1", "203.0.113.9"))

        lines = _capture_web_log(_run)
        self.assertEqual(seen, ["203.0.113.9"], "非法值必须回退缺省的「信任」")
        self.assertTrue(any(TRUST_ENV_KEY in ln and "非法" in ln for ln in lines),
                        f"非法值必须留一条可 grep 的告警：{lines}")

    def test_开关须同时进env示例与README(self):
        for rel in (".env.example", "README.md"):
            with self.subTest(rel=rel):
                self.assertIn(TRUST_ENV_KEY, _read(rel),
                              f"{rel} 未登记 {TRUST_ENV_KEY}（开关必须可被部署者发现）")


class ForwardedProtoTrustTest(_AppBase):
    """`X-Forwarded-Proto` 与 XFF 共用同一个"首跳是否可信"判据与同一个开关。"""

    def _secure_after(self, app, c, remote_addr, proto):
        c.get("/api/clock", headers={"X-Forwarded-Proto": proto},
              environ_base={"REMOTE_ADDR": remote_addr})
        return app.config["SESSION_COOKIE_SECURE"]

    def test_回环首跳采信proto(self):
        app, c = self._app(None, cookie_secure="")
        self.assertTrue(self._secure_after(app, c, "127.0.0.1", "https"))

    def test_非回环首跳丢弃proto(self):
        app, c = self._app(None, cookie_secure="")
        self.assertFalse(self._secure_after(app, c, "203.0.113.9", "https"))

    def test_显式关时回环首跳也不采信proto(self):
        app, c = self._app("0", cookie_secure="")
        self.assertFalse(self._secure_after(app, c, "127.0.0.1", "https"),
                         "关掉转发头信任后，回环首跳的 X-Forwarded-Proto 也必须丢弃")


class AmbiguousForwardedTrustWarningTest(_AppBase):
    """缺省且出现"回环来源 + XFF"时告警一次；显式配置后不再告警。"""

    def _warnings(self, trust_value, remote_addr="127.0.0.1", n_requests=2):
        _app, c = self._app(trust_value)

        def _hit():
            for _ in range(n_requests):
                c.get("/api/clock", headers={"X-Forwarded-For": "203.0.113.9"},
                      environ_base={"REMOTE_ADDR": remote_addr})
        return [ln for ln in _capture_web_log(_hit) if TRUST_ENV_KEY in ln]

    def test_缺省时告警一次且点名键(self):
        lines = self._warnings(None)
        self.assertEqual(len(lines), 1, f"歧义形态只许告警一次：{lines}")
        self.assertIn(TRUST_ENV_KEY, lines[0])
        self.assertIn("反向代理", lines[0])

    def test_显式配置后不再告警(self):
        for value in ("1", "0"):
            with self.subTest(value=value):
                self.assertEqual(self._warnings(value), [],
                                 f"{TRUST_ENV_KEY}={value} 是显式决定，不该再告警")

    def test_非回环来源不触发(self):
        """直连非回环（XFF 本就被丢弃）不是歧义形态，不该告警。"""
        self.assertEqual(self._warnings(None, remote_addr="203.0.113.9"), [])


class PwGateTierBucketSwitchIsRealTest(_AppBase):
    """实测 `tests/test_pw_gate_tiers.py` 的"换桶"是否真换成功。

    该文件用 `_xff=` 换出口却不给 `REMOTE_ADDR`。若 Flask 测试客户端的默认
    `REMOTE_ADDR` 不是回环，`_client_ip()` 会退回它，所有请求落进同一个桶 ⇒
    那批用例的"换环境"断言就是自证（换了个没人读的头）。本类把这条隐式依赖
    显式钉住：默认必须是回环，且两个不同 XFF 必须给出两个不同的桶。
    """

    #: 匿名可达的非 `/api/` 路径（`require_login` 对非 /api/ 直接放行）。
    ANON_PATH = "/login"

    def _probe(self, app):
        """在真 `test_client()` 请求里记录 remote_addr 与桶（不显式给 REMOTE_ADDR）。"""
        from flask import request
        seen = {}

        @app.before_request
        def _probe_client_addr():
            seen["remote_addr"] = request.remote_addr
            seen["bucket"] = self.webapp._client_ip()

        return seen

    def test_测试客户端默认remote_addr是回环(self):
        app, c = self._app(None)
        seen = self._probe(app)
        r = c.get(self.ANON_PATH, headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(seen.get("remote_addr"), "127.0.0.1",
                         "Flask 测试客户端默认 REMOTE_ADDR 变了——"
                         "tests/test_pw_gate_tiers.py 的换桶断言会退化成自证")
        self.assertEqual(seen.get("bucket"), "203.0.113.9")

    def test_两个不同XFF给出两个不同桶(self):
        app, c = self._app(None)
        seen = self._probe(app)
        buckets = []
        for xff in ("203.0.113.7", "203.0.113.9"):
            c.get(self.ANON_PATH, headers={"X-Forwarded-For": xff})
            buckets.append(seen["bucket"])
        self.assertEqual(buckets, ["203.0.113.7", "203.0.113.9"],
                         "两个不同 XFF 必须给出两个不同的桶（否则那批用例在自证）")


class TrustPredicateSingleSourceGuardTest(unittest.TestCase):
    """结构守卫：回环谓词只有一个定义点，信任门必须过开关。

    下一个人若在 `web/` 下再写第二份"首跳可信"判据（旧 `TRUSTED_PROXIES` 元组、
    `startswith("127.")` 之类），或让信任门绕过开关，本类必须变红。
    """

    WEB_PY = ("web/app.py", "web/security.py")

    @classmethod
    def setUpClass(cls):
        cls.srcs = {rel: _read(rel) for rel in cls.WEB_PY}
        cls.trees = {rel: ast.parse(src) for rel, src in cls.srcs.items()}

    def _defs(self, name):
        out = []
        for rel, tree in self.trees.items():
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and node.name == name:
                    out.append((rel, node))
        return out

    def test_回环谓词只有一个定义点(self):
        self.assertEqual([rel for rel, _n in self._defs("_is_loopback_addr")],
                         ["web/security.py"],
                         "回环谓词必须只在 web/security.py 定义一次")
        self.assertEqual(self._defs("_is_loopback_host"), [],
                         "旧的 `_is_loopback_host` 必须已并入 `_is_loopback_addr`")

    def test_旧的可信代理元组不得回归(self):
        for rel in self.WEB_PY:
            with self.subTest(rel=rel):
                self.assertNotIn("TRUSTED_PROXIES", self.srcs[rel],
                                 "`TRUSTED_PROXIES` 是同一事实的第二份取值域，不许回归")

    def test_信任门必须过开关与同一谓词(self):
        body = ast.get_source_segment(self.srcs["web/security.py"],
                                      self._defs("_client_ip")[0][1])
        self.assertIn("_forwarded_trust_enabled", body,
                      "`_client_ip` 必须过显式开关（否则开关是死键）")
        self.assertIn("_is_loopback_addr", body,
                      "`_client_ip` 必须用唯一回环谓词判首跳")
        proto = ast.get_source_segment(self.srcs["web/app.py"],
                                       self._defs("_forwarded_proto_is_https")[0][1])
        self.assertIn("_forwarded_trust_enabled", proto,
                      "`X-Forwarded-Proto` 侧必须与 XFF 侧同源同开关")
        self.assertNotIn('startswith("127.")', self.srcs["web/app.py"],
                         '`startswith("127.")` 是旧取值域，不许回归')


if __name__ == "__main__":
    unittest.main()
