# -*- coding: utf-8 -*-
"""急停端到端可复跑链路：`.env` 暂停 → run.sh 真链引擎 rc=2 → 页面显示与事实同源。

标签：D · 状态词汇与账号生命周期
覆盖：run.sh 入口驱动的**真实引擎**（scripts/signin.py 兼容壳 → yiban/engine/runner.main）
    在 `.env` 写 `YIBAN_GLOBAL_PAUSE=1` 时整轮 rc=2、run.sh 按判码契约写状态文件
    GLOBAL_PAUSED、引擎日志打暂停门措辞、假上游记账为零；把同一份 `.env` 的暂停键翻成 0
    后同一条链的 rc/状态文件/页面显示**一起翻转**（判据与显示都现读同一份文件）；
    解除轮的判码**由构造决定、不由平台/日期/用例先后决定**：台账归零 + 运行锁空置 ⇒
    rc=2/SKIPPED（干净腿），持住运行锁构造忙态 ⇒ rc=3 且不写状态文件（活体反例
    "忙态被拒"）；
    web 侧 `_day_off_reason` 与日历页内联上下文（day_off/图例/状态表载荷）与引擎判定同源；
    活体反例：伪造 web 读源（read_env 桩成空）或只翻文件一侧 ⇒ 同源性判据必须变红
对应实现：run.sh（`.env` 装载、`_write_status_from_exit` 判码契约）、
    yiban/engine/runner.py（`_day_off_skip`→2、取锁争抢→3）、
    yiban/engine/cli_support.py（`_acquire_run_lock`：忙/无锁后端一律 fail-closed）、
    yiban/engine/schedule.py（`day_off`
    三道门的唯一实现）、web/app.py（`_day_off_reason` 现读注入）、
    web/services/signstatus.py（`DAY_OFF_TEXT`/`day_off_payload`）、yiban/status.py
    （`DISPLAY`/`legend_items`/`display_payload`）、scripts/loadtest/mock_yiban.py（假上游记账）
关键断言：rc 契约本单**只钉不改**：暂停命中 ⇒ 引擎 rc=2 且 run.sh 状态文件为
    GLOBAL_PAUSED（与窗口/账号级跳过的 SKIPPED 分文不误）；同源判据 = 引擎侧
    `day_off(同一份 .env)` 与 web 侧 `_day_off_reason` 相等，且页面内联 day_off 载荷
    的文案/语气档逐字段取自 `DAY_OFF_TEXT`/表派生——伪造任一侧后该等式必须不成立，
    用例里以显式 assertNotEqual 钉住"判据会咬人"，不是恒真比较
依赖：bash（缺则整类 skip，Git Bash 即可）；run.sh 用 fakebin 假 flock/真透传 timeout；
    引擎子进程的 https 由**测试侧**传输层适配器改写到 127.0.0.1 上的 mock_yiban 独立进程
    （回环劫持守卫失败即 rc=9 拒绝跑链）；Flask test client 真渲染日历页；
    不联网、零真实凭据（假号 13800000001 / 假密码 / 64 位假钥）

**为什么需要**：上一轮整改的端到端主证据来自一次性临时脚本（跑完即弃），聚焦用例虽覆盖
同一断言面，但没有一条能**重放全链**的机器证据：`.env`（真值）→ run.sh（真解析、真判码写
状态）→ runner.main（真门、真退出码）→ mock 上游（真零请求第三方证据）→ web 显示（真读同一
份文件）。账号级"自暂停"（DB `user_paused`）一侧的 web 派发前过滤与引擎拦截已由
`tests/test_manual_paused_spawn_filter.py`（Popen 打桩逐字对 argv）与
`tests/test_global_pause.py`（手动腿豁免红线）覆盖，本文件不重复；仅在解除轮用
`user_paused=true` 的测试账号做**离线性保险**（保证任何时刻跑链都在登录之前被账号级门挡下，
不触真实网络），并断言暂停键翻 0 后 GLOBAL_PAUSED 判码随之消失。
"""
import contextlib
import glob
import http.client
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_SH = os.path.join(BASE, "run.sh")
MOCK_YIBAN = os.path.join(BASE, "scripts", "loadtest", "mock_yiban.py")
_HAS_BASH = shutil.which("bash") is not None
# 运行锁争抢的构造件（忙态反例要真的"持住"锁）。注意判据必须落在 fcntl **模块本身**
# （与引擎同源，见 yiban/engine/cli_support.py 的 try-import）：旧版在此写
# `hasattr(os, "fcntl")` 恒为 False——fcntl 是顶层模块，`os` 上没有这个属性——于是
# "按平台分支的解除轮断言"永远走 rc=3 一侧，Linux 语义的 rc=2 一侧从未生效。
try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None

