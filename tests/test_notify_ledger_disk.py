# -*- coding: utf-8 -*-
"""批次15 P2-3 回归测试：notify 每日预算磁盘持久化（跨进程共享额度）。

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
sys.path.insert(0, os.path.join(BASE, "scripts"))

KEY = "f" * 64
SCT_KEY = "SCT406257TESTTESTTESTTESTTEST"


class NotifyLedgerDiskTest(unittest.TestCase):
    """P2-3：额度磁盘账本（单进程内验证文件语义）。"""

    def setUp(self):
        import notify
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
        # 实例 B（进程 2）：重新加载模块
        for k in list(sys.modules):
            if k.startswith("notify"):
                del sys.modules[k]
        sys.path.insert(0, os.path.join(BASE, "scripts"))
        import notify as notify_b
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
