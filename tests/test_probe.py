# -*- coding: utf-8 -*-
"""探针模式 + 注册时账号验证测试（v0.23.x）。

覆盖：
- signin.verify_account：登录+拉任务成功 / 失败 / 异常（mock YibanClient）
- signin._probe_due：未开启 / 未到时间 / 当天已跑 / 每 N 天频率 / once 单次
- signin.run_probe：预警收集（管理员合并 + 用户个人）、落库 stage=probe、once 自动关闭
- web._account_verify_enabled / _verify_account_clean（mock signin.verify_account）

全程本地（mock 网络与邮件），无真实请求。
用法（项目根目录）：py -m pytest tests/test_probe.py -v
"""
import contextlib
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))


class ProbeSigninTest(unittest.TestCase):
    """signin 层：verify_account 与探针调度/执行逻辑（mock 网络与邮件）。"""

    def setUp(self):
        os.environ["YIBAN_PROBE_ENABLE"] = "0"
        os.environ["YIBAN_PROBE_TIME"] = "20:00"
        os.environ["YIBAN_PROBE_INTERVAL_DAYS"] = "1"
        os.environ.pop("YIBAN_STATE_DIR", None)
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
            ok, msg = self.s.verify_account(acc)
        self.assertTrue(ok)
        inst.login.assert_called_once()

    def test_verify_account_fail(self):
        acc = self._mk_account()
        with mock.patch.object(self.s, "YibanClient") as m:
            inst = m.return_value
            inst.use_killyiban = False
            inst.verify.return_value = (False, "登录失败（账号或密码错误）")
            ok, msg = self.s.verify_account(acc)
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

    # ---- run_probe ----
    def test_run_probe_collects_and_flushes(self):
        self._set_probe("1", "20:00", "1")
        ok_acc = self._mk_account("13800138001")
        bad_acc = self._mk_account("13800138002")
        bad_acc.owner = "owner@test.com"
        with mock.patch.object(self.s, "_health_probe_due", return_value=True), \
             mock.patch.object(self.s, "verify_account", side_effect=[
                 (True, "账号健康，可正常签到"),
                 (False, "登录失败（账号或密码错误）"),
             ]), \
             mock.patch.object(self.s, "_collect_admin_mail") as col, \
             mock.patch.object(self.s, "_flush_admin_mail_summary") as fl, \
             mock.patch.object(self.s, "send_user_fail_mail") as suf, \
             mock.patch.object(self.s, "_write_probe_state") as wsp, \
             mock.patch.object(self.s, "_env_update_probe") as eup, \
             mock.patch.object(self.s.db, "add_sign_event") as add:
            self.s.run_probe([ok_acc, bad_acc])
        col.assert_called_once()
        # v0.24.4：探针路径的用户邮件带 scenario="probe"（措辞与签到失败解耦）
        suf.assert_called_once_with("owner@test.com", "13800138002",
                                    mock.ANY, scenario="probe")
        fl.assert_called_once()
        wsp.assert_called_once()
        eup.assert_not_called()  # 非 once 不自动关闭
        self.assertEqual(add.call_count, 2)  # 每账号一条（stage=probe）

    def test_run_probe_once_auto_disable(self):
        self._set_probe("1", "20:00", "once")
        acc = self._mk_account()
        with mock.patch.object(self.s, "_health_probe_due", return_value=True), \
             mock.patch.object(self.s, "verify_account", return_value=(True, "健康")), \
             mock.patch.object(self.s, "_write_probe_state"), \
             mock.patch.object(self.s, "_env_update_probe") as eup:
            self.s.run_probe([acc])
        eup.assert_called_once_with(auto_disable=True)

    def test_run_probe_disabled_is_silent(self):
        # 探针关闭：完全静默——不调到期判断、不探测、不落库、不写状态、不预警
        acc = self._mk_account()
        with mock.patch.object(self.s, "verify_account") as va, \
             mock.patch.object(self.s, "_health_probe_due") as h, \
             mock.patch.object(self.s, "_write_probe_state") as wsp, \
             mock.patch.object(self.s, "_collect_admin_mail") as col, \
             mock.patch.object(self.s.db, "add_sign_event") as add:
            self.s.run_probe([acc])
        va.assert_not_called()
        h.assert_not_called()
        wsp.assert_not_called()
        col.assert_not_called()
        add.assert_not_called()

    def test_run_probe_skipped_when_enabled_but_not_due(self):
        # 已开启但未到触发时间/频率：跳过且不探测、不写状态
        self._set_probe("1", "20:00", "1")
        acc = self._mk_account()
        with mock.patch.object(self.s, "_health_probe_due", return_value=False), \
             mock.patch.object(self.s, "verify_account") as va, \
             mock.patch.object(self.s, "_write_probe_state") as wsp:
            self.s.run_probe([acc])
        va.assert_not_called()
        wsp.assert_not_called()


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
