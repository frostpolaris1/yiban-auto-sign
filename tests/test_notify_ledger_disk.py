# -*- coding: utf-8 -*-
"""回归测试：notify 每日预算磁盘持久化（跨进程共享额度）。

覆盖：
- 额度占用落盘：consume 后磁盘账本文件存在且计数正确；
- 跨"进程"恢复：重新加载模块状态（模拟另一进程）后仍读到已占用的计数；
- 跨日归零：日期切换后磁盘账本归零并写回；
- 退还落盘：refund 后磁盘计数回退；
- 双进程并发不超发：两个独立模块实例（模拟 web + signin）共享同一账本文件，
  合计占用不超过上限。

用法（项目根目录）：
    py -m pytest tests/test_batch15_notify_ledger_0831.py -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

KEY = "f" * 64
SCT_KEY = "SCT406257TESTTESTTESTTESTTEST"


class NotifyLedgerDiskTest(unittest.TestCase):
    """P2-3：额度磁盘账本（单进程内验证文件语义）。"""

    def setUp(self):
        # 内部账本名走子模块（旧的 scripts/notify.py 兼容壳已删除）
        from yiban.notify import ledger as notify
        self.notify = notify
        self.tmp = tempfile.mkdtemp(prefix="yiban-ledger-")
        self.env_file = os.path.join(self.tmp, "nope.env")
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ["YIBAN_ENV_FILE"] = self.env_file
        os.environ["YIBAN_ACCOUNTS_KEY"] = KEY
        notify._throttle_ts.clear()
        notify._general_daily["state"].update({"date": "", "count": 0})
        notify._urgent_daily["state"].update({"date": "", "count": 0})
        for ledger in (notify._general_daily, notify._urgent_daily):
            ledger["notice"].update({"pending": False, "notified": False, "warned": False})
        notify._skip_logged.clear()

    def tearDown(self):
        for k in ("YIBAN_STATE_DIR", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_KEY"):
            os.environ.pop(k, None)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ledger_file(self):
        return os.path.join(self.tmp, "notify-ledger.json")

    def _read_disk(self):
        with open(self._ledger_file(), encoding="utf-8") as f:
            return json.load(f)

    def test_consume_persists_to_disk(self):
        """占用后磁盘账本存在且计数=1。"""
        os.environ["YIBAN_NOTIFY_DAILY_MAX"] = "5"
        ticket = self.notify._consume_daily_budget("general")
        self.assertTrue(ticket.allowed)
        self.assertTrue(os.path.exists(self._ledger_file()))
        disk = self._read_disk()
        self.assertEqual(disk["general"]["count"], 1)
        self.assertEqual(disk["general"]["date"], self.notify._daily_today())

    def test_cross_process_reload_sees_consumed(self):
        """模拟另一进程：重新 import notify（清内存态）后仍读到已占用的计数。"""
        os.environ["YIBAN_NOTIFY_DAILY_MAX"] = "5"
        self.notify._consume_daily_budget("general")
        self.notify._consume_daily_budget("general")
        # 模拟新进程：清空模块级内存账本（新 import 会重建为初始值）
        self.notify._general_daily["state"].update({"date": "", "count": 0})
        # 新进程第一次 _roll_locked 应从磁盘恢复
        remaining = self.notify._daily_remaining("general")
        self.assertEqual(remaining, 3, "磁盘计数 2，上限 5，剩余应为 3（跨进程不重置）")

    def test_daily_rollover_resets_disk(self):
        """跨日：新日期首次调用归零磁盘账本。"""
        os.environ["YIBAN_NOTIFY_DAILY_MAX"] = "5"
        day1 = ("2026-08-31",)
        with mock.patch.object(self.notify, "_daily_today", lambda: day1[0]):
            self.notify._consume_daily_budget("general")
            self.notify._consume_daily_budget("general")
        day2 = ("2026-09-01",)
        with mock.patch.object(self.notify, "_daily_today", lambda: day2[0]):
            remaining = self.notify._daily_remaining("general")
        self.assertEqual(remaining, 5, "跨日后应归零重新计数")

    def test_refund_persists_to_disk(self):
        """退还后磁盘计数回退。"""
        os.environ["YIBAN_NOTIFY_DAILY_MAX"] = "5"
        ticket = self.notify._consume_daily_budget("general")
        self.notify._refund_daily_budget(ticket)
        disk = self._read_disk()
        self.assertEqual(disk["general"]["count"], 0, "退还后磁盘计数回退为 0")

    def test_two_module_instances_share_quota(self):
        """双模块实例（模拟 web + signin 两进程）共享同一账本：合计不超过上限。"""
        os.environ["YIBAN_NOTIFY_DAILY_MAX"] = "3"
        # 实例 A（进程 1）
        sent_a = []
        for i in range(2):
            t = self.notify._consume_daily_budget("general")
            self.assertTrue(t.allowed, f"A 第 {i+1} 条应允许")
            sent_a.append(t)
        # 实例 B（进程 2）：把实现包从 sys.modules 里整体摘掉再导入，等价于
        # "新进程从零起"（内存账本为空，只能从磁盘恢复已占用计数）
        #
        # ⚠ 必须还原：摘掉再导入会让本进程里"先前导入方持有的旧模块对象"与
        # "函数内重新解析到的新对象"同时存在（两份账本实例）。不还原就会污染
        # 同进程后续用例——表现为 `test_notify_webhook.py` 的跨日退还用例在全量
        # 运行下稳定失败（`daily_remaining` 读到陈旧计数，先跑本文件才复现）。
        saved_modules = {k: v for k, v in sys.modules.items()
                         if k.startswith("yiban.notify")}

        def _restore_notify_modules():
            for k in [k for k in sys.modules if k.startswith("yiban.notify")]:
                del sys.modules[k]
            sys.modules.update(saved_modules)

        self.addCleanup(_restore_notify_modules)
        for k in list(saved_modules):
            del sys.modules[k]
        from yiban.notify import ledger as notify_b
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ["YIBAN_ENV_FILE"] = self.env_file
        os.environ["YIBAN_ACCOUNTS_KEY"] = KEY
        # B 第一次占用应从磁盘读到已用 2/3
        t = notify_b._consume_daily_budget("general")
        self.assertTrue(t.allowed, "B 第 1 条应允许（合计 3/3）")
        t2 = notify_b._consume_daily_budget("general")
        self.assertFalse(t2.allowed, "合计已达 3/3，B 第 2 条应被拒（共享额度不超发）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
