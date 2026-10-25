# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""队列抽屉事件时间线的截断方向（`web/routes/queue_api.py::recent_window`）。

标签：F · 前端与界面守卫
覆盖：`GET /api/queue/<idx>/events` 的封顶口径——保留**最近** N 条，砍最旧的一端。
对应实现：`web/routes/queue_api.py` 的 `recent_window` / `api_queue_events`。
关键断言：截断后仍按时间升序；最后一条恒为输入的最后一条（最新）；`cap <= 0` 返回空表
    而不是全量（"封顶参数写坏就放全量"是静默放宽）。
依赖：纯函数，无 DB、无网络、不建 app。

为什么要有它：页面文案是「近 30 天事件 · 已截断」。若实现取 `events[:cap]`，截断留下
**最早**的 50 条，而今天的记录被丢掉——界面写着"已截断"却看不到最新事件，等于给出
"最近无事发生"的假信号。方向错误不会报错，只会在事件多的账号上静默误导。
"""

import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BASE, os.path.join(BASE, "scripts"), os.path.join(BASE, "web")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from web.routes.queue_api import recent_window  # noqa: E402


def _events(n):
    """按时间升序构造 n 条事件（ts 递增，message 为序号便于断言）。"""
    return [{"ts": f"2026-10-01 06:{i // 60:02d}:{i % 60:02d}", "message": f"e{i}"} for i in range(n)]


class RecentWindowTest(unittest.TestCase):
    def test_keeps_tail_and_preserves_ascending_order(self):
        window = recent_window(_events(60), cap=50)
        self.assertEqual(len(window), 50)
        self.assertEqual(window[0]["message"], "e10")
        self.assertEqual(window[-1]["message"], "e59")  # 最新一条必须留下
        self.assertEqual([e["message"] for e in window], sorted(
            (e["message"] for e in window), key=lambda s: int(s[1:])))

    def test_short_series_is_returned_whole(self):
        rows = _events(3)
        self.assertEqual(recent_window(rows, cap=50), rows)

    def test_empty_series(self):
        self.assertEqual(recent_window([], cap=50), [])

    def test_non_positive_cap_is_empty_not_unbounded(self):
        self.assertEqual(recent_window(_events(5), cap=0), [])
        self.assertEqual(recent_window(_events(5), cap=-1), [])

    def test_does_not_mutate_input(self):
        rows = _events(5)
        before = list(rows)
        recent_window(rows, cap=2)
        self.assertEqual(rows, before)


if __name__ == "__main__":
    unittest.main()
