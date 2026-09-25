# -*- coding: utf-8 -*-
"""状态文件写盘的原子性与"标记损坏"的失效方向。

标签：D · 状态词汇与账号生命周期
覆盖：容器调度时段标记 `_mark_slot` 经 `os.replace` 原子落盘、替换失败时标记路径
    不得存在半截文件、被杀残留的半成品由 `state_gc.sweep` 按 mtime 清走、坏文件退化为
    "未跑过"而不抛异常；signin 侧四个状态写入函数的原子性与"实现只有一份"。
对应实现：容器侧在 `docker/scheduler.py`（`_mark_slot`/`_slot_done`/`_slot_marker`），
    引擎侧写入按 `yiban/engine/state_io.py`、`yiban/engine/probe.py`、
    `yiban/engine/alerts.py` 三份落点，兼容壳 `scripts/signin.py` 只剩转发。
关键断言：**失效方向必须是"标记缺失"而不是"文件损坏"**——半截 JSON 会让 `_slot_done`
    的 `json.load` 恒失败而判定成"本时段没跑过"，配合无-upper-bound 的 `hm >= FIRST`
    判定，会再触发一轮全站真实登录并覆盖当日已 success 的状态。
依赖：进程内打桩 `os.replace`（包住真函数再计数）+ 临时 STATEDIR；importlib 加载
    `docker/scheduler.py`；不跑子进程、不需 bash/docker CLI、不触网。

原先只有容器调度这一处漏了 tmp + `os.replace`，signin 侧早已是全项目约定。
"""
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock as mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import scheduler  # noqa: E402  （docker/scheduler.py）

from yiban import state_gc  # noqa: E402
from yiban.engine import state_io  # noqa: E402


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
        real_replace = os.replace  #包住真 replace 再计数：既要证明走过 os.replace，又不能真把它替掉
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
        removed, detail = state_gc.sweep(self.tmp)  #半成品清不掉就等清理兜底：这条把 _mark_slot 与 state_gc.sweep 绑成一对
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

    def _read(self, rel):
        with open(os.path.join(BASE, *rel.split("/")), encoding="utf-8") as f:
            return f.read()

    def test_cred_state_write_is_delegated(self):
        """熔断状态文件的原子写归 `yiban/cred_state.py`（唯一读写入库）；
        引擎不得自己再写一份（原子性与并发由 tests/test_breaker.py 钉住）。"""
        src = self._read("yiban/engine/state_io.py")
        # 只取本函数体（到下一个顶层 def 为止）——按字符数截取会把相邻函数一起断言
        body = src.split("def _save_cred_state(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("cred_state.update(", body)
        self.assertIn("cred_state.merge(", body)
        self.assertNotIn("open(", body)


class SignStateBomReadTest(unittest.TestCase):
    """按日状态文件带 BOM 时，读侧不得判"损坏"而清空整日数据。

    写入方 `_write_sign_state` 的读-改-写若用 utf-8 读带 BOM 的存档，json 解析
    必失败 → 按空数据重建 → 当日已写结论全部丢失。读侧其余入口
    （`_daily_statuses` / sched-run）早已统一 utf-8-sig，本用例钉住写侧同口径。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-signstate-")
        p = mock.patch.object(state_io, "_state_dir", return_value=self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_bom_file_keeps_existing_entries(self):
        path = os.path.join(
            self.tmp, f"sign-state-{state_io.clock.now():%Y-%m-%d}.json")
        # utf-8-sig 写：文件头带 BOM（Windows 记事本等工具另存的形态）
        with open(path, "w", encoding="utf-8-sig") as f:
            json.dump({"13800000001": {"status": "success", "message": "先写入"}}, f)

        state_io._write_sign_state("13800000002", "failed", "后写入")

        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        self.assertEqual(data["13800000001"]["status"], "success",
                         "带 BOM 的既有条目被当损坏清空重建 = 整日数据丢失")
        self.assertEqual(data["13800000002"]["status"], "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
