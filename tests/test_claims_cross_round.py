# -*- coding: utf-8 -*-
"""领取池的跨轮上限：弃权原因分档与「显式路径才可再领」。

标签：B · 调度：领取/队列/执行体
覆盖：`claims.give_up` 的原因分档（result 字段前缀协议，无库迁移）：预算耗尽/风控类落
   `final:` 档，窗口外/无点位落 `retry:` 档；`claims.try_claim` 的 `allow_failed` 显式
   参数（默认关，等价 allow_settled 那一档）；收尸路径（`reap_unreported` /
   `reap_abandoned`）默认仍可再领；崩溃留下的 `claimed` 行按租约接管不受影响；
   `round.run_queue_retry` 把补签轮/手动两条**有界**显式路径接到 `allow_failed` 上；兜底常驻
   是无界循环、取默认（不接），否则预算耗尽档在窗口内每轮重登一次。
对应实现：yiban/store/claims.py（try_claim / give_up / reap_unreported / reap_abandoned /
   RESULT_RETRY_PREFIX / RESULT_FINAL_PREFIX / RETRYABLE_GIVE_UP_STATUSES）、
   yiban/engine/round.py（_claim / _settle_claims / retry_failed）。
关键断言：**预算耗尽而弃权的账号，当日不得被默认参数再领**——旧行为下 `failed` 与
   `claimed` 同列"可再接手"，后面每一轮（补签轮、兜底常驻、别的执行体）都会把它重领一遍
   重走登录+签到，无上限。修法是把"该重试"（窗口外/无点位）与"不该无上限重试"
   （预算耗尽/风控）在领取层分开：前者默认放行，后者必须走**有界**显式路径（补签轮/手动）——兜底常驻是无界
   循环、取默认，不得作为这条路径。
   判据的输入由被判对象写（result 前缀），故每条活体反例都自带"把结果改坏 ⇒ 默认再领必须红"
   的方向（见 MF-91）。收尸行必须仍默认可领，否则崩溃账号当天再也签不上。
依赖：临时 sqlite（每用例重建）+ 冻结业务时钟 + 真两轮领取（无子进程、无网络）。
"""

import contextlib
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402

from yiban.store import claims  # noqa: E402

