# -*- coding: utf-8 -*-
"""轮末收尸的 e2e：被杀子进程留在 `sign_tasks` 里的在领任务必须被回退待领。

标签：B · 调度：领取/队列/执行体
覆盖：真子进程用 `claim_batch` 领取任务后自杀；监督进程判它异常退出，轮末把该任务
   回退 `pending`、放开租约、`epoch+1`，下一次 `claim_batch` 即可领取。不这样做，
   被杀执行体的在领任务没人回退，该账号当天无人再签（漏签）。
对应实现：yiban/store/queue_store.py（`reap_abandoned`）、
   yiban/engine/workers.py（`_await_workers` 后的轮末收尸）。
关键断言：判据只能是「进程已退出」——覆盖信号杀（rc<0）与退出码 1（未预期异常）两种
   形状，两种都必须被收住；看退出码的写法会漏掉其中一种。
依赖：临时 sqlite + 真子进程（POSIX：SIGKILL 模拟崩溃）；非 POSIX 平台整类 skip。

2026-10-16 饮食批：原组 2/组 3（心跳停摆后的**租约**接管、旧表 `sign_claims` 的轮末
收尸）已删——任务级租约随工单 `l1rx` 删除，`sign_claims` 随单池化冻结、生产零写入
（工单 `77s2`）。真跨进程争用与收尸判据现由
`tests/test_live_claim_cross_process_contention_e2e.py` 承担。
"""
import contextlib
import datetime
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402

from yiban import clock  # noqa: E402
from yiban.engine import workers  # noqa: E402
from yiban.store import queue_store  # noqa: E402

#: 真 `subprocess.Popen` 的原始引用：组 3 要把它换成"拉真子进程"的替身，
#: 替身内部必须用原引用，否则替换会递归。
_REAL_POPEN = subprocess.Popen

TEST_KEY = "a" * 64
DAY = "2026-09-25"
PHONE = "13800138000"
OWNER_B = "worker-1@testhost:222:090000"


def _ts(**kw):
    return (clock.now() + datetime.timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


#: v3 队列（`sign_tasks`）的被杀子进程：用 claim_batch 领一条已到期的待领任务后自杀。
#: owner 与旧表路径同源（稳定槽位名拼 PID/代次），故监督进程用同一稳定名前缀即可收尸。
_CLAIM_TASK_DIE_SRC = r"""
import json, os, signal, sys
sys.path.insert(0, os.environ["E2E_REPO"])
from yiban import egress
from yiban.store import db, queue_store
owner = egress.runtime_owner(os.environ["YIBAN_EXECUTOR_ID"])
db.init_db(os.environ["E2E_DB"], env_file=os.environ.get("E2E_ENV_FILE") or None,
           cleanup=False)
rows = queue_store.claim_batch(owner, os.environ["E2E_DAY"], (0,))
with open(os.environ["E2E_OUT"], "w", encoding="utf-8") as f:
    json.dump({"n": len(rows),
               "epoch": rows[0]["epoch"] if rows else 0,
               "owner": owner}, f)
os.kill(os.getpid(), signal.SIGKILL)   # 模拟崩溃：来不及收尾
"""

#: v3 队列的 rc=1 子进程：领到已到期的待领任务后，**未走到自己的收尾**就以退出码 1 收场。
#: 与 `_CLAIM_TASK_DIE_SRC` 的 SIGKILL 形态互补——前者是信号杀（rc<0），本形态是未预期异常
#: （rc=1）。监督进程的轮末收尸对两者都必须生效；判据只能是"进程已退出"，不得看退出码。
_CLAIM_TASK_EXIT1_SRC = r"""
import json, os, sys
sys.path.insert(0, os.environ["E2E_REPO"])
from yiban import egress
from yiban.store import db, queue_store
owner = egress.runtime_owner(os.environ["YIBAN_EXECUTOR_ID"])
db.init_db(os.environ["E2E_DB"], env_file=os.environ.get("E2E_ENV_FILE") or None,
           cleanup=False)
rows = queue_store.claim_batch(owner, os.environ["E2E_DAY"], (0,))
with open(os.environ["E2E_OUT"], "w", encoding="utf-8") as f:
    json.dump({"n": len(rows),
               "epoch": rows[0]["epoch"] if rows else 0,
               "owner": owner}, f)
raise RuntimeError("未预期异常：来不及收尾")   # 退出码 1
"""


class _ClaimE2EBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-reap-e2e-")
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
        cls._close()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_LOG_FILE", "E2E_PHONE", "E2E_DAY",
                  "E2E_OUT", "E2E_DB", "E2E_ENV_FILE", "E2E_REPO", "E2E_OWNER",
                  "E2E_MODE"):
            os.environ.pop(k, None)

    @staticmethod
    def _close():
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def setUp(self):
        self._close()
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)

    def tearDown(self):
        self._close()


# ---------------------------------------------------------------------------
# e2e 组 2：崩溃心跳停摆 ⇒ 租约到期接管 + 原主被 fence
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# e2e 组 3：监督进程存活、执行体子进程死亡 ⇒ 轮末显式收尸
# ---------------------------------------------------------------------------
@unittest.skipUnless(os.name == "posix" and hasattr(signal, "SIGKILL"),
                     "SIGKILL 模拟崩溃仅 POSIX 可用")


