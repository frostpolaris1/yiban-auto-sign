# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""命名门：`docs/dev/target-tree.md` §6.1 的命名规则 + §6.2 基线只缩不涨（B1 收口）。

标签：M · 架构分层与基础设施
覆盖（判据全部落在跟踪集上）：
    ① 目录名：Python 包目录 `snake_case`；非代码目录 `kebab-case`（允许 `.`，隐藏目录除外）。
    ② 模块名：`snake_case`；可执行程序允许连字符，但只在 `scripts/` 顶层。
    ③ 测试名：`tests/**` 的 `.py` 只许 `test_*.py`、`_*.py`、`conftest.py`。
    ④ 禁字面量：版本号、工单号、日期、批次号不得进文件名（`v3`、`mf60`、`20261025` 等）。
    ⑤ 可执行程序：`scripts/*.sh` 用连字符；`scripts/lib/*.sh` 是库，按下划线。
    ⑥ 包名与模块名不得重复。
    ⑦ `yiban/` 包根只许 `__init__.py`。
    —— 违反以上任一条且不在 `scripts/gate/naming-baseline.tsv` 里 ⇒ 红。
       基线里的行若已不再违规、或文件已删 ⇒ 也红（基线只许缩小）。

**为什么基线是数据而不是散文**：§6.2 的已知例外如果只写在文档里，下一个人新增
`test_x_v4.py` 时没有任何东西会红。把例外冻结成一行数据，新增即红，删掉旧例时门
会催人同批删行——这就是「只缩不涨」的机械形态（AGENTS §十三 第 27 条）。

**扫空不得当绿**：面取自 `git ls-files`；少于 700 个文件判红（ref＝`origin/develop`
`aa75e2b9` 实测 966）。新文件须先 `git add` 才进面（与既有跟踪集门同口径）。

