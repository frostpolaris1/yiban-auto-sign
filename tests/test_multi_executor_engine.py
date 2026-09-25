# -*- coding: utf-8 -*-
"""多执行体的**引擎侧**行为断言：领取池进入执行循环后的分派与了结语义。

标签：B · 调度：领取/队列/执行体
覆盖：领取池进入执行循环后的四条性质：当日已了结账号下一轮不再自动碰、未了结账号必须能被下一轮接手、别的执行体在飞时不碰且租约过期可接管、库未初始化时零副作用；计划态与结果态的覆盖次序；子进程入口与
   --workers 下传剔除；监督进程退出码汇总。
对应实现：scripts/signin.py（run_queue_retry 的领取/收尾/接管路径、_write_sign_state
   的状态优先级）、yiban/engine/workers.py（run_worker_supervisor、子进程 argv
   与退出码汇总）。
关键断言：「同一账号同一天只碰一次」与「未了结必须被下一轮接手」是一对：前者是防重复登录的第一道闸，后者是补签轮存在的意义——这里一起挡等于把补签轮废掉。no_task
   也算了结（再登录一次纯属风控暴露），而 --only
   是用户主动触发可豁免。每个执行体启动都会写一遍全量计划，故计划态不得覆盖已有结果，但事实之间照旧后写覆盖。子进程入口必须是
   python -m yiban.cli sign 且 argv 不得带 --workers（否则递归拉起）。
依赖：临时 sqlite（每用例重建）+ 固定业务时钟 + 打桩 attempt_signin / 写盘 /
   sleep；拉起断言为源码与 argv 检查，不 spawn 真子进程。不发网络请求。无
   skip。

另外两条边界：别的执行体正在做（`claimed` 且租约有效）时本进程不碰；库未初始化时
领取池不参与、也不留下任何副作用（纯状态文件部署不受影响）。
"""
import contextlib
import os
import re
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

    def _run(self, phone, result, *, reclaim=False, executor="", schedule=True):
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


class WorkerRelaunchCommandTest(_Base):
    """多执行体拉起的子进程入口必须是 `python -m yiban.cli sign`。

    实现迁进 `yiban/engine/workers.py` 后，旧写法 `[sys.executable, os.path.abspath(__file__), ...]`
    会把"包内模块"当脚本跑（模块体只有定义，跑完就退）——多执行体必然坏掉，而且是
    静默的（子进程退出码 0）。同时 `--workers` 与其数值必须继续从子命令行走剔掉，
    否则子进程会再次进入监督分支、递归拉起。
    """

    def _run_supervisor(self, n, argv, slots=None):
        """跑监督进程（不改真进程）：返回 (退出码, 每个子进程记录的 cmd/env/cwd)。"""
        spawned = []

        class _FakeProc:
            """替身只实现监督进程真正会调用的接口：`poll()` 立刻返回退出码。

            监督进程用轮询而不是 `wait()`（存活期间要按周期刷心跳），故这里给
            `poll()`；返回 0 = 子进程已正常退出。
            """

            def __init__(self, cmd, env=None, cwd=None):
                spawned.append({"cmd": list(cmd), "env": dict(env or {}), "cwd": cwd})

            def poll(self):
                return 0

        with mock.patch.object(signin, "load_accounts",
                               return_value=[self._acc(PHONE_OK)]), \
                mock.patch.object(signin.subprocess, "Popen", _FakeProc), \
                mock.patch.object(signin.time, "sleep"):
            rc = signin.run_worker_supervisor(n, argv, slots=slots)
        return rc, spawned

    def test_child_entry_is_module_cli_not_file_path(self):
        rc, spawned = self._run_supervisor(2, ["--workers", "2", "--only", PHONE_OK])
        self.assertEqual(rc, 0)
        self.assertEqual(len(spawned), 2, "应拉起 n 个子进程")
        for i, rec in enumerate(spawned):
            with self.subTest(child=i):
                self.assertNotIn("scripts/signin.py", " ".join(rec["cmd"]))
                self.assertNotIn(os.path.abspath(signin.__file__), rec["cmd"],
                                 "不得再按 __file__ 路径拉起子进程")
                self.assertEqual(rec["cmd"][1:4], ["-m", "yiban.cli", "sign"],
                                 "子进程入口应为模块方式执行同一个 CLI")
                self.assertEqual(rec["cwd"], BASE, "cwd 必须是仓库根（python -m 需要）")
                self.assertIn(BASE, rec["env"]["PYTHONPATH"].split(os.pathsep),
                              "PYTHONPATH 必须含仓库根")

    def test_child_argv_drops_workers_flag_and_its_value(self):
        """`--workers 2` 与其数值都不得下传（否则子进程递归拉起执行体）。"""
        rc, spawned = self._run_supervisor(2, ["--workers", "2", "--only", PHONE_OK])
        self.assertEqual(rc, 0)
        for rec in spawned:
            tail = rec["cmd"][4:]   # [python, -m, yiban.cli, sign, *child_argv]
            self.assertNotIn("--workers", tail)
            self.assertEqual(tail, ["--only", PHONE_OK])

    def test_child_argv_drops_workers_value_not_equal_to_slot_count(self):
        """清单模式：槽位数与命令行 `--workers N` 的 N 不等时，N 也不得下传。

        网页改执行体清单只写 `YIBAN_EXECUTORS`、不回写 `YIBAN_WORKERS`，于是
        `--workers 4`（旧值）+ 3 个拉起槽位是常态。按值匹配去参数会把 "4" 留在
        argv 里 → 子进程 argparse 收到位置参数直接报错退出（exit 2）→ 每个执行体
        都零请求退出，整天静默不签到。故按 argv 序位剔除 `--workers` 与其后随值。
        """
        for flag in (["--workers", "4"], ["--workers=4"]):
            with self.subTest(flag=flag):
                rc, spawned = self._run_supervisor(
                    3, [*flag, "--only", PHONE_OK], slots=[0, 1, 3])
                self.assertEqual(rc, 0)
                self.assertEqual(len(spawned), 3, "应按 3 个槽位拉起")
                for rec in spawned:
                    tail = rec["cmd"][4:]
                    self.assertNotIn("--workers", tail)
                    self.assertNotIn("4", tail, "命令行 N 不得下传（子进程会当位置参数）")
                    self.assertEqual(tail, ["--only", PHONE_OK])

    def test_argv_position_does_not_eat_lookalike_values(self):
        """只吃 `--workers` 的后随值：其余参数里同名的值照旧下传。

        这正是按值匹配的另一个坑——`--workers 2` 之外任何等于 "2" 的参数
        （如某账号手机号尾部）都会被误删，子进程收到的参数就少了一个。
        """
        rc, spawned = self._run_supervisor(2, ["--workers", "2", "--only", "2"])
        self.assertEqual(rc, 0)
        for rec in spawned:
            self.assertEqual(rec["cmd"][4:], ["--only", "2"])

    def test_source_no_longer_spawns_by_file_path(self):
        """源级断言：拉起子进程的命令行不得再用 `__file__` 作入口。

        只看 `cmd = [...]` 那一行：`__file__` 在本模块里另有正当用途（上溯仓库根），
        故不能对整个文件做"不得出现 __file__"的粗断言。
        """
        with open(os.path.join(BASE, "yiban", "engine", "workers.py"), encoding="utf-8") as f:
            src = f.read()
        m = re.search(r"(?m)^\s*cmd = .*$", src)
        self.assertIsNotNone(m, "workers.py 里应有子进程命令行构造")
        self.assertNotIn("__file__", m.group(0), "不得再按 __file__ 路径拉起子进程")
        self.assertIn('"-m", "yiban.cli"', m.group(0))
        self.assertIn('a == "--workers"', src, "命令行仍须按序位剔除 --workers")


