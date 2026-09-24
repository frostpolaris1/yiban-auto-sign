# -*- coding: utf-8 -*-
"""`docs/dev/scheduler-v3.md` 的门禁式断言：8 节齐 + 两条运维纪律在场 + 灰度前置清单。

标签：B · 调度：领取/队列/执行体
覆盖：docs/dev/scheduler-v3.md
   的存在性、八个节标题齐备、两条运维纪律的判据短语在场、R-T14
   灰度前置要点的摘入（当日单写者、平移、补账）、速率单位声明在场。
对应实现：docs/dev/scheduler-v3.md（被测对象是这份文档本身）；被约束的代码是
   yiban/engine/executor_v3.py 与灰度开关。
关键断言：本文件测的是文档结构，不是代码行为：它保证运维在灰度当天手里那份文档仍然含得住两条纪律与灰度前置清单。因此「删掉一节」「把纪律改写成同义词」都会红，而这不代表代码坏了。节标题按
   ## 层级 + 前缀匹配判定，纪律与要点按判据短语在场判定。
依赖：只读仓库内的 docs/dev/scheduler-v3.md
   文本（不导入业务模块、不建库、不联网）。文档被移动或改名会让 setUpClass
   直接读不到文件。整文件在本机执行，无 skip。

对应简报 ⑤.10：文档存在性、8 个节标题齐、两条运维纪律在场，并摘入
`docs/dev/m1-task-rearrangement-20260923.md` 的 R-T14 灰度前置要点（尤其
「开闸日必须晚于平移/补账日」与「当日单写者」）。
"""
import os
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(BASE, "docs", "dev", "scheduler-v3.md") # 被测对象就是这份文档：它改名或移位，本文件的用例会一起变红

#: 简报 ④.6 逐字给出的八个节标题。
SECTIONS = (
    "开关与灰度", # 节标题逐字取自设计文档：改成同义词（哪怕更通顺）也算这一节没了
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
                        if ln.strip().startswith("#")] # 只收标题行：正文里再出现同名短语不算「这一节还在」

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
