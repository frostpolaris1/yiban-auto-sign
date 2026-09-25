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
   真进程）；不发网络请求、不建库。整文件在本机执行，无 skip。

用法（项目根目录）：
    py -m pytest tests/test_supervisor_recursion_guard.py -v
"""
import os
import sys
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
        runner.main([])
        self.assertEqual(self.calls, [], "子进程按清单又拉一轮 = 递归成进程树")

    def test_child_ignores_explicit_workers_flag(self):
        """子进程即使带上 `--workers N`（等号写法同样）也不得派发。"""
        os.environ["YIBAN_EXECUTOR_ID"] = egress.worker_owner(1, "myhost")
        for argv in (["--workers", "2"], ["--workers=2"]):
            with self.subTest(argv=argv):
                self.calls.clear()
                runner.main(argv)
                self.assertEqual(self.calls, [])

    def test_fallback_identity_also_blocks_dispatch(self):
        """兜底常驻进程（`fallback@{主机}`）同样不得派发监督进程。"""
        os.environ["YIBAN_EXECUTOR_ID"] = egress.fallback_owner("myhost")
        runner.main([])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
