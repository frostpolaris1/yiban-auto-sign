# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""假上游搬家后的守卫（仓库结构整理批 A3）。

标签：K · 登录协议与自研链路
覆盖：本批把假上游从 `tests/fake_yiban_server.py` 搬到
    `yiban/adapters/protocol_fake/`（目标架构契约 `docs/dev/architecture-target.md`
    第 3 节：`protocol_fake/` 是适配器层的假服务器条目）。本文件钉三条不变式：
    ① **旧坐标消失**：`tests/fake_yiban_server.py` 不存在——留着它就是第二份实现。
    ② **新坐标能真跑**：起真子进程 `-m yiban.adapters.protocol_fake --no-tls`，
       读到就绪文件里的端口，请求 `/__health` 得 `{"ok": true}`。
       这一条是**行为**判据，不是文本判据：搬路径最容易出的错是脚本仍指旧文件，
       而那类错在"只比字符串"的用例里看不出来。
    ③ **读者面只引新坐标**：跑演练与依赖它的 e2e 文件里，旧路径字样清零。
对应实现：`yiban/adapters/protocol_fake/`（假上游本体）、`scripts/drill_multi_exec.py`
    与 `scripts/drill-container.sh`（演练驱动）、`tests/test_login_e2e_mock.py` 等
    依赖它的 e2e（夹具装载路径）。
关键断言：① ③ 读文件与源码文本；② 起真进程 + 真 HTTP 请求（回环，不触网）。
依赖：`sys.executable`、标准库 `urllib`；不联网（只连 127.0.0.1）。无 skip。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW_DIR = "yiban/adapters/protocol_fake"
OLD_REL = os.path.join("tests", "fake_yiban_server.py")

#: 引假上游的文件（读者面）。前三个是演练驱动，后八个是依赖它的用例。
#: 后增的两条（故障旋钮门、无门面登记表）读假上游**源码**做 AST/文本解析。
#: 它们是读者，但初版名册漏了它们，所以对"还有读者指旧路径"这件事假绿。
#: 本批补齐名册：任一读者回退到旧路径，本文件必须变红。
READERS = (
    "scripts/drill_multi_exec.py",
    "scripts/drill-multi-exec.sh",
    "scripts/drill-container.sh",
    "tests/test_login_e2e_mock.py",
    "tests/test_login_protocol_shape.py",
    "tests/test_protocol_masking.py",
    "tests/test_global_pause_chain_e2e.py",
    "tests/test_multi_executor_drill_e2e.py",
    "tests/test_drill_redline_e2e.py",
    "tests/test_fake_upstream_fault_knobs_gate.py",
)


def _read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


def _read_abs(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class FakeUpstreamRelocationTest(unittest.TestCase):
    """① 坐标与产物。"""

    def test_old_path_is_gone(self):
        self.assertFalse(
            os.path.isfile(os.path.join(BASE, OLD_REL)),
            f"{OLD_REL} 仍在：假上游的第二份实现")

    def test_package_is_at_the_new_coordinate(self):
        for name in ("__init__.py", "__main__.py"):
            with self.subTest(name=name):
                self.assertTrue(os.path.isfile(os.path.join(BASE, NEW_DIR, name)),
                                f"新坐标缺 {NEW_DIR}/{name}")

    def test_scan_surface_is_alive(self):
        """防废门：新坐标下必须真有文件可扫。"""
        self.assertTrue(os.path.isdir(os.path.join(BASE, NEW_DIR)))
        self.assertGreater(os.path.getsize(os.path.join(BASE, NEW_DIR, "__init__.py")), 10000,
                           "假上游本体的体量远小于预期：搬过来的不是它")


class FakeUpstreamRunTest(unittest.TestCase):
    """② 新坐标能真跑：起进程、读端口、请求健康端点。"""

    def test_module_entry_serves_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            ready = os.path.join(tmp, "ready.txt")
            proc = subprocess.Popen(
                [sys.executable, "-m", "yiban.adapters.protocol_fake",
                 "--no-tls", "--port", "0", "--no-ipv6", "--ready-file", ready],
                cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace")
            try:
                port = self._await_port(proc, ready)
                self.assertIsNotNone(port, "假上游没在期限内写出就绪文件")
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/__health" % port, timeout=30) as resp:
                    self.assertEqual(resp.status, 200)
                    payload = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(payload, {"ok": True},
                                 "健康端点没回约定载荷：%r" % (payload,))
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=30)

    def _await_port(self, proc, ready, deadline_sec=60):
        """等就绪文件；进程先死则立刻返回 None（不空等）。"""
        end = time.time() + deadline_sec
        while time.time() < end:
            if os.path.isfile(ready):
                text = _read_abs(ready).strip()
                if text.isdigit():
                    return int(text)
            if proc.poll() is not None:
                return None
            time.sleep(0.1)
        return None

    def test_public_surface_is_importable_from_the_new_coordinate(self):
        """用例侧要的两个公共名必须能从包直接拿到。"""
        from yiban.adapters.protocol_fake import DEFAULT_PUBKEY_PEM, waf_challenge_body

        self.assertIn("PUBLIC KEY", DEFAULT_PUBKEY_PEM)
        self.assertTrue(callable(waf_challenge_body))


def _read_abs(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class ReaderCoordinateTest(unittest.TestCase):
    """③ 读者面只引新坐标。"""

    #: 旧路径的几种书写形态（读者面里都出现过）。
    OLD_SHAPES = (
        "tests/fake_yiban_server.py",
        '"fake_yiban_server.py"',
        "'fake_yiban_server.py'",
        '"tests", "fake_yiban_server.py"',
    )

    def test_readers_point_at_the_new_coordinate(self):
        for rel in READERS:
            with self.subTest(rel=rel):
                self.assertIn(NEW_DIR, _read(rel),
                              f"{rel} 没有指向新坐标 {NEW_DIR}")

    def test_no_reader_points_at_the_old_path(self):
        bad = []
        for rel in READERS:
            text = _read(rel)
            for shape in self.OLD_SHAPES:
                if shape in text:
                    bad.append(f"{rel}: {shape}")
        self.assertEqual(bad, [], "读者面仍指旧路径：" + "; ".join(bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
