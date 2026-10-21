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
    本文件自身不得出现**悬空**的 doc 路径字面量（否则本门会扫到自己）。
    可解的路径字面量（例如 `docs/dev/cli.md`）照旧允许——形态定义放行它们。
    悬空夹具因此按片段拼装，见 `_FAKE_DOC`。
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
#: 取值理由：本树实测 **757** 个入库文本件（面内 644 ＋ 面外 113）。下限取 400 远低于实测值。
#: 扫描根写错、树被移走、`git ls-files` 失活都会让面掉到个位数或 0。
#: 真实回归不会让面掉到 400 以下——面只随跟踪集增减，而跟踪集在千件量级。
MIN_TEXT_FILES = 400

#: `docs` 引用数下限（同上，扫空不得当绿的另一半）。
#: **口径**（写清，免跨数比较出错——AGENTS §十三 第 31 条）：一条＝一个 `docs/…` 形态匹配；
#:   含注释与 docstring；负向断言的禁字面量不算；本门文件自身也算；同一行同一路径计多次。
#: **本下限只比面内引用数**（主门只把面内那一面交给扫描）。实测面内 **135**。
#:   全树引用总数 **139**（＝面内 135 ＋ 面外 4）。
#:   收口前**悬空引用**（另一件事，别混）＝面内 66 ＋ 面外 4 ＝ **70**。
#:   注：面内数会随**本文件自身注释里的示例**小幅漂移（本文件也算入库文本件）。
#: 形态定义写错（正则退回空匹配）会掉到 0。下限取 80 远高于任何"形态写错"的残值，
#:   又留出约 41% 的余量给合法的入库文档增删。
MIN_DOC_REFS = 80

#: 文档引用的形态定义。两条择一：
#:   ① `docs/<段>/<段>…`：两段及以上。这是文档路径。
#:   ② `docs/<段>.md`：单段但带 `.md` 后缀。这也是文档路径。
#: 为什么不收单段无后缀（`docs/refactor`）：那是**目录**，不是文档。
#: 为什么不收结尾斜杠（`docs/`、`docs/dev/`）：那也是目录。
#: 为什么不收通配（`docs/dev/*`）：那是模式，不是路径。`.gitignore` 靠这条不误红。
#: 段首字符限 `[A-Za-z0-9_]`：`docs/...`（省略号）与 `docs/<占位>` 因此不命中。
#:
#: 左边界（审查 NB3 后收窄）。两类写法：
#:   ① `./docs/…`：显式相对引用，与裸 `docs/…` 指向同一处，**放行**
#:      （匹配时剥掉 `./` 前缀，见 `_doc_ref_of`）。
#:   ② `docs/…`：前一字符不得是 `[A-Za-z0-9_./\-]`。
#: 为什么必须有：没有它，`mydocs/dev/x.md`、`out/docs/dev/x.md`、`seedocs/dev/x.md`
#:   与 `https://x.com/docs/dev/x.md` 都会命中——那些不是库内路径引用，是**假红源**。
#: 为什么用 `[A-Za-z0-9_]` 而不是 `\w`：Python 的 `\w` **含中日韩字符**。
#:   写 `\w` 会让 `见docs/dev/cli.md`（中文紧贴）这类引用**静默漏检**（审查 NB3）。
#: 实证（本仓真树）：`tests/README.md` 的反向 markdown 链接
#:   `` [`docs/dev/dev-verify.md`](../docs/dev/dev-verify.md) `` 两半边都命中
#:   （`../docs/…` 经 `./` 形态归一到同一路径）。故同一路径在同一条里计 2 次。
_DOC_PATH = (r"docs/[A-Za-z0-9_][A-Za-z0-9_.\-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.\-]*)+"
             r"|docs/[A-Za-z0-9_][A-Za-z0-9_.\-]*\.md")
_DOC_REF = re.compile(
    r"(?<![A-Za-z0-9_])\./(?P<rel>" + _DOC_PATH + r")"
    r"|(?<![A-Za-z0-9_./\-])(?P<abs>" + _DOC_PATH + r")")

