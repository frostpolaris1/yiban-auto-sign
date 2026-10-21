# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""145-E 壳退役后的「零旧坐标」总门：20 个退役坐标不得再有读者。

标签：M · 架构分层与基础设施
覆盖：`145-E` 删除 20 个转发壳。壳删后旧坐标不再是合法 import 路径，
    串内坐标当场 `ModuleNotFoundError`，属性打桩静默打空。
    本文件是全仓的**总门**（各家族门另有 `tests/test_t5_t7_relocation.py`、
    `tests/test_deploy_entry_imports.py`；本门按形状收口，不逐族重复口径）。

20 个退役坐标（点号形态）：
    `yiban.clock`、`yiban.status`、`yiban.window`、`yiban.masking`、
    `yiban.config_loader`、`yiban.cred_state`、`yiban.state_gc`、
    `yiban.infra.env_io`、`yiban.infra.account_crypto`、
    `yiban.engine.hrw`、`yiban.engine.round`、`yiban.engine.runner`、
    `yiban.engine.probe`、`yiban.engine.alerts`、`yiban.engine.state_io`、
    `yiban.engine.workers`、`yiban.cli`、`yiban.mail`、`yiban.notify`、`yiban.store`。

四条不变式：
  ① **退役壳本体不存在**：20 条路径在库内均不可见（先红后绿的主驱动）。
  ② **零旧坐标读者**：自有面不得以旧坐标作 import 路径、属性链或运行期字符串。
  ③ **豁免逐条有锚点**：登记的文件须仍在，且须**仍写出**旧坐标——豁免失去理由即红。
     日志通道名按**实参位**豁免（`getLogger`／`assertLogs`／`assertNoLogs`），不按文件豁免。
  ④ **门自身有牙**：夹具样本必判红；干净样本不得误红；扫描面有下限（扫空不得当绿）。

**为什么判据必须同时扫 import 语句与运行期字符串面**：静态依赖图只看 `import`。
`importlib.import_module("yiban.store.db")`、`mock.patch("yiban.clock.now")` 这类
**字符串坐标**不在它的判定面内。故本门两面包围。
**为什么 import 判据要认"父包别名"形态**：`from yiban.infra import env_io` 里没有
`yiban.infra.env_io` 这个连续字面量。只按点号字面量扫会**漏掉它**（批 1 实测漏过
`report.py`）。本门按 AST 逐项判名，并展开属性链。

**口径（改动面怎么数出来的）**：某文件命中＝该文件出现上述 import 写法之一、
属性链、或运行期字符串。**注释与 docstring 里的旧坐标字样不计入**（散文指针与
历史叙述不误红）。`.md`／`.toml`／`.sh` 等非 Python 文本件按**整行文本**判（无注释面）。

**面（扫描根）**：`yiban/`、`web/`、`scripts/`、`docker/`、`deploy/`、`tests/`、
`docs/dev/` 七棵子树 ＋ 根目录的 `run.sh`、`run_probe.sh`、`pyproject.toml`、
`.env.example`、`README.md`、`CHANGELOG.md`。
`frontend/` 与 `web/static/` 归用户独立完成，本门**不扫**（与
`tests/test_doc_reference_gate.py` 的面外口径一致）。
`docs/` 只扫 `docs/dev/`（契约类入库；其余是本地过程稿，不入库也不扫）。

**与 `tests/test_doc_reference_gate.py` 的分工**：那门扫**入库文本件**里的
**路径形态**（`yiban/` 下的 `.py` 旧坐标），读者是"打开文件的人"。本门扫**读者面**的
**点号形态**（`yiban.clock`），读者是"跑起来的进程"。两门面有重叠，判据不同。

