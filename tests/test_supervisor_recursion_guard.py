# -*- coding: utf-8 -*-
"""多执行体的**子进程不得再当监督进程**（2026-09-17 对抗性审查 H1 的钉版回归）。

标签：B · 调度：领取/队列/执行体
覆盖：有子进程身份时 runner.main 不再派发监督进程：父进程照旧派发（反向控制）、带
   YIBAN_EXECUTOR_ID 的子进程不派发、显式 --workers 与 --workers=2
   两种写法都挡、兜底常驻身份同样挡。
对应实现：yiban/engine/runner.py（子进程身份短路）、yiban/engine/workers.py（run_worker_supervisor
   的调用点）、yiban/egress.py（worker_owner / fallback_owner 注入的
   YIBAN_EXECUTOR_ID 取值）。
关键断言：清单是环境变量，子进程原样继承，故「给子进程 argv 去掉
   --workers」挡不住清单路径——它根本不经
   argv。判据必须是身份本身：短路的依据是YIBAN_EXECUTOR_ID
   存在，而不是参数形状。反向控制同权重：没有子进程身份时照旧按清单派发，别把正常路径也挡了。业务时刻钉成工作日，否则周末跑测时「周末签到未开启」的提前门会先拦下、派发根本走不到。
依赖：环境变量夹具 + 打桩 workers.run_worker_supervisor（不 spawn
   真进程）；子进程用例钉窗口外业务钟并把 STATE_DIR/LOG_FILE/LOCK_DIR/DB_FILE
   钉进临时目录（MF-114 修复：此前裸真实时钟 + 真实状态目录/锁/仓库根库，
   真实时钟落进签到窗口时是真登录外联 + 重试睡眠，全量套件曾在该窗口挂起）。
   不发网络请求、不触真实目录。整文件在本机执行，无 skip。

用法（项目根目录）：
    py -m pytest tests/test_supervisor_recursion_guard.py -v
"""
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

from yiban import egress  # noqa: E402
from yiban.engine import runner, workers  # noqa: E402

MANIFEST = egress.dump_manifest([
    {"slot": 0, "type": "worker", "proxy": ""},
    {"slot": 1, "type": "worker", "proxy": ""}, # 两行 worker：一旦递归就是 2 个子进程，短路失效立刻可见
])
ACCOUNTS = '[{"phone":"13800000000","password":"x"}]' # 账号必须非空：零账号守卫会提前 return，派发这条路根本走不到

#: 固定业务时刻（周三 06:40，非周末、非暂停）：派发用例与"跑测当天是星期几"解耦，
#: 否则周末跑测时「周末签到未开启」的提前门会先拦下，派发根本走不到
WEEKDAY_06_40 = datetime(2026, 9, 2, 6, 40)
#: 固定业务时刻（周三午间）：**窗口外**。子进程三个用例的 runner.main 在派发短路后
#: 会继续走完一轮（真实时钟下落在签到窗口内时就是真登录外联 + 重试睡眠——MF-114
#: 挂起的时间条件），钉在窗口外让该轮确定性收敛为"窗口外跳过"（rc=2、零外联）。
WEEKDAY_NOON = datetime(2026, 9, 2, 12, 0)


