# -*- coding: utf-8 -*-
"""`yiban/engine/planner.py`（双粒度分片 Planner）与 `schedule.capacity_accounts_v3` 的契约用例。

标签：A · 调度：计划与分片
覆盖：计划层的全部形状契约：确定性与可重放、`uniform`/`normal` 与执行体数 K
   解耦、`front`（默认）提前铺完与安全铺点间距、落点有界、分层零方差与微槽/相位两级自由度、
   跨天重排、小 N 前载、自暂停零占位、重复号折叠、自选硬约束与双层溢出（先到先得）、
   分布三态与正态密度/削峰、压缩模式与元数据、plan_stats 摘要与直方图基点、
   capacity_accounts_v3 取值、幂等落库与降级信号。
对应实现：yiban/engine/planner.py（build_plan、write_plan、has_plan、plan_stats）、yiban/engine/schedule.py（capacity_accounts_v3、planner_config）、yiban/window.py、yiban/store/queue_store
   与 clock_meta（计划行与元数据的落库处）。
关键断言：`uniform`/`normal` 下计划层与执行体数无关：改 executors 只改 owner，一个 run_at
   都不许动——否则换一台机器重排会把当天已跑完的账号再排一遍。`front`（默认）**有意**依赖
   K：安全铺点速率随并发数放大，铺点分片数 = 首个 K 能及时消化的人数段。分片归属必须由H(phone‖day)
   决定并与 hrw
   同口径（用例自算哈希核对）。自选片与直方图桶键的基点一律取「窗口起点」而非收缩后的有效窗口起点——网页片号与计划桶号必须同号，否则用户选的片和实际落的片错位。库不可用时
   has_plan 读作「当日无计划」让执行体降级动态领取，而 write_plan
   必须抛异常让调用方显式降级，不得静默半途而废。容量公式不得回退到忽略令牌桶封顶的原稿口径（会高估
   2.3 倍）。
依赖：临时 sqlite 库（表由 v18/v19 迁移建齐，仅落库类用例）+
   环境变量快照还原；纯计算类用例不碰库。不发网络请求。整文件在本机执行，无
   skip。

窗口取 06:00~07:20、前后各裁 300s ⇒ 有效窗口 70 分钟 = 4200s = 70 个 1 分钟分片
（每片 60 个 1 秒微槽），与容量核算口径同数。自选片 `slot_min` 仍是"相对窗口起点"的
5 分钟格（与 web `_pref_slots`、`schedule._slot_to_bi` 同源），故 `slot_min=35` 对应
06:35~06:40。
"""
import contextlib
import datetime
import hashlib
import math
import os
import shutil
import tempfile
import unittest
from unittest import mock

from yiban import window
from yiban.engine import planner, schedule
from yiban.store import clock_meta, queue_store

#: 计划用例共用的窗口配置（有效窗口 06:05~07:15，4200s / 70 分片 / 4200 微槽）
WINDOW_ENV = {
    "YIBAN_SIGN_START": "06:00",
    "YIBAN_SIGN_END": "07:20",
    "YIBAN_WINDOW_EDGE_FRONT_SEC": "300",
    "YIBAN_WINDOW_EDGE_BACK_SEC": "300",
}
#: 缓冲过大：窗口仅 10 分钟、前后各 300s ⇒ `window.bounds` **保留窗口**、把缓冲等比
#: 收缩为各 60s（有效窗口 07:01~07:09）。此时"窗口起点 + 片偏移"必须以窗口起点 420 分
#: （07:00）为基点，且只有偏移 0 与 5 两个片可用。
CLAMPED_WINDOW_ENV = {
    "YIBAN_SIGN_START": "07:00",
    "YIBAN_SIGN_END": "07:10",
    "YIBAN_WINDOW_EDGE_FRONT_SEC": "300",
    "YIBAN_WINDOW_EDGE_BACK_SEC": "300",
}
#: 会被用例改动、必须逐个还原的环境键（含旧 YIBAN_SIGN_MODE 与出口桶速率键）
_TOUCHED = (
    "YIBAN_SIGN_START", "YIBAN_SIGN_END", "YIBAN_WINDOW_EDGE_FRONT_SEC",
    "YIBAN_WINDOW_EDGE_BACK_SEC", "YIBAN_WINDOW_EDGE_SEC", "YIBAN_SIGN_MODE",
    "YIBAN_SIGN_ORDER", "YIBAN_SIGN_DIST", "YIBAN_EGRESS_RATE",
    "YIBAN_ALLOW_TIME_PREF", "YIBAN_SCHEDULE_SIGMA_MIN_PCT",
    "YIBAN_SCHEDULE_SIGMA_MAX_PCT", "YIBAN_SCHEDULE_MU_MIN_PCT",
    "YIBAN_SCHEDULE_MU_MAX_PCT",
    # `front` 的安全铺点速率吃单账号周期 avg+gap：两者泄漏会让 front 用例的
    # `used_slices` 与期望值错位（单跑绿、全量红），必须同批还原。
    "YIBAN_AVG_ATTEMPT_SEC", "YIBAN_ACCOUNT_GAP_MAX",
)
DAY = "2026-09-23"
NEXT_DAY = "2026-09-24"
EXECUTORS = ["worker-0@hostA", "worker-1@hostA", "worker-2@hostA"]
#: 计划落库用的库（write_plan / has_plan / plan_stats 的元数据）需要一把加密键
TEST_KEY = "a" * 64


def _phone(i):
    return f"138{i:08d}"


class _Acc:
    """最小账号替身：Planner 只读 `.phone` 与 `.user_paused`（与 `schedule.build_schedule` 同）。"""

    __slots__ = ("phone", "user_paused")

    def __init__(self, phone, user_paused=False):
        self.phone = phone
        self.user_paused = user_paused


def _accounts(n, start=0, paused=()):
    return [_Acc(_phone(i), user_paused=(i in paused)) for i in range(start, start + n)]


def _stamp(i):
    """自选片用例的 updated_at：越小的 i 越早（先到先得按它排序）。"""
    return f"2026-09-23 00:{i // 60:02d}:{i % 60:02d}"


def _prefs(phones, slot_min):
    return {p: {"slot_min": slot_min, "updated_at": _stamp(i)} for i, p in enumerate(phones)}


