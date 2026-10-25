# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""cron 时刻等值门：宿主 cron 表、容器调度器与 window.py 常量三方对拍。

标签：B · 调度：领取/队列/执行体
覆盖：`deploy/prod/cron.d/yiban-sign` 的两次签到触发、`docker/scheduler.py` 的
    `FIRST/SECOND`、`yiban/core/window.py` 的 `DEFAULT_FIRST_HM/DEFAULT_RETRY_HM`。
对应实现：`yiban.core.window` 是时刻常量的唯一定义点；cron 表是静态文件，无法导入，
故由本门读取原文对拍。`docker/scheduler.py` 的 `FIRST` 直接引用该常量。
关键断言：
  ① cron 表首签时刻 == `window.DEFAULT_FIRST_HM`；
  ② cron 表兜底时刻 == `window.DEFAULT_RETRY_HM`；
  ③ 容器 `FIRST` 与 `DEFAULT_FIRST_HM` 相等；
  ④ 容器 `SECOND` 的 07:10 分叉按显式豁免登记，登记值与代码真值一致，理由非空；
  ⑤ 解析器对合成漂移报出差异、空文本回空表（扫空不得当绿）。
依赖：只读仓库文本与 `yiban.core.window` 常量；不启动 cron、容器或网络。
"""

import os
import re
import unittest

from yiban.core import window

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRON = os.path.join(BASE, "deploy", "prod", "cron.d", "yiban-sign")
SCHEDULER = os.path.join(BASE, "docker", "scheduler.py")

#: cron 时间行：五个时间字段均为数字（签到的两次调用）；`* * * * *` 轮询行不匹配。
_CRON_TIME_LINE = re.compile(r"^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*\s+\S", re.MULTILINE)

#: 容器调度器的首签/补签元组（两行合一）。
_SCHED_TUPLE = re.compile(r"^FIRST, SECOND = (.+?), (\([^)]*\))\s*$", re.MULTILINE)

#: 显式豁免：容器补签 07:10 与宿主 07:12 的有意分叉。
#: 判据：容器在同一循环里等首轮子进程返回后再判补签，不存在宿主「07:12 cron 撞
#: 首轮 flock 而空转」的形状，故提前到 07:10 收尾。改这个值即产品决定，必须同步
#: 更新本登记（否则下面的等值断言红）。
CONTAINER_EXEMPTIONS = {
    "docker/scheduler.py::SECOND": {
        "value": (7, 10),
        "reason": "容器同循环内判补签，无宿主 07:12 cron 撞 flock 空转；提前 2 分钟收尾",
    },
}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def cron_times(text):
    """cron 原文 → [(hour, minute), …]（只取纯数字时间行）。空文本回空列表。"""
    return [(int(h), int(m)) for m, h in _CRON_TIME_LINE.findall(text)]


def scheduler_times(text):
    """scheduler.py 原文 → `(first, second)`；表达式按真值求值，缺行抛 AssertionError。"""
    m = _SCHED_TUPLE.search(text)
    if m is None:
        raise AssertionError("docker/scheduler.py 未找到 FIRST, SECOND 元组")
    # 只喂仓库内常量表达式（window.DEFAULT_FIRST_HM / 字面量元组），不接受外部输入。
    first = eval(m.group(1), {"window": window})
    second = eval(m.group(2), {})
    return first, second


class CronTimeEquivalenceTest(unittest.TestCase):
    """三方时刻真值必须逐一相等（容器分叉除外，见豁免表）。"""

    @classmethod
    def setUpClass(cls):
        cls.cron = cron_times(_read(CRON))
        cls.first, cls.second = scheduler_times(_read(SCHEDULER))

    def test_cron_has_exactly_two_sign_runs(self):
        self.assertEqual(len(self.cron), 2,
                         "cron 表的纯数字时间行应恰为两次签到调用；新增/删除行须同步本门")

    def test_first_run_matches_shared_constant(self):
        self.assertEqual(self.cron[0], window.DEFAULT_FIRST_HM,
                         "cron 首签时刻与 yiban.core.window.DEFAULT_FIRST_HM 漂移")

    def test_retry_run_matches_shared_constant(self):
        self.assertEqual(self.cron[1], window.DEFAULT_RETRY_HM,
                         "cron 兜底时刻与 yiban.core.window.DEFAULT_RETRY_HM 漂移")

    def test_container_first_matches_shared_constant(self):
        self.assertEqual(self.first, window.DEFAULT_FIRST_HM,
                         "容器首签时刻与 yiban.core.window.DEFAULT_FIRST_HM 漂移")

    def test_container_second_divergence_is_registered(self):
        entry = CONTAINER_EXEMPTIONS["docker/scheduler.py::SECOND"]
        self.assertEqual(self.second, tuple(entry["value"]),
                         "容器补签时刻与豁免登记不一致（改值须同步登记与理由）")
        self.assertTrue(entry["reason"].strip(), "豁免理由不得为空")
        self.assertNotEqual(self.second, window.DEFAULT_RETRY_HM,
                            "容器补签已与宿主同值：请删除本豁免登记，改为等值断言")


class ParserTeethTest(unittest.TestCase):
    """解析器必须能报出单侧漂移；空文本必须回空表。"""

    # 自证用**合成输入**，不读真文件：否则真文件被外部改错时本组会跟着假红，
    # 看不出是"解析器坏"还是"被解析的文件坏"。

    def test_cron_parser_reports_single_side_drift(self):
        text = "0 6 * * * yiban /opt/run.sh\n0 7 * * * yiban /opt/run.sh\n"
        self.assertEqual(cron_times(text), [(6, 0), (7, 0)])
        mutated = text.replace("0 6 ", "1 6 ", 1)
        self.assertNotEqual(cron_times(mutated)[0], (6, 0))

    def test_scheduler_parser_reports_single_side_drift(self):
        text = "FIRST, SECOND = window.DEFAULT_FIRST_HM, (7, 10)\n"
        self.assertEqual(scheduler_times(text), (window.DEFAULT_FIRST_HM, (7, 10)))
        mutated = text.replace("(7, 10)", "(7, 11)", 1)
        self.assertNotEqual(scheduler_times(mutated)[1], (7, 10))

    def test_empty_text_parses_empty(self):
        self.assertEqual(cron_times(""), [])


if __name__ == "__main__":
    unittest.main()
