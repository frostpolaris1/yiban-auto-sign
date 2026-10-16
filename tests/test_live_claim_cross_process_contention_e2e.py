# -*- coding: utf-8 -*-
"""现役领取面的**真跨进程争用**行为用例：claim / 互斥 / 收尸 各一条，且各有牙。

标签：B · 调度：领取/队列/执行体

**为什么必须真跨进程**：`claim_batch` 在 `yiban.store.connection._conn_lock`
（进程内 `threading.RLock`）下执行。同进程两线程调它会被**串行化**，争用窗口不出现，
用例对"跨进程原子性"**无判别力**（形状与判据见 `tests/test_concurrency_fidelity_gate.py`）。
真争用只能用**独立进程**（每进程一个独立连接）制造——这正是本文件的形态。

**本文件测什么（三条不变式，不是编舞）**：
  ① **claim 原子性**：N（≥2）个独立进程同一时刻抢**同一行**，赢家**恰为 1**——
     同一账号不得被并发持有。
  ② **互斥**：一行被某个存活持有者持有时，另一个独立进程**领不走**它。
  ③ **收尸**：持有者被 SIGKILL 后，`reap_abandoned(稳定名)` 把该行回退 `pending`、
     `epoch+1`、清 owner；随后另一进程能领走它（不丢签）。

**每条都有牙**（人为破坏协调 ⇒ 用例红）：
  ① 用"先查后写"的破坏版领取（SELECT 后 sleep 再无条件 UPDATE）⇒ 赢家 ≥2；
  ② 用忽略 `state` 的破坏版领取（无条件抢）⇒ 持有中被抢走；
  ③ 不调 `reap_abandoned` ⇒ 行停在 `claimed`（漏签形态）。
  牙的形态沿用普查 §三的跨进程探针（当时已在变异树上实测出 `n_winners=2`）。

**与靶场 e2e 的关系（不重复造）**：`tests/test_multi_executor_drill_e2e.py` 在**引擎级**
（真监督进程 + 真执行体 + 假上游）验同一批性质。本文件在**原语级**（直接调
`queue_store`）验，粒度更细、墙钟更短、且不依赖假上游；两者互补，非替代。

对应实现：yiban/store/queue_store.py（`claim_batch` / `reap_abandoned`）、
   yiban/store/connection.py（`_conn_lock` 根因）。
关键断言：钉"同一账号不得被并发持有""持有中不得被抢""死者名下行必须回退待领"三条
   不变式；不钉任何调用顺序。
依赖：真子进程（POSIX：SIGKILL）＋ 临时 sqlite；无网络。非 POSIX 整组 skip。
"""
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_POSIX = (os.name == "posix"
          and hasattr(signal, "SIGKILL")
          and os.path.isdir("/proc"))

DAY = "2026-09-22"          # 周二，避开周末门
NOW = f"{DAY} 06:40:00.000"
PHONE = "13800000001"
TEST_KEY = "a" * 64