def _h(*parts):
    """与 `hrw` 同一口径的 blake2b（用例自算，用于核对"按 H(phone‖day) 选片"这条契约）。"""
    raw = "\x1f".join(str(p) for p in parts).encode("utf-8") # 用例独立重算一遍哈希口径：与实现共用常量的话，实现偷换编码就测不出来
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big")


def _row(phone, run_at, owner="worker-0@hostA", vshard=0):
    """手造计划行（只喂 `plan_stats` 这类只读摘要的用例）。"""
    return {"phone": phone, "day": DAY, "vshard": vshard, "owner": owner,
            "run_at": run_at, "priority": 5, "state": "pending", "epoch": 0}


def _dt(run_at):
    return datetime.datetime.strptime(run_at, "%Y-%m-%d %H:%M:%S.%f")


def _off_sec(run_at, eff_lo_min):
    """落点相对有效窗口起点的秒数。"""
    t = _dt(run_at)
    base = t.replace(hour=0, minute=0, second=0, microsecond=0)
    return (t - base).total_seconds() - eff_lo_min * 60


def _slice_of(run_at, eff_lo_min):
    return int(_off_sec(run_at, eff_lo_min) // 60)


def _slot_phase(run_at, eff_lo_min, slot_sec=1.0):
    """落点拆回 (微槽序号, 槽内相位秒)。"""
    rem = _off_sec(run_at, eff_lo_min) - _slice_of(run_at, eff_lo_min) * 60
    j = int(rem / slot_sec)
    return j, rem - j * slot_sec


class _Base(unittest.TestCase):
    """纯函数用例：只动环境（窗口/三模式/桶速率），不碰库。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _TOUCHED} # 先存后清：漏还原会让下一个文件读到本用例的窗口（单跑绿、全量红）
        for k in _TOUCHED:
            os.environ.pop(k, None)
        os.environ.update(WINDOW_ENV)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ---- 共用入口 ----
    def cfg(self):
        return schedule.planner_config()

    def bounds(self):
        return window.bounds(self.cfg())

    # executors 默认值故意固定：改它只该影响 owner，改 run_at 的用例走的是上面的契约
    def plan(self, accounts, day=DAY, executors=EXECUTORS, **kw):
        return planner.build_plan(accounts, day, executors, **kw)

    def eff_lo_min(self):
        return self.bounds().lo_min


class DeterminismTest(_Base):
    def test_same_inputs_give_field_by_field_identical_rows(self):
        """同 (accounts, day, executors, order, dist, 固定 prefs) 两次计划逐字段相等（含亚秒相位）。"""
        accs = _accounts(120)
        prefs = _prefs([_phone(i) for i in range(0, 40, 7)], 35)
        kw = {"order": "sequence", "dist": "uniform", "prefs": prefs}
        first = self.plan(accs, **kw)
        second = self.plan(accs, **kw)
        self.assertEqual(len(first), 120)
        self.assertEqual(first, second)

    def test_rows_carry_contract_fields(self):
        rows = self.plan(_accounts(5))
        self.assertEqual(
            set(rows[0]),
            {"phone", "day", "vshard", "owner", "run_at", "priority", "state", "epoch"},
        )
        for r in rows:
            self.assertEqual(r["day"], DAY)
            self.assertEqual(r["state"], "pending")
            self.assertEqual(r["priority"], 5)
            self.assertEqual(r["epoch"], 0)
            self.assertRegex(r["run_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$")

    def test_day_falls_back_to_now(self):
        """`now` 的用途：`day` 缺省时取它的日期（默认 clock.now()）。"""
        rows = self.plan(_accounts(3), day=None,
                         now=datetime.datetime(2026, 9, 23, 6, 0))
        self.assertEqual({r["day"] for r in rows}, {DAY})


class IndependenceOfKTest(_Base):
    def test_run_at_identical_owner_changes_with_executors(self):
        """`uniform`/`normal` 与 K 无关：改 executors 只改 owner，不改任何 run_at。

        `uniform`/`normal` 的计划层与执行体数必须解耦——否则换一台机器重排会把当天已跑完
        的账号再排一遍。此处**显式**取 uniform：`front`（默认）有意依赖 K（见
        `FrontFillTest.test_front_run_at_depends_on_executor_count`），那条依赖不是本用例
        的判据。
        """
        accs = _accounts(200)
        one = self.plan(accs, executors=["worker-0@hostA"], dist="uniform")
        three = self.plan(accs, executors=EXECUTORS, dist="uniform")
        self.assertEqual([r["run_at"] for r in one], [r["run_at"] for r in three])
        self.assertEqual([r["vshard"] for r in one], [r["vshard"] for r in three])
        self.assertEqual({r["owner"] for r in one}, {"worker-0@hostA"})
        self.assertNotEqual({r["owner"] for r in one}, {r["owner"] for r in three})
        self.assertTrue({r["owner"] for r in three} <= set(EXECUTORS))

    def test_owner_is_empty_without_executors(self):
        rows = self.plan(_accounts(5), executors=[])
        self.assertEqual({r["owner"] for r in rows}, {""})


class WindowBoundsTest(_Base):
    def _assert_in_window(self, rows):
        win = self.bounds()
        lo = win.lo_min * 60
        hi = win.hi_min * 60
        base = datetime.datetime.strptime(DAY, "%Y-%m-%d")
        for r in rows:
            off = (datetime.datetime.strptime(r["run_at"], "%Y-%m-%d %H:%M:%S.%f") - base).total_seconds()
            self.assertGreaterEqual(off, lo, r)
            self.assertLess(off, hi, r)

    def test_all_rows_inside_effective_window(self):
        self._assert_in_window(self.plan(_accounts(700)))

    def test_other_edge_config_also_respected(self):
        """换一套前后裁剪配置再断一次（有效窗口 75 分钟）。"""
        os.environ.update({
            "YIBAN_WINDOW_EDGE_FRONT_SEC": "120",
            "YIBAN_WINDOW_EDGE_BACK_SEC": "180",
        })
        win = self.bounds()
        self.assertEqual((win.lo_min, win.hi_min), (362.0, 437.0))
        rows = self.plan(_accounts(400), dist="uniform")
        self._assert_in_window(rows)
        self.assertEqual(max(_slice_of(r["run_at"], win.lo_min) for r in rows), 74)


class StratifiedTest(_Base):
    def test_seven_hundred_accounts_fill_seventy_slices_exactly(self):
        """N=700、N_slices=70 ⇒ 每片恰 10 人（`slice_i = i mod 70` 的零方差分层）。

        显式取 `uniform`：零方差铺满 70 片是均匀模式的语义；`front` 只铺前段（见
        `FrontFillTest`），默认值改为 front 后必须在这里固定住 uniform 的逐值行为。
        """
        eff_lo = self.eff_lo_min()
        rows = self.plan(_accounts(700), dist="uniform")
        counts = {}
        for r in rows:
            k = _slice_of(r["run_at"], eff_lo)
            counts[k] = counts.get(k, 0) + 1
        self.assertEqual(len(counts), 70)
        self.assertEqual(set(counts.values()), {10})

    def test_small_n_is_front_loaded(self):
        """小 N 前载是预期行为（早签留足重试余量），不是"没铺满窗口"的 bug。

        显式取 `uniform` 钉住逐值形状：`front` 的小 N 更前载（多账号共片），另见
        `FrontFillTest`。
        """
        eff_lo = self.eff_lo_min()
        three = self.plan(_accounts(3), dist="uniform")
        self.assertEqual(sorted(_slice_of(r["run_at"], eff_lo) for r in three), [0, 1, 2])
        thirty = self.plan(_accounts(30), dist="uniform")
        self.assertEqual(sorted(_slice_of(r["run_at"], eff_lo) for r in thirty), list(range(30)))

    def test_user_paused_accounts_are_not_planned(self):
        """自暂停账号零占位（与 v2 `build_schedule` 同口径）：不出现、也不留空位。"""
        eff_lo = self.eff_lo_min()
        rows = self.plan(_accounts(30, paused=(0, 5)), dist="uniform")
        self.assertEqual(len(rows), 28)
        self.assertNotIn(_phone(0), {r["phone"] for r in rows})
        self.assertEqual(sorted(_slice_of(r["run_at"], eff_lo) for r in rows), list(range(28)))

    def test_duplicate_phones_collapse(self):
        """同一手机号重复出现只计划一次（否则计划里先分叉，落库才被主键 IGNORE）。"""
        self.assertEqual(len(self.plan(_accounts(3) + _accounts(1))), 3)


class SlotPhaseTest(_Base):
    def test_slot_and_phase_ranges_and_coverage(self):
        eff_lo = self.eff_lo_min()
        rows = self.plan(_accounts(700))
        slots = []
        for r in rows:
            j, phase = _slot_phase(r["run_at"], eff_lo)
            self.assertTrue(0 <= j < 60, r)
            self.assertTrue(0.0 <= phase < 1.0, r)
            slots.append(j)
        self.assertEqual(sorted(set(slots)), list(range(60)), "微槽分布覆盖 60 个取值")

    def test_slot_is_independent_of_slice(self):
        """同一片内的账号不被压到同一个微槽（片与槽是两级独立自由度）。"""
        eff_lo = self.eff_lo_min()
        rows = self.plan(_accounts(700))
        by_slice = {}
        for r in rows:
            by_slice.setdefault(_slice_of(r["run_at"], eff_lo), []).append(
                _slot_phase(r["run_at"], eff_lo)[0])
        for k, js in by_slice.items():
            self.assertGreater(len(set(js)), 1, f"片 {k} 的 10 个账号落在同一微槽")


class CrossDayTest(_Base):
    def test_slice_stable_slot_phase_and_vshard_reshuffle(self):
        accs = _accounts(200)
        eff_lo = self.eff_lo_min()
        today = {r["phone"]: r for r in self.plan(accs, day=DAY, order="sequence")}
        tomorrow = {r["phone"]: r for r in self.plan(accs, day=NEXT_DAY, order="sequence")}
        self.assertEqual(
            {p: _slice_of(r["run_at"], eff_lo) for p, r in today.items()},
            {p: _slice_of(r["run_at"], eff_lo) for p, r in tomorrow.items()},
            "片序号只由账号序号决定，跨天不变",
        )
        moved = sum(1 for p in today if today[p]["run_at"] != tomorrow[p]["run_at"])
        self.assertGreaterEqual(moved, 0.3 * len(today), "微槽/相位跨天重排")
        rehashed = sum(1 for p in today if today[p]["vshard"] != tomorrow[p]["vshard"])
        self.assertGreaterEqual(rehashed, 0.3 * len(today), "vshard 吃 day，跨天重排")


class PrefHardTest(_Base):
    def test_pref_account_lands_inside_its_five_minutes(self):
        """自选硬约束：`slot_min=35` ⇒ 100% 落在 06:35~06:40。"""
        eff_lo = self.eff_lo_min()
        phones = [_phone(i) for i in range(50)]
        rows = self.plan(_accounts(50), prefs=_prefs(phones, 35))
        for r in rows:
            k = _slice_of(r["run_at"], eff_lo)
            self.assertIn(k, (30, 31, 32, 33, 34), r)
        self.assertEqual(len(rows), 50, "自选账号不丢号")

    def test_pref_of_another_block_uses_that_block(self):
        eff_lo = self.eff_lo_min()
        rows = self.plan(_accounts(20), prefs=_prefs([_phone(i) for i in range(20)], 10))
        self.assertEqual(sorted({_slice_of(r["run_at"], eff_lo) for r in rows}), [5, 6, 7, 8, 9])

    def test_unknown_phone_in_prefs_is_ignored(self):
        """换号/删号后的孤儿不占容量，也不产生计划行。"""
        prefs = _prefs([_phone(i) for i in range(5)], 35)
        prefs["13900000000"] = {"slot_min": 35, "updated_at": _stamp(0)}
        rows = self.plan(_accounts(5), prefs=prefs)
        self.assertEqual(len(rows), 5)
        self.assertNotIn("13900000000", {r["phone"] for r in rows})

    def test_unavailable_slot_falls_back_to_auto(self):
        """片号不在今日可选范围（`_slot_to_bi` 成员性判定）→ 回退自动分配而不是丢号。

        窗口 06:00~07:20 前裁 300s 后，第 0 片（06:00~06:05）整片落在有效窗口之外。
        """
        eff_lo = self.eff_lo_min()
        rows = self.plan(_accounts(10), prefs=_prefs([_phone(0)], 0), dist="uniform")
        self.assertEqual(len(rows), 10)
        self.assertEqual(sorted(_slice_of(r["run_at"], eff_lo) for r in rows), list(range(10)))


class PrefOverflowTest(_Base):
    """自选溢出的双层语义：内层在自选片内消化，外层按 v2 `_nearest_available` 就近跨片。"""

    def test_inner_layer_absorbs_until_block_is_full(self):
        eff_lo = self.eff_lo_min()
        phones = [_phone(i) for i in range(300)]
        rows = self.plan(_accounts(300), prefs=_prefs(phones, 35))
        actual = {r["phone"]: _slice_of(r["run_at"], eff_lo) for r in rows}
        counts = {}
        for k in actual.values():
            counts[k] = counts.get(k, 0) + 1
        self.assertEqual(sorted(counts), [30, 31, 32, 33, 34], "全部留在自选片内（内层消化）")
        self.assertEqual(set(counts.values()), {60}, "片内 5 个 1 分钟分片各 60 槽被塞满")
        chosen = {p: 30 + _h(p, DAY) % 5 for p in phones}  # 契约：按 H(phone‖day) 取其一
        relocated = [p for p in phones if actual[p] != chosen[p]]
        self.assertTrue(relocated, "片满时账号被顺延到同片内的其它 1 分钟分片")
        self.assertTrue(all(30 <= actual[p] <= 34 for p in relocated))

    def test_outer_layer_spills_to_nearest_block_earlier_first(self):
        eff_lo = self.eff_lo_min()
        phones = [_phone(i) for i in range(305)]
        rows = self.plan(_accounts(305), prefs=_prefs(phones, 35))
        by_slice = {}
        for r in rows:
            by_slice.setdefault(_slice_of(r["run_at"], eff_lo), []).append(r["phone"])
        # 自选片的 5 个 1 分钟分片先被塞满（各 60）；余下 5 个溢出到**早一个 5 分钟片**里
        # 离自选片最近的那一片（v2 `_nearest_available`：同距离优先更早的片，片内就近）
        self.assertEqual(sorted(by_slice), [29, 30, 31, 32, 33, 34])
        self.assertEqual(sorted(len(v) for v in by_slice.values()),
                         [5] + [60] * 5, "自选片 300 满 + 邻近早片 5")

    def test_first_come_first_served_within_block(self):
        """同一片内 updated_at 早者留在片内，晚者溢出（v2 先到先得语义）。"""
        eff_lo = self.eff_lo_min()
        phones = [_phone(i) for i in range(305)]
        rows = {r["phone"]: r for r in self.plan(_accounts(305), prefs=_prefs(phones, 35))}
        in_block = {p for p, r in rows.items() if _slice_of(r["run_at"], eff_lo) in range(30, 35)}
        self.assertEqual(in_block, set(phones[:300]), "最早的 300 个留在自选片内")
        self.assertEqual({p for p in phones if p not in in_block}, set(phones[300:]))


class PrefBasePointTest(_Base):
    """片号基点唯一：候选分片与溢出半径都以**窗口起点**为基点。

    片号（`slot_min`）是相对窗口起点的 5 分钟格（与 web `_pref_slots` /
    `schedule._slot_to_bi` 同号），故"基点"必须与它们同源；否则缓冲过大而缓冲被收缩时，
    展示按窗口起点、计划按收缩后的有效窗口起点，落点整体错位。
    """

    def test_pref_slices_normal_window_pinned(self):
        """正常窗口（无退化）：基点即窗口起点，候选分片逐值不变（显式期望）。"""
        cfg = self.cfg()
        win = self.bounds()
        self.assertFalse(win.fell_back)
        self.assertEqual(win.start_min, 360)
        cands = planner._pref_slices(35, cfg, win.lo_min, win.hi_min,
                                     planner._slice_count(cfg), schedule._slot_to_bi(cfg))
        # 片 35 → 06:00 + 35 分钟 = 06:35；有效窗口 06:05~07:15 → 分片 30~34
        self.assertEqual(cands, [30, 31, 32, 33, 34])

    def test_pref_slices_use_clamped_window_start(self):
        """缓冲过大：基点仍取窗口起点（07:00），收缩的只是缓冲（有效窗口 07:01~07:09）。"""
        os.environ.update(CLAMPED_WINDOW_ENV)
        cfg = self.cfg()
        win = self.bounds()
        self.assertTrue(win.edges_clamped)
        self.assertFalse(win.fell_back)
        self.assertEqual((win.start_min, win.end_min), (420, 430))
        self.assertEqual((win.lo_min, win.hi_min), (421.0, 429.0))
        # 片 0 与片 5 覆盖收缩后的有效窗口（偏移 0 = 07:00~07:05，偏移 5 = 07:05~07:10）
        cands = planner._pref_slices(5, cfg, win.lo_min, win.hi_min,
                                     planner._slice_count(cfg), schedule._slot_to_bi(cfg))
        self.assertEqual(cands, [4, 5, 6, 7])
        # 片 5 → 07:00 + 5 分钟 = 07:05 起；绝对分钟 = 有效窗口起点 + 分片号
        self.assertEqual(win.lo_min + cands[0], 425.0)

    def test_spill_block_reaches_the_window_start_slice(self):
        """溢出搜索半径同样按窗口起止算：够得到窗口起点片里仅存的空分片。"""
        os.environ.update(CLAMPED_WINDOW_ENV)
        cfg = self.cfg()
        win = self.bounds()
        n_slices = planner._slice_count(cfg)
        self.assertEqual(n_slices, 8)
        filled = [1] * n_slices
        for k in (0, 1, 2, 3):  # 窗口起点片（偏移 0 = 07:00~07:05）是唯一有空位的片
            filled[k] = 0
        got = planner._spill_block(5, cfg, win.lo_min, win.hi_min, n_slices,
                                   schedule._slot_to_bi(cfg), filled, 1, 4)
        self.assertIsNotNone(got, "搜索半径按原始窗口算 → 够不到窗口起点片的空分片")
        # 逐值钉住：距片 5 最近的空分片落在窗口起点片，且只到该片的最后一个分片（3）
        self.assertEqual(got, 3)


class ModeTest(_Base):
    def test_random_order_differs_from_sequence_but_stays_in_window(self):
        accs = _accounts(50)
        seq = self.plan(accs, order="sequence", dist="uniform")
        rnd = self.plan(accs, order="random", dist="uniform")
        self.assertNotEqual([r["run_at"] for r in seq], [r["run_at"] for r in rnd])
        win = self.bounds()
        span = (win.hi_min - win.lo_min) * 60
        for r in rnd:
            self.assertTrue(0 <= _off_sec(r["run_at"], win.lo_min) < span, r)

    def test_legacy_sign_mode_still_parsed(self):
        """旧 YIBAN_SIGN_MODE 兼容仍在 `_schedule_config`（三模式是产品契约，不静默取消）。"""
        os.environ["YIBAN_SIGN_MODE"] = "normal"
        cfg = self.cfg()
        self.assertEqual((cfg["order"], cfg["dist"]), ("sequence", "normal"))
        os.environ["YIBAN_SIGN_MODE"] = "random"
        self.assertEqual(self.cfg()["order"], "random")

    def test_normal_density_is_middle_concentrated(self):
        """中段集中：默认 σ 范围下比**密度**（中段带宽只有端部总和的一半，人数不可比），
        窄 σ（不撞 `span/3` 上限）时按人数比——分位数断言 40~60% > 两端。"""
        os.environ["YIBAN_SIGN_DIST"] = "normal"
        eff_lo = self.eff_lo_min()
        span = 4200.0
        offs = [_off_sec(r["run_at"], eff_lo) / span for r in self.plan(_accounts(400))]
        mid_band = [x for x in offs if 0.4 <= x < 0.6]
        edges = [x for x in offs if x < 0.2 or x >= 0.8]
        self.assertGreater(len(mid_band) / 0.2, len(edges) / 0.4, "中段密度高于端部密度")
        os.environ.update({"YIBAN_SCHEDULE_SIGMA_MIN_PCT": "4",
                           "YIBAN_SCHEDULE_SIGMA_MAX_PCT": "6"})
        offs = [_off_sec(r["run_at"], eff_lo) / span for r in self.plan(_accounts(60))]
        self.assertGreater(sum(1 for x in offs if 0.4 <= x < 0.6),
                           sum(1 for x in offs if x < 0.2 or x >= 0.8))

    def test_normal_peak_rate_is_capped_by_bucket(self):
        """φ_max × N ≤ Λ（密度峰值速率受出口令牌桶封顶）。"""
        os.environ["YIBAN_SIGN_DIST"] = "normal"
        rows = self.plan(_accounts(400))
        st = planner.plan_stats(rows, self.cfg(), DAY)
        self.assertEqual(st["dist"], "normal")
        self.assertLessEqual(st["rate_peak"], st["lam"] + 1e-9)

    def test_violating_peak_flattens_density_instead_of_sigma(self):
        """违反 φ_max×N ≤ Λ 时压平密度（削峰），σ_eff 不动。"""
        os.environ["YIBAN_SIGN_DIST"] = "normal"
        accs = _accounts(400)
        plain = planner.plan_stats(self.plan(accs), self.cfg(), DAY)
        os.environ["YIBAN_EGRESS_RATE"] = "0.01"
        flat = planner.plan_stats(self.plan(accs), self.cfg(), DAY)
        self.assertEqual(flat["sigma_min"], plain["sigma_min"], "σ_eff 不因削峰而改")
        self.assertGreater(flat["flatten_alpha"], 0.0, "违反约束时压平密度")
        self.assertLess(flat["rate_peak"], plain["rate_peak"], "峰值被削")

    def test_uniform_density_reports_flat_peak(self):
        """均匀模式无密度整形：φ_max = 1/整窗、α = 0。显式取 uniform（front 亦无整形）。"""
        os.environ["YIBAN_SIGN_DIST"] = "uniform"
        rows = self.plan(_accounts(700), dist="uniform")
        st = planner.plan_stats(rows, self.cfg(), DAY)
        self.assertEqual(st["dist"], "uniform")
        self.assertAlmostEqual(st["phi_max"], 1.0 / 4200.0, places=12)
        self.assertEqual(st["flatten_alpha"], 0.0)


#: 生产窗口（2026-10-10 首轮实测口径）：有效窗口 06:31~07:45 = 74 分钟 = 74 个 1 分钟分片。
#: avg=3、gap=10 是名下缺省（YIBAN_AVG_ATTEMPT_SEC / YIBAN_ACCOUNT_GAP_MAX），显式写入
#: 以免别的用例泄漏覆盖。
PROD_WINDOW_ENV = {
    "YIBAN_SIGN_START": "06:30",
    "YIBAN_SIGN_END": "07:50",
    "YIBAN_WINDOW_EDGE_FRONT_SEC": "60",
    "YIBAN_WINDOW_EDGE_BACK_SEC": "300",
    "YIBAN_AVG_ATTEMPT_SEC": "3",
    "YIBAN_ACCOUNT_GAP_MAX": "10",
}


class FrontFillTest(_Base):
    """`front`（"提前铺完"，默认）：铺进容量允许的最早一段，余窗留作重试与兜底。

    `front` 只改自由账号分层所用的分片数，自选（pinned）账号落点完全不变。铺点分片数
    = `ceil(ceil(n_free/K)×(avg+gap)/60)`，K = 执行体数（声明名册行数）。整窗装不下时
    `used_slices` 退化成 `n_slices`，`front` 与 `uniform` 逐字段相同（设计降级路径）。
    """

    def setUp(self):
        super().setUp()
        os.environ.update(PROD_WINDOW_ENV)

    def _place(self, n, k):
        """铺 n 个自由账号（K 个执行体），返回 (rows, 升序落点秒)。"""
        execs = [f"worker-{i}@hostA" for i in range(k)]
        rows = self.plan(_accounts(n), executors=execs, dist="front")
        eff_lo = self.eff_lo_min()
        return rows, sorted(_off_sec(r["run_at"], eff_lo) for r in rows)

    def test_average_spacing_never_faster_than_the_gate(self):
        """**校验者**：排序后相邻 run_at 的平均间距 ≥ (avg+gap)/K。

        平均值 = (最大 run_at − 最小 run_at) / (n−1)。只断平均值，**不断最小值**：
        分片内微槽与相位由哈希派生，同一秒内可能落两个账号，个例很密属正常；平均值
        才是"计划是否跑在闸门前面"的观测量。
        """
        for n, k in ((183, 2), (1200, 6)):
            with self.subTest(n=n, k=k):
                _, offs = self._place(n, k)
                avg_gap = (offs[-1] - offs[0]) / (len(offs) - 1)
                self.assertGreaterEqual(avg_gap, (3 + 10) / k)

    def test_span_within_capacity_allowed_segment(self):
        """跨度判据：last − first ≤ span_needed + 一片宽（末片内相位可跨一点点）。"""
        for n, k in ((183, 2), (1200, 6)):
            with self.subTest(n=n, k=k):
                _, offs = self._place(n, k)
                span_needed = math.ceil(n / k) * (3 + 10)
                self.assertLessEqual(offs[-1] - offs[0], span_needed + planner.SLICE_SEC)

    def test_production_scale_leaves_thirty_minute_tail(self):
        """183 账号 / K=2（首轮生产量级）：最后一个 run_at 距有效窗口末 ≥ 30 分钟。"""
        win = self.bounds()
        _, offs = self._place(183, 2)
        last_min = self.eff_lo_min() + offs[-1] / 60.0
        self.assertGreaterEqual(win.hi_min - last_min, 30.0,
                                "尾部余量不足 30 分钟，出问题时来不及兜底")

    def test_degrades_to_uniform_when_span_exceeds_window(self):
        """span_needed ≥ 整窗 ⇒ `front` 与 `uniform` 逐字段相同（设计降级路径）。"""
        # n=1000、K=2 ⇒ span_needed = 500×13 = 6500s > 4440s 整窗 ⇒ used=n_slices
        execs = ["worker-0@hostA", "worker-1@hostA"]
        front = self.plan(_accounts(1000), executors=execs, dist="front")
        uni = self.plan(_accounts(1000), executors=execs, dist="uniform")
        self.assertEqual(front, uni)

    def test_front_run_at_depends_on_executor_count(self):
        """`front` 有意依赖 K：改 executors（K=1 vs K=3）会改 run_at。

        安全铺点速率随并发数放大 ⇒ 计划落点随之变化。这是设计语义，不是缺陷；
        `uniform`/`normal` 的解耦契约另在 `IndependenceOfKTest` 钉住。
        """
        accs = _accounts(600)
        one = self.plan(accs, executors=["worker-0@hostA"], dist="front")
        three = self.plan(accs, executors=EXECUTORS, dist="front")
        self.assertNotEqual([r["run_at"] for r in one], [r["run_at"] for r in three])

    def test_pinned_accounts_are_unaffected_by_front(self):
        """自选账号落点与 uniform 完全一致：`front` 只改自由账号分层所用的分片数。"""
        phones = [_phone(i) for i in range(40)]
        prefs = _prefs(phones[:20], 35)
        front = {r["phone"]: r["run_at"]
                 for r in self.plan(_accounts(40), dist="front", prefs=prefs)}
        uni = {r["phone"]: r["run_at"]
               for r in self.plan(_accounts(40), dist="uniform", prefs=prefs)}
        for p in phones[:20]:
            self.assertEqual(front[p], uni[p], "自选账号 %s 的落点被 front 改动了" % p)

    def test_default_dist_is_front(self):
        """不传 dist、环境无该键：`build_plan` 与 `plan_stats` 都按 front 出图。"""
        self.assertNotIn("YIBAN_SIGN_DIST", os.environ)
        self.assertEqual(self.cfg()["dist"], "front")
        rows = self.plan(_accounts(183), executors=["worker-0@hostA", "worker-1@hostA"])
        self.assertEqual(planner.plan_stats(rows, self.cfg(), DAY)["dist"], "front")
        eff_lo = self.eff_lo_min()
        n_slices = planner._slice_count(self.cfg())
        max_slice = max(_slice_of(r["run_at"], eff_lo) for r in rows)
        self.assertLess(max_slice, n_slices - 1, "默认未走 front：落点铺满了整窗")

    def test_plan_stats_density_denominator_is_the_front_span(self):
        """`plan_stats` 对 `front` 的 φ 分母取计划实际占用的跨度，不是整窗。

        用整窗分母会把 front 的峰值报成与 `uniform` 同值；而 `front` 是默认值，
        影子对账（`shadow_stats`）与容量核对都会照着这个错值比。
        `plan_stats` 的模式与 K 都取自 `cfg`（不是行），故两侧各自把 cfg 摆对——
        这也是生产调用方的口径（`shadow_stats` 传的就是同一份 cfg）。
        """
        n = 183
        execs = ["worker-0@hostA", "worker-1@hostA"]
        base = dict(self.cfg(), executors=execs)
        st_front = planner.plan_stats(
            self.plan(_accounts(n), executors=execs, dist="front"), dict(base, dist="front"), DAY)
        st_uni = planner.plan_stats(
            self.plan(_accounts(n), executors=execs, dist="uniform"), dict(base, dist="uniform"), DAY)
        self.assertEqual(st_front["dist"], "front")
        self.assertEqual(st_uni["dist"], "uniform")
        win_sec = planner._slice_count(base) * planner.SLICE_SEC
        # front 的密度分母（1/φ）必须落在前段：小于整窗一半
        self.assertLess(1.0 / st_front["phi_max"], win_sec * 0.5,
                        "front 的密度分母仍是整窗 ⇒ 峰值被报成与 uniform 同值")
        self.assertGreater(st_front["phi_max"], st_uni["phi_max"] * 2)
        # uniform 的分母仍是整窗（逐值不变）
        self.assertAlmostEqual(st_uni["phi_max"], 1.0 / win_sec, places=12)


class CompressionTest(_Base):
    def test_over_slot_capacity_halves_slot_width(self):
        cfg = self.cfg()
        self.assertEqual(planner.slot_width_ms(4200, cfg), 1000)
        self.assertEqual(planner.slot_width_ms(4201, cfg), 500)
        eff_lo = self.eff_lo_min()
        rows = self.plan(_accounts(4201))
        self.assertEqual(len(rows), 4201)
        slots = set()
        for r in rows:
            j, phase = _slot_phase(r["run_at"], eff_lo, slot_sec=0.5)
            self.assertTrue(0 <= j < 120, r)
            self.assertTrue(0.0 <= phase < 0.5, r)
            slots.add(j)
        self.assertEqual(max(slots), 119, "压缩后每片 120 槽（槽数 8400）")


class PlanStatsTest(_Base):
    def test_summary_shape_and_totals(self):
        rows = self.plan(_accounts(700))
        st = planner.plan_stats(rows)
        self.assertEqual(st["n"], 700)
        self.assertEqual(sum(st["owners"].values()), 700)
        self.assertEqual(sum(st["shards"].values()), 700)
        self.assertEqual(sum(st["hist"].values()), 700)
        self.assertEqual(set(st["owners"]), set(EXECUTORS))

    def test_histogram_buckets_are_five_minute_slices(self):
        rows = self.plan(_accounts(700), dist="uniform")
        st = planner.plan_stats(rows)
        # 桶键 = 自选片号（相对窗口起点的 5 分钟格，与 time_prefs.slot_min 同号）：
        # 第 0 格（06:00~06:05）被前裁吃空，其后 14 格各 50 人
        self.assertEqual(sorted(st["hist"]), list(range(5, 75, 5)))
        self.assertEqual(set(st["hist"].values()), {50})

    def test_histogram_base_pinned_to_config_start_without_fallback(self):
        """正常窗口（无回退）：桶键基点逐值等于 `sign_start`（06:00），首格键为 5。

        显式期望值钉住"无回退时基点与原始配置同值"，故落点分桶与既有分布逐格一致。
        """
        cfg = self.cfg()
        win = window.bounds(cfg)
        self.assertFalse(win.fell_back)
        self.assertEqual(win.start_min, cfg["sign_start"][0] * 60 + cfg["sign_start"][1])
        self.assertEqual(win.start_min, 6 * 60)
        rows = [_row(_phone(0), f"{DAY} 06:05:00.000"),
                _row(_phone(1), f"{DAY} 07:14:59.000")]
        # 有效窗口起点 06:05 → 键 5；07:14:59 距 06:00 共 74 分 59 秒 → 键 70（末格）
        self.assertEqual(planner.plan_stats(rows, cfg, DAY)["hist"], {5: 1, 70: 1})

    def test_histogram_base_follows_clamped_window_start(self):
        """缓冲过大：桶键基点取窗口起点（07:00 ⇒ 键 0），与自选片号同号。

        基点若按收缩后的有效窗口起点（07:01）算，07:00 的落点会得到负键，与 web 侧
        "片号相对窗口起点"的片号错格，影子期（dry_run）落点对比随之整体偏移。
        """
        os.environ.update(CLAMPED_WINDOW_ENV)
        cfg = self.cfg()
        win = window.bounds(cfg)
        self.assertTrue(win.edges_clamped)
        self.assertEqual((win.start_min, win.end_min), (420, 430))
        rows = [_row(_phone(0), f"{DAY} 07:00:01.000"),
                _row(_phone(1), f"{DAY} 07:05:00.000"),
                _row(_phone(2), f"{DAY} 07:09:59.000")]
        self.assertEqual(planner.plan_stats(rows, cfg, DAY)["hist"], {0: 1, 5: 2})

    def test_geometry_derived_from_a_single_window_view(self):
        """分片数/槽宽都由同一份有效窗口视图派生：逐值钉住，且 `bounds` 只读一次。

        约简前经 `_span`、`window.bounds(cfg).start_min`、`_slice_count`（两次）共读 4 次
        有效窗口；约简为取一次 `win` 再派生，逐值必须不变（正常与缓冲收缩两种窗口都钉）。
        """
        cfg = self.cfg()                       # 有效窗口 06:05~07:15 → 70 分钟分片
        win = window.bounds(cfg)
        self.assertFalse(win.fell_back)
        with mock.patch.object(planner.window, "bounds",
                               wraps=planner.window.bounds) as spy:
            st = planner.plan_stats([], cfg, DAY)
        self.assertEqual(spy.call_count, 1, "几何应只读一次有效窗口")
        self.assertEqual(st["n_slices"], 70)
        self.assertEqual(st["slot_width_ms"], 1000)
        self.assertEqual(st["n_slices"], int((win.hi_min - win.lo_min) * 60 // 60))
        self.assertEqual(st["hist"], {})
        # 缓冲收缩：同一份视图给有效窗口 07:01~07:09（8 分片），仍只读一次
        os.environ.update(CLAMPED_WINDOW_ENV)
        cfg2 = self.cfg()
        win2 = window.bounds(cfg2)
        self.assertTrue(win2.edges_clamped)
        with mock.patch.object(planner.window, "bounds",
                               wraps=planner.window.bounds) as spy2:
            st2 = planner.plan_stats([], cfg2, DAY)
        self.assertEqual(spy2.call_count, 1, "收缩下同样只读一次有效窗口")
        self.assertEqual(st2["n_slices"], 8)
        self.assertEqual(st2["n_slices"], int((win2.hi_min - win2.lo_min) * 60 // 60))

    def test_peak_per_sec_counts_landings_in_the_same_second(self):
        """`peak_per_sec` 是同一秒内的落点数（聚合口径，与计划形状无关）。"""
        rows = [
            _row(_phone(0), f"{DAY} 06:30:01.000"),
            _row(_phone(1), f"{DAY} 06:30:01.400"),
            _row(_phone(2), f"{DAY} 06:30:01.999"),
            _row(_phone(3), f"{DAY} 06:30:02.000"),
        ]
        st = planner.plan_stats(rows)
        self.assertEqual(st["peak_per_sec"], 3)
        self.assertEqual(st["n"], 4)
        self.assertEqual(sum(st["hist"].values()), 4)


class CapacityV3Test(unittest.TestCase):
    """`schedule.capacity_accounts_v3`：逐位钉死修正版容量公式的取值。"""

    def test_reference_values(self):
        self.assertEqual(
            schedule.capacity_accounts_v3(4200, k=1, avg=3, bucket_rate=1.0), 3360)
        self.assertEqual(
            schedule.capacity_accounts_v3(4200, k=1, avg=3, bucket_rate=4.0), 13440)
        self.assertEqual(
            schedule.capacity_accounts_v3(4200, k=2, avg=3, bucket_rate=1.0), 6720)

    def test_m_is_min_of_sixteen_and_twice_the_bucket(self):
        # M = min(16, ceil(bucket_rate × avg × 2))：桶 1/s、avg 3s → 6；桶 4/s → 16（封顶）
        self.assertEqual(schedule.capacity_accounts_v3(4200, k=1, avg=3, bucket_rate=1.0), 3360)
        self.assertEqual(schedule.capacity_accounts_v3(4200, k=1, avg=3, bucket_rate=4.0), 13440)
        # 桶再高也没用：瓶颈变成通道能力 M/avg = 16/3（这正是 min(M/avg, bucket) 的含义）
        self.assertEqual(schedule.capacity_accounts_v3(4200, k=1, avg=3, bucket_rate=8.0), 17920)

    def test_does_not_fall_back_to_the_old_two_point_six_formula(self):
        """反例：原稿拿通道吞吐 M/avg 算容量（忽略令牌桶封顶）会得 8960，高估 2.3 倍。"""
        got = schedule.capacity_accounts_v3(4200, k=1, avg=3, bucket_rate=1.0)
        self.assertEqual(got, 3360)
        self.assertNotEqual(got, 8960, "2.6667 × 4200 × 0.8 = 8960 是原稿口径")

    def test_util_is_the_derating_factor(self):
        self.assertEqual(
            schedule.capacity_accounts_v3(4200, k=1, avg=3, bucket_rate=1.0, util=1.0), 4200)

    def test_legacy_capacity_function_is_unchanged(self):
        """旧串行公式（web 保存闸门仍按它取数）一字未改：4200s / avg 3 / gap 10 → 323。"""
        self.assertEqual(schedule.capacity_accounts(4200, gap=10, avg=3), 323)


class _DbBase(unittest.TestCase):
    """落库用例：一把临时库，签表由 v18/v19 迁移建齐。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-planner-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls._saved = {k: os.environ.get(k) for k in _TOUCHED}
        for k in _TOUCHED:
            os.environ.pop(k, None)
        os.environ.update(WINDOW_ENV)
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
            # 状态目录同样要隔离：迁移会读它补账，未隔离时读到的是本机真实部署的状态文件
            "YIBAN_STATE_DIR": cls.tmp,
        })

    @classmethod
    def tearDownClass(cls):
        cls._close_conn()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR"):
            os.environ.pop(k, None)
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @staticmethod
    def _close_conn():
        from yiban.store import db
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def setUp(self):
        from yiban.store import db
        self._close_conn()
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        self.conn = db.get_conn()

    def tearDown(self):
        self._close_conn()

    def _rows(self, day=DAY):
        return self.conn.execute(
            "SELECT * FROM sign_tasks WHERE day=? ORDER BY phone", (day,)).fetchall()

    def _plan(self, n=20, day=DAY):
        return planner.build_plan(_accounts(n), day, EXECUTORS)


