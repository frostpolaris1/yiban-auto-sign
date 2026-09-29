# -*- coding: utf-8 -*-
"""`.env` 原子写的瞬态失败重试（2026-09-17，Windows 上实测复现的 500）。

**缺陷与复现**：前端复审"清单上限"连续追加行时报 `web/app.py` 的原子写偶发
`WinError 5 拒绝访问` → 500，预览目录留下若干 `.env.tmp*` 残留。本机复现脚本
（一边持续读 `.env`、一边连续原子写 400 次）复现率 **400/400 失败**、残留 256 个
tmp 文件。机制：Windows 的 `open()` 不带 `FILE_SHARE_DELETE`，**读者正打开着目标文件
时 `os.replace` 会被拒**；而 `.env` 是高频读取的文件（同进程其他线程的 `read_env`、
引擎子进程、预览工具）。Linux 的 `rename` 不受读者影响，故生产形态不涉及。

本文件钉三件事：
1. 瞬态失败**重试后成功**（内容正确、无 tmp 残留）；
2. 一直失败 → 原样抛出，**且必须清掉 tmp**（那是一份完整 `.env` 副本，含密钥与口令哈希）；
3. 真实并发（读线程 + 连续写）下不再失败——这条是本缺陷的正向回归。

用法（项目根目录）：
    py -m pytest tests/test_env_write_transient_failure.py -v

标签：G · 安全：脱敏/审计/配置注入
覆盖：`.env` 原子写的瞬态失败面——重试后成功、一直失败时原样抛出且必须清掉 tmp、
尝试预算有限（不得无限重试）、真并发（读线程 + 连续写）不再失败。
对应实现：`yiban/infra/env_io.py` 的原子写（`write_env_keys` 的 tmp + `os.replace` 路径）
与其重试包装。
关键断言：失败分支必须**成对**断"抛出"与"tmp 已清"——留下的是含密钥与管理员口令哈希的
完整 `.env` 副本，只断抛出等于把泄露留在原地；`test_attempt_budget_is_finite` 守的是
"重试不许变成死循环"，与上一条方向相反，两条都在才算闭环。
依赖：重试与预算两条在任意平台跑（用注入的失败模拟）；`ConcurrentReadWriteTest` 那条
真并发只在 Windows 上构成负例（Linux `rename` 不受读者影响，模块头已写明），
非 Windows 上它只会通过、不会失败。子进程/线程用 `time_mod` 注入控制节奏。
"""
import contextlib
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))


def _load_webapp():
    """**独立名字**加载 web/app.py（它导入期就把 ENV_FILE 等读成模块级常量，
    与别的测试文件共用同一模块对象会读到另一个 .env）。"""
    spec = importlib.util.spec_from_file_location(
        "webapp_envwrite", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_envwrite"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class ReplaceRetryTest(unittest.TestCase):
    """重试预算与失败清理（打桩 `os.replace`，跨平台可跑）。"""

    @classmethod
    def setUpClass(cls):
        cls.webapp = _load_webapp()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-envwrite-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # 退避重试不必真等（预算 0.05+0.1+0.2+0.4+0.8s）
        p = mock.patch.object(self.webapp.time, "sleep", lambda _s: None)
        p.start()
        self.addCleanup(p.stop)

    def _tmp_leftovers(self, path):
        return [n for n in os.listdir(os.path.dirname(path))
                if n.startswith(os.path.basename(path) + ".tmp")]

    def test_transient_permission_error_is_retried_then_succeeds(self):
        """前两次 WinError 5、第三次成功：写入成功、内容正确、无残留。"""
        target = os.path.join(self.tmp, ".env")
        real = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(1)
            if len(calls) <= 2:
                raise PermissionError(13, "拒绝访问。", dst)
            return real(src, dst)

        with mock.patch.object(self.webapp.os, "replace", side_effect=flaky):
            self.webapp._atomic_write(target, "YIBAN_WORKERS=3\n", chmod_priv=True)
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "YIBAN_WORKERS=3\n")
        self.assertEqual(len(calls), 3, "应按退避重试到成功")
        self.assertEqual(self._tmp_leftovers(target), [], "成功后不得残留 tmp")

    def test_persistent_failure_raises_and_removes_tmp(self):
        """一直失败：原样抛出，但**不许**把整份 .env 副本留在磁盘上。"""
        target = os.path.join(self.tmp, ".env")
        with open(target, "w", encoding="utf-8") as f:
            f.write("旧内容\n")

        def always(src, dst):
            raise PermissionError(13, "拒绝访问。", dst)

        with mock.patch.object(self.webapp.os, "replace", side_effect=always), \
                self.assertRaises(PermissionError):
            self.webapp._atomic_write(target, "新内容\n", chmod_priv=True)
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "旧内容\n", "失败时原文件必须保持原样")
        self.assertEqual(self._tmp_leftovers(target), [],
                         "失败路径必须删掉 tmp（那份副本含密钥/口令哈希）")

    def test_attempt_budget_is_finite(self):
        """重试次数有上界（不能变成无限等锁）。"""
        target = os.path.join(self.tmp, ".env")
        calls = []

        def always(src, dst):
            calls.append(1)
            raise PermissionError(13, "拒绝访问。", dst)

        with mock.patch.object(self.webapp.os, "replace", side_effect=always), \
                self.assertRaises(PermissionError):
            self.webapp._atomic_write(target, "x\n")
        self.assertEqual(len(calls), self.webapp._REPLACE_RETRY_ATTEMPTS)


