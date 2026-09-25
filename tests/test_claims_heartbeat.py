# -*- coding: utf-8 -*-
"""在领账号的心跳与轮末收尸：`claims.touch` 的生产调用者、周期、降级、库信号分档。

标签：B · 调度：领取/队列/执行体
覆盖：`claims.HEARTBEAT_SEC` 与租约的倍数关系；`round._ClaimHeartbeat` 的周期节流、
   `force`、分段睡眠、touch 失败降级（不外呼、不中断签到主流程）；`run_queue_retry`
   对在领账号**真的**周期性续租（带领取时的 epoch）；`claims.reap_unreported` 的轮末
   收尸语义（置 failed + 立刻放开租约，别人可马上接手）与它的接线；`db.pool_db_declared`
   的只读判据（不建库）与 `_claim` 的两档分叉——部署未配库（照旧放行、不建库）vs
   配了库但当前不可用（拒跑 + 告警）。
对应实现：yiban/store/claims.py（HEARTBEAT_SEC / touch / reap_unreported）、
   yiban/store/connection.py（pool_db_declared）、yiban/engine/round.py（_ClaimHeartbeat、
   _claim、_settle_claims）。
关键断言：**库信号分叉的判据是"部署声明"而不是"磁盘上有文件"**——默认路径恰是
   `yiban.db`，用文件存在性会把开发机/旧部署遗留的库误判成池部署；
   **心跳必须由执行侧周期性写入**——`touch` 从没有生产调用者时，任何超过
   900s 租约的在领账号都会被别的执行体按"租约过期"接管，同一账号当天被真实登录两次
   （第一红线）；故本文件的接线用例断言"跑一轮队列 ⇒ touch 真的被调到，且带的是该行
   当前 epoch"，而不只是断言存在一个 `beat()` 函数。库信号分叉的判别力在"两种形态
   给出相反的结论"：同一个 `is_initialized()=False`，磁盘上有池库 ⇒ 拒跑；没有 ⇒ 放行
   且**不得顺手建库**。
依赖：临时 sqlite（每用例重建）+ 打桩 `attempt_signin` / `alerts._collect_admin_mail`；
   无网络请求、无真子进程。整文件在本机执行，无 skip。
"""
import contextlib
import datetime
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402

from yiban import clock  # noqa: E402
from yiban.engine import alerts  # noqa: E402
from yiban.engine import round as round_mod  # noqa: E402
from yiban.store import claims  # noqa: E402

TEST_KEY = "a" * 64
DAY = "2026-09-25"
P1 = "13800138000"
P2 = "13800138001"
OWNER_A = "worker-0@testhost:111:090000"
OWNER_B = "worker-1@testhost:222:090000"


def _ts(**kw):
    return (clock.now() + datetime.timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


def _today():
    """跑一轮队列写行用的是业务钟的"今天"（不是固定的 DAY 常量）。"""
    return clock.now().strftime("%Y-%m-%d")


class _FakeHeartbeatEnv:
    """假业务钟 + 假睡眠：心跳的周期判据只依赖读钟，与真实等待解耦。

    `step` 是每次**读钟**自身推进的秒数：接线用例取正数，好让真实调用序列里每个
    心跳点都判定为"周期已到"；周期/节流的单测取 0，只由用例显式推时刻。
    """

    def __init__(self, step=0.0):
        self.t = datetime.datetime(2026, 9, 25, 7, 0, 0)
        self.step = step
        self.sleeps = []

    def now(self):
        cur = self.t
        self.t += datetime.timedelta(seconds=self.step)
        return cur

    def sleep(self, sec):
        self.sleeps.append(sec)
        self.t += datetime.timedelta(seconds=max(0.0, float(sec)))


class _DbBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-heartbeat-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
        })

    @classmethod
    def tearDownClass(cls):
        cls._close()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    @staticmethod
    def _close():
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def setUp(self):
        self._close()
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def tearDown(self):
        self._close()

    def _acc(self, phone):
        return SimpleNamespace(phone=phone, user_paused=False, owner="u@" + phone,
                               password="p", account_id=0)

    def _row(self, phone, day=DAY):
        return dict(db.get_conn().execute(
            "SELECT * FROM sign_claims WHERE phone=? AND day=?", (phone, day)).fetchone())

    def _run(self, accounts, **kw):
        """跑一轮手动队列（schedule 空）并返回 (结果, attempt_signin 收到的手机号)。"""
        calls = []
        with mock.patch.object(round_mod.attempts_mod, "attempt_signin",
                               side_effect=lambda a: calls.append(a.phone) or (
                                   True, "签到成功", False, "success")):
            out = round_mod.run_queue_retry(
                accounts, "", 0, kw.pop("gap_max", 0), schedule=None,
                cred_state={}, **kw)
        return out, calls


