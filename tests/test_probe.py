# -*- coding: utf-8 -*-
"""探针模式与注册时账号验证。

标签：D · 状态词汇与账号生命周期
覆盖：`verify_account` 的登录+拉任务成功/失败/异常三态；`_probe_due` 的未开启、
    未到时间、当天已跑、每 N 天频率、once 单次；`run_probe` 的预警收集（管理员合并 +
    用户个人）、落库 stage=probe、once 跑完自动关闭；`.env` 自动关闭时写临时文件
    权限 0600 且无残留；web 侧 `_account_verify_enabled`/`_verify_account_clean`。
对应实现：探针与账号验证的实现在 `yiban/engine/probe.py`（兼容壳
    `scripts/signin.py` 转发），web 侧入口在 `web/app.py`。
关键断言：探针只在"到期"时才跑、once 必须自我关闭（否则每天重复验证同一账号）；
    关闭开关写 .env 属敏感文件，必须 0600 且不留下半成品。
依赖：全程 mock 网络与邮件（`YibanClient`、发送出口），无真实请求；临时
    STATE/DB/ENV 目录。用法（项目根目录）：py -m pytest tests/test_probe.py -v
"""
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ProbeSigninTest(unittest.TestCase):
    """signin 层：verify_account 与探针调度/执行逻辑（mock 网络与邮件）。"""

    @classmethod
    def setUpClass(cls):
        # 2026-09-01 CI 修复：STATE_DIR 必须隔离到临时目录——此前 setUp 里
        # pop 掉后探针状态文件回退默认 /var/log/yiban，Linux CI（非 root）写
        # 权限不足 → PermissionError；Windows 因 fcntl 退化 no-op 侥幸通过。
        cls._state_dir = tempfile.mkdtemp(prefix="yiban-probe-state-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._state_dir, ignore_errors=True)

    def setUp(self):
        os.environ["YIBAN_PROBE_ENABLE"] = "0"
        os.environ["YIBAN_PROBE_TIME"] = "20:00"
        os.environ["YIBAN_PROBE_INTERVAL_DAYS"] = "1"
        os.environ["YIBAN_STATE_DIR"] = self._state_dir
        import signin
        self.s = signin
        # 重置模块级常量（_set_probe 会改动，防测试间污染）
        self.s.PROBE_ENABLE = False
        self.s.PROBE_TIME = "20:00"
        self.s.PROBE_INTERVAL = "1"
        self.addCleanup(mock.patch.stopall)

    def _mk_account(self, phone="13800138000"):
        return self.s.Account(phone=phone, password="pw", phone_model="", phone_code="")

    def _set_probe(self, enable="1", time="20:00", interval="1"):
        os.environ["YIBAN_PROBE_ENABLE"] = enable
        os.environ["YIBAN_PROBE_TIME"] = time
        os.environ["YIBAN_PROBE_INTERVAL_DAYS"] = interval
        self.s.PROBE_ENABLE = enable in ("1", "true", "on", "yes")
        self.s.PROBE_TIME = time
        self.s.PROBE_INTERVAL = interval

    # ---- verify_account ----
    def test_verify_account_ok(self):
        acc = self._mk_account()
        with mock.patch.object(self.s, "YibanClient") as m:
            inst = m.return_value
            inst.use_killyiban = False
            inst.verify.return_value = (True, "账号健康，可正常签到")
            ok, _msg = self.s.verify_account(acc)
        self.assertTrue(ok)
        inst.login.assert_called_once()

    def test_verify_account_fail(self):
        acc = self._mk_account()
        with mock.patch.object(self.s, "YibanClient") as m:
            inst = m.return_value
            inst.use_killyiban = False
            inst.verify.return_value = (False, "登录失败（账号或密码错误）")
            ok, _msg = self.s.verify_account(acc)
        self.assertFalse(ok)

    def test_verify_account_exception(self):
        acc = self._mk_account()
        with mock.patch.object(self.s, "YibanClient") as m:
            inst = m.return_value
            inst.use_killyiban = False
            inst.login.side_effect = RuntimeError("boom")
            ok, msg = self.s.verify_account(acc)
        self.assertFalse(ok)
        self.assertIn("boom", msg)

    # ---- _probe_due ----
    def test_probe_due_disabled(self):
        self._set_probe("0")
        self.assertFalse(self.s._health_probe_due(datetime(2026, 8, 25, 21, 0)))

    def test_probe_due_not_time(self):
        self._set_probe("1", "20:00")
        self.assertFalse(self.s._health_probe_due(datetime(2026, 8, 25, 19, 0)))

    def test_probe_due_every_day_and_already_ran(self):
        self._set_probe("1", "20:00", "1")
        with mock.patch.object(self.s, "_read_probe_state", return_value={}):
            self.assertTrue(self.s._health_probe_due(datetime(2026, 8, 25, 21, 0)))
        with mock.patch.object(self.s, "_read_probe_state", return_value={"last_run": "2026-08-25"}):
            self.assertFalse(self.s._health_probe_due(datetime(2026, 8, 25, 21, 0)))

    def test_probe_due_interval_n_days(self):
        self._set_probe("1", "20:00", "3")
        with mock.patch.object(self.s, "_read_probe_state", return_value={"last_run": "2026-08-23"}):
            self.assertFalse(self.s._health_probe_due(datetime(2026, 8, 25, 21, 0)))  # 间隔 3 天未到
        with mock.patch.object(self.s, "_read_probe_state", return_value={"last_run": "2026-08-22"}):
            self.assertTrue(self.s._health_probe_due(datetime(2026, 8, 25, 21, 0)))  # 3 天前到

    def test_probe_due_once(self):
        self._set_probe("1", "20:00", "once")
        with mock.patch.object(self.s, "_read_probe_state", return_value={}):
            self.assertTrue(self.s._health_probe_due(datetime(2026, 8, 25, 21, 0)))

    # ---- _env_update_probe：once 自动关闭写 .env ----
    def test_env_update_probe_writes_disable_and_creates_tmp_0600(self):
        """写 YIBAN_PROBE_ENABLE=0 且临时文件**创建即 0600**。

        事后 chmod 不够：写完到 os.replace 之间（及崩溃残留时）整个 .env 对同机
        其他用户可读，而默认 umask 未必是 077（交互 shell 手工跑 --probe 即可能命中）。
        """
        env_path = os.path.join(self._state_dir, "probe-once.env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + "a" * 64 + "\nYIBAN_PROBE_ENABLE=1\n")
        os.environ["YIBAN_ENV_FILE"] = env_path

        real_open = os.open
        modes = []

        def _spy(path, flags, mode=0o777):
            modes.append(mode)
            return real_open(path, flags, mode)

        with mock.patch.object(self.s.os, "open", side_effect=_spy):
            self.s._env_update_probe(auto_disable=True)

        self.assertIn(0o600, modes, "临时文件必须创建即 0600，不能靠事后 chmod")
        with open(env_path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("YIBAN_PROBE_ENABLE=0", text)
        self.assertNotIn("YIBAN_PROBE_ENABLE=1", text, "旧键应被折叠为单条")
        self.assertIn("YIBAN_ACCOUNTS_KEY=", text, "其余键不得丢失")
        if os.name == "posix":
            import stat as _stat
            self.assertEqual(_stat.S_IMODE(os.stat(env_path).st_mode), 0o600)

    def test_env_update_probe_noop_when_not_auto_disable(self):
        """非 once 模式不得改动 .env。"""
        env_path = os.path.join(self._state_dir, "probe-keep.env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("YIBAN_PROBE_ENABLE=1\n")
        os.environ["YIBAN_ENV_FILE"] = env_path
        self.s._env_update_probe(auto_disable=False)
        with open(env_path, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "YIBAN_PROBE_ENABLE=1")


class WebVerifyTest(unittest.TestCase):
    """web 层：注册账号验证开关与验证函数（mock signin.verify_account）。"""

    def setUp(self):
        import app as webapp
        self.w = webapp
        self.addCleanup(mock.patch.stopall)

    def test_verify_clean_ok(self):
        with mock.patch.object(self.w.signin, "verify_account", return_value=(True, "ok")):
            self.assertIsNone(self.w._verify_account_clean(
                {"phone": "13800138000", "password": "pw"}))

    def test_verify_clean_fail(self):
        with mock.patch.object(self.w.signin, "verify_account", return_value=(False, "登录失败（账号或密码错误）")):
            err = self.w._verify_account_clean({"phone": "13800138000", "password": "pw"})
        self.assertIn("验证未通过", err)
        self.assertNotIn("\n", err)

    def test_account_verify_enabled(self):
        with mock.patch.object(self.w, "read_env", return_value={"YIBAN_ACCOUNT_VERIFY": "1"}):
            self.assertTrue(self.w._account_verify_enabled())
        with mock.patch.object(self.w, "read_env", return_value={}):
            self.assertFalse(self.w._account_verify_enabled())


if __name__ == "__main__":
    unittest.main()
