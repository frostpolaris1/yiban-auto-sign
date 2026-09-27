# -*- coding: utf-8 -*-
"""MF-49 出口面（一）：`yiban.cli` 维护类子命令的 stdout/stderr 遮罩收口。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`_say`/`_emit_json` 两个出口收口的遮罩（含"字符串叶子遮、数字叶子不遮"的分型）、
    `config` 失败/成功两态、`capacity`/`state`/`db`/`version` 四个维护子命令的
    `--json`+stderr 出口、维护子命令入口的日志装配（`logging.lastResort` 裸写旁路
    必须被 `MaskingFormatter` 装配堵掉）。
对应实现：`yiban/cli.py`（`_say`、`_emit_json`、`_masked_tree`、`main` 装配点）与
    `yiban/engine/cli_support.py::_setup_cli_logging`（复用，不新造）。
关键断言：登记原文的验收不变量——构造含裸号/明文邮箱的记录 ⇒ 每个出口都是遮罩形态：
    断"遮罩形态在场"且"裸形态缺席"两侧；只断缺席会放过"字段整个消失"的假绿。
依赖：起 `sys.executable -m yiban.cli` / `python -c` 子进程（纯 Python）；全部用
    临时 STATE/LOG/DB/ENV 与随机假钥，不碰真实 .env；无 bash/node/网络。

出口形态沿用 `tests/test_cli_contract.py` 的进程级口径（stdout 单行 JSON 可解析、
人话进 stderr）。假值按规范取 138****0000 风格：`13800001234 → 138****1234`。
"""
import contextlib
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PHONE_RAW = "13800001234"
PHONE_MASKED = "138****1234"
OWNER_EMAIL = "student99@example.com"
OWNER_EMAIL_MASKED = "stu******@example.com"


