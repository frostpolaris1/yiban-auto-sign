# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""入库引用门：入库文本件里出现的路径，必须在库内（AGENTS §十 第 24 条）。

标签：J · 运维：部署/备份/发布
覆盖：AGENTS.md 第十节第 24 条要求的那道门。本门此前**从未存在**（工单 v35f）。
    判据一句话：**入库文件里出现的路径，必须在库内。**
    为什么：`docs/*` 被有意 gitignore，过程稿不外露。只有契约类文档 force-add 入库。
    规范是"契约入库、引用契约"。引用**过程稿**的路径，读者检出后打不开。
    分档判据：读者缺它就无法正确使用代码 ⇒ 契约类 ⇒ 入库。
    计划稿／审计／复盘／研究记录／带日期的任务稿 ⇒ 过程类 ⇒ 留本地。
    过程类路径不得出现在入库文件里。

    本门扫**入库文本件**（`git ls-files` 面，含二进制跳过）。它抓两类引用：
      ① `docs/<目录>/<文件>` 形态的文档路径（含反引号包裹、含 `#anchor` 后缀）；
      ② 20 件兼容壳的路径形态（`yiban/<旧坐标>.py`，见 `OLD_COORD_PATHS`）。
    两类都要求 `git ls-files` 可解。不可解即红，逐条点名 `文件:行` 与路径。

    形态定义（为什么这样定，见 `_DOC_REF` 的注释）：引用必须指向**文件或文档**。
    目录形态（`docs/`、`docs/dev/`、`docs/refactor`）与通配（`docs/dev/*`）不算引用。
    理由：`.gitignore` 与注释里的目录提及是**合法**的（它们本就在命名被忽略的树）。
    把目录形态算进来会让 `.gitignore` 自己变红，那不是缺陷。

    注释行**要扫**。本门管的正是注释与 docstring 里的悬空引用。
    这与 `tests/test_deploy_entry_imports.py` 的"注释不算"坐标门**口径相反**。
    理由：那些门管的是**运行期启动点**，注释不是启动点。本门管的是**读者可达性**，
    注释与 docstring 正是读者读的地方。

关键断言：① 全部入库文本件零悬空引用；② 扫描面与引用数都有下限（扫空不得当绿，
    AGENTS §30）；③ 形态定义自证：目录形态／通配／可解引用都不误红，注释行要命中；
    ④ 旧坐标清单非空且逐条是 `yiban/` 下的路径。
依赖：只读跟踪面文本 + `git ls-files` 子进程。不起服务、无网络、无第三方库。
    本文件自身不得出现真实 doc 路径字面量（否则本门会扫到自己）。
    突变夹具因此按片段拼装，见 `_FAKE_DOC`。
