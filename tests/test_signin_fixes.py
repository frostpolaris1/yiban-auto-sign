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
import tempfile
import unittest
import unittest.mock as mock
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import signin  # noqa: E402


def _challenge_text(target_url, cookie="abc123", arg=0, k=1000, legacy_bound=False):
    """构造可被 _solve_ydclearance 解析的本地假挑战页（不访问网络）。

    按真模板形状造：po 循环 `qo < oo.length - 1` 比 C 段上界多取一格，而真模板里
    n_c = len(oo) - 3，故 n_c+1 落在 C 变换范围之外——那一格直接存"收尾字符 ^ arg"
    （收尾字符是跳转路径的右引号）。两段都按 `chr(oo[i] ^ arg)` 的读法预先把 arg 异或
    编进去（arg 需 ≤ 0xFF，否则异或的高位会在解码侧带回来）。legacy_bound=True 还原
    改造前的假页形状（数组不设最后两格、po 上界恰好等于 n_c），用作"少解收尾字符"的负对照。
    """
    desired = f"https_ydclearance={cookie};window.document.location=\"{target_url}\""
    add1, add2, shift_l, shift_r = 1, 2, 3, 5
    head, tail = (desired, "") if legacy_bound else (desired[:-1], desired[-1])
    n_c = len(head)
    vals = [ord(c) ^ arg for c in head]  # 先异或 arg：解码侧读法是 chr(oo[i] ^ arg)
    arr = [0] + [
        ((((v >> shift_l) | ((v << shift_r) & 0xFF)) - add1 - add2) & 0xFF) for v in vals
    ]
    if not legacy_bound:
        arr += [ord(tail) ^ arg, 0]  # n_c+1 格存尾字符；len(oo)-1 格在模板里从不参与运算
    arr_str = ",".join(hex(x) for x in arr)
    return (
        "function ab(arg) { "
        'eval("qo=eval;qo(po);"); '
        "oo = [" + arr_str + "]; "
        '"qo=1; do{oo[qo]=(-oo[qo])&0xff;'
        "oo[qo]=((oo[qo]>>3)|((oo[qo]<<5)&0xff)-1)&0xff;} while(--qo>=2);\" "
        "qo = 1; do { oo[qo] = (oo[qo] - oo[qo - 1]) } while(--qo>=2); "
        f"if (qo > {n_c}) break; "
        "oo[qo] = ((((oo[qo] + 1) & 0xff) + 2) & 0xff) << 3) >> 5); qo++; "
        f"for (qo = 1; qo < oo.length - 1; qo++) "
        f"if (qo % {k}) po += String.fromCharCode(oo[qo] ^ arg); "
        f'window.document.location="{target_url}"; }} '
        f'window.onload=setTimeout("ab({arg})", 200) </script>'
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

    # ---- T-WAF-9：po 上界与真模板 `qo < oo.length - 1` 对齐 ----
    def test_solve_ydclearance_decodes_tail_char_outside_transform_c(self):
        """收尾字符（路径右引号）在下标 len(oo)-2，不在 C 变换范围内，必须逐字解出。

        arg 同时覆盖非 0 值：假页的 head 与 tail 两段都要按 `chr(oo[i] ^ arg)` 的
        读法把参数异或编进去，否则只有 arg=0 才自洽。158 是公开样本里的真实量级。
        """
        client = signin.YibanClient.__new__(signin.YibanClient)
        for arg in (0, 7, 158):
            with self.subTest(arg=arg):
                cookie, target = client._solve_ydclearance(
                    _challenge_text("https://f.yiban.cn/iapp7463", arg=arg)
                )
                self.assertEqual(cookie, "abc123")
                self.assertEqual(target, "https://f.yiban.cn/iapp7463")

    def test_solve_ydclearance_old_fixture_shape_fails_loudly(self):
        """改造前的假页形状（po 上界恰好等于 C 段上界）在新实现下必须响亮失败，不得静默截断。"""
        client = signin.YibanClient.__new__(signin.YibanClient)
        with self.assertRaisesRegex(RuntimeError, "ydclearance 挑战解析失败"):
            client._solve_ydclearance(
                _challenge_text("https://f.yiban.cn/iapp7463", legacy_bound=True)
            )

    # ---- T-WAF-6：异常契约（越界输入给明确 RuntimeError，不给裸内置异常）----
    def test_solve_ydclearance_rejects_arg_beyond_code_point_range(self):
        client = signin.YibanClient.__new__(signin.YibanClient)
        with self.assertRaisesRegex(RuntimeError, "ydclearance 挑战解析失败"):
            client._solve_ydclearance(
                _challenge_text("https://f.yiban.cn/iapp7463", arg=0x110000)
            )

    def test_solve_ydclearance_rejects_zero_k(self):
        client = signin.YibanClient.__new__(signin.YibanClient)
        with self.assertRaisesRegex(RuntimeError, "ydclearance 挑战解析失败"):
            client._solve_ydclearance(_challenge_text("https://f.yiban.cn/iapp7463", k=0))

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
            # 重试打散项 random.uniform(0, RETRY_GAP_MAX) 统一返回 0：排除随机抖动后
            # 精确断言下限
            rnd.uniform.return_value = 0.0
            signin.run_queue_retry([acc], "", 0, 30)
        self.assertTrue(sleeps, "应发生重试等待")
        self.assertGreaterEqual(sleeps[0], signin.RETRY_MIN_INTERVAL,
                                "重试总间隔不得小于 RETRY_MIN_INTERVAL")
        # 重试回队的间隔对齐只补残差（elapsed 已含原地等待），不得二次等待：
        # 总次数=原地等待+对齐残差，且合计仍 ≥ RETRY_MIN_INTERVAL
        self.assertEqual(len(sleeps), 2)
        self.assertLessEqual(sleeps[1], 30)
        self.assertGreaterEqual(sleeps[0] + sleeps[1], signin.RETRY_MIN_INTERVAL)

    def test_manual_queue_honors_account_gap_floor(self):
        """手动队列（schedule=None）相邻请求间隔必须 ≥ 账号间隔设置（下限语义）。

        手动路径与自动调度、容量预估 (avg+gap) 共用「最小间隔」口径；
        原实现此处为 U(0, gap) 随机打散：期望仅 gap/2、下界 0，违背
        「自动与手动签到均生效的最小间隔」承诺。"""
        accs = [signin.Account(phone=f"1380000000{i}", password="p") for i in (1, 2)]
        sleeps = []
        with mock.patch.object(signin, "attempt_signin",
                               return_value=(True, "已签到", False, "already")), \
             mock.patch.object(signin.time, "sleep", side_effect=sleeps.append), \
             mock.patch.object(signin.time, "monotonic", return_value=1000.0), \
             mock.patch.object(signin, "_write_sign_state"), \
             mock.patch.object(signin, "_update_cred_state"):
            signin.run_queue_retry(accs, "", 0, 10, schedule=None, cred_state={})
        # monotonic 冻结 → 账号 2 弹出时距上次尝试 elapsed=0，应补足整段间隔 10s；
        # 首个账号不等待
        self.assertEqual(sleeps, [10.0])

    def test_manual_queue_gap_zero_no_wait(self):
        """账号间隔 0=关闭：手动队列不得引入额外等待。"""
        accs = [signin.Account(phone=f"1380000000{i}", password="p") for i in (3, 4)]
        sleeps = []
        with mock.patch.object(signin, "attempt_signin",
                               return_value=(True, "已签到", False, "already")), \
             mock.patch.object(signin.time, "sleep", side_effect=sleeps.append), \
             mock.patch.object(signin.time, "monotonic", return_value=1000.0), \
             mock.patch.object(signin, "_write_sign_state"), \
             mock.patch.object(signin, "_update_cred_state"):
            signin.run_queue_retry(accs, "", 0, 0, schedule=None, cred_state={})
        self.assertEqual(sleeps, [])

    def test_gap_max_clamped_to_3600(self):
        """.env 直配超大值不得把队列睡死（与网页设置侧 3600 上限同口径）。"""
        accs = [signin.Account(phone=f"1380000000{i}", password="p") for i in (5, 6)]
        sleeps = []
        with mock.patch.object(signin, "attempt_signin",
                               return_value=(True, "已签到", False, "already")), \
             mock.patch.object(signin.time, "sleep", side_effect=sleeps.append), \
             mock.patch.object(signin.time, "monotonic", return_value=1000.0), \
             mock.patch.object(signin, "_write_sign_state"), \
             mock.patch.object(signin, "_update_cred_state"):
            signin.run_queue_retry(accs, "", 0, 86400, schedule=None, cred_state={})
        self.assertTrue(sleeps and max(sleeps) <= 3600)

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
        测试见 tests/test_notify_webhook.py（实现已从 signin 内联迁出）。
        """
        with mock.patch.object(signin.notify, "send", return_value=True) as m:
            signin.send_notification("标题", "内容", "https://legacy.example.com/hook")
        # send_notification 透传 urgent/force（默认 False）
        m.assert_called_once_with("标题", "内容", urgent=False, force=False)

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
