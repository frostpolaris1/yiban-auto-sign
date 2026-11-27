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

_DIRECT_GATE = re.compile(r'^\s*(?:"?\$\{?py\}?"?\s+-m\s+)?pytest\b')


def _is_direct_gate_command(cmd):
    """门命令必须直接以 pytest 开头，不能被同一行的条件前缀包住。"""
    return bool(_DIRECT_GATE.match(cmd))


def _gate_placement(body):
    """返回 (命中红线门的逻辑命令, 该命令所在的条件深度)；没命中返回 (None, 0)。

    只把命令首词当控制关键字：`echo fi` 不是闭合，不能把深度洗回 0。
    """
    depth = 0
    for cmd in _logical_commands(body):
        for part in re.split(r"\s*(?:;|&&|\|\||\bthen\b|\bdo\b|\belse\b|\belif\b)\s*", cmd):
            words = part.strip().split()
            if not words:
                continue
            head = words[0]
            if head in {"if", "case", "for", "while"}:
                depth += 1
            elif head in {"fi", "esac", "done"}:
                depth = max(0, depth - 1)
        if GATE_FILE in cmd:
            return cmd, depth
    return None, 0


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
        self.assertRegex(self.entry, r"(?m)^run_ci(?!\s*\()",
                         "run_ci 不在脚本顶层调用")

    def test_entrypoint_runs_gate_and_wiring_together(self):
        self.assertTrue(self.body, "抽不到 run_ci（脚本结构变了）")
        cmd, depth = _gate_placement(self.body)
        self.assertIsNotNone(cmd, "run_ci 未跑公开数据红线门")
        self.assertEqual(depth, 0, "红线门被放进条件块：%s" % cmd)
        self.assertTrue(_is_direct_gate_command(cmd),
                        "红线门命令被前缀包住：%s" % cmd)
        self.assertIn(WIRING_FILE, cmd)
        for bad in ("|| true", "-k ", "--ignore", "--deselect"):
            self.assertNotIn(bad, cmd, "红线门被削弱：%s" % cmd)


class GatePlacementHelperTest(unittest.TestCase):
    """合成脚本体：条件块里的门必须判红，单行 if 之后的门必须判绿。"""

    def test_gate_in_else_is_conditional(self):
        body = ("if [ -f x ]; then\n  echo ok\nelse\n  pytest %s %s\nfi"
                % (GATE_FILE, WIRING_FILE))
        _cmd, depth = _gate_placement(body)
        self.assertEqual(depth, 1)

    def test_gate_in_case_is_conditional(self):
        body = ("case \"$x\" in\n  a) pytest %s %s ;;\nesac"
                % (GATE_FILE, WIRING_FILE))
        _cmd, depth = _gate_placement(body)
        self.assertEqual(depth, 1)

    def test_echo_fi_does_not_close_a_block(self):
        body = ("if [ -f x ]; then\n  echo fi\n  pytest %s %s\nfi"
                % (GATE_FILE, WIRING_FILE))
        _cmd, depth = _gate_placement(body)
        self.assertEqual(depth, 1, "echo fi 不得被当成闭合")

    def test_gate_after_one_line_if_is_top_level(self):
        body = ("if [ -f x ]; then echo ok; fi\npytest %s %s"
                % (GATE_FILE, WIRING_FILE))
        cmd, depth = _gate_placement(body)
        self.assertEqual(depth, 0)
        self.assertIn(GATE_FILE, cmd)

    def test_gate_after_one_line_if_is_still_conditional(self):
        body = ("if [ -f x ]; then echo ok; fi; pytest %s %s"
                % (GATE_FILE, WIRING_FILE))
        cmd, depth = _gate_placement(body)
        self.assertEqual(depth, 0)
        self.assertFalse(_is_direct_gate_command(cmd),
                         "单行 if 前缀不得被净额深度洗白")

    def test_direct_pytest_command_is_accepted(self):
        self.assertTrue(_is_direct_gate_command(
            '"$py" -m pytest %s %s -q' % (GATE_FILE, WIRING_FILE)))


if __name__ == "__main__":
    unittest.main()
