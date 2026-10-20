# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""T5 与 T7 两族旧坐标迁移后的守卫（仓库结构整理批 1）。

标签：M · 架构分层与基础设施
覆盖：本批把两族读者改指新坐标。旧坐标留转发壳，壳由 `145-E` 删除。
    T5 族＝`yiban.config_loader`、`yiban.infra.env_io`、`yiban.infra.account_crypto`、
    `yiban.state_gc`、`yiban.cred_state`。
    T7 族＝`yiban.mail`、`yiban.notify`。
    新坐标＝`yiban.adapters.config.loader`、`yiban.adapters.config.env_io`、
    `yiban.adapters.config.account_crypto`、`yiban.adapters.statefiles.state_gc`、
    `yiban.adapters.statefiles.cred_state`、`yiban.adapters.notifier.mail`、
    `yiban.adapters.notifier.notify`。
    本文件钉四条不变式：
    ① **自有面零旧坐标读者**：面＝`yiban/**`、`web/**`（非 `web/static/**`）、
       `scripts/**`、`docker/**`、`tests/**`、`run.sh`。七个旧坐标不得再作 import 路径
       （四种写法：`import X`、`from X import Y`、`from <父包> import <子名>`、
       `import yiban as A` 后的属性访问）、属性链（`yiban.infra.env_io.f(...)`）、
       或**运行期字符串**（`importlib.import_module("yiban.notify")`、`python -m yiban.mail`、
       `mock.patch("yiban.cred_state.x")`、`"from yiban.infra import env_io"` 这类代码串）。
    ② **打桩面单列**：`mock.patch("<旧坐标>.…")` 在转发壳下能成功但不控被测路径
       （静默失效）。本族实测有打桩点，本条防复发。
    ③ **新坐标在位且被消费方指向**：七个新坐标可按模块导入，`__file__` 在新路径下；
       本批迁移过的生产读者各自引到新坐标。
    ④ **门自身有牙**：夹具样本必须被判红；干净样本不得判红；扫描面过小即红（扫空不得当绿）。

**为什么判据必须同时扫 import 语句与运行期字符串面**：静态依赖图（import-linter）
只看 `import` 语句。`importlib.import_module("yiban.notify")` 这类**字符串坐标**不在它的
判定面内。壳删除后串内坐标当场 `ModuleNotFoundError`。故本门两面包围。
**为什么 import 判据要认"父包别名"形态**：`from yiban.infra import env_io` 里没有
`yiban.infra.env_io` 这个连续字面量。只按点号字面量扫会**漏掉它**（施工稿第 7.1 节的
教训：T3 族只按点号形态扫，漏掉 25/57 个文件）。本门按 AST 逐项判名，并展开属性链。

**口径（改动面怎么数出来的）**：与施工稿第 7 节同口径——某文件命中＝该文件出现
上述 import 写法之一、属性链、或运行期字符串。**注释与 docstring 里的旧坐标字样不计入**
（与 T2／T3 同口径：散文指针不误红，历史叙述不误红）。实测：自有面命中 81 个 `.py`
文件（T5 65 ＋ T7 31，重叠 15）＋ 1 个 `.sh` 文件。

**豁免（按文件给，不是跳过；未登记的文件命中即红并点名 `文件:行`）**：
  本文件自身——`OLD_MODULES` 是禁字面量，夹具样本用于自证本门有牙（与 T3 门的
    自豁免同形）。
  `test_exempt_files_still_exist_and_are_hit` 另钉"每个豁免文件**必须仍命中本门**"，
  防"顺手把文件加进豁免面来让门变绿"（AGENTS 第 14 条反面处置③）。
  `145-E` 已把七个旧壳删净，故 `SHELL_RELS`／`SHELL_DIRS` 两条豁免随之删除；
  全仓总门（`tests/test_zero_old_coordinates.py`）另钉"壳本体不存在"。

**日志通道名豁免（不是坐标）**：`yiban.config_loader` 与 `yiban.cred_state` 同时是
    `logging.getLogger(...)` 的**通道名**（实现在 `yiban/adapters/config/loader.py` 与
    `yiban/adapters/statefiles/cred_state.py`），`tests/` 里另有 `assertLogs` 用同名。
    改名会改动**运维可见的日志出口名**，属语义变更 ⇒ 本批不改，本门豁免实参位的那个字面量。
    T3／T4 先例同此（`yiban.window`／`yiban.cred_state` 的日志名保留）。
    `test_logger_name_exemption_is_anchored` 钉住豁免不腐烂。
