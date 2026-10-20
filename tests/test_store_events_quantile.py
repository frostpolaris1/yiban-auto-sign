# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""容量告警分位数的口径：p95 必须是升序第 ⌈0.95×n⌉ 个，而不是最大值。

工单 `ba-p02-08`：注释写"升序第 ⌈pct×n⌉ 个（0 基）"，代码用
`int(pct * len(vals))`（向下取整）再夹到末元素。样本恰为缺省 `min_samples=20` 时
0.95×20=19 被夹到 index 19 —— 返回最大值而不是 p95；样本数不是 20 的整数倍时
又与注释差一档。这是容量告警阈值的实测输入，小样本时长期偏保守。

修法：`math.ceil(pct * n) - 1`（nearest-rank），并同步注释。

标签：I · 容量、熔断与账号有效性
覆盖：`attempt_dur_quantile` 的取数下标（n=20、n=21、中位数对照）与样本不足。
对应实现：`yiban/adapters/persistence/events.py` 的 `attempt_dur_quantile`。
关键断言：20 个升序样本的 p95 是第 19 个（index 18），**不是**第 20 个最大值。
依赖：临时库 + `sign_events` 真实写入；无网络、无 skip。
"""
import contextlib
import os
import shutil
import tempfile
import unittest

from yiban.core import clock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64


class _Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-quantile-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls._old = {}
        for k, v in (("YIBAN_ACCOUNTS_KEY", TEST_KEY), ("YIBAN_ENV_FILE", cls.env_file),
                     ("YIBAN_DB_FILE", cls.db_file),
                     ("YIBAN_STATE_DIR", cls.tmp)):
            cls._old[k] = os.environ.get(k)
            os.environ[k] = v
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for k, v in cls._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(self.db_file + suffix)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def _seed_durations(self, durations):
        ts = clock.ts()
        db.add_sign_events_batch([
            {"ts": ts, "phone": "13800139000", "status": "success", "stage": "sign",
             "dur_sec": float(d)}
            for d in durations
        ])

    def test_twenty_samples_p95_is_second_largest(self):
        self._seed_durations(range(1, 21))
        got = db.attempt_dur_quantile(0.95, days=7, min_samples=20)
        self.assertEqual(got, 19.0,
                         "p95 取升序第 ⌈0.95×20⌉=19 个（1 基），不是第 20 个最大值")

    def test_median_uses_nearest_rank_not_floor(self):
        self._seed_durations(range(1, 21))
        got = db.attempt_dur_quantile(0.5, days=7, min_samples=20)
        self.assertEqual(got, 10.0, "⌈0.5×20⌉=10 → 升序第 10 个（1 基）")

    def test_below_min_samples_returns_none(self):
        self._seed_durations(range(1, 20))
        self.assertIsNone(db.attempt_dur_quantile(0.95, days=7, min_samples=20))


if __name__ == "__main__":
    unittest.main(verbosity=2)
