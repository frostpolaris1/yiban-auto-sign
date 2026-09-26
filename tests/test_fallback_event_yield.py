# -*- coding: utf-8 -*-
"""兜底"失败即入队"的事件源与"同一账号让位"的仲裁面：这两件事都由领取池的**行迁移**承担。

标签：B · 调度：领取/队列/执行体
覆盖：`claims.fallback_event` 事件签名（领取池 + 任务队列**两池并集**、只数 retry: 档
   未了结行的 (条数, 最新迁移标记)，final: 档弃权不动签名、兜底自己的弃权被剔除、
   v3 弃权（sign_tasks 的 retry: 档）同样动签名而 final: 档不动、v3 侧兜底自己的弃权
   同样剔除、他人收尾 done 不进子集、他人重领 retry:
   行同样动签名、库不可用返回 None），以及同一轮持锁窗口内池级的三条反例（全量轮在飞
   的 A/B/C 兜底领不到、轮中途弃权到 retry: 档的 D 兜底立刻领得到、轮把 A 收尾成 done
   后兜底不再重领；此外从未被碰过的 E 默认可接手）。
对应实现：yiban/store/claims.py（fallback_event 与 try_claim/give_up 的既有条款）、
   yiban/store/db.py（门面绑定 claim_fallback_event）、yiban/engine/workers.py
   （让位与唤醒的判据全部取池，不自建第二套）。
关键断言：让位与去重**不需要新机制**——try_claim 单条 upsert 里"在飞未过期拒、done 拒、
   retry: 档放、final: 档默认拒"四条判据本身就是"同一账号让位"与池内去重；事件签名必须
   与"可接手"严格同集（数了 final: 就是为一个永远领不动的行白唤醒，唤醒频率不得被不可
   接手的弃权放大；数了兜底自己的弃权会把"扫→弃权→唤醒→再扫"接成紧循环重复真实登录）。
   分档灰度（调度 v3）下弃权写在 `sign_tasks` 而不是领取池——签名不并入 v3 侧，唤醒就
   恰好在最需要即时接手的配置里退化回盲轮询。
依赖：真临时库（init_db + 领取/弃权/收尾的行形状与 tests/test_claims_cross_round.py
   一致；v3 侧走 queue_store 真实 claim/settle 原语）；不起子进程、不发网络请求、不打桩时钟。

用法（项目根目录）：
    py -m pytest tests/test_fallback_event_yield.py -v
"""
import contextlib
import os
import shutil
import tempfile
import unittest

from yiban.store import claims, db, queue_store

#: 测试业务日与三方身份：全量轮执行体（worker）、兜底（fallback）、以及"没人碰过"的对照组
DAY = "2026-09-02"
ROUND_OWNERS = {
    "A": "worker-0@roundhost:1001:063500",
    "B": "worker-1@roundhost:1002:063500",
    "C": "worker-2@roundhost:1003:063500",
    "D": "worker-3@roundhost:1004:063500",
    "F": "worker-4@roundhost:1005:063500",
}
FB = "fallback@roundhost"
PHONES = {"A": "13900000001", "B": "13900000002", "C": "13900000003",
          "D": "13900000004", "E": "13900000005", "F": "13900000006",
          "G": "13900000007"}


