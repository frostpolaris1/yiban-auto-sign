# -*- coding: utf-8 -*-
"""`yiban/engine/planner.py`（双粒度分片 Planner）与 `schedule.capacity_accounts_v3` 的契约用例。

窗口取 06:00~07:20、前后各裁 300s ⇒ 有效窗口 70 分钟 = 4200s = 70 个 1 分钟分片
（每片 60 个 1 秒微槽），与容量核算口径同数。自选片 `slot_min` 仍是"相对窗口起点"的
5 分钟格（与 web `_pref_slots`、`schedule._slot_to_bi` 同源），故 `slot_min=35` 对应
06:35~06:40。

覆盖：确定性/可重放、与执行体数 K 无关、落点有界、分层零方差、微槽与相位范围、
跨天重排、小 N 前载、自选硬约束与双层溢出、先到先得、三模式保留、压缩模式与元数据、
幂等落库与降级信号、容量公式取值。
"""
import contextlib
import datetime
import hashlib
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
#: 裁剪吃空：窗口仅 10 分钟、前后各裁 300s ⇒ 有效窗口宽度为 0，`window.bounds` 回退默认窗口
#: （06:30~07:50）。此时"窗口起点 + 片偏移"必须以**回退后**的 390 分（06:30）为基点。
EMPTY_WINDOW_ENV = {
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
    raw = "\x1f".join(str(p) for p in parts).encode("utf-8")
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
        self._saved = {k: os.environ.get(k) for k in _TOUCHED}
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
        """与 K 无关：改 executors 只改 owner，不改任何 run_at（计划层与执行体数解耦）。"""
        accs = _accounts(200)
        one = self.plan(accs, executors=["worker-0@hostA"])
        three = self.plan(accs, executors=EXECUTORS)
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
        rows = self.plan(_accounts(400))
        self._assert_in_window(rows)
        self.assertEqual(max(_slice_of(r["run_at"], win.lo_min) for r in rows), 74)


class StratifiedTest(_Base):
    def test_seven_hundred_accounts_fill_seventy_slices_exactly(self):
        """N=700、N_slices=70 ⇒ 每片恰 10 人（`slice_i = i mod 70` 的零方差分层）。"""
        eff_lo = self.eff_lo_min()
        rows = self.plan(_accounts(700))
        counts = {}
        for r in rows:
            k = _slice_of(r["run_at"], eff_lo)
            counts[k] = counts.get(k, 0) + 1
        self.assertEqual(len(counts), 70)
        self.assertEqual(set(counts.values()), {10})

    def test_small_n_is_front_loaded(self):
        """小 N 前载是预期行为（早签留足重试余量），不是"没铺满窗口"的 bug。"""
        eff_lo = self.eff_lo_min()
        three = self.plan(_accounts(3))
        self.assertEqual(sorted(_slice_of(r["run_at"], eff_lo) for r in three), [0, 1, 2])
        thirty = self.plan(_accounts(30))
        self.assertEqual(sorted(_slice_of(r["run_at"], eff_lo) for r in thirty), list(range(30)))

    def test_user_paused_accounts_are_not_planned(self):
        """自暂停账号零占位（与 v2 `build_schedule` 同口径）：不出现、也不留空位。"""
        eff_lo = self.eff_lo_min()
        rows = self.plan(_accounts(30, paused=(0, 5)))
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
        rows = self.plan(_accounts(10), prefs=_prefs([_phone(0)], 0))
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
    """片号基点唯一：候选分片与溢出半径都以**有效窗口起点**为基点。

    片号（`slot_min`）是相对窗口起点的 5 分钟格（与 web `_pref_slots` /
    `schedule._slot_to_bi` 同号），故"基点"必须与它们同源；否则窗口被裁剪吃空而
    `window.bounds` 回退默认窗口时，展示按回退窗口、计划按原始配置，落点整体错位。
    """

    def test_pref_slices_normal_window_pinned(self):
        """正常窗口（无回退）：基点即原始窗口起点，候选分片逐值不变（显式期望）。"""
        cfg = self.cfg()
        win = self.bounds()
        self.assertFalse(win.fell_back)
        self.assertEqual(win.start_min, 360)
        cands = planner._pref_slices(35, cfg, win.lo_min, win.hi_min,
                                     planner._slice_count(cfg), schedule._slot_to_bi(cfg))
        # 片 35 → 06:00 + 35 分钟 = 06:35；有效窗口 06:05~07:15 → 分片 30~34
        self.assertEqual(cands, [30, 31, 32, 33, 34])

    def test_pref_slices_use_fallback_window_start(self):
        """裁剪吃空回退：基点取回退窗口起点（06:30），而非原始配置的 07:00。"""
        os.environ.update(EMPTY_WINDOW_ENV)
        cfg = self.cfg()
        win = self.bounds()
        self.assertTrue(win.fell_back)
        self.assertEqual((win.start_min, win.end_min), (390, 470))
        cands = planner._pref_slices(30, cfg, win.lo_min, win.hi_min,
                                     planner._slice_count(cfg), schedule._slot_to_bi(cfg))
        self.assertEqual(cands, [29, 30, 31, 32, 33])
        # 片 30 → 06:30 + 30 分钟 = 07:00 起；绝对分钟 = 有效窗口起点 + 分片号
        self.assertEqual(win.lo_min + cands[0], 420.0)

    def test_spill_block_reaches_the_fallback_window_start(self):
        """溢出搜索半径同样按有效窗口算：够得到回退窗口起点处仅存的空片。"""
        os.environ.update(EMPTY_WINDOW_ENV)
        cfg = self.cfg()
        win = self.bounds()
        n_slices = planner._slice_count(cfg)
        filled = [1] * n_slices
        for k in (0, 1, 2, 3):  # 窗口起点片（偏移 0 = 06:30~06:35）是唯一空片
            filled[k] = 0
        got = planner._spill_block(75, cfg, win.lo_min, win.hi_min, n_slices,
                                   schedule._slot_to_bi(cfg), filled, 1, 0)
        self.assertIsNotNone(got, "搜索半径按原始窗口算 → 够不到回退窗口起点的空片")
        # 逐值钉住：距 75 片最近的空片就是回退窗口起点片（偏移 0），就近取 k0=0
        self.assertEqual(got, 0)


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
        rows = self.plan(_accounts(700))
        st = planner.plan_stats(rows, self.cfg(), DAY)
        self.assertAlmostEqual(st["phi_max"], 1.0 / 4200.0, places=12)
        self.assertEqual(st["flatten_alpha"], 0.0)


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
        rows = self.plan(_accounts(700))
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

    def test_histogram_base_follows_fallback_window_start(self):
        """裁剪吃空回退：桶键基点取回退窗口起点（06:30 ⇒ 键 0），与自选片号同号。

        基点若仍按原始配置的 07:00 算，06:30 的落点会得到负键（-30），与 web 侧
        "片号相对回退后窗口起点"的片号错格，影子期（dry_run）落点对比随之整体偏移。
        """
        os.environ.update(EMPTY_WINDOW_ENV)
        cfg = self.cfg()
        win = window.bounds(cfg)
        self.assertTrue(win.fell_back)
        self.assertEqual((win.start_min, win.end_min), (390, 470))
        rows = [_row(_phone(0), f"{DAY} 06:30:01.000"),
                _row(_phone(1), f"{DAY} 06:35:00.000"),
                _row(_phone(2), f"{DAY} 07:49:59.000")]
        self.assertEqual(sorted(planner.plan_stats(rows, cfg, DAY)["hist"]), [0, 5, 75])

    def test_geometry_derived_from_a_single_window_view(self):
        """分片数/槽宽都由同一份有效窗口视图派生：逐值钉住，且 `bounds` 只读一次。

        约简前经 `_span`、`window.bounds(cfg).start_min`、`_slice_count`（两次）共读 4 次
        有效窗口；约简为取一次 `win` 再派生，逐值必须不变（正常与回退两种窗口都钉）。
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
        # 裁剪吃空回退：同一份视图给回退窗口（06:31~07:49 → 78 分片），仍只读一次
        os.environ.update(EMPTY_WINDOW_ENV)
        cfg2 = self.cfg()
        win2 = window.bounds(cfg2)
        self.assertTrue(win2.fell_back)
        with mock.patch.object(planner.window, "bounds",
                               wraps=planner.window.bounds) as spy2:
            st2 = planner.plan_stats([], cfg2, DAY)
        self.assertEqual(spy2.call_count, 1, "回退下同样只读一次有效窗口")
        self.assertEqual(st2["n_slices"], 78)
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
