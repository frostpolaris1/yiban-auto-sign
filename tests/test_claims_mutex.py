# -*- coding: utf-8 -*-
"""`(phone, day)` 互斥的 store 层收口：领取租约、owner 身份、v3 重排门、purge 时钟守卫。

标签：B · 调度：领取/队列/执行体
覆盖：领取冲突分支的租约判据（同名进程不得互相重入）、缺省执行体身份含
   进程号与代次、done 行的显式重开（`allow_settled` + epoch 判据）、崩溃接管窗口的
   接管与 fencing、v3 `requeue_task` 的 state/epoch 门（done 不得复活）、
   `claims.purge` 的时钟跳变守卫（拨快 >72h 跳过本轮、参照点推进后恢复）。
对应实现：yiban/store/claims.py（try_claim / purge）、yiban/store/queue_store.py
   （requeue_task）、yiban/egress.py（runtime_owner / parse_owner）、
   yiban/engine/round.py（缺省执行体身份与轮内重试）。
关键断言：**同名 owner 不再等于"自己人"**——旧实现里冲突分支的
   `owner = excluded.owner` 只要命中就短路整个租约判据，于是同机上 cron 与网页手动
   （两者缺省身份都是 `single@{主机名}`）零互斥、并同时登录同一账号（第一红线）。
   现在"自己人重入"必须出示领取时拿到的 epoch（fencing voucher）：只有真持有者知道它。
   每条判据都带活体反例（把输入改坏 ⇒ 判据必须红），见各用例的断言文本。
依赖：临时 sqlite（每用例重建库与 -wal/-shm）+ 两个真子进程（并发组）+ 打桩
   yiban.engine.alerts；无网络请求。整文件在本机执行，无 skip。

跨进程互斥只能用**真多进程**证明：单进程内的两次调用证明不了两个进程会同时放行。
"""
import contextlib
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402

from yiban import clock, egress  # noqa: E402
from yiban.store import queue_store  # noqa: E402

TEST_KEY = "a" * 64
DAY = "2026-09-16"
PHONE = "13800138000"
PHONE_B = "13800138001"
#: 旧格式身份串（含进程号段）：本文件多数用例只把它当不透明的持有者标识
OWNER_A = "hostA:100:090000"
OWNER_B = "hostB:200:090001"