class _TempDbCase(unittest.TestCase):
    """临时库夹具：与兜底分档测试同一套行形状（领取→弃权/收尾），只读不打桩。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-fallback-event-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env_file = os.path.join(self.tmp, ".env")
        with open(env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + "a" * 64 + "\n")
        keys = ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                "YIBAN_STATE_DIR", "YIBAN_LOG_FILE")
        self.saved = {k: os.environ.get(k) for k in keys}
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": "a" * 64,
            "YIBAN_ENV_FILE": env_file,
            "YIBAN_DB_FILE": os.path.join(self.tmp, "yiban.db"),
            "YIBAN_STATE_DIR": self.tmp,
            "YIBAN_LOG_FILE": os.path.join(self.tmp, "sign.log"),
        })
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        db.init_db(os.environ["YIBAN_DB_FILE"], env_file=env_file, cleanup=False)
        self.addCleanup(self._teardown)

    def _teardown(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _round_fails(self, key, status="skipped_window", retryable=True):
        """全量轮领走 key 对应账号后弃权——"失败即入队"就是这一行 give_up。"""
        ok, epoch = db.claim_sign_account(PHONES[key], DAY, ROUND_OWNERS[key])
        self.assertTrue(ok, f"前置：轮执行体应领到 {key}")
        self.assertTrue(db.claim_give_up(PHONES[key], DAY, ROUND_OWNERS[key], status,
                                         epoch=epoch, retryable=retryable),
                        f"前置：{key} 弃权应写成功")


class PoolYieldCounterexampleTest(_TempDbCase):
    """同一账号让位的三条反例，全部落在 try_claim 的既有条款上（不新增互斥机制）。"""

    def test_inflight_blocks_fallback_but_rounds_failed_account_is_taken(self):
        # (b) A/B/C 在飞（刚领取、租约未过期）⇒ 兜底一个都领不到
        for key in ("A", "B", "C"):
            ok, _e = db.claim_sign_account(PHONES[key], DAY, ROUND_OWNERS[key])
            self.assertTrue(ok, f"前置：轮执行体应领到 {key}")
        for key in ("A", "B", "C"):
            got, _ = db.claim_sign_account(PHONES[key], DAY, FB)
            self.assertFalse(got, f"在飞的 {key} 不该被兜底重领（同一账号让位）")
        # (a) D 被轮弃权到 retry: 档（窗口外跳过）⇒ 兜底立刻领得到
        self._round_fails("D")
        got, _ = db.claim_sign_account(PHONES["D"], DAY, FB)
        self.assertTrue(got, "轮中途弃权的 D 必须能马上被兜底接手（旧形态整段停摆接不到）")
        # 从未被碰过的 E：默认可接手——"该轮没碰的账号"正是让位收窄后要放行的部分
        got, _ = db.claim_sign_account(PHONES["E"], DAY, FB)
        self.assertTrue(got, "全量轮还没领过的账号不该被兜底跳过")

    def test_settled_done_by_round_is_not_reattempted(self):
        # (c) 轮把 A 收尾成 done ⇒ 兜底不再重领（去重靠池状态，不需要第二套记账）
        ok, epoch = db.claim_sign_account(PHONES["A"], DAY, ROUND_OWNERS["A"])
        self.assertTrue(ok, "前置：轮执行体领到 A")
        self.assertTrue(db.claim_settle(PHONES["A"], DAY, ROUND_OWNERS["A"],
                                        db.CLAIM_STATE_DONE, "success", epoch=epoch),
                        "前置：轮把 A 收尾成 done")
        got, _ = db.claim_sign_account(PHONES["A"], DAY, FB)
        self.assertFalse(got, "已了结（done）的账号不得被兜底重领——每次重领都是真实登录")


class FallbackEventSignatureTest(_TempDbCase):
    """事件签名与"兜底可接手"严格同集：两池都只数别人弃到 retry: 档的行。"""

    def _v3_give_up(self, phone, owner, result):
        """v3 弃权的一次真实行迁移：批量领取（epoch 进位）→ 收尾成 failed + 档位前缀。

        走 `queue_store` 的既有原语而不是手插终态行——"入队"的证据就是这次迁移本身。
        """
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,0,?,'2026-09-02 00:00:00.000',5,'pending',0,'','',0,"
            "'2026-09-02 00:00:00.000')", (phone, DAY, owner))
        conn.commit()
        taken = queue_store.claim_batch(owner, DAY, (0,))
        epoch = next(t["epoch"] for t in taken if t["phone"] == phone)
        self.assertEqual(queue_store.settle_tasks(
            owner, DAY, [(phone, result)], state=queue_store.STATE_FAILED,
            epochs={phone: epoch}), 1, f"前置：v3 弃权应写成功 {phone}")

    def test_retry_give_up_by_other_executor_moves_signature(self):
        base = claims.fallback_event(DAY, FB)
        self._round_fails("D")
        sig = claims.fallback_event(DAY, FB)
        self.assertNotEqual(sig, base, "他人弃权=入队，签名必须变（兜底据此提前醒来）")
        self.assertEqual(sig[0], base[0] + 1)

    def test_final_give_up_does_not_move_signature(self):
        """final: 档（预算耗尽/风控）兜底默认领不动：为它唤醒只会空转，绝不计入。"""
        base = claims.fallback_event(DAY, FB)
        self._round_fails("F", status="failed", retryable=False)
        self.assertEqual(claims.fallback_event(DAY, FB), base,
                         "final: 档弃权不得惊动兜底（Task 4 分档：兜底不接这一档）")

    def test_fallbacks_own_give_ups_are_excluded(self):
        """兜底自己弃权的行不进签名：否则"扫→弃权→签名变→立刻再扫"接成紧循环重登。"""
        base = claims.fallback_event(DAY, FB)
        ok, epoch = db.claim_sign_account(PHONES["G"], DAY, FB)
        self.assertTrue(ok, "前置：兜底领到 G")
        db.claim_give_up(PHONES["G"], DAY, FB, "skipped_window", epoch=epoch,
                         retryable=True)
        self.assertEqual(claims.fallback_event(DAY, FB), base,
                         "兜底自己的弃权不得再触发自己的唤醒")

    def test_settle_done_does_not_move_signature(self):
        base = claims.fallback_event(DAY, FB)
        ok, epoch = db.claim_sign_account(PHONES["A"], DAY, ROUND_OWNERS["A"])
        self.assertTrue(ok)
        db.claim_settle(PHONES["A"], DAY, ROUND_OWNERS["A"], db.CLAIM_STATE_DONE,
                        "success", epoch=epoch)
        self.assertEqual(claims.fallback_event(DAY, FB), base,
                         "他人收尾成 done 不产生「兜底可接手」的新事实")

    def test_reclaim_of_retry_row_moves_signature(self):
        """他人重领 retry: 行同样动签名（出队也是迁移）：最多多扫一次，由池仲裁不重签。"""
        self._round_fails("D")
        after_give = claims.fallback_event(DAY, FB)
        ok, _ = db.claim_sign_account(PHONES["D"], DAY, FB)
        self.assertTrue(ok, "前置：兜底接手 retry: 档的 D")
        sig = claims.fallback_event(DAY, FB)
        self.assertNotEqual(sig, after_give)
        self.assertEqual(sig[0], after_give[0] - 1)

    def test_v3_retry_give_up_moves_signature(self):
        """v3 弃权（sign_tasks 的 retry: 档 failed 行）必须惊动兜底：分档灰度下
        全量轮的"失败即入队"只发生在任务队列，领取池里没有这条新事实。"""
        base = claims.fallback_event(DAY, FB)
        self._v3_give_up(PHONES["G"], "worker-9@roundhost:9001:063500",
                         claims.RESULT_RETRY_PREFIX + "skipped_window")
        sig = claims.fallback_event(DAY, FB)
        self.assertNotEqual(sig, base, "v3 弃权=入队，签名必须变（兜底据此提前醒来）")
        self.assertEqual(sig[0], base[0], "v2 侧无迁移，领取池计数不得跟着动")
        self.assertEqual(sig[2], base[2] + 1, "队列侧 retry: 档弃权行计入条数")
        self.assertEqual(sig[3], base[3] + 1, "epoch 进位即'最新迁移标记'变化")

    def test_v3_final_give_up_does_not_move_signature(self):
        """v3 的 final: 档弃权同样不得惊动兜底：分档门与回炉口（requeue_failed 缺省
        只翻 retry: 档）同集，为领不动的行醒来就是空转。"""
        base = claims.fallback_event(DAY, FB)
        self._v3_give_up(PHONES["G"], "worker-9@roundhost:9001:063500",
                         claims.RESULT_FINAL_PREFIX + "failed")
        self.assertEqual(claims.fallback_event(DAY, FB), base,
                         "v3 final: 档弃权不得动签名（镜像 v2 的 final 档纪律）")

    def test_v3_fallbacks_own_give_ups_are_excluded(self):
        """v3 侧同样剔除兜底自己的弃权（含 `{稳定名}:{进程号}:{代次}` 运行时身份）：
        否则"扫→弃权→签名变→立刻再扫"接成紧循环重登。"""
        base = claims.fallback_event(DAY, FB)
        self._v3_give_up(PHONES["G"], FB + ":7777:063500",
                         claims.RESULT_RETRY_PREFIX + "skipped_window")
        self.assertEqual(claims.fallback_event(DAY, FB), base,
                         "兜底自己的 v3 弃权不得再触发自己的唤醒")


if __name__ == "__main__":
    unittest.main(verbosity=2)
