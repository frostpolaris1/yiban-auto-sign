# -*- coding: utf-8 -*-
"""0.21.0 Task 4：签到核心修复失败测试。

覆盖（先红后绿）：
- H2：状态文件损坏/目录不可写不抛异常
- H8：ydclearance 跳转 URL 白名单
- H9：attempt_signin 异常返回脱敏 safe_err
- M13：重试等待总间隔不小于 retry_min_interval
- M16：通知响应状态码检查与 URL 脱敏
- 低项：user_paused 显式布尔解析
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import signin  # noqa: E402


def _challenge_text(target_url, cookie="abc123"):
    """构造可被 _solve_ydclearance 解析的本地假挑战页（不访问网络）。"""
    desired = f"https_ydclearance={cookie};window.document.location=\"{target_url}\""
    add1, add2, shift_l, shift_r = 1, 2, 3, 5
    arr = [0] + [
        ((((ord(c) >> shift_l) | ((ord(c) << shift_r) & 0xFF)) - add1 - add2) & 0xFF)
        for c in desired
    ]
    arr_str = ",".join(hex(x) for x in arr)
    n_c = len(desired)
    return (
        "function ab(arg) { "
        'eval("qo=eval;qo(po);"); '
        "oo = [" + arr_str + "]; "
        '"qo=1; do{oo[qo]=(-oo[qo])&0xff;'
        "oo[qo]=((oo[qo]>>3)|((oo[qo]<<5)&0xff)-1)&0xff;} while(--qo>=2);\" "
        "qo = 1; do { oo[qo] = (oo[qo] - oo[qo - 1]) } while(--qo>=2); "
        f"if (qo > {n_c}) break; "
        "oo[qo] = ((((oo[qo] + 1) & 0xff) + 2) & 0xff) << 3) >> 5); qo++; "
        "if (qo % 1000) po += String.fromCharCode(oo[qo] ^ arg); "
        f'window.document.location="{target_url}"; }} '
        'window.onload=setTimeout("ab(0)", 200) </script>'
    )


class SigninFixes021Test(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="yiban-signin-fix-")
        self._old_state_dir = os.environ.get("YIBAN_STATE_DIR")
        os.environ["YIBAN_STATE_DIR"] = self._tmp

    def tearDown(self):
        if self._old_state_dir is None:
            os.environ.pop("YIBAN_STATE_DIR", None)
        else:
            os.environ["YIBAN_STATE_DIR"] = self._old_state_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---- H2 ----
    def test_write_sign_state_rebuilds_corrupt_json(self):
        today = datetime.now().strftime("%Y-%m-%d")
        state_path = os.path.join(self._tmp, f"sign-state-{today}.json")
        with open(state_path, "w", encoding="utf-8") as f:
            f.write("{ not json !!!")
        signin._write_sign_state("13800138000", "success", "ok")
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["13800138000"]["status"], "success")

    def test_write_sign_state_rebuilds_non_dict_json(self):
        today = datetime.now().strftime("%Y-%m-%d")
        state_path = os.path.join(self._tmp, f"sign-state-{today}.json")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(["not", "dict"], f)
        signin._write_sign_state("13800138000", "already", "今日已签到")
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["13800138000"]["status"], "already")

    def test_write_sign_state_unwritable_dir_does_not_raise(self):
        # state_dir 是一个普通文件：makedirs 会失败，必须被吞掉
        blocked = os.path.join(self._tmp, "blocked")
        with open(blocked, "w", encoding="utf-8") as f:
            f.write("x")
        os.environ["YIBAN_STATE_DIR"] = blocked
        signin._write_sign_state("13800138000", "failed", "err")  # 不应抛异常

    # ---- H8 ----
    def test_solve_ydclearance_accepts_whitelist_absolute_url(self):
        client = signin.YibanClient.__new__(signin.YibanClient)
        cookie, target = client._solve_ydclearance(_challenge_text("https://f.yiban.cn/iapp7463"))
        self.assertEqual(cookie, "abc123")
        self.assertTrue(target.startswith("https://f.yiban.cn"), target)

    def test_solve_ydclearance_rejects_non_whitelist_url(self):
        client = signin.YibanClient.__new__(signin.YibanClient)
        with self.assertRaisesRegex(RuntimeError, "ydclearance 跳转目标不在白名单"):
            client._solve_ydclearance(_challenge_text("http://evil.example"))

    def test_solve_ydclearance_rejects_lookalike_host(self):
        client = signin.YibanClient.__new__(signin.YibanClient)
        with self.assertRaisesRegex(RuntimeError, "ydclearance 跳转目标不在白名单"):
            client._solve_ydclearance(_challenge_text("https://f.yiban.cn.evil.com/iapp7463"))

    def test_solve_ydclearance_rejects_userinfo_bypass(self):
        client = signin.YibanClient.__new__(signin.YibanClient)
        with self.assertRaisesRegex(RuntimeError, "ydclearance 跳转目标不在白名单"):
            client._solve_ydclearance(_challenge_text("https://f.yiban.cn@evil.com/iapp7463"))

    def test_is_fyiban_url_helper(self):
        self.assertTrue(signin._is_fyiban_url("https://f.yiban.cn/iapp7463"))
        self.assertFalse(signin._is_fyiban_url("https://f.yiban.cn.evil.com/iapp7463"))
        self.assertFalse(signin._is_fyiban_url("https://f.yiban.cn@evil.com/iapp7463"))
        self.assertFalse(signin._is_fyiban_url("http://f.yiban.cn/iapp7463"))

    # ---- H9 ----
    def test_attempt_signin_returns_safe_err_not_raw_exception(self):
        acc = signin.Account(phone="13800138000", password="secret")
        with mock.patch.object(signin.YibanClient, "login_killyiban",
                               side_effect=ValueError("boom password='raw'")), \
             mock.patch.object(signin, "_sanitize_text", return_value="SANITIZED"):
            success, message, skip, status = signin.attempt_signin(acc)
        self.assertFalse(success)
        self.assertEqual(message, "SANITIZED")
        self.assertFalse(skip)
        self.assertEqual(status, signin.STATUS_FAILED)

    # ---- M13 ----
    def test_retry_wait_never_below_retry_min_interval(self):
        acc = signin.Account(phone="13800138000", password="p")
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)

        with mock.patch.object(signin, "attempt_signin",
                               return_value=(False, "网络超时", False, signin.STATUS_FAILED)), \
             mock.patch.object(signin, "classify_failure", return_value=2), \
             mock.patch.object(signin, "random") as rnd, \
             mock.patch.object(signin.time, "sleep", side_effect=fake_sleep), \
             mock.patch.object(signin, "_write_sign_state"), \
             mock.patch.object(signin, "_update_cred_state"), \
             mock.patch.object(signin, "send_notification"):
            # random_delay 里也调用 random.uniform；统一返回 0 使旧实现暴露出最小间隔不足
            rnd.uniform.return_value = 0.0
            signin.run_queue_retry([acc], "", 0, 30)
        self.assertTrue(sleeps, "应发生重试等待")
        self.assertGreaterEqual(sleeps[0], signin.RETRY_MIN_INTERVAL,
                                "重试总间隔不得小于 RETRY_MIN_INTERVAL")

    def test_retry_wait_schedule_branch_respects_sch_cfg_retry_min_interval(self):
        """P4（2026-08-27）：schedule 分支重试改为非阻塞重插——重试落点 ≥ now + retry_min_interval，
        不再原地 sleep ≥5s 阻塞整条队列。"""
        from random import Random as _R
        acc = signin.Account(phone="13800138000", password="p")
        sleeps = []
        scheduled_at = datetime.now()
        captured = {}
        orig_next = signin._next_retry_at

        def spy(now_dt, sch_cfg, rng=None):
            captured["nxt"] = orig_next(now_dt, sch_cfg, rng=_R(1))
            return captured["nxt"]

        with mock.patch.dict(
            os.environ,
            {
                "YIBAN_RETRY_MIN_INTERVAL": "5",
                "YIBAN_SIGN_START": "00:00",
                "YIBAN_SIGN_END": "23:59",
                "YIBAN_WINDOW_EDGE_SEC": "0",
            },
            clear=False,
        ), \
             mock.patch.object(signin, "attempt_signin",
                               return_value=(False, "网络超时", False, signin.STATUS_FAILED)), \
             mock.patch.object(signin, "classify_failure", return_value=2), \
             mock.patch.object(signin.time, "sleep", side_effect=lambda s: sleeps.append(s)), \
             mock.patch.object(signin, "_write_sign_state"), \
             mock.patch.object(signin, "_update_cred_state"), \
             mock.patch.object(signin, "send_notification"), \
             mock.patch.object(signin, "_next_retry_at", side_effect=spy):
            signin.run_queue_retry(
                [acc], "", 0, 0, schedule={acc.phone: scheduled_at}
            )
        self.assertIn("nxt", captured, "schedule 分支应计算重试落点")
        # 落点距失败时刻 ≥ retry_min_interval（5s，容差 2s），且不超出窗口末端（eff_hi=23:59）
        self.assertGreaterEqual(
            captured["nxt"] - datetime.now(),
            __import__("datetime").timedelta(seconds=3),
            "重试落点不得早于 now + retry_min_interval",
        )
        # 落点 ≤ eff_hi = sign_end - edge_back = 23:59（当日）
        eff_hi = datetime.now().replace(hour=23, minute=59, second=0, microsecond=0)
        self.assertLessEqual(captured["nxt"], eff_hi, "重试落点不得越过窗口末端")

    # ---- 通知组件化（2026-08-29）----
    def test_send_notification_delegates_to_notify_component(self):
        """signin 侧 webhook 已组件化：send_notification 委托 scripts/notify.py。

        webhook 的格式适配（Server酱 title+desp）、节流、响应检查与日志脱敏
        测试见 tests/test_notify_0829.py（实现已从 signin 内联迁出）。
        """
        with mock.patch.object(signin.notify, "send", return_value=True) as m:
            signin.send_notification("标题", "内容", "https://legacy.example.com/hook")
        m.assert_called_once_with("标题", "内容")

    # ---- 低项：user_paused 显式布尔解析 ----
    def test_parse_account_dict_user_paused_explicit_truthy(self):
        for raw in ("1", "true", "on", "yes", " True ", 1):
            acc = signin._parse_account_dict(
                {"phone": "13800138000", "password": "p", "user_paused": raw}
            )
            self.assertTrue(acc.user_paused, raw)
        for raw in ("0", "false", "off", "no", "", 0):
            acc = signin._parse_account_dict(
                {"phone": "13800138000", "password": "p", "user_paused": raw}
            )
            self.assertFalse(acc.user_paused, raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
