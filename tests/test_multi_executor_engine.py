# -*- coding: utf-8 -*-
"""多执行体的**引擎侧**行为断言：领取池进入执行循环后的分派与了结语义。

三件必须钉住的事（缺一个就会在生产上表现为"漏签"或"重复登录"）：

1. **同一账号同一天只被自动执行碰一次**：昨轮已了结（成功/已签到/今日无任务）
   的账号，下一轮（补签轮/兜底执行体）不再发起任何请求——这是防重复登录的第一道闸；
2. **未了结的账号必须能被下一轮接手**：失败、窗口外跳过等落在 `failed` 并放开租约，
   补签轮正是为它们存在（若这里也挡，等于把补签轮废掉）；
3. **手动指定账号（--only）照做**：用户主动点的签到可以重签当日已了结的账号。

另外两条边界：别的执行体正在做（`claimed` 且租约有效）时本进程不碰；库未初始化时
领取池不参与、也不留下任何副作用（纯状态文件部署不受影响）。
"""
import contextlib
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402
import signin  # noqa: E402

#: 固定业务时间（周三 06:40，落在默认签到窗口内）：窗口判定与"当日"都因此确定，
#: 也让领取池的 day 键稳定（否则跨午夜跑测会落到两天上）
FIXED_NOW = datetime(2026, 9, 16, 6, 40)
DAY = "2026-09-16"

PHONE_OK = "13800000001"
PHONE_FAIL = "13800000002"
PHONE_BUSY = "13800000003"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-mx-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + "a" * 64 + "\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": "a" * 64,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": os.path.join(cls.tmp, "yiban.db"),
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
        })

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
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            path = os.environ["YIBAN_DB_FILE"] + suffix
            if os.path.exists(path):
                os.remove(path)
        db.init_db(os.environ["YIBAN_DB_FILE"], env_file=self.env_file, cleanup=False)

    def _acc(self, phone):
        return SimpleNamespace(phone=phone, user_paused=False, owner="", account_id=0,
                               password="p")

    @staticmethod
    def _frozen_clock():
        """把业务时钟固定在 FIXED_NOW（窗口判定、当日、租约判据都因此确定）。

        预置领取行与跑队列**必须用同一个时钟**：租约判据是拿"当前时间"与行上的心跳
        比大小，一边用真实时间、一边用冻结时间就会得出"租约还没到期"（或反之）的
        错误结论——这个陷阱我在此处踩过一次。
        """
        class _FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return FIXED_NOW

        return mock.patch.object(signin.clock, "now", _FakeDT.now)

    def _run(self, phone, result, *, reclaim=False, executor="", schedule=True):
        """跑一轮单账号队列，返回 (results, 实际发起尝试的账号, 状态写入记录)。"""
        calls = []
        states = []

        def fake_attempt(_acc):
            calls.append(_acc.phone)
            return result

        with mock.patch.dict(os.environ, {"YIBAN_EXECUTOR_ID": executor} if executor else {},
                             clear=False), \
                self._frozen_clock(), \
                mock.patch.object(signin, "attempt_signin", side_effect=fake_attempt), \
                mock.patch.object(signin, "_write_sign_state",
                                  side_effect=lambda p, st, msg, **kw: states.append((p, st))), \
                mock.patch.object(signin.time, "sleep"):
            sched = {phone: FIXED_NOW - timedelta(seconds=5)} if schedule else None
            results = signin.run_queue_retry([self._acc(phone)], None, 0, 0,
                                             schedule=sched, cred_state={},
                                             reclaim=reclaim)
        return results, calls, states


class SettledAccountNotRetriedTest(_Base):
    """① 已了结的账号，下一轮不再自动碰。"""

    OK = (True, "签到成功", False, signin.STATUS_SUCCESS)

    def test_second_run_skips_settled_account(self):
        _r, calls1, _s = self._run(PHONE_OK, self.OK, executor="exec-A:1")
        self.assertEqual(calls1, [PHONE_OK], "第一轮应当尝试")
        self.assertEqual(db.claim_states_for_day(DAY)[PHONE_OK], db.CLAIM_STATE_DONE)

        results2, calls2, _s2 = self._run(PHONE_OK, self.OK, executor="exec-B:2")
        self.assertEqual(calls2, [], "已了结的账号不得被下一轮再次登录")
        self.assertEqual(results2, {}, "别的执行体的活不计入本进程结果")

    def test_no_task_counts_as_settled(self):
        """`no_task`（今日无需签到）同样算了结——补签轮对它们再登录一次纯属风控暴露。"""
        self._run(PHONE_OK, (True, "今日无需签到（非签到日）", False, signin.STATUS_NO_TASK),
                  executor="exec-A:1")
        _r, calls, _s = self._run(PHONE_OK, self.OK, executor="exec-B:2")
        self.assertEqual(calls, [])


