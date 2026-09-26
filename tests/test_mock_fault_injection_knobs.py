# -*- coding: utf-8 -*-
"""假上游故障注入旋钮 + mock CA 证书扩展（E1）的 RED→GREEN 用例。

标签：J · 运维：部署/备份/发布
覆盖：mock_yiban 四类注入旋钮（登录失败 / 提交失败 / 风控挑战页 / 非 JSON 响应）与
   假成功档 login-shallow（最终认证 code==0 无签发回执）的
   默认关闭契约、注入形态与真实判据的对照（yiban/fyiban/waf.py 的
   looks_like_challenge 输入、yiban/security.py 的 is_waf_blocked 双判据形状——挑战形态
   不受长度限制、仅关键词命中按 len>2000 设界；假成功形状对照 protocol 的签发回执判据）、
   热读场景声明（--config 运行中切换，含 login-shallow）、记账对平（total==durable+log_errors、
   JSONL 行数==__stats.total、injected 计数）；mock_env.ensure_certs 生成的 CA
   带 basicConstraints(critical,CA:TRUE) 与 keyUsage(critical,keyCertSign,cRLSign)
   的**扩展存在性**断言与 strict TLS 活体握手；各旋钮 × run.sh 入口全链实跑
   （桩 signin 走真实客户端链 → 假易班进程按 CLI 旋钮注入 → JSONL 记账断言）
   与旋钮关闭态回归；引擎档位闭环（YIBAN_E2E_ENGINE_LOOP）：waf/nonjson/login-shallow
   经真实 attempt_signin+_retry_budget+清缓存联动决策，JSONL 对"总尝试=1"给出第三方证据，
   login-shallow 链另取证"登录成功"日志计数（假成功=0、真成功=1）与缓存零写入。
对应实现：scripts/loadtest/mock_yiban.py（旋钮与注入形态、MockState 记账）、
   scripts/loadtest/mock_env.py（ensure_certs）、run.sh（全链入口）、
   yiban/fyiban/waf.py 与 yiban/security.py（注入形态所对照的真实判据，只读）。
关键断言：注入旋钮是后续"WAF 分类与假成功"反例的证据基础设施，形态必须与**真实
   判据的输入**逐字
   对照而非自造：挑战页喂 looks_like_challenge（window.onload=setTimeout +
   eval("qo=eval;qo(po);") 双特征，注入在旧流程真实遇挑战的 GET /iapp7463 落点）；
   非 JSON 页喂"leg② 形状"——>2000 字符、无挑战形态的拦截 HTML 在 is_waf_blocked 的
   关键词长度界外放行、在 .json() 处抛 Expecting value:。CA 缺 keyUsage/basicConstraints 时 ≥3.14 默认
   VERIFY_X509_STRICT 会在握手层全灭且记账为 0（失败安静），因此扩展断言 +
   strict 握手必须同时钉住；记账与请求不对平的注入轮测不得用。
依赖：仅标准库 + requests（测试侧适配器）；证书用例需 PATH 上有 openssl（缺则跳过）；
   run.sh 全链用例需 bash（缺则整类跳过，Git Bash 即可）；mock 子进程只绑 127.0.0.1
   随机端口、明文 HTTP（TLS 只在握手用例里真跑）；不连外网，桩件零真实凭据。

用法（项目根目录）：
    py -m pytest tests/test_mock_fault_injection_knobs.py -v
"""
import http.client
import importlib
import io
import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_LOADTEST = os.path.join(_ROOT, "scripts", "loadtest")
if os.path.dirname(_LOADTEST) not in sys.path:
    sys.path.insert(0, os.path.dirname(_LOADTEST))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

mock_yiban = importlib.import_module("loadtest.mock_yiban")
mock_env = importlib.import_module("loadtest.mock_env")

from yiban import security  # noqa: E402
from yiban.fyiban import waf  # noqa: E402

_HAS_OPENSSL = shutil.which("openssl") is not None
_HAS_BASH = shutil.which("bash") is not None
_RUN_SH = os.path.join(_ROOT, "run.sh")


def _read_jsonl(path):
    """读 mock 落盘 JSONL；只容忍**末行**半写（并发追加），中间坏行照抛。"""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip()]
    rows = []
    for i, line in enumerate(lines):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break
            raise
    return rows