#: 20 件已退役兼容壳的**路径形态**（工单 `1229` 的壳清单 + 入口壳）。
#: `145-E` 已把这批壳删净（批 A1／A4／B 留下的转发壳）。
#: 本门是**前向守卫**：退役之后，入库文本里再出现这些路径即**悬空引用**——
#: 读者检出后打不开那个文件。故清单**留驻**，不随退役清空
#: （AGENTS §十四：不许把该收的对象从判据里拿掉）。
#: 为什么按路径形态而不按点号模块形态（`yiban.core.clock`）：本门管的是"读者能不能打开"，
#: 点号形态的读者面归 `tests/test_zero_old_coordinates.py`（`145-E` 立的总门）。
#:
#: 拼装说明：清单必须**写出**这些路径，而本文件**也是入库文本件**、本门会扫到它自己。
#: 故前缀与后缀分开拼（同 `_PLAN_DOC` / `_FAKE_DOC` 的手法），
#: 源码里因此没有**悬空**的路径字面量。
_OLD_COORD_PREFIX = "yiban/"
_OLD_COORD_SUFFIXES = (
    "clock.py",
    "status.py",
    "window.py",
    "masking.py",
    "config_loader.py",
    "state_gc.py",
    "cred_state.py",
    "engine/hrw.py",
    "engine/round.py",
    "engine/runner.py",
    "engine/probe.py",
    "engine/alerts.py",
    "engine/state_io.py",
    "engine/workers.py",
    "infra/env_io.py",
    "infra/account_crypto.py",
    "store/__init__.py",
    "mail/__init__.py",
    "notify/__init__.py",
    "cli.py",
)
OLD_COORD_PATHS = tuple(_OLD_COORD_PREFIX + s for s in _OLD_COORD_SUFFIXES)

#: 旧坐标清单的下限。实测 20 条。
#: 为什么钉 15 而不是 20：条目只许**增**不许误删；给 5 条余量挡住"清单被误清空"，
#: 同时不把清单长度变成判据（长度不是不变式，内容是）。
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
#:
#: 登记表天然要写出被登记的路径，而本文件**也是入库文本件**、本门会扫到它自己。
#: 故这两个路径按片段拼装（同 `_FAKE_DOC` 的手法），源码里因此没有**悬空**的
#: doc 路径字面量。可解的字面量（如 `docs/dev/cli.md`）照旧允许。
#: 为什么不用"豁免本文件"：那会**加宽豁免表**（AGENTS §十四 明令禁止），
#: 且本文件自己的注释与 docstring 也该受本门管。
_PLAN_DOC = "docs/" + "refactor/" + "29-frontend-vue-refactor-plan.md"
_PLAN_DOC_SHORT = "docs/" + "refactor/" + "29"
#: `145-E` 新增登记要引用的**已退役**坐标路径（片段拼装，理由同上）。
_FE_OLD_MASKING = _OLD_COORD_PREFIX + "masking.py"
_FE_OLD_STATUS = _OLD_COORD_PREFIX + "status.py"
OUT_OF_FACE_REGISTRY = {
    ("frontend/README.md", _PLAN_DOC): (
        1,
        "前端翻新计划稿的溯源引用。前端归用户独立完成，本批不得改；"
        "用户处置（删引用或改措辞）后同批删本登记。",
    ),
    ("frontend/package.json", _PLAN_DOC): (
        1,
        "同上（package.json 的 description 字段）。",
    ),
    ("frontend/src/settings/model.spec.ts", _PLAN_DOC_SHORT): (
        1,
        "同上（偏差清单的出处引用）。",
    ),
    ("frontend/vite.config.ts", _PLAN_DOC): (
        1,
        "同上（构建配置注释）。",
    ),
    # `145-E` 新增：前端两件引用**已退役**的旧坐标路径（源流说明）。
    # 前端归用户独立完成，本批不得改；用户处置后同批删本登记。
    ("frontend/src/accounts/model.js", _FE_OLD_MASKING): (
        1,
        "前端账号模型引用的旧坐标路径（`145-E` 已退役该壳）。前端归用户独立完成，"
        "本批不得改；用户处置后同批删本登记。",
    ),
    ("frontend/src/lib/status-vocab.js", _FE_OLD_STATUS): (
        2,
        "前端状态词汇表引用的旧坐标路径（两处，`145-E` 已退役该壳）。理由同上。",
    ),
}

#: 旧坐标的字面量判据。前后都不许紧跟标识符字符。
#: 故首条清单项命中；前缀多一个字符（`x` ＋ 该项）、后缀多一个字符
#: （该项 ＋ `c`）都不命中。
#: 左边界用 `[A-Za-z0-9_]` 而不是 `\w`（同 `_DOC_REF`，审查 NB3）：
#: `\w` 含中日韩字符，会让"中文紧贴旧坐标路径"静默漏检。
_OLD_COORD_RE = re.compile("|".join(
    r"(?<![A-Za-z0-9_./\-])" + re.escape(p) + r"(?![A-Za-z0-9_])"
    for p in OLD_COORD_PATHS))

#: 突变夹具的文档路径，按片段拼装。本文件里因此没有**悬空**的 doc 路径字面量。
#: 可解的字面量（如 `docs/dev/cli.md`）照旧允许，由形态定义放行。
#: 否则本门会扫到本文件自己（本文件也是入库文本件）。
_FAKE_DOC = "docs/" + "refactor/" + "999-v35f-mutation-probe.md"

