# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""统一跑测入口 scripts/dev-verify.sh 的契约（旧 Python 栈退役后）。

钉住四件事：
  1. CI 的 verify 作业真的调 bash scripts/dev-verify.sh --ci；
  2. run_ci 真的跑 ruff（tests/ scripts/）；
  3. 公开数据红线门与它的接线守卫在同一条逻辑命令里；
  4. run_ci 真的跑 pytest tests/。

不冻结完整命令清单：命令原文只住 run_ci 一份，本文件只钉"哪些面必须在"。
依赖：只读文本；无网络。
"""
import io
import os
import re
import shutil
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(BASE, "scripts", "dev-verify.sh")
CI_YML = os.path.join(BASE, ".github", "workflows", "ci.yml")
GATE = "tests/test_public_exposure_gate.py"
WIRING = "tests/test_public_exposure_gate_wiring.py"


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _func_body(text, name):
    m = re.search(r"^%s\(\) \{\s*(?:#.*)?$" % re.escape(name), text, re.M)
    if m is None:
        return ""
    rest = text[m.end():]
    end = re.search(r"^\}$", rest, re.M)
    return rest[:end.start()] if end else ""


def _ci_job_body(text, name):
    m = re.search(r"^  %s:\s*$" % re.escape(name), text, re.M)
    if m is None:
        return ""
    rest = text[m.end():]
    end = re.search(r"^  [A-Za-z0-9_-]+:\s*$", rest, re.M)
    return rest[:end.start()] if end else rest


def _commands(body):
    """把函数正文拼成逻辑命令行；注释行不参与判据。"""
    out = []
    buf = ""
    for raw in body.splitlines():
        line = raw.strip()
        if not buf and (not line or line.startswith("#")):
            continue
        buf = (buf + " " + line).strip()
        if buf.endswith("\\"):
            buf = buf[:-1].rstrip()
            continue
        out.append(buf)
        buf = ""
    if buf:
        out.append(buf)
    return out


class DevVerifyEntryTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.entry = _read(ENTRY)
        cls.ci = _read(CI_YML)
        cls.body = _func_body(cls.entry, "run_ci")

    def test_ci_calls_the_entrypoint(self):
        verify = _ci_job_body(self.ci, "verify")
        self.assertTrue(verify, "ci.yml 抽不出 verify 作业")
        runs = [ln.strip()[4:].strip() for ln in verify.splitlines()
                if ln.strip().startswith("run:")]
        self.assertIn("bash scripts/dev-verify.sh --ci", runs)

    def test_no_retired_stack_inputs_in_verify(self):
        verify = _ci_job_body(self.ci, "verify")
        self.assertTrue(verify, "ci.yml 抽不出 verify 作业")
        for dead in ("requirements.lock", "requirements.txt", "scripts/ci-scope.sh",
                     "test_vue_build_reproducible.py", "--scope"):
            self.assertNotIn(dead, verify, "verify 仍引用已退役输入：%s" % dead)

    def test_run_ci_covers_ruff_and_tests(self):
        self.assertTrue(self.body, "抽不到 run_ci（脚本结构变了）")
        joined = "\n".join(_commands(self.body))
        self.assertRegex(joined, r"-m ruff check tests scripts")
        self.assertTrue(
            any(cmd.startswith('"$py" -m pytest tests/ ') and "--dist" in cmd
                for cmd in _commands(self.body)),
            "run_ci 未真跑 pytest tests/（只点名具体文件的命令不算）")
        self.assertIn(GATE, joined)
        self.assertIn(WIRING, joined)

    def test_public_gate_shares_one_command_with_wiring(self):
        """红线门与接线守卫必须同一条逻辑命令：拆开就可能只跑一边。"""
        for cmd in _commands(self.body):
            if GATE in cmd:
                self.assertIn(WIRING, cmd)
                return
        self.fail("run_ci 未跑公开数据红线门")

    def test_no_suppression(self):
        for cmd in _commands(self.body):
            for bad in ("|| true", "-k ", "--ignore", "--deselect"):
                self.assertNotIn(bad, cmd, "run_ci 削弱了门：%s" % cmd)

    @unittest.skipUnless(shutil.which("bash"), "需要 bash：--help 要真跑入口脚本")
    def test_help_exits_zero(self):
        import subprocess
        proc = subprocess.run(["bash", ENTRY, "--help"],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dev-verify", proc.stdout)


if __name__ == "__main__":
    unittest.main()