TEST_KEY = "a" * 64
FAKE_PHONE = "13800000001"
FAKE_PW = "e2e-fake-pw-not-a-real-secret"
USER_EMAIL = "chain-e2e-user@example.com"
USER_PW = "ChainE2ePass#2026"

#: 引擎入口在临时 APP_DIR 下的转调桩：真壳真 runner 全量执行，仅把 https **传输层**
#: 改写到 127.0.0.1 假易班（与既有假上游用例的适配器同型）——判定、门、rc、写盘都是真码。
_CHAIN_SHIM = '''# -*- coding: utf-8 -*-
import os, sys
sys.path.insert(0, %r)
sys.path.insert(0, os.path.join(%r, "scripts"))
import requests
from urllib.parse import urlsplit

import signin

_BASE = "http://127.0.0.1:" + os.environ["YIBAN_E2E_MOCK_PORT"]


class _Rewrite(requests.adapters.BaseAdapter):
    """连接层改写：逻辑 URL/Host 保留，只把去向钉死在回环假易班上。"""

    def __init__(self):
        super().__init__()
        self._inner = requests.adapters.HTTPAdapter()

    def send(self, request, **kwargs):
        logical = request.url
        parts = urlsplit(request.url)
        request.headers["Host"] = parts.netloc
        request.url = _BASE + parts.path + (("?"+parts.query) if parts.query else "")
        resp = self._inner.send(request, **kwargs)
        resp.url = logical  # 还原逻辑形态，跳转白名单判据看到的仍是真实链形状
        return resp

    def close(self):
        self._inner.close()


class _HealthGuard(requests.adapters.BaseAdapter):
    """回环劫持守卫专用：不还原 resp.url（本会话只做同机直连探测）。"""

    def __init__(self):
        super().__init__()
        self._inner = requests.adapters.HTTPAdapter()

    def send(self, request, **kwargs):
        parts = urlsplit(request.url)
        request.headers["Host"] = parts.netloc
        request.url = _BASE + parts.path
        return self._inner.send(request, **kwargs)

    def close(self):
        self._inner.close()


# 先探活：本机加速器/TUN 若按 Host 转发回环，探测就会打到真站——必须立刻拒跑而不是
# "测试失败但真实请求已经发生"。守卫会话与业务会话**挂载不同**：守卫保留逻辑 URL，
# requests 会把 https 会话的重定向判成跨主机而丢弃 Host，真站侧 SNI 就丢回了 127.0.0.1
# （实测 3.14：被劫持机上此探返回真站 301、判据恒绿——假绿即"静默放行真实易班"）。
# 业务适配器仍还原逻辑 URL（跳转白名单需要），两种挂载各司其职。
_guard = requests.Session()
_guard.trust_env = False
_guard.proxies = {}
_guard.mount("https://", _HealthGuard())
try:
    _g = _guard.get("https://oauth.yiban.cn/__health", timeout=3, allow_redirects=False)
    if "yiban-mock" not in _g.headers.get("Server", "").lower():
        sys.stderr.write("loopback intercepted: Server=%%s\\n" %% _g.headers.get("Server"))
        sys.exit(9)
except Exception as e:
    sys.stderr.write("loopback guard failed: %%r\\n" %% (e,))
    sys.exit(9)

_RealClient = signin.YibanClient


class _E2EClient(_RealClient):
    def __init__(self, account):
        super().__init__(account)
        self.session.mount("https://", _Rewrite())
        self.session.trust_env = False
        self.session.proxies = {}


signin.YibanClient = _E2EClient  # 同名转发壳：属性落到真正持有 YibanClient 的实现模块

sys.exit(signin.main(sys.argv[1:]))
'''