class SupervisorRecursionGuardTest(unittest.TestCase):
    """有 `YIBAN_EXECUTOR_ID` = 已经是执行体子进程 → 绝不再派发监督进程。"""

    def setUp(self):
        self._old = {k: os.environ.get(k) for k in
                     ("YIBAN_EXECUTORS", "YIBAN_ACCOUNTS_JSON", "YIBAN_EXECUTOR_ID")}
        os.environ.update({"YIBAN_EXECUTORS": MANIFEST, "YIBAN_ACCOUNTS_JSON": ACCOUNTS})
        os.environ.pop("YIBAN_EXECUTOR_ID", None)
        self.calls = []
        p = mock.patch.object(workers, "run_worker_supervisor",
                              lambda n, argv, slots=None, migrate=True:
                              (self.calls.append((n, slots)), 0)[1])
        p.start()
        self.addCleanup(p.stop)
        self._isolate_runtime()

    def _isolate_runtime(self):
        """MF-114 修复：运行面全隔离，"是否挂起"从时间条件变确定性判据。

        三个子进程用例的 `runner.main` 在派发短路后**会继续走完一轮**：真实时钟
        落在签到窗口（06:31-07:49）时是真登录外联 + 重试睡眠（全量套件挂起的
        时间条件）；写盘落点是真实 `/var/log/yiban`、进程锁是真实 `/var/lock/yiban`、
        领取池写的是**仓库根的真实 yiban.db**（实测复现：跑一次本文件，仓库根库文件
        mtime 即刷新）。四路（状态/日志/锁/库）全部钉进本用例临时目录，兜底常驻
        开关一并摘除（防全量套件里其他用例的env 泄漏把它点亮），恢复交 addCleanup。
        """
        self._old_runtime = {k: os.environ.get(k) for k in
                             ("YIBAN_STATE_DIR", "YIBAN_LOG_FILE", "YIBAN_LOCK_DIR",
                              "YIBAN_DB_FILE", "YIBAN_FALLBACK_ENABLE")}
        self._tmp = tempfile.mkdtemp(prefix="yiban-supv-")
        os.environ.update({
            "YIBAN_STATE_DIR": self._tmp,
            "YIBAN_LOG_FILE": os.path.join(self._tmp, "sign.log"),
            "YIBAN_LOCK_DIR": os.path.join(self._tmp, "lock"),
            "YIBAN_DB_FILE": os.path.join(self._tmp, "yiban.db"),
        })
        os.environ.pop("YIBAN_FALLBACK_ENABLE", None)
        self.addCleanup(self._restore_runtime)

    def _restore_runtime(self):
        shutil.rmtree(self._tmp, ignore_errors=True)
        for k, v in self._old_runtime.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _child_main(self, argv=()):
        """以**窗口外**的固定业务钟跑 runner.main（子进程用例专用）。

        06:40 的窗口内钉只适合父进程用例（它在派发处被替身拦下、走不到一轮）；
        子进程会真的进一轮，窗口内钉会把它送进真登录路径，故这里用午间钉。
        """
        with mock.patch.object(runner.clock, "now", lambda: WEEKDAY_NOON):
            return runner.main(list(argv))

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_parent_with_manifest_dispatches_supervisor(self):
        """反向控制：没有子进程身份时照旧按清单派发（别把正常路径也挡了）。"""
        with mock.patch.dict(os.environ, {"YIBAN_GLOBAL_PAUSE": "0"}), \
                mock.patch.object(runner.clock, "now", lambda: WEEKDAY_06_40):
            self.assertEqual(runner.main([]), 0)
        self.assertEqual(len(self.calls), 1, "清单里有 2 个 worker 行，父进程应派发监督")
        self.assertEqual(self.calls[0][0], 2)

    def test_child_with_executor_id_never_dispatches(self):
        os.environ["YIBAN_EXECUTOR_ID"] = egress.worker_owner(0, "myhost") # 短路的判据就是这个变量在不在，而不是 argv 的形状
        self._child_main()
        self.assertEqual(self.calls, [], "子进程按清单又拉一轮 = 递归成进程树")

    def test_child_ignores_explicit_workers_flag(self):
        """子进程即使带上 `--workers N`（等号写法同样）也不得派发。"""
        os.environ["YIBAN_EXECUTOR_ID"] = egress.worker_owner(1, "myhost")
        for argv in (["--workers", "2"], ["--workers=2"]):
            with self.subTest(argv=argv):
                self.calls.clear()
                self._child_main(argv)
                self.assertEqual(self.calls, [])

    def test_fallback_identity_also_blocks_dispatch(self):
        """兜底常驻进程（`fallback@{主机}`）同样不得派发监督进程。"""
        os.environ["YIBAN_EXECUTOR_ID"] = egress.fallback_owner("myhost")
        self._child_main()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
