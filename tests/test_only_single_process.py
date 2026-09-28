# -*- coding: utf-8 -*-
"""手动签到 `--only` 的派发收敛与 terminate 进程树：单进程、无孤儿。

标签：B · 调度：领取/队列/执行体
覆盖：`runner.main` 在 `--only` 下**不派发**多执行体监督进程（即使 `--workers 2` 或执行体
   清单在场）；`--only` 的门豁免与退出码契约不变（拿不到锁仍返回 3）；一条 `--only`
   真进程树整轮 `attempt_signin` 调用数 == 1、引擎侧 `Popen` 记录 == 0、成功 rc ∈ {0}；
   web 侧 `terminate()` 杀**整个进程组**（进程组 kill，子进程随监督进程退出、不留孤儿）。
对应实现：yiban/engine/runner.py（`_dispatch` 的 `--only` 收敛判据）、
   web/services/manual_sign.py（`_terminate_signin_proc`）、
   web/routes/signin_api.py（终止旧子进程走进程树 kill）、
   web/services/manual_sign.py::_launch_signin_proc（`start_new_session=True`）。
关键断言：`--only` 的豁免只该豁免**门**，不该让派发照旧——同一单号被 N 个子进程各自
   领一次时，抢输的一路 rc=2 会让 web 把其实成功的那次点击写成"本轮未实际签到"；
   且 N 个子进程各持独立锁，只杀监督进程会留下孤儿。收敛为单进程后这三条危害都不存在，
   而"仍能返回 3"的既有语义必须保持。判据用**真进程树**（子进程记自己的 pid）与
   **进程表**（kill 后 pid 必须消失）证明，不用源码字符串。
依赖：真子进程（Python；sitecustomize 注入 attempt_signin 替身，绝不发网络请求）+
   临时 sqlite/.env/状态目录；terminate 组在非 POSIX 平台 skip（无进程组语义）。无网络。
"""

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 包导入引导必须先于任何 yiban 导入：以文件路径直跑本模块时 `sys.path[0]` 只有 tests/，
# 仓库根不在路径上（tests/test_deploy_entry_imports.py 按位置钉住这条顺序）。
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from web.services import manual_sign  # noqa: E402
from yiban import status as yiban_status  # noqa: E402
from yiban.engine import runner, workers  # noqa: E402

PHONE = "13800138000"
TEST_KEY = "a" * 64


