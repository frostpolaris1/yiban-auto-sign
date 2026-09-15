# -*- coding: utf-8 -*-
"""部署入口脚本的导入条件：只在 `scripts/` 入 sys.path 时也必须能导入。

镜像里 `docker/scheduler.py` 被复制为 `scripts/container_scheduler.py` 并由
supervisord 直接执行——此时 `sys.path[0]` 是 `scripts/`，而共享包 `yiban/` 在仓库根，
**缺包导入引导就会 ModuleNotFoundError**。2026-09-15 容器冒烟实测到该故障
（sched 进程反复退出 → FATAL），原因是新加的 `from yiban import clock` 没带引导。

本测试在等价条件下（清空 PYTHONPATH、cwd=仓库根、直接执行脚本）跑真实进程：
导入失败会立刻以非零码退出，进入主循环则超时——两种情况都能区分。
"""
import os
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DeployEntryImportTest(unittest.TestCase):
    def _run(self, rel, args=(), timeout=8):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)  # 只留脚本自身目录 → 等价于容器内的导入条件
        tmp = tempfile.mkdtemp(prefix="yiban-entry-")
        env.update({
            "YIBAN_STATE_DIR": tmp,
            "YIBAN_LOG_FILE": os.path.join(tmp, "sign.log"),
            "YIBAN_DB_FILE": os.path.join(tmp, "yiban.db"),
            "YIBAN_ENV_FILE": os.path.join(tmp, ".env"),
        })
        return subprocess.run([sys.executable, rel, *args], cwd=BASE, env=env,
                              capture_output=True, text=True, timeout=timeout)

    def test_signin_cli_imports_without_repo_root_on_path(self):
        r = self._run("scripts/signin.py", ("--help",))
        self.assertNotIn("ModuleNotFoundError", r.stderr, r.stderr[-600:])
        self.assertEqual(r.returncode, 0, r.stderr[-600:])

    def test_container_scheduler_imports(self):
        """调度器必须能导入（不真跑主循环：模块体在 __main__ 守卫里才起循环）。

        跑仓库内的源文件 `docker/scheduler.py`——镜像里它被复制为
        `scripts/container_scheduler.py`，两处都只比仓库根低一层，
        同一段引导在两种位置都成立。
        """
        code = (
            "import importlib.util, sys;"
            "spec = importlib.util.spec_from_file_location('sched_probe', sys.argv[1]);"
            "m = importlib.util.module_from_spec(spec);"
            "spec.loader.exec_module(m);"
            "print('IMPORT_OK' if callable(getattr(m, 'main_loop', None)) else 'EMPTY_MODULE')"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        r = subprocess.run([sys.executable, "-c", code, "docker/scheduler.py"],
                           cwd=BASE, env=env, capture_output=True, text=True, timeout=30)
        self.assertNotIn("ModuleNotFoundError", r.stderr, r.stderr[-800:])
        # 注意校验"装配完成"：空文件同样能 import 成功（2026-09-15 实测踩到过），
        # 那样的模块在容器里会静默不调度——比崩溃更难发现。
        self.assertIn("IMPORT_OK", r.stdout,
                      f"模块未装配出 main_loop（空文件/截断？）：{r.stdout!r} {r.stderr[-400:]}")

    def test_all_local_package_importers_are_bootstrapped(self):
        """凡导入 yiban 的运行时脚本，都必须自己保证仓库根在 sys.path 上。

        结构断言（此处无可执行的行为判据：脚本一旦跑起来就是守护进程/真实签到）。
        """
        import re
        roots = ["scripts", "docker", "web"]
        problems = []
        for d in roots:
            for name in sorted(os.listdir(os.path.join(BASE, d))):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(BASE, d, name)
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                if not re.search(r"^\s*(from yiban|import yiban)", text, re.M):
                    continue
                if "_REPO_ROOT" in text or "dirname(os.path.dirname" in text:
                    continue
                problems.append(f"{d}/{name}")
        self.assertEqual(problems, [], f"以下脚本导入 yiban 却没有包导入引导：{problems}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