class SupervisorExitCodeAggregationTest(unittest.TestCase):
    """监督进程的退出码汇总：补签轮判定的「需要补跑」(10) 必须原样透出。

    被归一成 0 后，宿主 run.sh 会把"首轮有账号需要补跑"读成"一切正常"，
    补签轮不触发、当天失败的账号不再重试。10 还要先于 1/3/2 判定——否则会被
    2（跳过/窗口外）或 3（锁忙）掩盖成别的语义。
    """

    @staticmethod
    def _supervise(codes):
        """跑监督进程（不起真子进程）：替身的 `poll()` 按序返回给定退出码。"""
        from yiban.engine import workers
        pending = list(codes)

        class _FakeProc:
            def __init__(self, cmd, env=None, cwd=None):
                pass

            def poll(self):
                return pending.pop(0)

        acc = SimpleNamespace(phone="13800000000", user_paused=False)
        with mock.patch.object(workers.cli_support, "_acquire_run_lock", return_value=None), \
                mock.patch.object(workers.accounts_mod, "load_accounts", return_value=[acc]), \
                mock.patch.object(workers.subprocess, "Popen", _FakeProc), \
                mock.patch.object(workers.time, "sleep"), \
                mock.patch.object(workers.state_io, "mark_worker_started", lambda *a, **k: None), \
                mock.patch.object(workers.state_io, "mark_worker_finished", lambda *a, **k: None):
            return workers.run_worker_supervisor(len(codes), ["--workers", str(len(codes))])

    def test_second_run_code_is_passed_through(self):
        from yiban.engine import runner, workers
        self.assertEqual(workers._SECOND_RUN_CHECK_NEED, runner.SECOND_RUN_CHECK_NEED,
                         "两处补签判定退出码必须同值，否则子进程的 10 会被读成别的码")
        for codes, want in (
            ([10, 0], 10),
            ([0, 10], 10),
            ([10, 2], 10),   # 不被「跳过/窗口外」掩盖
            ([10, 3], 10),   # 不被「锁忙」掩盖
            ([10, 1], 10),   # 也不被「真失败」掩盖（调用方需要知道要补跑）
        ):
            with self.subTest(codes=codes):
                self.assertEqual(self._supervise(codes), want)

    def test_existing_priority_unchanged(self):
        for codes, want in (([1, 2], 1), ([2, 3], 3), ([2, 0], 2), ([0, 0], 0), ([0, 3], 3)):
            with self.subTest(codes=codes):
                self.assertEqual(self._supervise(codes), want)


if __name__ == "__main__":
    unittest.main(verbosity=2)