对应实现：七个新坐标下的模块本体（批 A4／批 B 已搬）。
关键断言：① ② ④ 读源码文本与 `ast`；③ 用 `importlib` 判模块在位。
依赖：只读仓库文件与导入包；不联网、不建库、不发邮件、不发通知。
"""
import ast
import functools
import importlib
import io
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: T5 族旧坐标（配置与状态文件适配器）。
T5_OLD = ("yiban.config_loader", "yiban.infra.env_io", "yiban.infra.account_crypto",
          "yiban.state_gc", "yiban.cred_state")
#: T7 族旧坐标（通知适配器）。
T7_OLD = ("yiban.mail", "yiban.notify")
OLD_MODULES = T5_OLD + T7_OLD

#: 旧坐标 → 新坐标（本批迁移的映射，施工稿已现量核实）。
NEW_OF = {
    "yiban.config_loader": "yiban.adapters.config.loader",
    "yiban.infra.env_io": "yiban.adapters.config.env_io",
    "yiban.infra.account_crypto": "yiban.adapters.config.account_crypto",
    "yiban.state_gc": "yiban.adapters.statefiles.state_gc",
    "yiban.cred_state": "yiban.adapters.statefiles.cred_state",
    "yiban.mail": "yiban.adapters.notifier.mail",
    "yiban.notify": "yiban.adapters.notifier.notify",
}

#: 日志通道名（不是坐标）：见模块头。实参位出现这些字面量即豁免。
LOGGER_CHANNELS = ("yiban.config_loader", "yiban.cred_state")
#: 以日志通道名为首个实参的调用名。
LOGGER_CALLS = frozenset(("getLogger", "assertLogs", "assertNoLogs"))

#: 职责就是夹具自证本门有牙的文件：必须写出旧坐标。
#: `145-E` 删了旧壳与两件"壳＝实现同一对象"守卫（`test_batch_b_relocation.py`、
#: `test_notifier_relocation.py`），故它们从本表移除。
#: `tests/test_zero_old_coordinates.py` 是 `145-E` 立的**全仓总门**，它的
#: `OLD_MODULES` 是 20 条禁字面量（含本族的七条），故必须登记。
FACE_EXEMPT = frozenset((
    "tests/test_t5_t7_relocation.py",
    "tests/test_zero_old_coordinates.py",
))

#: 自有面目录（不含面外 `frontend/**` 与 `web/static/**`：归用户独立完成）。
FACE_DIRS = ("yiban", "web", "scripts", "docker", "tests")
FACE_SHELL = ("run.sh",)
SKIP_DIRS = frozenset(("node_modules", "__pycache__", ".venv", "venv", "dist", "build"))
#: 面内不归本批改的子树（前端产物）。
SKIP_REL_PREFIX = ("web/static/",)

#: 生产读者 → 迁移后必须引到的新坐标**父包**（不变式③：消费方指向新坐标）。
#: 判父包而不是全点号坐标：`from yiban.adapters.config import env_io` 这种别名形态里
#: 没有连续字面量 `yiban.adapters.config.env_io`。旧坐标清零由不变式①另钉。
PRODUCTION_CONSUMERS = (
    ("docker/scheduler.py", "yiban.adapters.statefiles"),
    ("scripts/audit_verify.py", "yiban.adapters.config"),
    ("scripts/backup_sentinel.py", "yiban.adapters.notifier"),
    ("scripts/check-config-registry.py", "yiban.adapters.config"),
    ("scripts/dev_visual_seed.py", "yiban.adapters.config"),
    ("scripts/drill-container.sh", "yiban.adapters.config"),
    ("scripts/drill_multi_exec.py", "yiban.adapters.config"),
    ("scripts/ledger_check.py", "yiban.adapters.config"),
    ("scripts/list_duplicate_owners.py", "yiban.adapters.config"),
    ("scripts/state_cleanup.py", "yiban.adapters.statefiles"),
    ("web/routes/notify.py", "yiban.adapters.config"),
    ("web/routes/pages.py", "yiban.adapters.config"),
    ("web/routes/settings_api.py", "yiban.adapters.config"),
    ("web/security.py", "yiban.adapters.notifier"),
    ("web/services/capacity.py", "yiban.adapters.config"),
    ("web/services/channel_health.py", "yiban.adapters.notifier"),
    ("web/services/env_io.py", "yiban.adapters.notifier"),
    ("web/services/executor_env.py", "yiban.adapters.config"),
    ("web/services/logs.py", "yiban.adapters.statefiles"),
    ("web/services/notify_mail.py", "yiban.adapters.notifier"),
    ("web/services/run_stream.py", "yiban.adapters.config"),
    ("web/services/signstatus.py", "yiban.adapters.config"),
    ("yiban/adapters/notifier/report.py", "yiban.adapters.notifier"),
    ("yiban/engine/accounts.py", "yiban.adapters.notifier"),
)

#: 旧坐标的字面量判据：模块名后不得紧跟标识符字符
#: （`yiban.mail` 命中；`yiban.mailer` 这类同前缀他模块不命中）。
_OLD_LITERAL = {m: re.compile(r"(?<![\w.])" + re.escape(m) + r"(?![\w])")
                for m in OLD_MODULES}

#: 文本里的"父包别名"形态：`from yiban.infra import env_io` / `from yiban import mail`。
#: 取首个标识符，不剥字符集（shell 的引号、反引号、`as` 都要能带出短名）。
_ALIAS_FROM = re.compile(r"\bfrom\s+(yiban[A-Za-z_.]*)\s+import\s+([^\n#;]*)")


@functools.lru_cache(maxsize=None)
def _text_of(path):
    """该文件的源码文本（按路径缓存）。"""
    with io.open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _read(path):
    return _text_of(path)


def _old_of(dotted):
    """`dotted` 是哪个旧坐标（或其子模块）；不是则返回 `None`。"""
    for m in OLD_MODULES:
        if dotted == m or dotted.startswith(m + "."):
            return m
    return None


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

    覆盖四种写法：`import yiban.infra.env_io`（绑顶层名 `yiban`）、
    `import yiban.infra.env_io as E`（绑 `E`）、`from yiban import mail`（绑 `mail` →
    `yiban.mail`）、`from yiban.infra import env_io`（绑 `env_io` → `yiban.infra.env_io`）。
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


def _logger_name_ids(tree):
    """作为日志通道名使用的字符串常量节点 id（`getLogger("yiban.cred_state")` 等）。"""
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if fname not in LOGGER_CALLS:
            continue
        for a in node.args:
            if isinstance(a, ast.Constant) and a.value in LOGGER_CHANNELS:
                ids.add(id(a))
    return ids


@functools.lru_cache(maxsize=None)
def _ast_of(path):
    """该文件的 AST（按路径缓存：两个用例各扫一遍面，重复解析没有必要）。"""
    return ast.parse(_read(path))


@functools.lru_cache(maxsize=None)
def t57_old_coordinate_hits(path):
    """返回该文件引用旧坐标的位置：`(行号, 说明)` 列表。

    Python：① import 语句（四种写法，含父包别名与 `import yiban as A` 的属性访问）；
    ② 属性链根在旧坐标（`yiban.infra.env_io.f`、`env_io.f` 经父包别名绑定）；
    ③ 非 docstring 字符串常量含旧坐标（覆盖 `importlib.import_module("yiban.notify")`、
    打桩目标、`python -m yiban.mail`、`"from yiban.infra import env_io"` 这类代码串）。
    日志通道名在 `getLogger`/`assertLogs`/`assertNoLogs` 实参位豁免。
    Shell：**非注释行**含旧坐标字面量或父包别名形态。
    注释与 docstring 不算（任务口径：散文指针与历史叙述不误红）。
    """
    text = _read(path)
    if path.endswith((".sh", ".bash")) or os.path.basename(path).startswith("Dockerfile"):
        hits = []
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if any(_OLD_LITERAL[m].search(s) for m in OLD_MODULES):
                hits.append((i, "shell 非注释行含旧坐标字面量"))
            elif _alias_names(s):
                hits.append((i, "shell 非注释行含 from <父包> import <旧子名>"))
        return sorted(set(hits))
    tree = _ast_of(path)
    docs = _docstring_const_ids(tree)
    logger_ids = _logger_name_ids(tree)
    pref = _bound_prefixes(tree)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if _old_of(a.name):
                    hits.append((node.lineno, "import 旧坐标 " + a.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            mod = node.module or ""
            if _old_of(mod):
                hits.append((node.lineno, "from 旧坐标 import ... " + mod))
            for a in node.names:
                if a.name == "*":
                    continue
                full = (mod + "." + a.name) if mod else a.name
                if _old_of(full):
                    hits.append((node.lineno, "from 父包 import 旧子名 " + full))
        elif isinstance(node, ast.Attribute):
            dotted = _dotted_of(node, pref)
            if dotted and _old_of(dotted):
                hits.append((node.lineno, "属性链 " + dotted))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docs and id(node) not in logger_ids:
            if any(_OLD_LITERAL[m].search(node.value) for m in OLD_MODULES):
                hits.append((node.lineno, "非 docstring 字符串 " + node.value[:60]))
            elif _alias_names(node.value):
                hits.append((node.lineno, "非 docstring 字符串含父包别名 " + node.value[:60]))
    return sorted(set(hits))


def _alias_names(text):
    """文本里 `from <父包> import <子名>` 命中的旧子名列表（按项判名）。

    `from yiban.infra import env_io` → `["yiban.infra.env_io"]`。
    取首个标识符，不剥字符集：shell 的 `python3 -c "from yiban.infra import env_io"`
    与转义引号都会把收尾引号带进列表项，剥字符集会**静默漏检**（施工稿第 9 节同族缺陷）。
    """
    out = []
    for m in _ALIAS_FROM.finditer(text):
        parent = m.group(1)
        for item in m.group(2).split(","):
            token = re.search(r"[A-Za-z_]\w*", item)
            if not token:
                continue
            full = parent + "." + token.group(0)
            if _old_of(full):
                out.append(full)
    return out


def t57_own_face_files(root):
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


def _is_exempt(rel):
    """该文件是否按职责必须写出旧坐标（旧坐标门自身）。

    `145-E` 之后只剩本门自身：旧壳已删，`SHELL_RELS`／`SHELL_DIRS` 两条豁免已随之移除。
    """
    return rel in FACE_EXEMPT


class T57OwnFaceTest(unittest.TestCase):
    """① ② 自有面零旧坐标读者；打桩面零命中。"""

    def test_own_face_has_no_old_coordinate(self):
        files = t57_own_face_files(BASE)
        self.assertGreaterEqual(
            len(files), 400,
            f"自有面扫描面过小（{len(files)} 个文件）——扫描根写错或树被移走（扫空不得当绿）")
        bad = []
        for rel in files:
            if _is_exempt(rel):
                continue
            try:
                hits = t57_old_coordinate_hits(os.path.join(BASE, rel))
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
        """打桩面单列一条：`mock.patch("<旧坐标>.…")` 是静默失效的典型形态。"""
        files = t57_own_face_files(BASE)
        self.assertGreaterEqual(len(files), 400, f"扫描面过小（{len(files)}）")
        bad = []
        for rel in files:
            if not rel.endswith(".py") or _is_exempt(rel):
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
                for a in node.args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                            and id(a) not in docs \
                            and any(_OLD_LITERAL[m].search(a.value) for m in OLD_MODULES):
                        bad.append(f"{rel}:{node.lineno}: 打桩目标 {a.value}")
        self.assertEqual(
            bad, [],
            "打桩目标仍指旧坐标——壳退役后会静默失效（打桩成功但不控被测路径）：\n"
            + "\n".join(bad))


class T57NewCoordinateTest(unittest.TestCase):
    """③ 新坐标在位，且生产消费方指向它。"""

    def test_new_coordinates_are_importable(self):
        for new in NEW_OF.values():
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
        """映射表自洽：七个旧坐标各有新坐标，且新旧不同。"""
        self.assertEqual(len(NEW_OF), len(OLD_MODULES))
        for old in OLD_MODULES:
            self.assertIn(old, NEW_OF, f"{old} 没有登记新坐标")
            self.assertNotEqual(old, NEW_OF[old])


class T57ExemptionAnchorTest(unittest.TestCase):
    """④ 豁免不腐烂：日志名确为通道名；豁免文件仍在且仍命中本门。"""

    def test_logger_name_exemption_is_anchored(self):
        """`yiban.config_loader` / `yiban.cred_state` 确实是实现里的日志通道名。"""
        found = set()
        for rel in ("yiban/adapters/config/loader.py",
                    "yiban/adapters/statefiles/cred_state.py"):
            tree = ast.parse(_read(os.path.join(BASE, rel)))
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
            f"实现里不再有这些日志通道名：{sorted(set(LOGGER_CHANNELS) - found)}。"
            "本门的日志名豁免已腐烂，须同批删掉该豁免")

    def test_exempt_files_still_exist_and_are_hit(self):
        """豁免清单不得腐烂：登记的文件必须还在，且**必须仍命中本门**。"""
        for rel in sorted(FACE_EXEMPT):
            with self.subTest(rel=rel):
                self.assertTrue(os.path.isfile(os.path.join(BASE, rel)), f"豁免文件 {rel} 不在了")
                self.assertTrue(t57_old_coordinate_hits(os.path.join(BASE, rel)),
                                f"{rel} 已不再含旧坐标：豁免失去理由，须从豁免面移除")


class T57GateHasTeethTest(unittest.TestCase):
    """④ 门自身有牙：夹具样本必判红；干净样本不得判红。"""

    def _hits_of_source(self, src):
        """把源码写进临时文件后走真判据（复用被测路径，不另写第二份逻辑）。"""
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".py", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(src)
            return t57_old_coordinate_hits(path)
        finally:
            os.unlink(path)

    def test_fixtures_are_detected(self):
        cases = (
            "import yiban.state_gc",
            "from yiban import mail",
            "from yiban.infra import env_io",
            "from yiban.infra.env_io import parse_env_file",
            "from yiban.mail.config import _mask_addr",
            "import yiban.notify as N\nN.send()",
            "from yiban import notify\nnotify.send()",
            'import importlib\nimportlib.import_module("yiban.notify")',
            'TARGET = "yiban.cred_state.path"',
            'CODE = "from yiban.infra import env_io; env_io.x()"',
            "import yiban\nx = yiban.mail.get_config()",
        )
        for src in cases:
            with self.subTest(src=src):
                self.assertTrue(self._hits_of_source(src),
                                f"夹具未被判红：本门没有牙 —— {src!r}")

    def test_clean_fixtures_are_not_flagged(self):
        cases = (
            "from yiban.adapters.config import env_io",
            "from yiban.adapters.notifier.mail import layout",
            "import yiban.adapters.statefiles.cred_state as cs",
            'import importlib\nimportlib.import_module("yiban.adapters.notifier.notify")',
            "# yiban.mail 只是散文里的旧坐标，不是读者",
            '"""docstring 里的 yiban.notify 不算读者。"""',
            'logger = logging.getLogger("yiban.cred_state")',
            'with self.assertLogs("yiban.config_loader", level="WARNING"):\n    pass',
            "from yiban.mailer_thing import x",     # 同前缀他模块，不得误红
            "yiban.mailer_x = 1",
        )
        for src in cases:
            with self.subTest(src=src):
                self.assertEqual(self._hits_of_source(src), [],
                                 f"干净样本被误红：{src!r}")

    def test_alias_scanner_has_teeth(self):
        """父包别名判据：`from <父包> import <旧子名>` 必须命中。"""
        self.assertTrue(_alias_names("from yiban.infra import env_io"))
        self.assertTrue(_alias_names("from yiban import mail"))
        self.assertTrue(_alias_names('from yiban.infra import account_crypto, env_io'))
        self.assertEqual(_alias_names("from yiban.infra import locks, private_json"), [])
        self.assertEqual(_alias_names("from yiban.adapters.config import env_io"), [])

    def test_scan_surface_is_alive(self):
        """防废门：扫描面必须真的走到文件。"""
        files = t57_own_face_files(BASE)
        self.assertGreaterEqual(len(files), 400, f"扫描面过小（{len(files)}）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
