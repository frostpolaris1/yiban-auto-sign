# -*- coding: utf-8 -*-
"""兜底"失败即入队"的事件源与"同一账号让位"的仲裁面：都由**任务队列（sign_tasks）**的行迁移承担。

标签：B · 调度：领取/队列/执行体
覆盖：`queue_store.fallback_event` 事件签名（只数 `sign_tasks` 里 `retry:` 档未了结行的
   `(条数, 最新迁移标记)`，`final:` 档弃权不动签名、兜底自己的弃权被剔除、他人收尾 `done`
   不进子集、他人重领 `retry:` 行同样动签名、库不可用返回 None），以及同一轮内队列级的
   三条反例（全量轮在飞的账号兜底领不到、轮中途弃权到 `retry:` 档的账号兜底（回炉后）
   立刻领得到、轮把账号收尾成 `done` 后兜底不再重领；此外从未被碰过的账号默认可接手）。
对应实现：yiban/store/queue_store.py（fallback_event 与 claim_batch/requeue_failed/settle_tasks
   的既有条款）、yiban/engine/workers.py（让位与唤醒的判据全部取队列，不自建第二套）。
关键断言：让位与去重**不需要新机制**——队列的"`pending` 才可领、`claimed` 仅在租约过期
   后回收、`done`/`skipped` 不复活、`final:` 档须显式路径才回炉"本身就是"同一账号让位"
   与队列内去重；事件签名必须与"可接手"严格同集（数了 `final:` 就是为一个永远领不动的行
   白唤醒；数了兜底自己的弃权会把"扫→弃权→唤醒→再扫"接成紧循环重复真实登录）。
依赖：真临时库（init_db + 走 `queue_store` 真实领取/收尾/回炉原语）；不起子进程、不发网络
   请求、不打桩时钟。

用法（项目根目录）：
    py -m pytest tests/test_fallback_event_yield.py -v
"""
import contextlib
import os
import shutil
import tempfile
import unittest
from unittest import mock

from yiban.store import claims, db, queue_store

#: 测试业务日与三方身份：全量轮执行体（worker）与兜底（fallback）
DAY = "2026-09-02"
ROUND_OWNER = "worker-0@roundhost:1001:063500"
FB = "fallback@roundhost"
PHONE = "13900000001"
PHONE_G = "13900000007"


class _TempDbCase(unittest.TestCase):
    """临时库夹具：走队列真实原语（领取→弃权/收尾），只读不打桩。"""

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

    # ---- 队列原语夹具（走 queue_store 的真实行迁移，"入队"的证据就是这次迁移本身）----
    def _add_pending(self, phone, owner=""):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,0,?,'2026-09-02 00:00:00.000',5,'pending',0,'','',0,"
            "'2026-09-02 00:00:00.000')", (phone, DAY, owner))
        conn.commit()

    #: 远未来时刻：回炉/重排后行的 `run_at` 是毫秒精度的"现在"，用秒精度默认 now 会因
    #: 字符串序（'.000' > ''）判成"还没到点"；夹具统一以远未来取行，只测状态语义。
    _FUTURE = "2099-01-01 00:00:00.000"

    def _claim_batch(self, owner):
        return queue_store.claim_batch(owner, DAY, (0,), now=self._FUTURE)

    def _claim(self, phone, owner):
        epoch = next(t["epoch"] for t in self._claim_batch(owner) if t["phone"] == phone)
        return epoch

    def _round_fails(self, phone, status="skipped_window", retryable=True,
                     owner=ROUND_OWNER):
        """全量轮领走账号后弃权——"失败即入队"就是这次 failed 收尾（带档位前缀）。"""
        self._add_pending(phone)
        epoch = self._claim(phone, owner)
        prefix = (claims.RESULT_RETRY_PREFIX if retryable
                  else claims.RESULT_FINAL_PREFIX)
        self.assertEqual(queue_store.settle_tasks(
            owner, DAY, [(phone, prefix + status)], state=queue_store.STATE_FAILED,
            epochs={phone: epoch}), 1, f"前置：轮弃权应写成功 {phone}")

    def _round_settles_done(self, phone, owner=ROUND_OWNER):
        self._add_pending(phone)
        epoch = self._claim(phone, owner)
        self.assertEqual(queue_store.settle_tasks(
            owner, DAY, [(phone, "success")], state=queue_store.STATE_DONE,
            epochs={phone: epoch}), 1, "前置：轮把账号收尾成 done")


