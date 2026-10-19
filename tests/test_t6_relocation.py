# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""T6 族旧坐标迁移后的守卫（仓库结构整理批 2）。

标签：M · 架构分层与基础设施
覆盖：本批把数据访问层（T6 族）的读者改指新坐标。旧坐标留转发壳，壳由 `145-E` 删除。
    T6 族＝`yiban.store`，含十七个子模块（`accounts`／`audit_chain`／`claims`／
    `cleanup`／`clock_meta`／`connection`／`db`／`events`／`migrations`／
    `purge_guard`／`queue_store`／`run_events`／`session_cache`／`time_prefs`／
    `tracking`／`users`／`verify_jobs`）。
    新坐标＝`yiban.adapters.persistence`（同名平移）。
    本文件钉五条不变式：
    ① **自有面零旧坐标读者**：面＝`yiban/**`、`web/**`（非 `web/static/**`）、
       `scripts/**`、`docker/**`、`tests/**`、`run.sh`。旧坐标不得再作 import 路径
       （四种写法：`import X`、`from X import Y`、`from <父包> import <子名>`、
       `import yiban as A` 后的属性访问）、属性链（`yiban.store.db.f(...)`）、
       或**运行期字符串**（`importlib.import_module("yiban.store.db")`、
       `mock.patch("yiban.store.db.get_conn")`、`python -c "from yiban.store import db"`）。
    ② **打桩面单列**：`mock.patch("<旧坐标>.…")` 在转发壳下能成功但不控被测路径
       （静默失效）。本族实测有打桩点，本条防复发。
    ③ **新坐标在位且被消费方指向**：新坐标可按模块导入，`__file__` 在新路径下；
       本批迁移过的生产读者各自引到新坐标。
    ④ **门自身有牙**：夹具样本必须被判红；干净样本不得判红；扫描面过小即红
       （扫空不得当绿）。
    ⑤ **豁免有锚点**：日志通道名与策略表裸串的豁免各自钉住，豁免腐烂时变红。

**为什么判据必须同时扫 import 语句与运行期字符串面**：静态依赖图（import-linter）
只看 `import` 语句。`importlib.import_module("yiban.store.db")` 这类**字符串坐标**
不在它的判定面内。壳删除后串内坐标当场 `ModuleNotFoundError`。故本门两面包围。
**为什么 import 判据要认"父包别名"形态**：`from yiban.store import db` 里没有
`yiban.store.db` 这个连续字面量。只按点号字面量扫会漏掉它。

**为什么本族打桩面最危险**：壳把十七个子模块登记进 `sys.modules` 并转发属性，
故 `mock.patch("yiban.store.db.get_conn")` 迁移后**仍然生效**——打桩命中壳转发的
同一对象。于是"改了一半"会**静默通过**：门若只扫 import，打桩旧坐标全绿。故不变式②
单列一条，且突变验证必须打桩面留一处不改就判红。

**口径（改动面怎么数出来的）**：某文件命中＝该文件出现上述 import 写法之一、属性链、
或运行期字符串。**注释与 docstring 里的旧坐标字样不计入**（与 T2／T3／T5T7 同口径：
散文指针不误红，历史叙述不误红）。实测（底本 `origin/develop`，本批开树前）：
自有面命中 69 个真 import 文件 ＋ 48 个含字符串命中的文件，去重后 99 个文件；
扣除豁免面后本批改动 81 个文件。