_FAKE_FLOCK_OK = "#!/usr/bin/env bash\nexit 0\n"
_FAKE_TIMEOUT_EXEC = '#!/usr/bin/env bash\nshift\nexec "$@"\n'
_PY_WRAPPER = '#!/bin/sh\nexec "%s" "$@"\n'


def _posix(p):
    return str(p).replace("\\", "/")


def _read_jsonl(path):
    """读假易班 JSONL；只容忍**末行**半写（并发追加），中间坏行照抛。"""
    if not os.path.exists(path):
        return []
    with io.open(path, encoding="utf-8") as f:
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


class _ChainBase(unittest.TestCase):
    """临时 APP_DIR + run.sh 真链 + 进程外假易班 + 隔离加载的 web/app.py。"""

    @classmethod
    def setUpClass(cls):
        cls.ctmp = tempfile.mkdtemp(prefix="yiban-chain-e2e-cls-")
        # APP_DIR 与 `.env` 是类级的：run.sh 按契约只认 `$APP_DIR/.env`，web 的 ENV_FILE
        # 也指到这同一个文件——**两侧读的就是这一个文件**（同源性判据的物质基础）。
        # 每轮只隔离 state/lock 目录，run.sh 的标记与状态文件按轮互不沾染。
        cls.app = os.path.join(cls.ctmp, "app")
        os.makedirs(os.path.join(cls.app, "scripts"), exist_ok=True)
        os.makedirs(os.path.join(cls.app, ".venv", "bin"), exist_ok=True)
        with io.open(os.path.join(cls.app, "scripts", "signin.py"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write(_CHAIN_SHIM % (BASE, BASE))
        py_wrap = os.path.join(cls.app, ".venv", "bin", "python3")
        with io.open(py_wrap, "w", encoding="utf-8", newline="\n") as f:
            f.write(_PY_WRAPPER % _posix(sys.executable))
        os.chmod(py_wrap, os.stat(py_wrap).st_mode | stat.S_IEXEC)
        cls.env_file = os.path.join(cls.app, ".env")
        cls.db_file = os.path.join(cls.ctmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.ctmp, "accounts.json")
        with io.open(cls.accounts_file, "w", encoding="utf-8") as f:
            f.write("[]")
        # web 进程侧（本测试进程）的键：与 .env 写同一份路径，两侧读同一个文件。
        # 暂停/周末键刻意**不进**进程环境——伪造 web 读源的用例依赖这一点。
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.ctmp
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        for k in ("YIBAN_GLOBAL_PAUSE", "YIBAN_SATURDAY_SIGN", "YIBAN_SUNDAY_SIGN"):
            os.environ.pop(k, None)
        import db as _db
        cls._db = _db
        _db.init_db(cls.db_file, migrate_from=cls.accounts_file, env_file=cls.env_file)
        spec = importlib.util.spec_from_file_location(
            "webapp_chain_e2e", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_chain_e2e"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        cls.user = _db.find_user(USER_EMAIL)
        if cls.user is None:
            _db.create_user(USER_EMAIL, cls.webapp.generate_password_hash(USER_PW))
            cls.user = _db.find_user(USER_EMAIL)

    @classmethod
    def tearDownClass(cls):
        if cls._db._conn is not None:
            with contextlib.suppress(Exception):
                cls._db._conn.close()
            cls._db._conn = None
        sys.modules.pop("webapp_chain_e2e", None)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_ACCOUNTS_FILE", "YIBAN_DISABLE_PURGE_LOOP"):
            os.environ.pop(k, None)
        shutil.rmtree(cls.ctmp, ignore_errors=True)

    # ---- 夹具 ----
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-chain-e2e-")
        self.state = os.path.join(self.tmp, "state")
        self.lock = os.path.join(self.tmp, "lock")
        self.bin = os.path.join(self.tmp, "bin")
        for d in (self.state, self.lock, self.bin):
            os.makedirs(d, exist_ok=True)
        for name, body in (("flock", _FAKE_FLOCK_OK), ("timeout", _FAKE_TIMEOUT_EXEC)):
            p = os.path.join(self.bin, name)
            with io.open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        self.mock_log = os.path.join(self.tmp, "mock.jsonl")
        self.mock_proc = None
        self.mock_port = self._start_mock()
        # web 侧现读路径显式钉到类级唯一 .env（ENV_FILE 可被测试改写是既有注入口径）
        self.webapp.ENV_FILE = self.env_file
        self._write_env(pause="1", user_paused="false")

    def tearDown(self):
        self._stop_mock()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_env(self, *, pause, user_paused, run_lock_wait=None):
        """唯一真值出口：本轮 run 的一切"暂停/自暂停"事实都只写在这一个文件里。

        `run_lock_wait`：把全量轮引擎等锁上限（`YIBAN_RUN_LOCK_WAIT`，秒）写进 `.env`，
        运行锁忙态用例据此把默认的 600s 压成几秒（等满即拒跑 rc=3）；不传则该键
        根本不存在，引擎走默认值。配置真值只有 `.env` 一个出口，测试不打环境补丁。
        """
        accounts_json = json.dumps([{
            "phone": FAKE_PHONE, "password": FAKE_PW,
            "name": "e2e-fake", "user_paused": user_paused,
        }])
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                "YIBAN_ACCOUNTS_KEY=%s\n" % TEST_KEY +
                "YIBAN_DB_FILE=%s\n" % _posix(self.db_file) +
                "YIBAN_SATURDAY_SIGN=1\n"
                "YIBAN_SUNDAY_SIGN=1\n"      # 周末门显式放行：任何星期跑链，暂停键都是唯一变量
                "YIBAN_SIGN_START=06:30\n"
                "YIBAN_SIGN_END=07:50\n"
                "YIBAN_ACCOUNTS_JSON=%s\n" % accounts_json +
                "YIBAN_GLOBAL_PAUSE=%s\n" % pause +
                (("YIBAN_RUN_LOCK_WAIT=%s\n" % run_lock_wait) if run_lock_wait else "")
            )

    def _zero_test_account_ledger(self):
        """把测试账号在领取池（sign_tasks / sign_claims）里的台账行清零。

        类级 DB 在同一 pytest 进程内跨用例、跨业务日共享：解除轮的断言若想要**构造
        决定**而非"谁先跑过、跑在哪个业务日"决定，发起轮次前必须显式把台账置为已知
        状态。机理复核（2026-09-26 批）实证：领取池的当日 claimed 残留行**不改变**
        本轮判码（照常 rc=2，只翻转 run.sh 补签/封存的收口形状），判码的忙源是引擎
        进程级运行锁而非台账——归零因此是"轮型唯一化"的保险，不是 rc 的成因。
        """
        con = sqlite3.connect(self.db_file)
        try:
            for table in ("sign_tasks", "sign_claims"):
                # 表未落地（迁移未及）：本就无台账行，等价于已归零
                with contextlib.suppress(sqlite3.OperationalError):
                    con.execute("DELETE FROM " + table + " WHERE phone=?", (FAKE_PHONE,))
            con.commit()
        finally:
            con.close()

    def _engine_run_lock_path(self):
        """引擎全量轮的进程级运行锁路径（与 cli_support._acquire_run_lock 同口径）。"""
        return os.path.join(self.state, "signin-run.lock")

    # ---- 假易班（进程外，Task 11 的 CLI 入口与记账） ----
    def _start_mock(self):
        ready = os.path.join(self.tmp, "mock.ready")
        cmd = [sys.executable, MOCK_YIBAN, "--no-tls", "--port", "0", "--no-ipv6",
               "--ready-file", ready, "--log", self.mock_log]
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

    def _mock_evidence(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.mock_port, timeout=5)
        conn.request("GET", "/__stats", headers={"Host": "api.uyiban.com"})
        stats = json.loads(conn.getresponse().read().decode("utf-8"))
        conn.close()
        rows = _read_jsonl(self.mock_log)
        business = [r for r in rows if r.get("path") not in ("/__health", "/__stats")]
        return stats, business

    # ---- run.sh 真链 ----
    def _run_chain(self):
        env = dict(os.environ)
        # 链上进程环境不得残留任何 YIBAN_* 键：配置真值只准来自本轮写下的 .env。
        for k in list(env):
            if k.startswith("YIBAN_"):
                env.pop(k, None)
        env.update({
            "PATH": self.bin + os.pathsep + env.get("PATH", ""),
            "YIBAN_APP_DIR": _posix(self.app),
            "YIBAN_STATE_DIR": _posix(self.state),
            "YIBAN_LOCK_DIR": _posix(self.lock),
            "YIBAN_LOG_FILE": _posix(os.path.join(self.state, "sign.log")),
            "YIBAN_TRIGGER": "chain-e2e",
            "YIBAN_SECOND_RUN_TIME": "00:01",
            "YIBAN_HOST_SECOND_ROUND": "0",   # 只断首签轮的判码；补签轮由宿主外壳用例另有覆盖
            "YIBAN_E2E_MOCK_PORT": str(self.mock_port),
        })
        r = subprocess.run([shutil.which("bash"), RUN_SH], capture_output=True,
                           env=env, cwd=self.app, timeout=240, stdin=subprocess.DEVNULL)
        statuses = []
        for p in sorted(glob.glob(os.path.join(self.state, "sign-status-*.txt"))):
            with io.open(p, encoding="utf-8") as f:
                statuses.append(f.read().strip())
        log_blob = ""
        for p in sorted(glob.glob(os.path.join(self.state, "*.log*"))):
            with io.open(p, encoding="utf-8", errors="replace") as f:
                log_blob += f.read()
        return {"rc": r.returncode, "statuses": statuses, "log": log_blob,
                "stdout": r.stdout.decode("utf-8", "replace"),
                "stderr": r.stderr.decode("utf-8", "replace")}

    # ---- web 显示侧读法（与既有日历用例同源口径） ----
    def _engine_truth_reason(self, now):
        """引擎侧事实判定：喂**同一份 .env** 的门唯一实现（子进程内引擎读的是它）。"""
        from yiban import clock
        from yiban.engine import schedule as yb_schedule
        from yiban.infra import env_io
        return yb_schedule.day_off(now or clock.now(),
                                   env=env_io.parse_env_file(self.env_file))

    def _calendar_html(self):
        c = self.webapp.create_app().test_client()
        with c.session_transaction() as s:
            s["auth"] = True
            s["role"] = "user"
            s["username"] = USER_EMAIL
            s["auth_source"] = "user"
            s["pw_version"] = self.user.get("pw_version", 1)
            s["login_ts"] = int(time.time())
            s["sid"] = "0" * 32
        r = c.get("/user/calendar")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        return r.get_data(as_text=True)

    @staticmethod
    def _inline_context(html):
        m = re.search(r"window\.YB_CALENDAR_STATE\s*=\s*(\{.*?\});</script>", html, re.S)
        assert m, "日历页未渲染内联状态上下文 window.YB_CALENDAR_STATE"
        return json.loads(m.group(1))


@unittest.skipUnless(_HAS_BASH, "全链用例需要 bash（Git Bash 即可）")
class GlobalPauseChainTest(_ChainBase):
    """.env 急停 → run.sh 真链 → rc/状态文件/页面显示三处一致。"""

    def test_pause_full_chain_rc2_and_display_same_source(self):
        from yiban import clock
        from yiban import status as yiban_status

        run = self._run_chain()
        # ① 判码契约（只钉不改）：引擎在 main 的门处 return 2，run.sh 原样透传退出。
        self.assertEqual(run["rc"], 2,
                         "急停轮 run.sh 退出码必须为 2（SKIPPED 语义）\n"
                         "stdout=%s stderr=%s log=%s" % (run["stdout"], run["stderr"], run["log"]))
        # ② run.sh 侧对**同一份 .env** 的解释：rc=2 + 暂停键 ⇒ GLOBAL_PAUSED（不是 SKIPPED/SUCCESS）。
        self.assertEqual(run["statuses"], ["GLOBAL_PAUSED"],
                         "run.sh 状态文件必须按判码契约写 GLOBAL_PAUSED：%s" % run["statuses"])
        # ③ 引擎侧的声音：门跳过文案（runner._GATE_SKIP_MESSAGES 的原文）。
        self.assertIn("签到已暂停（管理员通过 Web UI 一键暂停），跳过执行", run["log"])
        # ④ 第三方证据：假上游整轮零业务请求——暂停轮根本不该碰登录链。
        stats, business = self._mock_evidence()
        self.assertEqual((business, stats["total"]), ([], 0),
                         "急停轮出现了上游请求：链路没有按暂停语义短路")

        # ⑤ 显示与事实同源：web 判定 == 引擎判定（同一份文件、各自现读）。
        now = clock.now()
        engine_truth = self._engine_truth_reason(now)
        self.assertEqual(engine_truth, "paused")
        self.assertEqual(self.webapp._day_off_reason(now), engine_truth,
                         "web 显示判定必须与引擎读同一份 .env 得同一结论")

        # ⑥ 页面数据源（内联上下文）与显示表逐字段一致——显示的是"已暂停"，不是"失败/未签/排队"。
        html = self._calendar_html()
        ctx = self._inline_context(html)
        self.assertEqual(ctx["day_off"], {"reason": "paused",
                                          "text": "全局暂停（急停）：今日自动签到已停止",
                                          "tone": "warn"})
        self.assertNotIn("排队", ctx["day_off"]["text"])
        entry = yiban_status.DISPLAY[yiban_status.STATUS_GLOBAL_PAUSED]
        self.assertEqual(ctx["by_code"][yiban_status.STATUS_GLOBAL_PAUSED]["text"],
                         entry["text"], "页面状态行载荷与 DISPLAY 表已分叉")
        self.assertEqual(ctx["by_code"][yiban_status.STATUS_GLOBAL_PAUSED]["tone"],
                         entry["tone"])
        self.assertIn(entry["legend"], html, "图例缺急停条目（应与 DISPLAY 同源生成）")

    def test_release_flips_status_and_display_on_the_same_chain(self):
        """只翻暂停键 ⇒ 同一链路上 GLOBAL_PAUSED 判码与页面急停显示一起消失。

        这是"暂停导致的 rc=2/GLOBAL_PAUSED 来自暂停键本身"的反事实：键置 0 后
        run.sh 不再可能写出 GLOBAL_PAUSED、web 不再可能渲染急停；同时账号侧带
        `user_paused=true` 的测试号保证任何时刻跑链都在登录之前被挡下（不触网）。

        **判码由构造决定，不由平台分支/日期/用例先后决定**（机理复核 2026-09-26 批）：
        旧实现在这里用 `hasattr(os, "fcntl")` 分支 rc=2/rc=3——该判据恒为 False
        （fcntl 是顶层模块，`os` 上无此属性；引擎的平台判定在 cli_support 的
        try-import，见 yiban/engine/cli_support.py），rc=3 一侧永远生效，而真实轮
        取到哪一侧只取决于**引擎能否取到进程级运行锁**：被活进程占用（或锁后端缺失
        ——无 fcntl 的解释器在同一取锁点同样"不可用"）⇒ fail-closed rc=3，恰好
        "匹配"死分支而假绿；取得到 ⇒ 走账号级跳过 rc=2、判红。本文件每用例的
        STATE_DIR 都是新建 tmp，正常全量跑不存在占锁，故本用例把判码钉成构造的
        必然：
        ①台账归零（领取池行不是判码成因，实证见 `_zero_test_account_ledger`，清它是
        为了让 run.sh 补签/封存的收口形状唯一）；②前置断言本轮 STATE_DIR 里
        运行锁件不存在（每用例新建 tmp ⇒ 天然空置，断言把前提钉在明面上）。
        ⇒ 无条件钉：解除轮过门取锁成功、止步账号级自暂停（rc=2），run.sh 按判码
        契约写 SKIPPED。无锁后端平台（Windows）在同一取锁点 fail-closed 到 rc=3
        的既有语义不再在本用例分支，改由 `test_release_run_lock_busy_is_rejected_rc3`
        对照用例钉住（构造持锁 ⇒ 无锁后端平台同码），平台差异只留这段注记，不留死分支。
        """
        from yiban import clock
        self._write_env(pause="0", user_paused="true")
        self._zero_test_account_ledger()
        self.assertFalse(os.path.exists(self._engine_run_lock_path()),
                         "前提：本轮运行锁件应随每用例新建的 STATE_DIR 空置（解除轮 rc=2 依赖此前提）")
        run = self._run_chain()
        self.assertNotEqual(run["rc"], 0, "解除急停轮不得被误读为「放行真实签到」（本用例无真实凭据）")
        self.assertNotIn("GLOBAL_PAUSED", run["statuses"],
                         "暂停键已翻 0，run.sh 不得再按急停写 GLOBAL_PAUSED：%s" % run["statuses"])
        self.assertNotIn("签到已暂停（管理员通过 Web UI 一键暂停），跳过执行", run["log"],
                         "暂停门文案不得在解除轮出现（引擎判定必须跟 .env 走）")
        stats, business = self._mock_evidence()
        self.assertEqual((business, stats["total"]), ([], 0),
                         "解除轮也不得触碰上游（离线性保险：账号级门先于登录）")
        # 判码契约（无条件钉，构造决定）：门放行 → 取锁成功 → 账号级自暂停以"全 skip"
        # 收场（runner 返 2），run.sh 对 rc=2+非急停写 SKIPPED。
        self.assertEqual(run["rc"], 2,
                         "解除轮应止步账号级跳过（rc=2→SKIPPED）：log=%s" % run["log"])
        self.assertEqual(run["statuses"], ["SKIPPED"], run["statuses"])

        now = clock.now()
        self.assertEqual(self._engine_truth_reason(now), "", "引擎判定未随文件翻转")
        self.assertEqual(self.webapp._day_off_reason(now), "", "web 显示判定未随文件翻转")
        ctx = self._inline_context(self._calendar_html())
        self.assertIsNone(ctx["day_off"])

    @unittest.skipIf(_fcntl is None, "持锁构造需要 fcntl 锁后端（Windows 腿走同一判码但无从构造忙态）")
    def test_release_run_lock_busy_is_rejected_rc3(self):
        """活体反例（运行锁侧）：忙态下的解除轮必须被**拒跑**（rc=3），不得无锁照跑。

        rc=3（队列忙）族的唯一产线源头：引擎进程级运行锁 `signin-run.lock` 的争抢
        ——`_RunLockHeld`/`_RunLockUnavailable`（等待超时、无锁后端、锁件不可开）
        一律 fail-closed 返 3（yiban/engine/runner.py），run.sh 对非 0/2 码**不写
        状态文件**（`_write_status_from_exit`）⇒ statuses 为空。
        构造：与干净腿同一轮型（暂停键翻 0 + 账号级自暂停）且台账同样归零——本轮
        **唯一变量是"锁忙/锁闲"**：本测试进程用 fcntl 持住该锁件（模拟"前一轮还
        活着"），`.env` 把等锁上限压到 3s（引擎等满即拒）。干净腿钉 rc=2、本用例钉
        rc=3，两侧都由构造保证，与业务日、用例先后、平台一律无关。
        """
        self._write_env(pause="0", user_paused="true", run_lock_wait="3")
        self._zero_test_account_ledger()
        fh = io.open(self._engine_run_lock_path(), "a+", encoding="utf-8")
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        try:
            run = self._run_chain()
        finally:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
            fh.close()
        self.assertEqual(run["rc"], 3,
                         "锁忙态的解除轮必须走队列忙判码 3（拿不到互斥绝不无锁照跑）：%s"
                         % run["log"])
        self.assertEqual(run["statuses"], [],
                         "rc=3 不属 run.sh 写状态文件的码族（忙≠已有结论）：%s" % run["statuses"])
        self.assertIn("拒绝运行", run["log"],
                      "忙态拒跑必须留痕（fail-closed 不得静默），log=%s" % run["log"])
        stats, business = self._mock_evidence()
        self.assertEqual((business, stats["total"]), ([], 0),
                         "被拒的忙轮同样不得触碰上游（运行锁在登录链之前）")

    def test_same_source_equality_bites_on_forged_display_side(self):
        """活体反例：把 web 读源伪造掉（旧病灶 `env=None` 落回 os.environ 的形状），
        同源性等式两侧必须不等——证明上一条用例的 assertEqual 不是恒真。"""
        from yiban import clock
        now = clock.now()
        truth = self._engine_truth_reason(now)
        self.assertEqual(truth, "paused")            # 本轮 .env 写着急停（setUp 默认）
        self.assertEqual(self.webapp._day_off_reason(now), truth)  # 正常态：两侧一致
        # 伪造 web 读源（旧病灶 `env=None` 落回 os.environ 的形状）：读不到文件真值。
        # 具体落点随星期漂移（周末门可能先以 saturday/sunday 形态出现）——判据不关心
        # 它错成哪一种，只关心它**不再是 paused**：真实日期下 ''/周末原因都构成失配。
        with mock.patch.object(self.webapp, "read_env", return_value={}):
            forged = self.webapp._day_off_reason(now)
        self.assertNotEqual(forged, "paused",
                            "read_env 桩必须演示得出'读不到真值'的旧病灶（急停显示丢失）")
        self.assertNotEqual(forged, truth,
                            "伪造显示侧后同源性判据必须变红——否则上一条用例的等式是假绿")

    def test_single_source_file_flip_moves_engine_and_page_together(self):
        """活体反例（事实侧）：引擎事实与显示各自独立地现读**同一份文件**；
        改写文件 ⇒ 两侧同时翻转，而上一轮 run.sh 落盘的 GLOBAL_PAUSED 仍是历史事实——
        显示跟的是源，不是旧 rc。"""
        from yiban import clock
        now = clock.now()
        self.assertEqual(self._engine_truth_reason(now), "paused")
        self.assertEqual(self.webapp._day_off_reason(now), "paused")
        stale_engine_fact = "paused"  # 上一急停轮的判码结果（run.sh 已写盘的历史事实）

        self._write_env(pause="0", user_paused="false")
        self.assertEqual(self._engine_truth_reason(now), "",
                         "引擎判定读的是改写后的同一份 .env")
        self.assertEqual(self.webapp._day_off_reason(now), "",
                         "web 显示判定与引擎读同一份 .env，翻页必须同步")
        self.assertNotEqual(self.webapp._day_off_reason(now), stale_engine_fact,
                           "显示只认源：与旧轮判码的不一致是设计上的正确行为，判据必须报出来")
        # 再翻回 1：两侧再同步回到 paused（"各读一次同一源"不是一次性快照）
        self._write_env(pause="1", user_paused="false")
        self.assertEqual(self._engine_truth_reason(now), "paused")
        self.assertEqual(self.webapp._day_off_reason(now), "paused")


if __name__ == "__main__":
    unittest.main(verbosity=2)
