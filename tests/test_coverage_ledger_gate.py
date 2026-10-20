# -*- coding: utf-8 -*-
"""覆盖面对账门（最小版）：改动文件必须 ⊆ 覆盖并集或显式登记（工单 drqw）。

标签：J · 运维：部署/备份/发布
覆盖：`scripts/check-coverage-ledger.py` 的判定函数与命令入口，登记表
    `scripts/gate/coverage-ledger.tsv`。
关键断言：① 测试文件自身/被测试提到/登记 covered_by ⇒ 覆盖；② 无覆盖且无登记 ⇒
    退 1 并点名；③ 空改动集退 2（扫空不得当绿）；④ 登记 covered_by 指向不存在或
    未归类的测试 ⇒ 判红（登记本身也要对账）；⑤ 真树上取样：名册测试、dev-verify、
    README 各为绿，未登记探针为红。
依赖：只读仓库文本与名册；不起服务、无网络。
"""
import contextlib
import importlib.util
import io
import os
import unittest
from typing import ClassVar

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(BASE, "scripts", "check-coverage-ledger.py")


def _load_gate():
    spec = importlib.util.spec_from_file_location("check_coverage_ledger", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_main(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = mod.main(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 2
    return code, out.getvalue() + err.getvalue()


class EvaluateTest(unittest.TestCase):
    """纯函数：覆盖判定与登记对账。"""

    ROOT = BASE
    ROSTER: ClassVar[set[str]] = {"tests/test_a.py", "tests/test_gate_lane_roster.py"}
    TEXTS: ClassVar[dict[str, str]] = {
        "tests/test_a.py": "from yiban.adapters.persistence import db\n",
        "tests/test_gate_lane_roster.py": "import scripts.dev_verify_helper\n",
    }
    REG: ClassVar[list[tuple[str, str, str]]] = [
        ("docs/*", "uncovered=文档", "文档"),
        ("scripts/dev-verify.sh", "covered_by=tests/test_gate_lane_roster.py", "命令表冻结"),
        ("broken.sh", "covered_by=tests/test_missing.py", "悬空登记"),
    ]

    def _eval(self, changed):
        return _load_gate().evaluate(
            changed, self.ROSTER, self.TEXTS, self.REG, self.ROOT)

    def test_test_file_itself_is_covered(self):
        bad, covered, _reg = self._eval(["tests/test_a.py"])
        self.assertEqual(bad, [])
        self.assertEqual(len(covered), 1)

    def test_source_mentioned_by_a_test_is_covered(self):
        bad, covered, _ = self._eval(["yiban/adapters/persistence/db.py"])
        self.assertEqual(bad, [], covered)

    def test_unmentioned_source_is_red(self):
        bad, _, _ = self._eval(["yiban/nobody/looks.py"])
        self.assertEqual(len(bad), 1)
        self.assertIn("没有测试提到它", bad[0][1])

    def test_registry_uncovered_is_explicitly_registered(self):
        # 片段拼装：本文件是入库文本件，写死未入库的 doc 路径会被文档引用门判红。
        probe = "docs/" + "dev/" + "x.md"
        bad, _, reg = self._eval([probe])
        self.assertEqual(bad, [])
        self.assertEqual(len(reg), 1)

    def test_registry_covered_by_must_point_to_a_rostered_test(self):
        bad, covered, _ = self._eval(["scripts/dev-verify.sh"])
        self.assertEqual(bad, [], covered)
        bad, _, _ = self._eval(["broken.sh"])
        self.assertEqual(len(bad), 1)
        self.assertIn("不存在/未归类", bad[0][1])

    def test_removing_a_registry_row_turns_green_into_red(self):
        """突变：删掉登记行后，同一改动必须变红（门有牙）。"""
        mod = _load_gate()
        good = mod.evaluate(["scripts/dev-verify.sh"], self.ROSTER, self.TEXTS,
                            self.REG, self.ROOT)[0]
        self.assertEqual(good, [])
        without = [r for r in self.REG if r[0] != "scripts/dev-verify.sh"]
        bad = mod.evaluate(["scripts/dev-verify.sh"], self.ROSTER, self.TEXTS,
                           without, self.ROOT)[0]
        self.assertEqual(len(bad), 1)


class RealTreeTest(unittest.TestCase):
    """真树取样：绿/红两侧都要有。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_gate()

    def test_rostered_test_file_is_green(self):
        rc, out = _run_main(self.mod, ["--changed", "tests/test_gate_lane_roster.py"])
        self.assertEqual(rc, 0, out)

    def test_registered_non_test_file_is_green(self):
        rc, out = _run_main(self.mod, ["--changed", "scripts/dev-verify.sh"])
        self.assertEqual(rc, 0, out)
        rc, out = _run_main(self.mod, ["--changed", "README.md"])
        self.assertEqual(rc, 0, out)

    def test_unregistered_probe_is_red(self):
        # 片段拼装：本文件也是测试文本，写死路径会让探针被自己"提到"而假绿。
        probe = "scripts/probe_uncovered_" + "7f3a9c1e.py"
        rc, out = _run_main(self.mod, ["--changed", probe])
        self.assertEqual(rc, 1, out)
        self.assertIn("UNCOVERED", out)

    def test_empty_change_set_is_an_environment_error(self):
        rc, out = _run_main(self.mod, [])
        self.assertEqual(rc, 2, out)
        self.assertIn("空集不得当绿", out)


if __name__ == "__main__":
    unittest.main()
