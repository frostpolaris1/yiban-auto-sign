# -*- coding: utf-8 -*-
"""运行锁三条 fail-open 的收敛：拿不到锁一律拒跑，四个调用点都检查返回值。

标签：B · 调度：领取/队列/执行体
覆盖：`_acquire_run_lock` 的三条 fail-open（阻塞等待超时"无锁继续"、锁文件 `OSError`
   零日志放行、无 fcntl 时交出**未加锁**句柄）全部改 fail-closed；不可用时显式抛
   `_RunLockUnavailable` 并留 ERROR 日志；四个调用点（兜底常驻 / 探针 / 单执行体主路径 /
   多执行体监督进程）各自把它映射到**既有**退出码族（锁忙 3 / 跳过 0）且不再继续跑；
   `_run_lock_held` 探测不出锁状态时按"有人在跑"处置；两个真子进程抢同一把锁时恰一个
   运行、另一个以 rc=3 拒绝且日志有因。
对应实现：yiban/engine/cli_support.py（`_acquire_run_lock` / `_run_lock_held` /
   `_RunLockUnavailable`）、yiban/engine/runner.py（三处调用点）、
   yiban/engine/workers.py（监督进程的全局锁）。
关键断言：三条路径都必须**抛**，而不是交出未加锁句柄或静默继续——"拿不到锁就继续"
   在多执行体下等于同一账号被两个进程并发真实登录（本项目第一红线）；只改判据不改状态
   是不够的，四个调用点必须真的中断本轮（退出码在 0/1/2/3/10 契约内，不新增码值）。
   判别力：把三条注入分别拿掉（打桩回旧行为）本文件的用例必须红。
依赖：临时目录（作为 YIBAN_STATE_DIR）+ 打桩 `builtins` 的 `open` 与模块级 `fcntl` +
   两个真子进程（并发组，POSIX flock）；无网络请求。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from yiban.engine import cli_support, runner, workers  # noqa: E402


def _unavailable(reason="锁不可用（测试注入）"):
    return cli_support._RunLockUnavailable(reason)


class _StateDirBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-runlock-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {
            "YIBAN_STATE_DIR": self.tmp,
            "YIBAN_LOG_FILE": os.path.join(self.tmp, "sign.log"),
            "YIBAN_RUN_LOCK_NAME": "",
        }, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)


# ---------------------------------------------------------------------------
# 三条 fail-open 路径 → fail-closed
# ---------------------------------------------------------------------------
class AcquireFailClosedTest(_StateDirBase):
    """`_acquire_run_lock` 的三种"拿不到锁"都必须拒绝，而不是交出未加锁的句柄。"""

    def test_nonblocking_contention_still_raises_run_lock_held(self):
        """既有语义不变：非阻塞（--only/兜底）被占仍抛 `_RunLockHeld`，不是新异常。"""
        held = cli_support._acquire_run_lock(True, name="contend.lock")
        self.addCleanup(held.close)
        with self.assertRaises(cli_support._RunLockHeld):
            cli_support._acquire_run_lock(True, name="contend.lock")

    def test_blocking_timeout_refuses_instead_of_continuing_unlocked(self):
        """旧行为：等满 `YIBAN_RUN_LOCK_WAIT` 后 warning + **无锁继续**。现须拒绝。"""
        held = cli_support._acquire_run_lock(True, name="timeout.lock")
        self.addCleanup(held.close)
        with mock.patch.dict(os.environ, {"YIBAN_RUN_LOCK_WAIT": "0.5"}, clear=False),                 self.assertLogs("yiban", "ERROR") as cm,                 self.assertRaises(cli_support._RunLockUnavailable):
            cli_support._acquire_run_lock(False, name="timeout.lock")
        self.assertTrue(any("拒绝运行" in line for line in cm.output),
                        f"超时拒跑必须留 ERROR 日志说明原因，实际 {cm.output}")

    def test_open_failure_logs_and_refuses(self):
        """旧行为：`except OSError: return None`（零日志）。现须显式拒绝 + 留日志。"""
        with mock.patch.object(cli_support, "open", create=True,
                               side_effect=OSError("state dir 不可写")),                 self.assertLogs("yiban", "ERROR") as cm,                 self.assertRaises(cli_support._RunLockUnavailable):
            cli_support._acquire_run_lock(True, name="broken.lock")
        self.assertTrue(any("拒绝运行" in line for line in cm.output),
                        f"锁文件不可用必须留 ERROR 日志，实际 {cm.output}")

    def test_missing_fcntl_refuses_instead_of_unlocked_handle(self):
        """旧行为：无 fcntl 时 warning 后返回**未加锁**句柄。现须显式报不可用。"""
        with mock.patch.object(cli_support, "fcntl", None),                 self.assertLogs("yiban", "ERROR") as cm,                 self.assertRaises(cli_support._RunLockUnavailable):
            cli_support._acquire_run_lock(True, name="nofcntl.lock")
        self.assertTrue(any("不可用" in line for line in cm.output),
                        f"无锁后端必须显式报不可用，实际 {cm.output}")

    def test_probe_treats_unverifiable_lock_as_contended(self):
        """`_run_lock_held` 探测不出锁状态时按"有人在跑"处置（fail-closed）。

        旧行为返回 False（"锁不生效就不假装有人在跑"）——兜底常驻于是照样开跑，
        与无法探测到的全量轮抢同一批账号。探测不出来的安全侧是**让位**。
        """
        with mock.patch.object(cli_support, "fcntl", None), \
                mock.patch.object(cli_support, "_PROBE_UNAVAILABLE_WARNED", False),                 self.assertLogs("yiban", "WARNING") as cm:
            self.assertTrue(cli_support._run_lock_held(), "探测不出必须按被持有处置")
        self.assertTrue(any("不可用" in line or "无法探测" in line for line in cm.output),
                        f"降级必须留痕，实际 {cm.output}")


# ---------------------------------------------------------------------------
# 四个调用点：拒绝运行必须在调用点真的中断本轮（既有退出码族）
# ---------------------------------------------------------------------------
class CallSiteRefusalTest(_StateDirBase):
    """四个调用点各自把不可用信号落成既有退出码（3=锁忙 / 0=探针跳过）。"""

    def _acc(self, phone="13800138000"):
        return SimpleNamespace(phone=phone, user_paused=False, owner="", password="p",
                               account_id=0)

    def test_fallback_branch_returns_lock_busy_3(self):
        called = []
        with mock.patch.object(runner.cli_support, "_acquire_run_lock",
                               side_effect=_unavailable()), \
                mock.patch.object(workers, "run_fallback_worker",
                                  lambda argv: called.append(argv) or 0),                 self.assertLogs("yiban", "ERROR"):
            rc = runner.main(["--fallback"])
        self.assertEqual(rc, 3, "锁不可用必须按既有'锁忙'语义退出，不得继续跑")
        self.assertEqual(called, [], "拒绝之后不得再拉起兜底循环")

    def test_probe_branch_skips_with_0(self):
        """探针是完整登录的只读健康检查；拿不到互斥时与"已有签到在跑"同一处置（跳过）。"""
        with mock.patch.object(runner.cli_support, "_acquire_run_lock",
                               side_effect=_unavailable()), \
                mock.patch.object(runner.accounts_mod, "load_accounts",
                                  return_value=[self._acc()]), \
                mock.patch.object(runner.schedule_mod, "day_off", return_value=None), \
                mock.patch.object(runner.probe, "run_probe") as run_probe,                 self.assertLogs("yiban", "WARNING"):
            rc = runner.main(["--probe"])
        self.assertEqual(rc, 0, "探针跳过沿用既有 rc（与'已有签到进程在运行'一致）")
        run_probe.assert_not_called()

    def test_single_executor_main_returns_3(self):
        """主路径（全量/--only）：拿不到锁必须退出 3，不得进队列发起真实登录。"""
        with mock.patch.object(runner.cli_support, "_acquire_run_lock",
                               side_effect=_unavailable()), \
                mock.patch.object(runner.accounts_mod, "load_accounts",
                                  return_value=[self._acc()]), \
                mock.patch.object(runner.db, "purge_expired_deleted_accounts",
                                  lambda: None), \
                mock.patch.object(runner.round_mod, "run_queue_retry") as run_queue,                 self.assertLogs("yiban", "ERROR"):
            rc = runner.main(["--only", "13800138000"])
        self.assertEqual(rc, 3, "锁不可用必须按'锁忙'退出")
        run_queue.assert_not_called()

    def test_worker_supervisor_returns_3_without_spawning(self):
        spawned = []
        with mock.patch.object(workers.cli_support, "_acquire_run_lock",
                               side_effect=_unavailable()), \
                mock.patch.object(workers.subprocess, "Popen",
                                  lambda *a, **k: spawned.append(a) or None),                 self.assertLogs("yiban", "ERROR"):
            rc = workers.run_worker_supervisor(2, ["--workers", "2"])
        self.assertEqual(rc, 3, "监督进程拿不到全局锁不得拉起执行体")
        self.assertEqual(spawned, [], "拒绝之后不得 spawn 任何子进程")


# ---------------------------------------------------------------------------
# e2e：两个真子进程抢同一把锁
# ---------------------------------------------------------------------------
_HOLDER_SRC = r"""
import os, sys, time
sys.path.insert(0, os.environ["LOCK_E2E_REPO"])
from yiban.engine import cli_support
fh = cli_support._acquire_run_lock(True, name=os.environ["LOCK_E2E_NAME"])
sys.stdout.write("HELD\n")
sys.stdout.flush()
time.sleep(float(os.environ.get("LOCK_E2E_HOLD", "30")))
"""


class TwoProcessLockContentionE2ETest(_StateDirBase):
    """两个真子进程抢同一把锁：恰一个运行，另一个按既定退出码拒绝且日志有因。"""

    def _log_path(self):
        from yiban import clock
        return os.path.join(self.tmp, f"sign-{clock.now():%Y-%m-%d}.log")

    def test_second_process_refused_with_recorded_reason(self):
        holder_src = os.path.join(self.tmp, "holder.py")
        with open(holder_src, "w", encoding="utf-8") as f:
            f.write(_HOLDER_SRC)
        env = dict(os.environ)
        env.update({
            "LOCK_E2E_REPO": BASE,
            "LOCK_E2E_NAME": workers.FALLBACK_LOCK_NAME,
            "LOCK_E2E_HOLD": "20",
            "YIBAN_STATE_DIR": self.tmp,
            "YIBAN_LOG_FILE": os.path.join(self.tmp, "sign.log"),
            "YIBAN_RUN_LOCK_NAME": "",
        })
        holder = subprocess.Popen([sys.executable, holder_src], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  cwd=BASE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "HELD",
                             "子进程 A 必须真的持有锁（否则本用例什么都没测）")
            contender_env = dict(env)
            contender_env.pop("LOCK_E2E_REPO", None)
            contender_env.pop("LOCK_E2E_NAME", None)
            contender_env["YIBAN_RUN_LOCK_NAME"] = workers.FALLBACK_LOCK_NAME
            contender_env["PYTHONPATH"] = BASE
            proc = subprocess.run(
                [sys.executable, "-m", "yiban.cli", "sign", "--fallback"],
                env=contender_env, cwd=BASE, capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 3,
                             f"抢输的一方必须按'锁忙'退出 3，实际 {proc.returncode}；"
                             f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
            with open(self._log_path(), encoding="utf-8", errors="replace") as f:
                log_text = f.read()
            self.assertIn("已有兜底常驻执行体在运行", log_text,
                          "拒绝必须留下原因（日志有因，不是静默退出）")
        finally:
            holder.kill()
            holder.wait(timeout=30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
