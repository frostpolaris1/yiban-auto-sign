# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""公开数据红线门的 CI 接线守卫（旧 Python 栈退役后）。

钉住两条：
  1. CI verify 作业真的调 bash scripts/dev-verify.sh --ci；
  2. run_ci 里公开数据门与它的接线守卫出现在同一条逻辑命令，且没有被
     || true / -k / --ignore / --deselect 削弱。

旧版还钉 if [ "$CI_SCOPE" ... ] 之外——scope 分支已随旧线退役删除。
现在要求门在 run_ci 的顶层命令流里（不在 if/case/for/while 内），
且 run_ci 在脚本顶层被调用，避免门被条件短路。
"""
import io
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CI_YML = os.path.join(BASE, ".github", "workflows", "ci.yml")
ENTRY_SCRIPT = os.path.join(BASE, "scripts", "dev-verify.sh")

GATE_FILE = "tests/test_public_exposure_gate.py"
WIRING_FILE = "tests/test_public_exposure_gate_wiring.py"


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


def _logical_commands(body):
    out, buf = [], ""
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


class GateWiringTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.ci = _read(CI_YML)
        cls.entry = _read(ENTRY_SCRIPT)
        cls.body = _func_body(cls.entry, "run_ci")

    def test_ci_runs_the_repo_entrypoint(self):
        verify = _ci_job_body(self.ci, "verify")
        self.assertTrue(verify, "ci.yml 抽不出 verify 作业")
        runs = [ln.strip()[4:].strip() for ln in verify.splitlines()
                if ln.strip().startswith("run:")]
        self.assertIn("bash scripts/dev-verify.sh --ci", runs)

    def test_entrypoint_invokes_run_ci_at_top_level(self):
        self.assertRegex(self.entry, r"(?m)^run_ci\b",
                         "run_ci 不在脚本顶层调用")

    def test_entrypoint_runs_gate_and_wiring_together(self):
        self.assertTrue(self.body, "抽不到 run_ci（脚本结构变了）")
        depth = 0
        for cmd in _logical_commands(self.body):
            if cmd.startswith(("if ", "if!", "case ", "for ", "while ")):
                depth += 1
            elif cmd in ("fi", "esac", "done") or cmd.startswith(("fi;", "done;")):
                depth -= 1
            if GATE_FILE in cmd:
                self.assertEqual(depth, 0, "红线门被放进条件块：%s" % cmd)
                self.assertIn(WIRING_FILE, cmd)
                for bad in ("|| true", "-k ", "--ignore", "--deselect"):
                    self.assertNotIn(bad, cmd, "红线门被削弱：%s" % cmd)
                return
        self.fail("run_ci 未跑公开数据红线门")


if __name__ == "__main__":
    unittest.main()