依赖：只读跟踪集路径 + 基线文件；不导入业务包、不联网。
"""
import io
import os
import re
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(BASE, "scripts", "gate", "naming-baseline.tsv")

RULE_IDS = (
    "pkg-name",
    "module-name",
    "test-name",
    "forbidden-token",
    "sh-underscore",
    "yiban-root-loose",
    "pkg-module-duplicate",
    "dir-name",
)

#: 版本号 / 工单号 / 日期 / 批次号进文件名的判据。
#: `v3` / `mf60` / `m9bi` / `p05` / `b1` / `20261025` 都在面内；`e2e` 不在（它是用例类型）。
FORBIDDEN_TOKEN = re.compile(
    r"(?:^|_)(?:v\d+(?:\.\d+)*|m\d+[a-z0-9]*|mf\d+[a-z0-9]*|p\d+[a-z0-9]*"
    r"|b\d+|(?:19|20)\d{6})(?:_|\.|$)")

SNAKE = re.compile(r"_?[a-z][a-z0-9_]*\.py$")
HYPHEN_PROG = re.compile(r"[a-z][a-z0-9-]*\.py$")
SNAKE_DIR = re.compile(r"[a-z][a-z0-9_]*$")
KEBAB_DIR = re.compile(r"[a-z0-9][a-z0-9.-]*$")

#: 扫描面下限（AGENTS §30）。
MIN_FILES = 700


def forbidden_token(name):
    """文件名是否含被禁 token（版本/工单/日期/批次）。"""
    return bool(FORBIDDEN_TOKEN.search(name))


def module_name_problem(rel, name):
    """模块名规则：返回规则 id 或 None。"""
    if name in ("__init__.py", "__main__.py"):
        return None
    if SNAKE.match(name):
        return None
    top_script = rel.startswith("scripts/") and rel.count("/") == 1
    if top_script and HYPHEN_PROG.fullmatch(name):
        return None
    return "module-name"


def check_test_name(rel, name):
    """测试名规则：返回规则 id 或 None。"""
    if not rel.startswith("tests/"):
        return None
    if name == "conftest.py":
        return None
    if re.fullmatch(r"(?:test_[a-z0-9_]+|_[a-z0-9_]+)\.py", name):
        return None
    return "test-name"


def dir_name_problem(name, is_package):
    """目录名规则：返回规则 id 或 None。"""
    if name.startswith("."):
        return None
    if is_package:
        return None if SNAKE_DIR.fullmatch(name) else "pkg-name"
    return None if KEBAB_DIR.fullmatch(name) else "dir-name"


def violations(paths):
    """跟踪集里的全部命名违规：`[(path, rule)]`（升序去重）。

    `paths` 是相对仓库根的 posix 路径（`git ls-files` 口径）。
    """
    files = set(paths)
    dirs = {os.path.dirname(p) for p in files if os.path.dirname(p)}
    out = set()
    for rel in files:
        name = os.path.basename(rel)
        problem = module_name_problem(rel, name) if rel.endswith(".py") else None
        if problem:
            out.add((rel, problem))
        if rel.endswith(".py"):
            problem = check_test_name(rel, name)
            if problem:
                out.add((rel, problem))
            if forbidden_token(name):
                out.add((rel, "forbidden-token"))
            if rel.startswith("yiban/") and rel.count("/") == 1 \
                    and name != "__init__.py":
                out.add((rel, "yiban-root-loose"))
        if rel.startswith("scripts/") and rel.count("/") == 1 \
                and rel.endswith(".sh") and "_" in name:
            out.add((rel, "sh-underscore"))
    for d in dirs:
        is_pkg = (d + "/__init__.py") in files
        problem = dir_name_problem(os.path.basename(d), is_pkg)
        if problem:
            out.add((d, problem))
        if is_pkg:
            for p in files:
                if os.path.dirname(p) == d and p.endswith(".py") \
                        and os.path.basename(p)[:-3] == os.path.basename(d):
                    out.add((p, "pkg-module-duplicate"))
    return sorted(out)


def _tracked(root):
    """`git ls-files` 跟踪集；非 git 树返回 None。"""
    try:
        proc = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                              capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return sorted(p for p in proc.stdout.decode("utf-8", "replace").split(chr(0)) if p)


def load_baseline(path=BASELINE):
    """读数基线：`[(path, rule)]`。坏行直接抛 ValueError（不许静默跳过）。"""
    rows = []
    with io.open(path, encoding="utf-8") as f:
        for i, raw in enumerate(f, 1):
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) < 3:
                raise ValueError("基线第 %d 行少于 3 列：%r" % (i, line))
            rel, rule = cols[0].strip(), cols[1].strip()
            if rule not in RULE_IDS:
                raise ValueError("基线第 %d 行的规则未知：%r" % (i, rule))
            rows.append((rel, rule))
    return rows


class NamingGateTest(unittest.TestCase):
    """真实跟踪集：违规 ⊆ 基线，且基线逐行仍然有效。"""

    @classmethod
    def setUpClass(cls):
        cls.tracked = _tracked(BASE)
        cls.baseline = load_baseline()

    def test_face_is_not_empty(self):
        self.assertIsNotNone(self.tracked, "读不到 git 跟踪集：本门无法判定（不是绿）")
        self.assertGreaterEqual(len(self.tracked), MIN_FILES,
                                "扫描面只有 %d 个文件（下限 %d）：扫描口径坏了"
                                % (len(self.tracked), MIN_FILES))
        self.assertTrue(self.baseline, "命名基线为空：已知例外没有登记处")

    def test_no_violation_outside_baseline(self):
        known = set(self.baseline)
        fresh = [v for v in violations(self.tracked) if v not in known]
        self.assertEqual(
            fresh, [],
            "出现未登记的命名违规（新增即红；确需保留请改基线并写理由）：\n"
            + "\n".join("%s\t%s" % v for v in fresh))

    def test_baseline_entries_stay_live(self):
        """基线只许缩小：每行必须仍真的违规，且文件仍在。"""
        current = set(violations(self.tracked))
        stale = []
        for rel, rule in self.baseline:
            if not os.path.exists(os.path.join(BASE, rel)):
                stale.append("%s\t%s：文件已不存在，删掉该行" % (rel, rule))
            elif (rel, rule) not in current:
                stale.append("%s\t%s：已不再违规，删掉该行" % (rel, rule))
        self.assertEqual(stale, [], "命名基线出现死行：\n" + "\n".join(stale))

    def test_baseline_has_no_duplicates(self):
        rows = load_baseline()
        self.assertEqual(len(rows), len(set(rows)),
                         "基线有重复行：同一 (path, rule) 登记了两次")

    def test_gate_and_baseline_are_in_the_face(self):
        rel_self = os.path.relpath(os.path.abspath(__file__), BASE).replace(os.sep, "/")
        rel_base = os.path.relpath(BASELINE, BASE).replace(os.sep, "/")
        for rel in (rel_self, rel_base):
            self.assertIn(rel, self.tracked,
                          "%s 不在跟踪集：门或基线会被扫描面漏掉" % rel)


class RuleTeethTest(unittest.TestCase):
    """判据自身有牙：每一条规则都能对合成输入开火，干净样本不得误红。"""

    def test_forbidden_token_teeth(self):
        for name in ("test_x_v4.py", "test_x_mf61.py", "test_20261026.py",
                     "test_x_b2.py", "test_x_p06.py"):
            with self.subTest(name=name):
                self.assertTrue(forbidden_token(name), "禁字面量判据无牙：%s" % name)
        for name in ("test_x_e2e.py", "test_window_edge.py", "executor.py"):
            with self.subTest(name=name):
                self.assertFalse(forbidden_token(name), "干净文件名被误红：%s" % name)

    def test_module_name_teeth(self):
        self.assertEqual(module_name_problem("scripts/check-x.py", "check-x.py"), None)
        self.assertEqual(module_name_problem("tools/check-x.py", "check-x.py"), "module-name")
        self.assertEqual(module_name_problem("scripts/x.py", "X.py"), "module-name")
        self.assertEqual(module_name_problem("yiban/__init__.py", "__init__.py"), None)

    def test_test_name_teeth(self):
        self.assertEqual(check_test_name("tests/test_a.py", "test_a.py"), None)
        self.assertEqual(check_test_name("tests/_helper.py", "_helper.py"), None)
        self.assertEqual(check_test_name("tests/conftest.py", "conftest.py"), None)
        self.assertEqual(check_test_name("tests/helper.py", "helper.py"), "test-name")

    def test_dir_name_teeth(self):
        self.assertEqual(dir_name_problem("gate", False), None)
        self.assertEqual(dir_name_problem("my_lib", False), "dir-name")
        self.assertEqual(dir_name_problem("protocol_yiban", True), None)
        self.assertEqual(dir_name_problem("ProtocolYiban", True), "pkg-name")

    def test_violations_scanner_teeth(self):
        synth = [
            "yiban/core/x.py",
            "yiban/core/__init__.py",
            "scripts/check-x.py",
            "scripts/check_x.py",
            "scripts/lib/util.sh",
            "scripts/deploy_x.sh",
            "tests/test_ok.py",
            "tests/helper.py",
            "yiban/loose.py",
            "yiban/workers/workers.py",
            "yiban/workers/__init__.py",
            "my_dir/file.txt",
        ]
        got = set(violations(synth))
        want = {
            ("scripts/deploy_x.sh", "sh-underscore"),
            ("tests/helper.py", "test-name"),
            ("yiban/loose.py", "yiban-root-loose"),
            ("yiban/workers/workers.py", "pkg-module-duplicate"),
            ("my_dir", "dir-name"),
        }
        self.assertEqual(got, want, "违规扫描器与预期不符（夹具须覆盖每条规则）")

    def test_clean_synthetic_tree_is_green(self):
        synth = [
            "yiban/core/__init__.py",
            "yiban/core/clock.py",
            "scripts/check-x.py",
            "scripts/lib/util.sh",
            "tests/test_ok.py",
            "tests/_helper.py",
            "tools/build_thing.py",
            "tools/example.md",
        ]
        self.assertEqual(violations(synth), [], "干净合成树被误红")


if __name__ == "__main__":
    unittest.main(verbosity=2)