class ConcurrentReadWriteTest(unittest.TestCase):
    """真实并发：一个线程持续读 `.env`（带间隔，模拟逐请求读取），主线程连续原子写。

    修前实测：并发读下 400 次写入**全部**失败（WinError 5）且残留 256 个 tmp；
    修后应 0 失败、0 残留。Windows 上这条最有用（Linux 的 rename 本就不受影响，
    但跑一遍也不亏：它同时验证了写入路径在并发读下的正确性）。

    **已知边界（有意不掩盖）**：读者若把 `.env` **长期打开着**（100% 占用，例如某些
    编辑器/杀软/同步盘），重试预算内找不到空档，写入仍会失败——但会**清掉 tmp 并抛出**
    （不会半写、不会留凭据副本）。生产里的读都是"逐请求 open→read→close"的微秒级占用，
    下面的读线程即按此模拟（每次读之间留 1ms）。
    """

    @classmethod
    def setUpClass(cls):
        cls.webapp = _load_webapp()

    def test_no_failure_while_env_is_being_read(self):
        import time as _time
        tmp = tempfile.mkdtemp(prefix="yiban-envwrite-race-")
        self.addCleanup(shutil.rmtree, tmp, True)
        target = os.path.join(tmp, ".env")
        with open(target, "w", encoding="utf-8") as f:
            f.write("YIBAN_WORKERS=1\n")

        stop = threading.Event()
        reads = [0]
        reader = threading.Thread(target=self._reader, args=(target, stop, reads, _time),
                                  daemon=True)
        reader.start()
        try:
            for i in range(60):
                try:
                    self.webapp._atomic_write(target, f"YIBAN_WORKERS={i % 5 + 1}\n",
                                              chmod_priv=True)
                except PermissionError as e:
                    self.fail(f"第 {i + 1} 次写入在并发读下失败（读者占用应远小于重试预算）: {e}")
        finally:
            stop.set()
            reader.join(timeout=3)
        self.assertGreater(reads[0], 0, "读线程没跑起来，这条用例就没意义")
        leftovers = [n for n in os.listdir(tmp) if n.startswith(".env.tmp")]
        self.assertEqual(leftovers, [], f"并发写留下了 tmp 残留: {leftovers}")

    @staticmethod
    def _reader(target, stop, counter, time_mod):
        from yiban.infra import env_io
        while not stop.is_set():
            with contextlib.suppress(Exception):
                env_io.parse_env_file(target)
            counter[0] += 1
            time_mod.sleep(0.001)   # 模拟逐请求读取：占用是微秒级，不是常驻句柄


if __name__ == "__main__":
    unittest.main(verbosity=2)