class OpenAccountIsHandedOverTest(_Base):
    """② 未了结的账号必须能被下一轮接手（补签轮的存在意义）。"""

    def test_failed_account_is_retried_by_next_run(self):
        # "账号或密码错误"= 确定性认证失败 → 本轮只试 1 次即放弃（不重试）
        failed = (False, "登录失败: 账号或密码错误", False, signin.STATUS_FAILED)
        _r, calls1, _s = self._run(PHONE_FAIL, failed, executor="exec-A:1")
        self.assertEqual(calls1, [PHONE_FAIL])
        self.assertEqual(db.claim_states_for_day(DAY)[PHONE_FAIL], db.CLAIM_STATE_FAILED,
                         "失败应落 failed 而不是 done")
        self.assertEqual(db.claim_stats(DAY)["open"], 1, "failed 属于未了结")

        _r2, calls2, _s2 = self._run(PHONE_FAIL, (True, "签到成功", False,
                                                  signin.STATUS_SUCCESS),
                                     executor="exec-B:2")
        self.assertEqual(calls2, [PHONE_FAIL], "未了结账号必须能被下一轮接手")
        self.assertEqual(db.claim_states_for_day(DAY)[PHONE_FAIL], db.CLAIM_STATE_DONE)

    def test_manual_reclaim_resigns_settled_account(self):
        """手动指定账号（--only，reclaim=True）不受"当日已了结"限制。"""
        ok = (True, "签到成功", False, signin.STATUS_SUCCESS)
        self._run(PHONE_OK, ok, executor="exec-A:1")
        _r, calls, _s = self._run(PHONE_OK, ok, executor="exec-B:2", reclaim=True)
        self.assertEqual(calls, [PHONE_OK], "手动指定账号应当照做")


class ConcurrentExecutorTest(_Base):
    """③ 别的执行体正在做时，本进程不碰；租约过期后可以接管。"""

    def test_in_flight_account_is_left_alone(self):
        with self._frozen_clock():   # 预置行必须与跑队列用同一个时钟（租约判据比时间）
            db.claim_sign_account(PHONE_BUSY, DAY, "exec-other:9")
        _r, calls, _s = self._run(PHONE_BUSY, (True, "签到成功", False,
                                               signin.STATUS_SUCCESS),
                                  executor="exec-A:1")
        self.assertEqual(calls, [], "别人在飞的账号本进程不得碰（否则同一账号两次登录）")

    def test_expired_lease_is_taken_over(self):
        """死执行体留下的记录（心跳比租约还旧）必须被接管——崩溃自愈的前提。"""
        with self._frozen_clock():
            # 心跳写在 05:00，比"现在 06:40 减去 900s 租约"（06:25）还早 → 已过期
            db.claim_sign_account(PHONE_BUSY, DAY, "exec-dead:9", now="2026-09-16 05:00:00")
        _r, calls, _s = self._run(PHONE_BUSY, (True, "签到成功", False,
                                               signin.STATUS_SUCCESS),
                                  executor="exec-A:1")
        self.assertEqual(calls, [PHONE_BUSY], "租约过期=执行体已死，应被接管（崩溃自愈）")


class NoDatabaseNoSideEffectTest(_Base):
    """④ 库未初始化（纯状态文件部署）时领取池不参与，且不留下任何副作用。"""

    def test_without_db_the_run_still_happens_and_creates_no_db(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        db._conn = None
        self.assertFalse(db.is_initialized(), "前置：库未初始化")
        _r, calls, _s = self._run(PHONE_OK, (True, "签到成功", False,
                                             signin.STATUS_SUCCESS))
        self.assertEqual(calls, [PHONE_OK], "无库时照常签到（领取池可有可无）")
        self.assertFalse(db.is_initialized(), "不得因为没有领取池就顺手开一个库")


if __name__ == "__main__":
    unittest.main(verbosity=2)