class KnobContractTest(unittest.TestCase):
    """旋钮存在性与默认关闭契约：不开 = 现行为零变化。"""

    def test_fail_stages_list_four_knobs(self):
        for knob in ("login", "signIn", "waf", "nonjson"):
            self.assertIn(knob, mock_yiban.FAIL_STAGES,
                          "四类注入旋钮须以 fail_stage 场景声明形式存在")
        cfg = mock_yiban.MockConfig()
        self.assertEqual(cfg.snapshot()["fail_stage"], "none",
                         "默认必须关闭（none）——不开时现行为零变化")
        self.assertEqual(cfg.snapshot()["fail_rate"], 0.0)

    def test_challenge_body_matches_real_recognition_input(self):
        body = mock_yiban.waf_challenge_body()
        self.assertTrue(waf.looks_like_challenge(body),
                        "挑战页必须命中 waf.looks_like_challenge 的真实特征对")
        self.assertTrue(waf.looks_like_challenge("", "https_ydclearance=x") is True)
        self.assertLess(len(body), 2000, "挑战页是短 JS 页，不得撞 is_waf_blocked 的长度界")
        self.assertFalse(any(kw in body for kw in security.WAF_KEYWORDS),
                         "挑战页体不应混入拦截关键词（关键词判据与形态判据各自独立喂）")
        self.assertTrue(security.is_waf_blocked(body),
                        "挑战形态经归一后的 is_waf_blocked 必须拦（形态判定不受长度限制）")

    def test_nonjson_body_reproduces_leg2_shape(self):
        body = mock_yiban.nonjson_block_body()
        self.assertGreater(len(body), 2000,
                           "leg② 形状=「>2000 拦截页过 is_waf_blocked 短路后 .json() 抛」")
        self.assertFalse(security.is_waf_blocked(body),
                         "长页仅关键词命中、无挑战形态——维持不拦（法律文本防误伤边界保留）")
        try:
            json.loads(body)
        except ValueError as e:
            self.assertIn("Expecting value:", str(e))
        else:
            self.fail("非 JSON 体必须让 .json() 抛 Expecting value:")

    def test_login_shallow_stage_contract(self):
        """假成功档：code==0 但无签发回执；默认关闭；只打带 verifyRequest 的完成认证步。"""
        self.assertIn("login-shallow", mock_yiban.FAIL_STAGES)
        self.assertEqual(mock_yiban.MockConfig().snapshot()["fail_stage"], "none",
                         "假成功档同样必须默认关闭——不开时对现行为零变化")
        cfg = mock_yiban.MockConfig(fail_stage="login-shallow", fail_rate=1.0)
        self.assertEqual(cfg.snapshot()["fail_stage"], "login-shallow")

    def test_help_and_readme_document_knobs_and_default_off(self):
        """约束说明义务：旋钮清单 + 默认关闭契约必须进 --help 与 loadtest README。"""
        p = subprocess.run([sys.executable, os.path.join(_LOADTEST, "mock_yiban.py"),
                            "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        for token in ("login", "signIn", "waf", "nonjson", "login-shallow",
                      "默认 none=全关", "零变化"):
            self.assertIn(token, p.stdout, "--help 必须列全旋钮并写明默认关闭")
        with io.open(os.path.join(_LOADTEST, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        for token in ("waf", "nonjson", "login-shallow", "默认全关", "零变化"):
            self.assertIn(token, readme, "README 必须同口径记录旋钮与默认关闭契约")


class _InProcessMock(unittest.TestCase):
    """进程内假服务端（明文 HTTP 随机端口）：旋钮开/关的形态与记账。"""

    def _serve(self, **cfg):
        log_dir = tempfile.mkdtemp(prefix="knob-mock-")
        self.addCleanup(shutil.rmtree, log_dir, True)
        self.log_path = os.path.join(log_dir, "mock.jsonl")
        state = mock_yiban.MockState(log_path=self.log_path)
        config = mock_yiban.MockConfig(**cfg)
        servers, state, config = mock_yiban.create_servers(
            host="127.0.0.1", port=0, state=state, config=config, enable_ipv6=False)
        self.server, self.state = servers[0], state
        self.port = self.server.server_address[1]
        t = threading.Thread(target=self.server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def _get(self, path, host="f.yiban.cn"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Host": host})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        status, headers = resp.status, dict(resp.getheaders())
        conn.close()
        return status, headers, body

    def _post(self, path, host="oauth.yiban.cn", data="x=1"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", path, body=data,
                     headers={"Host": host, "Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        status, headers = resp.status, dict(resp.getheaders())
        conn.close()
        return status, headers, body

    def _wait_rows(self, n, timeout=3.0):
        """等落盘记录到齐：mock 在**写完响应之后**才落盘，读早了会少最后一条。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = _read_jsonl(self.log_path)
            if len(rows) >= n:
                return rows
            time.sleep(0.02)
        return _read_jsonl(self.log_path)

    def test_knobs_off_baseline_transparent(self):
        self._serve()
        st, _, body = self._get("/code/html", host="oauth.yiban.cn")
        self.assertEqual((st, 'id="key"' in body), (200, True))
        st, _, body = self._get("/iapp7463")
        self.assertEqual(st, 302, "关闭态 /iapp7463 仍是旧流程 302 落点")
        st, _, body = self._post("/code/usersure")
        self.assertEqual((st, json.loads(body).get("code")), (200, "s200"))
        rows = self._wait_rows(3)
        self.assertEqual([r["injected"] for r in rows], [False, False, False])
        self.assertEqual(self.state.snapshot({})["injected"], 0)

    def test_waf_knob_injects_challenge_at_real_landing(self):
        self._serve(fail_stage="waf", fail_rate=1.0)
        st, headers, body = self._get("/iapp7463")
        self.assertEqual(st, 200)
        self.assertTrue(waf.looks_like_challenge(body),
                        "开启 waf 旋钮后落点响应必须可被真实判据识别为挑战页")
        self.assertIn("https_ydclearance", headers.get("Set-Cookie", ""))
        # 未注入的端点零变化：登录页仍是可解析的 HTML
        st, _, body = self._get("/code/html", host="oauth.yiban.cn")
        self.assertEqual((st, 'id="key"' in body), (200, True))
        rows = self._wait_rows(2)
        by_path = {r["path"]: r["injected"] for r in rows}
        self.assertEqual(by_path["/iapp7463"], True)
        self.assertEqual(by_path["/code/html"], False)
        snap = self.state.snapshot({})
        self.assertEqual(snap["injected"], 1)
        self.assertEqual(snap["total"], len(rows), "记账与 JSONL 对平")
        self.assertEqual(snap["durable"], snap["total"])
        self.assertEqual(snap["log_errors"], 0)

    def test_nonjson_knob_injects_long_block_on_json_endpoints(self):
        self._serve(fail_stage="nonjson", fail_rate=1.0)
        st, headers, body = self._post("/code/usersure")
        self.assertEqual(st, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertGreater(len(body), 2000)
        with self.assertRaises(ValueError) as ctx:
            json.loads(body)
        self.assertIn("Expecting value:", str(ctx.exception))
        # GET 登录页（HTML 期望点）不受该旋钮影响——旋钮只打 JSON 期望端点
        st, _, body = self._get("/code/html", host="oauth.yiban.cn")
        self.assertEqual((st, 'id="key"' in body), (200, True))
        snap = self.state.snapshot({})
        self.assertEqual(snap["injected"], 1)

    def test_login_shallow_knob_injects_receiptless_success(self):
        """假成功注入：完成认证步回 code==0 **无 data 载荷**；入口步与关闭态不受影响。"""
        self._serve(fail_stage="login-shallow", fail_rate=1.0)
        st, _, body = self._get("/base/c/auth/yiban?verifyRequest=x&CSRF=y",
                                host="api.uyiban.com")
        self.assertEqual(st, 200)
        payload = json.loads(body)
        self.assertEqual(payload.get("code"), 0, "假成功仍须伪装 code==0")
        self.assertNotIn("data", payload, "回执载荷必须缺失（这正是被拒的形状）")
        # 旧流程入口步（同路径、不带 verifyRequest）不受该档影响
        st, _, body = self._get("/base/c/auth/yiban?CSRF=y", host="api.uyiban.com")
        self.assertEqual(json.loads(body)["data"]["Data"],
                         "https://oauth.yiban.cn/code/html"
                         "?client_id=95626fa3080300ea"
                         "&redirect_uri=https://f.yiban.cn/iapp7463")
        rows = self._wait_rows(2)
        snap = self.state.snapshot({})
        self.assertEqual(snap["injected"], 1, "只注入完成认证那一跳")
        self.assertEqual([r["injected"] for r in rows], [True, False],
                         "假成功那一跳带 injected 标记、入口步不带（逐条可对账）")
        self.assertEqual(snap["total"], len(rows), "记账与 JSONL 对平")
        self.assertEqual(snap["durable"], snap["total"])

    def test_login_shallow_off_keeps_receipt(self):
        """关闭态：完成认证仍带回执（data 存在）——不开零变化。"""
        self._serve()
        st, _, body = self._get("/base/c/auth/yiban?verifyRequest=x&CSRF=y",
                                host="api.uyiban.com")
        self.assertEqual((st, "data" in json.loads(body)), (200, True))

    def test_hot_config_scenario_declaration_switches_at_runtime(self):
        tmp = tempfile.mkdtemp(prefix="knob-cfg-")
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg_path = os.path.join(tmp, "mock_config.json")
        with io.open(cfg_path, "w", encoding="utf-8") as f:
            f.write('{"fail_stage": "none"}')
        self._serve(config_path=cfg_path)
        st, _, _ = self._get("/iapp7463")
        self.assertEqual(st, 302, "场景未声明时零变化")
        with io.open(cfg_path, "w", encoding="utf-8") as f:
            f.write('{"fail_stage": "waf", "fail_rate": 1.0}')
        st, _, body = self._get("/iapp7463")
        self.assertEqual(st, 200)
        self.assertTrue(waf.looks_like_challenge(body), "热读场景声明须无需重启即生效")
        with io.open(cfg_path, "w", encoding="utf-8") as f:
            f.write('{"fail_stage": "none"}')
        st, _, _ = self._get("/iapp7463")
        self.assertEqual(st, 302, "场景撤回后回到零注入")

    def test_hot_config_can_switch_to_login_shallow(self):
        """假成功注入优先走 Task 11 的 --config 热读场景声明（无需额外通道）。"""
        tmp = tempfile.mkdtemp(prefix="knob-cfg-shallow-")
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg_path = os.path.join(tmp, "mock_config.json")
        with io.open(cfg_path, "w", encoding="utf-8") as f:
            f.write('{"fail_stage": "none"}')
        self._serve(config_path=cfg_path)
        auth = "/base/c/auth/yiban?verifyRequest=x&CSRF=y"
        st, _, body = self._get(auth, host="api.uyiban.com")
        self.assertEqual((st, "data" in json.loads(body)), (200, True), "关闭态带回执")
        with io.open(cfg_path, "w", encoding="utf-8") as f:
            f.write('{"fail_stage": "login-shallow", "fail_rate": 1.0}')
        st, _, body = self._get(auth, host="api.uyiban.com")
        payload = json.loads(body)
        self.assertEqual((st, payload.get("code"), "data" in payload), (200, 0, False),
                         "热读声明切档后必须是无回执的假成功")


@unittest.skipUnless(_HAS_OPENSSL, "需要 openssl 可执行文件")
class CertExtensionTest(unittest.TestCase):
    """E1 代码化：ensure_certs 生成的 CA 必须带齐 strict 校验所需扩展（防回归）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="knob-certs-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.certs = mock_env.ensure_certs(self.tmp, mock_env.DEFAULT_DOMAINS)

    def _text(self, pem_path):
        p = subprocess.run(["openssl", "x509", "-in", pem_path, "-noout", "-text"],
                           capture_output=True, text=True, check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_ca_carries_critical_basic_constraints_and_key_usage(self):
        text = self._text(self.certs["ca"])
        self.assertIn("X509v3 Basic Constraints: critical", text)
        self.assertIn("CA:TRUE", text)
        self.assertIn("X509v3 Key Usage: critical", text)
        self.assertIn("Certificate Sign", text)
        self.assertIn("CRL Sign", text)

    def test_server_cert_still_ca_false_with_san(self):
        text = self._text(self.certs["cert"])
        self.assertIn("CA:FALSE", text)
        self.assertIn("DNS:api.uyiban.com", text)

    def test_stale_ca_without_keyusage_is_upgraded_in_place(self):
        """存量幂等分支不得把"握手全灭"的坏 CA 永远留在盘上。"""
        self.tmp2 = tempfile.mkdtemp(prefix="knob-certs-stale-")
        self.addCleanup(shutil.rmtree, self.tmp2, True)
        ca_dir = os.path.join(self.tmp2, "ca")
        os.makedirs(ca_dir)
        ca_key = os.path.join(ca_dir, "ca.key")
        ca_pem = os.path.join(ca_dir, "ca.pem")
        # 先按修复前口径生成一把无扩展自签 CA（复刻存量）
        subprocess.run(["openssl", "genrsa", "-out", ca_key, "2048"],
                       check=True, capture_output=True)
        subprocess.run(["openssl", "req", "-x509", "-new", "-nodes", "-key", ca_key,
                        "-sha256", "-days", "3650", "-subj", "/CN=yiban-loadtest-ca",
                        "-out", ca_pem], check=True, capture_output=True)
        self.assertNotIn("X509v3 Key Usage", self._text(ca_pem))
        for name in ("server.key", "server.pem", "pub.pem"):
            with io.open(os.path.join(ca_dir, name), "w", encoding="utf-8") as f:
                f.write("placeholder")  # 凑齐"证书已存在"分支所需文件
        mock_env.ensure_certs(self.tmp2, mock_env.DEFAULT_DOMAINS)
        self.assertIn("X509v3 Key Usage: critical", self._text(ca_pem))
        self.assertIn("CA:TRUE", self._text(ca_pem))
        self.assertIn("BEGIN PUBLIC KEY",
                      io.open(os.path.join(ca_dir, "pub.pem"), encoding="utf-8").read())

    def test_strict_tls_handshake_against_mock(self):
        """活体反证 E1：strict 校验（≥3.14 默认开启）下对 mock 完成 TLS 握手。"""
        servers, _state, _ = mock_yiban.create_servers(
            host="127.0.0.1", port=0, cert=self.certs["cert"], key=self.certs["key"],
            enable_ipv6=False)
        self.addCleanup(servers[0].shutdown)
        self.addCleanup(servers[0].server_close)
        threading.Thread(target=servers[0].serve_forever, daemon=True).start()
        port = servers[0].server_address[1]
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.verify_flags |= ssl.VERIFY_X509_STRICT  # 显式置位：任何 Python 版本都按 ≥3.14 默认口径判
        ctx.load_verify_locations(cafile=self.certs["ca"])
        raw = socket.create_connection(("127.0.0.1", port), timeout=10)
        with ctx.wrap_socket(raw, server_hostname="api.uyiban.com") as tls:
            tls.sendall(b"GET /__health HTTP/1.1\r\nHost: api.uyiban.com\r\n"
                        b"Connection: close\r\n\r\n")
            data = b""
            while b"\r\n\r\n" not in data or not data.endswith(b"}"):
                chunk = tls.recv(4096)
                if not chunk:
                    break
                data += chunk
        self.assertIn(b"200", data.split(b"\r\n", 1)[0], data[:120])


# ---------------------------------------------------------------------------
# run.sh 入口全链：桩 signin 走真实客户端链，假易班以**独立进程 + CLI 旋钮**启动
# ---------------------------------------------------------------------------
_CHAIN_STUB = r'''# -*- coding: utf-8 -*-
import json, os, sys
sys.path.insert(0, %r)
sys.path.insert(0, os.path.join(%r, "scripts"))
import requests
from unittest import mock as _mock
from urllib.parse import urlsplit

STATE = os.environ.get("YIBAN_STATE_DIR", ".")
if "--second-run-check" in sys.argv:
    sys.exit(0)

BASE = "http://127.0.0.1:" + os.environ["YIBAN_E2E_MOCK_PORT"]


class _Rewrite(requests.adapters.BaseAdapter):
    def __init__(self):
        super().__init__()
        self._inner = requests.adapters.HTTPAdapter()

    def send(self, request, **kwargs):
        logical = request.url
        parts = urlsplit(request.url)
        request.headers["Host"] = parts.netloc
        request.url = BASE + parts.path + (("?"+parts.query) if parts.query else "")
        resp = self._inner.send(request, **kwargs)
        resp.url = logical
        return resp

    def close(self):
        self._inner.close()


def _out(payload, rc):
    with open(os.path.join(STATE, "engine_result.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    sys.exit(rc)


_guard = requests.Session()
_guard.trust_env = False
try:
    g = _guard.get(BASE + "/__health", headers={"Host": "oauth.yiban.cn"}, timeout=3)
    if "yiban-mock" not in g.headers.get("Server", "").lower():
        _out({"error": "loopback-intercepted: Server=%%s" %% g.headers.get("Server")}, 9)
except Exception as e:
    _out({"error": "loopback-guard: %%r" %% (e,)}, 9)

import signin
acc = signin.Account(phone="13800138000", password="secret-pw", account_id=0)
os.environ.pop("YIBAN_PROXY", None)
if os.environ.get("YIBAN_E2E_ENGINE_LOOP", "") == "1":
    # 引擎档位闭环：以真实 attempt_signin/_retry_budget/clear_session_cache_quiet 复刻
    # round 的逐账号决策顺序（尝试→档位→清缓存→上限比较）；会话缓存用真实临时库，
    # mock 的 JSONL 即"总尝试数"的第三方证据。RETRY_MIN_INTERVAL 休眠与重试落点采样
    # 与档位判据无关，本桩不复刻。
    import logging
    signin.db.init_db(db_file=os.path.join(STATE, "e2e.db"), cleanup=False)
    result = {"legacy": os.environ.get("YIBAN_LEGACY_LOGIN", "")}
    if os.environ.get("YIBAN_E2E_SEED_CACHE", "") == "1":
        signin.db.set_session_cache(acc.phone, json.dumps({"seeded": "1"}), "seeded-csrf")
    result["cache_before"] = signin.db.get_session_cache(acc.phone) is not None

    # "登录成功"日志的出现次数直接取证（假成功链上必须为 0、真成功链上恰好 1）：
    # 挂在协议 logger 上，桩是唯一 handler，不依赖 root 级别。
    _logged = []

    class _LogCap(logging.Handler):
        def emit(self, record):
            _logged.append(record.getMessage())

    _plog = logging.getLogger("yiban.fyiban.protocol")
    _plog.addHandler(_LogCap())
    _plog.setLevel(logging.INFO)

    _RealClient = signin.YibanClient

    class _LoopClient(_RealClient):
        def __init__(self, account):
            super().__init__(account)
            self.session.mount("https://", _Rewrite())
            self.session.trust_env = False
            self.session.proxies = {}

    signin.YibanClient = _LoopClient
    attempts_n = 0
    success, message, skip, status = False, "", False, ""
    while attempts_n < 10:
        attempts_n += 1
        success, message, skip, status = signin.attempt_signin(acc)
        if success or skip:
            break
        budget, clear_cache = signin._retry_budget(message)
        if clear_cache:
            signin.clear_session_cache_quiet(acc.phone)
        if attempts_n >= budget:
            break
    result["attempts"] = attempts_n
    result["signin"] = [bool(success), str(message), bool(skip), str(status)]
    result["cache_after"] = signin.db.get_session_cache(acc.phone) is not None
    result["login_success_logged"] = sum(1 for m in _logged if "登录成功" in m)
    if not success:
        result["error"] = str(message)
    _out(result, 0 if success else 1)
with _mock.patch.object(signin.db, "is_initialized", return_value=False):
    client = signin.YibanClient(acc)
client.session.mount("https://", _Rewrite())
client.session.trust_env = False
client.session.proxies = {}

result = {"legacy": os.environ.get("YIBAN_LEGACY_LOGIN", "")}
try:
    if result["legacy"] == "1":
        client.login()
    else:
        client.login_killyiban()
    result["login"] = "ok"
    ok, msg = client.verify()
    result["verify"] = [bool(ok), str(msg)]
    if ok:
        success, message, skip, status = client.signin()
        result["signin"] = [bool(success), str(message), bool(skip), str(status)]
except Exception as e:
    result["error"] = "%%s: %%s" %% (type(e).__name__, e)
    _out(result, 1)
all_ok = (result.get("login") == "ok" and result.get("verify", [False])[0]
          and result.get("signin", [False])[0])
_out(result, 0 if all_ok else 1)
'''

_FAKE_FLOCK_OK = "#!/usr/bin/env bash\nexit 0\n"
_FAKE_TIMEOUT_EXEC = '#!/usr/bin/env bash\nshift\nexec "$@"\n'
_PY_WRAPPER = '''#!/bin/sh
if [ "$1" = "-m" ]; then
    exit 0
fi
if [ "$1" = "-" ]; then
    exit ${E2E_FALLBACK_ALIVE:-1}
fi
exec "%s" "$@"
'''


@unittest.skipUnless(_HAS_BASH, "run.sh 全链用例需要 bash（Git Bash 即可）")
class RunShKnobChainTest(unittest.TestCase):
    """四旋钮 × run.sh 入口全链实跑 + 关闭态回归：注入形态出现且记账对平。"""

    def setUp(self):
        self.bash = shutil.which("bash")
        self.tmp = tempfile.mkdtemp(prefix="runsh-knob-")
        self.app = os.path.join(self.tmp, "app")
        self.state_dir = os.path.join(self.tmp, "state")
        self.lock = os.path.join(self.tmp, "lock")
        self.bin = os.path.join(self.tmp, "bin")
        for d in (os.path.join(self.app, "scripts"),
                  os.path.join(self.app, ".venv", "bin"),
                  self.state_dir, self.lock, self.bin):
            os.makedirs(d, exist_ok=True)
        stub = os.path.join(self.app, "scripts", "signin.py")
        with io.open(stub, "w", encoding="utf-8", newline="\n") as f:
            f.write(_CHAIN_STUB % (_ROOT, _ROOT))
        with io.open(os.path.join(self.app, ".env"), "w", encoding="utf-8") as f:
            f.write("YIBAN_SIGN_END=07:50\n")
        py_wrap = os.path.join(self.app, ".venv", "bin", "python3")
        with io.open(py_wrap, "w", encoding="utf-8", newline="\n") as f:
            f.write(_PY_WRAPPER % sys.executable.replace("\\", "/"))
        os.chmod(py_wrap, os.stat(py_wrap).st_mode | stat.S_IEXEC)
        for name, body in (("flock", _FAKE_FLOCK_OK), ("timeout", _FAKE_TIMEOUT_EXEC)):
            p = os.path.join(self.bin, name)
            with io.open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        self.mock_log = os.path.join(self.tmp, "mock.jsonl")
        self.mock_proc = None
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # ---- 假易班进程 ----
    def _start_mock(self, extra_args=()):
        ready = os.path.join(self.tmp, "mock.ready")
        cmd = [sys.executable, os.path.join(_LOADTEST, "mock_yiban.py"),
               "--no-tls", "--port", "0", "--no-ipv6",
               "--ready-file", ready, "--log", self.mock_log]
        cmd += list(extra_args)
        self.mock_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.PIPE)
        self.addCleanup(self._stop_mock)
        deadline = time.time() + 20
        while time.time() < deadline:
            if os.path.exists(ready) and os.path.getsize(ready) > 0:
                break
            if self.mock_proc.poll() is not None:
                err = self.mock_proc.stderr.read().decode("utf-8", "replace")
                self.fail("mock 子进程提前退出: %s" % err)
            time.sleep(0.1)
        else:
            self.fail("mock 子进程未就绪")
        with io.open(ready, encoding="utf-8") as f:
            return int(f.read().strip())

    def _stop_mock(self):
        if self.mock_proc and self.mock_proc.poll() is None:
            self.mock_proc.terminate()
            try:
                self.mock_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.mock_proc.kill()

    def _stats(self, port):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/__stats", headers={"Host": "api.uyiban.com"})
        body = conn.getresponse().read().decode("utf-8")
        conn.close()
        return json.loads(body)

    def _run(self, port, **controls):
        env = dict(os.environ)
        env.update({
            "PATH": self.bin + os.pathsep + env.get("PATH", ""),
            "YIBAN_APP_DIR": self.app,
            "YIBAN_STATE_DIR": self.state_dir,
            "YIBAN_LOCK_DIR": self.lock,
            "YIBAN_LOG_FILE": os.path.join(self.state_dir, "sign.log"),
            "YIBAN_SECOND_RUN_TIME": "00:01",
            "STATE_DIR": self.state_dir,
            "YIBAN_E2E_MOCK_PORT": str(port),
        })
        for k in ("YIBAN_SECOND_RUN", "YIBAN_HOST_SECOND_ROUND", "YIBAN_EXECUTORS",
                  "YIBAN_WORKERS", "YIBAN_FALLBACK_ENABLE", "YIBAN_GLOBAL_PAUSE",
                  "YIBAN_EXECUTOR_ID", "YIBAN_SIGN_START", "YIBAN_PROXY",
                  "YIBAN_LEGACY_LOGIN"):
            env.pop(k, None)
        if controls.get("legacy"):
            env["YIBAN_LEGACY_LOGIN"] = "1"
        if controls.get("engine_loop"):
            env["YIBAN_E2E_ENGINE_LOOP"] = "1"
        if controls.get("seed_cache"):
            env["YIBAN_E2E_SEED_CACHE"] = "1"
        with io.open(os.path.join(self.state_dir, "check_exit"), "w", encoding="utf-8") as f:
            f.write("0")
        r = subprocess.run([self.bash, _RUN_SH], capture_output=True, env=env,
                           cwd=self.app, timeout=240, stdin=subprocess.DEVNULL)
        result_path = os.path.join(self.state_dir, "engine_result.json")
        self.assertTrue(os.path.exists(result_path),
                        "run.sh 未驱动到签到轮（engine_result 缺失）：%s\n%s"
                        % (r.stdout.decode("utf-8", "replace"),
                           r.stderr.decode("utf-8", "replace")))
        with io.open(result_path, encoding="utf-8") as f:
            engine = json.load(f)
        return r, engine

    def _assert_accounting(self, port, expected_paths, expected_injected):
        rows = []
        stats = {}
        for _ in range(100):  # 末条 JSONL 可能比 run.sh 收尾晚几毫秒落盘
            rows = _read_jsonl(self.mock_log)
            stats = self._stats(port)
            if len(rows) == stats["total"] == len(expected_paths):
                break
            time.sleep(0.05)
        self.assertEqual([r["path"] for r in rows], expected_paths)
        self.assertEqual(sum(1 for r in rows if r["injected"]), expected_injected)
        self.assertEqual(stats["total"], len(rows), "记账 total 与 JSONL 行数对平")
        self.assertEqual(stats["injected"], expected_injected)
        self.assertEqual(stats["durable"], stats["total"])
        self.assertEqual(stats["log_errors"], 0, "有请求处理了却没落盘——本轮测不得用")

    def test_off_regression_full_chain_success(self):
        port = self._start_mock()
        r, engine = self._run(port)
        self.assertEqual(engine.get("error"), None, engine)
        self.assertTrue(engine.get("verify", [False])[0])
        self.assertTrue(engine.get("signin", [False])[0])
        self._assert_accounting(port, [
            "/code/html", "/code/usersure", "/iframe/index", "/base/c/auth/yiban",
            "/nightAttendance/student/index/signPosition",
            "/nightAttendance/student/index/signPosition",
            "/nightAttendance/student/index/signIn",
        ], 0)
        hosts = set(self._stats(port)["by_host"])
        self.assertTrue(hosts.issubset(set(mock_env.DEFAULT_DOMAINS)), hosts)
        self.assertEqual(r.returncode, 0, "关闭态：run.sh 整轮成功")

    def test_login_fail_knob(self):
        port = self._start_mock(["--fail-stage", "login", "--fail-rate", "1.0"])
        _, engine = self._run(port)
        self.assertIn("登录失败", engine["error"])
        self.assertIn("mock injected login failure", engine["error"])
        self._assert_accounting(port, ["/code/html", "/code/usersure"], 1)

    def test_submit_fail_knob(self):
        port = self._start_mock(["--fail-stage", "signIn", "--fail-rate", "1.0"])
        _, engine = self._run(port)
        self.assertEqual(engine.get("error"), None, engine)
        self.assertFalse(engine["signin"][0], "提交失败须落成业务失败而非异常")
        self.assertIn("mock injected signIn failure", engine["signin"][1])
        self._assert_accounting(port, [
            "/code/html", "/code/usersure", "/iframe/index", "/base/c/auth/yiban",
            "/nightAttendance/student/index/signPosition",
            "/nightAttendance/student/index/signPosition",
            "/nightAttendance/student/index/signIn",
        ], 1)

    def test_waf_challenge_knob(self):
        port = self._start_mock(["--fail-stage", "waf", "--fail-rate", "1.0"])
        _, engine = self._run(port, legacy=True)
        self.assertIn("ydclearance 挑战解析失败", engine["error"],
                      "旧流程落点遇挑战页必须走 looks_like_challenge→solve 的真实解析支路")
        self._assert_accounting(port, [
            "/base/c/auth/yiban", "/code/html", "/code/usersure", "/iapp7463",
        ], 1)

    def test_nonjson_knob(self):
        port = self._start_mock(["--fail-stage", "nonjson", "--fail-rate", "1.0"])
        _, engine = self._run(port)
        self.assertIn("Expecting value:", engine["error"],
                      "leg② 现网文案是 requests 的 Expecting value: ——注入须引出同一文案")
        self._assert_accounting(port, ["/code/html", "/code/usersure"], 1)

    def test_login_shallow_knob(self):
        """假成功档全链：客户端必须以"无签发方回执"拒绝，链停在最终认证之后。"""
        port = self._start_mock(["--fail-stage", "login-shallow", "--fail-rate", "1.0"])
        r, engine = self._run(port)
        self.assertIn("无签发方回执", engine.get("error", ""), engine)
        self.assertNotIn("login", engine, "假成功不得置登录成功（引擎侧无 login=ok）")
        self.assertEqual(r.returncode, 1, "假成功轮必须以失败 rc 收尾，不得误报 0")
        self._assert_accounting(port, [
            "/code/html", "/code/usersure", "/iframe/index", "/base/c/auth/yiban",
        ], 1)

    # ---- 引擎档位闭环：MF-71 的"总尝试=1 + 清会话"在全链上的第三方证据 ----
    def test_waf_knob_engine_loop_single_attempt_clears_cache(self):
        """waf 旋钮 → 真实 attempt_signin 链 → 挑战解析失败 → 显式档：尝试 1 次即止、
        种子会话缓存被联动清除、mock 记账恰为**一次**登录链（旧档位=3 次在此为红）。"""
        port = self._start_mock(["--fail-stage", "waf", "--fail-rate", "1.0"])
        _, engine = self._run(port, legacy=True, engine_loop=True, seed_cache=True)
        self.assertIn("ydclearance 挑战解析失败", engine.get("error", ""))
        self.assertEqual(engine.get("cache_before"), True, "前置：种子会话缓存存在")
        self.assertEqual(engine.get("attempts"), 1,
                         "硬失败档总尝试必须=1（旧口径落普通档 3 次并复用死会话）")
        self.assertEqual(engine.get("cache_after"), False,
                         "档位联动 clear_session_cache_quiet 必须清掉会话缓存")
        self._assert_accounting(port, [
            "/base/c/auth/yiban", "/code/html", "/code/usersure", "/iapp7463",
        ], 1)

    def test_nonjson_knob_engine_loop_single_attempt(self):
        """nonjson 旋钮 → leg② "Expecting value:" → 显式档：尝试 1 次即止、不再伪装
        网络抖动打满重试；种子会话缓存被联动清除；记账为一次登录链。"""
        port = self._start_mock(["--fail-stage", "nonjson", "--fail-rate", "1.0"])
        _, engine = self._run(port, engine_loop=True, seed_cache=True)
        self.assertIn("Expecting value:", engine.get("error", ""))
        self.assertEqual(engine.get("attempts"), 1)
        self.assertEqual(engine.get("cache_before"), True, "前置：种子会话缓存存在")
        self.assertEqual(engine.get("cache_after"), False,
                         "硬失败档联动 clear_session_cache_quiet 必须清掉会话缓存")
        self._assert_accounting(port, ["/code/html", "/code/usersure"], 1)

    def test_engine_loop_off_regression(self):
        """关闭态（无注入）走引擎档位循环：一次成功、会话缓存被正常保存、零注入记账。"""
        port = self._start_mock()
        _, engine = self._run(port, engine_loop=True)
        self.assertEqual(engine.get("error"), None, engine)
        self.assertTrue(engine.get("signin", [False])[0], "关闭态整链必须成功")
        self.assertEqual(engine.get("attempts"), 1)
        self.assertEqual(engine.get("cache_after"), True,
                         "登录成功路径应写会话缓存（档位闭环不干扰成功侧）")
        self.assertEqual(engine.get("login_success_logged"), 1,
                         '"登录成功"取证通道在场且真成功恰落一次（回执判据不改真成功行为）')
        self._assert_accounting(port, [
            "/code/html", "/code/usersure", "/iframe/index", "/base/c/auth/yiban",
            "/nightAttendance/student/index/signPosition",
            "/nightAttendance/student/index/signIn",
        ], 0)

    def test_login_shallow_knob_engine_loop_single_attempt_no_cache(self):
        """假成功 × 引擎档位闭环：拒绝（A 段不可重试档 ⇒ 总尝试=1）、联动清种子缓存、
        "登录成功"日志零出现、mock 记账恰一条假成功注入。"""
        port = self._start_mock(["--fail-stage", "login-shallow", "--fail-rate", "1.0"])
        _, engine = self._run(port, engine_loop=True, seed_cache=True)
        self.assertIn("无签发方回执", engine.get("error", ""), engine)
        self.assertEqual(engine.get("attempts"), 1,
                         "假成功落不可重试档：总尝试必须=1（拒绝后重发同一应答必然同果）")
        self.assertEqual(engine.get("cache_before"), True, "前置：种子会话缓存存在")
        self.assertEqual(engine.get("cache_after"), False,
                         "种子缓存被档位联动清除，且假成功零写入——全链后必须无缓存行")
        self.assertEqual(engine.get("login_success_logged"), 0,
                         '"登录成功"日志在假成功链上必须零出现（审计不可信是登记的直接后果）')
        self._assert_accounting(port, [
            "/code/html", "/code/usersure", "/iframe/index", "/base/c/auth/yiban",
        ], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