# ---------------------------------------------------------------------------
# e2e 组 4：v3 队列（sign_tasks）的对称收尸——被杀子进程的在领任务回退待领
# ---------------------------------------------------------------------------
@unittest.skipUnless(os.name == "posix" and hasattr(signal, "SIGKILL"),
                     "SIGKILL 模拟崩溃仅 POSIX 可用")
class SupervisorReapDeadWorkerV3E2ETest(_ClaimE2EBase):
    """真子进程用 `claim_batch` 领取任务后自杀：监督进程轮末把该任务回退待领。

    与组 3 对称：两套持有记录（旧表 `sign_claims` 与 v3 `sign_tasks`）必须在同一个轮末
    收尸路径里各自处置。不做的话，被杀执行体的在领任务没人回退，该账号当天无人再签——
    v3 侧的回退走 `queue_store.reap_abandoned`（进程句柄口径；任务级租约已随工单 `l1rx`
    删除，不再有"等租约+宽限过期"这条路）。
    """

    class _RealChildPopen:
        """替身 Popen：真的拉起一个 v3 领任务子进程，保留 poll 语义。

        `src_text` 决定子进程以哪种形状"死"——覆盖信号杀（rc<0）与退出码 1（未预期异常）
        两种。两种都必须被轮末收尸接住，故判据只能是「进程已退出」，不得看退出码。
        """

        src_text = _CLAIM_TASK_DIE_SRC

        def __init__(self, cmd, env=None, cwd=None):
            src = os.path.join(os.environ["E2E_OUT_DIR"], "claim_task_and_die.py")
            with open(src, "w", encoding="utf-8") as f:
                f.write(self.src_text)
            self.proc = _REAL_POPEN([sys.executable, src], env=env, cwd=cwd,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)

        def poll(self):
            return self.proc.poll()

    def _task_row(self, phone=PHONE, day=DAY):
        return dict(db.get_conn().execute(
            "SELECT * FROM sign_tasks WHERE phone=? AND day=?", (phone, day)).fetchone())

    def _run_supervisor_and_assert_reaped(self, popen_cls):
        """用给定形态的子进程跑一次监督进程，断言它留下的在领任务被轮末收尸。"""
        out_dir = os.path.join(self.tmp, "child_v3")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "claim.json")
        os.environ.update({
            "E2E_OUT_DIR": out_dir,
            "E2E_OUT": out_path,
            "E2E_DB": self.db_file,
            "E2E_ENV_FILE": self.env_file,
            "E2E_PHONE": PHONE,
            "E2E_DAY": DAY,
            "E2E_REPO": BASE,
        })
        self.addCleanup(os.environ.pop, "E2E_OUT_DIR", None)
        # 前置：一条已到期的待领任务（子进程的 claim_batch 领它）
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (PHONE, DAY, 0, "", _ts(seconds=-1), 5, "pending", 0, "", "",
             _ts(seconds=-60)))
        conn.commit()
        acc = SimpleNamespace(phone=PHONE, user_paused=False, owner="u", password="p",
                              account_id=0)
        with mock.patch.object(workers.cli_support, "_acquire_run_lock",
                               return_value=None), \
                mock.patch.object(workers.accounts_mod, "load_accounts",
                                  return_value=[acc]), \
                mock.patch.object(workers.subprocess, "Popen", popen_cls):
            rc = workers.run_worker_supervisor(1, ["--workers", "1"], slots=[0])
        self.assertIn(rc, (0, 1, 2, 3, 10), "退出码必须落在契约内")
        # 前置佐证：子进程确实以运行时身份领到了那条任务（不是它根本没跑起来）
        with open(out_path, encoding="utf-8") as f:
            claimed = json.load(f)
        self.assertEqual(claimed["n"], 1, "子进程必须真的领到那条任务")
        self.assertIn("worker-0@", claimed["owner"], "持有者是该槽位的运行时身份")
        row = self._task_row()
        self.assertEqual(row["state"], "pending",
                         "死执行体的在领任务必须在轮末回退待领（不是干等租约+宽限期）")
        self.assertEqual(row["lease_until"], "", "收尸必须放开租约")
        self.assertEqual(row["owner"], "", "回退后的行不再归任何执行体")
        self.assertGreater(row["epoch"], claimed["epoch"],
                           "收尸自增 epoch，fence 原持有者迟到的旧代写")
        got = queue_store.claim_batch(OWNER_B, DAY, (0,), now=_ts())
        self.assertEqual([r["phone"] for r in got], [PHONE],
                         "收尸之后下一次 claim_batch 必须能立刻拿到该任务")
        self.assertGreater(got[0]["epoch"], row["epoch"], "再领取换最新一代 fencing token")

    def test_round_end_reaps_claimed_task_of_killed_v3_child(self):
        """被杀（SIGKILL，rc<0）子进程在 v3 队列留下的在领任务，轮末必须回退待领。"""
        self._run_supervisor_and_assert_reaped(self._RealChildPopen)

    def test_round_end_reaps_claimed_task_of_exit1_v3_child(self):
        """rc=1（未预期异常）的子进程同样必须收尸——判据是进程已退出，不是退出码。

        旧实现 `if rc is not None and rc < 0: _reap_dead_worker(...)` 会漏掉本形态：
        该子进程没走到自己的收尾，留下的在领行当天永久卡死＝漏签。
        """
        popen_cls = type("_Exit1Popen", (self._RealChildPopen,),
                         {"src_text": _CLAIM_TASK_EXIT1_SRC})
        self._run_supervisor_and_assert_reaped(popen_cls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