"""
import ast
import io
import os
import re
import subprocess
import tempfile
import unittest
import warnings

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 扫描面下限（AGENTS §30：扫空不得当绿）。
#: 取值理由：本树实测 **756** 个入库文本件。下限取 400 远低于实测值。
#: 扫描根写错、树被移走、`git ls-files` 失活都会让面掉到个位数或 0。
#: 真实回归不会让面掉到 400 以下——面只随跟踪集增减，而跟踪集在千件量级。
MIN_TEXT_FILES = 400

#: `docs` 引用数下限（同上，扫空不得当绿的另一半）。
#: 取值理由：本批收口后实测 **128** 条（收口前 195 条：71 条是悬空引用，已逐条处置）。
#: 形态定义写错（正则退回空匹配）会掉到 0。下限取 80 远高于任何"形态写错"的残值，
#: 又留出 37% 的余量给合法的入库文档增删。
MIN_DOC_REFS = 80

#: 文档引用的形态定义。两条择一：
#:   ① `docs/<段>/<段>…`：两段及以上。这是文档路径。
#:   ② `docs/<段>.md`：单段但带 `.md` 后缀。这也是文档路径。
#: 为什么不收单段无后缀（`docs/refactor`）：那是**目录**，不是文档。
#: 为什么不收结尾斜杠（`docs/`、`docs/dev/`）：那也是目录。
#: 为什么不收通配（`docs/dev/*`）：那是模式，不是路径。`.gitignore` 靠这条不误红。
#: 段首字符限 `[A-Za-z0-9_]`：`docs/...`（省略号）与 `docs/<占位>` 因此不命中。
_DOC_REF = re.compile(
    r"docs/[A-Za-z0-9_][A-Za-z0-9_.\-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.\-]*)+"
    r"|docs/[A-Za-z0-9_][A-Za-z0-9_.\-]*\.md")

#: 20 件兼容壳的**现行形态**（工单 `1229` 的壳清单 + `yiban/cli.py` 等）。
#: 这些路径今天都在库内。`145-E` 退役这批壳之后，指向它们的引用即悬空。
#: 本门是**前向守卫**：今天绿，退役那天红并点名残留的读者。
#: 为什么按路径形态而不按点号模块形态（`yiban.clock`）：任务口径是路径形态。
#: 点号形态的读者面归 `145-B` 各 `T` 片的坐标门（`tests/test_deploy_entry_imports.py`）。
OLD_COORD_PATHS = (
    "yiban/clock.py",
    "yiban/status.py",
    "yiban/window.py",
    "yiban/masking.py",
    "yiban/config_loader.py",
    "yiban/state_gc.py",
    "yiban/cred_state.py",
    "yiban/engine/hrw.py",
    "yiban/engine/round.py",
    "yiban/engine/runner.py",
    "yiban/engine/probe.py",
    "yiban/engine/alerts.py",
    "yiban/engine/state_io.py",
    "yiban/engine/workers.py",
    "yiban/infra/env_io.py",
    "yiban/infra/account_crypto.py",
    "yiban/store/__init__.py",
    "yiban/mail/__init__.py",
    "yiban/notify/__init__.py",
    "yiban/cli.py",
)

#: 旧坐标清单的下限。实测 20 条。下限取 15 挡住"清单被误清空"。
#: 为什么不用 20：清单会随退役批次缩短，钉死 20 会让合法删条目变红。
MIN_OLD_COORD_PATHS = 15

#: 面外目录前缀。`frontend/**` 2026-10-18 起归用户独立完成，本批**不得改**它
#: （与 `tests/test_deploy_entry_imports.py` 的 `_T2_OUT_OF_FACE_REGISTRY` 同口径）。
#: 面外残留不判红，改为与登记表对账——未登记即红、次数不符即红、登记消失即红。
#: 这不是"把面从判据里拿掉"：残留逐条可见、逐条对账，两侧都不许静默。
OUT_OF_FACE_PREFIX = "frontend/"

#: 面外（`frontend/**`）的待销账登记。键＝`(相对路径, 引用路径)`，值＝`(次数, 说明)`。
#: 为什么按 `(路径, 引用, 次数)` 而不把行号钉进判据：前端归用户独立完成，行号会随其
#: 无关改动漂移。把行号做成判据会让**用户的 PR 因一行位移而红**，而修法却落在我的文件里
#: ——那正是"门看的东西 ≠ 实际改的东西"（AGENTS §十三 第 29 条）。
#: 四处都是"前端翻新计划稿"的溯源引用（过程稿，不入库）。用户处置后须同批删本登记。
OUT_OF_FACE_REGISTRY = {
    ("frontend/README.md", "docs/refactor/29-frontend-vue-refactor-plan.md"): (
        1,
        "前端翻新计划稿的溯源引用。前端归用户独立完成，本批不得改；"
        "用户处置（删引用或改措辞）后同批删本登记。",
    ),
    ("frontend/package.json", "docs/refactor/29-frontend-vue-refactor-plan.md"): (
        1,
        "同上（package.json 的 description 字段）。",
    ),
    ("frontend/src/settings/model.spec.ts", "docs/refactor/29"): (
        1,
        "同上（偏差清单的出处引用）。",
    ),
    ("frontend/vite.config.ts", "docs/refactor/29-frontend-vue-refactor-plan.md"): (
        1,
        "同上（构建配置注释）。",
    ),
}

#: 旧坐标的字面量判据。前后都不许紧跟标识符字符。
#: 故 `yiban/clock.py` 命中；`xyiban/clock.py`、`yiban/clock.pyc` 不命中。
_OLD_COORD_RE = re.compile("|".join(
    r"(?<![\w./\-])" + re.escape(p) + r"(?![\w])" for p in OLD_COORD_PATHS))

#: 突变夹具的文档路径，按片段拼装。本文件里因此没有真实 doc 路径字面量。
#: 否则本门会扫到本文件自己（本文件也是入库文本件）。
_FAKE_DOC = "docs/" + "refactor/" + "999-v35f-mutation-probe.md"

#: 二进制判据的取样字节数（含 NUL 即跳）。
_SNIFF = 8192

#: 负向断言的调用名。它们的字符串操作数是**禁字面量**，不是引用。
#: 见 `_negated_literal_lines`。
_NEG_ASSERT_FUNCS = frozenset((
    "assertNotIn", "assertNotRegex", "assertNotEqual", "assertNotIsInstance"))


def _negated_literal_lines(text):
    """该 Python 文本里"负向断言的字符串操作数"所在行号集合。

    形态定义的一部分，不是豁免表。理由：本门判据是"入库文件里出现的**路径引用**
    必须在库内"。负向断言（`assertNotIn` 一族）的字符串操作数是**禁字面量**：
    它断言该串**不得出现**，读者不会被要求去打开它。把它读成引用是形态误判。
    实证：`tests/test_audit_chain.py` 的 `assertNotIn("docs/web-console/…", self.src)`
    断言备份脚本**不得**引用一份仓库里不存在的部署清单——它本身就是本族约束的守卫。

    非 Python 文本、语法错的文本都返回空集（不静默吞错：语法错的行照旧被扫）。
    解析别的文件时抑制 SyntaxWarning：那是**被扫文件**的告警，不是本门的。
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return frozenset()
    lines = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if fn not in _NEG_ASSERT_FUNCS:
            continue
        for a in node.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                start = getattr(a, "lineno", None)
                if start:
                    lines.update(range(start, (getattr(a, "end_lineno", None) or start) + 1))
    return frozenset(lines)