TEST_KEY = "a" * 64
DAY = "2026-09-16"
PHONE = "13800138000"
OWNER_A = "hostA:100:090000"
OWNER_B = "hostB:200:090001"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-claims-round-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
        })

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def tearDown(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def _row(self, phone=PHONE):
        return dict(db.get_conn().execute(
            "SELECT * FROM sign_claims WHERE phone=? AND day=?", (phone, DAY)).fetchone())


class GiveUpReasonTierTest(_Base):
    """弃权原因分档：预算/风控档默认不可再领，窗口外/无点位档默认可再领。"""

    def test_budget_give_up_is_final_and_default_reclaim_refused(self):
        """默认参数再领必须 False；显式 allow_failed 才 True，且 attempts 递增可查。"""
        ok, e1 = db.claim_sign_account(PHONE, DAY, OWNER_A)
        self.assertTrue(ok)
        self.assertTrue(db.claim_give_up(PHONE, DAY, OWNER_A, "重试耗尽", epoch=e1))
        row = self._row()
        self.assertEqual(row["state"], db.CLAIM_STATE_FAILED)
        self.assertTrue(row["result"].startswith(claims.RESULT_FINAL_PREFIX),
                        f"预算耗尽档必须带 final 前缀，实际 {row['result']!r}")

        self.assertEqual(db.claim_sign_account(PHONE, DAY, OWNER_B), (False, 0),
                         "预算耗尽弃权后，默认参数不得再领（旧行为会无上限重来一遍）")
        ok2, e2 = db.claim_sign_account(PHONE, DAY, OWNER_B, allow_failed=True)
        self.assertTrue(ok2, "有界显式路径（补签轮/手动）才可再领")
        self.assertGreater(e2, e1, "再领取换新代 token")
        self.assertEqual(self._row()["attempts"], 1,
                         "attempts 必须递增可查（第一次领取记 0，再领自增到 1）")

    def test_window_out_give_up_is_retryable_by_default(self):
        """窗口外（该重试）默认可再领——否则补签轮/兜底在窗口重开时领不到。"""
        ok, e1 = db.claim_sign_account(PHONE, DAY, OWNER_A)
        self.assertTrue(ok)
        self.assertTrue(db.claim_give_up(PHONE, DAY, OWNER_A, "skipped_window",
                                         epoch=e1, retryable=True))
        self.assertTrue(self._row()["result"].startswith(claims.RESULT_RETRY_PREFIX),
                        "窗口外档必须带 retry 前缀")
        ok2, _e2 = db.claim_sign_account(PHONE, DAY, OWNER_B)
        self.assertTrue(ok2, "窗口外弃权的账号当日应可被默认参数再接手")

    def test_no_position_status_is_in_retryable_tier(self):
        """无点位属"该重试"侧：领取层要按同一份档位表分派，不能各写一份。"""
        self.assertIn("no_position", claims.RETRYABLE_GIVE_UP_STATUSES)
        self.assertIn("skipped_window", claims.RETRYABLE_GIVE_UP_STATUSES)
        self.assertIn("skipped_norange", claims.RETRYABLE_GIVE_UP_STATUSES)
        self.assertNotIn("failed", claims.RETRYABLE_GIVE_UP_STATUSES,
                         "预算耗尽的 failed 不在默认可再领档")

    def test_reap_paths_stay_reclaimable_by_default(self):
        """收尸行必须仍默认可领：崩溃/无结论不等于"预算耗尽"，否则当天再也签不上。"""
        ok, e1 = db.claim_sign_account(PHONE, DAY, OWNER_A)
        self.assertTrue(ok)
        claims.reap_unreported(OWNER_A, {PHONE: (DAY, e1)}, set())
        self.assertEqual(self._row()["state"], db.CLAIM_STATE_FAILED)
        self.assertTrue(self._row()["result"].startswith(claims.RESULT_RETRY_PREFIX),
                        "轮末收尸行必须落 retry 档")
        self.assertTrue(db.claim_sign_account(PHONE, DAY, OWNER_B)[0],
                        "收尸行默认就该能被下一轮接手")

    def test_reap_abandoned_stays_reclaimable_by_default(self):
        """监督进程对死执行体的收尸同理：默认可领（e2e 组 3 的既有前提不变）。"""
        ok, _e1 = db.claim_sign_account(PHONE, DAY, OWNER_A)
        self.assertTrue(ok)
        self.assertEqual(claims.reap_abandoned(OWNER_A, day=DAY), 1)
        self.assertTrue(self._row()["result"].startswith(claims.RESULT_RETRY_PREFIX))
        self.assertTrue(db.claim_sign_account(PHONE, DAY, OWNER_B)[0])

    def test_claimed_expired_lease_takeover_unaffected(self):
        """崩溃自愈不受影响：`claimed` 行仍按租约接管，与本条分档无关。"""
        self.assertTrue(db.claim_sign_account(PHONE, DAY, OWNER_A)[0])
        self.assertTrue(db.claim_sign_account(PHONE, DAY, OWNER_B, lease_sec=0)[0],
                        "租约过期接管必须照旧放行")


class CrossRoundClaimE2ETest(_Base):
    """临时库 + 两轮真领取：第二轮默认领不到、显式路径领到、attempts 递增。"""

    def test_two_real_rounds_budget_giveup(self):
        # 第一轮：领取 → 收敛为预算耗尽弃权
        ok, e1 = db.claim_sign_account(PHONE, DAY, "round1:1:090000")
        self.assertTrue(ok)
        db.claim_give_up(PHONE, DAY, "round1:1:090000", "failed", epoch=e1)

        # 第二轮（默认参数，模拟后面的补签/兜底/别的执行体的"照旧派遣"）：领不到
        self.assertEqual(db.claim_sign_account(PHONE, DAY, "round2:2:090100"), (False, 0))

        # 第二轮（显式路径，模拟补签轮/手动）：领到，且 attempts 从 0 递增
        ok3, e3 = db.claim_sign_account(PHONE, DAY, "round2:2:090100", allow_failed=True)
        self.assertTrue(ok3)
        self.assertGreater(e3, e1)
        self.assertEqual(self._row()["attempts"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
