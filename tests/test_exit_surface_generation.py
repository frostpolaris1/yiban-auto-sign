# -*- coding: utf-8 -*-
"""MF-49 出口面（二）：文本直出出口的**生成点**遮罩（stderr 摘要 / --json.errors /
子进程 stdout 重定向 / argv 面 / run.sh 的 `2>&1`）。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`cli_support.report_fatal_error`（一条摘要 fan-out 到三个不经 formatter 的出口：
    stderr、`--json` 的 error 字段、被 run.sh `2>&1` 与 web 手动签到 `stdout=log_fh`
    落进当天日志的那份）、`signin_api._launch_signin_proc` 的 argv/env 面清点钉、
    `run.sh` 自身文本不 echo 凭据键的静态实证。
对应实现：`yiban/engine/cli_support.py::report_fatal_error`、
    `yiban/cli.py::_cmd_sign/_cmd_probe`（消费 `last_fatal_error`）、
    `web/routes/signin_api.py::_launch_signin_proc`、`run.sh`。
关键断言：生成点遮——同一份原语（`sanitize_text` + `mask_phones_in_text`），不新造
    第二套口径；摘要"一行"契约不破；argv 只允许承载标识符（裸号=已登记残余，
    见 task-2-9a-report），任何凭据（口令/密钥/代理 userinfo/邮箱）不得进 argv。
依赖：进程内用例换 `sys.stderr` 缓冲；`sign --json` 与 stdout 重定向用例起
    `sys.executable` 子进程（纯 Python，无网络）；argv 用例打桩
    `subprocess.Popen`（测试内还原）；run.sh 静态读文本。

stdout=log_fh 的等价拓扑：web 手动签到用 `Popen([...], stdout=log_fh,
stderr=STDOUT)` 把子进程两路输出并入当天日志——本文件用同一重定向直接起
`scripts/signin.py --only`，断"落点文件只剩遮罩形态"，与 `_launch_signin_proc`
的 argv 钉合起来覆盖该出口的两端（传输口 + 生成口）。
"""
import contextlib
import importlib.util
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PHONE_RAW = "13800001234"
PHONE_MASKED = "138****1234"
OWNER_EMAIL = "student99@example.com"
FAKE_PASSWORD = "hunter2-under-key-a"