def _ls_tracked(root):
    """跟踪集（相对路径集合）。`git ls-files -z`。

    WSL 里跑 Windows git worktree 时，`.git` 文件里的 gitdir 是 Windows 绝对路径
    （`D:/...`），WSL git 无法解析——换算成 `/mnt/<盘>/...` 重试。
    这条与 `tests/test_deploy_prod_artifacts.py`、`tests/test_alert_frequency_caliber.py`
    的既有做法同口径。
    """
    run = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                         capture_output=True, timeout=120)
    if run.returncode != 0:
        gitfile = os.path.join(root, ".git")
        if os.path.isfile(gitfile):
            with io.open(gitfile, encoding="utf-8") as f:
                gitdir = f.read().strip()
            if gitdir.startswith("gitdir:"):
                gitdir = gitdir[len("gitdir:"):].strip().replace("\\", "/")
            if len(gitdir) > 2 and gitdir[1] == ":":
                gitdir = "/mnt/" + gitdir[0].lower() + gitdir[2:]
            run = subprocess.run(["git", "--git-dir", gitdir, "--work-tree", root,
                                  "ls-files", "-z"], capture_output=True, timeout=120)
    assert run.returncode == 0, run.stderr.decode("utf-8", "replace")
    return {p.decode("utf-8") for p in run.stdout.split(b"\x00") if p}


def _is_binary(path):
    """含 NUL 字节即二进制（跳过）。读不到也当二进制（不当成空文本）。"""
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(_SNIFF)
    except OSError:
        return True


def text_files_of(root, tracked):
    """入库文本件清单（跟踪集去掉二进制）。"""
    out = []
    for rel in sorted(tracked):
        if not _is_binary(os.path.join(root, rel)):
            out.append(rel)
    return out


