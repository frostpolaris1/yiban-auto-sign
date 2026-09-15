# -*- coding: utf-8 -*-
"""账密熔断状态文件的读-改-写原子性（D-4：SCH-6 / DAT-2）。

**缺陷**：三个写入方（签到全量轮、签到 `--only` 轮、Web 端改密/编辑后清除熔断）都做
"读 → 改 → 写"，但只有**写动作**内部持锁，读改写整体不原子：

- 签到进程从启动起持有一份内存快照，收尾整体覆盖 → 运行期间 Web 端刚清掉的暂停被写回，
  该账号继续用错密码登录（加重风控）（SCH-6）；
- Web 端 `clear_fuse_pause` 自己读整个文件、删一条、再整体写回，**完全不持锁** →
  按自己的读取结果重写会抹掉签到进程并发写入的其他账号记录（DAT-2）。

**修法**：`yiban/cred_state.py` 为唯一读写入口——整段读-改-写同锁，且保存按手机号
**增量合并**（调用方只声明"改了哪些账号"，其余账号以磁盘最新值为准）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import signin  # noqa: E402

from yiban import cred_state  # noqa: E402

P1, P2 = "13800138000", "13800138001"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-cred-")
        self.env = dict(os.environ)
        os.environ["YIBAN_STATE_DIR"] = self.tmp

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, mapping):
        with open(cred_state.path(), "w", encoding="utf-8") as f:
            json.dump(mapping, f)

    def _disk(self):
        return cred_state.read()


class IncrementalMergeTest(_Base):
    """签到侧：内存快照不得整体覆盖磁盘（否则抹掉并发写入）。"""

    def test_merge_keeps_untouched_accounts(self):
        self._write({P1: {"fail_days": 9, "paused_since": "2026-09-01"}})
        # 签到进程跑完后只声明 P2 的变化（P1 是别人并发写的，必须原样保留）
        signin._save_cred_state(
            {P2: {"fail_days": 1, "last_fail": "2026-09-15"}}, touched={P2})
        disk = self._disk()
        self.assertEqual(disk[P1]["fail_days"], 9, "未处理账号的记录不得被覆盖")
        self.assertEqual(disk[P2]["fail_days"], 1)

    def test_merge_deletes_only_touched(self):
        """本次处理账号成功 → 删除其记录；其他账号不受影响。"""
        self._write({P1: {"fail_days": 9, "paused_since": "2026-09-01"},
                     P2: {"fail_days": 2}})
        signin._save_cred_state({P2: {"fail_days": 2}}, touched={P1})
        disk = self._disk()
        self.assertNotIn(P1, disk, "本次处理且已成功的账号应清除记录")
        self.assertEqual(disk[P2]["fail_days"], 2)

    def test_stale_snapshot_cannot_resurrect_pause(self):
        """SCH-6 主场景：Web 端清掉暂停后，签到收尾的旧快照不得把它写回来。"""
        self._write({P1: {"fail_days": 3, "paused_since": "2026-09-01"}})
        stale_snapshot = {P1: {"fail_days": 3, "paused_since": "2026-09-01"}}  # 启动时读到的
        cred_state.clear(P1)  # 用户改密 → Web 端清除
        # 签到进程收尾：它**没有处理** P1（不在 touched 里），旧快照不得写回
        signin._save_cred_state(stale_snapshot, touched=set())
        self.assertNotIn(P1, self._disk(), "运行期间清除的暂停被旧快照复活了")

    def test_legacy_full_replace_still_works(self):
        """兼容入口（touched=None）保持整体覆盖语义；空数据删除文件。"""
        self._write({P1: {"fail_days": 1}})
        signin._save_cred_state({P2: {"fail_days": 5}})
        disk = self._disk()
        self.assertNotIn(P1, disk)
        self.assertEqual(disk[P2]["fail_days"], 5)
        signin._save_cred_state({})
        self.assertFalse(os.path.exists(cred_state.path()),
                         "无记录 = 文件不存在（既有语义）")


class WebConcurrentEditTest(_Base):
    """Web 侧：清除熔断不得抹掉并发写入的其他账号记录（DAT-2）。"""

    def _clear_via_web(self, phone):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "webapp", os.path.join(BASE, "web", "app.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("webapp", mod)
        with mock.patch.dict(os.environ, {"YIBAN_STATE_DIR": self.tmp}):
            spec.loader.exec_module(mod)
        mod.clear_fuse_pause(phone)

    def test_web_clear_blocks_on_same_lock_as_signin(self):
        """Web 的清除与签到的保存共用同一把锁 → 两者不可能交错（DAT-2 的根治）。

        验证方式：先在主线程持有该文件锁（等价于"签到进程正在保存"），再从另一线程
        调 Web 的清除——它必须**等锁**而不是直接读改写；释放后清除完成，
        且等待期间别的进程写进来的记录仍然在。
        """
        import threading

        import locks
        self._write({P1: {"fail_days": 3, "paused_since": "2026-09-01"}})
        done = threading.Event()

        def _clear():
            self._clear_via_web(P1)
            done.set()

        with locks.file_lock(cred_state.path()):
            # 锁持有期间（签到在保存）：写入 P2 模拟其落盘结果
            disk = cred_state.read()
            disk[P2] = {"fail_days": 4, "paused_since": "2026-09-15"}
            with open(cred_state.path(), "w", encoding="utf-8") as f:
                json.dump(disk, f)
            t = threading.Thread(target=_clear, daemon=True)
            t.start()
            self.assertFalse(done.wait(0.3), "Web 清除必须在同一把锁上等待，不得绕过")
        self.assertTrue(done.wait(5), "释放锁后 Web 清除应完成")
        t.join(timeout=5)
        final = self._disk()
        self.assertNotIn(P1, final, "目标账号的暂停应被清除")
        self.assertIn(P2, final, "锁外写入的其他账号记录不得被 Web 覆盖")

    def test_clear_missing_entry_is_noop(self):
        """文件不存在/无该账号：静默无操作（用户每次编辑账号都会走这里）。"""
        self.assertFalse(cred_state.clear(P1))
        self._write({P1: {"fail_days": 1}})
        self.assertFalse(cred_state.clear(P2))
        self.assertIn(P1, self._disk())


class LockIsUsedTest(_Base):
    """整段读-改-写必须在同一把跨进程锁内完成。"""

    def test_update_holds_file_lock(self):
        import locks
        self._write({P1: {"fail_days": 1}})
        seen = []
        real_lock = locks.file_lock

        def spy(path, *a, **kw):
            seen.append(os.path.basename(path))
            return real_lock(path, *a, **kw)

        with mock.patch.object(cred_state.locks if hasattr(cred_state, "locks") else locks,
                               "file_lock", side_effect=spy):
            cred_state.clear(P1)
        self.assertIn("cred-state.json", seen, "读-改-写须经统一文件锁原语")

    def test_signin_save_path_also_locks(self):
        import locks
        seen = []
        real_lock = locks.file_lock

        def spy(path, *a, **kw):
            seen.append(os.path.basename(path))
            return real_lock(path, *a, **kw)

        with mock.patch.object(locks, "file_lock", side_effect=spy):
            signin._save_cred_state({P1: {"fail_days": 1}}, touched={P1})
        self.assertIn("cred-state.json", seen)


if __name__ == "__main__":
    unittest.main(verbosity=2)