# ---------------------------------------------------------------------------
# 单元：`--only` 不派发监督进程（只进单进程路径），且显式路径参数已接线
# ---------------------------------------------------------------------------
class ManualOnlyDispatchConvergenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-only-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {
            "YIBAN_STATE_DIR": self.tmp,
            "YIBAN_LOG_FILE": os.path.join(self.tmp, "sign.log"),
            "YIBAN_RUN_LOCK_NAME": "",
        }, clear=False).start()
        for key in ("YIBAN_EXECUTOR_ID", "YIBAN_EXECUTORS", "YIBAN_SECOND_RUN"):
            os.environ.pop(key, None)

    def _acc(self):
        return SimpleNamespace(phone=PHONE, user_paused=False, owner="", password="p",
                               account_id=0)

    def test_only_with_workers_flag_does_not_dispatch_supervisor(self):
        ok_result = (True, "签到成功", False, yiban_status.STATUS_SUCCESS)
        with mock.patch.object(runner.accounts_mod, "load_accounts",
                               return_value=[self._acc()]), \
                mock.patch.object(runner.db, "purge_expired_deleted_accounts",
                                  lambda: None), \
                mock.patch.object(runner.cli_support, "_acquire_run_lock",
                                  return_value=None), \
                mock.patch.object(runner.state_io, "_is_second_run",
                                  return_value=False), \
                mock.patch.object(runner.state_io, "_load_cred_state",
                                  return_value={}), \
                mock.patch.object(runner.state_io, "_save_cred_state",
                                  lambda *a, **k: None), \
                mock.patch.object(runner.alerts, "_maybe_alert_zero_success",
                                  lambda *a, **k: None), \
                mock.patch.object(runner.alerts, "_flush_admin_mail_summary",
                                  lambda *a, **k: None), \
                mock.patch.object(runner.executor_v3, "run_executor_v3",
                                  return_value={PHONE: ok_result}) as m_run, \
                mock.patch.object(workers, "run_worker_supervisor") as m_sup:
            rc = runner.main(["--only", PHONE, "--workers", "2"])
        m_sup.assert_not_called()
        self.assertEqual(rc, 0)
        self.assertEqual(m_run.call_count, 1, "手动单号必须走进程内的单执行体路径")
        self.assertTrue(m_run.call_args.kwargs.get("reclaim"),
                        "手动指定账号保留重签已了结账号的语义")
        self.assertTrue(m_run.call_args.kwargs.get("requeue_final"),
                        "手动是显式路径：必须允许重领预算耗尽档的失败账号")
        self.assertFalse(m_run.call_args.kwargs.get("claim_all"),
                         "手动轮不得用通配宽分片：会把当日别人的行一并领走/回炉")

    def test_only_still_returns_3_when_lock_unavailable(self):
        """`--only` 仍能返回 3 的既有语义不变（锁不可用 = 队列忙）。"""
        with mock.patch.object(runner.accounts_mod, "load_accounts",
                               return_value=[self._acc()]), \
                mock.patch.object(runner.db, "purge_expired_deleted_accounts",
                                  lambda: None), \
                mock.patch.object(runner.cli_support, "_acquire_run_lock",
                                  side_effect=runner.cli_support._RunLockUnavailable("注入")), \
                mock.patch.object(runner.executor_v3, "run_executor_v3") as m_run, \
                self.assertLogs("yiban", "ERROR"):
            rc = runner.main(["--only", PHONE, "--workers", "2"])
        self.assertEqual(rc, 3)
        m_run.assert_not_called()


# ---------------------------------------------------------------------------
# e2e：真进程树
# ---------------------------------------------------------------------------
#: 注入到子进程的 `sitecustomize`：把 `attempt_signin` 换成记账替身（绝不发网络请求），
#: 并给 `subprocess.Popen` 记账。放在 PYTHONPATH 上 ⇒ 监督进程与它拉起的执行体子进程
#: **都**会加载它，因此"是否真的 fan-out"可用 attempt/popen 的 pid 直接读出来。
_SITECUSTOMIZE_SRC = r'''
import os
import subprocess as _sp

_REC = os.environ.get("M74_REC")


def _note(line):
    if not _REC:
        return
    try:
        with open(_REC, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


_POPEN = _sp.Popen


def _spy_popen(cmd, *a, **kw):
    try:
        shown = " ".join(str(c) for c in cmd)
    except TypeError:
        shown = "<cmd>"
    _note("popen pid=%d cmd=%s" % (os.getpid(), shown))
    return _POPEN(cmd, *a, **kw)


_sp.Popen = _spy_popen

try:
    from yiban.engine import attempts as _attempts

    def _fake_attempt(acc):
        _note("attempt pid=%d phone=%s" % (os.getpid(), acc.phone))
        return (True, "签到成功", False, "success")

    _attempts.attempt_signin = _fake_attempt
except Exception:
    pass
'''

#: 驱动脚本：以真 `runner.main(["--only", …])` 跑一轮（带 `--workers 2`，旧行为下会 fan-out）。
_DRIVER_SRC = r'''
import os, sys
sys.path.insert(0, os.environ["M74_REPO"])
from yiban.engine import runner

rc = runner.main(["--only", os.environ["M74_PHONE"], "--workers", "2"])
with open(os.environ["M74_REC"], "a", encoding="utf-8") as f:
    f.write("rc pid=%d rc=%d\n" % (os.getpid(), int(rc)))
sys.exit(int(rc))
'''

#: 持全局锁的进程：让 `--only` 撞锁、按既有语义返回 3。句柄必须**保活到进程结束**
#: （flock 随句柄释放），故存进模块级名字。
_HOLDER_SRC = r'''
import os, sys, time
sys.path.insert(0, os.environ["M74_REPO"])
from yiban.engine import cli_support
_LOCK = cli_support._acquire_run_lock(True)   # 缺省取全局锁 signin-run.lock
sys.stdout.write("HELD\n")
sys.stdout.flush()
time.sleep(float(os.environ.get("M74_HOLD", "30")))
del _LOCK
'''