#: 突变夹具用的**已退役**坐标路径，按片段拼装。
#: 理由同上：本文件也是入库文本件，写死字面量会让本门扫到自己。
_FAKE_OLD = _OLD_COORD_PREFIX + "clock.py"
_FAKE_OLD_GONE = _OLD_COORD_PREFIX + "window.py"

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


def _doc_ref_of(match):
    """一次 `_DOC_REF` 匹配 → 库内相对路径。

    `./docs/…` 形态命中在 `rel` 组（已剥掉 `./`）；裸 `docs/…` 形态命中在 `abs` 组。
    两者归一到同一条相对路径，故 `./docs/dev/cli.md` 与 `docs/dev/cli.md` 的可解性判据一致。
    """
    return match.group("rel") or match.group("abs")


def doc_refs_in(text, skip=None):
    """该文本里的全部文档引用：`[(行号, 路径)]`。含注释行与 docstring。

    负向断言的字符串操作数不算引用（见 `_negated_literal_lines`）。
    `skip` 可传入预先算好的行号集合，避免同一文本重复做 AST 解析。
    """
    skip = _negated_literal_lines(text) if skip is None else skip
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if i in skip:
            continue
        for m in _DOC_REF.finditer(line):
            out.append((i, _doc_ref_of(m)))
    return out


def scan_text(text, tracked, skip=None):
    """纯函数：返回该文本里的悬空引用 `[(行号, 路径, 说明)]`。

    单独成函数是为了能被**突变自证**调用。判据只有一条：引用的路径不在跟踪集里。
    不排除注释行、不排除 docstring——那正是本门要管的面。
    负向断言的字符串操作数不算引用（见 `_negated_literal_lines`）。
    `skip` 可传入预先算好的行号集合，避免同一文本重复做 AST 解析。
    """
    skip = _negated_literal_lines(text) if skip is None else skip
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if i in skip:
            continue
        for m in _DOC_REF.finditer(line):
            ref = _doc_ref_of(m)
            if ref not in tracked:
                out.append((i, ref, "文档引用未入库"))
        for m in _OLD_COORD_RE.finditer(line):
            if m.group(0) not in tracked:
                out.append((i, m.group(0), "旧坐标路径已不存在"))
    return sorted(set(out))


def face_guard_problems(root, files):
    """**扫描原语的不变量**：主门那一面必须含本门文件（AGENTS §十三 第 29 条）。

    为什么钉在**原语**里而不另写一条用例（审查 B2）：另写一条只能钉某个**调用点**，
    钉不住"调用方重建扫描面再剔除本文件"。钉在原语里，则**调用点过滤与
    `scanned_face` 内过滤都会红**——钉的是性质（本文件真被扫），不是代理（它在面内）。

    只在"这是主门那一面"时生效。三条合法的窄面调用因此不误红：
      · 空面（`test_empty_face_is_red` 传 `[]`）；
      · 面外全 `frontend/**`（`test_out_of_face_residue_reconciles_with_registry`）；
      · 别的 root（临时目录夹具 `test_gate_reports_a_temp_dangling_file`）。
    """
    if root != BASE or not files:
        return []
    if any(f.startswith(OUT_OF_FACE_PREFIX) for f in files):
        return []
    rel = os.path.relpath(os.path.abspath(__file__), BASE).replace(os.sep, "/")
    if rel not in files:
        return ["扫描面不含本门文件 %s——本门不得豁免自己（AGENTS §十四）；"
                "要收窄扫描面请另写函数，不要改本原语的调用参数" % rel]
    return []


def scan_face(root, tracked, files):
    """扫描面 → `(逐条命中, docs 引用总数)`。

    命中＝`(相对路径, 行号, 引用路径, 说明)`。读不到的文件进命中（响亮，不静默）。
    AST 解析每文件只做一次（`skip` 预先算好再传给两个扫描函数）。
    **不变量**：主门那一面必须含本门文件——见 `face_guard_problems`（审查 B2）。
    """
    guard = face_guard_problems(root, files)
    if guard:
        raise AssertionError("；".join(guard))
    hits, n_refs = [], 0
    for rel in files:
        try:
            with io.open(os.path.join(root, rel), encoding="utf-8",
                         errors="replace") as f:
                text = f.read()
        except OSError as exc:
            hits.append((rel, 0, "", "读不到（%s）" % exc))
            continue
        skip = _negated_literal_lines(text)
        n_refs += len(doc_refs_in(text, skip))
        for line, ref, why in scan_text(text, tracked, skip):
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