**已知盲点（显式登记，AGENTS §28）**：
  ① **Python 注释与 docstring 里的点号旧坐标不计入**（散文指针与历史叙述不误红，
     与 `tests/test_t5_t7_relocation.py`、`tests/test_deploy_entry_imports.py` 同口径）。
     **实测规模**（口径＝`git ls-files` 跟踪集里的 `.py`，AST 取 docstring 跨行区间 ＋
     `#` 行，判据＝20 条点号坐标，ref＝本分支工作树 `145-E` 收口态）：**444 处 / 134 文件**。
     例：`scripts/db.py` 的 docstring 写"兼容壳 → `yiban.store.db`"，那是**历史叙述**。
     **谁守**：无门。该面归 `145-F`（散文与观测面批次）按 `AGENTS §24`（入库文本不得
     引用库外的东西）逐条处置。本批只处置**路径形态**（那由 `tests/test_doc_reference_gate.py`
     逐条点名，本批已清零）。
  ② **`.md` 之外的纯散文载体**（`CHANGELOG.md` 之类）按整行判点号形态；行内历史叙述
     若含点号旧坐标会误红。当前仓内实测为 0，故本批不另开豁免。
  ③ **面外 `frontend/**` 不扫**：前端归用户独立完成（`AGENTS §7`）。其旧坐标路径引用由
     `tests/test_doc_reference_gate.py` 的 `OUT_OF_FACE_REGISTRY` 逐条对账（本批新增 2 条登记）。