**豁免（按文件或按实参位给，不是跳过；未登记的文件命中即红并点名 `文件:行`）**：
  ① 本文件自身——`OLD_MODULE` 是禁字面量，夹具样本用于自证本门有牙。
  ② **日志通道名豁免（不是坐标）**：十四处 `logging.getLogger("yiban.store.<名>")`
     住在 `yiban/adapters/persistence/*.py`，`tests/` 里另有 `assertLogs` 用同名。
     改名会改动**运维可见的日志出口名**，属语义变更 ⇒ 本批不改。
     T3／T4／T5T7 先例同此（日志名保留）。判据是"实参位**逐字等于**通道名"，
     故 `mock.patch("yiban.store.db")`（同为逐字等于）不因此豁免——它在打桩位。
  ⑤ **策略表裸串豁免（不是读者）**：三个文件把 `"yiban.store"` 当作**层名／坐标名
     枚举项**写进策略表（禁用前缀表、陈旧坐标黑名单）。它们是**判定**的一部分，
     不是"读旧坐标的地方"；改字面量会改变判定覆盖面 ⇒ 本批不改（`145-E` 一并处置）。
     判据是"字符串逐字等于 `yiban.store`"且**不是任何调用的实参**（实参位即坐标读取）。
     三个文件：`tests/test_import_linter_gate.py`、`tests/test_infra_layer.py`、
     `tests/test_provenance_guard.py`。注意 `tests/test_provenance_guard.py` 里
     `from yiban.store import accounts` 是**真读者**，本批已改——故豁免只覆盖裸串，
     不覆盖该文件。