class PoolYieldCounterexampleTest(_TempDbCase):
    """同一账号让位的三条反例，全部落在队列的既有条款上（不新增互斥机制）。"""

    def test_inflight_blocks_fallback_and_rounds_failed_account_is_takeable(self):
        # (b) 账号在飞（刚被轮领取、租约未过期）⇒ 队列里是 claimed，兜底领不到
        phone = "13900000002"
        self._add_pending(phone)
        self._claim(phone, ROUND_OWNER)
        self.assertEqual(self._claim_batch(FB), [],
                         "在飞的行是 claimed，兜底不该重领（同一账号让位）")
        # (a) 轮把 D 弃权到 retry: 档 ⇒ 回炉后兜底立刻领得到
        d = "13900000003"
        self._round_fails(d)
        self.assertEqual(queue_store.requeue_failed(DAY, (0,), include_final=False), 1,
                         "retry: 档应由默认回炉口翻回 pending")
        taken = self._claim_batch(FB)
        self.assertIn(d, [t["phone"] for t in taken],
                      "轮中途弃权到 retry: 档的账号必须能马上被兜底接手")
        # 从未被碰过的 E 已在队列里 pending：默认可接手
        e = "13900000005"
        self._add_pending(e)
        self.assertIn(e, [t["phone"] for t in self._claim_batch(FB)],
                      "轮还没领过的账号不该被兜底跳过")

    def test_settled_done_by_round_is_not_reattempted(self):
        # (c) 轮把账号收尾成 done ⇒ 兜底不再重领（去重靠队列状态，不需要第二套记账）
        phone = "13900000004"
        self._round_settles_done(phone)
        self.assertEqual(self._claim_batch(FB), [],
                         "已了结（done）的账号不得被兜底重领——每次重领都是真实登录")
        self.assertEqual(queue_store.requeue_failed(DAY, (0,), include_final=True), 0,
                         "done 是终态，回炉口也不得复活它")


class FallbackEventSignatureTest(_TempDbCase):
    """事件签名与"兜底可接手"严格同集：只数 `retry:` 档、剔除自己的弃权。"""

    def test_retry_give_up_by_other_executor_moves_signature(self):
        base = queue_store.fallback_event(DAY, FB)
        self._round_fails(PHONE)
        sig = queue_store.fallback_event(DAY, FB)
        self.assertNotEqual(sig, base, "他人弃权=入队，签名必须变（兜底据此提前醒来）")
        self.assertEqual(sig[0], base[0] + 1)

    def test_final_give_up_does_not_move_signature(self):
        """final: 档（预算耗尽/风控）兜底默认领不动：为它唤醒只会空转，绝不计入。"""
        base = queue_store.fallback_event(DAY, FB)
        self._round_fails(PHONE, status="failed", retryable=False)
        self.assertEqual(queue_store.fallback_event(DAY, FB), base,
                         "final: 档弃权不得惊动兜底（档位纪律：兜底不接这一档）")

    def test_fallbacks_own_give_ups_are_excluded(self):
        """兜底自己弃权的行不进签名：否则"扫→弃权→签名变→立刻再扫"接成紧循环重登。"""
        base = queue_store.fallback_event(DAY, FB)
        self._round_fails(PHONE_G, owner=FB + ":7777:063500")
        self.assertEqual(queue_store.fallback_event(DAY, FB), base,
                         "兜底自己的弃权不得再触发自己的唤醒（含运行时身份前缀）")

    def test_settle_done_does_not_move_signature(self):
        base = queue_store.fallback_event(DAY, FB)
        self._round_settles_done(PHONE)
        self.assertEqual(queue_store.fallback_event(DAY, FB), base,
                         "他人收尾成 done 不产生「兜底可接手」的新事实")

    def test_reclaim_of_retry_row_moves_signature(self):
        """他人重领 retry: 行同样动签名（出队也是迁移）：回炉/重领都让签名比"有失败行"
        时更小，兜底据此又醒一次——最多多扫一遍，由队列仲裁不重签。"""
        base = queue_store.fallback_event(DAY, FB)
        self._round_fails(PHONE)
        after_give = queue_store.fallback_event(DAY, FB)
        self.assertEqual(after_give[0], base[0] + 1, "弃权后 retry: 行进入可接手集")
        self.assertEqual(queue_store.requeue_failed(DAY, (0,), include_final=False), 1)
        after_requeue = queue_store.fallback_event(DAY, FB)
        self.assertEqual(after_requeue[0], base[0], "回炉后该行不再是 failed，退出手集")
        self.assertNotEqual(after_requeue, after_give, "回炉=出队也是迁移，签名必须变")
        self._claim(PHONE, FB)
        self.assertEqual(queue_store.fallback_event(DAY, FB), after_requeue,
                         "领取不再改变 failed 计数")

    def test_v18_historical_rows_are_not_counted(self):
        """`vshard=-1` 的历史行（旧平移/补账遗留）永不被领取，不得计入唤醒频率。"""
        base = queue_store.fallback_event(DAY, FB)
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,-1,'','2026-09-02 00:00:00.000',5,'failed',0,'',?,1,"
            "'2026-09-02 00:00:00.000')",
            (PHONE_G, DAY, claims.RESULT_RETRY_PREFIX + "skipped_window"))
        conn.commit()
        self.assertEqual(queue_store.fallback_event(DAY, FB), base,
                         "历史惰性行不属于任何分片集，不得计入兜底可接手集")

    def test_library_unavailable_returns_none(self):
        """库不可用返回 None：调用方退回等满间隔（事件驱动是延迟优化，不是正确性依赖）。"""
        class _BoomConn:
            def execute(self, *a, **k):
                raise RuntimeError("db down")

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                queue_store, "_queue_conn",
                return_value=(_BoomConn(), contextlib.nullcontext())))
            self.assertIsNone(queue_store.fallback_event(DAY, FB))


if __name__ == "__main__":
    unittest.main(verbosity=2)
