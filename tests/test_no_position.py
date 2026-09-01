# -*- coding: utf-8 -*-
"""无点位账号独立状态回归测试（2026-09-01，用户裁决）。

背景：易班侧无签到点位（登录成功、signPosition 返回 code=0 但 Position 为空，
如任务未配置/当日任务已关闭）此前并入 STATUS_FAILED，导致两个问题：
  1. 与凭据/网络真失败混淆，逐账号触发"易班签到失败"邮件 + webhook + 用户邮件
     ——管理员无从修复，属误报轰炸；
  2. 展示上无法与真失败区分。

现改为独立状态 STATUS_NO_POSITION="no_position"：
  - attempt_signin 无点位分支返回独立状态码；STATUS_SYMBOL 有独立符号 🚫；
  - run_queue_retry 放弃时按"无点位"静默（不触发任何失败通知），仅留日志；
  - main() 汇总归入跳过计数但单独展示（🚫 N 无点位），不计 has_real_failure；
  - **不进入失败重试**：signin 内部 retry budget 1 次即止
    （NO_POSITION_MAX_ATTEMPTS=1），不会像 failed 那样逐账号反复重试；
  - **补签闸门视为未了结**（2026-09-01 修正）：scheduler._UNDONE_STATUSES 含
    no_position——与宿主 run.sh 退出码 2 → SKIPPED → 07:10 补签轮重跑一致
    （学校上午任务未配置=无点位，07:10 已配置=顺带补上；无点位账号 1 次即止、
    幂等无害，不会白跑太多）；
  - 每日状态文件（sign-daily）写入 🚫 供日历展示。

用法（项目根目录）：
    py -m pytest tests/test_no_position_status_091.py -v
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import signin  # noqa: E402

_SCHED_PATH = os.path.join(BASE, "docker", "scheduler.py")

# 与 attempt_signin 无点位分支拼出的完整消息一致（生产 2026-08-31 原样）
NO_POSITION_MSG = "未找到签到位置数据（易班未返回该账号的签到点位，非账号密码问题）"


def _load_sched():
    """按文件路径加载调度器（它位于 docker/ 而非包内，与 test_container_scheduler 同法）。"""
    spec = importlib.util.spec_from_file_location("container_scheduler", _SCHED_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_main(argv=None, run_queue_result=None):
    """在隔离环境下执行 signin.main()，返回退出码或捕获的 SystemExit。

    run_queue_result：替换 run_queue_retry 返回值（不触网）；load_accounts
    按结果的手机号构造账号，保证汇总循环遍历到所有结果。
    """
    tmp = tempfile.mkdtemp(prefix="yiban-nopos-test-")
    old_env = {k: os.environ.get(k) for k in (
        "YIBAN_ACCOUNTS_JSON", "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE",
        "YIBAN_GLOBAL_PAUSE", "YIBAN_SUNDAY_SIGN", "YIBAN_SATURDAY_SIGN",
        "YIBAN_SIGN_MODE", "YIBAN_PROBE_ENABLE", "YIBAN_SIGN_START", "YIBAN_SIGN_END",
        "YIBAN_WINDOW_EDGE_FRONT_SEC", "YIBAN_WINDOW_EDGE_BACK_SEC",
    )}
    old_argv = sys.argv[:]
    try:
        os.environ["YIBAN_ACCOUNTS_JSON"] = json.dumps(
            [{"phone": "13800000000", "password": "test-pass", "name": "测试"}]
        )
        os.environ["YIBAN_DB_FILE"] = os.path.join(tmp, "empty.db")
        os.environ["YIBAN_STATE_DIR"] = tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(tmp, "sign.log")
        for k in ("YIBAN_GLOBAL_PAUSE", "YIBAN_SUNDAY_SIGN", "YIBAN_SATURDAY_SIGN",
                  "YIBAN_SIGN_MODE", "YIBAN_PROBE_ENABLE",
                  "YIBAN_WINDOW_EDGE_FRONT_SEC", "YIBAN_WINDOW_EDGE_BACK_SEC"):
            os.environ.pop(k, None)
        # 窗口覆盖全天：避免测试运行时已过默认 07:50 截止导致 build_schedule 提前收口
        os.environ["YIBAN_SIGN_START"] = "00:00"
        os.environ["YIBAN_SIGN_END"] = "23:59"
        sys.argv = ["signin.py"] + (argv or [])
        result = run_queue_result or {}

        def _fake_load_accounts():
            return [
                signin.Account(phone=p, password="test-pass", name="测试")
                for p in result
            ] or [
                signin.Account(phone="13800000000", password="test-pass", name="测试")
            ]

        with mock.patch.object(signin, "load_accounts", side_effect=_fake_load_accounts), \
             mock.patch.object(signin, "run_queue_retry", return_value=result), \
             mock.patch.object(signin, "_acquire_run_lock", return_value=None), \
             mock.patch.object(signin, "_save_cred_state"), \
             mock.patch.object(signin, "_flush_admin_mail_summary"), \
             mock.patch.object(signin, "_maybe_alert_zero_success", return_value=False):
            try:
                signin.main()
                return 0, tmp
            except SystemExit as e:
                return e.code, tmp
    finally:
        for k in ("YIBAN_ACCOUNTS_JSON", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE", "YIBAN_GLOBAL_PAUSE", "YIBAN_SUNDAY_SIGN",
                  "YIBAN_SATURDAY_SIGN", "YIBAN_SIGN_MODE", "YIBAN_PROBE_ENABLE",
                  "YIBAN_SIGN_START", "YIBAN_SIGN_END",
                  "YIBAN_WINDOW_EDGE_FRONT_SEC", "YIBAN_WINDOW_EDGE_BACK_SEC"):
            if old_env.get(k) is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old_env[k]
        sys.argv = old_argv


class NoPositionConstantTest(unittest.TestCase):
    """独立状态码与符号必须存在，且不与既有状态混淆。"""

    def test_status_code_is_independent(self):
        self.assertEqual(signin.STATUS_NO_POSITION, "no_position")
        # 不与任何既有状态码重复
        existing = {
            signin.STATUS_SUCCESS, signin.STATUS_ALREADY, signin.STATUS_NO_TASK,
            signin.STATUS_FAILED, signin.STATUS_RETRYING, signin.STATUS_SKIPPED_WINDOW,
            signin.STATUS_SKIPPED_NORANGE, signin.STATUS_PAUSED,
            signin.STATUS_USER_CANCELLED, signin.STATUS_PENDING,
            signin.STATUS_GLOBAL_PAUSED,
        }
        self.assertNotIn(signin.STATUS_NO_POSITION, existing)

    def test_symbol_mapping_exists(self):
        self.assertIn(signin.STATUS_NO_POSITION, signin.STATUS_SYMBOL)
        self.assertEqual(signin.STATUS_SYMBOL[signin.STATUS_NO_POSITION], "🚫")

    def test_retry_budget_still_one_and_no_cache_clear(self):
        """无点位 1 次即止、不清会话缓存（独立状态不改变既有重试预算）。"""
        self.assertEqual(signin._retry_budget(NO_POSITION_MSG),
                         (signin.NO_POSITION_MAX_ATTEMPTS, False))
        self.assertEqual(signin.NO_POSITION_MAX_ATTEMPTS, 1)

    def test_not_credential_failure(self):
        """无点位消息不得被当作凭据失败累计账密熔断。"""
        self.assertFalse(signin._is_credential_failure(NO_POSITION_MSG))


class NoPositionAttemptSigninTest(unittest.TestCase):
    """attempt_signin 无点位分支必须返回独立状态码（而非 STATUS_FAILED）。"""

    def test_signin_no_position_returns_independent_status(self):
        acc = signin.Account(phone="13800000000", password="p")
        client = signin.YibanClient(acc)
        client.logged_in = True  # 跳过真实登录
        resp = mock.Mock()
        resp.text = "{}"
        resp.json.return_value = {"code": 0, "data": {"Msg": "", "Position": [], "Range": {}}}
        client.session.get = mock.Mock(return_value=resp)

        success, message, skip, status = client.signin()

        self.assertFalse(success)
        self.assertIn("未找到签到位置数据", message)
        self.assertFalse(skip, "无点位不是窗口跳过，不应标 skip（保持不重试语义由 _retry_budget 控制）")
        self.assertEqual(status, signin.STATUS_NO_POSITION)
        self.assertNotEqual(status, signin.STATUS_FAILED,
                            "无点位必须独立于 failed，否则会被当失败告警、计入 fail_n")

    def test_attempt_signin_wrapper_keeps_independent_status(self):
        """attempt_signin 包装层（含 _wipe_credentials）透传独立状态码。"""
        acc = signin.Account(phone="13800000000", password="p")
        fake_client = mock.Mock()
        fake_client.use_killyiban = True
        fake_client.logged_in = True
        fake_client.signin.return_value = (
            False, NO_POSITION_MSG, False, signin.STATUS_NO_POSITION
        )
        with mock.patch.object(signin, "YibanClient", return_value=fake_client):
            _success, _message, _skip, status = signin.attempt_signin(acc)
        self.assertEqual(status, signin.STATUS_NO_POSITION)
        fake_client._wipe_credentials.assert_called_once()


class NoPositionRetryNotificationTest(unittest.TestCase):
    """run_queue_retry 放弃无点位账号时不得触发任何"签到失败"通知（防误报轰炸）。

    对照组：真失败仍须照常通知（确保没有把真失败静默掉）。
    """

    def setUp(self):
        self.acc = signin.Account(phone="13800000000", password="p")
        self.acc.owner = "admin"

    def _run_queue(self, attempt_result, schedule=None):
        acc = self.acc
        # 窗口覆盖全天：避免测试运行时已过默认 07:50 截止，schedule 分支在
        # _window_closed 处整体落 skipped_window（与无点位分支无关）。
        with mock.patch.dict(os.environ, {
            "YIBAN_SIGN_START": "00:00",
            "YIBAN_SIGN_END": "23:59",
            "YIBAN_WINDOW_EDGE_FRONT_SEC": "0",
            "YIBAN_WINDOW_EDGE_BACK_SEC": "0",
        }), \
             mock.patch.object(signin, "attempt_signin", return_value=attempt_result), \
             mock.patch.object(signin, "_write_sign_state"), \
             mock.patch.object(signin, "_update_cred_state"), \
             mock.patch.object(signin, "_collect_admin_mail") as m_mail, \
             mock.patch.object(signin, "send_notification") as m_notify, \
             mock.patch.object(signin, "send_user_fail_mail") as m_user, \
             mock.patch.object(signin.time, "sleep"):
            results = signin.run_queue_retry(
                [acc], "http://notify.invalid", 0, 0, schedule=schedule
            )
        return results, m_mail, m_notify, m_user

    def test_queue_mode_no_position_no_notifications(self):
        results, m_mail, m_notify, m_user = self._run_queue(
            (False, NO_POSITION_MSG, False, signin.STATUS_NO_POSITION)
        )
        self.assertEqual(results[self.acc.phone][3], signin.STATUS_NO_POSITION)
        self.assertIn("未找到签到位置数据", results[self.acc.phone][1],
                      "结果 message 应透传")
        m_mail.assert_not_called()
        m_notify.assert_not_called()
        m_user.assert_not_called()

    def test_queue_mode_real_failure_still_notifies(self):
        """对照组：真失败（凭据）仍须发邮件/webhook/用户邮件——不能因本次改动静默。"""
        results, m_mail, m_notify, m_user = self._run_queue(
            (False, "登录失败: 账号或密码错误", False, signin.STATUS_FAILED)
        )
        self.assertEqual(results[self.acc.phone][3], signin.STATUS_FAILED)
        m_mail.assert_called_once()
        m_notify.assert_called_once()
        m_user.assert_called_once()

    def test_schedule_mode_no_position_no_notifications(self):
        """schedule（时间驱动）分支同样跳过通知。"""
        schedule = {self.acc.phone: datetime.now()}
        results, m_mail, m_notify, m_user = self._run_queue(
            (False, NO_POSITION_MSG, False, signin.STATUS_NO_POSITION),
            schedule=schedule,
        )
        self.assertEqual(results[self.acc.phone][3], signin.STATUS_NO_POSITION)
        m_mail.assert_not_called()
        m_notify.assert_not_called()
        m_user.assert_not_called()

    def test_schedule_mode_real_failure_still_notifies(self):
        schedule = {self.acc.phone: datetime.now()}
        results, m_mail, _m_notify, _m_user = self._run_queue(
            (False, "登录失败: 账号或密码错误", False, signin.STATUS_FAILED),
            schedule=schedule,
        )
        self.assertEqual(results[self.acc.phone][3], signin.STATUS_FAILED)
        m_mail.assert_called_once()


class NoPositionMainSummaryTest(unittest.TestCase):
    """main() 汇总与退出码：无点位 ≠ 真失败，但当天确未签到（SKIPPED 语义）。"""

    def test_no_position_only_exits_2(self):
        """仅无点位账号 → 不计失败（exit 1 不应出现）；无实际执行 → exit 2（SKIPPED）。"""
        code, _ = _run_main(run_queue_result={
            "13800000000": (False, NO_POSITION_MSG, False, signin.STATUS_NO_POSITION),
        })
        self.assertEqual(code, 2, "无点位=当天未签到但非账号失败，应走 SKIPPED 语义")

    def test_no_position_with_success_exits_0(self):
        """无点位 + 成功 → 有已了结账号且无真失败 → exit 0（不拖累整体）。"""
        code, _ = _run_main(run_queue_result={
            "13800000000": (False, NO_POSITION_MSG, False, signin.STATUS_NO_POSITION),
            "13900000001": (True, "签到成功", False, signin.STATUS_SUCCESS),
        })
        self.assertEqual(code, 0)

    def test_no_position_with_real_failure_exits_1(self):
        """无点位 + 真失败 → 真失败主导 → exit 1（失败告警不受无点位稀释）。"""
        code, _ = _run_main(run_queue_result={
            "13800000000": (False, NO_POSITION_MSG, False, signin.STATUS_NO_POSITION),
            "13900000001": (False, "登录失败: 账号或密码错误", False, signin.STATUS_FAILED),
        })
        self.assertEqual(code, 1)

    def test_daily_state_file_writes_no_position_symbol(self):
        """按日状态文件（sign-daily）应写入 🚫 供日历展示（与 failed 的 ❌ 区分）。"""
        _code, tmp = _run_main(run_queue_result={
            "13800000000": (False, NO_POSITION_MSG, False, signin.STATUS_NO_POSITION),
        })
        daily_path = os.path.join(tmp, f"sign-daily-{datetime.now():%Y-%m-%d}.json")
        self.assertTrue(os.path.exists(daily_path), "应写入按日状态文件")
        with open(daily_path, encoding="utf-8") as f:
            daily = json.load(f)
        self.assertEqual(daily.get("13800000000"), "🚫")


class NoPositionSchedulerGateTest(unittest.TestCase):
    """容器补签闸门：no_position **计入**未了结，07:10 顺带重试（对齐宿主 exit 2）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sched-nopos-")
        self.sched = _load_sched()
        self.sched.STATEDIR = self.tmp
        self.sched.ENV_FILE = os.path.join(self.tmp, ".env")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_marker(self):
        with open(os.path.join(self.tmp, f"sched-run-{datetime.now():%Y-%m-%d}.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"completed": True}, f)

    def _write_state(self, data):
        with open(os.path.join(self.tmp, f"sign-state-{datetime.now():%Y-%m-%d}.json"),
                  "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_no_position_in_undone_statuses(self):
        """no_position 必须列入未了结——容器 07:10 与宿主 run.sh SKIPPED 语义一致。"""
        self.assertIn("no_position", self.sched._UNDONE_STATUSES)

    def test_no_position_only_needs_second_sign(self):
        """只有无点位账号（且全量标记已写）→ 补签仍重跑（07:10 顺带重试一次兜底）。"""
        self._write_marker()
        self._write_state({"13800000000": {"status": "no_position"}})
        self.assertTrue(self.sched._has_undone_today())

    def test_failed_plus_no_position_still_needs_second_sign(self):
        """无点位 + 真失败 → 补签照常重跑（真失败账号需要兜底）。"""
        self._write_marker()
        self._write_state({
            "13800000000": {"status": "no_position"},
            "13800000001": {"status": "failed"},
        })
        self.assertTrue(self.sched._has_undone_today())

    def test_host_container_dual_path_consistent(self):
        """宿主/容器双路径一致：宿主 no_position 汇总归 skip → exit 2 → run.sh 写
        SKIPPED → 07:10 补签重跑；容器 _UNDONE_STATUSES 含 no_position →
        _has_undone_today() 为真 → 07:10 补签同样重跑。两条路径都必须触发补签轮。"""
        self._write_marker()
        self._write_state({"13800000000": {"status": "no_position"}})
        self.assertTrue(self.sched._has_undone_today(),
                        "容器侧：no_position 计入未了结，补签轮应重跑")
        code, _ = _run_main(run_queue_result={
            "13800000000": (False, NO_POSITION_MSG, False, signin.STATUS_NO_POSITION),
        })
        self.assertEqual(code, 2,
                         "宿主侧：仅无点位 → exit 2（SKIPPED），run.sh 07:10 补签重跑")


if __name__ == "__main__":
    unittest.main(verbosity=2)
