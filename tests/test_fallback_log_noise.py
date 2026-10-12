# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""兜底常驻的**空转轮日志面**：级别收敛、文案与实际间隔一致、以及「不提前退出」的评估钉。

生产 2026-10-07 实测（sign-2026-10-07.log）：『兜底执行体：本轮处理 N 个账号（…），60s
后再扫』与 v3 横幅『6 条通道 / 64 个分片 / 出口』成对以 **5 秒**周期出现，全天各 180/184
次。两个成因都在本文件钉住：

1. 轮次行**每轮都打 INFO**——事实没变的空转轮（`own` / `settled` / 等待形态三者不变）没有
   必要每轮出声，只有事实变化才打 INFO；
2. 文案里的间隔是**配置间隔**（60），而实际等待是 `min(间隔, 5)`（有活）、事件驱动的短轮询
   （扫空后有池）、或盲的整段间隔（无池）——三种等待的秒数必须与打出来的数字一致。

标签：B · 调度：领取/队列/执行体
覆盖：兜底轮次行的级别收敛（空转 N 轮只留一条 INFO、事实变化时重新出声）、三种等待形态
   下「打出来的秒数 = 实际睡掉的秒数」、以及「窗口内不因 settled 提前退出」的评估结论钉
   （用项目自己的 `state_io.has_undone_accounts_today` 证明「已判 settled」，再证明它照旧
   继续扫）。
对应实现：`yiban/engine/workers.py` 的 `run_fallback_worker`（轮次行、等待选择、
   `_await_pool_event`）、`yiban/engine/state_io.py` 的了结判据。
关键断言：**空转轮的 INFO 条数有上界**（事实不变即不产），而「有活 / 状态变化」仍出声
   （反向控制同权重——把降噪做成「永远不打」会让排障失去轮次可见性）；轮次行里的秒数必须
   等于本轮真实睡掉的秒数，不然就是「文案在声称代码没做的事」。
不覆盖：v3 横幅本身（钉在 `tests/test_executor_v3.py::LogAttributionTest`）；**是否**该让
   兜底在 settled 后提前退出——本文件钉的是「**不**做」，理由见 `NoEarlyExitAfterSettleTest`。
依赖：同目录夹具 `test_fallback_gates._FallbackHarness`（假时钟 + 打桩执行体/时钟/锁探测，
   不真实 sleep、不起子进程、不发网络请求），本文件只加日志捕获与逐轮变化的结果集替身。
用法（项目根目录）：
    bash scripts/dev-verify.sh --target tests/test_fallback_log_noise.py
