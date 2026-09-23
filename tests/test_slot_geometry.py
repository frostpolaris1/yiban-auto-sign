# -*- coding: utf-8 -*-
"""自选片几何的准绳一致性：`schedule._slot_to_bi` / web `_pref_slots` 与 `_schedule_blocks` 同源。

**覆盖**：
- 正常窗口（06:30~07:50，前后各裁 60s）下 `_slot_to_bi` 逐键等价（显式期望字典钉住回归，
  生产默认窗口无裁剪即此形态）；
- 有效窗口被前后裁剪吃空（`window.bounds` 判回退）时自选片仍能映射到块，键集合与默认窗口一致；
- 块索引集合与 `_schedule_blocks` 的块数一一对应（两者准绳不得分叉）；
- v2 / v3 两条链路在裁剪配置下仍尊重用户所选片，且不再产出"不在今日可选范围"告警；
- `slot_min` 是相对窗口起点的分钟偏移（`accounts_data._slot_to_label` 语义钉住）；
- web 侧自选片同准绳（裁剪吃空后不全为灰，片号与正常窗口同构）。

全程 mock / 纯计算，不访问易班服务器（无任何网络请求）。
"""
import contextlib
import logging
import os
import unittest
from datetime import datetime
from unittest import mock

import signin

from web.routes import my
from web.services import accounts_data
from yiban import window
from yiban.engine import planner, schedule

#: 正常窗口：06:30~07:50，前后各裁 60s（与生产默认一致）
NORMAL = {
    "YIBAN_SIGN_START": "06:30",
    "YIBAN_SIGN_END": "07:50",
    "YIBAN_WINDOW_EDGE_FRONT_SEC": "60",
    "YIBAN_WINDOW_EDGE_BACK_SEC": "60",
}
#: 裁剪吃空：窗口仅 10 分钟，前后各裁 300s ⇒ 有效窗口宽度为 0
CROPPED = {
    "YIBAN_SIGN_START": "06:30",
    "YIBAN_SIGN_END": "06:40",
    "YIBAN_WINDOW_EDGE_FRONT_SEC": "300",
    "YIBAN_WINDOW_EDGE_BACK_SEC": "300",
}
#: 会被用例改动、必须逐个还原的环境键
_TOUCHED = (
    "YIBAN_SIGN_START", "YIBAN_SIGN_END", "YIBAN_WINDOW_EDGE_FRONT_SEC",
    "YIBAN_WINDOW_EDGE_BACK_SEC", "YIBAN_WINDOW_EDGE_SEC", "YIBAN_SIGN_ORDER",
    "YIBAN_SIGN_DIST", "YIBAN_SIGN_MODE", "YIBAN_ALLOW_TIME_PREF",
    "YIBAN_BLOCK_CAP", "YIBAN_EGRESS_RATE",
)
#: 默认窗口（06:30~07:50，前后各裁 60s）下的完整映射：16 个片，键 = 相对窗口起点的偏移
DEFAULT_SLOT_TO_BI = {
    0: 0, 5: 1, 10: 2, 15: 3, 20: 4, 25: 5, 30: 6, 35: 7,
    40: 8, 45: 9, 50: 10, 55: 11, 60: 12, 65: 13, 70: 14, 75: 15,
}


@contextlib.contextmanager
def _cfg_env(**overrides):
    """在受控环境变量下取一份调度配置快照（退出时逐键还原，防跨用例污染）。"""
    old = {k: os.environ.get(k) for k in _TOUCHED}
    try:
        for k in _TOUCHED:
            os.environ.pop(k, None)
        for k, v in overrides.items():
            os.environ[k] = str(v)
        yield signin._schedule_config()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _Acc:
    """最小账号替身：Planner 只读 `.phone` 与 `.user_paused`。"""

    __slots__ = ("phone", "user_paused")

    def __init__(self, phone, user_paused=False):
        self.phone = phone
        self.user_paused = user_paused


