# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`.env` 写锁判据：锁在 `write_env_keys` 内部取得，且不存在不持锁的写入方。

背景：`write_env_keys` 曾**不加锁**，把"调用方须自持 `env_lock.env_write_lock`"的
责任写在 docstring 里。逐个核对全部写入点，只要有一个漏持锁，并发读-改-写的后
落盘者就会把对方刚写入的行**整行抹掉**（它的 out 里根本没有对方刚 append 的那行）。

本文件钉三格：
1. **行为格**：`write_env_keys` 不靠外层也自持写锁（锁钩子被调用 + 锁文件真实创建）；
   外层已持同一路径锁时同线程嵌套直接放行（`locks.file_lock` 可重入语义，不是死锁）；
   另一线程持锁时内层锁**挡住**第二个写入方直到释放（真互斥，非仅 RLock 摆设）。
2. **grep 格**：AST 扫运行时目录（`web/ yiban/ scripts/ docker/`）——任何对 env 形状
   路径（名字含 `env`/`ENV` 或字面量 `.env`）的写模式 open / os.open(WRONLY) /
   os.replace|rename 目标，其所在函数必须引用 `write_env_key`/`env_write_lock`；
   唯一豁免是 `env_io` 的两个底层原子写助手，它们只能被 `write_env_keys` 引用一次。
3. **登记格**：原不持锁写入点（loadtest 建 .env 头文件的裸 `open(path,"w")` 截断）
   现与读-改-写同处一把锁的临界区，且行为不变（建新文件仍带头注释）。

标签：G · 安全：脱敏/审计/配置注入
覆盖：write_env_keys 内部取锁（钩子计数、锁文件创建）、同线程嵌套放行、跨线程互斥
    等待、运行时目录 grep 级"无不持锁 .env 写入方"、`_atomic_replace_env`/
    `_restore_env_bytes` 仅 write_env_keys 可达、seed_accounts 建头并入锁内。
对应实现：`yiban/infra/env_io.py` 的 `write_env_keys`（内持 `env_lock.env_write_lock`）
    与 `_atomic_replace_env`/`_restore_env_bytes`；`yiban/infra/env_lock.py`、
    `yiban/infra/locks.py`（可重入与互斥语义）；`web/services/env_io.py` 的
    `write_env_batch`（外层锁去重）；`yiban/engine/probe.py` 的 `_env_update_probe`
    （自写读-改-写并入单一写入口）；`scripts/loadtest/seed_accounts.py` 的
    `upsert_env`/`_ensure_env_headed`（建文件头入锁）。
关键断言：grep 断言必须**两向钉**——列出全部违例（新写点漏锁即红），同时钉"底层
    助手引用恰好 1 处且在 write_env_keys 内"（豁免面失守即红）；只断"锁函数被调用过"
    不够，还要断**持锁期间另一线程进不来**（互斥真实生效）。
依赖：临时 .env + 线程；无网络、无 skip；跨线程用例用 0.3s 存活窗判"被挡"，随后
    10s 内 join 兜底，慢机上不会误红。
