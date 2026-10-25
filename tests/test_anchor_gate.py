# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""锚点门：目标树的「现存」锚点必须在场，B1 搬迁的旧坐标必须清零（B1 收口）。

标签：M · 架构分层与基础设施
覆盖：
    ① `docs/dev/target-tree.md` §8.2 的「现存」锚点逐个存在（目录或文件）；
    ② 标「计划」的锚点只登记、不要求存在（B3 才落地）；
    ③ B1 搬迁的 8 个旧坐标（2 个共享模块 + 6 个开发工具）本体不存在；
    ④ 跟踪集内不得再有旧坐标读者：路径字面量与裸 import 两面都判；
    ⑤ 门自身有牙：夹具样本必红、干净样本不得误红、扫描面有下限（扫空不得当绿）。

**为什么判据要同时扫路径与 import**：旧路径文件删掉后，`import child_env` 在缺
`scripts/` 的部署形态下当场炸，而路径字面量（`python scripts/child_env.py`）不炸但
读者按它找不到文件。两面合起来才是「旧坐标清零」。

**面**：`git ls-files` 跟踪集（全仓）。跟踪集外的本地残留不改变结论；新文件须先
`git add` 才进面（与 `tests/test_shared_facts_gate.py` 同口径）。
**豁免**：`docs/dev/target-tree.md` 必须写出旧坐标（迁移记录是它的职责）；
本门自身必须写出禁字面量做夹具。两条豁免都按「仍在写旧坐标」锚住，失去理由即红。
**已知盲点（显式登记，AGENTS §28）**：只判路径形态与 import 形态。运行期字符串
`importlib.import_module("child_env")` 不在判据面内；今天仓内实测 0 处。