对应实现：`yiban/adapters/persistence/` 整包（批 B 已搬）。
关键断言：① ② ④ ⑤ 读源码文本与 `ast`；③ 用 `importlib` 判模块在位。
依赖：只读仓库文件、导入包、`git ls-files`；不联网、不建库、不发邮件、不发通知。
"""
import ast
import functools
import importlib
import io
import os
import re
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 本族旧坐标（数据访问层）。
OLD_MODULE = "yiban.store"
#: 本族新坐标（同名平移）。
NEW_MODULE = "yiban.adapters.persistence"

#: 十七个子模块（与旧坐标包登记的 `_SUBMODULES` 同名；旧壳已由 `145-E` 删净）。
STORE_SUBMODULES = (
    "accounts", "audit_chain", "claims", "cleanup", "clock_meta", "connection",
    "db", "events", "migrations", "purge_guard", "queue_store", "run_events",
    "session_cache", "time_prefs", "tracking", "users", "verify_jobs",
)

#: 旧坐标 → 新坐标（包级；子模块同名，故映射由 `NEW_MODULE` 加短名推出）。
NEW_OF = {OLD_MODULE: NEW_MODULE}
NEW_OF.update({OLD_MODULE + "." + s: NEW_MODULE + "." + s for s in STORE_SUBMODULES})

#: 日志通道名（不是坐标）：见模块头④。实参位逐字等于这些值即豁免。
LOGGER_CHANNELS = tuple(OLD_MODULE + "." + s for s in (
    "accounts", "audit_chain", "claims", "cleanup", "clock_meta", "events",
    "migrations", "queue_store", "run_events", "session_cache", "time_prefs",
    "tracking", "users", "verify_jobs",
))
#: 以日志通道名为实参的调用名。`_format` 是 `tests/test_log_masking_formatter.py`
#: 的记录格式化助手，它的 `name` 形参就是日志通道名（锚点见豁免锚定用例）。
LOGGER_CALLS = frozenset(("getLogger", "assertLogs", "assertNoLogs", "Logger", "_format"))

#: 策略表裸串（不是读者）：见模块头⑤。
POLICY_TOKEN_FILES = frozenset((
    "tests/test_import_linter_gate.py",
    "tests/test_infra_layer.py",
    "tests/test_provenance_guard.py",
))
#: 策略表里当作层名／坐标名枚举的那个裸串。
POLICY_TOKEN_VALUE = OLD_MODULE

#: 职责就是夹具自证本门有牙的文件：必须写出旧坐标。
#: `145-E` 删了旧壳与批 B 守卫（`tests/test_batch_b_relocation.py`），故它从本表移除。
#: `tests/test_zero_old_coordinates.py` 是 `145-E` 立的**全仓总门**，它的
#: `OLD_MODULES` 是 20 条禁字面量（含 `yiban.store`），故必须登记。
FACE_EXEMPT = frozenset((
    "tests/test_t6_relocation.py",
    "tests/test_zero_old_coordinates.py",
))

#: 自有面目录（不含面外 `frontend/**` 与 `web/static/**`：归用户独立完成）。
FACE_DIRS = ("yiban", "web", "scripts", "docker", "tests")
FACE_SHELL = ("run.sh",)
SKIP_DIRS = frozenset(("node_modules", "__pycache__", ".venv", "venv", "dist", "build"))
#: 面内不归本批改的子树（前端产物）。
SKIP_REL_PREFIX = ("web/static/",)

#: 生产读者 → 迁移后必须引到的新坐标**父包**（不变式③：消费方指向新坐标）。
#: 判父包而不是全点号坐标：`from yiban.adapters.persistence import db` 这种形态里
#: 没有连续字面量 `yiban.adapters.persistence.db`。旧坐标清零由不变式①另钉。
PRODUCTION_CONSUMERS = (
    ("scripts/backup_sentinel.py", NEW_MODULE),
    ("scripts/db.py", NEW_MODULE),
    ("scripts/deploy_gate.sh", NEW_MODULE),
    ("scripts/drill_multi_exec.py", NEW_MODULE),
    ("scripts/generate_demo_data.py", NEW_MODULE),
    ("scripts/ledger_check.py", NEW_MODULE),
    ("web/routes/audit_api.py", NEW_MODULE),
    ("web/routes/run_events_api.py", NEW_MODULE),
    ("web/routes/stream_api.py", NEW_MODULE),
    ("web/security.py", NEW_MODULE),
    ("web/services/capacity.py", NEW_MODULE),
    ("web/services/channel_health.py", NEW_MODULE),
    ("web/services/executor_env.py", NEW_MODULE),
    ("web/services/measure.py", NEW_MODULE),
    ("web/services/notify_mail.py", NEW_MODULE),
    ("web/services/run_stream.py", NEW_MODULE),
)

#: 旧坐标的字面量判据：模块名后不得紧跟标识符字符
#: （`yiban.store` 命中；`yiban.storefront` 这类同前缀他模块不命中）。
_OLD_LITERAL = re.compile(r"(?<![\w.])" + re.escape(OLD_MODULE) + r"(?![\w])")

#: 文本里的"父包别名"形态：`from yiban import store` / `from yiban.store import db`。
#: 取首个标识符，不剥字符集（shell 的引号、反引号、`as` 都要能带出短名）。
_ALIAS_FROM = re.compile(r"\bfrom\s+(yiban[A-Za-z_.]*)\s+import\s+([^\n#;]*)")

#: `.md` 里的**可执行**片段形态（散文引用不在此列）。
#: 别名形态（`from yiban import store`）复用 `_alias_names`，不另写松正则。
_MD_SNIPPETS = (
    re.compile(r"from\s+" + re.escape(OLD_MODULE) + r"[\w.]*\s+import\b"),
    re.compile(r"import\s+" + re.escape(OLD_MODULE) + r"[\w.]*\b"),
)


@functools.lru_cache(maxsize=None)
def _text_of(path):
    """该文件的源码文本（按路径缓存）。"""
    with io.open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _read(path):
    return _text_of(path)


def _rel_of(path):
    """相对仓库根的路径（`/` 分隔）；不在库内则返回 `None`。"""
    try:
        rel = os.path.relpath(path, BASE)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    return rel.replace("\\", "/")


def _is_exempt_file(rel):
    """该文件是否整体豁免（本门自身）。

    `145-E` 之后只剩本门自身：旧壳已删，`SHELL_REL` 那条豁免随之移除。
    """
    if rel is None:
        return False
    return rel in FACE_EXEMPT


def _is_old(dotted):
    """`dotted` 是本族旧坐标（或其子模块）吗。"""
    return dotted == OLD_MODULE or dotted.startswith(OLD_MODULE + ".")


def _docstring_const_ids(tree):
    """模块/类/函数首个语句若是字符串常量，记下它的对象 id（docstring 不算读者）。"""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _bound_prefixes(tree):
    """标识符 → 它绑定的点号前缀（由 import 语句得出）。

    覆盖四种写法：`import yiban.store.db`（绑顶层名 `yiban`）、
    `import yiban.store.db as S`（绑 `S`）、`from yiban import store`（绑 `store` →
    `yiban.store`）、`from yiban.store import db`（绑 `db` → `yiban.store.db`）。
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
    """把 Name/Attribute 节点展开成点号路径（属性链按绑定前缀拼接）。"""
    if isinstance(node, ast.Name):
        return pref.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted_of(node.value, pref)
        return (base + "." + node.attr) if base else None
    return None