"""
import ast
import contextlib
import importlib
import io
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 包导入引导先于任何 yiban 导入（test_deploy_entry_imports 按**出现位置**钉顺序，
# 写在 setUpClass 里的后补引导过不了这条守卫）：scripts/ 入 path 供 loadtest 命名空间包
if os.path.join(BASE, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(BASE, "scripts"))

from yiban.infra import env_io, env_lock  # noqa: E402

#: 与被豁免实现同口径的运行时目录（测试代码不列：那里的写法不构成生产事实）
RUNTIME_DIRS = ("web", "yiban", "scripts", "docker")

#: 唯一豁免：`env_io` 的原子落盘助手，只能由**已持锁**的 `write_env_keys` 调用
#: （"只能被 write_env_keys 可达"由引用计数断言单独钉死）
SANCTIONED_WRITERS = {
    ("yiban/infra/env_io.py", "_atomic_replace_env"),
    ("yiban/infra/env_io.py", "_restore_env_bytes"),
}

#: 函数源码里出现任一标记即视为"该函数已把锁责任交给/落在 write_env_keys 一侧"
LOCK_MARKERS = ("write_env_key", "env_write_lock")

_WRITE_MODE_OPEN_FUNCS = ("open", "io.open")           # 内置 open / 显式 io.open
_REPLACE_FUNCS = ("os.replace", "os.rename", "os.renames")


def _dotted(func_node):
    """调用目标点号名（`os.open` / `open` / `builtins.open`），取不到返回 None。"""
    if isinstance(func_node, ast.Name):
        return func_node.id
    if isinstance(func_node, ast.Attribute):
        parts = []
        node = func_node
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return ".".join(reversed(parts))
    return None


def _expr_is_envish(src):
    """路径表达式是否 env 形状：标识符/属性名含 `env`/`ENV`，或字面量含 `.env`。"""
    low = src.lower()
    return "env" in low


def _write_mode(src):
    """open() 第二参/`mode=` 是否为写模式（w/a/x/+ 皆算）。"""
    mode = src.strip()
    if not (mode.startswith(("'", '"')) and mode.endswith(("'", '"'))):
        return False          # 动态模式串：宁可漏判也不炸测试——实仓无此形态
    m = mode.strip("'\"b")
    return bool(m) and (m[0] in "wax+" or "+" in m)


def _scan_file(path):
    """→ 该文件所有『对 env 形状目标的写操作』违例 [(函数名或 None, lineno, 文本)]。"""
    with io.open(path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename=path)
    hits = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body_src = ast.get_source_segment(src, fn) or ""
        if any(marker in body_src for marker in LOCK_MARKERS):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            dotted = _dotted(node.func)
            if not dotted:
                continue
            target_src = None
            if dotted in _WRITE_MODE_OPEN_FUNCS and node.args:
                mode_src = ""
                if len(node.args) > 1:
                    mode_src = ast.get_source_segment(src, node.args[1]) or ""
                for kw in node.keywords:
                    if kw.arg == "mode":
                        mode_src = ast.get_source_segment(src, kw.value) or ""
                if _write_mode(mode_src):
                    target_src = ast.get_source_segment(src, node.args[0])
            elif dotted == "os.open" and node.args:
                flags_src = ast.get_source_segment(src, node.args[1]) if len(node.args) > 1 else ""
                if any(t in (flags_src or "") for t in ("O_WRONLY", "O_RDWR", "O_APPEND")):
                    target_src = ast.get_source_segment(src, node.args[0])
            elif dotted in _REPLACE_FUNCS and len(node.args) >= 2:
                target_src = ast.get_source_segment(src, node.args[1])
            if target_src and _expr_is_envish(target_src):
                hits.append((fn.name, node.lineno,
                             ast.get_source_segment(src, node) or ""))
    return hits


class NoUnlockedEnvWriterTest(unittest.TestCase):
    """grep 级断言：运行时目录不存在不持锁（也非经 write_env_keys）的 .env 写入方。"""

    def _iter_runtime_py(self):
        for root in RUNTIME_DIRS:
            base = os.path.join(BASE, root)
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for name in sorted(filenames):
                    if name.endswith(".py"):
                        yield os.path.join(dirpath, name)

    def test_every_env_shaped_write_is_under_lock(self):
        bad = []
        for path in self._iter_runtime_py():
            rel = os.path.relpath(path, BASE).replace(os.sep, "/")
            for fn_name, lineno, text in _scan_file(path):
                if (rel, fn_name) in SANCTIONED_WRITERS:
                    continue
                bad.append(f"{rel}:{lineno} in {fn_name}(): {text.strip()[:80]}")
        self.assertEqual(
            bad, [],
            "不持锁的 .env 写入方（所在函数既不调 write_env_key* 也不取 env_write_lock）："
            + "; ".join(bad))

    def test_low_level_rewriters_only_reachable_from_write_env_keys(self):
        """豁免面不失控：两个原子写助手在 env_io 内各**只剩一处**引用，且在
        `write_env_keys` 函数体里（引用计数漂移 = 出现第二个调用方 = 锁被旁路）。"""
        with io.open(os.path.join(BASE, "yiban", "infra", "env_io.py"),
                     encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        holder = {}
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Name) and node.id in (
                        "_atomic_replace_env", "_restore_env_bytes"):
                    holder.setdefault(node.id, []).append(fn.name)
        for name in ("_atomic_replace_env", "_restore_env_bytes"):
            with self.subTest(helper=name):
                self.assertEqual(holder.get(name), ["write_env_keys"],
                                 f"{name} 的引用方不是唯一的 write_env_keys（内持写锁）")

    def test_write_env_keys_itself_holds_the_lock(self):
        """源码判据：锁在函数体内取（`with env_lock.env_write_lock(env_file)` 包住读-改-写）。"""
        import inspect
        src = inspect.getsource(env_io.write_env_keys)
        self.assertIn("env_lock.env_write_lock(env_file)", src)
        self.assertLess(src.index("env_write_lock(env_file)"), src.index("_read_env_text"),
                        "读原文必须在取锁之后——锁外读、锁内写等于没锁")


class WriteEnvKeysInternalLockBehaviorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-envlock-")
        self.env = os.path.join(self.tmp, "app.env")
        with io.open(self.env, "w", encoding="utf-8") as f:
            f.write("YIBAN_KEEP=1\n")

    def tearDown(self):
        for name in os.listdir(self.tmp):
            os.unlink(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)

    def test_lock_acquired_without_any_outer_caller(self):
        """外层什么都不持：write_env_keys 仍必须自己走 env_write_lock（验收不变量）。"""
        calls = []
        real = env_lock.env_write_lock

        @contextlib.contextmanager
        def spy(p):
            calls.append(os.path.abspath(p))
            with real(p):
                yield

        with mock.patch.object(env_io, "env_lock",
                               types.SimpleNamespace(env_write_lock=spy)):
            env_io.write_env_keys(self.env, {"YIBAN_NEW": "v"})
        self.assertEqual(calls, [os.path.abspath(self.env)])
        self.assertIn("YIBAN_NEW=v", io.open(self.env, encoding="utf-8").read())

    def test_real_lock_file_created(self):
        """不加桩的真调用必须创建 `<env>.lock` 锁文件（真锁原语在场，不是纸面互斥）。"""
        env_io.write_env_keys(self.env, {"YIBAN_NEW": "v"})
        self.assertTrue(os.path.exists(os.path.abspath(self.env) + ".lock"))

    def test_same_thread_outer_lock_nests_fine(self):
        """外层已持同一路径锁：内层直接放行（可重入），不得死锁。"""
        with env_lock.env_write_lock(self.env):
            env_io.write_env_keys(self.env, {"YIBAN_IN": "1"})
        self.assertIn("YIBAN_IN=1", io.open(self.env, encoding="utf-8").read())

    def test_other_thread_blocks_until_outer_releases(self):
        """另一线程持锁期间，写方被**挡在门外**；释放后才落盘——内层锁是真互斥。"""
        done = threading.Event()
        started = threading.Event()

        def _worker():
            started.set()
            env_io.write_env_keys(self.env, {"YIBAN_LATE": "1"})
            done.set()

        with env_lock.env_write_lock(self.env):
            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            started.wait(2)
            time.sleep(0.3)
            self.assertFalse(done.is_set(), "外层持锁期间写入方不得完成（互斥未生效？）")
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertTrue(done.is_set())
        self.assertIn("YIBAN_LATE=1", io.open(self.env, encoding="utf-8").read())


class SeedAccountsHeadUnderLockTest(unittest.TestCase):
    """原"不持锁写入点"（loadtest 裸 open(w) 截断建头）现在锁内，行为不变。"""

    @classmethod
    def setUpClass(cls):
        cls.seed = importlib.import_module("loadtest.seed_accounts")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-seed-head-")

    def tearDown(self):
        for name in os.listdir(self.tmp):
            p = os.path.join(self.tmp, name)
            os.unlink(p) if os.path.isfile(p) else None
        os.rmdir(self.tmp)

    def test_upsert_creates_headered_file_and_writes_keys(self):
        path = os.path.join(self.tmp, "fresh.env")
        self.seed.upsert_env(path, {"YIBAN_SIGN_ORDER": "random"})
        with io.open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("#"), "新 .env 必须带头注释（与旧行为一致）")
        self.assertIn("YIBAN_SIGN_ORDER=random", text)

    def test_upsert_head_creation_holds_lock(self):
        """建头路径经函数级判据：`_ensure_env_headed` 只能在持锁临界区内被调用——
        调用它的 `upsert_env` 与 `main` 都在 `env_write_lock` 里，由 grep 格整体钉住。"""
        import inspect
        src = inspect.getsource(self.seed.upsert_env)
        self.assertIn("env_write_lock", src)
        self.assertIn("_ensure_env_headed", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