对应实现：20 个新坐标下的模块本体（批 A1／A4／B 已搬）。
关键断言：① ③ ④ 读文件系统与源码文本；② 用 `ast` 判 import 与字符串面。
依赖：只读仓库文件；不导入包、不联网、不建库。
"""

import ast
import functools
import io
import os
import re
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 20 个退役坐标的点号形态。**本表是禁字面量**，本门自身按路径豁免（见 `EXEMPT_FILES`）。
OLD_MODULES = (
    "yiban.clock",
    "yiban.status",
    "yiban.window",
    "yiban.masking",
    "yiban.config_loader",
    "yiban.cred_state",
    "yiban.state_gc",
    "yiban.infra.env_io",
    "yiban.infra.account_crypto",
    "yiban.engine.hrw",
    "yiban.engine.round",
    "yiban.engine.runner",
    "yiban.engine.probe",
    "yiban.engine.alerts",
    "yiban.engine.state_io",
    "yiban.engine.workers",
    "yiban.cli",
    "yiban.mail",
    "yiban.notify",
    "yiban.store",
)

#: 同一批坐标的**路径形态**（退役壳本体）。不变式①钉这 20 条不存在。
#:
#: 拼装说明：本表必须写出这些路径，而本文件也是入库文本件——
#: `tests/test_doc_reference_gate.py` 会扫它，且那门对**路径形态**没有文件级豁免。
#: 故前缀与后缀分开拼（同该门 `_FAKE_DOC` 的手法），源码里因此没有悬空路径字面量。
_RETIRED_PREFIX = "yiban/"
_RETIRED_SUFFIXES = (
    "clock.py",
    "status.py",
    "window.py",
    "masking.py",
    "config_loader.py",
    "cred_state.py",
    "state_gc.py",
    "infra/env_io.py",
    "infra/account_crypto.py",
    "engine/hrw.py",
    "engine/round.py",
    "engine/runner.py",
    "engine/probe.py",
    "engine/alerts.py",
    "engine/state_io.py",
    "engine/workers.py",
    "cli.py",
    "mail/__init__.py",
    "notify/__init__.py",
    "store/__init__.py",
)
RETIRED_SHELL_PATHS = tuple(_RETIRED_PREFIX + s for s in _RETIRED_SUFFIXES)

#: 职责就是写出旧坐标的文件（禁字面量表／夹具样本／策略枚举）。逐条给理由，
#: 由 `test_exempt_files_are_anchored` 钉住：文件须仍在，且须仍写出旧坐标。
EXEMPT_FILES = {
    "tests/test_zero_old_coordinates.py":
        "本门自身：`OLD_MODULES`／`RETIRED_SHELL_PATHS` 是禁字面量，夹具样本自证有牙",
    "tests/test_deploy_entry_imports.py":
        "旧坐标门族：夹具样本与禁字面量表按职责必须写出旧坐标",
    "tests/test_t5_t7_relocation.py":
        "T5／T7 旧坐标门：`OLD_MODULES` 与 `SHELL_RELS` 是禁字面量与豁免锚点",
    "tests/test_t6_relocation.py":
        "T6 旧坐标门：`OLD_MODULES` 与 `SHELL_REL` 是禁字面量与豁免锚点",
    "tests/test_infra_layer.py":
        "层名策略表：`yiban.store` 等是层名枚举，改字面量会改判定语义",
    "tests/test_provenance_guard.py":
        "协议层禁用前缀表：永久路径枚举，改字面量会改判定语义",
    "tests/test_import_linter_gate.py":
        "契约元测试：迁移期坐标枚举，改字面量会改判定语义",
}

#: **跨镜像旧坐标桥**（工单 e0ro，2026-10-20）：`scripts/deploy_gate.sh` 的灌数代码在
#: v0.6.2 旧镜像里执行，旧镜像只有 `yiban.store`。豁免按**逐字行**给，不按文件：
#: 整文件豁免会让该脚本其余位置也能回退旧坐标。与 `tests/test_t6_relocation.py` 的
#: `LEGACY_BRIDGE_LINES` 是同一条桥——两门各扫一面，故各登记一次（内容逐字相同）。
LEGACY_BRIDGE_LINES = {
    "scripts/deploy_gate.sh": (
        "old) printf 'yiban.store\\n' ;;",
    ),
}

#: 日志通道名：不是坐标，是**运维可见的日志出口名**。改名即改语义，故保留。
#: 只在 `getLogger`／`assertLogs`／`assertNoLogs` 的**实参位**豁免。
LOGGER_CHANNELS = frozenset((
    "yiban.window",
    "yiban.config_loader",
    "yiban.cred_state",
    "yiban.engine.round",
    "yiban.store." + "accounts",
    "yiban.store." + "audit_chain",
    "yiban.store." + "claims",
    "yiban.store." + "cleanup",
    "yiban.store." + "clock_meta",
    "yiban.store." + "events",
    "yiban.store." + "migrations",
    "yiban.store." + "queue_store",
    "yiban.store." + "run_events",
    "yiban.store." + "session_cache",
    "yiban.store." + "time_prefs",
    "yiban.store." + "tracking",
    "yiban.store." + "users",
    "yiban.store." + "verify_jobs",
))
#: 日志通道名的**汇聚点**：这些函数的实参位（含 `name=`／`logger=`／`channel=` 关键字）
#: 出现通道名即豁免。`_format` 是本仓日志测试里构造 `LogRecord` 的助手
#: （`tests/test_log_masking_formatter.py`），它把通道名传给 `LogRecord`，同为通道名用法。
#: 判据只认这些**明确的汇聚点**，不认"任何调用的实参"——否则
#: `mock.patch("yiban.store.session_cache")` 这类**真读者**会被静默放过。
LOGGER_SINKS = frozenset((
    "getLogger", "assertLogs", "assertNoLogs", "LogRecord", "addHandler",
    "_format",
))
#: 汇聚点上作为通道名传递的关键字参数名。
LOGGER_KWARGS = frozenset(("name", "logger", "channel"))

#: 面：七棵子树 ＋ 根文件。每一条都必须是**现存**目录／文件（见 `face_problems`）。
FACE_DIRS = ("yiban", "web", "scripts", "docker", "deploy", "tests", "docs/dev")
FACE_FILES = ("run.sh", "run_probe.sh", "pyproject.toml", ".env.example",
              "README.md", "CHANGELOG.md")
#: 面内不归本批改的子树（前端产物归用户）。
SKIP_REL_PREFIX = ("web/static/",)
SKIP_DIRS = frozenset(("node_modules", "__pycache__", ".venv", "venv",
                       "dist", "build", "work", ".git"))
#: 面的文件扩展名（读者面的载体：Python／shell／容器／配置／契约文档）。
FACE_EXTS = (".py", ".sh", ".bash", ".md", ".toml", ".conf", ".example",
             ".json", ".tsv", ".yml", ".yaml")

#: 扫描面下限（AGENTS §30：扫空不得当绿）。
MIN_FACE_FILES = 400

#: 旧坐标的字面量判据：模块名前后都不许紧跟标识符字符。
#: 故 `yiban.mail` 命中，`yiban.mailer` 不命中；`yiban.cli` 命中，`yiban.client` 不命中。
_OLD_LITERAL = {m: re.compile(r"(?<![\w.])" + re.escape(m) + r"(?![\w])")
                for m in OLD_MODULES}

#: 文本里的"父包别名"形态：`from yiban.infra import env_io` / `from yiban import mail`。
_ALIAS_FROM = re.compile(r"\bfrom\s+(yiban[A-Za-z_.]*)\s+import\s+([^\n#;]*)")


def _read(path):
    """该文件的源码文本。"""
    with io.open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


@functools.lru_cache(maxsize=None)
def _ast_of(path):
    """该文件的 AST（按路径缓存：多个用例各扫一遍面，重复解析没有必要）。"""
    return ast.parse(_read(path))


def old_of(dotted):
    """`dotted` 是哪个退役坐标（或其子模块）；不是则返回 `None`。"""
    for m in OLD_MODULES:
        if dotted == m or dotted.startswith(m + "."):
            return m
    return None


def _docstring_const_ids(tree):
    """模块／类／函数首个语句若是字符串常量，记下它的对象 id（docstring 不算读者）。"""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _logger_arg_ids(tree):
    """作为日志通道名使用的字符串常量节点 id（汇聚点实参位豁免）。"""
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if fname not in LOGGER_SINKS:
            continue
        cand = list(node.args) + [kw.value for kw in node.keywords
                                  if kw.arg in LOGGER_KWARGS]
        for a in cand:
            if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                    and a.value in LOGGER_CHANNELS:
                ids.add(id(a))
    return ids


def _bound_prefixes(tree):
    """标识符 → 它绑定的点号前缀（由 import 语句得出）。

    覆盖四种写法：`import yiban.infra.env_io`（绑顶层名 `yiban`）、
    `import yiban.infra.env_io as E`（绑 `E`）、`from yiban import mail`（绑 `mail`
    → `yiban.mail`）、`from yiban.infra import env_io`（绑 `env_io`
    → `yiban.infra.env_io`）。
    """
    pref = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    pref[a.asname] = a.name
                else:
                    pref[a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.level:      # 包内相对导入：不是旧坐标
                continue
            mod = node.module or ""
            for a in node.names:
                if a.name == "*":
                    continue
                pref[a.asname or a.name] = (mod + "." + a.name) if mod else a.name
    return pref


def _dotted_of(node, pref):
    """把 Name／Attribute 节点展开成点号路径（属性链按绑定前缀拼接）。"""
    if isinstance(node, ast.Name):
        return pref.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted_of(node.value, pref)
        return (base + "." + node.attr) if base else None
    return None


def alias_names(text):
    """文本里 `from <父包> import <旧子名>` 命中的旧坐标列表（按项判名）。

    `from yiban.infra import env_io` → `["yiban.infra.env_io"]`。
    取首个标识符，不剥字符集：shell 的引号与转义引号都要能带出短名。
    """
    out = []
    for m in _ALIAS_FROM.finditer(text):
        parent = m.group(1)
        for item in m.group(2).split(","):
            token = re.search(r"[A-Za-z_]\w*", item)
            if not token:
                continue
            full = parent + "." + token.group(0)
            if old_of(full):
                out.append(full)
    return out


def old_coordinate_hits(path):
    """该文件引用退役坐标的位置：`[(行号, 说明)]` 列表（升序去重）。

    Python：① import 语句（四种写法，含父包别名与属性链）；
    ② 非 docstring 字符串常量含旧坐标（覆盖 `importlib.import_module("yiban.store.db")`、
    打桩目标、`python -m yiban.cli` 这类代码串）。
    日志通道名在 `getLogger`／`assertLogs`／`assertNoLogs` 实参位豁免。
    非 Python 文本件：整行文本含旧坐标字面量或父包别名形态。
    注释与 docstring 不算（散文指针与历史叙述不误红）。
    """
    text = _read(path)
    if not path.endswith(".py"):
        rel = os.path.relpath(path, BASE).replace("\\", "/")
        bridge = LEGACY_BRIDGE_LINES.get(rel, ())
        hits = []
        for i, line in enumerate(text.splitlines(), 1):
            if line.strip() in bridge:
                continue
            if any(_OLD_LITERAL[m].search(line) for m in OLD_MODULES):
                hits.append((i, "文本件含旧坐标字面量"))
            elif alias_names(line):
                hits.append((i, "文本件含 from <父包> import <旧子名>"))
        return sorted(set(hits))
    tree = _ast_of(path)
    docs = _docstring_const_ids(tree)
    logger_ids = _logger_arg_ids(tree)
    pref = _bound_prefixes(tree)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if old_of(a.name):
                    hits.append((node.lineno, "import 旧坐标 " + a.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            mod = node.module or ""
            if old_of(mod):
                hits.append((node.lineno, "from 旧坐标 import ... " + mod))
            for a in node.names:
                if a.name == "*":
                    continue
                full = (mod + "." + a.name) if mod else a.name
                if old_of(full):
                    hits.append((node.lineno, "from 父包 import 旧子名 " + full))
        elif isinstance(node, ast.Attribute):
            dotted = _dotted_of(node, pref)
            if dotted and old_of(dotted):
                hits.append((node.lineno, "属性链 " + dotted))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docs and id(node) not in logger_ids:
            if any(_OLD_LITERAL[m].search(node.value) for m in OLD_MODULES):
                hits.append((node.lineno, "非 docstring 字符串 " + node.value[:60]))
            elif alias_names(node.value):
                hits.append((node.lineno, "非 docstring 字符串含父包别名 "
                             + node.value[:60]))
    return sorted(set(hits))


def _tracked_files(root):
    """`git ls-files` 跟踪集（相对 root）；非 git 树返回 `None`。

    **为什么面要取跟踪集**：本门守的是「入库文本不得引用旧坐标」。`docs/dev/` 下
    既有入库契约，也有本地过程稿（`docs/` 整体 gitignore）。按文件系统走会把
    本机未入库的过程稿算进面 ⇒ 本机恒红、干净检出恒绿，两边结论不一致。跟踪集
    消除这个口径差；`subprocess` 失败时退回文件系统面（元测试的合成树没有 git）。
    """
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                             capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return {p for p in out.stdout.decode("utf-8", "replace").split(chr(0)) if p}


def own_face_files(root):
    """自有面的扫描文件清单（相对路径，升序）。

    七棵子树按 `FACE_DIRS` 走；根文件按 `FACE_FILES` 逐个取（存在的才算）。
    git 树只保留**跟踪集**里的文件（见 `_tracked_files`）。
    """
    tracked = _tracked_files(root)
    out = []
    for d in FACE_DIRS:
        for dirpath, dirs, files in os.walk(os.path.join(root, d)):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            for name in sorted(files):
                if not name.endswith(FACE_EXTS):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name),
                                      root).replace("\\", "/")
                if rel.startswith(SKIP_REL_PREFIX):
                    continue
                out.append(rel)
    for name in FACE_FILES:
        if os.path.exists(os.path.join(root, name)):
            out.append(name)
    if tracked is not None:
        out = [rel for rel in out if rel in tracked]
    return sorted(set(out))


def face_problems(root, files):
    """面的登记必须真实存在（`os.walk` 对不存在目录静默返回空 ⇒ 静默扫空）。

    145-D 审查抓过这个形状：把扫描根写成不存在的目录，门会"绿"但什么也没扫。
    故逐条钉：登记的每个面目录／面文件都必须存在，且文件数有下限。
    """
    problems = []
    for d in FACE_DIRS:
        if not os.path.isdir(os.path.join(root, d)):
            problems.append("面目录不存在：%s（`os.walk` 会静默返回空 ⇒ 静默扫空）" % d)
    for name in FACE_FILES:
        if not os.path.isfile(os.path.join(root, name)):
            problems.append("面文件不存在：%s" % name)
    if len(files) < MIN_FACE_FILES:
        problems.append("扫描面过小（%d 个文件，下限 %d）——扫描根写错或树被移走"
                        "（扫空不得当绿）" % (len(files), MIN_FACE_FILES))
    return problems


def _is_exempt(rel):
    """该文件是否按职责必须写出旧坐标。"""
    return rel in EXEMPT_FILES


class RetiredShellAbsenceTest(unittest.TestCase):
    """① 20 个退役壳本体在库内均不可见。"""

    def test_retired_shell_paths_are_gone(self):
        alive = [p for p in RETIRED_SHELL_PATHS
                 if os.path.exists(os.path.join(BASE, p))]
        self.assertEqual(
            alive, [],
            "退役壳本体仍在（`145-E` 应删净）：\n" + "\n".join(alive))
        self.assertEqual(len(RETIRED_SHELL_PATHS), 20,
                         "退役壳清单条数变了：本门按 20 条立，清单被误改即红")

    def test_the_database_facade_is_not_deleted_by_mistake(self):
        """`yiban/adapters/persistence/db.py` 也含 `sys.modules[__name__]`，但它是
        新实现的**门面**（`_StateForwardingModule`），不是旧坐标壳。误删会全盘断。"""
        self.assertTrue(
            os.path.isfile(os.path.join(BASE, "yiban/adapters/persistence/db.py")),
            "yiban/adapters/persistence/db.py 不在了——它是新实现门面，不是旧坐标壳")
        self.assertFalse(
            os.path.isfile(os.path.join(BASE, "yiban/store/db.py")),
            "yiban/store/db.py 又出现了：旧坐标包不得复活")


class ZeroOldCoordinateReaderTest(unittest.TestCase):
    """② 自有面零旧坐标读者；③ 豁免逐条有锚点。"""

    def test_own_face_has_no_old_coordinate_reader(self):
        files = own_face_files(BASE)
        problems = face_problems(BASE, files)
        bad = []
        for rel in files:
            if _is_exempt(rel):
                continue
            try:
                hits = old_coordinate_hits(os.path.join(BASE, rel))
            except SyntaxError as exc:
                bad.append("%s: 语法错误，判据无法判定（%s）" % (rel, exc))
                continue
            for line, why in hits:
                bad.append("%s:%d: %s" % (rel, line, why))
        self.assertEqual(
            problems + bad, [],
            "自有面仍以退役坐标作 import 路径、属性链或运行期字符串"
            "（壳退役后 import 会断、串内坐标会 ModuleNotFoundError）：\n"
            + "\n".join(problems + bad))

    def test_exempt_files_are_anchored(self):
        """豁免不腐烂：登记的文件须仍在，且须**仍写出**旧坐标。"""
        for rel, why in sorted(EXEMPT_FILES.items()):
            with self.subTest(rel=rel):
                path = os.path.join(BASE, rel)
                self.assertTrue(os.path.isfile(path),
                                "豁免文件 %s 不在了——豁免失去对象，须同批删掉（理由：%s）"
                                % (rel, why))
                self.assertTrue(old_coordinate_hits(path),
                                "%s 已不再写出旧坐标——豁免失去理由，须从豁免面移除"
                                % rel)

    def test_legacy_image_bridge_is_anchored_and_narrow(self):
        """e0ro 跨镜像桥：登记的逐字行仍在，且该文件桥以外零旧坐标读者。"""
        for rel, lines in sorted(LEGACY_BRIDGE_LINES.items()):
            with self.subTest(rel=rel):
                path = os.path.join(BASE, rel)
                self.assertTrue(os.path.isfile(path), "桥文件 %s 不在了" % rel)
                stripped = [ln.strip() for ln in _read(path).splitlines()]
                for expected in lines:
                    self.assertIn(expected, stripped,
                                  "%s 里登记的桥行已不存在：豁免失去理由" % rel)
                self.assertEqual(
                    old_coordinate_hits(path), [],
                    "%s 在桥以外仍有旧坐标读者（豁免只该覆盖登记行）：%r"
                    % (rel, old_coordinate_hits(path)))

    def test_logger_channel_exemption_is_anchored(self):
        """日志通道名豁免不腐烂：登记的名字须仍是实现里的 `getLogger` 实参。"""
        want = {"yiban.window": "yiban/core/window.py",
                "yiban.config_loader": "yiban/adapters/config/loader.py",
                "yiban.cred_state": "yiban/adapters/statefiles/cred_state.py"}
        for channel, rel in sorted(want.items()):
            with self.subTest(channel=channel):
                tree = ast.parse(_read(os.path.join(BASE, rel)))
                found = any(
                    isinstance(n, ast.Call)
                    and getattr(n.func, "attr", None) == "getLogger"
                    and any(isinstance(a, ast.Constant) and a.value == channel
                            for a in n.args)
                    for n in ast.walk(tree))
                self.assertTrue(
                    found,
                    "%s 里不再有日志通道名 %r——本门的日志名豁免已腐烂，"
                    "须同批删掉该豁免" % (rel, channel))
        self.assertEqual(
            len(LOGGER_CHANNELS), 18,
            "日志通道名清单条数变了：本门按 18 条立，清单被误改即红")


class GateHasTeethTest(unittest.TestCase):
    """④ 门自身有牙：夹具样本必判红；干净样本不得误红；扫描面有下限。"""

    def _hits_of_source(self, src):
        """把源码写进临时文件后走真判据（复用被测路径，不另写第二份逻辑）。"""
        fd, path = tempfile.mkstemp(suffix=".py", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(src)
            return old_coordinate_hits(path)
        finally:
            os.unlink(path)

    def test_fixtures_are_detected(self):
        cases = (
            "import yiban.clock",
            "import yiban.store.db",
            "from yiban.clock import today",
            "from yiban import cli",
            "from yiban.infra import env_io",
            "from yiban.engine import workers",
            "import yiban.notify as N\nN.send()",
            "import yiban\nx = yiban.mail.get_config()",
            'import importlib\nimportlib.import_module("yiban.store.db")',
            'TARGET = "yiban.cred_state.path"',
            'CODE = "from yiban.infra import env_io; env_io.x()"',
            'mock.patch("yiban.cli.main")',
        )
        for src in cases:
            with self.subTest(src=src):
                self.assertTrue(self._hits_of_source(src),
                                "夹具未被判红：本门没有牙 —— %r" % (src,))

    def test_clean_fixtures_are_not_flagged(self):
        cases = (
            "from yiban.adapters.config import env_io",
            "from yiban.adapters.statefiles import cred_state",
            "import yiban.core.clock as c",
            "from yiban.entry import cli",
            'import importlib\nimportlib.import_module("yiban.adapters.persistence.db")',
            "# yiban.mail 只是散文里的旧坐标，不是读者",
            '"""docstring 里的 yiban.notify 不算读者。"""',
            'logger = logging.getLogger("yiban.cred_state")',
            'with self.assertLogs("yiban.store.session_cache", level="INFO"):\n    pass',
            "from yiban.mailer_thing import x",      # 同前缀他模块，不得误红
            "yiban.client = 1",                      # `yiban.cli` 的同前缀他模块
            "yiban.storehouse = 1",
        )
        for src in cases:
            with self.subTest(src=src):
                self.assertEqual(self._hits_of_source(src), [],
                                 "干净样本被误红：%r" % (src,))

    def test_alias_scanner_has_teeth(self):
        """父包别名判据：`from <父包> import <旧子名>` 必须命中。"""
        self.assertTrue(alias_names("from yiban.infra import env_io"))
        self.assertTrue(alias_names("from yiban import store"))
        self.assertTrue(alias_names('from yiban.infra import account_crypto, env_io'))
        self.assertEqual(alias_names("from yiban.infra import locks, private_json"), [])
        self.assertEqual(alias_names("from yiban.adapters.config import env_io"), [])

    def test_scan_surface_is_alive(self):
        """防废门：扫描面必须真的走到文件；面目录必须都存在。"""
        files = own_face_files(BASE)
        self.assertEqual(face_problems(BASE, files), [],
                         "面登记或扫描面下限不成立（扫空不得当绿）")
        self.assertGreaterEqual(len(files), MIN_FACE_FILES,
                                "扫描面过小（%d）" % len(files))

    def test_the_gate_scans_itself(self):
        """本门不得把自己从面里剔掉（§29：门看的东西 ＝ 实际改的东西吗）。"""
        rel = os.path.relpath(os.path.abspath(__file__),
                              BASE).replace(os.sep, "/")
        self.assertIn(rel, own_face_files(BASE),
                      "扫描面不含本门文件——剔除本文件会让门静默变窄")
        self.assertIn(rel, EXEMPT_FILES,
                      "本门写出禁字面量，必须显式登记豁免（不许静默）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