def _cli_env(tmp_path, env_extra=None):
    """隔离环境：临时路径四件套 + 剥掉继承的全部 YIBAN_*（防串进真实部署）。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
    env.update({
        "YIBAN_STATE_DIR": os.path.join(tmp_path, "state"),
        "YIBAN_LOG_FILE": os.path.join(tmp_path, "logs", "sign.log"),
        "YIBAN_DB_FILE": os.path.join(tmp_path, "yiban.db"),
        "YIBAN_ENV_FILE": os.path.join(tmp_path, ".env"),
        "PYTHONPATH": BASE,
        "PYTHONIOENCODING": "utf-8",
    })
    env.update(env_extra or {})
    return env


def _run_cli(argv, env, timeout=120):
    return subprocess.run([sys.executable, "-m", "yiban.cli", *argv], cwd=BASE,
                          env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL, timeout=timeout)


class _CliExitHarness(unittest.TestCase):
    """临时目录 + 一条含裸号/明文邮箱的"记录"的公共底座。

    记录有两种载体，各测试按需选用：
    - `accounts_json_env(key)`：YIBAN_ACCOUNTS_JSON 密文账号（口令加密钥 = key），
      配 `config`/`sign`；钥不匹配即解密失败，异常消息带**裸号**；
    - `_add_db_account`：真实临时库里的账号行（phone 列即裸号明文存储）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-cli-exit-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.key_a = secrets.token_hex(32)
        self.key_b = secrets.token_hex(32)

    def env(self, extra=None):
        return _cli_env(self.root, extra)

    def encrypt_password(self):
        """用 key_a 加密一个口令，返回密文对象（JSON 可嵌 dict）。"""
        code = ("import json,sys;from yiban.infra import account_crypto;"
                "print(json.dumps(account_crypto.encrypt_password("
                "'under-key-a', bytes.fromhex(sys.argv[1]), sys.argv[2])))")
        r = subprocess.run([sys.executable, "-c", code, self.key_a, PHONE_RAW],
                           cwd=BASE, env=self.env({"YIBAN_ACCOUNTS_KEY": self.key_a}),
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        return json.loads(r.stdout.strip())

    def accounts_json_env(self, key_hex):
        """构造"密文按 A 钥加密、装载时用 B 钥解"的环境 ⇒ 异常消息必含裸号。"""
        ct = self.encrypt_password()
        items = [{"phone": PHONE_RAW, "password": ct, "owner": OWNER_EMAIL,
                  "name": "测试账号"}]
        return {"YIBAN_ACCOUNTS_JSON": json.dumps(items, ensure_ascii=False),
                "YIBAN_ACCOUNTS_KEY": key_hex}

    def _add_db_account(self):
        """临时库写一个 active 账号（phone 列即裸号明文；口令按 key_a 加密）。"""
        code = ("import sys;from yiban.store import db;"
                "db.init_db(db_file=sys.argv[1], env_file=sys.argv[2], cleanup=False);"
                "db.add_account({'name': 'A', 'phone': sys.argv[3], 'password': 'p1',"
                " 'status': 'active', 'owner': 'admin'});db.get_conn().close()")
        r = subprocess.run([sys.executable, "-c", code,
                            os.path.join(self.root, "yiban.db"),
                            os.path.join(self.root, ".env"), PHONE_RAW],
                           cwd=BASE, env=self.env({"YIBAN_ACCOUNTS_KEY": self.key_a}),
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(r.returncode, 0, f"临时账号写入失败: {r.stdout}{r.stderr}")


class CliChokeUnitTest(_CliExitHarness):
    """出口收口单元：`_say`/`_emit_json` 两个直写点（进程内，流捕获）。"""

    def test_say_masks_raw_phone_on_stderr(self):
        """`_say` 的 stderr 行不得带裸号：遮罩形态在场、裸形态缺席。"""
        from yiban import cli
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            cli._say(f"配置加载失败: 账号 {PHONE_RAW} 密码解密失败")
        out = buf.getvalue()
        self.assertNotIn(PHONE_RAW, out)
        self.assertIn(PHONE_MASKED, out)

    def test_emit_json_masks_string_leaves_and_keeps_numbers(self):
        """`_emit_json`：字符串叶子遮号、**整数字段原样**（否则整行 JSON 被遮成非法）。

        钉的是收口的分型语义：整数（如 `size_bytes`）不是"手机号字符串"，
        对序列化后的整行打码会把 11 位整数改成 `138****0000` 这种非法 JSON——
        必须按类型放行，数字叶子保持原值、JSON 仍可解析。
        """
        from yiban import cli
        buf = io.StringIO()
        payload = {
            "command": "config", "ok": False,
            "errors": [f"配置加载失败: 账号 {PHONE_RAW} 密码解密失败"],
            "detail": [f"文件 {PHONE_RAW}.json"],
            "size_bytes": 140737488355328,   # 15 位整数，恰不被当号，验证数字原样
            "accounts": 3,
            "paths": {"db_file": f"/var/log/yiban/{PHONE_RAW}.db"},
        }
        with contextlib.redirect_stdout(buf):
            cli._emit_json(payload)
        line = buf.getvalue()
        self.assertIn(PHONE_MASKED, line)
        parsed = json.loads(line)  # 数字叶子未被改写 → 整行仍是可解析 JSON
        self.assertEqual(parsed["size_bytes"], 140737488355328)
        self.assertEqual(parsed["accounts"], 3)
        # 字符串叶子只剩遮罩形态：把树里所有字符串拼起来不得再有裸号
        strs = json.dumps(parsed["errors"]) + json.dumps(parsed["detail"]) + \
            json.dumps(parsed["paths"])
        self.assertNotIn(PHONE_RAW, strs)
        self.assertIn(PHONE_MASKED, strs)


class CliSubprocessExitTest(_CliExitHarness):
    """七个子命令里本段负责的五个：config/capacity/state/db/version（进程级真跑）。"""

    def test_config_fail_masks_stderr_and_json_errors(self):
        """密文与钥不匹配 ⇒ 异常消息含裸号；`config` 的 stderr 与 `--json errors` 双出口都遮。"""
        r = _run_cli(["config", "--json"], self.env(self.accounts_json_env(self.key_b)))
        self.assertEqual(r.returncode, 1, r.stderr[-400:])
        payload = json.loads(r.stdout)
        self.assertFalse(payload["ok"])
        combined = r.stdout + r.stderr
        self.assertNotIn(PHONE_RAW, combined, f"裸号出现在 CLI 出口: {combined[:400]}")
        self.assertNotIn(OWNER_EMAIL, combined)
        self.assertIn(PHONE_MASKED, r.stderr)
        self.assertIn(PHONE_MASKED, json.dumps(payload, ensure_ascii=False))

    def test_config_success_masks_every_field(self):
        """装载成功态：`--json` 的 phones_masked 与 stderr 明细行都只有遮罩形态。"""
        items = [{"phone": PHONE_RAW, "password": "plain-pw", "owner": OWNER_EMAIL}]
        r = _run_cli(["config", "--json"],
                     self.env({"YIBAN_ACCOUNTS_JSON": json.dumps(items)}))
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        payload = json.loads(r.stdout)
        self.assertEqual(payload["phones_masked"], [PHONE_MASKED])
        combined = r.stdout + r.stderr
        self.assertNotIn(PHONE_RAW, combined)
        self.assertNotIn(OWNER_EMAIL, combined)
        self.assertNotIn("plain-pw", combined)

    def test_capacity_exit_has_no_raw_phone(self):
        """capacity 读库（phone 列即裸号）但出口只有数字与路径：裸号必须全程缺席。"""
        self._add_db_account()
        r = _run_cli(["capacity", "--json"], self.env({"YIBAN_ACCOUNTS_KEY": self.key_a}))
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        json.loads(r.stdout)
        combined = r.stdout + r.stderr
        self.assertNotIn(PHONE_RAW, combined)

    def test_state_exit_has_no_raw_phone(self):
        """state 明细是固定形态的按日文件名：裸号必须全程缺席（含 --yes 前的指纹行）。"""
        state_dir = os.path.join(self.root, "state")
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "sched-run-2020-01-01.json"), "w") as f:
            f.write("{}")
        r = _run_cli(["state", "--json"], self.env())
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        json.loads(r.stdout)
        combined = r.stdout + r.stderr
        self.assertNotIn(PHONE_RAW, combined)

    def test_db_exit_has_no_raw_phone(self):
        """db --integrity 读的是含裸号行的库：出口（表清单/计数/detail）不得带出裸号。"""
        self._add_db_account()
        r = _run_cli(["db", "--integrity", "--json"],
                     self.env({"YIBAN_ACCOUNTS_KEY": self.key_a}))
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        payload = json.loads(r.stdout)
        self.assertTrue(payload["integrity_ok"])
        combined = r.stdout + r.stderr
        self.assertNotIn(PHONE_RAW, combined)

    def test_version_exit_has_no_raw_phone(self):
        """version 出口（版本号/Python/schema 版本）与账号数据无交集：裸号缺席的实证。"""
        self._add_db_account()
        r = _run_cli(["version", "--json"], self.env({"YIBAN_ACCOUNTS_KEY": self.key_a}))
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        json.loads(r.stdout)
        combined = r.stdout + r.stderr
        self.assertNotIn(PHONE_RAW, combined)

    def test_check_config_stdout_print_masked(self):
        """`sign --check-config` 的 stdout 直印（`config_check.print_config_summary`）：
        号码遮罩在调用点已有，这里钉它不被绕过、口令位恒为星号、邮箱不出现。"""
        items = [{"phone": PHONE_RAW, "password": "plain-pw", "owner": OWNER_EMAIL,
                  "phone_model": "Pixel 7", "phone_code": "code-1"}]
        r = _run_cli(["sign", "--check-config"],
                     self.env({"YIBAN_ACCOUNTS_JSON": json.dumps(items)}))
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn(PHONE_MASKED, r.stdout)
        self.assertNotIn(PHONE_RAW, r.stdout + r.stderr)
        self.assertNotIn("plain-pw", r.stdout + r.stderr)
        self.assertNotIn(OWNER_EMAIL, r.stdout + r.stderr)

    def test_maintenance_command_mounts_masking_assembly(self):
        """维护子命令必须接上 `_setup_cli_logging`：lastResort 裸写 stderr 的旁路堵死。

        机理：旧格式装载链路对畸形条目会 `logger.error` 一条**含裸号**的消息；
        装配缺席时 root 无 handler，`logging.lastResort` 把 WARNING+ 裸写 stderr
        （formatter 整条防线被旁路）。装配后 stderr 不出现裸号，按天日志文件里
        只剩遮罩形态——"装配在场"与"落点遮罩"两件事一起钉。
        """
        env = self.env({"YIBAN_ACCOUNTS": f"{PHONE_RAW} 缺冒号的畸形条目"})
        r = _run_cli(["config", "--json"], env)
        self.assertEqual(r.returncode, 1, r.stderr[-400:])
        self.assertNotIn(PHONE_RAW, r.stderr,
                         f"lastResort 旁路未被堵掉，stderr 出现裸号: {r.stderr[:400]}")
        self.assertNotIn(PHONE_RAW, r.stdout)
        day_logs = [p for p in os.listdir(os.path.join(self.root, "logs"))
                    if p.startswith("sign-") and p.endswith(".log")]
        self.assertTrue(day_logs, "入口未装配合按天日志（装配点缺席）")
        body = ""
        for name in day_logs:
            with open(os.path.join(self.root, "logs", name), encoding="utf-8") as f:
                body += f.read()
        self.assertIn(PHONE_MASKED, body, "按天日志里应留遮罩形态的告警行")
        self.assertNotIn(PHONE_RAW, body)


class CliProbePassthroughWiringTest(_CliExitHarness):
    """sign/probe 是透传子命令：其 `--json.errors` 出口见
    `tests/test_exit_surface_generation.py`（同一条 `report_fatal_error` 生成点）。"""

    def test_passthrough_commands_delegate_runner(self):
        """结构钉：sign/probe 经 runner.main（装配与生成点遮罩随透传自动生效）。"""
        import inspect

        from yiban import cli
        for fn in (cli._cmd_sign, cli._cmd_probe):
            src = inspect.getsource(fn)
            self.assertIn("runner.main", src)
            self.assertIn("last_fatal_error", src)
        self.assertNotIn("config", cli._PASSTHROUGH)
        self.assertEqual(set(cli._PASSTHROUGH), {"sign", "probe"})