#: 子进程脚本：独立进程、独立连接，按 `MODE` 在**同一发令时刻**后行动。
_CHILD_SRC = r'''
import json, os, sys, time

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.environ.update(json.loads(os.environ["PROBE_ENV"]))

import db  # noqa: E402
from yiban.store import queue_store  # noqa: E402

db.init_db(os.environ["YIBAN_DB_FILE"], env_file=os.environ["YIBAN_ENV_FILE"], cleanup=False)
MODE = os.environ["PROBE_MODE"]
OWNER = os.environ["PROBE_OWNER"]
DAY = os.environ["PROBE_DAY"]
NOW = os.environ["PROBE_NOW"]
PHONE = os.environ["PROBE_PHONE"]
OUT = os.environ["PROBE_OUT"]


def emit(**kw):
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(kw, f)


def _rows(sql, params):
    conn = db.get_conn()
    return conn.execute(sql, params).fetchall()


if MODE in ("claim", "hold"):
    start = float(os.environ["PROBE_START"])
    time.sleep(max(0.0, start - time.time()))
    got = queue_store.claim_batch(OWNER, DAY, (0,), now=NOW)
    won = [r["phone"] for r in got] if got else []
    emit(mode=MODE, owner=OWNER, won=won)
    if MODE == "hold":
        time.sleep(600)          # 持有在飞行，等父进程 SIGKILL
elif MODE == "claim_broken":
    # 破坏版：先查后写（跨语句窗口）。sleep 保证所有竞位者都在任何写之前读到 pending。
    start = float(os.environ["PROBE_START"])
    time.sleep(max(0.0, start - time.time()))
    conn = db.get_conn()
    seen = conn.execute("SELECT phone FROM sign_tasks WHERE day=? AND state='pending'",
                        (DAY,)).fetchall()
    time.sleep(0.4)
    won = []
    for r in seen:
        conn.execute("UPDATE sign_tasks SET state='claimed', owner=?, epoch=epoch+1 "
                     "WHERE day=? AND phone=?", (OWNER, DAY, r["phone"]))
        won.append(r["phone"])
    conn.commit()
    emit(mode=MODE, owner=OWNER, won=won)
elif MODE == "claim_steal":
    # 破坏版：忽略 state 的无条件抢（把在飞行也抢走）。
    start = float(os.environ["PROBE_START"])
    time.sleep(max(0.0, start - time.time()))
    conn = db.get_conn()
    cur = conn.execute("UPDATE sign_tasks SET state='claimed', owner=?, epoch=epoch+1 "
                       "WHERE day=? AND phone=?", (OWNER, DAY, PHONE))
    conn.commit()
    emit(mode=MODE, owner=OWNER, won=[PHONE] if cur.rowcount else [])
elif MODE in ("reap", "reap_off"):
    if MODE == "reap_off":
        emit(mode=MODE, affected=0, skipped=True)
    else:
        n = queue_store.reap_abandoned(OWNER, DAY)
        emit(mode=MODE, affected=n)
else:
    raise SystemExit("unknown MODE: " + MODE)
'''


def _seed(db_file, env_file, tmp):
    """建库并写一行 pending（在父进程内，用独立于子进程的连接）。

    `sys.path` 无需自插：`pyproject.toml` 的 `pythonpath` 已含 `.` 与 `scripts`。
    环境变量用 `mock.patch.dict` 夹住，退出即还原，不把临时库路径泄给同进程后续用例。
    """
    with mock.patch.dict(os.environ, {
        "YIBAN_ACCOUNTS_KEY": TEST_KEY,
        "YIBAN_ENV_FILE": env_file,
        "YIBAN_DB_FILE": db_file,
        "YIBAN_STATE_DIR": tmp,
    }):
        import db
        db.init_db(db_file, env_file=env_file, cleanup=False)
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone,day,vshard,owner,run_at,priority,state,"
            "attempts,lease_until,result,epoch,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (PHONE, DAY, 0, "", NOW, 5, "pending", 0, "", "", 0, f"{DAY} 06:30:00.000"))
        conn.commit()
        conn.close()
        db._conn = None