def _load_child_env():
    """按文件路径装载 `scripts/child_env.py`（非包内模块，沿用既有装载方式）。"""
    spec = importlib.util.spec_from_file_location(
        "yiban_child_env_exit", os.path.join(BASE, "scripts", "child_env.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ReportFatalErrorGenerationTest(unittest.TestCase):
    """生成点收口：摘要离开 `report_fatal_error` 之前必须已遮。"""

    def setUp(self):
        from yiban.engine import cli_support
        self.cli_support = cli_support
        cli_support.clear_fatal_error()

    def test_summary_masked_before_stderr_and_registry(self):
        """stderr 行与 `last_fatal_error()`（即 `--json.errors` 的来源）同一份遮罩；
        口令字面量走 `sanitize_text` 同口径；"一行摘要"契约不破。"""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.cli_support.report_fatal_error(
                f"配置加载失败: 账号 {PHONE_RAW} 密码解密失败: password='{FAKE_PASSWORD}'")
        err = buf.getvalue()
        stored = self.cli_support.last_fatal_error()
        for surface, text in (("stderr", err), ("last_fatal_error", stored)):
            with self.subTest(surface):
                self.assertNotIn(PHONE_RAW, text)
                self.assertNotIn(FAKE_PASSWORD, text)
                self.assertIn(PHONE_MASKED, text)
        self.assertEqual(err.count("\n"), 1, "摘要必须保持一行（换行注入防护）")

    def test_plain_summary_passes_through_idempotently(self):
        """不含敏感形态的摘要原样通过（幂等遮罩不加戏）。"""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.cli_support.report_fatal_error("未配置任何账号")
        self.assertIn("未配置任何账号", buf.getvalue())
        self.assertEqual(self.cli_support.last_fatal_error(), "未配置任何账号")


class _JsonEnvFixture(unittest.TestCase):
    """共享底座：密文按 A 钥加密、装载用 B 钥 ⇒ 异常消息必含裸号。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-gen-exit-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.key_a = secrets.token_hex(32)
        self.key_b = secrets.token_hex(32)

    def env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
        env.update({
            "YIBAN_STATE_DIR": os.path.join(self.root, "state"),
            "YIBAN_LOG_FILE": os.path.join(self.root, "logs", "sign.log"),
            "YIBAN_DB_FILE": os.path.join(self.root, "yiban.db"),
            "YIBAN_ENV_FILE": os.path.join(self.root, ".env"),
            "PYTHONPATH": BASE,
            "PYTHONIOENCODING": "utf-8",
        })
        return env

    def broken_accounts_env(self):
        code = ("import json,sys;from yiban.infra import account_crypto;"
                "print(json.dumps([{'phone': sys.argv[1],"
                "'password': account_crypto.encrypt_password("
                "sys.argv[3], bytes.fromhex(sys.argv[2]), sys.argv[1])}]))")
        r = subprocess.run([sys.executable, "-c", code, PHONE_RAW, self.key_a,
                            FAKE_PASSWORD], cwd=BASE, env=self.env(),
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        return {"YIBAN_ACCOUNTS_JSON": r.stdout.strip(),
                "YIBAN_ACCOUNTS_KEY": self.key_b}


class SignJsonErrorExitTest(_JsonEnvFixture):
    """`--json` 的 error 字段：与 stderr 同一条摘要，遮罩在生成点已完成。"""

    def test_sign_json_error_field_masked(self):
        env = self.env()
        env.update(self.broken_accounts_env())
        r = subprocess.run([sys.executable, "-m", "yiban.cli", "sign", "--json"],
                           cwd=BASE, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(r.returncode, 1, r.stderr[-400:])
        payload = json.loads(r.stdout)
        self.assertEqual(payload["command"], "sign")
        self.assertIn(PHONE_MASKED, payload.get("error", ""))
        combined = r.stdout + r.stderr
        self.assertNotIn(PHONE_RAW, combined, combined[:400])
        self.assertNotIn(FAKE_PASSWORD, combined)


class ChildStdoutRedirectTest(_JsonEnvFixture):
    """`stdout=log_fh` 出口：web 手动签到的重定向拓扑下，落点文件只剩遮罩形态。"""

    def test_signin_child_redirect_file_has_no_raw_phone(self):
        env = self.env()
        env.update(self.broken_accounts_env())
        logs = os.path.join(self.root, "logs")
        os.makedirs(logs, exist_ok=True)
        redirect = os.path.join(logs, "manual-redirect.log")
        with open(redirect, "a", encoding="utf-8", buffering=1) as log_fh:
            proc = subprocess.Popen(
                [sys.executable, "scripts/signin.py", "--only", PHONE_RAW],
                cwd=BASE, env=env, stdout=log_fh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL)
            rc = proc.wait(timeout=120)
        self.assertEqual(rc, 1)
        with open(redirect, encoding="utf-8") as f:
            body = f.read()
        self.assertIn(PHONE_MASKED, body, "重定向落点应有生成点遮罩后的摘要行")
        self.assertNotIn(PHONE_RAW, body)
        self.assertNotIn(OWNER_EMAIL, body)
        self.assertNotIn(FAKE_PASSWORD, body)
        # 子进程自己的按天日志（经 MaskingFormatter 装配）同场核对
        day = ""
        for name in os.listdir(logs):
            if name.startswith("sign-") and name.endswith(".log"):
                with open(os.path.join(logs, name), encoding="utf-8") as f:
                    day += f.read()
        self.assertNotIn(PHONE_RAW, day)
        self.assertIn(PHONE_MASKED, day)


class SpawnArgvEnvironSurfaceTest(unittest.TestCase):
    """argv/environ 清点钉：标识符进 argv 是已登记残余，凭据形态绝不进 argv。"""

    def _launch_capture(self):
        from web.routes import signin_api
        root = tempfile.mkdtemp(prefix="yiban-spawn-argv-")
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        env_file = os.path.join(root, ".env")
        key = secrets.token_hex(32)
        proxy = "http://proxyuser:proxypass@10.0.0.9:8080"
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={key}\n"
                    f"YIBAN_PASSWORD={FAKE_PASSWORD}\n"
                    f"YIBAN_PROXY={proxy}\n")
        m = types.SimpleNamespace(
            __file__=os.path.join(BASE, "web", "app.py"),
            ENV_FILE=env_file,
            DB_FILE=os.path.join(root, "yiban.db"),
            child_env=_load_child_env(),
            log_path_for=lambda: os.path.join(root, "sign.log"),
        )
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return types.SimpleNamespace(pid=os.getpid())

        saved = {k: v for k, v in os.environ.items() if k.startswith("YIBAN_")}
        for k in saved:
            del os.environ[k]
        try:
            with mock.patch.object(subprocess, "Popen", fake_popen):
                signin_api._launch_signin_proc(m, PHONE_RAW)
        finally:
            os.environ.update(saved)
        return captured, key, proxy

    def test_argv_carries_identifier_not_credentials(self):
        """argv 面（/proc/<pid>/cmdline 全局可读）：只允许 `--only <号>`；
        口令/密钥/代理 userinfo/邮箱一旦出现在任一参数即红。"""
        captured, key, proxy = self._launch_capture()
        argv = [str(a) for a in captured["cmd"]]
        joined = " ".join(argv)
        self.assertIn("--only", argv)
        self.assertIn(PHONE_RAW, joined, "已登记残余：号在 argv（收口需改调用契约，"
                                        "方案选项见 task-2-9a-report）")
        for secret in (FAKE_PASSWORD, key, proxy.split("://")[1].split("@")[0],
                       OWNER_EMAIL):
            self.assertNotIn(secret, joined, f"凭据进了 argv（ps/proc 全局可见）: {secret}")

    def test_child_env_still_holds_credentials_same_uid(self):
        """environ 面钉桩：.env 的 YIBAN_* 仍注入子进程环境（机制事实，
        /proc/<pid>/environ 仅同 uid 可读）——残余登记口径以本钉为准，
        任何人把它"顺手改成剥离凭据"会让现有传递契约静默断裂。"""
        captured, key, proxy = self._launch_capture()
        env = captured["kwargs"]["env"]
        self.assertEqual(env.get("YIBAN_ACCOUNTS_KEY"), key)
        self.assertIn(proxy, env.get("YIBAN_PROXY", ""))
        # stderr 并入 stdout 的同一通道（重定向拓扑的另一半）
        self.assertIs(captured["kwargs"]["stderr"], subprocess.STDOUT)


class RunShStaticTest(unittest.TestCase):
    """run.sh 的 `2>&1`：脚本自身不 echo 凭据；重定向只承载 `"$PY"` 子进程输出
    （其文本源已在生成点收口——见本文件另两类）。"""

    def test_runsh_never_interpolates_credential_keys(self):
        with open(os.path.join(BASE, "run.sh"), encoding="utf-8") as f:
            src = f.read()
        for key in ("YIBAN_PROXY", "YIBAN_ACCOUNTS", "YIBAN_PASSWORD",
                    "YIBAN_ACCOUNTS_KEY", "YIBAN_NOTIFY_URL", "YIBAN_MAIL",
                    "YIBAN_PHONE"):
            self.assertNotIn(key, src, f"run.sh 开始引用凭据键 {key}——_log 行将把"
                                       "值明文带进 $LOG_FILE")

    def test_runsh_redirections_only_carry_python_children(self):
        """每条 `2>&1` 都必须落在 `"$PY"` 调用行上：新增 shell 侧直出前，先被这条钉拦住。"""
        with open(os.path.join(BASE, "run.sh"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        sites = [(i, ln) for i, ln in enumerate(lines, 1) if "2>&1" in ln]
        self.assertTrue(sites, "run.sh 与 2>&1 的关系消失了——更新清点前请先核实")
        for num, line in sites:
            self.assertIn('"$PY"', line, f"run.sh:{num} 出现不包 `\"$PY\"` 的 2>&1 直出口")