# ---------------------------------------------------------------------------
# 周期与降级
# ---------------------------------------------------------------------------
class HeartbeatPeriodTest(unittest.TestCase):
    """周期必须**显著小于**租约：等于或接近租约时，续租来不及挡住接管窗口。"""

    def test_period_is_far_below_lease(self):
        self.assertLess(claims.HEARTBEAT_SEC, claims.LEASE_SECONDS)
        self.assertLessEqual(claims.HEARTBEAT_SEC, 300,
                             "周期超过 5 分钟就不再'显著小于'900s 租约")
        self.assertGreaterEqual(claims.LEASE_SECONDS // claims.HEARTBEAT_SEC, 3,
                                "至少要有 3 个心跳周期塞进租约，才容得下一次丢拍")


class _HeartbeatUnitBase(unittest.TestCase):
    def setUp(self):
        self.days = {}
        self.epochs = {}
        self.touched = []
        self.env = _FakeHeartbeatEnv()

    def _touch(self, phone, day, owner, epoch=None):
        self.touched.append((phone, day, owner, epoch))
        return True

    def _hb(self, **kw):
        kw.setdefault("now", self.env.now)
        kw.setdefault("sleep", self.env.sleep)
        kw.setdefault("touch", self._touch)
        return round_mod._ClaimHeartbeat("owner-1", self.days, self.epochs,
                                        period=claims.HEARTBEAT_SEC, **kw)


class ClaimHeartbeatUnitTest(_HeartbeatUnitBase):
    def test_first_beat_touches_then_throttles_until_period(self):
        hb = self._hb()
        self.days[P1] = DAY
        self.epochs[P1] = 3
        self.assertEqual(hb.beat(), 1, "认领后第一次心跳必须真的写一次")
        self.assertEqual(self.touched, [(P1, DAY, "owner-1", 3)],
                         "续租必须带上领取时拿到的 epoch（否则被接管者的续租会拉长租约）")
        self.env.t += datetime.timedelta(seconds=claims.HEARTBEAT_SEC / 2)
        self.assertEqual(hb.beat(), 0, "周期未到不得重复写（每账号每周期最多一次）")
        self.env.t += datetime.timedelta(seconds=claims.HEARTBEAT_SEC / 2)
        self.assertEqual(hb.beat(), 1, "周期到了必须再写一次")

    def test_force_beat_is_not_throttled(self):
        hb = self._hb()
        self.days[P1] = DAY
        self.assertEqual(hb.beat(force=True), 1)
        self.assertEqual(hb.beat(force=True), 1, "长操作前后必须能强制续租")

    def test_sleep_renews_across_long_waits(self):
        """单次长等跨越周期时必须分段续租——否则在领账号先过期、被别人接管。"""
        hb = self._hb()
        self.days[P1] = DAY
        hb.beat()
        before = len(self.touched)
        hb.sleep(claims.HEARTBEAT_SEC * 4)
        self.assertGreaterEqual(len(self.touched) - before, 3,
                                f"每段≤周期的一半，长等期间必须多次续租：{self.touched}")
        self.assertLess(max(self.env.sleeps), claims.HEARTBEAT_SEC,
                        "分段粒度必须小于周期本身")

    def test_touch_failure_degrades_without_raising(self):
        """心跳失败不得中断签到主流程：降级记录，不外呼、不抛。"""

        def boom(*a, **kw):
            raise RuntimeError("库抖动")

        hb = self._hb(touch=boom)
        self.days[P1] = DAY
        with mock.patch.object(round_mod.logger, "debug") as dbg:
            self.assertEqual(hb.beat(), 0, "失败按 0 条成功返回，不抛")
        self.assertTrue(dbg.called, "降级必须留痕（debug）")


