# -*- coding: utf-8 -*-
"""`docs/dev/scheduler-v3.md` 的门禁式断言：8 节齐 + 两条运维纪律在场 + 灰度前置清单。

对应简报 ⑤.10：文档存在性、8 个节标题齐、两条运维纪律在场，并摘入
`docs/dev/m1-task-rearrangement-20260923.md` 的 R-T14 灰度前置要点（尤其
「开闸日必须晚于平移/补账日」与「当日单写者」）。
"""
import os
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(BASE, "docs", "dev", "scheduler-v3.md")

#: 简报 ④.6 逐字给出的八个节标题。
SECTIONS = (
    "开关与灰度",
    "架构一图流",
    "容量与限速",
    "数据与状态",
    "运维操作",
    "观测",
    "已知限制与未做",
    "排障手册",
)

#: 两条运维纪律的判据短语（简报 ④.6）。
DISCIPLINES = (
    "加出口不再线性放大总速率",
    "待实测裁决",
)


class SchedulerV3DocTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(DOC, encoding="utf-8") as f:
            cls.text = f.read()
        cls.headings = [ln.strip() for ln in cls.text.splitlines()
                        if ln.strip().startswith("#")]

    def test_document_exists(self):
        self.assertTrue(os.path.isfile(DOC), "调度 v3 运维文档必须存在")

    def test_all_eight_sections_present_as_headings(self):
        for title in SECTIONS:
            with self.subTest(title=title):
                self.assertTrue(
                    any(h.startswith("##") and title in h for h in self.headings),
                    f"缺少节标题：{title}")

    def test_two_operational_disciplines_present(self):
        for phrase in DISCIPLINES:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.text)

    def test_gray_release_checklist_is_excerpted(self):
        """R-T14 八条要点必须摘进文档（至少两条硬前置）。"""
        for phrase in ("当日单写者", "平移", "补账"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.text)

    def test_attempt_rate_unit_is_stated(self):
        """容量数字必须标明单位：attempt/s（简报 ④.6 第 3 节的单位声明）。"""
        self.assertIn("attempt/s", self.text)


if __name__ == "__main__":
    unittest.main()
