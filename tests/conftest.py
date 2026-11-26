# -*- coding: utf-8 -*-
"""pytest 全局配置（旧 Python 栈退役后）。

保留四类进程级纪律：
  1. root logger 文件 handler 每个用例前后关闭，避免残留句柄。
  2. 类级 os.environ 快照，setUpClass 的改动不泄漏给后续文件。
  3. 进程 umask 逐用例还原。
  4. 工作树卫生门（工单 qg8f）：会话前后检出根不得新增文件；
     实现符号与 pytest_sessionstart/finish 由 tests/test_suite_cwd_isolation_gate.py
     直接引用，不要改名或删掉。
"""
import logging
import os
import sys

import pytest


def _close_root_file_handlers():
    """关闭并移除 root logger 上全部文件 handler。"""
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            try:
                h.close()
            finally:
                root.removeHandler(h)


@pytest.fixture(autouse=True)
def _close_root_file_handlers_after_each():
    _close_root_file_handlers()
    yield
    _close_root_file_handlers()


@pytest.fixture(autouse=True, scope="class")
def _restore_environ_around_class():
    """类级 os.environ 快照：setUpClass 的改动在 tearDownClass 之后整体还原。"""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.fixture(autouse=True)
def _restore_process_umask():
    """逐用例还原进程 umask（本仓有直调生产入口的用例会改它）。"""
    before = os.umask(0o022)
    os.umask(before)
    yield
    os.umask(before)


# ---------------------------------------------------------------------------
# 工作树卫生门（工单 qg8f）：一次测试会话不得在检出根留下新增文件
# ---------------------------------------------------------------------------
_RUNTIME_ARTIFACT_IGNORED_DIRS = frozenset({
    ".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".venv", "node_modules", "work",
})
_runtime_artifact_before = None


def _runtime_artifact_paths(root):
    """检出根内的文件相对路径集合（POSIX 分隔，跨平台可读）。"""
    found = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _RUNTIME_ARTIFACT_IGNORED_DIRS]
        for name in filenames:
            found.add(os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/"))
    return found


def _runtime_artifact_diff(root, before):
    """会话新增的运行产物（排序后的相对路径列表）；before 为空表示未启用判定。"""
    if before is None:
        return []
    return sorted(_runtime_artifact_paths(root) - before)


def pytest_sessionstart(session):
    """记录会话开始时的检出根文件集（仅 controller / 串行进程）。"""
    global _runtime_artifact_before
    if getattr(session.config, "workerinput", None) is not None:
        return
    root = getattr(session.config, "rootpath", None) or session.config.rootdir
    _runtime_artifact_before = _runtime_artifact_paths(str(root))


def pytest_sessionfinish(session, exitstatus):
    """进程收尾兜底：释放文件句柄 ＋ 判检出根是否新增运行产物（工单 qg8f）。"""
    _close_root_file_handlers()
    root = getattr(session.config, "rootpath", None) or session.config.rootdir
    added = _runtime_artifact_diff(str(root), _runtime_artifact_before)
    if not added:
        return
    lines = ["", "SUITE-CWD-ISOLATION: 本次会话在检出根留下 %d 个新增文件：" % len(added)]
    lines += ["  + " + rel for rel in added[:20]]
    if len(added) > 20:
        lines.append("  …（另 %d 个）" % (len(added) - 20))
    lines.append("用例必须把运行产物写进 tmp_path / tempfile，不得落在检出根。")
    sys.stderr.write("\n".join(lines) + "\n")
    sys.stderr.flush()
    # 污染的会话不得报绿：它让"全量绿"不可复现（qg8f 的原始危害）。
    session.exitstatus = 1