# ---------------------------------------------------------------------------
# 接线：run_queue_retry 真的有生产调用者
# ---------------------------------------------------------------------------
class HeartbeatWiringTest(_DbBase):
    """`touch` 的生产调用者就是这一轮队列：跑一轮 ⇒ 在领账号被周期性续租。"""

    def test_round_renews_claimed_rows_with_current_epoch(self):
        env = _FakeHeartbeatEnv(step=1.0)   # 每次读钟 +1s ⇒ period=0.01 时每点必到
        touched = []
        real_touch = db.claim_touch

        def spy_touch(phone, day, owner, lease_sec=claims.LEASE_SECONDS, now=None,
                      epoch=None):
            # 必须在**续租那一刻**读行：轮末收尾会把行置 done，事后再读就看不到
            # "续租的是在领行"这个事实了。
            row = self._row(phone, day=day)
            touched.append((phone, day, owner, epoch, row["state"], row["epoch"]))
            return real_touch(phone, day, owner, lease_sec=lease_sec, now=now,
                              epoch=epoch)

        real_hb = round_mod._ClaimHeartbeat

        def tiny_period(owner, days, epochs, **kw):
            kw.update(period=0.01, now=env.now, sleep=env.sleep)
            return real_hb(owner, days, epochs, **kw)

        with mock.patch.object(db, "claim_touch", spy_touch), \
                mock.patch.object(round_mod, "_ClaimHeartbeat", tiny_period):
            out, calls = self._run([self._acc(P1), self._acc(P2)])
        self.assertEqual(sorted(calls), [P1, P2], "前置：两个账号各走了一次尝试")
        self.assertEqual(sorted(out), [P1, P2])
        self.assertEqual({t[0] for t in touched}, {P1, P2},
                         f"在领账号必须都被周期续租过（实际 {touched}）")
        for _ph, day, owner, epoch, state, row_epoch in touched:
            self.assertEqual(day, _today(), "心跳写的是该行所属的业务日")
            self.assertEqual(state, "claimed", "续租的是在领行（轮末才收尾）")
            self.assertIsNotNone(epoch, "续租必须带领取时的 token（不带 token 的写不校验代）")
            self.assertEqual(epoch, row_epoch,
                             "续租必须带该行**当前**epoch（重入后旧代已作废）")
            self.assertIn(f":{os.getpid()}:", owner,
                          "续租必须用本进程的运行时身份（同槽位的另一代可分辨）")

    def test_touch_failure_does_not_break_the_round(self):
        def boom(*a, **kw):
            raise RuntimeError("库抖动")

        with mock.patch.object(db, "claim_touch", boom):
            out, calls = self._run([self._acc(P1)])
        self.assertEqual(calls, [P1], "心跳失败不得影响签到主流程（照常发请求）")
        self.assertEqual(self._row(P1, day=_today())["state"], "done",
                         "轮末收尾照常完成")
        self.assertIn(P1, out)

    def test_no_db_deployment_never_touches_and_creates_no_db(self):
        """未配库的纯状态文件形态：在领集合恒空 ⇒ 心跳与领取池都不碰库。"""
        self._close()
        with mock.patch.dict(os.environ, {"YIBAN_DB_FILE": ""}, clear=False):
            self.assertFalse(db.pool_db_declared(), "前置：部署没声明库路径")
            with mock.patch.object(db, "claim_touch") as touch:
                _out, calls = self._run([self._acc(P1)])
        self.assertEqual(calls, [P1], "无库时照常签到")
        touch.assert_not_called()
        self.assertIsNone(db._conn, "不得因为没有领取池就顺手开一个库")


