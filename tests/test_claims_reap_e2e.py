# -*- coding: utf-8 -*-
"""领取面两步显式处置的 e2e：心跳停摆后的租约接管，与轮末对死亡执行体的收尸。

标签：B · 调度：领取/队列/执行体
覆盖：真子进程领取后**持续心跳**的在领行不可被接管；心跳停摆（真进程被 kill）后
   租约到期，接管方成功拿到更大的 epoch，原主用旧 epoch 的收尾/续租/弃权全被 fence；
   监督进程存活而执行体子进程异常死亡的场景下，轮末按"已知死亡"显式收尸（置 failed +
   放开租约），下一轮可立刻接手。
对应实现：yiban/store/claims.py（try_claim 的租约判据 / touch / reap_unreported /
   reap_abandoned）、yiban/engine/workers.py（_await_workers 后的轮末收尸）、
   yiban/engine/round.py（心跳与轮末收尸的调用点）。
关键断言：两条路径都必须**显式**——活执行体持续心跳时接管者必须被拒（不是"也许没
   过期"），停心跳后必须恰好经由"租约过期 + epoch fencing"接手（不是静默并发双领取）。
   "无静默双登录"的判据是记账：活体期间 attempts 不增、接管后旧主的终态写被拒、库里
   只可能留下接管方的结论。
依赖：临时 sqlite + 两个/三个真子进程（POSIX：SIGKILL 模拟崩溃）；组 3 在非 POSIX
   平台 skip（无 SIGKILL）；无网络请求。
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
import time
import unittest
from types import SimpleNamespace
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402

from yiban import clock  # noqa: E402
from yiban.engine import workers  # noqa: E402
from yiban.store import claims  # noqa: E402

#: 真 `subprocess.Popen` 的原始引用：组 3 要把它换成"拉真子进程"的替身，
#: 替身内部必须用原引用，否则替换会递归。
_REAL_POPEN = subprocess.Popen

TEST_KEY = "a" * 64
DAY = "2026-09-25"
PHONE = "13800138000"
OWNER_A = "worker-0@testhost:111:090000"
OWNER_B = "worker-1@testhost:222:090000"


def _ts(**kw):
    return (clock.now() + datetime.timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


_CLAIM_HOLD_SRC = r"""
import json, os, signal, sys, time
sys.path.insert(0, os.environ["E2E_REPO"])
from yiban.store import db
owner = os.environ["E2E_OWNER"]
db.init_db(os.environ["E2E_DB"], env_file=os.environ.get("E2E_ENV_FILE") or None,
           cleanup=False)
ok, epoch = db.claim_sign_account(os.environ["E2E_PHONE"], os.environ["E2E_DAY"], owner)
with open(os.environ["E2E_OUT"], "w", encoding="utf-8") as f:
    json.dump({"ok": bool(ok), "epoch": int(epoch), "owner": owner}, f)
if os.environ.get("E2E_MODE") == "beat":
    while True:
        # 持续心跳：这正是"活执行体"的定义——它的在领行永不过期。
        db.claim_touch(os.environ["E2E_PHONE"], os.environ["E2E_DAY"], owner, epoch=epoch)
        time.sleep(0.2)
time.sleep(600)
"""

_CLAIM_DIE_SRC = r"""
import json, os, signal, sys
sys.path.insert(0, os.environ["E2E_REPO"])
from yiban import egress
from yiban.store import db
owner = egress.runtime_owner(os.environ["YIBAN_EXECUTOR_ID"])
db.init_db(os.environ["E2E_DB"], env_file=os.environ.get("E2E_ENV_FILE") or None,
           cleanup=False)
ok, epoch = db.claim_sign_account(os.environ["E2E_PHONE"], os.environ["E2E_DAY"], owner)
with open(os.environ["E2E_OUT"], "w", encoding="utf-8") as f:
    json.dump({"ok": bool(ok), "epoch": int(epoch), "owner": owner}, f)