def _call_arg_ids(tree):
    """作为**任何调用实参**出现的节点 id（实参位即"读者实际可达的坐标"）。"""
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for a in list(node.args) + [k.value for k in node.keywords]:
            ids.add(id(a))
    return ids


def _logger_arg_ids(tree):
    """作为日志通道名实参出现的字符串常量节点 id（`getLogger("yiban.store.db")` 等）。

    判据是"实参位**逐字等于**通道名"：`mock.patch("yiban.store.db")` 也逐字等于，
    但它的调用名不在 `LOGGER_CALLS` 里，故不豁免——这正是打桩面必须保持可见的原因。
    """
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if fname not in LOGGER_CALLS:
            continue
        for a in list(node.args) + [k.value for k in node.keywords]:
            if isinstance(a, ast.Constant) and a.value in LOGGER_CHANNELS:
                ids.add(id(a))
    return ids


def _alias_names(text):
    """文本里 `from <父包> import <子名>` 命中的旧子名列表（按项判名）。

    `from yiban import store` → `["yiban.store"]`；
    `from yiban.store import db, queue_store` → `["yiban.store.db", "yiban.store.queue_store"]`。
    取首个标识符，不剥字符集：shell 的 `python3 -c "from yiban.store import db"`
    与转义引号都会把收尾引号带进列表项，剥字符集会**静默漏检**。
    """
    out = []
    for m in _ALIAS_FROM.finditer(text):
        parent = m.group(1)
        for item in m.group(2).split(","):
            token = re.search(r"[A-Za-z_]\w*", item)
            if not token:
                continue
            full = parent + "." + token.group(0)
            if _is_old(full):
                out.append(full)
    return out


@functools.lru_cache(maxsize=None)
def _ast_of(path):
    """该文件的 AST（按路径缓存）。"""
    return ast.parse(_read(path))