# ---------------------------------------------------------------------------
# 轮末收尸（本轮自己名下仍 claimed 的行）
# ---------------------------------------------------------------------------
class RoundEndReapTest(_DbBase):
    def test_unreported_claim_is_explicitly_given_up_and_immediately_reclaimable(self):
        _ok, e1 = db.claim_sign_account(P1, DAY, OWNER_A)
        _ok, e2 = db.claim_sign_account(P2, DAY, OWNER_A)
        reaped = claims.reap_unreported(OWNER_A, {P1: (DAY, e1), P2: (DAY, e2)},
                                        reported={P1})
        self.assertEqual(reaped, [P2], "只收自己名下、本轮未产生结论的那些行")
        self.assertEqual(self._row(P1)["state"], "claimed", "已有结论的行不得被收尸改写")
        row_b = self._row(P2)
        self.assertEqual(row_b["state"], "failed", "收尸是'当日仍未了结'（failed），不是 done")
        self.assertLessEqual(row_b["heartbeat_at"], _ts(seconds=-claims.LEASE_SECONDS),
                             "收尸必须放开租约：否则要等满 900s 才有人能接手")
        self.assertTrue(db.claim_sign_account(P2, DAY, OWNER_B)[0],
                        "收尸后别的执行体必须能**立刻**接手（不留给下一轮误判）")
        self.assertEqual((self._row(P1)["owner"], self._row(P1)["state"]),
                         (OWNER_A, "claimed"), "别人的在飞行不得被收尸动到")

    def test_reap_skips_rows_taken_over_by_others(self):
        _ok, e1 = db.claim_sign_account(P1, DAY, OWNER_A)
        db.claim_sign_account(P1, DAY, OWNER_B, lease_sec=0)   # 被别人接管
        self.assertEqual(claims.reap_unreported(OWNER_A, {P1: (DAY, e1)}, reported=set()),
                         [], "已被接管的行不属于原主的收尸范围（fencing 已挡）")
        self.assertEqual(self._row(P1)["owner"], OWNER_B)

    def test_round_end_reap_is_wired(self):
        """接线断言：轮末必须调用收尸（MF-47③ 的另一半——'无调用者'就是缺陷本身）。"""
        with mock.patch.object(round_mod.claims_mod, "reap_unreported",
                               return_value=[]) as reap:
            self._run([self._acc(P1)])
        self.assertTrue(reap.called, "轮末必须对自己的在领行显式收尸")
        owner, claimed, reported = reap.call_args[0]
        self.assertIn(f":{os.getpid()}:", owner, "收尸身份必须是本进程的运行时身份")
        self.assertIn(P1, claimed, "收尸范围必须是本轮领到的账号")
        self.assertIn(P1, reported, "已产生结论的账号不计入收尸")


# ---------------------------------------------------------------------------
# 库信号分档：未配库 vs 配了库但不可用
# ---------------------------------------------------------------------------
class PoolDbDeclaredProbeTest(_DbBase):
    """`pool_db_declared` 的判据是**部署声明的路径**，且只读（不建库不建表不建连接）。"""

    def test_empty_declaration_is_not_configured(self):
        with mock.patch.dict(os.environ, {"YIBAN_DB_FILE": ""}, clear=False):
            self.assertFalse(db.pool_db_declared())

    def test_declared_path_counts_as_configured_even_before_it_exists(self):
        # 声明了路径就说明这个部署靠领取池做互斥：库还没建/连不上时也必须按"配了库"处置。
        target = os.path.join(self.tmp, "declared-but-absent.db")
        with mock.patch.dict(os.environ, {"YIBAN_DB_FILE": target}, clear=False):
            self.assertTrue(db.pool_db_declared())
        self.assertFalse(os.path.exists(target), "判据不得顺手建库文件")

    def test_cache_is_keyed_by_resolved_path(self):
        """判据按解析出的路径缓存：一次"未声明"不得污染另一条路径的结论。"""
        with mock.patch.dict(os.environ, {"YIBAN_DB_FILE": ""}, clear=False):
            self.assertFalse(db.pool_db_declared())
        self.assertTrue(db.pool_db_declared(), "换到声明了的路径必须重算出'有'")


class ClaimSignalSplitTest(_DbBase):
    """同一个"库未初始化"，两种形态必须给出相反结论。"""

    def test_configured_but_unavailable_refuses_and_alerts(self):
        self._close()   # 部署声明了库路径，但本进程连不上/未初始化
        self.assertTrue(db.pool_db_declared(), "前置：部署声明了库路径")
        self.assertFalse(db.is_initialized())
        with mock.patch.object(alerts, "_collect_admin_mail") as collect, \
                mock.patch.object(claims, "_pool_down_notified", False):
            out, calls = self._run([self._acc(P1)])
        self.assertEqual(calls, [], "配了库但不可用必须拒跑，不得静默放行")
        self.assertEqual(out, {})
        self.assertTrue(collect.called, "拒跑必须并入当日告警汇总（不能静默）")

    def test_unconfigured_runs_normally(self):
        self._close()
        with mock.patch.dict(os.environ, {"YIBAN_DB_FILE": ""}, clear=False):
            out, calls = self._run([self._acc(P1)])
        self.assertEqual(calls, [P1], "部署未配库（纯状态文件）必须保持放行")
        self.assertIn(P1, out)
        self.assertIsNone(db._conn, "放行不等于可以顺手建库")


if __name__ == "__main__":
    unittest.main(verbosity=2)