依赖：只读跟踪集文本 + 文件系统；不导入业务包、不联网。
"""
import ast
import io
import os
import re
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def REL(*parts):
    """posix 相对路径（跨平台拼接）。"""
    return "/".join(parts)

#: §8.2 的「现存」锚点（B1 收口态）。逐条对应契约表，新增项须同批改两处。
EXISTING_ANCHORS = (
    "yiban/core", "yiban/engine", "yiban/app", "yiban/workers", "yiban/adapters",
    "yiban/infra", "yiban/ports", "yiban/entry",
    "yiban/infra/env_child.py", "yiban/core/email_policy.py",
    "web/routes", "web/services", "web/templates", "web/static/vue", "web/deploy",
    "frontend/src", "frontend/e2e",
    "scripts", "tests", "deploy/prod", "docker", "tools",
    "config/registry.json", "docs/dev", "docs/dev/target-tree.md",
    "run.sh", "run_probe.sh", "docker-compose.yml",
)

#: §8.2 的「计划」锚点（B3 前不要求存在）。与现存表不得相交。
PLANNED_ANCHORS = (
    "yiban/usecases",
    "yiban/usecases/verify_jobs.py",
    "yiban/workers/pool.py",
    "scripts/compat",
)

#: B1 搬迁的旧坐标（模块 stem）。路径形态由 `scripts/` 前缀拼装，
#: 故本文件源码里没有悬空的旧路径字面量。
MOVED_STEMS = (
    "child_env",
    "email_policy",
    "build_cjk_font_slices",
    "build_lucide_sprite",
    "stamp_font_versions",
    "dev_visual_seed",
    "generate_demo_data",
    "elbow_shadow",
)
OLD_DIR = "scripts"

#: 职责就是写出旧坐标的文件。逐条给理由，并由 `test_exempt_files_are_anchored` 钉住。
EXEMPT_FILES = {
    "tests/test_anchor_gate.py":
        "本门自身：禁字面量做夹具，自证有牙",
    "docs/dev/target-tree.md":
        "迁移记录：§0/§2 必须写出源坐标，否则读者不知道旧路径去了哪里",
}

#: 扫描面下限（AGENTS §30：扫空不得当绿）。ref＝`origin/develop` `aa75e2b9` 实测 966。
MIN_FACE_FILES = 700


def _moved_paths():
    """B1 搬迁件的旧路径形态（`scripts/<stem>.py`）。"""
    return tuple(REL(OLD_DIR, stem + ".py") for stem in MOVED_STEMS)


def _old_path_re(path):
    """旧路径的字面量判据：前后不接路径/标识符字符。"""
    return re.compile(r"(?<![\w./-])" + re.escape(path) + r"(?![\w-])")


def old_path_hits(text, paths=None):
    """文本里出现的旧路径：`[(行号, 路径)]`。"""
    paths = paths if paths is not None else _moved_paths()
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        for p in paths:
            if _old_path_re(p).search(line):
                out.append((i, p))
    return sorted(set(out))


def bare_import_hits(source):
    """源码里的旧坐标 import：`[(行号, 说明)]`。

    覆盖 `import child_env`、`import child_env as x`、
    `from child_env import ...`、`from scripts.child_env import ...`。
    """
    tree = ast.parse(source)
    out = []
    old = set(MOVED_STEMS)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                if parts[0] in old or (
                        len(parts) > 1 and parts[0] == OLD_DIR and parts[1] in old):
                    out.append((node.lineno, "import " + a.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            mod = node.module or ""
            parts = mod.split(".")
            if parts[0] in old or (
                    len(parts) > 1 and parts[0] == OLD_DIR and parts[1] in old):
                out.append((node.lineno, "from " + mod + " import ..."))
            elif mod == OLD_DIR:
                for a in node.names:
                    if a.name in old:
                        out.append((node.lineno, "from scripts import " + a.name))
    return sorted(set(out))


def _tracked(root):
    """`git ls-files` 跟踪集（相对 root）；非 git 树返回 None。"""
    try:
        proc = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                              capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return {p for p in proc.stdout.decode("utf-8", "replace").split(chr(0)) if p}


def _read_text(path):
    """按 UTF-8 严格读；二进制/坏编码返回 None（不参与文本判据）。"""
    try:
        with io.open(path, encoding="utf-8") as f:
            return f.read()
    except (UnicodeDecodeError, OSError):
        return None


def face_files(root, tracked):
    """锚点门的面：跟踪集里可读文本件（升序相对路径）。"""
    out = []
    for rel in sorted(tracked):
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        if _read_text(path) is None:
            continue
        out.append(rel)
    return out


def old_coordinate_hits(root, rel):
    """该跟踪件的旧坐标读者：路径形态（全文本）+ import 形态（.py）。"""
    path = os.path.join(root, rel)
    text = _read_text(path)
    if text is None:
        return []
    hits = [(ln, "路径 " + p) for ln, p in old_path_hits(text)]
    if rel.endswith(".py"):
        try:
            hits += [(ln, why) for ln, why in bare_import_hits(text)]
        except SyntaxError as exc:
            hits.append((0, "语法错误，判据无法判定：" + str(exc)))
    return sorted(set(hits))


def anchor_problems(root, anchors):
    """锚点存在性：`[(路径, 说明)]`。"""
    return [(a, "锚点不存在") for a in anchors
            if not os.path.exists(os.path.join(root, a))]


class ExistingAnchorTest(unittest.TestCase):
    """① 现存锚点在场；② 计划锚点只登记。"""

    def test_existing_anchors_are_present(self):
        self.assertEqual(anchor_problems(BASE, EXISTING_ANCHORS), [],
                         "target-tree §8.2 的现存锚点缺失")

    def test_planned_anchors_are_registered_and_disjoint(self):
        self.assertTrue(PLANNED_ANCHORS, "计划锚点表为空：本门读不到 B3 的落点")
        self.assertEqual(set(EXISTING_ANCHORS) & set(PLANNED_ANCHORS), set(),
                         "同一个路径同时登记为现存与计划")

    def test_anchor_checker_has_teeth(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "yiban"))
            self.assertEqual(
                anchor_problems(tmp, ["yiban", "yiban/core"]),
                [("yiban/core", "锚点不存在")],
                "合成树里缺一个锚点必须被点名（否则本门无牙）")


class MovedCoordinateTest(unittest.TestCase):
    """③ 旧坐标本体不存在；④ 跟踪集零旧坐标读者。"""

    def test_moved_old_files_are_absent(self):
        alive = [p for p in _moved_paths()
                 if os.path.exists(os.path.join(BASE, p))]
        self.assertEqual(alive, [], "B1 搬迁的旧路径仍在（旧路径不留壳）："
                         + "；".join(alive))
        self.assertEqual(len(_moved_paths()), 8,
                         "搬迁清单条数变了：本门按 8 条立，清单被误改即红")

    def test_tracked_face_has_no_old_coordinate_reader(self):
        tracked = _tracked(BASE)
        self.assertIsNotNone(tracked, "读不到 git 跟踪集：本门无法判定（不是绿）")
        files = face_files(BASE, tracked)
        self.assertGreaterEqual(
            len(files), MIN_FACE_FILES,
            "扫描面只有 %d 个文本件（下限 %d）：扫描口径坏了，零命中会变成恒真断言"
            % (len(files), MIN_FACE_FILES))
        bad = []
        for rel in files:
            if rel in EXEMPT_FILES:
                continue
            for line, why in old_coordinate_hits(BASE, rel):
                bad.append("%s:%d: %s" % (rel, line, why))
        self.assertEqual(bad, [], "跟踪集仍引用 B1 搬迁前的旧坐标：\n" + "\n".join(bad))

    def test_exempt_files_are_anchored(self):
        """豁免不腐烂：文件仍在，且仍在写旧坐标。"""
        for rel, why in sorted(EXEMPT_FILES.items()):
            with self.subTest(rel=rel):
                self.assertTrue(os.path.isfile(os.path.join(BASE, rel)),
                                "豁免文件 %s 不在了（理由：%s）" % (rel, why))
                self.assertTrue(old_coordinate_hits(BASE, rel),
                                "%s 已不再写出旧坐标：豁免失去理由，须移除" % rel)

    def test_the_gate_itself_is_in_the_face(self):
        tracked = _tracked(BASE)
        self.assertIsNotNone(tracked, "读不到 git 跟踪集：本门无法判定（不是绿）")
        rel = os.path.relpath(os.path.abspath(__file__), BASE).replace(os.sep, "/")
        self.assertIn(rel, face_files(BASE, tracked),
                      "扫描面不含本门文件——剔除本文件会让门静默变窄")
        self.assertIn(rel, EXEMPT_FILES,
                      "本门写出禁字面量做夹具，必须显式登记豁免（不许静默）")


class GateHasTeethTest(unittest.TestCase):
    """⑤ 判据自证：夹具必红、干净样本不得误红。"""

    def test_path_fixtures_are_detected(self):
        sample = _moved_paths()[0]
        cases = (
            "python " + sample,
            "见 `" + sample + "` 的告警口径",
            '["' + sample + '"]',
        )
        for src in cases:
            with self.subTest(src=src):
                self.assertTrue(old_path_hits(src),
                                "旧路径夹具未被判红：本门没有牙 —— %r" % (src,))

    def test_clean_path_fixtures_are_not_flagged(self):
        stem = MOVED_STEMS[0]
        cases = (
            "python tools/" + stem + ".py",
            "yiban/infra/env_child.py",
            "# 旧路径 " + OLD_DIR + "/" + stem + "（无扩展名，指目录）",
            "scripts/signin.py",
        )
        for src in cases:
            with self.subTest(src=src):
                self.assertEqual(old_path_hits(src), [],
                                 "干净样本被误红：%r" % (src,))

    def test_import_fixtures_are_detected(self):
        stem = MOVED_STEMS[0]
        for src in (
            "import " + stem,
            "from " + stem + " import build_child_env",
            "from scripts." + stem + " import x",
            "from scripts import " + stem,
        ):
            with self.subTest(src=src):
                self.assertTrue(bare_import_hits(src),
                                "旧 import 夹具未被判红：%r" % (src,))

    def test_clean_import_fixtures_are_not_flagged(self):
        for src in (
            "from yiban.infra import env_child",
            "from tools import stamp_font_versions",
            "from yiban.core import email_policy",
            "import yiban",
        ):
            with self.subTest(src=src):
                self.assertEqual(bare_import_hits(src), [],
                                 "干净 import 被误红：%r" % (src,))


if __name__ == "__main__":
    unittest.main(verbosity=2)
