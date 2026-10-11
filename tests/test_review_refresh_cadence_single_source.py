# -*- coding: utf-8 -*-
"""待审核刷新节拍单一来源守卫（工单 vff0 复查 D2）。

判据：待审核刷新面四个取数点的节拍必须**相等**。
四处：/work/users 列表（`Users.vue`）、/work/accounts 列表（`Accounts.vue`）、
总览 KPI（`DashboardPage.vue`）、导航徽标（`core.js` 的 `NAV_BADGE_TTL`）。

为什么需要：节拍是"同一件事的一个数"，散在四个文件、又无任一用例断言其相等时，
后人只改一处即静默漂移（节拍分叉 = 徽标与列表不再同拍）。本仓实测把这类
"同一件事多个数"列为主导复发形状。

为什么不做成单一常量：`core.js` 是独立经典脚本，不属 Vite 打包产物，无法 import
TS 常量；抽常量得把 `core.js` 改成模块，改动面远大于本缺陷。故立守卫。

守卫自证（2026-10-10 实测）：把任一处改成 600000 ⇒ 本用例红；还原 ⇒ 绿。
"""
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 名 → (文件, 锚定该点节拍的提取式)。锚点写死到具体函数名，改坏形状即响亮失败。
SITES = {
    "Users.vue（/work/users 列表）": (
        os.path.join("frontend", "src", "users", "Users.vue"),
        r"setInterval\(\s*pollTick\s*,\s*(\d+)\s*\)",
    ),
    "Accounts.vue（/work/accounts 列表）": (
        os.path.join("frontend", "src", "accounts", "Accounts.vue"),
        r"setInterval\(\s*pollTick\s*,\s*(\d+)\s*\)",
    ),
    "DashboardPage.vue（总览 KPI）": (
        os.path.join("frontend", "src", "dashboard", "DashboardPage.vue"),
        r"setInterval\(\s*onPendingTick\s*,\s*(\d+)\s*\)",
    ),
    "core.js（导航徽标）": (
        os.path.join("web", "static", "js", "core.js"),
        r"var\s+NAV_BADGE_TTL\s*=\s*(\d+)\s*;",
    ),
}


def _read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as fh:
        return fh.read()


class ReviewRefreshCadenceTest(unittest.TestCase):
    def test_four_refresh_points_share_one_cadence(self):
        found = {}
        for name, (rel, pattern) in SITES.items():
            src = _read(rel)
            self.assertIsNotNone(re.search(pattern, src),
                                 "%s 里找不到待审核刷新的节拍（形状变了？守卫需同步锚点）：%s" % (name, rel))
            found[name] = int(re.search(pattern, src).group(1))
        unique = set(found.values())
        self.assertEqual(
            len(unique), 1,
            "四个待审核刷新点的节拍不一致——同一件事必须只一个数（毫秒）：%r" % (found,),
        )


if __name__ == "__main__":
    unittest.main()