def _scan(path):
    """返回该文件对旧坐标的全部命中：`(行号, 类别, 说明)` 列表。

    类别 ∈ `import`（import 语句）／`attr`（属性链）／`str`（运行期字符串）／
    `logger`（日志通道名实参，豁免）／`policy`（策略表裸串，豁免）。
    注释与 docstring 里的旧坐标字样不计入（任务口径：散文指针不误红）。
    """
    rel = _rel_of(path)
    text = _read(path)
    if path.endswith((".sh", ".bash")) or os.path.basename(path).startswith("Dockerfile"):
        hits = []
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if _OLD_LITERAL.search(s):
                hits.append((i, "str", "shell 非注释行含旧坐标字面量 " + s[:70]))
            elif _alias_names(s):
                hits.append((i, "str", "shell 非注释行含父包别名 " + s[:70]))
        return sorted(set(hits))
    tree = _ast_of(path)
    docs = _docstring_const_ids(tree)
    logger_ids = _logger_arg_ids(tree)
    call_args = _call_arg_ids(tree)
    pref = _bound_prefixes(tree)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if _is_old(a.name):
                    hits.append((node.lineno, "import", "import " + a.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            mod = node.module or ""
            if _is_old(mod):
                hits.append((node.lineno, "import", "from " + mod + " import ..."))
            for a in node.names:
                if a.name == "*":
                    continue
                full = (mod + "." + a.name) if mod else a.name
                if _is_old(full):
                    hits.append((node.lineno, "import", "from 父包 import " + full))
        elif isinstance(node, ast.Attribute):
            dotted = _dotted_of(node, pref)
            if dotted and _is_old(dotted):
                hits.append((node.lineno, "attr", "属性链 " + dotted))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docs:
            if id(node) in logger_ids:
                hits.append((node.lineno, "logger", "日志通道名 " + node.value))
                continue
            if rel in POLICY_TOKEN_FILES and node.value == POLICY_TOKEN_VALUE \
                    and id(node) not in call_args:
                hits.append((node.lineno, "policy", "策略表裸串 " + node.value))
                continue
            if _OLD_LITERAL.search(node.value):
                hits.append((node.lineno, "str", "非 docstring 字符串 " + node.value[:70]))
            elif _alias_names(node.value):
                hits.append((node.lineno, "str", "非 docstring 字符串含父包别名 "
                             + node.value[:70]))
    return sorted(set(hits))


def t6_old_coordinate_hits(path):
    """该文件的**读者**命中（已扣豁免）：`(行号, 说明)` 列表。"""
    rel = _rel_of(path)
    if _is_exempt_file(rel):
        return []
    return sorted({(ln, why) for ln, kind, why in _scan(path)
                   if kind in ("import", "attr", "str")})


def t6_exempt_hits(path, kind):
    """该文件按 `kind`（`logger`／`policy`）豁免掉的命中：`(行号, 说明)` 列表。"""
    rel = _rel_of(path)
    if _is_exempt_file(rel):
        return []
    return sorted({(ln, why) for ln, k, why in _scan(path) if k == kind})


def t6_own_face_files(root):
    """自有面的扫描文件清单（`.py` ＋ `.sh`／`.bash`／`Dockerfile*` ＋ `run.sh`）。"""
    out = []
    for d in FACE_DIRS:
        for dirpath, dirs, files in os.walk(os.path.join(root, d)):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            for name in sorted(files):
                if not name.endswith((".py", ".sh", ".bash")) \
                        and not name.startswith("Dockerfile"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), root).replace("\\", "/")
                if rel.startswith(SKIP_REL_PREFIX):
                    continue
                out.append(rel)
    for name in FACE_SHELL:
        if os.path.exists(os.path.join(root, name)):
            out.append(name)
    return sorted(set(out))


def _tracked_markdown(root):
    """入库的 `.md` 清单（`git ls-files`）。取不到时返回空表（调用方判红）。

    WSL 里跑 Windows git worktree 时，`.git` 文件里的 gitdir 是 Windows 绝对路径
    （`D:/...`），WSL git 无法解析——换算成 `/mnt/<盘>/...` 重试。
    这条与 `tests/test_doc_reference_gate.py` 的 `_ls_tracked` 同口径。
    """
    try:
        run = subprocess.run(["git", "-C", root, "ls-files", "-z", "--", "*.md"],
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
                                      "ls-files", "-z", "--", "*.md"],
                                     capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return []
    if run.returncode != 0:
        return []
    names = run.stdout.decode("utf-8", "replace").split("\0")
    return sorted(n for n in names if n and n.endswith(".md"))


class T6OwnFaceTest(unittest.TestCase):
    """① ② 自有面零旧坐标读者；打桩面零命中。"""

    def test_own_face_has_no_old_coordinate(self):
        files = t6_own_face_files(BASE)
        self.assertGreaterEqual(
            len(files), 400,
            f"自有面扫描面过小（{len(files)} 个文件）——扫描根写错或树被移走"
            "（扫空不得当绿）")
        bad = []
        for rel in files:
            try:
                hits = t6_old_coordinate_hits(os.path.join(BASE, rel))
            except SyntaxError as exc:
                bad.append(f"{rel}: 语法错误，判据无法判定（{exc}）")
                continue
            for line, why in hits:
                bad.append(f"{rel}:{line}: {why}")
        self.assertEqual(
            bad, [],
            "自有面仍以旧坐标作 import 路径、属性链或运行期字符串"
            "（壳退役后 import 会断、串内坐标会 ModuleNotFoundError）：\n"
            + "\n".join(bad))

    def test_no_patch_target_uses_old_coordinate(self):
        """打桩面单列一条：`mock.patch("<旧坐标>.…")` 是静默失效的典型形态。

        本条**不认策略表豁免**（策略表裸串若出现在打桩位就是坐标读取，不是策略）。
        """
        files = t6_own_face_files(BASE)
        self.assertGreaterEqual(len(files), 400, f"扫描面过小（{len(files)}）")
        bad = []
        for rel in files:
            if not rel.endswith(".py") or _is_exempt_file(rel):
                continue
            path = os.path.join(BASE, rel)
            text = _read(path)
            # 便宜的前置过滤：没有 `patch` / `object` 字样的文件不可能有打桩目标。
            if "patch" not in text and "object(" not in text:
                continue
            tree = _ast_of(path)
            docs = _docstring_const_ids(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if fname not in ("patch", "object"):
                    continue
                for a in list(node.args) + [k.value for k in node.keywords]:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                            and id(a) not in docs \
                            and _OLD_LITERAL.search(a.value):
                        bad.append(f"{rel}:{node.lineno}: 打桩目标 {a.value}")
        self.assertEqual(
            bad, [],
            "打桩目标仍指旧坐标——壳退役后会静默失效（打桩成功但不控被测路径）：\n"
            + "\n".join(bad))

    def test_markdown_and_shell_snippets_use_the_new_coordinate(self):
        """入库 `.md` 里的**可执行**片段必须走新坐标（散文引用不在此列）。

        为什么单列：批 1 漏了 `README.md` 的换钥 runbook（父包别名形态），
        因为面里只有 `.py`／`.sh`，`.md` 看不见。本批把 `.md` 面单独补上。
        """
        mds = _tracked_markdown(BASE)
        self.assertGreaterEqual(
            len(mds), 5,
            f"入库 `.md` 面过小（{len(mds)} 个）——`git ls-files` 失活或路径写错"
            "（扫空不得当绿）")
        bad = []
        for rel in mds:
            text = _read(os.path.join(BASE, rel))
            for i, line in enumerate(text.splitlines(), 1):
                if any(pat.search(line) for pat in _MD_SNIPPETS) or _alias_names(line):
                    bad.append(f"{rel}:{i}: {line.strip()[:90]}")
        self.assertEqual(
            bad, [],
            "入库 `.md` 里的可执行片段仍走旧坐标（壳退役后按此复制即断）：\n"
            + "\n".join(bad))


class T6NewCoordinateTest(unittest.TestCase):
    """③ 新坐标在位，且生产消费方指向它。"""

    def test_new_coordinates_are_importable(self):
        names = [NEW_MODULE] + [NEW_MODULE + "." + s for s in STORE_SUBMODULES]
        for new in names:
            with self.subTest(new=new):
                mod = importlib.import_module(new)
                self.assertTrue(os.path.isfile(mod.__file__), f"{new} 解析不到源文件")
                self.assertIn(os.path.join(*new.split(".")), mod.__file__,
                              f"{new} 的 __file__ 不在新坐标下（{mod.__file__}）")

    def test_production_consumers_point_at_the_new_coordinate(self):
        for rel, new_parent in PRODUCTION_CONSUMERS:
            with self.subTest(rel=rel):
                self.assertTrue(
                    os.path.isfile(os.path.join(BASE, rel)), f"{rel} 不在位")
                self.assertIn(new_parent, _read(os.path.join(BASE, rel)),
                              f"{rel} 没有引用新坐标父包 {new_parent}")

    def test_every_old_coordinate_has_a_new_home(self):
        """映射表自洽：十七个子模块各有新坐标，且新旧不同。"""
        self.assertEqual(len(NEW_OF), len(STORE_SUBMODULES) + 1)
        for old, new in NEW_OF.items():
            self.assertNotEqual(old, new)
            self.assertTrue(new.startswith(NEW_MODULE))


class T6ExemptionAnchorTest(unittest.TestCase):
    """⑤ 豁免不腐烂：日志通道名确为通道名；策略表裸串仍在；豁免文件仍在且仍命中。"""

    def test_logger_name_exemption_is_anchored(self):
        """`yiban.store.<名>` 确实是实现里的日志通道名（逐处）。"""
        import glob
        found = set()
        for path in sorted(glob.glob(
                os.path.join(BASE, "yiban", "adapters", "persistence", "*.py"))):
            tree = _ast_of(path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if getattr(node.func, "attr", None) != "getLogger":
                    continue
                for a in node.args:
                    if isinstance(a, ast.Constant) and a.value in LOGGER_CHANNELS:
                        found.add(a.value)
        self.assertEqual(
            found, set(LOGGER_CHANNELS),
            f"实现里的日志通道名与豁免名册不一致：多={sorted(found - set(LOGGER_CHANNELS))} "
            f"少={sorted(set(LOGGER_CHANNELS) - found)}。"
            "本门的日志名豁免已腐烂，须同批修正名册")

    def test_logger_exemption_is_not_vacuous(self):
        """豁免必须真的被用到：实现里十四处通道名各自被判成 `logger` 类命中。"""
        import glob
        seen = set()
        for path in sorted(glob.glob(
                os.path.join(BASE, "yiban", "adapters", "persistence", "*.py"))):
            for _ln, why in t6_exempt_hits(path, "logger"):
                seen.add(why.rsplit(" ", 1)[-1])
        self.assertEqual(
            seen, set(LOGGER_CHANNELS),
            "豁免面里没有这些通道名——豁免悬空，须删除或修正判据")

    def test_policy_token_exemption_is_anchored(self):
        """三个策略表文件必须**仍**把 `yiban.store` 当裸串枚举（否则豁免失去理由）。"""
        for rel in sorted(POLICY_TOKEN_FILES):
            with self.subTest(rel=rel):
                path = os.path.join(BASE, rel)
                self.assertTrue(os.path.isfile(path), f"豁免文件 {rel} 不在了")
                self.assertTrue(
                    t6_exempt_hits(path, "policy"),
                    f"{rel} 已不再把 {POLICY_TOKEN_VALUE} 当裸串枚举："
                    "豁免失去理由，须从豁免面移除")

    def test_policy_token_exemption_does_not_cover_readers(self):
        """策略表豁免**不得**掩盖读者：三件里凡 import／打桩／运行期串命中仍判红。"""
        bad = []
        for rel in sorted(POLICY_TOKEN_FILES):
            for line, why in t6_old_coordinate_hits(os.path.join(BASE, rel)):
                bad.append(f"{rel}:{line}: {why}")
        self.assertEqual(
            bad, [],
            "策略表文件里仍有旧坐标读者（豁免只该覆盖裸串枚举项）：\n" + "\n".join(bad))

    def test_exempt_files_still_exist_and_are_hit(self):
        """豁免清单不得腐烂：登记的文件必须还在，且**必须仍命中本门**。"""
        for rel in sorted(FACE_EXEMPT):
            with self.subTest(rel=rel):
                self.assertTrue(os.path.isfile(os.path.join(BASE, rel)),
                                f"豁免文件 {rel} 不在了")
                self.assertTrue(_scan(os.path.join(BASE, rel)),
                                f"{rel} 已不再含旧坐标：豁免失去理由，须从豁免面移除")


class T6GateHasTeethTest(unittest.TestCase):
    """④ 门自身有牙：夹具样本必判红；干净样本不得判红。"""

    def _hits_of_source(self, src):
        """把源码写进临时文件后走真判据（复用被测路径，不另写第二份逻辑）。"""
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".py", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(src)
            return t6_old_coordinate_hits(path)
        finally:
            os.unlink(path)

    def test_fixtures_are_detected(self):
        cases = (
            "import yiban.store",
            "import yiban.store.db",
            "from yiban import store",
            "from yiban.store import db",
            "from yiban.store.db import get_conn",
            "import yiban.store.queue_store as qs\nqs.claim_batch()",
            "from yiban import store\nstore.db.init_db()",
            'import importlib\nimportlib.import_module("yiban.store.db")',
            'TARGET = "yiban.store.db.get_conn"',
            'CODE = "from yiban.store import db; db.init_db()"',
            "import yiban\nx = yiban.store.db.get_conn()",
            'import unittest.mock as mock\nmock.patch("yiban.store.db.get_conn")',
            'mock.patch.dict("sys.modules", {"yiban.store.db": Fake})',
        )
        for src in cases:
            with self.subTest(src=src):
                self.assertTrue(self._hits_of_source(src),
                                f"夹具未被判红：本门没有牙 —— {src!r}")

    def test_clean_fixtures_are_not_flagged(self):
        cases = (
            "from yiban.adapters.persistence import db",
            "from yiban.adapters.persistence.db import get_conn",
            "import yiban.adapters.persistence.queue_store as qs",
            'import importlib\nimportlib.import_module("yiban.adapters.persistence.db")',
            "# yiban.store 只是散文里的旧坐标，不是读者",
            '"""docstring 里的 yiban.store 不算读者。"""',
            'logger = logging.getLogger("yiban.store.session_cache")',
            'with self.assertLogs("yiban.store.queue_store", level="WARNING"):\n    pass',
            'self.assertNoLogs("yiban.store.accounts", level="WARNING")',
            "from yiban.storefront import x",       # 同前缀他模块，不得误红
            "yiban.storefront_flag = 1",
        )
        for src in cases:
            with self.subTest(src=src):
                self.assertEqual(self._hits_of_source(src), [],
                                 f"干净样本被误红：{src!r}")

    def test_alias_scanner_has_teeth(self):
        """父包别名判据：`from <父包> import <旧子名>` 必须命中。"""
        self.assertTrue(_alias_names("from yiban import store"))
        self.assertTrue(_alias_names("from yiban.store import db, queue_store"))
        self.assertEqual(_alias_names("from yiban.adapters.persistence import db"), [])
        self.assertEqual(_alias_names("from yiban.adapters import persistence"), [])
        self.assertEqual(_alias_names("from yiban.storefront import x"), [])

    def test_policy_token_is_not_a_blanket_exemption(self):
        """策略表豁免是**按路径**的，不是按字面量：同一内容放到别处即判红。"""
        import tempfile
        rel = "tests/test_infra_layer.py"
        self.assertIn(rel, POLICY_TOKEN_FILES)
        path = os.path.join(BASE, rel)
        # 原位：裸串枚举被豁免 ⇒ 读者面为零
        self.assertEqual(t6_old_coordinate_hits(path), [],
                         f"{rel} 原位仍有读者命中")
        # 换路径（临时文件）⇒ 同一内容里的裸串不再是策略表项 ⇒ 必须判红
        fd, tmp = tempfile.mkstemp(suffix=".py", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(_read(path))
            hits = t6_old_coordinate_hits(tmp)
            self.assertTrue(
                hits,
                "同一内容换到非策略表路径后仍被判干净：豁免是按字面量而不是按路径，"
                "那它就会顺带豁免真正的读者")
        finally:
            os.unlink(tmp)

    def test_scan_surface_is_alive(self):
        """防废门：扫描面必须真的走到文件。"""
        files = t6_own_face_files(BASE)
        self.assertGreaterEqual(len(files), 400, f"扫描面过小（{len(files)}）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