#: 一棵真进程树：父进程再拉一个长睡子进程，两个 pid 都写盘（供进程表判定孤儿）。
_TREE_SRC = r'''
import os, subprocess, sys, time
with open(sys.argv[1], "w", encoding="utf-8") as f:
    f.write("%d\n" % os.getpid())
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
with open(sys.argv[1], "a", encoding="utf-8") as f:
    f.write("%d\n" % child.pid)
time.sleep(300)
'''


class _OnlyE2EBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-only-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.helper_dir = os.path.join(self.tmp, "helper")
        os.makedirs(self.helper_dir)
        with open(os.path.join(self.helper_dir, "sitecustomize.py"), "w",
                  encoding="utf-8") as f:
            f.write(_SITECUSTOMIZE_SRC)
        self.env_file = os.path.join(self.tmp, ".env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        self.rec = os.path.join(self.tmp, "rec.txt")
        self.db_file = os.path.join(self.tmp, "yiban.db")

    def _env(self):
        env = dict(os.environ)
        env.update({
            "M74_REPO": BASE,
            "M74_REC": self.rec,
            "M74_PHONE": PHONE,
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": self.env_file,
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_STATE_DIR": self.tmp,
            "YIBAN_LOG_FILE": os.path.join(self.tmp, "sign.log"),
            "YIBAN_ACCOUNTS_JSON": json.dumps([{"phone": PHONE, "password": "pw"}]),
            "PYTHONPATH": os.pathsep.join([BASE, self.helper_dir]),
        })
        for key in ("YIBAN_EXECUTOR_ID", "YIBAN_EXECUTORS", "YIBAN_RUN_LOCK_NAME",
                    "YIBAN_SECOND_RUN"):
            env.pop(key, None)
        return env

    def _records(self):
        if not os.path.exists(self.rec):
            return []
        with open(self.rec, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]

    def _run_driver(self):
        src = os.path.join(self.tmp, "driver.py")
        with open(src, "w", encoding="utf-8") as f:
            f.write(_DRIVER_SRC)
        return subprocess.run([sys.executable, src], env=self._env(), cwd=BASE,
                              capture_output=True, text=True, timeout=180)


class OnlyDoesNotTouchOtherAccountsE2ETest(_OnlyE2EBase):
    """`--only` 真跑一轮：库里**别的账号**的行与 sign-state 一字不变（FAIL-1 回归）。

    越界点：手动轮曾用通配宽分片 ⇒ 轮首把当日**所有** `failed` 回炉、`claim_batch` 再把
    当日**所有** `pending` 领走；不在本轮账号集里的行在 `_attempt` 走 `acc is None` 分支被
    `_finish(done/user_cancelled)`——不登录却判成了结，补签轮随后据 `pending_count=0` 判
    "已了结" ⇒ 静默漏签。本用例把同分片、已到期的"别的账号"种进去，跑真进程树的
    `--only`，断言它们的行与 sign-state 完全没动。
    """

    OTHER_PENDING = "13900000077"
    OTHER_FAILED = "13900000078"

    def _today(self):
        from yiban import clock
        return clock.now().strftime("%Y-%m-%d")

    def _parent_env(self):
        return {
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": self.env_file,
            "YIBAN_DB_FILE": self.db_file,
            "YIBAN_STATE_DIR": self.tmp,
        }

    #: 当日虚分片数：**种子里就要钉死**，否则真进程 `_ensure_plan` 取 `_max_vshard+1`
    #: 作 V ⇒ 与本用例算出的 V 不等，"同分片最坏几何"实际不成立（E2E 会在变异下仍绿）。
    V = 64

    def _seed_other_rows(self):
        """把当日 V 钉死，并在**实际会采用的 V** 下的同一分片里种别人的行。"""
        from yiban.engine import executor_v3, hrw, state_io
        from yiban.store import clock_meta, connection
        from yiban.store import db as store_db
        store_db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        day = self._today()
        # 钉死当日 V：落进 app_meta 的 V 元数据键 ⇒ 真进程 `_plan_v` 只读它（不再按
        # `_max_vshard+1` 兜底），故这里算的分片与执行体实际使用的分片**逐值一致**。
        clock_meta.set_meta(executor_v3.V_META_KEY_PREFIX + day, self.V)
        sh = hrw.vshard_of(PHONE, day, self.V)   # 与本轮账号同分片（最坏几何）
        self.assertEqual(
            hrw.vshard_of(PHONE, day, hrw.v_for(1)), sh,
            "夹具前提：本轮账号在 v_for(1) 与本用例钉死的 V 下都落在同一分片")
        conn = store_db.get_conn()
        with store_db._conn_lock:
            for phone, state, result in (
                    (self.OTHER_PENDING, "pending", ""),
                    (self.OTHER_FAILED, "failed", "retry:skipped_window")):
                conn.execute(
                    "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, "
                    "priority, state, attempts, lease_until, result, epoch, created_at) "
                    "VALUES (?,?,?,'','2026-01-01 00:00:00.000',5,?,0,'',?,0,"
                    "'2026-01-01 00:00:00.000')", (phone, day, sh, state, result))
            conn.commit()
        # 父进程写状态文件要先指到本轮 state 目录（否则落到宿主真实目录）
        with mock.patch.dict(os.environ, self._parent_env(), clear=False):
            state_io._write_sign_state(self.OTHER_PENDING, "pending", "计划 06:31")
        rows = {p: tuple(conn.execute(
            "SELECT state, owner, result, epoch FROM sign_tasks WHERE phone=? AND day=?",
            (p, day)).fetchone()) for p in (self.OTHER_PENDING, self.OTHER_FAILED)}
        conn.close()
        connection.reset_conn()
        return day, rows

    def _read_other(self, day):
        import json as _json

        from yiban.store import connection
        from yiban.store import db as store_db
        store_db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        conn = store_db.get_conn()
        with store_db._conn_lock:
            rows = {p: tuple(conn.execute(
                "SELECT state, owner, result, epoch FROM sign_tasks WHERE phone=? AND day=?",
                (p, day)).fetchone()) for p in (self.OTHER_PENDING, self.OTHER_FAILED)}
        conn.close()
        connection.reset_conn()
        with open(os.path.join(self.tmp, f"sign-state-{day}.json"),
                  encoding="utf-8-sig") as f:
            state = _json.load(f).get(self.OTHER_PENDING)
        return rows, state

    def test_manual_round_leaves_other_accounts_untouched(self):
        import json as _json
        day, before = self._seed_other_rows()
        with open(os.path.join(self.tmp, f"sign-state-{day}.json"),
                  encoding="utf-8-sig") as f:
            before_state = _json.load(f).get(self.OTHER_PENDING)
        proc = self._run_driver()
        self.assertEqual(proc.returncode, 0,
                         f"stderr={proc.stderr!r}\nrecords={self._records()}")
        after, after_state = self._read_other(day)
        self.assertEqual(after, before,
                         "别的账号的行状态/owner/result/epoch 一字不变")
        self.assertEqual(after_state, before_state,
                         "别的账号的 sign-state 不得被改写成 user_cancelled")


class OnlyProcessTreeE2ETest(_OnlyE2EBase):
    """一条 `--only` 真进程树：attempt 恰 1 次、引擎侧 Popen 0 次、成功 rc=0。"""

    def test_single_engine_process_attempts_once(self):
        proc = self._run_driver()
        self.assertEqual(proc.returncode, 0,
                         f"stderr={proc.stderr!r}\nrecords={self._records()}")
        records = self._records()
        attempts = [r for r in records if r.startswith("attempt ")]
        # 只数**引擎自己**拉起的执行体子进程（`yiban.cli sign …`）：解释器/依赖启动期
        # 可能另有零星的探测性 Popen（与本条无关），不能把它们混进来判 fan-out。
        engine_popens = [r for r in records
                         if r.startswith("popen ") and "yiban.cli sign" in r]
        rcs = [r for r in records if r.startswith("rc ")]
        self.assertEqual(len(attempts), 1,
                         f"整轮 attempt_signin 必须恰好 1 次，实际 {records}")
        self.assertEqual(len(engine_popens), 0,
                         "引擎自身不得再拉起执行体子进程（Popen 记录 ≤1 由 web 的一次 spawn 用掉）")
        self.assertEqual(len(rcs), 1)
        # 所有活动必须都发生在**同一个 pid**（驱动进程自己）——真进程树里只有它
        pids = {r.split("pid=")[1].split()[0] for r in records}
        self.assertEqual(len(pids), 1, f"只应有一个引擎进程，实际 pids={pids}")

    def test_only_still_returns_3_when_global_lock_held(self):
        holder_src = os.path.join(self.tmp, "holder.py")
        with open(holder_src, "w", encoding="utf-8") as f:
            f.write(_HOLDER_SRC)
        holder_env = self._env()
        holder = subprocess.Popen([sys.executable, holder_src], env=holder_env, cwd=BASE,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "HELD",
                             "持锁进程必须先真的拿到全局锁")
            proc = self._run_driver()
            self.assertEqual(proc.returncode, 3,
                             f"--only 撞全局锁必须按既有语义返回 3；stderr={proc.stderr!r}")
            self.assertEqual([r for r in self._records() if r.startswith("attempt ")], [])
        finally:
            holder.kill()
            holder.wait(timeout=30)


@unittest.skipUnless(os.name == "posix", "进程组 kill 仅 POSIX 有语义")
class TerminateProcessTreeE2ETest(_OnlyE2EBase):
    """`_terminate_signin_proc` 杀整个进程组：父与它拉起的子进程都从进程表消失。"""

    @staticmethod
    def _alive(pid):
        """pid 是否仍是**活**进程（僵尸算死）：孤儿判据不能被未回收的僵尸骗过。

        Linux/WSL 有 `/proc` 时读 state 字符（`Z` = 僵尸 = 已死但父进程未回收）；
        没有 `/proc` 的平台退回 `kill(pid, 0)` 探活。
        """
        if os.path.isdir("/proc"):
            try:
                with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                    data = f.read()
            except OSError:
                return False
            # state 是 comm 字段（括号内可能含空格/括号）之后的第一个字符
            try:
                state = data[data.rindex(")") + 2]
            except (ValueError, IndexError):
                return True
            return state != "Z"
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def test_terminate_leaves_no_orphan_child(self):
        pidfile = os.path.join(self.tmp, "tree.txt")
        src = os.path.join(self.tmp, "tree.py")
        with open(src, "w", encoding="utf-8") as f:
            f.write(_TREE_SRC)
        # 与 `_launch_signin_proc` 同一形态：新会话 ⇒ 自成进程组，可整组 kill
        proc = subprocess.Popen([sys.executable, src, pidfile], env=self._env(), cwd=BASE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            deadline = time.time() + 30
            pids = []
            while time.time() < deadline:
                if os.path.exists(pidfile):
                    pids = [int(x) for x in open(pidfile, encoding="utf-8").read().split()]
                if len(pids) == 2:
                    break
                time.sleep(0.05)
            self.assertEqual(len(pids), 2, "前置：父与子两个进程都要在进程表里")
            parent_pid, child_pid = pids
            self.assertTrue(self._alive(parent_pid), "前置：监督进程必须活着")
            self.assertTrue(self._alive(child_pid), "前置：子进程必须真的活着（不是启动失败）")

            manual_sign._terminate_signin_proc(proc)

            deadline = time.time() + 20
            while time.time() < deadline:
                proc.poll()   # 回收父进程，否则它作为僵尸会被误判成"还活着"
                if proc.returncode is not None and not self._alive(child_pid):
                    break
                time.sleep(0.1)
            proc.poll()
            self.assertIsNotNone(proc.returncode, "监督进程必须退出")
            self.assertFalse(self._alive(child_pid),
                             "子进程必须随监督进程一起退出——只杀监督进程会留下孤儿")
        finally:
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