class WritePlanTest(_DbBase):
    def test_insert_then_rerun_is_idempotent(self):
        """幂等落库：连跑两次行数不变，第二次写入 0 行。"""
        rows = self._plan(20)
        self.assertEqual(planner.write_plan(rows, DAY), 20)
        self.assertEqual(len(self._rows()), 20)
        self.assertEqual(planner.write_plan(rows, DAY), 0)
        self.assertEqual(len(self._rows()), 20)

    def test_existing_rows_are_not_overwritten(self):
        """已存在的行不被覆盖（先手工改成 done，重跑后仍为 done）。"""
        rows = self._plan(20)
        planner.write_plan(rows, DAY)
        self.conn.execute(
            "UPDATE sign_tasks SET state='done', result='签到成功' WHERE phone=?",
            (rows[0]["phone"],))
        self.conn.commit()
        planner.write_plan(rows, DAY)
        got = dict(self.conn.execute(
            "SELECT state, result FROM sign_tasks WHERE phone=? AND day=?",
            (rows[0]["phone"], DAY)).fetchone())
        self.assertEqual(got, {"state": "done", "result": "签到成功"})

    def test_columns_written_match_plan_row(self):
        rows = self._plan(3)
        planner.write_plan(rows, DAY)
        by_phone = {r["phone"]: dict(r) for r in self._rows()}
        for r in rows:
            got = by_phone[r["phone"]]
            for key in ("day", "vshard", "owner", "run_at", "priority", "state"):
                self.assertEqual(got[key], r[key], key)
            self.assertEqual(got["attempts"], 0)
            self.assertEqual(got["lease_until"], "")
            self.assertEqual(got["epoch"], 0)
            self.assertNotEqual(got["created_at"], "")

    def test_empty_rows_is_noop(self):
        self.assertEqual(planner.write_plan([], DAY), 0)
        self.assertEqual(len(self._rows()), 0)

    def test_day_defaults_to_row_day(self):
        rows = self._plan(3)
        self.assertEqual(planner.write_plan(rows), 3)
        self.assertEqual(len(self._rows()), 3)

    def test_db_failure_raises(self):
        """失败语义显式：库不可用时抛异常，调用方据此降级（不静默半途而废）。"""
        rows = self._plan(3)
        with mock.patch.object(queue_store, "_queue_conn",
                               side_effect=RuntimeError("库不可用")), \
                self.assertRaises(RuntimeError):
            planner.write_plan(rows, DAY)
        self.assertEqual(len(self._rows()), 0)

    def test_compressed_plan_writes_slot_width_metadata(self):
        """压缩模式：槽宽写进 app_meta（执行体无需感知），不落"压缩告警"了事。"""
        rows = planner.build_plan(_accounts(4201), DAY, EXECUTORS)
        self.assertEqual(planner.write_plan(rows, DAY), 4201)
        self.assertEqual(clock_meta.get_meta(planner.SLOT_WIDTH_META_KEY), "500")

    def test_normal_plan_writes_second_slot_width(self):
        planner.write_plan(self._plan(20), DAY)
        self.assertEqual(clock_meta.get_meta(planner.SLOT_WIDTH_META_KEY), "1000")


class HasPlanTest(_DbBase):
    def test_false_without_rows_true_with_rows(self):
        self.assertFalse(planner.has_plan(DAY))
        planner.write_plan(self._plan(5), DAY)
        self.assertTrue(planner.has_plan(DAY))
        self.assertFalse(planner.has_plan(NEXT_DAY))

    def test_db_failure_reads_as_no_plan(self):
        """库不可用按"当日无计划"处理 —— 执行体据此降级动态领取，而不是空转。"""
        with mock.patch.object(queue_store, "_queue_conn",
                               side_effect=RuntimeError("库不可用")):
            self.assertFalse(planner.has_plan(DAY))


if __name__ == "__main__":
    unittest.main()