class SlotToBiYardstickTest(unittest.TestCase):
    def test_default_window_mapping_is_pinned(self):
        """正常窗口逐键等价：默认窗口（无裁剪）的映射必须逐字不变。"""
        with _cfg_env(**NORMAL) as cfg:
            self.assertEqual(signin._slot_to_bi(cfg), DEFAULT_SLOT_TO_BI)

    def test_cropped_empty_window_keeps_slot_keys(self):
        """裁剪吃空：`bounds` 判回退，自选片映射非空且键集合与默认窗口一致。"""
        with _cfg_env(**CROPPED) as cropped:
            self.assertTrue(window.bounds(cropped).fell_back, "该配置应判为回退")
            mapping = signin._slot_to_bi(cropped)
        self.assertTrue(mapping, "裁剪吃空时映射为空 → 自选片被静默放弃")
        self.assertEqual(set(mapping), set(DEFAULT_SLOT_TO_BI))

    def test_block_indices_match_schedule_blocks(self):
        """同准绳：两种配置下块索引集合都与 `_schedule_blocks` 的块数一一对应。"""
        for overrides in (NORMAL, CROPPED):
            with _cfg_env(**overrides) as cfg:
                blocks, _eff_lo, _eff_hi = signin._schedule_blocks(cfg)
                self.assertEqual(set(signin._slot_to_bi(cfg).values()),
                                 set(range(len(blocks))), f"{overrides} 准绳分叉")


class PrefSliceEndToEndTest(unittest.TestCase):
    #: 自选片 30（相对窗口起点）→ 窗口起点 06:30 + 30 分钟 = 07:00 起的 5 分钟片
    SLOT = 30
    SLOT_LO_HM = (7, 0)
    SLOT_HI_HM = (7, 5)

    def _prefs(self, phone):
        return {phone: {"slot_min": self.SLOT, "updated_at": "2026-09-01 00:00:00"}}

    def test_v2_lands_inside_selected_slice(self):
        """v2：裁剪吃空配置下自选片仍被采纳，且不再产出"不在今日可选范围"告警。"""
        phone = "13900000001"
        base = datetime(2026, 9, 22, 0, 0)
        with _cfg_env(**CROPPED), self.assertLogs("yiban", level="WARNING") as cm:
            sched = signin.build_schedule(
                [signin.Account(phone=phone, password="pw")],
                prefs=self._prefs(phone), now=base)
        self.assertIn(phone, sched)
        t = sched[phone]
        self.assertGreaterEqual(t, base.replace(hour=self.SLOT_LO_HM[0],
                                                minute=self.SLOT_LO_HM[1]),
                                "落点早于所选片")
        self.assertLess(t, base.replace(hour=self.SLOT_HI_HM[0],
                                       minute=self.SLOT_HI_HM[1]),
                        "落点晚于所选片")
        self.assertNotIn("不在今日可选范围", "\n".join(cm.output),
                         "所选片被判成落窗外 → 回退自动分配")

    def test_v3_places_pref_inside_selected_slice(self):
        """v3：同一配置下自选片有非空候选分片，落点也在所选片内。"""
        phone = "13900000001"
        day = "2026-09-22"
        with _cfg_env(**CROPPED):
            cfg = schedule.planner_config()
            slot_to_bi = schedule._slot_to_bi(cfg)
            eff_lo, eff_hi = planner._span(cfg)
            cands = planner._pref_slices(self.SLOT, cfg, eff_lo, eff_hi,
                                         planner._slice_count(cfg), slot_to_bi)
            self.assertTrue(cands, "候选分片为空 → 计划侧回退自动分配")
            with self.assertLogs("yiban", level=logging.DEBUG) as cm:
                rows = planner.build_plan([_Acc(phone)], day, ["worker-0@hostA"],
                                          prefs=self._prefs(phone), cfg=cfg)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("不在今日可选范围", "\n".join(cm.output),
                         "所选片被判成落窗外 → 回退自动分配")
        t = planner._minute_of_day(rows[0]["run_at"])
        self.assertGreaterEqual(t, 420.0, "落点早于所选片（07:00）")
        self.assertLess(t, 425.0, "落点晚于所选片（07:05）")


