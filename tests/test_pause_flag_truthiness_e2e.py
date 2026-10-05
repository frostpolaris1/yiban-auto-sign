# -*- coding: utf-8 -*-
"""急停/暂停类布尔开关的**真值口径统一**（census P0-1，止血批 B0）。

标签：E · Web：认证/权限/API
覆盖：`YIBAN_GLOBAL_PAUSE=true` 时"引擎停、run.sh 记 GLOBAL_PAUSED、面板却说未暂停"
    的反向假安心；跨语言（Python ↔ bash）真值口径一致性；面板布尔键不再用
    `load_env_int` 读；非预期取值出声；写读往返相容（空值=关）。
对应实现：`yiban.infra.env_io.parse_env_flag`（唯一真值判定）、
    `yiban.engine.schedule._env_flag`、`web/routes/settings_api.py` 面板读、
    `web/services/env_io._settings_effective_values`、`web/services/capacity._registration_paused`、
    `run.sh` 的 `_is_truthy`、`run_probe.sh` 的探针开关门。
关键断言：同一个 `.env` 值在引擎（`schedule.day_off`）、面板（`GET /api/settings`）、
    `run.sh`（状态文件写 GLOBAL_PAUSED）三处给出同一结论；`true`（及大小写变体、
    首尾空白）一律判为"开"，`0/false/off/no/空` 判为"关"。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite；run.sh/run_probe.sh 用 fakebin
    桩（无网络）；bash 缺失时整类 skip。
用法（项目根目录）：
    python -m pytest tests/test_pause_flag_truthiness_e2e.py -v
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

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_SH = os.path.join(BASE, "run.sh")
RUN_PROBE_SH = os.path.join(BASE, "run_probe.sh")

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"

#: 真值字面量的矩阵（含大小写与首尾空白变体）——Python 与 bash 两侧必须逐值同结论
TRUTHY_MATRIX = ["1", "true", "TRUE", "True", "on", "ON", "yes", "YES", " true ", " 1 "]
#: 假值/未设矩阵——一律判"关"
FALSY_MATRIX = ["0", "false", "FALSE", "off", "Off", "no", "NO", "", "   ", "0 "]
#: 非预期取值：既非真值字面量也非假值字面量 → 按缺省（关）+ 出声
BOGUS = "maybe"


def _load_webapp(module_name):
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


def _extract_bash_func(path, name):
    """从 bash 脚本里抽出函数体（`name() { ... }`），供真跑该函数的 parity 测试。"""
    src = open(path, encoding="utf-8").read()
    m = re.search(r"^%s\(\)\s*\{.*?^\}" % re.escape(name), src, re.S | re.M)
    if not m:
        raise AssertionError("未在 %s 找到函数 %s" % (path, name))
    return m.group(0)


def _posix(p):
    return str(p).replace("\\", "/")


def _write_python_wrapper(app_dir):
    venv_bin = os.path.join(app_dir, ".venv", "bin")
    os.makedirs(venv_bin, exist_ok=True)
    path = os.path.join(venv_bin, "python3")
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write('#!/bin/sh\nexec "%s" "$@"\n' % _posix(sys.executable))
    os.chmod(path, os.stat(path).st_mode | 0o755)


class EnvFlagParityTest(unittest.TestCase):
    """Python（`parse_env_flag`）与 bash（`run.sh._is_truthy`）真跑同一矩阵，逐值同结论。"""

    @unittest.skipIf(shutil.which("bash") is None, "需要 bash（Git Bash/WSL）")
    def test_run_sh_is_truthy_matches_python_parse_env_flag(self):
        from yiban.infra import env_io
        func = _extract_bash_func(RUN_SH, "_is_truthy")
        values = TRUTHY_MATRIX + FALSY_MATRIX + [BOGUS, "2"]
        script = func + '\nfor v in "$@"; do _is_truthy "$v" && echo 1 || echo 0; done\n'
        r = subprocess.run([shutil.which("bash"), "-c", script, "_", *values],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        bash_bits = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(bash_bits), len(values), r.stdout)
        for v, bit in zip(values, bash_bits, strict=True):
            with self.subTest(value=v):
                py = env_io.parse_env_flag(v, default=False)
                self.assertEqual(bit, "1" if py else "0",
                                 "bash 与 Python 对 %r 结论必须一致" % v)


class RunProbeShTruthinessTest(unittest.TestCase):
    """`run_probe.sh` 的探针开关门大小写不敏感（旧实现 `^(1|true|on|yes)$` 大写敏感）。"""

    def setUp(self):
        if shutil.which("bash") is None:
            self.skipTest("需要 bash（Git Bash/WSL）")
        self.tmp = tempfile.mkdtemp(prefix="probe-flag-")
        self.app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(self.app, "scripts"), exist_ok=True)
        _write_python_wrapper(self.app)
        with io.open(os.path.join(self.app, "scripts", "signin.py"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write("import sys\nsys.exit(0)\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, value, tag):
        env_file = os.path.join(self.app, ".env")
        with io.open(env_file, "w", encoding="utf-8", newline="\n") as f:
            f.write("YIBAN_PROBE_ENABLE=%s\n" % value)
        lock = os.path.join(self.tmp, "lock-" + tag)
        state = os.path.join(self.tmp, "state-" + tag)
        os.makedirs(state, exist_ok=True)
        env = dict(os.environ)
        env.update({
            "YIBAN_APP_DIR": self.app,
            "YIBAN_LOCK_DIR": lock,
            "YIBAN_STATE_DIR": state,
            "YIBAN_PROBE_ENABLE": value,   # 脚本 export .env，但显式给一份消除进程残留疑义
        })
        r = subprocess.run([shutil.which("bash"), RUN_PROBE_SH],
                           capture_output=True, text=True, cwd=self.app,
                           env=env, timeout=60)
        # 门通过后才会创建 LOCK_DIR（脚本在开关门之后 mkdir）——它是"是否已开启"的物证
        return r, os.path.isdir(lock)

    def test_enabled_variants_pass_the_gate(self):
        for i, v in enumerate(["1", "true", "True", "TRUE", "on", "ON", "yes"]):
            with self.subTest(YIBAN_PROBE_ENABLE=v):
                r, passed = self._run(v, "on%d" % i)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue(passed, "%r 必须被判为已开启（通过开关门）" % v)

    def test_disabled_variants_skip_before_gate(self):
        for i, v in enumerate(["0", "false", "off", "no", "nope", ""]):
            with self.subTest(YIBAN_PROBE_ENABLE=v):
                r, passed = self._run(v, "off%d" % i)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertFalse(passed, "%r 必须被判为未开启（门在先，不建锁目录）" % v)


FAKE_FLOCK = "#!/usr/bin/env bash\nexit 0\n"
FAKE_TIMEOUT = '#!/usr/bin/env bash\necho "$*" >> "$FAKE_TIMEOUT_LOG"\nexit ${FAKE_TIMEOUT_EXIT:-0}\n'
_CHAIN_SHIM = "import sys\nsys.exit(%d)\n"
#: 面板用例的 .env 基线：内置主管理员凭据 + 账号密钥 + 固定会话密钥（避免 ensure_secret_key
#: 反复改写）。开关键由 `_write_env` 叠在其上；**绝不整体覆盖**（否则登录凭据被抹掉）。
_BASE_ENV = (
    "YIBAN_ACCOUNTS_KEY=%s\n"
    "YIBAN_ADMIN_USER=admin\n"
    "YIBAN_ADMIN_PASSWORD=%s\n"
    "YIBAN_SECRET_KEY=%s\n" % (TEST_KEY, ADMIN_PASS, "b" * 64)
)


@unittest.skipIf(shutil.which("bash") is None, "需要 bash（Git Bash/WSL）")
class GlobalPauseRedLineE2ETest(unittest.TestCase):
    """红线：`.env` 里 `YIBAN_GLOBAL_PAUSE=true` ⇒ 引擎停 / run.sh 记 GLOBAL_PAUSED /
    面板 global_pause 为真——三处同一结论（这是本缺陷的正面判据）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pause-redline-")
        cls.app = os.path.join(cls.tmp, "app")
        os.makedirs(os.path.join(cls.app, "scripts"), exist_ok=True)
        os.makedirs(os.path.join(cls.app, ".venv", "bin"), exist_ok=True)
        _write_python_wrapper(cls.app)
        # 假易班：run.sh 调 signin 得 rc=2（= 暂停门命中），据此写状态文件
        with io.open(os.path.join(cls.app, "scripts", "signin.py"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write(_CHAIN_SHIM % 2)
        cls.env_file = os.path.join(cls.app, ".env")
        with io.open(cls.env_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(_BASE_ENV)
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        with io.open(cls.accounts_file, "w", encoding="utf-8") as f:
            f.write("[]")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        for k in ("YIBAN_GLOBAL_PAUSE", "YIBAN_SATURDAY_SIGN", "YIBAN_SUNDAY_SIGN"):
            os.environ.pop(k, None)
        import db
        db.init_db(cls.db_file, migrate_from=cls.accounts_file, env_file=cls.env_file)
        cls.db = db
        cls.webapp = _load_webapp("webapp_pause_redline")

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        sys.modules.pop("webapp_pause_redline", None)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_ACCOUNTS_FILE", "YIBAN_DISABLE_PURGE_LOOP"):
            os.environ.pop(k, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        from yiban.infra import env_io
        env_io._env_flag_warned.clear()   # 告警"只喊一次"的进程内闩：每用例归零，避免互相吞
        self.webapp.ENV_FILE = self.env_file
        self._set_env({"YIBAN_GLOBAL_PAUSE": "true"})

    def _set_env(self, kv):
        """按 key 就地改写 `.env`（**只在原文件上增删这几行**，保留其余内容）。

        不能整体重写：web 首次用明文 `YIBAN_ADMIN_PASSWORD` 登录会把口令迁移成
        （哈希 + 版本号）落盘，"整体重写"会用明文把这些抹掉，下一次请求再次触发迁移、
        换发会话凭据，登录后拿到的会话当场失效（GET 变 403）。这里只动开关键。
        """
        with io.open(self.env_file, encoding="utf-8") as f:
            lines = f.read().splitlines()
        keys = set(kv)
        keep = [ln for ln in lines if ln.split("=", 1)[0].strip() not in keys]
        for k, v in kv.items():
            keep.append("%s=%s" % (k, v))
        with io.open(self.env_file, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(keep) + "\n")

    def _fakebin(self):
        fb = os.path.join(self.tmp, "fakebin")
        os.makedirs(fb, exist_ok=True)
        for name, body in (("flock", FAKE_FLOCK), ("timeout", FAKE_TIMEOUT)):
            p = os.path.join(fb, name)
            with io.open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | 0o755)
        return fb

    def _run_run_sh(self):
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        calls = os.path.join(self.tmp, "timeout-calls.log")
        if os.path.exists(calls):
            os.remove(calls)
        fb = self._fakebin()
        conv = subprocess.run(
            [shutil.which("bash"), "-c", 'cygpath -u "$1" 2>/dev/null || echo "$1"', "_", fb],
            capture_output=True, text=True)
        env = dict(os.environ)
        env.update({
            "YIBAN_STATE_DIR": state,
            "YIBAN_APP_DIR": self.app,
            "FAKE_TIMEOUT_LOG": calls,
            "FAKE_TIMEOUT_EXIT": "2",
        })
        env["PATH"] = (conv.stdout.strip() or fb) + os.pathsep + env.get("PATH", "")
        r = subprocess.run([shutil.which("bash"), RUN_SH], capture_output=True,
                           env=env, cwd=self.app, timeout=120)
        return r, state

    def _login(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c, {"X-CSRF-Token": c.get("/api/me").get_json()["csrf_token"]}

    # ---- 红线：三处同一结论 ----
    def test_true_pauses_engine_panel_and_run_sh(self):
        # (a) 引擎：day_off 判为暂停
        from yiban.engine import schedule
        env = self.webapp.read_env(self.env_file)
        self.assertEqual(schedule.day_off(env=env), schedule.DAY_OFF_PAUSED,
                         "YIBAN_GLOBAL_PAUSE=true 必须让引擎判为暂停")
        # (b) 面板：GET /api/settings 的 global_pause 必须为真
        c, hdr = self._login()
        self.assertEqual(c.get("/api/settings", headers=hdr).get_json()["global_pause"], 1,
                         "面板不得把 true 读成 0（load_env_int 的老毛病）")
        # (c) run.sh：状态文件按判码契约写 GLOBAL_PAUSED
        r, state = self._run_run_sh()
        self.assertEqual(r.returncode, 2, r.stderr.decode("utf-8", "replace"))
        status = os.path.join(state, "sign-status-%s.txt" % _today())
        self.assertTrue(os.path.exists(status), "run.sh 应写当日状态文件")
        with io.open(status, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "GLOBAL_PAUSED",
                             "true 必须与 1 同结论（旧口径 `= \"1\"` 会误写 SKIPPED）")

    # ---- 写读往返 ----
    def test_panel_write_read_roundtrip(self):
        c, hdr = self._login()
        # 关 → 开 → 关：写侧落 "1"/""，读侧必须同口径
        r = c.post("/api/settings", json={"global_pause": 0, "confirm_password": ADMIN_PASS,
                                          "confirm_delay_ack": True}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(c.get("/api/settings", headers=hdr).get_json()["global_pause"], 0)
        r = c.post("/api/settings", json={"global_pause": 1, "confirm_password": ADMIN_PASS,
                                          "confirm_delay_ack": True}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(c.get("/api/settings", headers=hdr).get_json()["global_pause"], 1)
        with io.open(self.env_file, encoding="utf-8") as f:
            self.assertIn("YIBAN_GLOBAL_PAUSE=1", f.read())
        r = c.post("/api/settings", json={"global_pause": 0, "confirm_password": ADMIN_PASS,
                                          "confirm_delay_ack": True}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(c.get("/api/settings", headers=hdr).get_json()["global_pause"], 0)

    def test_truthy_and_falsy_variants_on_panel(self):
        c, hdr = self._login()
        for v in TRUTHY_MATRIX:
            with self.subTest(raw=v):
                self._set_env({"YIBAN_GLOBAL_PAUSE": v})
                self.assertEqual(
                    c.get("/api/settings", headers=hdr).get_json()["global_pause"], 1,
                    "%r 应判为暂停" % v)
        for v in FALSY_MATRIX:
            with self.subTest(raw=v):
                self._set_env({"YIBAN_GLOBAL_PAUSE": v})
                self.assertEqual(
                    c.get("/api/settings", headers=hdr).get_json()["global_pause"], 0,
                    "%r 应判为未暂停" % v)

    def test_unexpected_value_warns_and_defaults_off(self):
        c, hdr = self._login()
        self._set_env({"YIBAN_GLOBAL_PAUSE": BOGUS})
        with self.assertLogs("web", level="WARNING") as cm:
            got = c.get("/api/settings", headers=hdr).get_json()["global_pause"]
        self.assertEqual(got, 0, "非预期取值按缺省（关）处理，不得静默当成未暂停之外的其它态")
        self.assertTrue(any("YIBAN_GLOBAL_PAUSE" in m and BOGUS in m for m in cm.output),
                        "非预期取值必须出声：%r" % cm.output)

    def test_same_family_keys_share_the_domain(self):
        """同族布尔键（注册暂停/周末/验证/探针）一并收口：=true 一律判开。"""
        c, hdr = self._login()
        self._set_env({
            "YIBAN_GLOBAL_PAUSE": "true",
            "YIBAN_REGISTRATION_PAUSE": "true",
            "YIBAN_SATURDAY_SIGN": "true",
            "YIBAN_SUNDAY_SIGN": "true",
            "YIBAN_ACCOUNT_VERIFY": "true",
            "YIBAN_PROBE_ENABLE": "true",
        })
        data = c.get("/api/settings", headers=hdr).get_json()
        for field in ("global_pause", "registration_pause", "saturday_sign",
                      "sunday_sign", "account_verify", "probe_enable"):
            with self.subTest(field=field):
                self.assertEqual(data[field], 1, "%s 应判为开" % field)
        # 公开端点与设置页读同一份判定
        self.assertTrue(c.get("/api/registration_paused").get_json()["paused"])


def _today():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")


if __name__ == "__main__":
    unittest.main()
