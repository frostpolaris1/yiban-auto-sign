# -*- coding: utf-8 -*-
"""真跨进程文件锁争用 e2e：`locks.file_lock` 的互斥必须在**独立进程**间成立（140 §六-7）。

标签：B · 判据：并发保真

**本门补哪一维**：140 §五 S2 现量——`yiban/infra/locks.py` 有**两把**锁：每路径一把
进程内 `RLock`（`_LOCKS`，在 `_acquire` 的 flock **之前**取）与 portalocker 的真文件锁。
用**线程**验"跨进程互斥"的用例会被那把 RLock 先在进程内串行化，**根本碰不到 flock 那一维**。
⇒ 全仓没有一条用例跑过"两个独立进程真的抢同一把文件锁"。

**本门钉的性质（一条）**：同一路径的临界区在**独立进程**之间**不得重叠**——
任一进程进入时，别的进程不在里面。

**怎么观察**：三个独立子进程在同一发令时刻抢同一把锁；临界区入口放一个**原子哨兵**
（`O_CREAT|O_EXCL` 建文件）：建成功＝独占进入，`FileExistsError`＝别人还在里面，
记一次违规。用 `O_EXCL` 而不是"读一个标志文件"是因为读-写不原子：三个进程若都在任何
写入之前读到空值，重叠就观察不到（本门第一版实测踩到：绕过锁也报零违规）。
**前置就绪**：三个进程必须**都真的进过**临界区（否则"零违规"可能只是"谁都没跑"），
且锁文件必须真的被创建。

**牙**：把子进程里的 `file_lock` 换成 `contextlib.nullcontext()`（`PROBE_BYPASS=1`）⇒
临界区重叠 ⇒ 违规数 > 0。这证明"零违规"这条断言能识别"互斥没生效"，不是恒绿摆设。

**另一条同族判据**：`locks._acquire` 在重试超时/文件系统不支持时会**降级为进程内锁**
（跨进程互斥静默失效，只留一条 WARNING）。故本门同时断言子进程 stderr 里**没有**
"退化为进程内锁"——降级即红。

对应实现：yiban/infra/locks.py（`file_lock` / `_acquire` / `_get_rlock`）。
关键断言：钉"独立进程间临界区不重叠""三个竞位者都真的进去过""没有降级告警"三条性质；
    不钉任何调用顺序。
依赖：真子进程 ＋ portalocker（本仓运行期依赖）＋ 临时目录；无网络。
    两个平台都支持（portalocker 有 posix/win 两个后端），故**无 skip**。
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

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 竞位者数量：≥3 才能观察到"两个同时在里面"以外的重叠形态。
RACERS = 3
#: 临界区停留时长（秒）：足够长，让重叠必然发生（若互斥失效）。
HOLD_SEC = 0.35

_CHILD_SRC = r'''
import contextlib, json, os, sys, time

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)

from yiban.infra import locks  # noqa: E402

KEY = os.environ["PROBE_KEY"]
SENTINEL = os.environ["PROBE_SENTINEL"]
OUT = os.environ["PROBE_OUT"]
HOLD = float(os.environ["PROBE_HOLD"])
START = float(os.environ["PROBE_START"])
BYPASS = os.environ.get("PROBE_BYPASS") == "1"


def _emit(**kw):
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(kw, f)


def _take_sentinel():
    """原子占位：建成功＝独占进入；已存在＝别人还在里面。"""
    try:
        fd = os.open(SENTINEL, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def _drop_sentinel():
    try:
        os.unlink(SENTINEL)
    except OSError:
        pass


time.sleep(max(0.0, START - time.time()))

violations = 0
entered = False
cm = contextlib.nullcontext() if BYPASS else locks.file_lock(KEY)
with cm:
    entered = True
    if not _take_sentinel():
        violations += 1          # 别人还在里面 ⇒ 互斥没生效
    time.sleep(HOLD)
    _drop_sentinel()

_emit(pid=os.getpid(), entered=entered, violations=violations)
'''


class CrossProcessFileLockTest(unittest.TestCase):
    """三个独立进程抢同一把 `file_lock`：临界区不得重叠。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-flock-")
        self.child = os.path.join(self.tmp, "child.py")
        with open(self.child, "w", encoding="utf-8") as f:
            f.write(_CHILD_SRC)
        self.key = os.path.join(self.tmp, "protected.state")
        self.sentinel = os.path.join(self.tmp, "sentinel")
        self._procs = []

    def tearDown(self):
        for p in self._procs:
            if p.poll() is None:
                p.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    p.wait(timeout=30)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spawn(self, idx, bypass=False, start_at=None):
        out = os.path.join(self.tmp, "out-%d.json" % idx)
        env = dict(os.environ)
        env.update({
            "PROBE_KEY": self.key,
            "PROBE_SENTINEL": self.sentinel,
            "PROBE_OUT": out,
            "PROBE_HOLD": str(HOLD_SEC),
            "PROBE_START": str(start_at if start_at is not None else time.time() + 1.0),
            "PROBE_BYPASS": "1" if bypass else "0",
        })
        proc = subprocess.Popen([sys.executable, self.child, BASE], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self._procs.append(proc)
        return proc, out

    def _race(self, bypass=False):
        """三个进程同一发令时刻抢锁，返回 [(结果 dict, 子进程输出)]。"""
        start = time.time() + 1.0
        spawned = [self._spawn(i, bypass=bypass, start_at=start) for i in range(RACERS)]
        results = []
        for proc, out in spawned:
            body, _ = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0,
                             "子进程异常退出（rc=%s）：%s"
                             % (proc.returncode, (body or b"").decode("utf-8", "replace")))
            self.assertTrue(
                os.path.exists(out),
                "子进程 rc=0 却没有产出结果文件（%s）：本判据拿不到观察值，按红处置而不是放过"
                % out)
            with open(out, encoding="utf-8") as f:
                results.append((json.load(f), (body or b"").decode("utf-8", "replace")))
        return results

    def test_独立进程间临界区不重叠(self):
        results = self._race()
        entered = [r for r, _out in results if r["entered"]]
        self.assertEqual(
            len(entered), RACERS,
            "前置就绪：%d 个竞位者必须**都真的进过**临界区（实际 %d 个）。"
            "少了这条，'零违规'可能只是'谁都没跑'" % (RACERS, len(entered)))
        self.assertEqual(
            len({r["pid"] for r, _out in results}), RACERS,
            "竞位者必须是 %d 个**不同的进程**（同 PID 说明退回了线程/同进程）" % RACERS)
        violations = sum(r["violations"] for r, _out in results)
        self.assertEqual(
            violations, 0,
            "同一路径的临界区在独立进程之间重叠了 %d 次：`locks.file_lock` 的跨进程互斥"
            "失效（并发读-改-写会互相覆盖：丢安全开关、丢账本额度）" % violations)
        self.assertTrue(
            os.path.exists(self.key + ".lock"),
            "锁文件必须真的被创建（%s.lock 不存在说明加锁路径没走到）" % self.key)

    def test_没有降级为进程内锁(self):
        """`_acquire` 失败会**降级为进程内锁**（跨进程互斥静默失效，只留 WARNING）。

        降级即红：那正是 S2 形状的"看着有锁、实际没有"。
        """
        results = self._race()
        degraded = [(r["pid"], out) for r, out in results if "退化为进程内锁" in out]
        self.assertEqual(
            degraded, [],
            "子进程出现了'退化为进程内锁'告警（pid=%s）：跨进程互斥已失效，"
            "而业务照常继续——这正是本门要挡的形状" % [p for p, _o in degraded])

    def test_牙_绕过文件锁则临界区重叠(self):
        """牙：子进程改用 `nullcontext()`（不加锁）⇒ 违规数必须 > 0。

        证明上面那条"零违规"断言真的能识别"互斥没生效"，不是恒绿摆设。
        """
        results = self._race(bypass=True)
        entered = [r for r, _out in results if r["entered"]]
        self.assertEqual(len(entered), RACERS, "前置就绪：绕过组也必须三个都跑完")
        violations = sum(r["violations"] for r, _out in results)
        self.assertGreater(
            violations, 0,
            "绕过文件锁后仍报零违规——说明本门的违规探针测不到重叠，"
            "上面那条'零违规'断言是恒绿摆设")


if __name__ == "__main__":
    unittest.main(verbosity=2)
