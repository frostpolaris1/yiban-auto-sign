# -*- coding: utf-8 -*-
"""工作树卫生门的牙：套件在检出根留下运行产物 ⇒ 会话必须判红（工单 `qg8f`）。

标签：A · 判据：测试卫生

**本门挡的缺陷形状**：某个用例按默认值打开库/日志（或忘了用 tmp 目录），于是运行
产物落进**检出根**。`.gitignore` 往往恰好命中这些名字（`yiban.db` / `data/`），
`git status` 看不见 ⇒ "全量绿"不可复现，而下一轮跑测读到上一轮残留又会变成假红。
历史证据：同一内容 4 次全量 1 次红；基点 `220aac87` 跑 2 次、两次都在检出根留下
`yiban.db`（184320 B）。

**判据（不变式，不钉编舞）**：一次测试会话结束时，检出根里的文件集合不得比开始时
多。判定住在 `tests/conftest.py` 的 `pytest_sessionstart` / `pytest_sessionfinish`，
与"哪个用例污染"无关。工具缓存目录与 `work/` 显式豁免（它们不是运行产物）。

**本文件测什么**：
  ① 差集逻辑：新增 `data/yiban.db` 必被点名；会话前已存在的文件不误报；工具缓存
     目录不误报。
  ② **有牙的 e2e**：在临时"检出根"里起一个真 pytest 会话，让一条用例写
     `data/yiban.db` ⇒ 会话退出码非 0 且 stderr 点名该路径；同样形状的**干净**
     会话必须退出 0（证明门不是"永远红"）。
  ③ 在 `-n 2`（xdist）下同样判红：真实跑法是 `-n auto --dist loadfile`，只在串行
     进程里生效的门在 CI 上等于不存在。

依赖：stdlib + pytest 子进程；不触网、不读库。全部平台照跑。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFTEST = os.path.join(BASE, "tests", "conftest.py")

_POLLUTING = '''\
import os


def test_writes_default_db_into_repo_root():
    os.makedirs("data", exist_ok=True)
    with open(os.path.join("data", "yiban.db"), "wb"):
        pass
    assert True
'''

_CLEAN = '''\
import os


def test_writes_nothing_into_repo_root():
    os.makedirs("__pycache__", exist_ok=True)
    with open(os.path.join("__pycache__", "ignored.pyc"), "wb"):
        pass
    assert True
'''


def _load_conftest():
    """按路径装载 conftest 模块，直接测差集逻辑（不另抄一份判据）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_conftest_under_test", CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ArtifactDiffLogicTest(unittest.TestCase):
    """差集逻辑：点名新增、放过既有、放过工具缓存。"""

    @classmethod
    def setUpClass(cls):
        cls.conftest = _load_conftest()

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="yiban-cwd-gate-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, rel, data=b""):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def test_detects_default_db_and_preexisting_is_not_flagged(self):
        self._write("keep.txt", b"old")
        before = self.conftest._runtime_artifact_paths(self.root)
        self.assertEqual(self.conftest._runtime_artifact_diff(self.root, before), [])
        self._write("data/yiban.db", b"SQLite format 3\x00")
        self.assertEqual(
            self.conftest._runtime_artifact_diff(self.root, before), ["data/yiban.db"],
            "新增的默认库必须被点名（它被 .gitignore 命中，git status 看不见）")

    def test_tool_caches_are_explicitly_exempt(self):
        before = self.conftest._runtime_artifact_paths(self.root)
        for rel in ("__pycache__/x.pyc", ".pytest_cache/v/cache/nodeids",
                    ".ruff_cache/x", ".mypy_cache/y", "node_modules/a/b.js",
                    "work/notes.md"):
            self._write(rel)
        self.assertEqual(self.conftest._runtime_artifact_diff(self.root, before), [],
                         "工具缓存与 work/ 是显式豁免面，不得当作运行产物")

    def test_disabled_check_is_silent(self):
        self.assertEqual(self.conftest._runtime_artifact_diff(self.root, None), [],
                         "未启用（非 controller）时不得判红")


class SessionGateE2ETest(unittest.TestCase):
    """真 pytest 会话：污染 ⇒ 红；干净 ⇒ 绿；xdist 下同样红。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="yiban-cwd-e2e-")
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copy(CONFTEST, os.path.join(self.root, "conftest.py"))

    def _run(self, source, *args):
        path = os.path.join(self.root, "test_probe.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(source)
        env = dict(os.environ)
        # 子会话不继承外部测试的 pytest 选项/插件开关；只保留解释器与 PYTHONPATH。
        for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_XDIST_WORKER"):
            env.pop(key, None)
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             *args, "test_probe.py"],
            cwd=self.root, env=env, capture_output=True, text=True, timeout=180)

    def test_polluting_session_is_red_and_names_the_path(self):
        proc = self._run(_POLLUTING)
        self.assertNotEqual(proc.returncode, 0,
                            "检出根新增了 data/yiban.db，会话不得报绿：\n%s\n%s"
                            % (proc.stdout, proc.stderr))
        self.assertIn("SUITE-CWD-ISOLATION", proc.stderr)
        self.assertIn("data/yiban.db", proc.stderr)

    def test_clean_session_is_green(self):
        proc = self._run(_CLEAN)
        self.assertEqual(proc.returncode, 0,
                         "干净会话不得假红（工具缓存目录是显式豁免面）：\n%s\n%s"
                         % (proc.stdout, proc.stderr))
        self.assertNotIn("SUITE-CWD-ISOLATION", proc.stderr)

    def test_pollution_is_caught_under_xdist_controller(self):
        """`-n 2` 下由 controller 判定：worker 跳过判定，但污染仍必须红。"""
        proc = self._run(_POLLUTING, "-n", "2", "--dist", "loadfile")
        self.assertNotEqual(proc.returncode, 0,
                            "xdist 跑法下污染同样不得报绿：\n%s\n%s"
                            % (proc.stdout, proc.stderr))
        self.assertIn("data/yiban.db", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
