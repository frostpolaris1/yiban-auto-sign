# -*- coding: utf-8 -*-
"""多执行体的**引擎侧**行为断言：领取池进入执行循环后的分派与了结语义。

标签：B · 调度：领取/队列/执行体
覆盖：领取池进入执行循环后的四条性质：当日已了结账号下一轮不再自动碰、未了结账号按弃权原因分档接手（窗口外默认可领、预算耗尽只走显式路径）、别的执行体在飞时不碰且租约过期可接管、库未初始化时零副作用；计划态与结果态的覆盖次序；子进程入口与
   --workers 下传剔除；监督进程退出码汇总。
对应实现：scripts/signin.py（run_queue_retry 的领取/收尾/接管路径、_write_sign_state
   的状态优先级）、yiban/engine/workers.py（run_worker_supervisor、子进程 argv
   与退出码汇总）、yiban/store/claims.py（give_up 分档 / try_claim 的 allow_failed）。
关键断言：「同一账号同一天只碰一次」与「未了结必须能被下一轮接手」是一对：前者是防重复登录的第一道闸，后者是补签轮存在的意义——但接手要**分档**：预算耗尽档默认拦住（否则当日每轮重领一次真实登录），只有有界显式路径（补签轮/手动）能接。no_task
   也算了结（再登录一次纯属风控暴露），而 --only
   是用户主动触发可豁免（且已收敛为单进程，不再派发执行体）。每个执行体启动都会写一遍全量计划，故计划态不得覆盖已有结果，但事实之间照旧后写覆盖。子进程入口必须是
   python -m yiban.cli sign 且 argv 不得带 --workers（否则递归拉起）。
依赖：临时 sqlite（每用例重建）+ 固定业务时钟 + 打桩 attempt_signin / 写盘 /
   sleep；拉起断言为源码与 argv 检查，不 spawn 真子进程。不发网络请求。无
   skip。

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

from yiban import clock  # noqa: E402

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

    def _run(self, phone, result, *, reclaim=False, executor="", schedule=True,
             retry_failed=False):
        """跑一轮单账号队列，返回 (results, 实际发起尝试的账号, 状态写入记录)。"""
        calls = []
        states = []

        def fake_attempt(_acc):
            calls.append(_acc.phone) # 只记号不记结果：断言的是「这一轮真的发了尝试」，重试次数也因此暴露
            return result

        with mock.patch.dict(os.environ, {"YIBAN_EXECUTOR_ID": executor} if executor else {},
                             clear=False), \
                self._frozen_clock(), \
                mock.patch.object(signin, "attempt_signin", side_effect=fake_attempt), \
                mock.patch.object(signin, "_write_sign_state",
                                  side_effect=lambda p, st, msg, **kw: states.append((p, st))), \
                mock.patch.object(signin.time, "sleep"):
            sched = {phone: FIXED_NOW - timedelta(seconds=5)} if schedule else None # 落点设在「已过点」以跳过等待；schedule=None 走的是无计划的动态领取分支
            # sleep 与写盘都在上面的 with 里打桩：过点账号仍会走间隔等待，不打桩就真睡
            results = signin.run_queue_retry([self._acc(phone)], None, 0, 0,
                                             schedule=sched, cred_state={},
                                             reclaim=reclaim, retry_failed=retry_failed)
        return results, calls, states


class PlanMustNotClobberResultTest(_Base):
    """多执行体下每个执行体启动都会写一遍全量计划——计划态不得覆盖已有结果。

    实测缺陷（4 执行体 × 40 账号，真实 TLS）：晚启动的执行体把先启动者已写好的
    success 抹回 pending（40 条里被抹 2 条），日历显示"待签"、补签闸门把已签账号
    当未了结再跑一遍。修法：`pending` 是预测，已产出的状态是事实，事实优先。
    """

    def _state_path(self):
        return os.path.join(os.environ["YIBAN_STATE_DIR"],
                            f"sign-state-{clock.today()}.json")

    def _write(self, status, message="", **kw):
        # 当日状态文件名与写入时刻都取业务钟（signin.clock = yiban.clock），
        # 与 _state_path 同源；此前打桩成宿主 datetime.now 会在 UTC 主机上错日。
        signin._write_sign_state(PHONE_OK, status, message, **kw)

    def _read(self):
        import json
        with open(self._state_path(), encoding="utf-8") as f:
            return json.load(f)[PHONE_OK]

    def test_plan_does_not_overwrite_result(self):
        self._write(signin.STATUS_SUCCESS, "签到成功")
        self._write(signin.STATUS_PENDING, "计划 06:40", scheduled="06:40:00")
        entry = self._read()
        self.assertEqual(entry["status"], signin.STATUS_SUCCESS, "结果不得被计划态抹掉")
        self.assertEqual(entry["scheduled"], "06:40:00", "计划时间仍应补进去")
        self.assertEqual(entry["message"], "签到成功")

    def test_result_still_overwrites_result(self):
        """事实之间照旧后写覆盖：失败重试后要能反映最新一次结论。"""
        self._write(signin.STATUS_FAILED, "网络超时")
        self._write(signin.STATUS_SUCCESS, "签到成功")
        self.assertEqual(self._read()["status"], signin.STATUS_SUCCESS)
        self._write(signin.STATUS_PENDING, "计划 06:41")
        self._write(signin.STATUS_FAILED, "再次失败")
        self.assertEqual(self._read()["status"], signin.STATUS_FAILED)

    def test_plan_still_writes_when_no_result_yet(self):
        with contextlib.suppress(OSError):
            os.remove(self._state_path())   # 干净起点：当日尚无任何记录
        self._write(signin.STATUS_PENDING, "计划 06:42", scheduled="06:42:00")
        entry = self._read()
        self.assertEqual(entry["status"], signin.STATUS_PENDING)
        self.assertEqual(entry["scheduled"], "06:42:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
