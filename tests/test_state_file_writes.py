# -*- coding: utf-8 -*-
"""状态文件写盘的原子性（DAT-7）与"标记损坏"的失效方向。

缺陷背景：容器调度的时段标记 `sched-slot-<kind>-<date>.json` 原先用
`open(path, "w")` 直写。容器在写入中途被杀会留下**半截 JSON**，
`_slot_done` 的 `json.load` 恒失败 → 判定为"本时段没跑过"，而 `hm >= FIRST/SECOND`
是无上界判定 → 再触发一轮全站登录（幂等但多一轮真实请求，且覆盖当日已 success 的
状态文件）。signin 侧所有状态文件写入早已是 tmp + `os.replace`，只有容器调度这一处
漏了。

判据（本文件锁住）：
1. 标记写入必须经过 `os.replace`（原子替换），且落盘内容可解析；
2. 替换失败（被杀/磁盘满）时**标记路径不得存在半截文件**——失效方向必须是
   "标记缺失 → 退化为既有闩锁语义"，而不是"文件损坏 → 每次 json.load 都失败"；
3. 半成品临时文件不残留（失败路径自行清理；被杀残留的由 state_gc 按 mtime 清）。
"""
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock as mock
from typing import ClassVar

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import scheduler  # noqa: E402  （docker/scheduler.py）

from yiban import state_gc  # noqa: E402


class SlotMarkerAtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-slot-")
        p = mock.patch.object(scheduler, "STATEDIR", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _marker(self, kind="first"):
        return scheduler._slot_marker(kind)

    def test_mark_slot_replaces_atomically(self):
        real_replace = os.replace
        calls = []

        def _spy(src, dst):
            calls.append((src, dst))
            real_replace(src, dst)

        with mock.patch.object(scheduler.os, "replace", _spy):
            scheduler._mark_slot("first")
        self.assertEqual(len(calls), 1, "标记必须经 os.replace 原子落盘")
        src, dst = calls[0]
        self.assertEqual(dst, self._marker("first"))
        self.assertTrue(src.startswith(dst), f"临时名应基于标记名: {src}")
        with open(dst, encoding="utf-8") as f:
            self.assertIn("triggered_at", json.load(f))
        self.assertTrue(scheduler._slot_done("first"))

    def test_failed_replace_leaves_no_partial_marker(self):
        """替换失败时标记路径必须**不存在**（而不是存在但内容损坏）。"""
        with mock.patch.object(scheduler.os, "replace",
                               side_effect=OSError("killed mid-write")):
            scheduler._mark_slot("second")
        self.assertFalse(os.path.exists(self._marker("second")),
                         "半截文件会让 _slot_done 每次解析失败，失效方向错")
        self.assertFalse(scheduler._slot_done("second"), "读不到即视为未跑过（既有闩锁语义）")
        leftovers = [n for n in os.listdir(self.tmp) if ".tmp" in n]
        self.assertEqual(leftovers, [], "失败路径必须清掉自己的半成品")

    def test_interrupted_write_is_swept_later(self):
        """被杀（SIGKILL）留下的半成品没有机会自清 → 由状态清理按 mtime 兜底。"""
        tmp_file = self._marker("first") + ".tmp424242"
        with open(tmp_file, "w", encoding="utf-8") as f:
            f.write('{"triggered')          # 半截 JSON
        os.utime(tmp_file, (1, 1))         # 1 = 1970，远早于 1 天阈值
        removed, detail = state_gc.sweep(self.tmp)
        self.assertEqual(removed, 1, detail)
        self.assertFalse(os.path.exists(tmp_file))

    def test_corrupt_marker_is_treated_as_not_done(self):
        """坏文件（历史遗留/手工改动）必须退化为"未跑过"，不得抛异常打断调度循环。"""
        with open(self._marker("first"), "w", encoding="utf-8") as f:
            f.write('{"triggered_at": ')
        self.assertFalse(scheduler._slot_done("first"))
        self.assertFalse(scheduler._slot_done("never-written"))


class SigninWritesAreAtomicTest(unittest.TestCase):
    """signin 侧的状态文件写入同口径（本项目防止该类回归的既有约定）。

    引擎按"执行一轮"的边界切分后，这些写入各自落在实现模块里：按日状态与全量收尾标记
    在 `yiban/engine/state_io.py`，探针状态在 `probe.py`，用户失败邮件额度账本在
    `alerts.py`，兼容壳 `scripts/signin.py` 只剩转发。故断言直接读实现模块，并额外锁住
    "实现只有一份"——壳里再出现同名定义就是两份实现，改一份另一份照旧跑。
    """

    #: 状态文件写入函数 → 实现所在文件（相对仓库根）
    WRITERS: ClassVar[dict] = {
        "_write_sign_state": "yiban/engine/state_io.py",
        "_write_sched_done": "yiban/engine/state_io.py",
        "_write_probe_state": "yiban/engine/probe.py",
        "_user_fail_mail_reserve": "yiban/engine/alerts.py",
    }

    def _read(self, rel):
        with open(os.path.join(BASE, *rel.split("/")), encoding="utf-8") as f:
            return f.read()

    def test_signin_state_writers_use_replace(self):
        for func, rel in self.WRITERS.items():
            with self.subTest(func=func):
                src = self._read(rel)
                body = src.split(f"def {func}(")[-1][:2500] if f"def {func}(" in src else ""
                self.assertTrue(body, f"{func} 不在 {rel}（断言会恒真，需同步改名）")
                self.assertIn("os.replace", body, f"{func} 未用临时名原子替换")

    def test_writers_have_single_implementation(self):
        """同上四处不得在兼容壳里留下第二份定义。"""
        shell = self._read("scripts/signin.py")
        for func in self.WRITERS:
            with self.subTest(func=func):
                self.assertNotIn(
                    f"def {func}(", shell,
                    f"scripts/signin.py 又出现了一份 {func}（应为转发到 yiban/engine/）",
                )

    def test_cred_state_write_is_delegated(self):
        """熔断状态文件的原子写归 `yiban/cred_state.py`（唯一读写入库）；
        引擎不得自己再写一份（原子性与并发由 tests/test_cred_state_concurrency.py 钉住）。"""
        src = self._read("yiban/engine/state_io.py")
        # 只取本函数体（到下一个顶层 def 为止）——按字符数截取会把相邻函数一起断言
        body = src.split("def _save_cred_state(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("cred_state.update(", body)
        self.assertIn("cred_state.merge(", body)
        self.assertNotIn("open(", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