os.kill(os.getpid(), signal.SIGKILL)   # 模拟崩溃：来不及收尾
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

    def _row(self, phone=PHONE):
        return dict(db.get_conn().execute(
            "SELECT * FROM sign_claims WHERE phone=? AND day=?", (phone, DAY)).fetchone())

    def _child_env(self, owner):
        env = dict(os.environ)
        env.update({
            "E2E_REPO": BASE,
            "E2E_DB": self.db_file,
            "E2E_ENV_FILE": self.env_file,
            "E2E_PHONE": PHONE,
            "E2E_DAY": DAY,
            "E2E_OWNER": owner,
            "E2E_OUT": os.path.join(self.tmp, "claim.json"),
        })
        return env


# ---------------------------------------------------------------------------
# e2e 组 2：崩溃心跳停摆 ⇒ 租约到期接管 + 原主被 fence
# ---------------------------------------------------------------------------
class CrashedWorkerTakeoverE2ETest(_ClaimE2EBase):
    """真进程领取：活着（持续心跳）时不可接管；被 kill 后租约到期才可接管。"""

    def _spawn(self, mode, owner):
        src = os.path.join(self.tmp, f"holder-{mode}.py")
        with open(src, "w", encoding="utf-8") as f:
            f.write(_CLAIM_HOLD_SRC)
        env = self._child_env(owner)
        env["E2E_MODE"] = mode
        with open(env["E2E_OUT"], "w", encoding="utf-8") as f:
            f.write("")   # 先占位：等子进程覆盖它即"已领取"
        return subprocess.Popen([sys.executable, src], env=env, cwd=BASE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _await_claim(self, out_path, proc):
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                self.fail(f"子进程提前退出（rc={proc.returncode}），未完成领取")
            with open(out_path, encoding="utf-8") as f:
                raw = f.read().strip()
            if raw:
                return json.loads(raw)
            time.sleep(0.05)
        self.fail("等待子进程领取超时")

    def test_live_heartbeat_blocks_takeover_then_expiry_allows_fenced_takeover(self):
        proc = self._spawn("beat", OWNER_A)
        try:
            claimed = self._await_claim(os.path.join(self.tmp, "claim.json"), proc)
            self.assertTrue(claimed["ok"], "前置：子进程必须真的领到")
            attempts_before = self._row()["attempts"]
            # ① 活执行体持续心跳 ⇒ 在领行永不过期，接管者不可抢
            self.assertEqual(db.claim_sign_account(PHONE, DAY, OWNER_B), (False, 0),
                             "活执行体仍在心跳时接管必须被拒")
            self.assertEqual(self._row()["attempts"], attempts_before,
                             "被拒的接管不得记账 —— 那是'静默双登录'的计数入口")
        finally:
            proc.kill()
            proc.wait(timeout=30)
        # ② 心跳停摆：让 900s 租约在测试里一次性流逝（等价于真的等了租约）
        with db._conn_lock:
            db.get_conn().execute(
                "UPDATE sign_claims SET heartbeat_at=? WHERE phone=? AND day=?",
                (_ts(seconds=-claims.LEASE_SECONDS), PHONE, DAY))
            db.get_conn().commit()
        ok, e_new = db.claim_sign_account(PHONE, DAY, OWNER_B)
        self.assertTrue(ok, "心跳停摆且租约过期后，接管方必须能成功（崩溃自愈）")
        self.assertGreater(e_new, claimed["epoch"], "接管必须换新代（旧代随即作废）")
        row = self._row()
        self.assertEqual((row["owner"], row["state"]), (OWNER_B, db.CLAIM_STATE_CLAIMED))
        # ③ 原主（已死进程）的旧代写一律被 fence：库里只可能留下接管方的结论
        e_old = claimed["epoch"]
        self.assertFalse(db.claim_settle(PHONE, DAY, OWNER_A, db.CLAIM_STATE_DONE,
                                        "原主的迟到结论", epoch=e_old))
        self.assertFalse(db.claim_give_up(PHONE, DAY, OWNER_A, "原主迟到弃权", epoch=e_old))
        self.assertFalse(db.claim_touch(PHONE, DAY, OWNER_A, epoch=e_old))
        row = self._row()
        self.assertEqual(row["result"], "", "原主的结论不得落库（否则一次登录被记两次结论）")
        self.assertEqual(row["state"], db.CLAIM_STATE_CLAIMED, "行仍由接管方持有")
        self.assertTrue(db.claim_settle(PHONE, DAY, OWNER_B, db.CLAIM_STATE_DONE,
                                        "接管方的结论", epoch=e_new))
        self.assertEqual(self._row()["state"], db.CLAIM_STATE_DONE)


# ---------------------------------------------------------------------------
# e2e 组 3：监督进程存活、执行体子进程死亡 ⇒ 轮末显式收尸
# ---------------------------------------------------------------------------
@unittest.skipUnless(os.name == "posix" and hasattr(signal, "SIGKILL"),
                     "SIGKILL 模拟崩溃仅 POSIX 可用")
class SupervisorReapDeadWorkerE2ETest(_ClaimE2EBase):
    """真子进程领取后自杀：监督进程判它异常退出，轮末把它的在领行显式了结。"""

    class _RealChildPopen:
        """替身 Popen：真的拉起一个子进程（而不是 yiban CLI），保留 poll 语义。"""

        def __init__(self, cmd, env=None, cwd=None):
            src = os.path.join(os.environ["E2E_OUT_DIR"], "claim_and_die.py")
            with open(src, "w", encoding="utf-8") as f:
                f.write(_CLAIM_DIE_SRC)
            self.proc = _REAL_POPEN([sys.executable, src], env=env, cwd=cwd,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)

        def poll(self):
            return self.proc.poll()

    def test_round_end_reaps_claims_of_killed_child(self):
        out_dir = os.path.join(self.tmp, "child")
        os.makedirs(out_dir, exist_ok=True)
        os.environ["E2E_OUT_DIR"] = out_dir
        os.environ["E2E_OUT"] = os.path.join(out_dir, "claim.json")
        os.environ["E2E_DB"] = self.db_file
        os.environ["E2E_ENV_FILE"] = self.env_file
        os.environ["E2E_PHONE"] = PHONE
        os.environ["E2E_DAY"] = DAY
        os.environ["E2E_REPO"] = BASE
        self.addCleanup(os.environ.pop, "E2E_OUT_DIR", None)
        acc = SimpleNamespace(phone=PHONE, user_paused=False, owner="u", password="p",
                              account_id=0)
        with mock.patch.object(workers.cli_support, "_acquire_run_lock",
                               return_value=None), \
                mock.patch.object(workers.accounts_mod, "load_accounts",
                                  return_value=[acc]), \
                mock.patch.object(workers.subprocess, "Popen",
                                  self._RealChildPopen):
            rc = workers.run_worker_supervisor(1, ["--workers", "1"], slots=[0])
        self.assertIn(rc, (0, 1, 2, 3, 10), "退出码必须落在契约内")
        row = self._row()
        self.assertEqual(row["state"], db.CLAIM_STATE_FAILED,
                         "子进程异常死亡 ⇒ 轮末必须显式了结它的在领行（不是留到 900s 后）")
        self.assertLessEqual(row["heartbeat_at"], _ts(seconds=-claims.LEASE_SECONDS),
                             "收尸必须放开租约，下一轮才能立刻接手")
        self.assertIn("worker-0@", row["owner"], "归属仍是那个已死的槽位身份")
        # 下一轮（或另一个执行体）必须能立刻接手，不留给下一轮误判
        self.assertTrue(db.claim_sign_account(PHONE, DAY, OWNER_B)[0],
                        "收尸之后接管必须立刻可行")


if __name__ == "__main__":
    unittest.main(verbosity=2)