class SlotOffsetSemanticsTest(unittest.TestCase):
    def test_offset_zero_is_window_start(self):
        """`slot_min` 是相对**有效**窗口起点的分钟偏移：偏移 0 → 窗口起点，不是绝对分钟。"""
        self.assertEqual(
            accounts_data._slot_to_label(0, lambda: window.from_env(
                {"YIBAN_SIGN_START": "06:30", "YIBAN_SIGN_END": "07:50"})), "06:30")
        self.assertEqual(
            accounts_data._slot_to_label(30, lambda: window.from_env(
                {"YIBAN_SIGN_START": "07:00", "YIBAN_SIGN_END": "08:00"})),
            "07:30", "偏移应加在窗口起点上，而非按当天 0:00 计")

    def test_non_fallback_label_uses_original_window_start(self):
        """非回退：有效窗口起点与原始窗口起点逐值相等 ⇒ 标签逐值不变（生产显示不变）。"""
        win = window.bounds({"sign_start": (7, 0), "sign_end": (8, 0),
                             "edge_front_sec": 60, "edge_back_sec": 60})
        self.assertFalse(win.fell_back)
        self.assertEqual(accounts_data._slot_to_label(0, lambda: win), "07:00")
        self.assertEqual(accounts_data._slot_to_label(30, lambda: win), "07:30")

    def test_fallback_label_uses_effective_window_start(self):
        """裁剪吃空回退：基点取回退窗口起点（06:30），而非原始配置的 07:00。

        原实现直读 `_sign_window` 的原始起点，回退时片卡（按有效窗口）显示 06:30，
        已存偏好与保存提示（按原始窗口）却说 07:00——同一页面出现两个钟点。
        """
        win = window.bounds({"sign_start": (7, 0), "sign_end": (7, 10),
                             "edge_front_sec": 300, "edge_back_sec": 300})
        self.assertTrue(win.fell_back)
        self.assertEqual((win.start_min, win.end_min), (390, 470))
        self.assertEqual(accounts_data._slot_to_label(0, lambda: win), "06:30")
        self.assertEqual(accounts_data._slot_to_label(30, lambda: win), "07:00")


class WebPrefSlotsYardstickTest(unittest.TestCase):
    def test_pref_slots_not_all_disabled_when_window_cropped(self):
        """web 同准绳：裁剪吃空后自选片不全为灰，且片号/结构集合与正常窗口同构。

        回退窗口 = 默认 06:30~07:50、前后各裁 60s，故逐片钉住具体 label/disabled：
        首末片各被裁 1 分钟（部分保留、可点选并给提示），中段无提示，**没有任何片被置灰**。
        """
        with _cfg_env(**CROPPED) as cropped_cfg:
            cropped = window.bounds(cropped_cfg)
        with _cfg_env(**NORMAL) as normal_cfg:
            normal = window.bounds(normal_cfg)
        self.assertTrue(cropped.fell_back)
        slots = my._pref_slots(cropped)
        base = my._pref_slots(normal)
        self.assertFalse(all(s["disabled"] for s in slots), "裁剪吃空后全部片被置灰")
        self.assertEqual({s["slot_min"] for s in slots},
                         {s["slot_min"] for s in base})
        self.assertEqual([sorted(s) for s in slots], [sorted(s) for s in base])
        self.assertEqual(
            slots[0], {"slot_min": 0, "label": "06:30",
                       "disabled": False, "edge_note": "开头 1 分钟保留"})
        self.assertEqual(
            slots[1], {"slot_min": 5, "label": "06:35", "disabled": False, "edge_note": ""})
        self.assertEqual(
            slots[-1], {"slot_min": 75, "label": "07:45",
                        "disabled": False, "edge_note": "结尾 1 分钟保留"})

    def test_pref_slots_pin_enabled_and_disabled_under_crop(self):
        """有效窗口被前裁吃满 5 分钟（未吃空）：首片整片在裁剪区内 → 置灰，次片可选。

        与上一例互补：回退窗口的前后裁固定 60s（< 5 分钟）故永远置灰不了任何片，
        这里用"仍有效但被裁剪"的窗口钉住置灰与可选两种取值，避免"不全灰"这种
        弱断言在置灰逻辑整体失效时仍然通过。
        """
        win = window.bounds({"sign_start": (6, 30), "sign_end": (7, 0),
                             "edge_front_sec": 300, "edge_back_sec": 0})
        self.assertFalse(win.fell_back)
        slots = my._pref_slots(win)
        self.assertEqual(
            slots[0], {"slot_min": 0, "label": "06:30",
                       "disabled": True, "edge_note": ""})
        self.assertEqual(
            slots[1], {"slot_min": 5, "label": "06:35", "disabled": False, "edge_note": ""})

    def test_app_forward_hands_window_over_to_bounds(self):
        """`web.app.sign_window_bounds` 只转发：起止与前后裁剪交给 `yiban.window.bounds`。"""
        import web.app as webapp
        with mock.patch.object(webapp, "_sign_window", return_value=((6, 30), (6, 40))), \
                mock.patch.object(webapp, "edge_config", return_value=(300, 300)):
            win = webapp.sign_window_bounds()
        self.assertTrue(win.fell_back)
        self.assertEqual((win.start_min, win.end_min), (390, 470))
        self.assertEqual((win.front_sec, win.back_sec), (60, 60))


if __name__ == "__main__":
    unittest.main(verbosity=2)