@unittest.skipUnless(_POSIX, "真 SIGKILL 与独立进程争用仅 POSIX 可用")
class LiveCrossProcessContentionTest(unittest.TestCase):
    """claim / 互斥 / 收尸 三条真跨进程不变式，各有牙。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-xproc-")
        self.db_file = os.path.join(self.tmp, "yiban.db")
        self.env_file = os.path.join(self.tmp, ".env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        self.child = os.path.join(self.tmp, "child.py")
        with open(self.child, "w", encoding="utf-8") as f:
            f.write(_CHILD_SRC)
        self._procs = []
        _seed(self.db_file, self.env_file, self.tmp)

    def tearDown(self):
        # 兜底回收：任一断言失败时，持有子进程（hold 模式 sleep 600s）也必须被清掉，
        # 否则错误路径上会漏一个活进程。
        for p in self._procs:
            if p.poll() is None:
                p.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    p.wait(timeout=30)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 子进程助手 ----
    def _spawn(self, mode, owner, out, start_at=None, phone=PHONE):
        env = dict(os.environ)
        env.update({
            "PROBE_MODE": mode,
            "PROBE_OWNER": owner,
            "PROBE_DAY": DAY,
            "PROBE_NOW": NOW,
            "PROBE_PHONE": phone,
            "PROBE_OUT": out,
            "PROBE_START": str(start_at if start_at is not None else time.time() + 1.5),
            "PROBE_ENV": json.dumps({
                "YIBAN_ACCOUNTS_KEY": TEST_KEY, "YIBAN_ENV_FILE": self.env_file,
                "YIBAN_DB_FILE": self.db_file, "YIBAN_STATE_DIR": self.tmp,
                "YIBAN_LOG_FILE": os.path.join(self.tmp, "sign.log"),
            }),
        })
        proc = subprocess.Popen([sys.executable, self.child, BASE], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self._procs.append(proc)
        return proc

    def _result(self, out):
        with open(out, encoding="utf-8") as f:
            return json.load(f)

    def _wait_file(self, out, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            if os.path.exists(out):
                return self._result(out)
            time.sleep(0.05)
        raise AssertionError("子进程未在 %ss 内产出结果文件 %s" % (timeout, out))

    def _race(self, mode, n=3):
        """n 个独立进程同一发令时刻执行 mode，返回每个进程的 won 列表。"""
        start = time.time() + 1.5
        procs, outs = [], []
        for i in range(n):
            out = os.path.join(self.tmp, f"{mode}-{i}.json")
            outs.append(out)
            procs.append(self._spawn(mode, f"single@host{i}", out, start_at=start))
        for p in procs:
            o, _ = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0,
                             "%s 子进程异常退出：%s" % (mode, (o or b"").decode("utf-8", "replace")))
        return [self._result(o) for o in outs]

    def _row(self):
        with mock.patch.dict(os.environ, {
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": self.env_file,
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_STATE_DIR": self.tmp,
        }):
            import db
            db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
            row = db.get_conn().execute(
                "SELECT state, owner, epoch FROM sign_tasks WHERE day=? AND phone=?",
                (DAY, PHONE)).fetchone()
            db.get_conn().close()
            db._conn = None
        return dict(row)

    # ---- ① claim 原子性 ----
    def test_claim_many_processes_one_winner(self):
        """3 个独立进程同刻抢同一行：恰 1 个赢家，库内持有者与赢家一致。"""
        results = self._race("claim")
        winners = [w for r in results for w in r["won"]]
        self.assertEqual(
            len(winners), 1,
            "同一行只能有一个赢家，实际 %d 个：%s——并发放行＝同一账号被并发持有"
            "＝上游风控红线" % (len(winners), results))
        self.assertEqual(winners[0], PHONE)
        row = self._row()
        self.assertEqual(row["state"], "claimed")
        self.assertIn(row["owner"], [r["owner"] for r in results if r["won"]],
                      "库内持有者必须正是那个赢家")

    def test_claim_teeth_broken_check_then_write_double_wins(self):
        """牙①：把领取换成'先查后写'⇒ 赢家 ≥2。证明上面的'恰 1 个赢家'有判别力。"""
        results = self._race("claim_broken")
        winners = [w for r in results for w in r["won"]]
        self.assertGreaterEqual(
            len(winners), 2,
            "破坏协调后仍未出现双领——该用例测不到双领，是恒绿摆设：%s" % results)

    # ---- ② 互斥：持有中的行不得被另一个进程领走 ----
    def test_mutex_held_row_not_claimed_by_other_process(self):
        """A 进程持有在飞行时，B 进程的合法领取必须领不走它。"""
        hold_out = os.path.join(self.tmp, "hold.json")
        holder = self._spawn("hold", "single@holder", hold_out, start_at=time.time())
        try:
            got = self._wait_file(hold_out)
            self.assertEqual(got["won"], [PHONE], "持有者必须先真的领到该行")
            out = os.path.join(self.tmp, "b.json")
            b = self._spawn("claim", "single@intruder", out, start_at=time.time())
            o, _ = b.communicate(timeout=120)
            self.assertEqual(b.returncode, 0, (o or b"").decode("utf-8", "replace"))
            self.assertEqual(
                self._result(out)["won"], [],
                "持有中的行被另一个进程领走——互斥失效＝同一账号被并发持有")
            self.assertEqual(self._row()["owner"], "single@holder",
                            "持有者不得被换手")
        finally:
            holder.kill()
            holder.wait(timeout=30)

    def test_mutex_teeth_steal_ignoring_state_double_holds(self):
        """牙②：用忽略 `state` 的破坏版领取 ⇒ 持有中被抢走。证明互斥判据有判别力。"""
        hold_out = os.path.join(self.tmp, "hold2.json")
        holder = self._spawn("hold", "single@holder", hold_out, start_at=time.time())
        try:
            self._wait_file(hold_out)
            out = os.path.join(self.tmp, "steal.json")
            s = self._spawn("claim_steal", "single@thief", out, start_at=time.time())
            o, _ = s.communicate(timeout=120)
            self.assertEqual(s.returncode, 0, (o or b"").decode("utf-8", "replace"))
            self.assertEqual(self._result(out)["won"], [PHONE],
                             "破坏版必须真的抢到持有中的行")
            self.assertEqual(self._row()["owner"], "single@thief",
                             "破坏协调后仍未换手——互斥判据无牙")
        finally:
            holder.kill()
            holder.wait(timeout=30)

    # ---- ③ 收尸：持有者死亡后回退待领 ----
    def test_reap_killed_holder_row_returns_to_pending(self):
        """SIGKILL 持有者后，`reap_abandoned` 把该行回退 pending、epoch+1、清 owner。"""
        hold_out = os.path.join(self.tmp, "hold3.json")
        holder = self._spawn("hold", "single@victim", hold_out, start_at=time.time())
        got = self._wait_file(hold_out)
        self.assertEqual(got["won"], [PHONE])
        epoch_held = self._row()["epoch"]
        holder.kill()
        holder.wait(timeout=30)

        out = os.path.join(self.tmp, "reap.json")
        r = self._spawn("reap", "single@victim", out, start_at=time.time())
        o, _ = r.communicate(timeout=120)
        self.assertEqual(r.returncode, 0, (o or b"").decode("utf-8", "replace"))
        self.assertEqual(self._result(out)["affected"], 1, "必须恰回收 1 行")
        row = self._row()
        self.assertEqual(row["state"], "pending", "收尸后必须回退待领（不是留 claimed）")
        self.assertEqual(row["owner"], "", "回退后的行不再归任何执行体")
        self.assertEqual(int(row["epoch"]), int(epoch_held) + 1,
                         "收尸必须换代 +1（fence 迟到旧代写）")

        # 不丢：收尸后另一进程能领走它。
        out2 = os.path.join(self.tmp, "reclaim.json")
        c = self._spawn("claim", "single@rescue", out2, start_at=time.time())
        o, _ = c.communicate(timeout=120)
        self.assertEqual(c.returncode, 0, (o or b"").decode("utf-8", "replace"))
        self.assertEqual(self._result(out2)["won"], [PHONE],
                         "收尸后的行必须能被下一轮领走（否则该账号当天漏签）")

    def test_reap_teeth_skipped_reap_leaves_claimed_row(self):
        """牙③：不调 `reap_abandoned` ⇒ 行停在 `claimed`（漏签形态）。"""
        hold_out = os.path.join(self.tmp, "hold4.json")
        holder = self._spawn("hold", "single@victim2", hold_out, start_at=time.time())
        self._wait_file(hold_out)
        holder.kill()
        holder.wait(timeout=30)

        out = os.path.join(self.tmp, "reapoff.json")
        r = self._spawn("reap_off", "single@victim2", out, start_at=time.time())
        r.communicate(timeout=120)
        self.assertEqual(self._result(out)["affected"], 0)
        row = self._row()
        self.assertEqual(row["state"], "claimed",
                         "跳过收尸后行应停在 claimed——若它自己回退了，"
                         "说明有别的机制在兜底，本判据的前提需重估")
        # 且它不可再领（漏签形态）。
        out2 = os.path.join(self.tmp, "reclaim2.json")
        c = self._spawn("claim", "single@rescue2", out2, start_at=time.time())
        c.communicate(timeout=120)
        self.assertEqual(self._result(out2)["won"], [],
                         "残留 claimed 行不该被再领——这正是漏签")


if __name__ == "__main__":
    unittest.main(verbosity=2)
