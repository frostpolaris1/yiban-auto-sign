# -*- coding: utf-8 -*-
"""D-6 批量：当前生产上可达的四个中小缺陷的回归防线。

| 缺陷 | 现象 |
|------|------|
| DAT-5 | 用户侧按 idx 寻址的写端点（编辑/删除/暂停）**缺防错位校验**——本人视图在渲染后可能因管理员删除/清除而漂移，放行会静默改到本人**另一行**（管理员侧 7 处一直都有该校验） |
| SCH-8 | 用户失败提醒的每日额度**在发送前占位**且失败不回滚——一次 SMTP 抖动就吞掉该账号当天唯一的提醒机会（docstring 承诺的却是"发送成功才消耗"） |
| SCH-9 | 账号加载**不按手机号去重**——JSON / 环境变量配置里同号重复时，对同一账号完整登录两次且共享重试计数（库内模式有唯一索引兜底） |
| SCH-10 | 窗口关闭只在堆弹出时判一次，其后还有 sleep 与间隔对齐——请求可能被推到窗口结束之后仍照发 |
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402
import signin  # noqa: E402

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"
EMAIL = "u1@test.local"
PHONE = "13800138000"


class _WebBase(unittest.TestCase):
    """临时 .env/DB + 全新 app（与既有 web 类测试同一骨架）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-d6-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
        })
        spec = importlib.util.spec_from_file_location(
            "webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    def setUp(self):
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                    f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n")
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        db.create_user(EMAIL, self.webapp.generate_password_hash(USER_PASS))
        db.add_account({"name": "我的号", "phone": PHONE, "password": "pw",
                        "owner": EMAIL, "status": "active",
                        "phone_model": "", "phone_code": ""})
        self.app = self.webapp.create_app()
        self.c = self.app.test_client()

    def _login(self):
        r = self.c.post("/api/login", json={"username": EMAIL, "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return self.c.get("/api/me").get_json()["csrf_token"]


class UserIndexDriftGuardTest(_WebBase):
    """DAT-5：用户侧写端点必须像 /api/accounts/* 一样拒绝错位请求。"""

    def test_delete_rejects_wrong_phone(self):
        token = self._login()
        r = self.c.delete("/api/my-accounts/0", json={"phone": "13900000000"},
                          headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertIn("已变化", r.get_json()["error"])
        self.assertFalse(self._acct()["deleted"], "错位请求不得改动任何账号")

    def test_pause_rejects_wrong_phone(self):
        token = self._login()
        r = self.c.put("/api/my-accounts/0/pause", json={"paused": True, "phone": "13900000000"},
                       headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertFalse(self._acct()["user_paused"])

    def test_update_rejects_wrong_phone(self):
        token = self._login()
        r = self.c.put("/api/my-accounts/0",
                       json={"name": "改名", "phone": "13900000000", "password": "pw"},
                       headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertEqual(self._acct()["name"], "我的号")

    def test_matching_phone_still_allowed(self):
        """携带正确手机号（脱敏形态，前端就是这么回传的）不得被误拦。"""
        token = self._login()
        masked = self.webapp._mask_phone(PHONE)
        r = self.c.put("/api/my-accounts/0/pause", json={"paused": True, "phone": masked},
                       headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._acct()["user_paused"])

    def test_without_phone_stays_compatible(self):
        """未携带 phone（旧客户端/测试）保持兼容不校验。"""
        token = self._login()
        r = self.c.delete("/api/my-accounts/0", headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def _acct(self):
        return next(a for a in db.load_accounts() if a["phone"] == PHONE)


class UserFailMailQuotaTest(unittest.TestCase):
    """SCH-8：额度语义 = "每天最多成功提醒 N 次"，发送失败必须归还。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-mailquota-")
        self.env = dict(os.environ)
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ["YIBAN_MAIL_USER_FAIL_DAILY_CAP"] = "1"
        # cap 在模块导入期由 parse_env_int 固定，测试内直接改常量
        self._old_cap = signin.USER_FAIL_MAIL_DAILY_CAP
        signin.USER_FAIL_MAIL_DAILY_CAP = 1

    def tearDown(self):
        signin.USER_FAIL_MAIL_DAILY_CAP = self._old_cap
        os.environ.clear()
        os.environ.update(self.env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _today(self):
        return signin.clock.now().strftime("%Y-%m-%d")

    def test_reserve_then_release_restores_quota(self):
        today = self._today()
        self.assertTrue(signin._user_fail_mail_reserve(PHONE, today))
        self.assertFalse(signin._user_fail_mail_reserve(PHONE, today), "额度已用尽")
        signin._user_fail_mail_release(PHONE, today)
        self.assertTrue(signin._user_fail_mail_reserve(PHONE, today), "归还后应可再用")

    def test_release_floor_is_zero(self):
        today = self._today()
        signin._user_fail_mail_release(PHONE, today)  # 无记录也不得为负
        self.assertTrue(signin._user_fail_mail_reserve(PHONE, today))

    def test_send_failure_releases_quota(self):
        """SMTP 全失败 → 额度归还，当天重试仍有机会（缺陷主场景）。"""
        with mock.patch.object(signin.db, "find_user",
                               return_value={"email": EMAIL, "mail_notify": 1}), \
                mock.patch.object(signin.mailer, "send_user", return_value=False) as send:
            signin.send_user_fail_mail(EMAIL, PHONE, "网络超时")
            signin.send_user_fail_mail(EMAIL, PHONE, "网络超时")  # 若没归还，这次会被额度挡住
        # 两次都尝试过发送 = 额度未被失败吞掉
        self.assertEqual(send.call_count, 2)

    def test_send_success_consumes_quota(self):
        with mock.patch.object(signin.db, "find_user",
                               return_value={"email": EMAIL, "mail_notify": 1}), \
                mock.patch.object(signin.mailer, "send_user", return_value=True) as send:
            signin.send_user_fail_mail(EMAIL, PHONE, "网络超时")
            signin.send_user_fail_mail(EMAIL, PHONE, "网络超时")
        self.assertEqual(send.call_count, 1, "成功后当天不再重复发")


class AccountDedupeTest(unittest.TestCase):
    """SCH-9：同号重复配置只保留一条。"""

    def setUp(self):
        self.env = dict(os.environ)
        os.environ["YIBAN_ACCOUNTS_JSON"] = json.dumps([
            {"phone": PHONE, "password": "p1", "name": "第一条"},
            {"phone": PHONE, "password": "p2", "name": "重复"},
            {"phone": "13800138001", "password": "p3"},
        ])
        os.environ.pop("YIBAN_ACCOUNTS", None)
        os.environ.pop("YIBAN_PHONE", None)
        os.environ["YIBAN_DB_FILE"] = os.path.join(tempfile.mkdtemp(prefix="yiban-dedupe-"), "x.db")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)

    def test_duplicate_phone_kept_once(self):
        accs = signin.load_accounts()
        phones = [a.phone for a in accs]
        self.assertEqual(phones.count(PHONE), 1, "同号必须去重")
        self.assertEqual(len(accs), 2)
        keep = next(a for a in accs if a.phone == PHONE)
        self.assertEqual(keep.name, "第一条", "保留首次出现的那条（顺序即优先级）")

    def test_dedupe_logs_warning(self):
        with self.assertLogs("yiban", "WARNING") as cm:
            signin.load_accounts()
        self.assertTrue(any("重复手机号" in m for m in cm.output), cm.output)


class WindowRecheckAfterSleepTest(unittest.TestCase):
    """SCH-10：等待/间隔对齐之后必须再判一次窗口。"""

    def setUp(self):
        for k in ("YIBAN_RETRY_MIN_INTERVAL", "YIBAN_SIGN_START", "YIBAN_SIGN_END",
                  "YIBAN_WINDOW_EDGE_SEC", "YIBAN_WINDOW_EDGE_FRONT_SEC",
                  "YIBAN_WINDOW_EDGE_BACK_SEC", "YIBAN_MIN_EXEC_GAP", "YIBAN_EXEC_GAP_MIN"):
            os.environ.pop(k, None)
        self.tmp = tempfile.mkdtemp(prefix="yiban-sch10-")
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ["YIBAN_SIGN_END"] = "07:50"

    def tearDown(self):
        for k in ("YIBAN_STATE_DIR", "YIBAN_SIGN_END"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_request_after_window_passes_during_wait(self):
        """到点时间在窗口内、但等完之后已过窗口 → 零请求且记为窗口外跳过。"""
        start = signin.clock.now().replace(hour=7, minute=0, second=0, microsecond=0)
        # 第一次取时（弹出判窗口）= 07:00；睡完之后（二次判定）= 08:00
        calls = {"n": 0}

        def fake_now():
            calls["n"] += 1
            return start if calls["n"] <= 2 else start.replace(hour=8, minute=0)

        acc = signin.Account(phone=PHONE, password="p")
        with mock.patch.object(signin.clock, "now", side_effect=fake_now), \
                mock.patch.object(signin, "attempt_signin") as attempt, \
                mock.patch.object(signin, "_update_cred_state"), \
                mock.patch.object(signin.time, "sleep"), \
                mock.patch.object(signin.time, "monotonic", return_value=100.0):
            results = signin.run_queue_retry(
                [acc], "", 0, 0,
                schedule={PHONE: start + signin.timedelta(seconds=30)})
        self.assertEqual(attempt.call_count, 0, "越过窗口后不得再发起请求")
        self.assertEqual(results[PHONE][3], signin.STATUS_SKIPPED_WINDOW)

    def test_normal_wait_still_executes(self):
        """窗口内等待后照常执行（对照组，防误拦）。"""
        at = signin.clock.now().replace(hour=7, minute=0, second=0, microsecond=0)
        acc = signin.Account(phone=PHONE, password="p")
        with mock.patch.object(signin.clock, "now", return_value=at), \
                mock.patch.object(signin, "attempt_signin",
                                  return_value=(True, "ok", False, signin.STATUS_SUCCESS)) as attempt, \
                mock.patch.object(signin, "_update_cred_state"), \
                mock.patch.object(signin.time, "sleep"), \
                mock.patch.object(signin.time, "monotonic", return_value=100.0):
            signin.run_queue_retry([acc], "", 0, 0,
                                   schedule={PHONE: at + signin.timedelta(seconds=30)})
        self.assertEqual(attempt.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ConfigSummaryMaskingTest(unittest.TestCase):
    """cli --check-config 摘要不得打印完整手机号（会落在 CI 日志/会话/运维群里）。"""

    def test_summary_masks_phone(self):
        import io as _io
        from contextlib import redirect_stdout
        accs = [signin.Account(phone="13800138000", password="p",
                               phone_model="Vivo-XXXX", phone_code="code"),
                signin.Account(phone="13900139001", password="p")]
        buf = _io.StringIO()
        with redirect_stdout(buf):
            signin.print_config_summary(accs)
        out = buf.getvalue()
        self.assertNotIn("13800138000", out, "完整手机号不得出现在摘要里")
        self.assertNotIn("13900139001", out)
        self.assertIn("138****8000", out, "脱敏形态仍应可区分账号")
        self.assertNotIn("code", out.replace("识别码已配置", ""), "识别码不得打印")