def _ts(**kw):
    """相对业务钟的时间串（租约/心跳判据与库内时间串同轴）。"""
    return (clock.now() + datetime.timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-mutex-")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.env_file = os.path.join(cls.tmp, ".env")
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

    def _claim(self, phone=PHONE, owner=OWNER_A, **kw):
        return db.claim_sign_account(phone, DAY, owner, **kw)

    def _row(self, phone=PHONE):
        return dict(db.get_conn().execute(
            "SELECT * FROM sign_claims WHERE phone=? AND day=?", (phone, DAY)).fetchone())


# ---------------------------------------------------------------------------
# ①/② 缺省身份与同名排除
# ---------------------------------------------------------------------------
class OwnerIdentityTest(_Base):
    """缺省执行体身份必须含进程号与代次：同机两个进程不能拿到同一个身份串。"""

    def test_runtime_owner_appends_pid_and_generation(self):
        owner = egress.runtime_owner(egress.single_owner("myhost"), pid=4321, gen="120000")
        self.assertEqual(owner, "single@myhost:4321:120000")
        self.assertEqual(egress.parse_owner(owner)["role"], egress.ROLE_SINGLE,
                         "运行时身份仍必须能被展示侧解析成角色（接口只回角色不回原串）")
        # 判据的判别力：换一个进程号/代次就必须是不同的身份串，否则互斥判据会误认自己人
        self.assertNotEqual(
            egress.runtime_owner("single@myhost", pid=4321, gen="120000"),
            egress.runtime_owner("single@myhost", pid=4322, gen="120000"),
            "不同进程号必须是不同身份串")
        self.assertNotEqual(
            egress.runtime_owner("single@myhost", pid=4321, gen="120000"),
            egress.runtime_owner("single@myhost", pid=4321, gen="130000"),
            "同一进程号换一代次也必须是不同身份串（进程号可被复用）")

    def test_legacy_owner_format_still_parses(self):
        """库内旧格式 owner 行的解析/展示不得因新身份格式而崩（幂等宽容、不迁移旧数据）。"""
        for old, role in (("single@oldhost", egress.ROLE_SINGLE),
                          (egress.worker_owner(2, "oldhost"), egress.ROLE_WORKER)):
            with self.subTest(owner=old):
                self.assertEqual(egress.parse_owner(old)["role"], role)

    def test_v2_default_executor_id_carries_pid(self):
        """v2 消费路径（run_queue_retry）的缺省身份必须带本进程号。

        只断言 store 构造器不够：真正写库的是消费侧拼出来的 executor_id，它若仍用
        `single@{主机名}`，同机 cron 与网页手动就还是同一个身份串、零互斥。
        """
        import signin  # 兼容壳（转发到 yiban.engine.round）
        phone = "13800138888"
        acc = type("A", (), {"phone": phone, "user_paused": False, "owner": "",
                             "account_id": 0, "password": "p"})()
        with mock.patch.dict(os.environ, {"YIBAN_EXECUTOR_ID": ""}, clear=False), \
                mock.patch.object(signin, "attempt_signin",
                                  return_value=(True, "签到成功", False, "success")):
            signin.run_queue_retry([acc], None, 0, 0,
                                   schedule={phone: clock.now() - datetime.timedelta(seconds=5)},
                                   cred_state={})
        owner = db.get_conn().execute(
            "SELECT owner FROM sign_claims WHERE phone=?", (phone,)).fetchone()["owner"]
        self.assertIn(f":{os.getpid()}:", owner,
                      f"缺省执行体身份必须含进程号（实际 {owner!r}）")
        self.assertEqual(egress.parse_owner(owner)["role"], egress.ROLE_SINGLE,
                         "展示侧解析必须仍认得它是单执行体")


_CHILD_SRC = r"""
import json, os, sys, time
sys.path.insert(0, os.environ["MUTEX_SCRIPTS"])
import db
owner = sys.argv[1]
out = sys.argv[2]
start_at = float(os.environ["MUTEX_START_AT"])
while time.time() < start_at:
    time.sleep(0.001)
ok, epoch = db.claim_sign_account(os.environ["MUTEX_PHONE"], os.environ["MUTEX_DAY"], owner)
with open(out, "w", encoding="utf-8") as f:
    json.dump({"ok": bool(ok), "epoch": int(epoch), "owner": owner}, f)
"""


class SameOwnerExclusionE2ETest(_Base):
    """两个真子进程抢同一 `(phone, day)`：恰一个 success（V6 双进程法）。

    同名 owner 并发是缺陷原文的复现法：旧实现的冲突分支含
    `sign_claims.owner = excluded.owner`，第二个进程自认"自己人"即可重入，
    两边都答 success ⇒ 同一账号两次真实登录。
    """

    def _run_pair(self, owner1, owner2):
        child = os.path.join(self.tmp, "mutex_child.py")
        with open(child, "w", encoding="utf-8") as f:
            f.write(_CHILD_SRC)
        start_at = time.time() + 1.5
        env = dict(os.environ)
        env.update({
            "MUTEX_SCRIPTS": os.path.join(BASE, "scripts"),
            "MUTEX_PHONE": PHONE,
            "MUTEX_DAY": DAY,
            "MUTEX_START_AT": f"{start_at:.3f}",
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_ENV_FILE": self.env_file,
        })
        outs, procs = [], []
        for i, owner in enumerate((owner1, owner2)):
            out = os.path.join(self.tmp, f"mutex-{i}.json")
            outs.append(out)
            procs.append(subprocess.Popen(
                [sys.executable, child, owner, out], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
        for p in procs:
            output, _ = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0,
                             output.decode("utf-8", "replace") if output else "")
        results = []
        for out in outs:
            with open(out, encoding="utf-8") as f:
                results.append(json.load(f))
        return results

    def test_two_processes_with_same_literal_owner_claim_exactly_once(self):
        same = "single@testhost:999999:000000"   # 字面完全相同的身份串：旧实现两边都赢
        results = self._run_pair(same, same)
        wins = [r for r in results if r["ok"]]
        self.assertEqual(len(wins), 1,
                         f"同名 owner 必须恰一个领到，实际 {[r['ok'] for r in results]}")
        row = self._row()
        self.assertEqual(row["state"], db.CLAIM_STATE_CLAIMED)
        self.assertEqual(row["owner"], same)

    def test_two_processes_with_distinct_runtime_owners_claim_exactly_once(self):
        """生产形态：两侧各用本进程的运行时身份（都含各自 PID，可辨识）。"""
        owner1 = egress.runtime_owner(egress.single_owner("testhost"), pid=1111, gen="000000")
        owner2 = egress.runtime_owner(egress.single_owner("testhost"), pid=2222, gen="000000")
        results = self._run_pair(owner1, owner2)
        wins = [r for r in results if r["ok"]]
        self.assertEqual(len(wins), 1,
                         f"两个不同进程的身份串必须恰一个领到，实际 {[r['ok'] for r in results]}")
        self.assertIn(f":{wins[0]['owner'].split(':')[-2]}:", wins[0]["owner"],
                      "获胜方的身份串必须含自己的进程号（可追溯是谁在跑）")
        self.assertEqual(self._row()["owner"], wins[0]["owner"],
                         "库里持有者必须就是报告获胜的那一方")


class SelfTokenReentryTest(_Base):
    """"自己人重入"必须出示领取时拿到的 epoch：只有真持有者知道它。"""

    def test_live_lease_reentry_without_token_is_refused(self):
        """判据的判别力：owner 串相同、租约有效、未出示 token ⇒ 必须拒绝。

        旧实现在这里答 success（`owner = excluded.owner` 短路租约判据），同名进程因此
        可以互相重入。
        """
        self.assertTrue(self._claim()[0])
        self.assertEqual(self._claim(), (False, 0),
                         "未出示 token 的同名重入必须被拒（租约仍然有效）")

    def test_live_lease_reentry_with_current_token_is_allowed(self):
        """持有者带着自己的 token 重入（轮内重试）必须放行，且换一代新 token。"""
        ok, e1 = self._claim()
        self.assertTrue(ok)
        ok2, e2 = self._claim(epoch=e1)
        self.assertTrue(ok2, "真持有者出示当前 token 应能重入")
        self.assertGreater(e2, e1, "任何一次成功领取都必须换新 token（旧的作废）")
        self.assertEqual(self._row()["epoch"], e2)

    def test_reentry_with_stale_token_is_refused(self):
        """被判据的对象是 token 而不是 owner：owner 相同、token 落后 ⇒ 拒绝。"""
        _ok, e1 = self._claim()
        _ok, e2 = self._claim(epoch=e1)
        self.assertEqual(self._claim(epoch=e1), (False, 0),
                         "落后 token 的重入必须被拒（否则被接管者迟到写会覆盖接管者结论）")
        self.assertEqual(self._row()["epoch"], e2)

    def test_expired_lease_takeover_still_works(self):
        """崩溃自愈不能丢：租约过期后任何执行体都能接管（不需要 token）。"""
        self.assertTrue(self._claim(now=_ts(minutes=-30))[0])
        self.assertTrue(self._claim(owner=OWNER_B)[0],
                        "租约过期后接管不得被新判据挡住")


class ReclaimFenceTest(_Base):
    """done 行的显式重开：`allow_settled` 是前提，给了 epoch 还必须对得上。"""

    def _finish(self, owner=OWNER_A):
        self.assertTrue(self._claim(owner=owner)[0])
        self.assertTrue(db.claim_settle(PHONE, DAY, owner, db.CLAIM_STATE_DONE, "ok"))

    def test_done_row_without_allow_settled_is_refused(self):
        self._finish()
        self.assertEqual(self._claim(owner=OWNER_B), (False, 0),
                         "没带 allow_settled 不得把 done 行改回 claimed")

    def test_done_row_with_allow_settled_and_current_epoch_is_accepted(self):
        self._finish()
        self.assertTrue(self._claim(owner=OWNER_B, allow_settled=True, epoch=1)[0],
                        "显式路径（手动/补签）带当前 epoch 应当放行")
        self.assertEqual(self._row()["state"], db.CLAIM_STATE_CLAIMED)

    def test_done_row_with_allow_settled_and_stale_epoch_is_refused(self):
        """带 allow_settled 且 epoch 落后 ⇒ 拒绝（显式重开也要走同一 WHERE 口径）。"""
        self._finish()
        self.assertEqual(self._claim(owner=OWNER_B, allow_settled=True, epoch=0), (False, 0),
                         "epoch 落后的显式重开必须被拒")
        self.assertEqual(self._row()["state"], db.CLAIM_STATE_DONE,
                         "被拒的重开不得改动行状态")

    def test_done_row_with_allow_settled_without_epoch_still_accepted(self):
        """签名向后兼容：不给 epoch 的显式重开保持旧语义（迁移期调用方）。"""
        self._finish()
        self.assertTrue(self._claim(owner=OWNER_B, allow_settled=True)[0])


class CrashTakeoverFenceE2ETest(_Base):
    """崩溃接管窗口：claimed + 心跳过期 ⇒ 接管者成功并换代；原主旧 token 被 fence。"""

    def test_takeover_succeeds_and_old_owner_writes_are_fenced(self):
        # 原主在很久以前领取（心跳已远早于 lease 截止）
        _ok, e_old = self._claim(owner=OWNER_A, now=_ts(hours=-3))
        _ok, e_new = self._claim(owner=OWNER_B)
        self.assertGreater(e_new, e_old, "接管必须换新代（旧代随即作废）")
        row = self._row()
        self.assertEqual((row["owner"], row["epoch"]), (OWNER_B, e_new))
        # 原主事后拿旧 token 收尾/弃权：两处都必须被拒，且不得改动接管者的行
        self.assertFalse(db.claim_settle(PHONE, DAY, OWNER_A, db.CLAIM_STATE_DONE, "迟到", epoch=e_old))
        self.assertFalse(db.claim_give_up(PHONE, DAY, OWNER_A, "迟到", epoch=e_old))
        self.assertFalse(db.claim_touch(PHONE, DAY, OWNER_A, epoch=e_old))
        row = self._row()
        self.assertEqual((row["owner"], row["state"], row["result"]), (OWNER_B, db.CLAIM_STATE_CLAIMED, ""))


# ---------------------------------------------------------------------------
# ④ v3 requeue_task 的 state/epoch 门
# ---------------------------------------------------------------------------
class V3RequeueGateTest(_Base):
    """迟到重排不得把 done 复活；重排 claimed 行必须带当前 epoch。"""

    SHARDS = (0, 1)

    def _add_task(self, phone=PHONE, state="pending", vshard=0):
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (phone, DAY, vshard, "", _ts(seconds=-1), 5, state, 0, "", "", _ts(minutes=-60)))
        conn.commit()

    def _task_row(self, phone=PHONE):
        return dict(db.get_conn().execute(
            "SELECT * FROM sign_tasks WHERE phone=? AND day=?", (phone, DAY)).fetchone())

    def test_late_requeue_on_done_row_does_not_revive(self):
        """e2e 注入形态：done 行 + 迟到 requeue ⇒ pending 数不变。"""
        self._add_task(state="done")
        self.assertEqual(queue_store.pending_count(DAY, self.SHARDS), 0, "前置：done 不是待办")
        self.assertEqual(queue_store.requeue_task(PHONE, DAY, _ts(seconds=-1)), 0,
                         "done 行不得被重排复活成 pending")
        self.assertEqual(self._task_row()["state"], "done")
        self.assertEqual(queue_store.pending_count(DAY, self.SHARDS), 0, "pending 计数必须不变")

    def test_late_requeue_on_skipped_row_does_not_revive(self):
        self._add_task(state="skipped")
        self.assertEqual(queue_store.requeue_task(PHONE, DAY, _ts(seconds=-1)), 0)

    def test_requeue_claimed_row_with_stale_epoch_does_not_revive(self):
        self._add_task()
        e1 = queue_store.claim_batch(OWNER_A, DAY, self.SHARDS, now=_ts())[0]["epoch"]
        self.assertEqual(queue_store.requeue_task(PHONE, DAY, _ts(minutes=1), epoch=e1 + 1), 0,
                         "落后 epoch 不得把在飞行重排回 pending")
        self.assertEqual(self._task_row()["state"], "claimed")

    def test_requeue_claimed_row_with_current_epoch_revives(self):
        self._add_task()
        e1 = queue_store.claim_batch(OWNER_A, DAY, self.SHARDS, now=_ts())[0]["epoch"]
        self.assertEqual(queue_store.requeue_task(PHONE, DAY, _ts(minutes=1), epoch=e1), 1)
        self.assertEqual(self._task_row()["state"], "pending")

    def test_requeue_failed_row_revives_for_same_day_second_chance(self):
        """failed 是未了结态：当日回炉口必须还能用（否则失败账号只能等第二天）。"""
        self._add_task(state="failed")
        self.assertEqual(queue_store.requeue_task(PHONE, DAY, _ts(seconds=-1)), 1)
        self.assertEqual(self._task_row()["state"], "pending")


# ---------------------------------------------------------------------------
# ⑥ claims.purge 的时钟跳变守卫
# ---------------------------------------------------------------------------
class PurgeClockGuardTest(_Base):
    """系统时间前跳 >72h 时跳过本轮清理（同库其余清理同形），预防"整删当日互斥面"。"""

    GUARD_KEY = "purge_claims_clock"

    def _add_old_row(self, phone=PHONE):
        old_day = (clock.now() - datetime.timedelta(days=20)).strftime("%Y-%m-%d")
        self.assertTrue(db.claim_sign_account(phone, old_day, OWNER_A)[0])
        return old_day

    def _seed_reference(self, value):
        conn = db.get_conn()
        with db._conn_lock:
            conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                         (self.GUARD_KEY, value))
            conn.commit()

    def _days(self):
        return {r["day"] for r in db.get_conn().execute(
            "SELECT day FROM sign_claims").fetchall()}

    def test_forward_clock_jump_skips_purge(self):
        old_day = self._add_old_row()
        self._seed_reference((clock.now() - datetime.timedelta(days=10)).strftime(
            "%Y-%m-%d %H:%M:%S"))   # 参照点在 10 天前 → 本次视为前跳
        self.assertEqual(db.purge_sign_claims(14), 0, "跳变必须跳过本轮清理")
        self.assertIn(old_day, self._days(), "跳变时超期行不得被删（否则当日互斥面被整删）")

    def test_jump_only_skips_one_round(self):
        """参照点随越界一并推进 ⇒ 下一轮恢复正常清理（不需要人工重置）。"""
        old_day = self._add_old_row()
        self._seed_reference((clock.now() - datetime.timedelta(days=10)).strftime(
            "%Y-%m-%d %H:%M:%S"))
        db.purge_sign_claims(14)
        self.assertEqual(db.purge_sign_claims(14), 1, "参照点推进后下一轮必须恢复清理")
        self.assertNotIn(old_day, self._days())

    def test_normal_purge_still_works(self):
        self._add_old_row()
        self.assertTrue(db.claim_sign_account(PHONE_B, DAY, OWNER_A)[0])
        self.assertEqual(db.purge_sign_claims(14), 1)
        self.assertEqual(self._days(), {DAY})


if __name__ == "__main__":
    unittest.main(verbosity=2)