def doc_refs_in(text):
    """该文本里的全部文档引用：`[(行号, 路径)]`。含注释行与 docstring。

    负向断言的字符串操作数不算引用（见 `_negated_literal_lines`）。
    """
    skip = _negated_literal_lines(text)
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if i in skip:
            continue
        for m in _DOC_REF.finditer(line):
            out.append((i, m.group(0)))
    return out


def scan_text(text, tracked):
    """纯函数：返回该文本里的悬空引用 `[(行号, 路径, 说明)]`。

    单独成函数是为了能被**突变自证**调用。判据只有一条：引用的路径不在跟踪集里。
    不排除注释行、不排除 docstring——那正是本门要管的面。
    负向断言的字符串操作数不算引用（见 `_negated_literal_lines`）。
    """
    skip = _negated_literal_lines(text)
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if i in skip:
            continue
        for m in _DOC_REF.finditer(line):
            if m.group(0) not in tracked:
                out.append((i, m.group(0), "文档引用未入库"))
        for m in _OLD_COORD_RE.finditer(line):
            if m.group(0) not in tracked:
                out.append((i, m.group(0), "旧坐标路径已不存在"))
    return sorted(set(out))


def scan_face(root, tracked, files):
    """扫描面 → `(逐条命中, docs 引用总数)`。

    命中＝`(相对路径, 行号, 引用路径, 说明)`。读不到的文件进命中（响亮，不静默）。
    """
    hits, n_refs = [], 0
    for rel in files:
        try:
            with io.open(os.path.join(root, rel), encoding="utf-8",
                         errors="replace") as f:
                text = f.read()
        except OSError as exc:
            hits.append((rel, 0, "", "读不到（%s）" % exc))
            continue
        n_refs += len(doc_refs_in(text))
        for line, ref, why in scan_text(text, tracked):
            hits.append((rel, line, ref, why))
    return hits, n_refs


def split_face(files):
    """扫描面 → `(面内文件, 面外文件)`。面外＝`frontend/**`（见 `OUT_OF_FACE_REGISTRY`）。"""
    in_face = [f for f in files if not f.startswith(OUT_OF_FACE_PREFIX)]
    out_face = [f for f in files if f.startswith(OUT_OF_FACE_PREFIX)]
    return in_face, out_face


def face_bounds_problems(n_files, n_refs):
    """下限检查（AGENTS §30：扫空不得当绿）。返回问题清单。"""
    problems = []
    if n_files < MIN_TEXT_FILES:
        problems.append(
            "扫描面过小（%d 个入库文本件，下限 %d）——扫描根写错或树被移走"
            "（扫空不得当绿）" % (n_files, MIN_TEXT_FILES))
    if n_refs < MIN_DOC_REFS:
        problems.append(
            "扫到的 docs 引用过少（%d 条，下限 %d）——形态定义写错或树被移走"
            "（扫空不得当绿）" % (n_refs, MIN_DOC_REFS))
    return problems


def out_of_face_problems(found, registry=None, where=None):
    """面外残留与登记表对账（纯函数，便于突变自证）：返回问题清单（空＝一致）。

    `found` ＝ `{(相对路径, 引用路径): 命中次数}`；`where` ＝ 同键的 `[行号]`（仅用于消息）。
    三条判据（同 `tests/test_deploy_entry_imports.py` 的 `*_out_of_face_problems`）：
      · 未登记的新残留 ⇒ 红；
      · 登记的次数与实际不符 ⇒ 红（多一处＝新增；少一处＝已处置一半）；
      · 登记整条消失 ⇒ 红（用户那边已处置，必须同批销账，否则登记变僵尸）。
    """
    registry = OUT_OF_FACE_REGISTRY if registry is None else registry
    where = where or {}
    problems = []
    for key, n in sorted(found.items()):
        entry = registry.get(key)
        if entry is None:
            problems.append(
                "%s:%s 出现未登记的面外引用 %s（%d 处）——"
                "要么在面内处置，要么在 OUT_OF_FACE_REGISTRY 登记"
                % (key[0], where.get(key), key[1], n))
        elif n != entry[0]:
            problems.append(
                "%s:%s 的面外引用 %s 实测 %d 处、登记 %d 处"
                "——两侧必须一致（新增不许漏登记；已处置的必须同批销账）"
                % (key[0], where.get(key), key[1], n, entry[0]))
    for key, (n, _why) in sorted(registry.items()):
        if key not in found:
            problems.append(
                "登记 %s 的 %s（%d 处）已不在代码里——"
                "用户那边已处置，请同批删掉这条登记（防登记变成僵尸）"
                % (key[0], key[1], n))
    return problems