"""
import logging
import os
import re
import shutil
import socket
import sys
import tempfile
import unittest
from typing import ClassVar
from unittest import mock

_BASE = os.path.dirname(os.path.abspath(__file__))
if _BASE not in sys.path:  # 同目录夹具（与 test_alert_channel_dispatch 的用法一致）
    sys.path.insert(0, _BASE)

from test_fallback_gates import WED, _at, _FallbackHarness  # noqa: E402

from yiban.engine import state_io  # noqa: E402

#: 轮次行的识别片段（级别收敛后这句话仍在，只是级别随事实变化）
ROUND_MARK = "本轮处理"
#: 从日志行里取秒数（`60s 后再扫` 与 `（1560 秒后开始）` 两种写法都认）
_SECS_RE = re.compile(r"(\d+)\s*(?:s|秒)")


class _RoundResults(dict):
    """逐轮变化的执行体结果集替身：**每被问一次 `len()` 就给出序列里的下一档**。

    `run_fallback_worker` 每轮只问一次 `len(results)`（`own = len(results)`），随后用
    `.values()` 判失败——故这个替身既能驱动 `own` 逐轮变化（0 → 2 → 2），又不引入任何
    真实结果行。`dict` 基类只为让 `.values()` 天然是空迭代。
    """

    def __init__(self, sizes):
        super().__init__()
        self._sizes = list(sizes)
        self._i = 0

    def __len__(self):
        value = self._sizes[min(self._i, len(self._sizes) - 1)]
        self._i += 1
        return value


def _level_lines(records, level, mark=None):
    """某一级别的记录文本（给了 `mark` 就只留含它的那些）。"""
    out = [r.getMessage() for r in records if r.levelno == level]
    return [m for m in out if mark is None or mark in m] if mark else out


def _first_secs(line):
    """行里第一个秒数。"""
    found = _SECS_RE.search(line)
    assert found, f"日志行里没有秒数：{line}"
    return int(found.group(1))


def _last_secs(line):
    """行里最后一个秒数（`…（1500 秒后开始），60 秒后再看` 取的是等待那个）。"""
    found = _SECS_RE.findall(line)
    assert found, f"日志行里没有秒数：{line}"
    return int(found[-1])


class _NoiseHarness(_FallbackHarness):
    """在 `_FallbackHarness` 之上加日志捕获的取数口。"""

    #: 测试用的执行体身份串：`@{主机名}` 段是**哨兵**，用来证明它没被写进日志
    TAG_HOST = "b2log-taghost"
    ENV: ClassVar[dict] = {"YIBAN_EXECUTOR_ID": "fallback@" + TAG_HOST}

    #: 窗口内两轮（06:35/06:36）+ 窗口已关的一轮（08:30，主循环由此退出）
    TIMES: ClassVar[list] = [_at(WED, (6, 35)), _at(WED, (6, 36)), _at(WED, (8, 30))]

    def _logs(self, times=None, **kw):
        """跑一遍兜底主循环，返回 `(rc, sleeps, scans, records)`（records = DEBUG 以上全部）。"""
        env = dict(self.ENV)
        env.update(kw.pop("env", {}))
        with self.assertLogs("yiban", level="DEBUG") as cm:
            rc, sleeps, _beats, scans = self._run(times or self.TIMES, env=env, **kw)
        return rc, sleeps, scans, cm.records


class IdleRoundNoiseTest(_NoiseHarness):
    """① 空转轮的轮次行不产 INFO（生产实测的 5 秒刷屏面）。"""

    def test_idle_rounds_log_info_once(self):
        _rc, sleeps, scans, records = self._logs(pool=True)
        self.assertEqual(len(scans), 2, "前置：窗口内应扫两轮")
        self.assertTrue(sleeps, "前置：空转轮必须真的等待（假时钟下记为睡步）")
        info = _level_lines(records, logging.INFO, ROUND_MARK)
        self.assertEqual(
            len(info), 1,
            f"空转轮每轮都打 INFO（{len(info)} 条）——生产实测 5 秒一对刷屏；"
            f"事实没变时只留一条：{info}")
        self.assertIn("[fallback r1]", info[0], "轮次行必须带稳定身份与轮次")

    def test_idle_rounds_still_emit_debug(self):
        """降噪是**降级**不是静默：空转轮在 DEBUG 下仍逐轮留痕。"""
        _rc, _sleeps, scans, records = self._logs(pool=True)
        self.assertEqual(len(scans), 2)
        debug = _level_lines(records, logging.DEBUG, ROUND_MARK)
        self.assertEqual(len(debug), len(scans) - 1,
                         "被降级的空转轮必须在 DEBUG 留有同一条留痕（否则等于静默）")

    def test_identity_tag_never_carries_the_host(self):
        """归因只回角色与槽位：身份原串带部署信息，任何日志都不得回串。"""
        _rc, _sleeps, _scans, records = self._logs(pool=True)
        joined = "\n".join(r.getMessage() for r in records)
        self.assertNotIn(self.TAG_HOST, joined, "日志里出现了身份原串里的主机名")
        self.assertNotIn(socket.gethostname(), joined, "日志里出现了本机名")

    def test_facts_change_logs_info_again(self):
        """反向控制：轮次事实变化（空转 → 有活 → 有活）必须重新出声。"""
        _rc, _sleeps, scans, records = self._logs(
            [_at(WED, (6, 35)), _at(WED, (6, 36)), _at(WED, (6, 37)), _at(WED, (8, 30))],
            pool=True, results=_RoundResults([0, 2, 2]))
        self.assertEqual(len(scans), 3, "前置：窗口内应扫三轮")
        info = _level_lines(records, logging.INFO, ROUND_MARK)
        self.assertEqual(len(info), 2,
                         f"事实变化（own 0→2）的轮次必须打 INFO，且只在那一次变化上：{info}")
        self.assertIn("[fallback r2]", info[1], "重新出声的那条必须带它自己的轮次号")


class RoundWaitTextTest(_NoiseHarness):
    """② 文案与真实间隔一致：打出来的秒数 = 本轮真实睡掉的秒数。"""

    def test_busy_round_reports_the_tight_interval_it_actually_sleeps(self):
        _rc, sleeps, scans, records = self._logs(
            pool=True, results={"13800000000": (False, "x", True, "skipped_window")})
        self.assertGreaterEqual(len(scans), 2)
        info = _level_lines(records, logging.INFO, ROUND_MARK)
        self.assertTrue(info)
        printed = _first_secs(info[0])
        self.assertEqual(printed, sleeps[0],
                         f"文案打的是 {printed}s，实际睡的是 {sleeps[0]}s——"
                         f"文案在声称代码没做的事")
        self.assertEqual(printed, 5, "有活干的轮应按 min(间隔, 5) 连扫")

    def test_idle_round_with_pool_reports_event_driven_bound(self):
        _rc, sleeps, scans, records = self._logs(pool=True)
        self.assertGreaterEqual(len(scans), 2)
        info = _level_lines(records, logging.INFO, ROUND_MARK)
        printed = _first_secs(info[0])
        self.assertIn("最多", info[0], "事件驱动等待是「最多等满间隔」——文案必须写成上界")
        ticks = -(-60 // 5)
        self.assertEqual(sleeps[:ticks], [5] * ticks, "事件驱动应走短轮询节拍")
        self.assertEqual(sum(sleeps[:ticks]), printed,
                         f"文案说最多 {printed}s，实际等了 {sum(sleeps[:ticks])}s")

    def test_idle_round_without_pool_reports_blind_interval(self):
        _rc, sleeps, scans, records = self._logs(pool=False)
        self.assertGreaterEqual(len(scans), 2)
        info = _level_lines(records, logging.INFO, ROUND_MARK)
        self.assertEqual(sleeps[0], _first_secs(info[0]),
                         "无池部署退回盲的整段间隔，文案秒数必须等于它")

    def test_window_not_open_line_keeps_its_real_wait(self):
        """既有的「窗口未开先等」行本来就与真实等待一致，降噪不得把它改坏。"""
        _rc, sleeps, _scans, records = self._logs(
            [_at(WED, (6, 5)), _at(WED, (6, 35)), _at(WED, (8, 30))])
        lines = [r.getMessage() for r in records if "尚未开始" in r.getMessage()]
        self.assertEqual(len(lines), 1, "窗口未开始应留一条等待行")
        self.assertEqual(_last_secs(lines[0]), sleeps[0], "等待秒数与实际睡步不一致")


class NoEarlyExitAfterSettleTest(_NoiseHarness):
    """③ 评估结论（**不做**）的钉子：settled 后不提前退出。

    兜底的存在理由是「窗口内随时可能还要兜」：学校晚放号、窗口内新审核通过的账号，在判据
    执行的那一刻**还不在账号列表里**（也没有队列行、没有状态记录）。「当前账号全了结 ⇒
    settled ⇒ 提前退出」会把这种情况判成了结并退出 ⇒ 那些账号当日无人接手；补签闸读的是
    同一份 `state_io.has_undone_accounts_today`，对新账号同样判不出「有活」。故窗口内不提前
    退出，空转的代价由①的降噪承担。

    本用例用项目**自己的**了结判据证明「确实已判 settled」，再证明兜底照旧按节律继续扫——
    若有人日后加上提前退出，这里必红。
    """

    PHONE = "13800000000"

    def _settled_state_dir(self):
        """写一份「全体已了结」的日状态文件，返回 `(目录, 业务日)`。"""
        tmp = tempfile.mkdtemp(prefix="yiban-fallback-settled-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        day = WED.strftime("%Y-%m-%d")
        with open(os.path.join(tmp, f"sign-state-{day}.json"), "w", encoding="utf-8") as f:
            f.write('{"%s": {"status": "success", "time": "07:29:00"}}' % self.PHONE)
        return tmp, day

    def test_settled_day_still_scans_inside_the_window(self):
        tmp, day = self._settled_state_dir()
        # 库侧显式打桩成「本部署没配库」：判据只读状态文件，前置才是确定的
        with mock.patch.object(state_io.db, "is_initialized", lambda: False), \
                mock.patch.object(state_io.db, "task_open_count", lambda d: 0):
            self.assertFalse(state_io.has_undone_accounts_today(state_dir=tmp, day=day),
                             "前置：这份状态文件必须被判成当日已了结（settled）")
        _rc, _sleeps, scans, _records = self._logs(
            env={"YIBAN_STATE_DIR": tmp}, pool=True)
        self.assertEqual(len(scans), 2,
                         "窗口内即便当日已判 settled 也必须继续扫"
                         "（窗口后期补放的号要靠它兜，补签闸对新账号判不出活）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
