# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""校验者：弃权分档词表（`retry:`/`final:` 前缀与"默认可再领"集合）唯一家在 `yiban.status`。

128-P1 把词表自 `yiban.store.claims`（D3a 退役对象，函数已无生产调用点）迁入状态码
词汇表：判据与成员（`STATUS_SKIPPED_WINDOW` / `STATUS_SKIPPED_NORANGE` /
`STATUS_NO_POSITION`）同文件，"唯一一份"才钉得住。本文件钉三件事，任何一条红都
意味着词表出现了第二份名字：

1. `yiban.status` 拥有三名且**值**不变（值是 `sign_tasks.result` 列的下游契约，
   `queue_store.requeue_failed` 的当日回炉口按它分档）；
2. `yiban.store.claims` 不再拥有它们——复活即"记成 retry、判成 final"的漂移起点；
3. `yiban.store.db` 门面不再导出 `CLAIM_*` 词表别名（名字唯一，防第三条读取路径）。

分档判据与执行侧同源的断言（`executor_v3._tier_prefix` 逐成员对照）由
`test_v3_same_day_requeue` 的同名校验者继续承担，此处不重复。
"""
import importlib
import unittest


class ResultVocabHomeTest(unittest.TestCase):
    """词表唯一家与旧家清零（128-P1 校验者）。"""

    def test_status_owns_the_vocabulary(self):
        status = importlib.import_module("yiban.core.status")
        self.assertEqual(status.RESULT_RETRY_PREFIX, "retry:")
        self.assertEqual(status.RESULT_FINAL_PREFIX, "final:")
        self.assertEqual(
            status.RETRYABLE_GIVE_UP_STATUSES,
            frozenset({"skipped_window", "skipped_norange", "no_position"}),
        )

    def test_claims_no_longer_owns_the_vocabulary(self):
        claims = importlib.import_module("yiban.store.claims")
        for name in ("RESULT_RETRY_PREFIX", "RESULT_FINAL_PREFIX",
                     "RETRYABLE_GIVE_UP_STATUSES"):
            self.assertFalse(
                hasattr(claims, name),
                f"词表复活在 yiban.store.claims.{name}——第二份判据会漂移",
            )

    def test_db_facade_no_longer_reexports_the_vocabulary(self):
        db = importlib.import_module("yiban.store.db")
        for name in ("CLAIM_RESULT_RETRY_PREFIX", "CLAIM_RESULT_FINAL_PREFIX",
                     "CLAIM_RETRYABLE_GIVE_UP_STATUSES"):
            self.assertFalse(
                hasattr(db, name),
                f"词表别名复活在 yiban.store.db.{name}——第三条读取路径",
            )


if __name__ == "__main__":
    unittest.main()