class DocReferenceGateTest(unittest.TestCase):
    """主门：入库文本件里不得有悬空引用（面外 `frontend/**` 与登记表对账）。"""

    def test_no_dangling_reference_in_tracked_text_files(self):
        tracked = _ls_tracked(BASE)
        files = text_files_of(BASE, tracked)
        in_face, _out_face = split_face(files)
        hits, n_refs = scan_face(BASE, tracked, in_face)
        problems = ["%s:%d: %s（%s）" % h for h in hits]
        problems += face_bounds_problems(len(files), n_refs)
        self.assertEqual(
            problems, [],
            "入库文本件引用了库外的路径（读者检出后打不开）：\n" + "\n".join(problems))

    def test_out_of_face_residue_reconciles_with_registry(self):
        """面外（`frontend/**`）残留与登记表逐条对账，两侧都不许静默。"""
        tracked = _ls_tracked(BASE)
        files = text_files_of(BASE, tracked)
        _in_face, out_face = split_face(files)
        self.assertGreaterEqual(
            len(out_face), 10,
            "面外扫描面过小（%d 个文件）——扫描根写错或前端被移走（扫空不得当绿）"
            % len(out_face))
        hits, _n_refs = scan_face(BASE, tracked, out_face)
        found, where = {}, {}
        for rel, line, ref, _why in hits:
            found[(rel, ref)] = found.get((rel, ref), 0) + 1
            where.setdefault((rel, ref), []).append(line)
        problems = out_of_face_problems(found, where=where)
        self.assertEqual(
            problems, [], "面外引用残留与登记表不一致：\n" + "\n".join(problems))

    def test_out_of_face_registry_reconciliation_has_teeth(self):
        """突变验证（合成输入）：未登记 ⇒ 红；次数不符 ⇒ 红；登记消失 ⇒ 红；一致 ⇒ 绿。"""
        key = ("frontend/x.js", "docs/refactor/29")
        reg = {key: (1, "登记理由")}
        self.assertEqual(out_of_face_problems({key: 1}, registry=reg), [],
                         "一致时不该报问题（否则三条判据可能是一组恒真断言）")
        self.assertTrue(out_of_face_problems(
            {key: 1, ("frontend/y.js", "docs/refactor/29"): 1}, registry=reg),
            "未登记的面外残留未判红：本门没有牙")
        self.assertTrue(out_of_face_problems({key: 2}, registry=reg),
                        "命中次数多于登记未判红：新增残留会被静默放过")
        self.assertTrue(out_of_face_problems({}, registry=reg),
                        "登记已消失未判红：登记会变成僵尸")

    def test_old_coordinate_list_is_intact(self):
        """旧坐标清单不得被误清空或写歪（否则形态二静默失效）。"""
        self.assertGreaterEqual(
            len(OLD_COORD_PATHS), MIN_OLD_COORD_PATHS,
            "旧坐标清单只剩 %d 条（下限 %d）——形态二会静默失效"
            % (len(OLD_COORD_PATHS), MIN_OLD_COORD_PATHS))
        for p in OLD_COORD_PATHS:
            self.assertTrue(p.startswith("yiban/") and p.endswith(".py"),
                            "旧坐标清单条目不是 `yiban/**/*.py` 形态：%r" % p)

    def test_scanner_has_teeth(self):
        """突变验证：塞进悬空引用 ⇒ 红；目录形态／通配／可解引用 ⇒ 不误红。

        注释行**必须**命中（本门与"注释不算"的坐标门口径相反）。
        """
        tracked = {"docs/dev/cli.md", "yiban/clock.py"}
        # ① 悬空 doc 引用（在注释行里）⇒ 命中，且行号正确
        bad = scan_text("# 口径见 " + _FAKE_DOC + " 第 3 节\nX = 1\n", tracked)
        self.assertEqual([(b[0], b[1]) for b in bad], [(1, _FAKE_DOC)],
                         "注释行里的悬空 doc 引用未命中：本门没有牙")
        # ② 可解引用 ⇒ 不误红
        self.assertEqual(scan_text("见 docs/dev/cli.md。\n", tracked), [],
                         "已入库的 docs 引用被误判成悬空")
        # ③ 目录形态与通配 ⇒ 不误红（`.gitignore` 靠这条不红）
        for line in ("docs/*\n", "!docs/dev/\n", "docs/dev/*\n",
                     "`tests/`、`docs/`、`README.md` 不在面内\n",
                     "见 docs/refactor 的 PENDING\n", "前缀 feat/fix/docs/...\n"):
            self.assertEqual(scan_text(line, tracked), [],
                             "目录形态／通配被误判成文档引用：%r" % line)
        # ④ 旧坐标路径：在库内 ⇒ 不误红；不在库内 ⇒ 命中
        self.assertEqual(scan_text("见 `yiban/clock.py` 转发壳。\n", tracked), [],
                         "在库内的旧坐标路径被误判成悬空")
        gone = scan_text("见 `yiban/window.py` 转发壳。\n", tracked)
        self.assertEqual([(g[0], g[1]) for g in gone], [(1, "yiban/window.py")],
                         "已退役的旧坐标路径未命中：形态二没有牙")
        # ⑤ 同前缀异路径不误红
        self.assertEqual(scan_text("xyiban/clock.py 与 yiban/clock.pyc\n", tracked), [],
                         "同前缀异路径被误判成旧坐标")
        # ⑥ 负向断言的字符串操作数是禁字面量，不算引用；正向断言的算
        neg = ('from unittest import TestCase\n'
               'self.assertNotIn("' + _FAKE_DOC + '", src)\n')
        self.assertEqual(scan_text(neg, tracked), [],
                         "负向断言的禁字面量被误判成引用（形态定义过宽）")
        pos = 'self.assertIn("' + _FAKE_DOC + '", src)\n'
        self.assertTrue(scan_text(pos, tracked),
                        "正向断言里的悬空引用未命中：本门没有牙")

    def test_empty_face_is_red(self):
        """扫空不得当绿：空面必须判红（两条下限都要挡）。"""
        problems = face_bounds_problems(0, 0)
        self.assertTrue(problems, "空扫描面判绿：下限断言失效，本门等于不存在")
        self.assertTrue(any("扫描面过小" in p for p in problems),
                        "空面未触发文件数下限：%r" % problems)
        self.assertTrue(any("引用过少" in p for p in problems),
                        "空面未触发引用数下限：%r" % problems)
        # 端到端那一半：空面上真跑一次扫描，命中与引用数都必须是 0。
        hits, n_refs = scan_face(BASE, set(), [])
        self.assertEqual((hits, n_refs), ([], 0))

    def test_gate_reports_a_temp_dangling_file(self):
        """端到端：往扫描面里塞一个含悬空引用的文件 ⇒ 必须点名该文件与行号。"""
        with tempfile.TemporaryDirectory(prefix="v35f-gate-") as tmp:
            rel = "tests/test_probe_synthetic.py"
            os.makedirs(os.path.join(tmp, "tests"), exist_ok=True)
            with io.open(os.path.join(tmp, rel), "w", encoding="utf-8") as f:
                f.write('"""合成探针。"""\n# 见 ' + _FAKE_DOC + "\n")
            hits, _n_refs = scan_face(tmp, {rel}, [rel])
            named = ["%s:%d: %s（%s）" % h for h in hits]
            hit = [p for p in named if p.startswith(rel + ":2:")]
            self.assertEqual(len(hit), 1,
                             "合成悬空引用未被点名（文件:行）：%r" % named)
            self.assertIn(_FAKE_DOC, hit[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