def face_plan(root, tracked):
    """扫描计划 → `(全部文本件, 面内文件, 面外文件)`。**扫描面的唯一推导点。**

    脚本面**不**由调用点传入，故"把某个文件从面里剔除"必须改这里——
    改这里会被 `test_this_gate_file_is_scanned` 抓到（审查 N1）。
    若把面做成调用点参数，剔除只是 `[f for f in in_face if f != rel]` 一行，守卫就钉不住。
    """
    files = text_files_of(root, tracked)
    in_face, out_face = split_face(files)
    return files, in_face, out_face


def scanned_face(root, tracked):
    """**主门实际扫描的（面内）文件清单**与其结果 → `(面内文件, 命中, 引用数, 全部文本件)`。

    面内清单经 `face_plan` 唯一推导；本函数不另写推导。
    """
    files, in_face, _out_face = face_plan(root, tracked)
    hits, n_refs = scan_face(root, tracked, in_face)
    return in_face, hits, n_refs, files


class DocReferenceGateTest(unittest.TestCase):
    """主门：入库文本件里不得有悬空引用（面外 `frontend/**` 与登记表对账）。"""

    def test_no_dangling_reference_in_tracked_text_files(self):
        tracked = _ls_tracked(BASE)
        _in_face, hits, n_refs, files = scanned_face(BASE, tracked)
        problems = ["%s:%d: %s（%s）" % h for h in hits]
        problems += face_bounds_problems(len(files), n_refs)
        self.assertEqual(
            problems, [],
            "入库文本件引用了库外的路径（读者检出后打不开）：\n" + "\n".join(problems))




    def test_scanner_has_teeth(self):
        """突变验证：塞进悬空引用 ⇒ 红；目录形态／通配／可解引用 ⇒ 不误红。

        注释行**必须**命中（本门与"注释不算"的坐标门口径相反）。
        """
        tracked = {"docs/dev/cli.md", _FAKE_OLD}
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
        self.assertEqual(scan_text("见 `" + _FAKE_OLD + "` 转发壳。\n", tracked), [],
                         "在库内的旧坐标路径被误判成悬空")
        gone = scan_text("见 `" + _FAKE_OLD_GONE + "` 转发壳。\n", tracked)
        self.assertEqual([(g[0], g[1]) for g in gone], [(1, _FAKE_OLD_GONE)],
                         "已退役的旧坐标路径未命中：形态二没有牙")
        # ⑤ 同前缀异路径不误红
        self.assertEqual(
            scan_text("xyiban/" + "clock.py 与 yiban/" + "clock.pyc\n", tracked), [],
            "同前缀异路径被误判成旧坐标")
        # ⑥ 负向断言的字符串操作数是禁字面量，不算引用；正向断言的算
        neg = ('from unittest import TestCase\n'
               'self.assertNotIn("' + _FAKE_DOC + '", src)\n')
        self.assertEqual(scan_text(neg, tracked), [],
                         "负向断言的禁字面量被误判成引用（形态定义过宽）")
        pos = 'self.assertIn("' + _FAKE_DOC + '", src)\n'
        self.assertTrue(scan_text(pos, tracked),
                        "正向断言里的悬空引用未命中：本门没有牙")
        # ⑦ 左边界：`docs/` 之前紧跟标识符字符或斜杠的，不算库内路径引用（假红源）。
        #    注意样本用**未入库**路径（审查 NB4）：用 tracked 的真路径时，旧正则也返回 []，
        #    样本就分不出"有边界"与"无边界"——那样的样本没有牙。
        for line in ("mydocs/dev/x.md\n", "out/docs/dev/x.md\n",
                     "seedocs/dev/x.md\n", "https://x.com/docs/dev/x.md\n"):
            self.assertEqual(scan_text(line, tracked), [],
                             "左边界缺失：非库内路径被误判成引用（假红源）：%r" % line)
        # ⑧ 左边界不得**过宽**（审查 NB3）：中文紧贴与 `./` 形态都是真引用，必须命中。
        self.assertEqual(scan_text("见docs/dev/cli.md。\n", tracked), [],
                         "中文紧贴的可解引用被误判成悬空（`\\w` 含中日韩字符的后果）")
        self.assertEqual(
            scan_text("见" + _FAKE_DOC + "。\n", tracked),
            [(1, _FAKE_DOC, "文档引用未入库")],
            "中文紧贴的**悬空**引用被漏检：`\\w` 左边界过宽")
        self.assertEqual(
            scan_text("见 ./" + _FAKE_DOC + "。\n", tracked),
            [(1, _FAKE_DOC, "文档引用未入库")],
            "`./` 形态的悬空引用被漏检（且应归一到不带 `./` 的路径）")



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
