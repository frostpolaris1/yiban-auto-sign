# -*- coding: utf-8 -*-
"""部署入口脚本的导入条件 ＋ 拆墙/壳退役批的**坐标门**（145-B 各 `T` 片）。

标签：J · 运维：部署/备份/发布
覆盖：① `scripts/signin.py` 与 `docker/scheduler.py` 在无 PYTHONPATH 下的真实导入、
    凡导入 `yiban` 的运行时脚本必须自带包导入引导、引导必须**先于**首个 yiban 导入；
    ② 拆墙批的坐标门（同一份"扫描 + 显式豁免 + 突变自证"骨架，逐片加一族）：
      · `T8`：运行期载体不得以旧坐标 `yiban.cli` 起进程；
      · `T8`：`tests/**` 不得以旧坐标 `yiban.cli` 作 import 路径或打桩目标；
      · `T1`：口令哈希两名不得再经 `web.app` 墙坐标读或被它打桩（自有面零命中；
        面外 `frontend/**` 的残留逐条登记并与代码对账，两侧都不许静默）。
对应实现：`scripts/signin.py`、`docker/scheduler.py`（镜像里被复制为
    `scripts/container_scheduler.py`）及各入口的 `sys.path` 引导块；
    坐标门的实现侧＝`yiban/cli.py` 转发壳与 `web/app.py` 转发墙（删它们归 `145-E`）。
关键断言：导入失败以非零码退出、进入主循环则超时——两种情况可区分；空文件/截断文件
    同样能 import 成功，故额外要求模块真的装配出 `main_loop`；每道坐标门都带扫描面
    下限（扫空不得当绿）、豁免清单防腐与突变自证（塞回一处 ⇒ 红；注释提及 ⇒ 不误红）。
依赖：起 `sys.executable` 子进程跑仓库内脚本（清空 PYTHONPATH、cwd=仓库根、临时
    STATE/LOG/DB/ENV）；坐标门是纯 AST + 文件遍历，不导入 web 应用。不需 bash/docker CLI。

镜像里 `docker/scheduler.py` 由 supervisord 直接执行——`sys.path[0]` 是 `scripts/`，
共享包 `yiban/` 在仓库根，缺引导就 ModuleNotFoundError（容器 sched 进程反复退出→FATAL，
本地跑测试与裸机部署都发现不了，它们的 sys.path 里本来就有仓库根）。
"""
import ast
import os
import re
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 包导入引导块的判据：真正的引导一定会在仓库根/scripts 上插 sys.path，
# 惯用写法是先算一个根路径变量再插（`_REPO_ROOT = ...`）——两种写法都算引导；
# 只在 docstring/注释里出现字样不算（本仓曾有过这样的模块：说明文字里有
# `_REPO_ROOT`，代码里一行引导都没有，却通过了早期那条存在性判据）。
BOOT = re.compile(r"(?m)^\s*(?:_REPO_ROOT\s*=|sys\.path\.insert)")
YIBAN_IMPORT = re.compile(r"(?m)^\s*(?:from|import)\s+yiban\b")


class DeployEntryImportTest(unittest.TestCase):
    def _run(self, rel, args=(), timeout=8):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)  # 只留脚本自身目录 → 等价于容器内的导入条件
        tmp = tempfile.mkdtemp(prefix="yiban-entry-")
        env.update({
            "YIBAN_STATE_DIR": tmp,
            "YIBAN_LOG_FILE": os.path.join(tmp, "sign.log"),
            "YIBAN_DB_FILE": os.path.join(tmp, "yiban.db"),
            "YIBAN_ENV_FILE": os.path.join(tmp, ".env"),
        })
        return subprocess.run([sys.executable, rel, *args], cwd=BASE, env=env,  #按文件路径直跑：这就是 supervisord 的执行条件，`-m` 会替它补上引导
                              capture_output=True, text=True, timeout=timeout)

    def test_signin_cli_imports_without_repo_root_on_path(self):
        r = self._run("scripts/signin.py", ("--help",))
        self.assertNotIn("ModuleNotFoundError", r.stderr, r.stderr[-600:])
        self.assertEqual(r.returncode, 0, r.stderr[-600:])

    def test_container_scheduler_imports(self):
        """调度器必须能导入（不真跑主循环：模块体在 __main__ 守卫里才起循环）。

        跑仓库内的源文件 `docker/scheduler.py`——镜像里它被复制为
        `scripts/container_scheduler.py`，两处都只比仓库根低一层，
        同一段引导在两种位置都成立。
        """
        code = (
            "import importlib.util, sys;"
            "spec = importlib.util.spec_from_file_location('sched_probe', sys.argv[1]);"
            "m = importlib.util.module_from_spec(spec);"
            "spec.loader.exec_module(m);"
            "print('IMPORT_OK' if callable(getattr(m, 'main_loop', None)) else 'EMPTY_MODULE')"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        r = subprocess.run([sys.executable, "-c", code, "docker/scheduler.py"],
                           cwd=BASE, env=env, capture_output=True, text=True, timeout=30)
        self.assertNotIn("ModuleNotFoundError", r.stderr, r.stderr[-800:])
        # 注意校验"装配完成"：空文件同样能 import 成功（2026-09-15 实测踩到过），
        # 那样的模块在容器里会静默不调度——比崩溃更难发现。
        self.assertIn("IMPORT_OK", r.stdout,
                      f"模块未装配出 main_loop（空文件/截断？）：{r.stdout!r} {r.stderr[-400:]}")

    def test_all_local_package_importers_are_bootstrapped(self):
        """凡导入 yiban 的运行时脚本，都必须自己保证仓库根在 sys.path 上。

        判据是**真实存在引导块且先于首个 yiban 导入**，不是"文本里出现过引导字样"：
        只在 docstring/注释里提一句 `_REPO_ROOT` 不算引导（`web/render.py` 曾如此通过
        存在性判据）。结构断言（此处无可执行的行为判据：脚本一旦跑起来就是守护进程/
        真实签到）。
        """
        problems = []
        for d in ("scripts", "docker", "web"):
            for name in sorted(os.listdir(os.path.join(BASE, d))):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(BASE, d, name)
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                m_imp = YIBAN_IMPORT.search(text)
                if not m_imp:
                    continue
                m_boot = BOOT.search(text)
                if m_boot is None or m_boot.start() > m_imp.start():  #比位置，不比字样：引导写在 yiban 导入之后等于没有
                    problems.append(f"{d}/{name}")
        self.assertEqual(problems, [], f"以下脚本导入 yiban 却没有包导入引导：{problems}")

    def test_bootstrap_precedes_yiban_import(self):
        """引导必须**先于**任何 yiban 导入——反过来写会在"直接跑脚本"时崩。

        2026-09-16 实测踩到：`db.py` 的 infra 导入被放到引导之前，于是只
        `import db` 的小 CLI（`python3 scripts/list_duplicate_owners.py`）报
        ModuleNotFoundError: No module named 'yiban'——而文件里"有引导"，上面那条
        存在性断言看不出来。这里按**出现位置**判定，把顺序钉死。
        """
        bad = []
        for d in ("scripts", "docker", "web", "yiban", "tests"):
            for dirpath, dirs, files in os.walk(os.path.join(BASE, d)):
                dirs[:] = [x for x in dirs if x != "__pycache__"]
                for name in sorted(files):
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    with open(path, encoding="utf-8") as f:
                        text = f.read()
                    m_boot, m_imp = BOOT.search(text), YIBAN_IMPORT.search(text)
                    if m_boot and m_imp and m_imp.start() < m_boot.start():
                        rel = os.path.relpath(path, BASE).replace("\\", "/")
                        line = text[:m_imp.start()].count("\n") + 1
                        bad.append(f"{rel}:{line}")
        self.assertEqual(bad, [], f"以下文件先导入 yiban、后补引导（直接运行会 ImportError）：{bad}")


# ---------------------------------------------------------------------------
# 拆墙/壳退役 T8：运行期载体不得以旧坐标 `yiban.cli` 起进程
# ---------------------------------------------------------------------------
# 来历（工单 `1229`，施工方案 §1.7 与 §4.1 风险 2）：
# `yiban/cli.py` 是批 A1 留下的转发壳，待退役。`yiban/workers/workers.py` 真起
# `python -m yiban.cli sign` 作为多执行体的子进程入口。壳一删，生产执行体全起不来——
# 这是整批唯一的运行期硬依赖。本门钉：运行期载体里旧坐标的**启动点**为零。
#
# 面（运行期载体）＝部署时真会被执行的文件：
#   `yiban/**/*.py`、`scripts/**/*.py`、`docker/**/*.py`、`scripts/**/*.sh`、
#   `docker/**/*.sh`，以及仓库根的 `run.sh`。
# 不在面内：`tests/`、`docs/`、`README.md`、注释、docstring，以及用户可见的帮助文本
#   （`yiban/entry/cli/` 的 `USAGE` / `prog` 写旧坐标是给当前用户的用法指引，不是启动点）。
#   文档与注释里的历史叙述不算（任务口径）。
RUNTIME_PY_DIRS = ("yiban", "scripts", "docker", "web")
RUNTIME_SHELL_NAMES = ("run.sh",)
OLD_CLI_MODULE = "yiban.cli"

#: 会拉起子进程的函数名（`-m <模块>` 起进程的宿主）。
_LAUNCH_FUNCS = frozenset((
    "Popen", "run", "call", "check_call", "check_output", "system",
    "spawnv", "spawnve", "spawnvp", "spawnl", "spawnle", "spawnlp",
    "execv", "execve", "execvp", "execl", "execle", "execlp",
))


#: 部署时真会被执行、但不以 `.py`/`.sh` 命名的载体（2026-10-19 独立审查补齐）：
#: 它们含命令行，必须纳入面——否则"看着绿"只因没扫到。
#: `deploy/prod/` 整目录纳入（install.sh 与备份 wrapper 以 root 执行）；cron.d 的每个
#: 文件都是一行命令行（无扩展名）；supervisord/Dockerfile/compose/systemd 的
#: `command=`/`ExecStart=` 同理。
RUNTIME_EXTRA_PATHS = (
    "run_probe.sh",
    "docker/supervisord.conf",
    "docker/Dockerfile",
    "docker-compose.yml",
    "web/deploy/yiban-web.service",
)
RUNTIME_EXTRA_DIRS = (
    "deploy",
    "docker/cron.d",
    "web/deploy",
)


def _runtime_files(root):
    """运行期载体文件清单。

    面＝① `RUNTIME_PY_DIRS` 下的 `.py`/`.sh`（含 `web/`——web 服务运行体）；
    ② 根部的 `RUNTIME_EXTRA_PATHS`；③ `RUNTIME_EXTRA_DIRS` 下的全部文件。
    非 `.py` 的一律按**文本**扫（命令行载体），不按 AST。
    """
    out = []
    for d in RUNTIME_PY_DIRS:
        for dirpath, dirs, files in os.walk(os.path.join(root, d)):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            for name in sorted(files):
                if name.endswith((".py", ".sh")):
                    out.append(os.path.join(dirpath, name))
    for name in RUNTIME_SHELL_NAMES + RUNTIME_EXTRA_PATHS:
        path = os.path.join(root, name)
        if os.path.exists(path):
            out.append(path)
    for d in RUNTIME_EXTRA_DIRS:
        for dirpath, dirs, files in os.walk(os.path.join(root, d)):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            for name in sorted(files):
                out.append(os.path.join(dirpath, name))
    return out


def _docstring_const_ids(tree):
    """模块/类/函数首个语句若是字符串常量，记下它的对象 id（docstring 不算启动点）。"""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _command_literals(node):
    """一次调用里可能作为命令行的字面量字符串（列表/元组元素与裸字符串实参）。"""
    vals = []
    for a in list(node.args) + [k.value for k in node.keywords if k.arg in ("args", "cmd")]:
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            vals.append(a.value)
        elif isinstance(a, (ast.List, ast.Tuple, ast.Set)):
            for e in a.elts:
                if isinstance(e, ast.Constant) and isinstance(e.value, str):
                    vals.append(e.value)
    return vals


#: 别名导入形态：`from yiban import …, cli, …`（**无点号**，点号判据看不见它）。
#: 同 T3 的 `_T3_FROM_YIBAN` 形状——T3 早就有，T8 一直缺，属"同一判据两处实现不对称"
#: （AGENTS §27）。145-D 实测踩到：`scripts/deploy_gate.sh:381` 的
#: `python -c "from yiban import cli; cli.main(...)"` 不被任何门看见，删壳后必断。
#: **按 import 列表逐项判名**，不用裸词界：裸 `\bcli\b` 会误伤 `--cli` 这类旗标
#: （同 T3 的 `--status` 假红）。
_FROM_YIBAN = re.compile(r"\bfrom\s+yiban\s+import\s+([^\n#;]*)")


def _alias_short_names(text):
    """文本里 `from yiban import …` 命中的短名（逐项取首个标识符，不剥引号）。"""
    names = []
    for m in _FROM_YIBAN.finditer(text):
        for item in m.group(1).split(","):
            token = re.search(r"[A-Za-z_]\w*", item)
            if token:
                names.append(token.group(0))
    return names


def old_coordinate_hits(path):
    """返回该文件里引用旧坐标的位置：`(行号, 说明)` 列表。

    **非 `.py` 一律按文本扫**（.sh、cron.d 行、supervisord/Dockerfile/compose/systemd
    的 command 行）：非注释行出现旧模块名**或别名导入**即命中。

    Python：① 进程拉起调用的命令行字面量含旧模块名；② 非 docstring 的字符串常量
    恰为旧模块名（覆盖 importlib / 变量间接形态）；③ 非 docstring 字符串常量里含
    **别名导入** `from yiban import …, cli, …`（`python -c` 形态）；
    ④ **真 import 语句**：`import yiban.cli[.x]`、`from yiban import cli`、
    `from yiban.cli import …`、属性链 `yiban.cli`。
    ④ 是 2026-10-19 独立审查抓出的阻塞洞——关掉它，"不对称"才算真正修完：
    T8 的 tests 面门与 T3 门本来就认 import 语句。

    已知盲点（登记，不假装覆盖）：变量拼接
    （`"yiban." + "cli"`、`importlib.import_module("yiban."+"cli")`）与
    `from yiban import *` 不命中——字面量扫描器的固有限制；`yiban/__init__.py`
    不导出 `cli`，星号导入取不到该名。
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    hits = []
    if not path.endswith(".py"):
        pat = re.compile(r"(?<![\w.-])" + re.escape(OLD_CLI_MODULE) + r"(?![\w.-])")
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if pat.search(s):
                hits.append((i, "命令行含 " + OLD_CLI_MODULE))
            elif "cli" in _alias_short_names(s):
                hits.append((i, "别名导入 from yiban import …, cli, …"))
        return hits
    tree = ast.parse(text)
    docs = _docstring_const_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if fname in _LAUNCH_FUNCS and OLD_CLI_MODULE in _command_literals(node):
                hits.append((node.lineno, "启动命令含 " + OLD_CLI_MODULE))
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == OLD_CLI_MODULE or a.name.startswith(OLD_CLI_MODULE + "."):
                    hits.append((node.lineno, "import 语句 " + a.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == OLD_CLI_MODULE or mod.startswith(OLD_CLI_MODULE + "."):
                hits.append((node.lineno, "from " + mod + " import …"))
            elif mod == "yiban":
                for a in node.names:
                    if a.name == "cli":
                        hits.append((node.lineno, "from yiban import cli"))
        elif (isinstance(node, ast.Attribute) and node.attr == "cli"
              and isinstance(node.value, ast.Name) and node.value.id == "yiban"):
            # 属性链 yiban.cli[.x]
            hits.append((node.lineno, "属性链 yiban.cli"))
        if isinstance(node, ast.Constant) and node.value == OLD_CLI_MODULE \
                and id(node) not in docs:
            hits.append((node.lineno, "非 docstring 字符串常量 " + OLD_CLI_MODULE))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docs and "cli" in _alias_short_names(node.value):
            hits.append((node.lineno, "字符串里的别名导入 from yiban import …, cli, …"))
    return sorted(set(hits))


class OldCliCoordinateGateTest(unittest.TestCase):
    """T8：运行期载体里旧坐标 `yiban.cli` 的启动点为零。"""

    def test_no_runtime_carrier_launches_old_coordinate(self):
        files = _runtime_files(BASE)
        self.assertGreaterEqual(
            len(files), 40,
            f"扫描面过小（{len(files)} 个文件）——扫描根写错或载体被移走（扫空不得当绿）")
        bad = []
        for path in files:
            for line, why in old_coordinate_hits(path):
                rel = os.path.relpath(path, BASE).replace("\\", "/")
                bad.append(f"{rel}:{line}: {why}")
        self.assertEqual(
            bad, [],
            "运行期载体仍以旧坐标起进程（转发壳退役后生产执行体起不来）：\n" + "\n".join(bad))

    def test_scanner_has_teeth(self):
        """突变验证：塞回一处 ⇒ 红；注释/docstring 提及 ⇒ 不误红。"""
        with tempfile.TemporaryDirectory(prefix="oldcli-gate-") as tmp:
            py = os.path.join(tmp, "fake_launcher.py")
            with open(py, "w", encoding="utf-8") as f:
                f.write(
                    '"""模块 docstring：-m yiban.cli 只是历史叙述，不算启动点。"""\n'
                    'import subprocess\n'
                    'import sys\n'
                    'CMD = "yiban.cli"\n'                       # 裸字符串常量 ⇒ 命中
                    'subprocess.Popen([sys.executable, "-m", "yiban.cli", "sign"])\n'
                    '# 注释里的 -m yiban.cli 不算\n')
            hits = old_coordinate_hits(py)
            self.assertTrue(hits, "塞回旧坐标启动点却未命中：本门没有牙")
            lines = sorted({ln for ln, _ in hits})
            self.assertIn(4, lines, f"裸字符串常量那一行应命中：{hits}")

            sh = os.path.join(tmp, "fake.sh")
            with open(sh, "w", encoding="utf-8") as f:
                f.write('# exec "$PY" -m yiban.cli sign\n'
                        'exec "$PY" -m yiban.cli sign --fallback\n')
            hits = old_coordinate_hits(sh)
            self.assertEqual(len(hits), 1, f"shell 面应只命中非注释那一行：{hits}")
            self.assertEqual(hits[0][0], 2, f"命中行应为第 2 行：{hits}")

        # 145-D 的缺口：**别名导入**（无点号）必须也能被看见。
        with tempfile.TemporaryDirectory(prefix="oldcli-alias-") as tmp:
            sh = os.path.join(tmp, "alias.sh")
            with open(sh, "w", encoding="utf-8") as f:
                f.write('# 注释里的 from yiban import cli 不算\n'
                        'run_py "from yiban import cli; cli.main([\'egress\'])"\n'
                        'run_py "from yiban.entry import cli; cli.main([\'egress\'])"\n')
            hits = old_coordinate_hits(sh)
            self.assertEqual([ln for ln, _ in hits], [2],
                             f"只应命中旧坐标别名导入那一行（新坐标不算）：{hits}")

            py = os.path.join(tmp, "alias.py")
            with open(py, "w", encoding="utf-8") as f:
                f.write('"""docstring 里的 from yiban import cli 不算启动点。"""\n'
                        'CMD = "import sys; from yiban import cli; sys.exit(0)"\n'
                        'OK = "import sys; from yiban.entry import cli; sys.exit(0)"\n'
                        '# 注释里的 from yiban import cli 不算\n')
            hits = old_coordinate_hits(py)
            self.assertEqual([ln for ln, _ in hits], [2],
                             f"只应命中字符串常量里的旧坐标别名导入：{hits}")

            flag = os.path.join(tmp, "flag.sh")
            with open(flag, "w", encoding="utf-8") as f:
                f.write('egress --cli status\n'
                        'from yiban import clogger\n')
            self.assertEqual(old_coordinate_hits(flag), [],
                             "`--cli` 旗标与同前缀他模块（clogger）不得误红")

        # 独立审查抓出的阻塞洞：运行期 `.py` 里的**真 import 语句**也必须被抓到。
        with tempfile.TemporaryDirectory(prefix="oldcli-import-") as tmp:
            for name, body, want in (
                ("imp.py", "import yiban.cli\n", 1),
                ("fromimp.py", "from yiban import cli\n", 1),
                ("subimp.py", "from yiban.cli import main\n", 1),
                ("attr.py", "import yiban\nyiban.cli.main([])\n", 1),
                ("new.py", "from yiban.entry import cli\ncli.main([])\n", 0),
                ("other.py", "from yiban import client\n", 0),
            ):
                p = os.path.join(tmp, name)
                with open(p, "w", encoding="utf-8") as f:
                    f.write(body)
                hits = old_coordinate_hits(p)
                if want:
                    self.assertTrue(hits, f"{name}：真 import 旧坐标却未命中（门有洞）")
                else:
                    self.assertEqual(hits, [], f"{name}：新坐标/同前缀被误红：{hits}")

            clean = os.path.join(tmp, "clean.py")
            with open(clean, "w", encoding="utf-8") as f:
                f.write('"""-m yiban.cli 与 yiban.cli 都只是历史叙述。"""\n'
                        '# -m yiban.cli\n'
                        'X = 1\n')
            self.assertEqual(old_coordinate_hits(clean), [], "注释/docstring 被误判成启动点")


# ---------------------------------------------------------------------------
# 拆墙/壳退役 T8：测试面不得再以旧坐标 `yiban.cli` 作 import 路径或打桩目标
# ---------------------------------------------------------------------------
# 来历（壳退役批 `145` 的 `T8` 片）：`yiban/cli.py` 是 `__getattr__` 型
# 转发壳。经它读属性仍是同一对象，故 `from yiban import cli` 能跑、不报错；但
# `mock.patch("yiban.cli.X")` 只改壳的属性，**不改实现的模块全局**——打桩成功、
# 不控被测路径。这是本片最危险的形状：**静默失效**（不报错，但测试不再控制被测路径）。
# 故测试面必须用新坐标 `yiban.entry.cli`。本门钉：`tests/**` 里旧坐标的 import
# 路径与打桩目标为零。
#
# 豁免（唯一两处，均为**验证壳/禁壳的守卫本身**）：
#   `tests/test_entry_cli_relocation.py`——验证壳转发同一对象（`145-F2` 删）。
#   `tests/test_deploy_entry_imports.py`——本文件，`OLD_CLI_MODULE` 是禁字面量，
#     夹具字符串用于自证本门有牙。
# 豁免按**文件**给：这两个文件的职责就是引用旧坐标。清单是白名单，不是跳过——
# 未登记的文件命中即红并点名 `文件:行`。
TESTS_FACE_EXEMPT = frozenset((
    "tests/test_entry_cli_relocation.py",
    "tests/test_deploy_entry_imports.py",
))


def _tests_face_hits(path):
    """`tests/**` 里引用旧坐标的位置：import 路径 + 非 docstring 字符串常量。

    字符串判据用「前缀 + 非标识符后继」：`yiban.cli` 与 `yiban.cli.main` 都命中，
    `yiban.client` 不命中（`e` 是标识符后继）。

    已知盲点：用变量拼接的目标（如 `patch(OLD_CLI_MODULE + ".main")`）不命中——
    本扫描只看 import 语句与字面量。当前仓内无此形态。
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    tree = ast.parse(text)
    docs = _docstring_const_ids(tree)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == OLD_CLI_MODULE or a.name.startswith(OLD_CLI_MODULE + "."):
                    hits.append((node.lineno, "import 旧坐标 " + a.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == OLD_CLI_MODULE or mod.startswith(OLD_CLI_MODULE + "."):
                hits.append((node.lineno, "from 旧坐标 import " + mod))
            elif mod == "yiban":
                for a in node.names:
                    if a.name == "cli":
                        hits.append((node.lineno, "from yiban import cli（旧坐标别名）"))
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docs
              and (node.value == OLD_CLI_MODULE
                   or node.value.startswith(OLD_CLI_MODULE + "."))):
            hits.append((node.lineno, "非 docstring 字符串常量 " + node.value))
    return sorted(set(hits))


class TestsFaceCliCoordinateGateTest(unittest.TestCase):
    """T8：测试面里旧坐标 `yiban.cli` 的 import 路径与打桩目标为零。"""

    def _tests_py_files(self):
        out = []
        for dirpath, dirs, files in os.walk(os.path.join(BASE, "tests")):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            for name in sorted(files):
                if name.endswith(".py"):
                    out.append(os.path.join(dirpath, name))
        return out

    def test_no_tests_face_uses_old_coordinate(self):
        files = self._tests_py_files()
        self.assertGreaterEqual(
            len(files), 100,
            f"扫描面过小（{len(files)} 个文件）——扫描根写错或测试被移走（扫空不得当绿）")
        bad = []
        for path in files:
            rel = os.path.relpath(path, BASE).replace("\\", "/")
            if rel in TESTS_FACE_EXEMPT:
                continue
            for line, why in _tests_face_hits(path):
                bad.append(f"{rel}:{line}: {why}")
        self.assertEqual(
            bad, [],
            "测试面仍以旧坐标 `yiban.cli` 作 import 路径或打桩目标"
            "（打桩会静默失效、壳退役后 import 会断）：\n" + "\n".join(bad))

    def test_exempt_files_still_exist(self):
        """豁免清单不得腐烂：登记的文件必须还在（否则豁免面静默变大）。"""
        for rel in sorted(TESTS_FACE_EXEMPT):
            self.assertTrue(os.path.isfile(os.path.join(BASE, rel)),
                            f"豁免清单登记了不存在的文件 {rel}")

    def test_scanner_has_teeth(self):
        """突变验证：塞回一处旧坐标 ⇒ 红；注释/docstring 提及 ⇒ 不误红。"""
        with tempfile.TemporaryDirectory(prefix="oldcli-tests-gate-") as tmp:
            py = os.path.join(tmp, "fake_test.py")
            with open(py, "w", encoding="utf-8") as f:
                f.write('"""历史叙述：-m yiban.cli 不算命中。"""\n'
                        'from yiban import cli\n')
            hits = _tests_face_hits(py)
            self.assertTrue(hits, "塞回旧坐标 import 却未命中：本门没有牙")
            self.assertEqual(hits[0][0], 2, f"命中行应为第 2 行：{hits}")

            py2 = os.path.join(tmp, "fake_patch.py")
            with open(py2, "w", encoding="utf-8") as f:
                f.write('from unittest import mock\n'
                        'mock.patch("yiban.cli.main")\n')
            hits = _tests_face_hits(py2)
            self.assertTrue(hits, "旧坐标打桩目标（带点后缀）未命中：本门没有牙")
            self.assertEqual(hits[0][0], 2, f"命中行应为第 2 行：{hits}")

            py3 = os.path.join(tmp, "fake_client.py")
            with open(py3, "w", encoding="utf-8") as f:
                f.write('from yiban import client\n'
                        'X = "yiban.client"\n')
            self.assertEqual(_tests_face_hits(py3), [],
                             "`yiban.client` 被误判成旧坐标（同前缀不同模块）")


# ---------------------------------------------------------------------------
# 拆墙/壳退役 T1：口令哈希两名不得再经 web.app 墙坐标读或被它打桩
# ---------------------------------------------------------------------------
# 来历（145-B `T1` 片）：`generate_password_hash` / `check_password_hash` 是
# `web/app.py` 从 `werkzeug.security` 再导出的墙名——路由经 `m.<名字>` 取、测试经
# `self.webapp.<名字>` 取。T1 把读者迁到真实实现坐标：路由与测试各自
# `from werkzeug.security import …` 后按**裸名**调用。**墙名本片不删**（删名归 `145-E`）。
#
# 本门钉三条不变式：
#   ① **自有面无墙坐标读者**（面＝`tests/**`、`web/**`、`scripts/**`）：这两个名字只许
#      以裸名出现，或经真实坐标表达式 `werkzeug.security.<名字>`；其余任何
#      `<基>.<名字>` 一律红并点名 `文件:行`。
#   ② **自有面无墙坐标打桩/读写目标**：`patch.object(<webapp 别名>, "<名字>")`、
#      `getattr`/`setattr`/`hasattr`/`delattr(<webapp 别名>, "<名字>")`、以及**字符串形态**
#      `patch("web.app.<名字>")` 一律红。为什么必须钉：墙名仍在，故打在它上面**会成功、
#      但不控制被测路径**——静默失效（用例看着绿，实际付真实 scrypt 开销、校验结果也不受控）。
#      本片实测修掉了 4 处这类打桩点（`tests/test_alert_channel_dispatch.py`、
#      `test_container_scheduler.py`、`test_notify_webhook.py`），改打真正读该绑定的模块
#      （`web.routes.auth` / `web.routes.me_api` / `web.security`）。
#   ③ **面外读者逐条登记**（面＝`frontend/**`）：前端 2026-10-18 起归用户独立完成，
#      本批不得改它，故它的残留**不判红、改为与登记表对账**（见
#      `_PWHASH_OUT_OF_FACE_REGISTRY`）：未登记 ⇒ 红；登记的次数与实际不符（多一处＝新增
#      残留、少一处或整条消失＝用户那边改掉了）⇒ 红。这条防的正是"自有面清零了、
#      面外还有活的读者，`145-E` 删墙名时 AttributeError 而无门会红"。
_PWHASH_WALL_NAMES = ("generate_password_hash", "check_password_hash")
_PWHASH_SCAN_DIRS = ("tests", "web", "scripts")
#: 面外目录：不归本片改，但残留必须登记。
_PWHASH_OUT_OF_FACE_DIRS = ("frontend",)
#: 扫描时跳过的目录名（`frontend/node_modules` 有一万六千个文件）。
_PWHASH_SKIP_DIRS = frozenset(("node_modules", "__pycache__", ".venv", "venv", "dist", "build"))

#: 真实实现坐标的模块表达式：唯一允许以属性形态出现的基。
_PWHASH_REAL_COORDINATE = "werkzeug.security"

#: 测试里承载 `web.app` 模块对象的别名形态（`webapp` / `self.webapp` /
#: `webapp_auditapi` 一族）。与 `tests/test_web_boundary.py` 的墙名对账门同口径。
_PWHASH_WALL_ALIAS = re.compile(r"(?:^|\.)webapp(?:_[A-Za-z0-9_]+)?$")

#: 墙坐标的模块路径字面量（字符串形态打桩目标 `patch("web.app.<名字>")` 的前缀）。
_PWHASH_WALL_MODULE = "web.app"

#: `frontend/**` 里仍读墙坐标的**待销账登记**（AGENTS §28：约束要么有门、要么显式豁免）。
#: 键＝`(相对路径, 名字)`，值＝`(命中次数, 处置说明)`。
#:
#: 为什么按 `(路径, 名字, 次数)` 而不把行号钉进判据：前端归用户独立完成，行号会随其
#: 无关改动漂移。把行号做成判据会让**用户的 PR 因一行位移而红**，而修法却落在我的文件里
#: ——那正是"门看的东西 ≠ 实际改的东西"（AGENTS §29）。按次数对账已能覆盖三件事：
#: 未登记的新读者 ⇒ 红；登记处多一处/少一处 ⇒ 红；整条消失 ⇒ 红（防腐烂）。
#: 红时消息里给出当前 `文件:行`，便于人定位。
#: 面外残留登记表。**2026-10-18 已清空**：唯一一条登记（`frontend/e2e/server.py` 读
#: `generate_password_hash`）经用户授权由主会话就地处置——该文件改直连 `werkzeug.security`。
#: 按判据第 3 条（"登记整条消失 ⇒ 红"），处置与销账必须同批，故同批清空。
#: **表空 ≠ 面空**：扫描面仍由 `_out_of_face_found` 断言非空（AGENTS §30 扫空不得当绿）。
_PWHASH_OUT_OF_FACE_REGISTRY = {}


def _pwhash_real_coordinate_bases(tree):
    """该文件里绑定到真实坐标模块 `werkzeug.security` 的基表达式集合。

    认三种写法：`import werkzeug.security`（基是 `werkzeug.security`）、
    `import werkzeug.security as ws`（基是 `ws`）、`from werkzeug import security`
    （基是 `security`）。按绑定解析而不是按名字猜，故同名的别的东西不会误放行。
    """
    out = {_PWHASH_REAL_COORDINATE}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "werkzeug.security":
                    out.add(a.asname or _PWHASH_REAL_COORDINATE)
                elif a.name == "werkzeug":
                    out.add((a.asname or "werkzeug") + ".security")
        elif isinstance(node, ast.ImportFrom) and node.module == "werkzeug":
            for a in node.names:
                if a.name == "security":
                    out.add(a.asname or "security")
    return out


def _pwhash_wall_string_target(value):
    """字符串形态的墙坐标目标 → 名字；不是该形态则返回 None。

    命中 `web.app.<名字>` 与 `<webapp 别名>.<名字>`（`mock.patch("web.app.check_password_hash")`
    这一形态旧版扫描器漏掉——两个门都漏，本版补上）。
    """
    for name in _PWHASH_WALL_NAMES:
        suffix = "." + name
        if not value.endswith(suffix):
            continue
        prefix = value[: -len(suffix)]
        if prefix == _PWHASH_WALL_MODULE or _PWHASH_WALL_ALIAS.search(prefix):
            return name
    return None


def pwhash_wall_hits(path):
    """返回该文件里经 web.app 墙坐标引用口令哈希两名的位置：`(行号, 名字, 说明)` 列表。

    四类命中：
      ① 属性读 —— `<基>.<名字>`，基表达式不是真实坐标 `werkzeug.security`（含其别名）；
      ② 打桩目标 —— `patch.object(<webapp 别名>, "<名字>")`；
      ③ 读写目标 —— `getattr`/`setattr`/`hasattr`/`delattr(<webapp 别名>, "<名字>")`；
      ④ 字符串形态 —— 非 docstring 字符串常量形如 `web.app.<名字>` 或 `<webapp 别名>.<名字>`。

    已知盲点（显式登记，AGENTS §28）：用变量拼接的目标（`patch.object(alias, NAME_VAR)`、
    `"web.app." + NAME`）不命中——本扫描只看字面量。当前仓内无此形态。
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    tree = ast.parse(text)
    real_bases = _pwhash_real_coordinate_bases(tree)
    docs = _docstring_const_ids(tree)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _PWHASH_WALL_NAMES:
            base = ast.unparse(node.value)
            if base not in real_bases:
                hits.append((node.lineno, node.attr, f"墙坐标属性读 {base}.{node.attr}"))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docs:
            name = _pwhash_wall_string_target(node.value)
            if name is not None:
                hits.append((node.lineno, name, f"墙坐标字符串目标 {node.value}"))
        if not isinstance(node, ast.Call) or not node.args:
            continue
        fn = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if fn in ("patch", "object"):
            if not _PWHASH_WALL_ALIAS.search(ast.unparse(node.args[0])):
                continue
            last = node.args[-1]
            if isinstance(last, ast.Constant) and last.value in _PWHASH_WALL_NAMES:
                hits.append((node.lineno, last.value,
                             f"墙坐标打桩目标 {ast.unparse(node.args[0])}.{last.value}"))
        elif fn in ("getattr", "setattr", "hasattr", "delattr") and len(node.args) >= 2:
            if not _PWHASH_WALL_ALIAS.search(ast.unparse(node.args[0])):
                continue
            last = node.args[1]
            if isinstance(last, ast.Constant) and last.value in _PWHASH_WALL_NAMES:
                hits.append((node.lineno, last.value,
                             f"墙坐标 {fn} 目标 {ast.unparse(node.args[0])}.{last.value}"))
    return sorted(set(hits))


def pwhash_out_of_face_problems(found, registry=None, where=None):
    """面外残留与登记表对账（纯函数，便于突变自证）：返回问题清单（空＝一致）。

    `found` ＝ `{(相对路径, 名字): 命中次数}`；`where` ＝ 同键的 `[行号]`（仅用于消息）。
    三条判据（AGENTS §28/§29）：
      · 未登记的新读者 ⇒ 红（`145-E` 删墙名时会断，而它以为读者已清零）；
      · 登记的次数与实际不符 ⇒ 红（多一处＝新增残留；少一处＝已处置一半）；
      · 登记整条消失 ⇒ 红（用户那边已处置，必须同批销账，否则登记变成僵尸）。
    """
    registry = _PWHASH_OUT_OF_FACE_REGISTRY if registry is None else registry
    where = where or {}
    problems = []
    for key, n in sorted(found.items()):
        entry = registry.get(key)
        if entry is None:
            problems.append(
                f"{key[0]}:{where.get(key)} 出现未登记的墙坐标读者 {key[1]}（{n} 处）——"
                "要么迁到 `werkzeug.security`，要么在 _PWHASH_OUT_OF_FACE_REGISTRY 登记")
        elif n != entry[0]:
            problems.append(
                f"{key[0]}:{where.get(key)} 的墙坐标读者 {key[1]} 实测 {n} 处、登记 {entry[0]} 处"
                "——两侧必须一致（新增残留不许漏登记；已处置的必须同批销账）")
    for key, (n, _why) in sorted(registry.items()):
        if key not in found:
            problems.append(
                f"登记 {key[0]} 的 {key[1]}（{n} 处）已不在代码里——"
                "用户那边已处置，请同批删掉这条登记（防登记变成僵尸）")
    return problems


class PwhashWallCoordinateGateTest(unittest.TestCase):
    """T1：口令哈希两名在自有面里不得再经 web.app 墙坐标读或被它打桩；面外残留须登记。"""

    def _scan_files(self, dirs=_PWHASH_SCAN_DIRS):
        out = []
        for d in dirs:
            for dirpath, dirs_, files in os.walk(os.path.join(BASE, d)):
                dirs_[:] = [x for x in dirs_ if x not in _PWHASH_SKIP_DIRS]
                for name in sorted(files):
                    if name.endswith(".py"):
                        out.append(os.path.join(dirpath, name))
        return out

    def _out_of_face_found(self):
        files = self._scan_files(_PWHASH_OUT_OF_FACE_DIRS)
        self.assertGreaterEqual(
            len(files), 1,
            f"面外扫描面为空（{len(files)} 个文件）——扫描根写错或前端被移走（扫空不得当绿）")
        found, where = {}, {}
        for path in files:
            rel = os.path.relpath(path, BASE).replace("\\", "/")
            for line, name, _why in pwhash_wall_hits(path):
                found[(rel, name)] = found.get((rel, name), 0) + 1
                where.setdefault((rel, name), []).append(line)
        return found, where

    def test_no_wall_coordinate_reader_or_patch_target(self):
        """不变式 ①②：自有面（`tests`＋`web`＋`scripts`）零命中。"""
        files = self._scan_files()
        self.assertGreaterEqual(
            len(files), 200,
            f"扫描面过小（{len(files)} 个文件）——扫描根写错或读者被移走（扫空不得当绿）")
        bad = []
        for path in files:
            rel = os.path.relpath(path, BASE).replace("\\", "/")
            for line, _name, why in pwhash_wall_hits(path):
                bad.append(f"{rel}:{line}: {why}")
        self.assertEqual(
            bad, [],
            "口令哈希两名仍经 web.app 墙坐标读或被它打桩（打桩会静默失效）：\n" + "\n".join(bad))

    def test_out_of_face_readers_match_registry(self):
        """不变式 ③：面外（`frontend/**`）的残留与登记表逐条对账，两侧都不许静默。"""
        found, where = self._out_of_face_found()
        self.assertEqual(pwhash_out_of_face_problems(found, where=where), [],
                         "面外墙坐标残留与登记表不一致：\n"
                         + "\n".join(pwhash_out_of_face_problems(found, where=where)))

    def test_registry_reconciliation_has_teeth(self):
        """突变验证（合成输入）：未登记 ⇒ 红；次数不符 ⇒ 红；登记消失 ⇒ 红；一致 ⇒ 绿。"""
        reg = {("frontend/e2e/server.py", "generate_password_hash"): (1, "登记理由")}
        key = ("frontend/e2e/server.py", "generate_password_hash")
        self.assertEqual(pwhash_out_of_face_problems({key: 1}, registry=reg), [],
                         "一致时不该报问题（否则三条判据可能是一组恒真断言）")
        extra = {key: 1, ("frontend/e2e/other.py", "check_password_hash"): 1}
        self.assertTrue(pwhash_out_of_face_problems(extra, registry=reg),
                        "未登记的墙坐标读者未判红：本门没有牙")
        self.assertTrue(pwhash_out_of_face_problems({key: 2}, registry=reg),
                        "命中次数多于登记未判红：新增残留会被静默放过")
        self.assertTrue(pwhash_out_of_face_problems({}, registry=reg),
                        "登记已消失（用户那边改掉了）未判红：登记会变成僵尸")

    def test_scanner_has_teeth(self):
        """突变验证：塞回四种墙坐标形态 ⇒ 红；真实坐标/裸名/注释/`web_security` ⇒ 不误红。"""
        with tempfile.TemporaryDirectory(prefix="pwhash-gate-") as tmp:
            wall = os.path.join(tmp, "fake_wall_reader.py")
            with open(wall, "w", encoding="utf-8") as f:
                f.write('"""docstring 提 generate_password_hash 不算命中。"""\n'
                        'from unittest import mock\n'
                        'H = self.webapp.generate_password_hash\n'          # ① 属性读 ⇒ 命中
                        'mock.patch.object(self.webapp, "check_password_hash")\n'  # ② 打桩 ⇒ 命中
                        'mock.patch("web.app.check_password_hash")\n'      # ④ 字符串 ⇒ 命中
                        'getattr(self.webapp, "generate_password_hash")\n'  # ③ getattr ⇒ 命中
                        '# 注释里的 self.webapp.check_password_hash 不算\n')
            hits = pwhash_wall_hits(wall)
            lines = sorted({ln for ln, _, _ in hits})
            for want, what in ((3, "墙坐标属性读"), (4, "墙坐标打桩目标"),
                               (5, "墙坐标字符串目标"), (6, "墙坐标 getattr 目标")):
                self.assertIn(want, lines, f"{what}那一行应命中：{hits}")
            self.assertEqual(
                sorted({name for _, name, _ in hits}),
                ["check_password_hash", "generate_password_hash"],
                f"命中名字应逐条带出：{hits}")

            route = os.path.join(tmp, "fake_route.py")
            with open(route, "w", encoding="utf-8") as f:
                f.write('def v():\n'
                        '    m = _appmod()\n'
                        '    return m.check_password_hash("h", "p")\n')      # 路由形态 ⇒ 命中
            self.assertTrue(pwhash_wall_hits(route), "路由 `m.<名字>` 形态未命中：本门没有牙")

            clean = os.path.join(tmp, "clean.py")
            with open(clean, "w", encoding="utf-8") as f:
                f.write('"""真实坐标与裸名都合规。"""\n'
                        'from werkzeug import security\n'
                        'from werkzeug.security import check_password_hash\n'
                        'A = security.generate_password_hash("p")\n'
                        'B = check_password_hash("h", "p")\n'
                        'C = "check_password_hash"\n'                        # 裸名常量不算
                        'mock.patch("werkzeug.security.check_password_hash")\n'  # 真实坐标不算
                        '# self.webapp.generate_password_hash 只是注释\n')
            self.assertEqual(pwhash_wall_hits(clean), [],
                             "真实坐标 `werkzeug.security.<名字>`、裸名或裸名常量被误判成墙坐标")

            other = os.path.join(tmp, "fake_other.py")
            with open(other, "w", encoding="utf-8") as f:
                f.write('from unittest import mock\n'
                        'import web.security as web_security\n'
                        'mock.patch.object(web_security, "check_password_hash")\n'
                        'mock.patch("web.security.check_password_hash")\n'
                        'getattr(web_security, "generate_password_hash")\n')
            self.assertEqual(pwhash_wall_hits(other), [],
                             "`web_security` / `web.security` 被误判成 web.app 墙坐标"
                             "（两者不是同一模块）")


# ---------------------------------------------------------------------------
# 拆墙/壳退役 T2：`yiban.clock` 的读者必须迁到 `yiban.core.clock`
# ---------------------------------------------------------------------------
# 来历（145-B `T2` 片）：`yiban/clock.py` 是 `sys.modules` 替换型转发壳，实现已搬
# `yiban/core/clock.py`（批 B）。`yiban/**` 的 28 个生产件**当时就迁完了**，剩下
# 自有面 **77 个文件**仍走旧坐标：`tests/**` 58 ＋ `web/**` 9 ＋ `scripts/**` 8
# （5 个 .py ＋ 3 个 .sh）＋ `docker/**` 1 ＋ `run.sh` 1。
# 口径（2026-10-18 本片接手时在 `2f2709b0` 上现量）：**含 import 路径或运行期代码串
# 的文件数**，不含只在 docstring／注释里提旧坐标的文件（那些不计入，见下"注释不算"）。
# 另一口径（同一底本，现量）：import 路径**处数** 74（`from yiban import clock` 71 ＋
# `from yiban.clock import X` 3）；shell 非注释行 4；串内旧坐标 12（其中**串内别名**
# 2 处是首版判据漏检的形态，见不变式①末段）。
#
# **本片只迁坐标，不动任何取时逻辑。** clock 是业务时间唯一来源（固定 +8）：宿主是
# UTC 时若某处改走 `datetime.now()`，窗口闸门会把"还没开始"判成"已结束"，当天全部
# 账号零请求（历史缺陷）。故本门只钉"坐标不得是旧的那个"，不碰 `now()` 的实现。
#
# 本门钉三条不变式：
#   ① **自有面零命中**：面＝`yiban/**`、`web/**`（非 `web/static/**`）、`scripts/**`、
#      `docker/**`、`run.sh`、`tests/**`。旧坐标 `yiban.clock` 不得再以 import 路径、
#      别名形态 `from yiban import clock`、属性访问 `yiban.clock.X`、或非 docstring
#      字符串常量（含打桩目标 `mock.patch("yiban.clock.now")`、`python -c` 代码串
#      里的 `"from yiban import clock"`）出现。
#      为什么必须钉：壳仍在，故 `from yiban import clock` **能跑**、字符串打桩
#      **能成功**；但 `145-E` 删壳后 import 直接 `ModuleNotFoundError`，而字符串打桩
#      目标会**静默失效**（不报错，只不再控制被测路径）。
#      为什么单列"串内别名"：首版判据只按字面量 `yiban.clock` 扫串，漏掉
#      `"from yiban import clock;print(clock.ts())"` 这类**运行期代码串**——本片接手
#      实测两处真实残留（`scripts/drill_multi_exec.py` 的子进程哨兵串、
#      `tests/test_cli_egress_reset.py` 的 `-c` 串）正是此形态，而门当时是绿的。
#      ⇒ 门的覆盖面必须等于改动面（AGENTS §十三 形状③）。
#   ② **非 Python 载体的非注释行同样零命中**：`run.sh` 与 `scripts/*.sh` 里的
#      `python -c "… from yiban.clock import today …"` 是**运行期硬依赖**（业务日唯一
#      来源，MF-109）。壳删后这四处会让按天文件名静默错位。
#   ③ **面外读者逐条登记**（面＝`frontend/**`，2026-10-18 起归用户独立完成）。
#
# 豁免（按**文件**给，不是跳过；未登记的文件命中即红并点名 `文件:行`）：
#   `tests/test_batch_b_relocation.py`——它的职责就是验证壳转发同一对象（`145-F2` 删）。
#   `tests/test_import_linter_gate.py`——`yiban.clock` 是它的**禁字面量**：该用例断言
#     import-linter 契约的 `forbidden_modules` **不许**列出迁移期坐标（列了契约会空转
#     退 2）。把这条字面量改成新坐标会**反转语义**（新坐标是永久坐标，契约列它是合法的）。
#   `tests/test_provenance_guard.py`——`yiban.clock` 是它的**禁前缀**（`145-T3` 审查 F2
#     把它补回前缀表，与 `yiban.core.clock` 并列）：壳 `yiban/clock.py` 仍在，只留新坐标
#     会让该常驻守卫漏拦旧路径。删壳后是否收窄归 `145-E`。
#   `tests/test_deploy_entry_imports.py`——本文件，`_T2_OLD_MODULE` 是禁字面量，
#     夹具字符串用于自证本门有牙。
_T2_OLD_MODULE = "yiban.clock"
_T2_NEW_MODULE = "yiban.core.clock"
#: 自有面（不含面外 `frontend/**`）。
_T2_OWN_FACE_PY_DIRS = ("yiban", "web", "scripts", "docker", "tests")
_T2_OWN_FACE_SHELL = ("run.sh",)
_T2_SKIP_DIRS = frozenset(("node_modules", "__pycache__", ".venv", "venv", "dist", "build"))
#: 自有面里不归本批改的子树（`web/static` 是前端产物，2026-10-18 起归用户）。
_T2_SKIP_REL_PREFIX = ("web/static/",)
_T2_FACE_EXEMPT = frozenset((
    "tests/test_batch_b_relocation.py",
    "tests/test_import_linter_gate.py",
    "tests/test_provenance_guard.py",
    "tests/test_deploy_entry_imports.py",
))
#: 面外（`frontend/**`）仍读旧坐标的**待销账登记**。键＝(相对路径, 名字)，值＝(次数, 说明)。
#: **2026-10-18 已清空**：唯一一条登记（`frontend/e2e/server.py` 以旧坐标导入 clock）经用户
#: 授权由主会话就地处置——该文件改 `from yiban.core import clock`。按判据"登记整条消失 ⇒ 红"，
#: 处置与销账必须同批，故同批清空。**表空 ≠ 面空**：扫描面仍由用例断言非空（§30）。
_T2_OUT_OF_FACE_REGISTRY = {}

#: 旧坐标的字面量判据：`yiban.clock` 后面不得紧跟标识符字符。
#: 故 `yiban.clock` / `yiban.clock.now` / `*yiban.clock*` 命中；
#: `yiban.clock_meta`（`yiban.store` 的另一模块）、`yiban.client` 不命中。
_T2_OLD_LITERAL = re.compile(r"(?<![\w.])" + re.escape(_T2_OLD_MODULE) + r"(?![\w])")

#: 别名形态**在文本里**的判据：`from yiban import … clock …`。
#: 为什么单列：字面量判据要求串里出现 `yiban.clock` 这个点号形态，抓不到
#: `"from yiban import clock;print(clock.ts())"` 这类**运行期代码串**（`python -c`
#: 的实参、子进程脚本常量）。这类串在壳退役后同样 `ModuleNotFoundError`，且
#: 字符串打桩不覆盖它们——本片实测两处真实残留正是此形态（首版判据漏检）。
#: `\bclock\b` 不误命中 `clock_meta`（`k` 与 `_` 之间无词边界）。
#: `[^\n#]` 限制在同一行且不跨注释，`from yiban.core import clock` 因 `yiban` 后
#: 不是空白而不命中。
_T2_OLD_ALIAS_IN_TEXT = re.compile(r"\bfrom\s+yiban\s+import\s+[^\n#]*\bclock\b")


def _t2_import_hits(tree):
    """该文件里以旧坐标作 import 路径的位置：`(行号, 说明)` 列表。

    三种写法：`import yiban.clock`、`from yiban.clock import X`、
    `from yiban import clock`（别名形态，是主流形态）。
    口径（**同出 T2 迁移前底本 `2f2709b0`**）：别名形态 `from yiban import clock`
    自有面 **71** 处 ＋ 面外 `frontend/e2e/server.py` **1** 处 ＝ **全仓 72** 处；
    `import yiban.clock` 0 处、`from yiban.clock import X` 3 处。
    **当前（2026-10-18 本批后）全仓别名读者 0 处**：自有面早随 T2 迁完，面外那 1 处由
    本批经用户授权处置。**两组数不许混用、不许相减**（AGENTS §31）。
    第四种：`import yiban` 后按属性访问 `yiban.clock.now()`——字面量判据抓不到它
    （那判据只看字符串），故在此按 `ast.Attribute` 认。
    """
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == _T2_OLD_MODULE or a.name.startswith(_T2_OLD_MODULE + "."):
                    hits.append((node.lineno, "import 旧坐标 " + a.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == _T2_OLD_MODULE or mod.startswith(_T2_OLD_MODULE + "."):
                hits.append((node.lineno, "from 旧坐标 import " + mod))
            elif mod == "yiban":
                for a in node.names:
                    if a.name == "clock":
                        hits.append((node.lineno, "from yiban import clock（旧坐标别名）"))
        elif isinstance(node, ast.Attribute) and node.attr == "clock":
            root = node.value
            if isinstance(root, ast.Name) and root.id == "yiban":
                hits.append((node.lineno, "属性访问 yiban.clock（旧坐标）"))
    return hits


def t2_old_coordinate_hits(path):
    """返回该文件里引用旧坐标的位置：`(行号, 说明)` 列表。

    Python：① 旧坐标 import（四种写法，含 `yiban.clock.X` 属性访问）；
    ② 非 docstring 字符串常量含旧坐标（覆盖打桩目标 `"yiban.clock.now"`、
    `-c` 代码串、`*yiban.clock*` 形态）；③ 字符串里的别名形态
    `"from yiban import clock…"`（运行期代码串，首版判据漏检的形态）。
    Shell：**非注释行**含旧坐标（字面量或别名形态）。
    注释与 docstring 不算（任务口径：历史叙述不误红）。
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if path.endswith((".sh", ".bash")) or path.endswith("Dockerfile"):
        hits = []
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if _T2_OLD_LITERAL.search(s):
                hits.append((i, "shell/文本非注释行含 " + _T2_OLD_MODULE))
            elif _T2_OLD_ALIAS_IN_TEXT.search(s):
                hits.append((i, "shell/文本非注释行含 from yiban import clock"))
        return hits
    tree = ast.parse(text)
    docs = _docstring_const_ids(tree)
    hits = list(_t2_import_hits(tree))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs):
            continue
        if _T2_OLD_LITERAL.search(node.value):
            hits.append((node.lineno, "非 docstring 字符串常量 " + node.value[:60]))
        elif _T2_OLD_ALIAS_IN_TEXT.search(node.value):
            hits.append((node.lineno, "非 docstring 字符串含别名形态 "
                         + node.value[:60]))
    return sorted(set(hits))


def t2_own_face_files(root):
    """自有面的扫描文件清单（.py ＋ .sh ＋ Dockerfile*），按 §7.2 的范围。"""
    out = []
    for d in _T2_OWN_FACE_PY_DIRS:
        for dirpath, dirs, files in os.walk(os.path.join(root, d)):
            dirs[:] = [x for x in dirs if x not in _T2_SKIP_DIRS]
            for name in sorted(files):
                if not name.endswith((".py", ".sh", ".bash")) and not name.startswith("Dockerfile"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), root).replace("\\", "/")
                if rel.startswith(_T2_SKIP_REL_PREFIX):
                    continue
                out.append(os.path.join(dirpath, name))
    for name in _T2_OWN_FACE_SHELL:
        path = os.path.join(root, name)
        if os.path.exists(path):
            out.append(path)
    return out


def t2_out_of_face_files(root):
    """面外（`frontend/**`）的扫描文件清单（.py ＋ .ts）。"""
    out = []
    for dirpath, dirs, files in os.walk(os.path.join(root, "frontend")):
        dirs[:] = [x for x in dirs if x not in _T2_SKIP_DIRS]
        for name in sorted(files):
            if name.endswith(".py"):
                out.append(os.path.join(dirpath, name))
    return out


def t2_out_of_face_problems(found, registry=None, where=None):
    """面外残留与登记表对账（纯函数，便于突变自证）：返回问题清单（空＝一致）。

    与 `pwhash_out_of_face_problems` 同口径：未登记 ⇒ 红；次数不符 ⇒ 红；整条消失 ⇒ 红。
    """
    registry = _T2_OUT_OF_FACE_REGISTRY if registry is None else registry
    where = where or {}
    problems = []
    for key, n in sorted(found.items()):
        entry = registry.get(key)
        if entry is None:
            problems.append(
                f"{key[0]}:{where.get(key)} 出现未登记的面外旧坐标读者 {key[1]}（{n} 处）——"
                "要么迁到 `yiban.core.clock`，要么在 _T2_OUT_OF_FACE_REGISTRY 登记")
        elif n != entry[0]:
            problems.append(
                f"{key[0]}:{where.get(key)} 的旧坐标读者实测 {n} 处、登记 {entry[0]} 处"
                "——两侧必须一致（新增残留不许漏登记；已处置的必须同批销账）")
    for key, (n, _why) in sorted(registry.items()):
        if key not in found:
            problems.append(
                f"登记 {key[0]} 的 {key[1]}（{n} 处）已不在代码里——"
                "前端那边已处置，请同批删掉这条登记（防登记变成僵尸）")
    return problems


class ClockCoordinateGateTest(unittest.TestCase):
    """T2：`yiban.clock` 的自有面读者清零；非 Python 载体零命中；面外残留须登记。"""

    def _found_in(self, paths):
        found, where = {}, {}
        for path in paths:
            rel = os.path.relpath(path, BASE).replace("\\", "/")
            hits = t2_old_coordinate_hits(path)
            if hits:
                found[(rel, _T2_OLD_MODULE)] = len(hits)
                where[(rel, _T2_OLD_MODULE)] = [ln for ln, _ in hits]
        return found, where

    def test_own_face_has_no_old_coordinate(self):
        files = t2_own_face_files(BASE)
        self.assertGreaterEqual(
            len(files), 400,
            f"自有面扫描面过小（{len(files)} 个文件）——扫描根写错或树被移走（扫空不得当绿）")
        bad = []
        for path in files:
            rel = os.path.relpath(path, BASE).replace("\\", "/")
            if rel in _T2_FACE_EXEMPT:
                continue
            for line, why in t2_old_coordinate_hits(path):
                bad.append(f"{rel}:{line}: {why}")
        self.assertEqual(
            bad, [],
            "自有面仍以旧坐标 `yiban.clock` 作 import 路径或字符串目标"
            "（壳退役后 import 会断、字符串打桩会静默失效）：\n" + "\n".join(bad))

    def test_no_patch_target_uses_old_coordinate(self):
        """打桩面单列一条：`mock.patch("<旧坐标>.…")` 是静默失效的典型形态。"""
        bad = []
        files = t2_own_face_files(BASE)
        self.assertGreaterEqual(len(files), 400, f"扫描面过小（{len(files)}）")
        for path in files:
            rel = os.path.relpath(path, BASE).replace("\\", "/")
            if not rel.endswith(".py") or rel in _T2_FACE_EXEMPT:
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            tree = ast.parse(text)
            docs = _docstring_const_ids(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if fname not in ("patch", "object"):
                    continue
                for a in node.args:
                    if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                            and id(a) not in docs
                            and _T2_OLD_LITERAL.search(a.value)):
                        bad.append(f"{rel}:{node.lineno}: 打桩目标 {a.value}")
        self.assertEqual(
            bad, [],
            "打桩目标仍指旧坐标 `yiban.clock`——壳退役后会静默失效（打桩成功但不控被测路径）：\n"
            + "\n".join(bad))

    def test_exempt_files_still_exist(self):
        """豁免清单不得腐烂：登记的文件必须还在，且**必须仍命中本门**。

        `145-T3` 审查 F2 把 `tests/test_provenance_guard.py` 加进本清单（该文件补回
        `yiban.clock` 禁前缀）。加清单这一动作本身需要守卫：豁免一旦失去理由（文件里
        已无 `yiban.clock`），留着它会静默放开该文件的扫描面。故同时钉"仍命中"。
        """
        for rel in sorted(_T2_FACE_EXEMPT):
            path = os.path.join(BASE, rel)
            self.assertTrue(os.path.isfile(path),
                            f"豁免清单登记了不存在的文件 {rel}")
            self.assertTrue(
                t2_old_coordinate_hits(path),
                f"豁免文件 {rel} 已不再命中本门——豁免失去理由，须同批删除该豁免")

    def test_out_of_face_registry_reconciles(self):
        """面外（`frontend/**`）的残留与登记表逐条对账，两侧都不许静默。"""
        files = t2_out_of_face_files(BASE)
        self.assertGreaterEqual(
            len(files), 1,
            f"面外扫描面为空（{len(files)} 个文件）——扫描根写错或前端被移走（扫空不得当绿）")
        found, where = self._found_in(files)
        problems = t2_out_of_face_problems(found, where=where)
        self.assertEqual(problems, [], "面外旧坐标残留与登记表不一致：\n" + "\n".join(problems))

    def test_scanner_has_teeth(self):
        """突变验证：塞回八种形态 ⇒ 红；docstring/注释/同前缀他模块 ⇒ 不误红。"""
        with tempfile.TemporaryDirectory(prefix="clock-gate-") as tmp:
            py = os.path.join(tmp, "fake_test.py")
            with open(py, "w", encoding="utf-8") as f:
                f.write('"""docstring 提 yiban.clock 不算命中。"""\n'
                        'from yiban import clock\n'                    # ① 别名 ⇒ 命中
                        'from yiban.clock import today\n'              # ② 旧坐标 ⇒ 命中
                        'from unittest import mock\n'
                        'mock.patch("yiban.clock.now")\n'              # ③ 打桩串 ⇒ 命中
                        'P = "*yiban.clock*"\n'                        # ④ 嵌入形态 ⇒ 命中
                        'CODE = "from yiban import clock;print(clock.ts())"\n'  # ⑤ 串内别名 ⇒ 命中
                        'PROBE = """from yiban import clock as _c"""\n'  # ⑥ 多行串内别名 ⇒ 命中
                        'import yiban\n'
                        'yiban.clock.now()\n'                          # ⑦ 属性访问 ⇒ 命中
                        '# 注释里的 yiban.clock 不算\n'
                        'import yiban.clock_meta\n')                   # ⑧ 同前缀他模块 ⇒ 不命中
            hits = t2_old_coordinate_hits(py)
            lines = sorted({ln for ln, _ in hits})
            for want in (2, 3, 5, 6, 7, 8, 10):
                self.assertIn(want, lines, f"第 {want} 行应命中：{hits}")
            self.assertNotIn(12, lines,
                             f"第 12 行 `yiban.clock_meta` 不该命中（同前缀他模块）：{hits}")

            clean = os.path.join(tmp, "clean.py")
            with open(clean, "w", encoding="utf-8") as f:
                f.write('"""历史叙述：yiban.clock 迁到 yiban.core.clock。"""\n'
                        'from yiban.core import clock\n'
                        'from yiban.core.clock import now\n'
                        'import yiban.adapters.persistence.clock_meta as cm\n'
                        'from yiban import client\n'
                        'A = "yiban.core.clock.now"\n'
                        'B = "yiban.clock_meta"\n'
                        'C = "yiban.client"\n'
                        'D = "from yiban.core import clock"\n'
                        'E = "from yiban import clock_meta"\n')
            self.assertEqual(
                t2_old_coordinate_hits(clean), [],
                "新坐标、`yiban.clock_meta`、`yiban.client`、docstring 被误判成旧坐标")

            sh = os.path.join(tmp, "fake.sh")
            with open(sh, "w", encoding="utf-8") as f:
                f.write('#!/usr/bin/env bash\n'
                        '# 注释里的 yiban.clock 不算\n'
                        'from yiban.clock import today\n'
                        '_d="$(python3 -c \'from yiban import clock;print(clock.ts())\')"\n')
            self.assertEqual([ln for ln, _ in t2_old_coordinate_hits(sh)], [3, 4],
                             "shell 非注释行未命中（字面量或别名形态）或注释被误判")

            reg = {("frontend/e2e/server.py", _T2_OLD_MODULE): (1, "理由")}
            key = ("frontend/e2e/server.py", _T2_OLD_MODULE)
            self.assertEqual(t2_out_of_face_problems({key: 1}, reg), [],
                             "登记与实际一致却判红：本门没有牙")
            self.assertTrue(t2_out_of_face_problems({key: 2}, reg),
                            "命中次数多于登记未判红：新增残留会被静默放过")
            self.assertTrue(t2_out_of_face_problems({("frontend/x.py", _T2_OLD_MODULE): 1}, reg),
                            "未登记的新读者未判红：本门没有牙")
            self.assertTrue(t2_out_of_face_problems({}, reg),
                            "登记已消失（用户那边改掉了）未判红：登记会变成僵尸")


# ---------------------------------------------------------------------------
# 拆墙/壳退役 T3：`yiban.status` / `yiban.window` / `yiban.masking` 的读者迁到 `yiban.core.*`
# ---------------------------------------------------------------------------
# 来历（145-B `T3` 片）：`yiban/{status,window,masking}.py` 都是 `sys.modules` 替换型转发壳，
# 实现在 `yiban/core/{status,window,masking}.py`（批 B）。三坐标各自承载**不得外委**的语义面
# （AGENTS §十二）：status ＝ 签到状态语义（状态词汇表、12/13 退出码契约）；window ＝ 容量与
# 调度（窗口、块容量、edge clamp）；masking ＝ 隐私与脱敏（掩码口径）。**本片只改坐标，不动
# 任何语义**——门只钉"坐标不得是旧的那个"。
#
# 口径（2026-10-18 在 `94675773` 上现量）：含 import 路径或运行期代码串的自有面文件 55 个
# （tests 40 ＋ web 12 ＋ scripts 2 ＋ docker 1）。注释与 docstring 里的旧坐标字样不计入
# （与 T2 同口径：历史叙述不误红）。
#
# 本门钉三条不变式：
#   ① **自有面零命中**：面＝`yiban/**`、`web/**`（非 `web/static/**`）、`scripts/**`、
#      `docker/**`、`run.sh`、`tests/**`。三个旧坐标不得再以 import 路径（四种写法）、
#      别名形态 `from yiban import status`、属性访问 `yiban.status.X`、或非 docstring
#      字符串常量出现。**字符串常量这一条覆盖串内坐标**：本片实测
#      `tests/test_result_vocab_home.py` 的 `importlib.import_module("yiban.status")`
#      正是此形态——壳仍在时它**能跑**，壳删后 `ModuleNotFoundError`；只按字面量扫
#      import 的判据抓不到它（T2 首版判据漏检的正是同族形态）。⇒ 门的覆盖面必须等于
#      改动面（AGENTS §十三 形状③）。
#   ② **打桩面单列**：`mock.patch("<旧坐标>.…")` 在转发壳下**能成功但不控被测路径**
#      （静默失效）。本片实测三坐标的字符串打桩点 **0 处**，本条是防复发守卫。
#   ③ **日志出口名不算坐标**：`yiban/core/window.py` 的 `logging.getLogger("yiban.window")`
#      与 `tests/test_window_edge_single_source.py` 的 `assertLogs("yiban.window")` 是
#      **同一枚日志通道名**，与模块坐标同名纯属巧合。改名会改动日志可见面（运维按名
#      grep 告警），属语义变更 ⇒ 本片不得改，本门对它豁免。
#
# 豁免（按**文件**给，不是跳过；未登记的文件命中即红并点名 `文件:行`）：
#   `tests/test_batch_b_relocation.py`——它的职责就是验证壳转发同一对象（`145-F2` 删）。
#   `tests/test_import_linter_gate.py`——三坐标是它的**禁字面量**：该用例断言
#     import-linter 契约的 `forbidden_modules` **不许**列出迁移期坐标。改成新坐标会
#     **反转语义**（新坐标是永久坐标，契约列它是合法的）。
#   `tests/test_provenance_guard.py`——`yiban.masking` 是它的**禁前缀**：该用例断言
#     协议层不得 import 业务/安全层。**旧路径与新征程必须并列**——壳 `yiban/masking.py`
#     仍是合法 import 路径，只留新坐标会让这道常驻守卫漏掉旧路径（审查 F2）。
#     删壳后是否收窄，归 `145-E`。
#   `tests/test_deploy_entry_imports.py`——本文件，`_T3_OLD_MODULES` 是禁字面量，
#     夹具字符串用于自证本门有牙。
# 豁免不得变成万能挡箭牌：`test_exempt_files_still_exist` 另钉"每个豁免文件**必须仍
# 命中本门**"（豁免一旦失去理由即红，防"顺手把文件加进豁免面来让门变绿"）。
_T3_OLD_MODULES = ("yiban.status", "yiban.window", "yiban.masking")
#: 日志出口名（不是坐标）：见不变式③。
_T3_LOGGER_NAME = "yiban.window"
#: 以日志通道名为首个实参的调用名。
_T3_LOGGER_CALLS = frozenset(("getLogger", "assertLogs", "assertNoLogs"))
#: 自有面（不含面外 `frontend/**`，2026-10-18 起归用户独立完成）。
_T3_OWN_FACE_PY_DIRS = ("yiban", "web", "scripts", "docker", "tests")
_T3_OWN_FACE_SHELL = ("run.sh",)
_T3_SKIP_DIRS = frozenset(("node_modules", "__pycache__", ".venv", "venv", "dist", "build"))
#: 自有面里不归本批改的子树（`web/static` 是前端产物）。
_T3_SKIP_REL_PREFIX = ("web/static/",)
_T3_FACE_EXEMPT = frozenset((
    "tests/test_batch_b_relocation.py",
    "tests/test_import_linter_gate.py",
    "tests/test_provenance_guard.py",
    "tests/test_deploy_entry_imports.py",
))
_T3_SHORT = tuple(m.split(".")[-1] for m in _T3_OLD_MODULES)

#: 旧坐标的字面量判据：模块名后不得紧跟标识符字符（`yiban.status` 命中；
#: `yiban.status_ledger` 这类同前缀他模块不命中）。
_T3_OLD_LITERALS = {m: re.compile(r"(?<![\w.])" + re.escape(m) + r"(?![\w])")
                    for m in _T3_OLD_MODULES}
#: 别名形态**在文本里**的判据：`from yiban import …, status, …`。为什么单列：字面量判据
#: 要求串里出现 `yiban.status` 这个点号形态，抓不到 `"from yiban import status;…"`
#: 这类运行期代码串（壳删后 `ModuleNotFoundError`）。
#: **只取 import 列表并按项判名**，不用 `\bstatus\b` 这类裸词界——实测
#: `scripts/deploy_gate.sh` 的 `cli.main(['egress','--status','--json'])` 里 `--status`
#: 会被裸词界误判成旧坐标（首版判据的真假红，本片接手时抓到）。列表终止于 `;`、`#` 或换行。
_T3_FROM_YIBAN = re.compile(r"\bfrom\s+yiban\s+import\s+([^\n#;]*)")


def _t3_alias_names(text):
    """文本里 `from yiban import …, <短名>, …` 命中的短名列表（按项判名，不看裸词）。

    `from yiban.core import status` 不命中：`yiban` 后是 `.core` 不是空白。

    **取首个标识符**，不剥字符集：shell 的 `python3 -c "from yiban import status"`、
    `eval 'from yiban import status'`、反引号 `` `from yiban import status` `` 与
    转义引号 `\"from yiban import status\"` 都会把收尾引号／反引号带进列表项。
    剥字符集（`strip("()\"'")`）会漏掉反引号与转义引号两种形态 ⇒ 静默漏检
    （2026-10-18 独立审查两轮抓出：先漏引号，再漏反引号／转义）。
    `re.search` 对 `workers`、`(workers)`、`workers"`、`workers as w` 都给 `workers`；
    对 `*` 无匹配 ⇒ 返回空串 ⇒ 自然不命中。
    """
    out = []
    for m in _T3_FROM_YIBAN.finditer(text):
        for item in m.group(1).split(","):
            token = re.search(r"[A-Za-z_]\w*", item)
            name = token.group(0) if token else ""
            if name in _T3_SHORT:
                out.append(name)
    return out


def _t3_logger_name_ids(tree):
    """作为日志通道名使用的字符串常量节点 id 集合（`getLogger("yiban.window")` 等）。

    只豁免"确为日志通道名"的那一个字面量：`assertLogs("yiban.window")` 是日志名，
    而 `mock.patch("yiban.window.retry_hm")` 是坐标（字符串不同，不受影响）。
    """
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if fname not in _T3_LOGGER_CALLS:
            continue
        for a in node.args:
            if isinstance(a, ast.Constant) and a.value == _T3_LOGGER_NAME:
                ids.add(id(a))
    return ids


def _t3_import_hits(tree):
    """该文件里以旧坐标作 import 路径的位置：`(行号, 说明)` 列表。

    写法：`import yiban.status`、`from yiban.status import X`、
    `from yiban import status`（别名形态，是主流形态）、`from yiban import *`、
    `import yiban` 或 `import yiban as Y` 后按属性访问 `yiban.status.X` / `Y.status.X`。

    **根名集合**：属性访问判据认 `yiban` 以及 `import yiban as Y` 绑定的每个别名。
    为什么不能只认字面 `yiban`：`import yiban as Y` 后写 `Y.window.X` 是同一处旧坐标
    读者，只比 `ast.Name.id == "yiban"` 会漏掉它（审查实测的逃逸形态之一）。
    `from yiban import *` 同理：`a.name == "*"` 也是"经旧坐标取子模块"的形态。
    """
    roots = {"yiban"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "yiban":
                    roots.add(a.asname or "yiban")
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                for m in _T3_OLD_MODULES:
                    if a.name == m or a.name.startswith(m + "."):
                        hits.append((node.lineno, "import 旧坐标 " + a.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:
                continue
            for m in _T3_OLD_MODULES:
                if mod == m or mod.startswith(m + "."):
                    hits.append((node.lineno, "from 旧坐标 import " + mod))
            if mod == "yiban":
                for a in node.names:
                    if a.name in _T3_SHORT or a.name == "*":
                        hits.append((node.lineno, "from yiban import " + a.name + "（旧坐标别名）"))
        elif isinstance(node, ast.Attribute) and node.attr in _T3_SHORT:
            root = node.value
            if isinstance(root, ast.Name) and root.id in roots:
                hits.append((node.lineno, "属性访问 " + root.id + "." + node.attr + "（旧坐标）"))
    return hits


def t3_old_coordinate_hits(path):
    """返回该文件里引用三坐标的位置：`(行号, 说明)` 列表。

    Python：① 旧坐标 import（四种写法，含 `yiban.status.X` 属性访问）；
    ② 非 docstring 字符串常量含旧坐标（覆盖 `importlib.import_module("yiban.status")`
    这类串内坐标、打桩目标、`-c` 代码串）；③ 字符串里的别名形态
    `"from yiban import status…"`（运行期代码串）。
    日志通道名 `yiban.window` 在 `getLogger`/`assertLogs`/`assertNoLogs` 实参位豁免。
    Shell：**非注释行**含旧坐标（字面量或别名形态）。
    注释与 docstring 不算（任务口径：历史叙述不误红）。
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if path.endswith((".sh", ".bash")) or path.endswith("Dockerfile"):
        hits = []
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if any(_T3_OLD_LITERALS[m].search(s) for m in _T3_OLD_MODULES):
                hits.append((i, "shell/文本非注释行含旧坐标"))
            elif _t3_alias_names(s):
                hits.append((i, "shell/文本非注释行含 from yiban import <旧短名>"))
        return hits
    tree = ast.parse(text)
    docs = _docstring_const_ids(tree)
    logger_ids = _t3_logger_name_ids(tree)
    hits = list(_t3_import_hits(tree))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs and id(node) not in logger_ids):
            continue
        if any(_T3_OLD_LITERALS[m].search(node.value) for m in _T3_OLD_MODULES):
            hits.append((node.lineno, "非 docstring 字符串常量 " + node.value[:60]))
        elif _t3_alias_names(node.value):
            hits.append((node.lineno, "非 docstring 字符串含别名形态 " + node.value[:60]))
    return sorted(set(hits))


def t3_own_face_files(root):
    """自有面的扫描文件清单（.py ＋ .sh ＋ Dockerfile*），按 §7.2 的范围。"""
    out = []
    for d in _T3_OWN_FACE_PY_DIRS:
        for dirpath, dirs, files in os.walk(os.path.join(root, d)):
            dirs[:] = [x for x in dirs if x not in _T3_SKIP_DIRS]
            for name in sorted(files):
                if not name.endswith((".py", ".sh", ".bash")) and not name.startswith("Dockerfile"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), root).replace("\\", "/")
                if rel.startswith(_T3_SKIP_REL_PREFIX):
                    continue
                out.append(os.path.join(dirpath, name))
    for name in _T3_OWN_FACE_SHELL:
        path = os.path.join(root, name)
        if os.path.exists(path):
            out.append(path)
    return out


class StatusWindowMaskingCoordinateGateTest(unittest.TestCase):
    """T3：三坐标的自有面读者清零；打桩面零命中；日志通道名不误红。"""

    def test_own_face_has_no_old_coordinate(self):
        files = t3_own_face_files(BASE)
        self.assertGreaterEqual(
            len(files), 400,
            f"自有面扫描面过小（{len(files)} 个文件）——扫描根写错或树被移走（扫空不得当绿）")
        bad = []
        for path in files:
            rel = os.path.relpath(path, BASE).replace("\\", "/")
            if rel in _T3_FACE_EXEMPT:
                continue
            for line, why in t3_old_coordinate_hits(path):
                bad.append(f"{rel}:{line}: {why}")
        self.assertEqual(
            bad, [],
            "自有面仍以旧坐标 `yiban.{status,window,masking}` 作 import 路径、"
            "属性访问或字符串目标（壳退役后 import 会断、串内坐标会 `ModuleNotFoundError`）：\n"
            + "\n".join(bad))

    def test_no_patch_target_uses_old_coordinate(self):
        """打桩面单列一条：`mock.patch("<旧坐标>.…")` 是静默失效的典型形态。"""
        bad = []
        files = t3_own_face_files(BASE)
        self.assertGreaterEqual(len(files), 400, f"扫描面过小（{len(files)}）")
        for path in files:
            rel = os.path.relpath(path, BASE).replace("\\", "/")
            if not rel.endswith(".py") or rel in _T3_FACE_EXEMPT:
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            tree = ast.parse(text)
            docs = _docstring_const_ids(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if fname not in ("patch", "object"):
                    continue
                for a in node.args:
                    if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                            and id(a) not in docs
                            and any(_T3_OLD_LITERALS[m].search(a.value)
                                    for m in _T3_OLD_MODULES)):
                        bad.append(f"{rel}:{node.lineno}: 打桩目标 {a.value}")
        self.assertEqual(
            bad, [],
            "打桩目标仍指旧坐标 `yiban.{status,window,masking}`——壳退役后会静默失效"
            "（打桩成功但不控被测路径）：\n" + "\n".join(bad))

    def test_logger_name_exemption_is_anchored(self):
        """豁免锚点：`yiban.window` 确实是实现里的日志通道名（否则豁免会静默腐烂）。"""
        with open(os.path.join(BASE, "yiban", "core", "window.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) == "getLogger":
                for a in node.args:
                    if isinstance(a, ast.Constant) and a.value == _T3_LOGGER_NAME:
                        found = True
        self.assertTrue(found,
                        f"实现里不再有 getLogger({_T3_LOGGER_NAME!r})：本门的日志名豁免已腐烂，"
                        "须同批删掉该豁免")

    def test_exempt_files_still_exist(self):
        """豁免清单不得腐烂：登记的文件必须还在，且**必须仍命中本门**。

        为什么要"仍命中"：豁免只对"确有禁字面量／转发配对表的守卫"成立。若某个豁免
        文件已不再含旧坐标（守卫被清理），豁免就失去理由——留着它会静默放开该文件的
        扫描面。这条同时防"顺手把文件加进豁免面来让门变绿"（AGENTS §14 反面处置③）。
        """
        for rel in sorted(_T3_FACE_EXEMPT):
            path = os.path.join(BASE, rel)
            self.assertTrue(os.path.isfile(path),
                            f"豁免清单登记了不存在的文件 {rel}")
            self.assertTrue(
                t3_old_coordinate_hits(path),
                f"豁免文件 {rel} 已不再命中本门——豁免失去理由，须同批删除该豁免")

    def test_scanner_has_teeth(self):
        """突变验证：塞回八种形态 ⇒ 红；新坐标/docstring/注释/日志名/同前缀他模块 ⇒ 不误红。"""
        with tempfile.TemporaryDirectory(prefix="t3-gate-") as tmp:
            py = os.path.join(tmp, "fake_test.py")
            with open(py, "w", encoding="utf-8") as f:
                f.write('"""docstring 提 yiban.status 不算命中。"""\n'
                        'from yiban import status\n'                       # ① 别名 ⇒ 命中
                        'from yiban.window import retry_hm\n'              # ② 旧坐标 ⇒ 命中
                        'from unittest import mock\n'
                        'mock.patch("yiban.masking.mask_phone")\n'         # ③ 打桩串 ⇒ 命中
                        'P = "*yiban.status*"\n'                           # ④ 嵌入形态 ⇒ 命中
                        'CODE = "from yiban import window;print(window.retry_hm())"\n'  # ⑤ 串内别名 ⇒ 命中
                        'PROBE = """from yiban import masking as _m"""\n'  # ⑥ 多行串内别名 ⇒ 命中
                        'import importlib\n'
                        'importlib.import_module("yiban.status")\n'        # ⑦ 串内坐标 ⇒ 命中
                        'import yiban\n'
                        'yiban.window.parse_edges({})\n'                   # ⑧ 属性访问 ⇒ 命中
                        '# 注释里的 yiban.status 不算\n'
                        'import yiban.status_ledger\n'                    # ⑨ 同前缀他模块 ⇒ 不命中
                        'FLAG = "from yiban import cli; cli.main([\'egress\',\'--status\'])"\n'  # ⑩ 别的坐标＋CLI 旗标 ⇒ 不命中
                        'MULTI = "from yiban import egress, window"\n'     # ⑪ 混名列表含旧短名 ⇒ 命中
                        'import yiban as Y\n'
                        'Y.masking.sanitize_text("x")\n'                  # ⑫ 包别名属性访问 ⇒ 命中
                        'from yiban import *\n')                          # ⑬ 星号导入 ⇒ 命中
            hits = t3_old_coordinate_hits(py)
            lines = sorted({ln for ln, _ in hits})
            for want in (2, 3, 5, 6, 7, 8, 10, 12, 16, 18, 19):
                self.assertIn(want, lines, f"第 {want} 行应命中：{hits}")
            for dont in (14, 15):
                self.assertNotIn(dont, lines,
                                 f"第 {dont} 行不该命中（同前缀他模块／别的坐标的 CLI 旗标）：{hits}")

            # shell 载体：与 T4 同形，同批修（独立审查两轮分别在 T4 与 T3 各抓出一批）。
            sh = os.path.join(tmp, "fake.sh")
            with open(sh, "w", encoding="utf-8") as f:
                f.write('#!/bin/bash\n'
                        '# 注释里的 yiban.status 不算\n'
                        'python3 -c "from yiban import status"\n'               # ① 双引号内一行式 ⇒ 命中
                        "eval 'from yiban import window'\n"                     # ② 单引号内 ⇒ 命中
                        'python3 -c "from yiban.core import status"\n'          # ③ 新坐标 ⇒ 不命中
                        'python3 -c "import yiban.masking"\n'                   # ④ 引号内点号 ⇒ 命中
                        'bt=`python3 -c "from yiban import status"`\n'          # ⑤ 反引号命令替换 ⇒ 命中
                        'esc="python3 -c \\"from yiban import window\\""\n')     # ⑥ 转义引号 ⇒ 命中
            sh_lines = sorted({ln for ln, _ in t3_old_coordinate_hits(sh)})
            for want in (3, 4, 6, 7, 8):
                self.assertIn(want, sh_lines,
                              f"shell 第 {want} 行应命中（引号／反引号／转义／点号形态）：{sh_lines}")
            self.assertNotIn(5, sh_lines,
                             f"shell 第 5 行 `from yiban.core import status` 是新坐标，不该命中：{sh_lines}")

            clean = os.path.join(tmp, "clean.py")
            with open(clean, "w", encoding="utf-8") as f:
                f.write('"""历史叙述：yiban.status 迁到 yiban.core.status。"""\n'
                        'import logging\n'
                        'from yiban.core import status, window, masking\n'
                        'from yiban.core.status import STATUS_SUCCESS\n'
                        'import yiban.adapters.persistence.queue_store as qs\n'
                        'LOGGER = logging.getLogger("yiban.window")\n'
                        'A = "yiban.core.status.STATUS_SUCCESS"\n'
                        'B = "yiban.status_ledger"\n'
                        'C = "from yiban.core import window"\n')
            self.assertEqual(
                t3_old_coordinate_hits(clean), [],
                "新坐标、`yiban.status_ledger`、docstring、日志通道名被误判成旧坐标")

            # 日志名豁免必须**只**认"作为日志通道名"的那一个：作为打桩目标仍须命中。
            logger_only = os.path.join(tmp, "logger_only.py")
            with open(logger_only, "w", encoding="utf-8") as f:
                f.write('import logging\n'
                        'L = logging.getLogger("yiban.window")\n'
                        'with self.assertLogs("yiban.window"):\n'
                        '    pass\n'
                        'T = "yiban.window.retry_hm"\n')
            got = [ln for ln, _ in t3_old_coordinate_hits(logger_only)]
            self.assertEqual(got, [5],
                             f"日志名豁免面过宽或过窄：应只命中第 5 行打桩目标串，实得 {got}")

            sh = os.path.join(tmp, "fake.sh")
            with open(sh, "w", encoding="utf-8") as f:
                f.write('#!/usr/bin/env bash\n'
                        '# 注释里的 yiban.masking 不算\n'
                        'python3 -c "from yiban.masking import mask_phone"\n'
                        '_s="$(python3 -c \'from yiban import status;print(status.TEXT)\')"\n')
            self.assertEqual([ln for ln, _ in t3_old_coordinate_hits(sh)], [3, 4],
                             "shell 非注释行未命中（字面量或别名形态）或注释被误判")


# ---------------------------------------------------------------------------
# 拆墙/壳退役 T4：七个 `yiban.engine.*` 壳坐标的读者迁真实实现层
# ---------------------------------------------------------------------------
# 来历（145-B `T4` 片）：`yiban/engine/{hrw,round,runner,probe,alerts,state_io,workers}.py`
# 都是 `sys.modules` 替换型转发壳（批 B）。七坐标各自迁到真实实现层（见下表）。
# **本片只改坐标，不动任何语义**——其中 `runner`/`workers`/`round`/`state_io` 是调度与
# 执行的编排面（AGENTS §三 例外①"执行核"的相邻面）。
#
# 口径（2026-10-18 在 `3f658c4d` 上现量，AST 逐项判名，不用点号字面 grep）：
# 自有面运行期读者 **48 文件**（py 47 ＋ `run.sh` 1）：46 个待迁读者 ＋ 2 个按设计必须
# 写出坐标字面量的登记/验证文件（`tests/test_batch_b_relocation.py`、
# `tests/test_notifier_relocation.py`，两者在 `_T4_FACE_EXEMPT`）。**面外读者 0 个**：
# `frontend/e2e/server.py` 那 1 个已由 2026-10-18 本批就地处置（改直连新坐标），
# `_T4_OUT_OF_FACE_REGISTRY` 同批清空。
# **点号字面只命中 9 个读者，漏掉 39 个（81%）**——那 39 个只能经
# `from yiban.engine import <短名>` 别名到达（AGENTS §7.1"扫描口径有洞"；
# 本门按 import 列表逐项判名，不按裸词界）。
#
# 复现（两个数必须**同总体**）：import 本文件，对 `t4_own_face_files(root)` 逐个跑
# `t4_old_coordinate_hits`，数非空项得 **48**（自有面运行期读者）；再对这同一批 48 个
# **读者**逐个查文本是否含点号字面，得 **9**；48 − 9 = **39**。
# ⚠ 另有 **24 个文件**"文本里出现过点号字面"，但那是**全体自有面文件**的数——其中
# **15 个不是读者**（`yiban/__init__.py`、`yiban/security.py`、七个壳本体等，只有
# docstring／叙述提及）。**不许拿 48 去减 24**：那是跨口径相减，会得出 24 这个错数。
# 这一条是 AGENTS §31 的现场实例——本注释的上一版就是这么错的（独立审查抓出）。
#
# 本门钉三条不变式：
#   ① **自有面零命中**：面＝`yiban/**`、`web/**`（非 `web/static/**`）、`scripts/**`、
#      `docker/**`、`run.sh`、`tests/**`。七旧坐标不得再以 import 路径（四种写法，含
#      `import yiban as Y; Y.engine.runner.X` 与 `from yiban.engine import *`）、别名形态
#      `from yiban.engine import runner`、属性访问 `yiban.engine.runner.X`、或非 docstring
#      字符串常量（含打桩目标、`importlib.import_module` 串、`python -c` 代码串里的
#      `"from yiban.engine import …"`）出现。
#      为什么必须钉：壳仍在，故 `from yiban.engine import runner` **能跑**、字符串打桩
#      **能成功**；但 `145-E` 删壳后 import 直接 `ModuleNotFoundError`，字符串打桩目标会
#      **静默失效**（不报错，只不再控制被测路径）。
#   ② **打桩面单列**：`mock.patch("<旧坐标>.…")` 在转发壳下**能成功但不控被测路径**。
#      本片实测字符串打桩点：`test_window_alert_reset.py` 6 处（`yiban.engine.alerts.*`）、
#      `test_key_sources_startup_assert.py` 1 处（`yiban.engine.runner.*`）——已迁新坐标。
#   ③ **非 Python 载体的非注释行同样零命中**：`run.sh` 的 heredoc 里
#      `from yiban.engine import state_io` 是运行期硬依赖，壳删后兜底心跳检测会崩。
#   ④ **日志通道名不算坐标**：`tests/test_log_masking_entry_mounts.py` 的
#      `logging.getLogger("yiban.engine.round")` 是**日志通道名**，与模块坐标同名纯属
#      巧合。本片**刻意沿用旧串**：日志通道名是运维取证面（出现在日志行里，被 grep、
#      被归属判定用），改名＝改变可观察行为，属语义变更。本批契约是"只许改坐标，不许
#      改任何语义"（AGENTS §十三 同口径：T3 对 `yiban/window.py` 的 `yiban.window`
#      原样保留）。该名的去留归 `145-F` 的观测面批次决定，不属坐标迁移。
#      门只豁免"作为日志通道名"的那一个字面量；同串作为打桩目标仍须命中。
#
# 豁免（按**文件**给，不是跳过；未登记的文件命中即红并点名 `文件:行`）：
#   `tests/test_batch_b_relocation.py`——它的职责就是验证壳转发同一对象（`145-F2` 删）。
#   `tests/test_notifier_relocation.py`——`test_alert_orchestration_has_one_implementation`
#     经 `import yiban.engine.alerts as old` 验证壳与实现同一对象（壳仍在时是正当读者）。
#   `tests/test_deploy_entry_imports.py`——本文件，`_T4_OLD_TO_NEW` 是禁字面量表，
#     夹具字符串用于自证本门有牙。
# 豁免不得变成万能挡箭牌：`test_exempt_files_still_exist` 另钉"每个豁免文件**必须仍命中
# 本门**"（豁免一旦失去理由即红，防"顺手把文件加进豁免面来让门变绿"）。
#
# 已知盲点（显式登记，AGENTS §28）：别名形态的文本判据 `_T4_FROM_ENGINE` 止于 `\n`／`#`／`;`，
# 故**跨行的** `"from yiban.engine import (\n    workers,\n)"` 这种串内多行别名形态扫不到。
# 仓内无此形态（实测：运行期串内别名均为单行）；真要用多行时，点号形态判据同样不命中，
# 须靠本表的手工登记发现。
_T4_OLD_TO_NEW = {
    "yiban.engine.hrw": "yiban.core.hrw",
    "yiban.engine.round": "yiban.app.round",
    "yiban.engine.runner": "yiban.app.runner",
    "yiban.engine.probe": "yiban.app.probe",
    "yiban.engine.alerts": "yiban.adapters.notifier.report",
    "yiban.engine.state_io": "yiban.adapters.statefiles.state_io",
    "yiban.engine.workers": "yiban.workers.workers",
}
_T4_OLD_MODULES = tuple(_T4_OLD_TO_NEW)
_T4_SHORT_TO_OLD = {m.rsplit(".", 1)[1]: m for m in _T4_OLD_MODULES}
#: 自有面（不含面外 `frontend/**`，2026-10-18 起归用户独立完成）。
_T4_OWN_FACE_PY_DIRS = ("yiban", "web", "scripts", "docker", "tests")
_T4_OWN_FACE_SHELL = ("run.sh",)
_T4_SKIP_DIRS = frozenset(("node_modules", "__pycache__", ".venv", "venv", "dist", "build"))
#: 自有面里不归本批改的子树（`web/static` 是前端产物）。
_T4_SKIP_REL_PREFIX = ("web/static/",)
_T4_FACE_EXEMPT = frozenset((
    "tests/test_batch_b_relocation.py",
    "tests/test_notifier_relocation.py",
    "tests/test_deploy_entry_imports.py",
))

#: 面外目录（`frontend/**`，2026-10-18 起归用户独立完成，本片不得改）。
_T4_OUT_OF_FACE_DIRS = ("frontend",)
#: `frontend/**` 里仍读七坐标的**待销账登记**（AGENTS §28：约束要么有门、要么显式豁免）。
#: 键＝`(相对路径, 旧坐标)`，值＝`(命中行数, 处置说明)`。
#:
#: 为什么按 `(路径, 坐标, 行数)` 而不钉行号：前端归用户独立完成，行号会随其无关改动漂移。
#: 把行号做成判据会让**用户的 PR 因一行位移而红**，而修法却落在我的文件里
#: ——那正是"门看的东西 ≠ 实际改的东西"（AGENTS §29）。按行数对账已能覆盖三件事：
#: 未登记的新读者 ⇒ 红；登记处多一行/少一行 ⇒ 红；整条消失 ⇒ 红（防腐烂）。
#:
#: 同族登记：`frontend/e2e/server.py` 曾是**已知的面外消费者**（三片各占一键）。该文件的
#: 三处旧坐标已于 2026-10-18 由用户授权主会话就地处置 ⇒ 三张表同批清空。
#: 三片各扫各的坐标键（表不跨片共享，避免改别人的门）：
#:   - `T1`：`generate_password_hash` → `_PWHASH_OUT_OF_FACE_REGISTRY`
#:   - `T2`：`yiban.clock` → `_T2_OUT_OF_FACE_REGISTRY`
#:   - `T4`：`yiban.engine.state_io` → 本表
#: **2026-10-18 已清空**：唯一一条登记（`frontend/e2e/server.py` 以旧坐标导入 state_io）
#: 经用户授权由主会话就地处置——该文件改 `from yiban.adapters.statefiles import state_io`。
#: 按判据"登记整条消失 ⇒ 红"，处置与销账必须同批，故同批清空。
#: **表空 ≠ 面空**：扫描面仍由用例断言非空（§30）。
_T4_OUT_OF_FACE_REGISTRY = {}

#: 旧坐标的字面量判据：模块名后不得紧跟标识符字符（`yiban.engine.runner` 命中；
#: `yiban.engine.runner_extra` 这类同前缀他模块不命中）。
_T4_OLD_LITERALS = {m: re.compile(r"(?<![\w.])" + re.escape(m) + r"(?![\w])")
                    for m in _T4_OLD_MODULES}
#: 别名形态**在文本里**的判据：`from yiban.engine import …, runner, …`。
#: 为什么单列：字面量判据要求串里出现点号形态，抓不到 `"from yiban.engine import runner"`。
#: **只取 import 列表并按项判名**，不用 `\brunner\b` 这类裸词界（裸词界会误命中普通词）。
_T4_FROM_ENGINE = re.compile(r"\bfrom\s+yiban\.engine\s+import\s+([^\n#;]*)")

#: 日志通道名（不是坐标）：见不变式④。沿用旧串是**刻意**的，去留归 `145-F`。
_T4_LOGGER_NAME = "yiban.engine.round"
#: 以日志通道名为首个实参的调用名。
_T4_LOGGER_CALLS = frozenset(("getLogger", "assertLogs", "assertNoLogs"))
#: 该日志通道名的在位锚点文件：豁免防腐（改名/删名即红）。
_T4_LOGGER_ANCHOR_FILE = "tests/test_log_masking_entry_mounts.py"


def _t4_alias_names(text):
    """文本里 `from yiban.engine import …, <短名>, …` 命中的旧坐标列表（按项判名）。

    **取首个标识符**，不剥字符集：shell 的 `python3 -c "from yiban.engine import workers"`、
    `eval 'from yiban.engine import workers'`、反引号与转义引号形态都会把收尾引号／
    反引号带进列表项。剥字符集会漏掉反引号与转义引号两种 ⇒ 静默漏检（2026-10-18
    独立审查两轮抓出：先漏引号，再漏反引号／转义）。T3 的同类解析器同形，同批修。
    """
    out = []
    for m in _T4_FROM_ENGINE.finditer(text):
        for item in m.group(1).split(","):
            token = re.search(r"[A-Za-z_]\w*", item)
            name = token.group(0) if token else ""
            if name in _T4_SHORT_TO_OLD:
                out.append(_T4_SHORT_TO_OLD[name])
    return out


def _t4_logger_name_ids(tree):
    """作为日志通道名使用的字符串常量节点 id 集合（`getLogger("yiban.engine.round")` 等）。

    只豁免"确为日志通道名"的那一个字面量：`assertLogs("yiban.engine.round")` 是日志名，
    而同串作为打桩目标（`mock.patch("yiban.engine.round.X")`）是坐标，仍须命中。
    """
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if fname not in _T4_LOGGER_CALLS:
            continue
        for a in node.args:
            if isinstance(a, ast.Constant) and a.value == _T4_LOGGER_NAME:
                ids.add(id(a))
    return ids


def _t4_bindings(tree):
    """该文件里 local 名 → 点号模块路径 的绑定表（供属性链解析别名根）。"""
    b = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                local = a.asname or a.name.split(".")[0]
                b[local] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            mod = node.module or ""
            for a in node.names:
                if a.name == "*":
                    continue
                b[a.asname or a.name] = (mod + "." + a.name) if mod else a.name
    return b


def _t4_dotted(node, bindings):
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(bindings.get(cur.id, cur.id))
        return ".".join(reversed(parts))
    return None


def _t4_import_hits(tree):
    """该文件里以旧坐标作 import 路径或属性访问的位置：`(行号, 说明, 坐标)` 列表。

    写法：`import yiban.engine.runner`、`from yiban.engine.runner import X`、
    `from yiban.engine import runner`（别名形态，是主流形态）、`from yiban.engine import *`、
    `import yiban` 或 `import yiban as Y` 后按属性访问 `yiban.engine.runner.X` / `Y.engine.runner.X`。
    """
    bindings = _t4_bindings(tree)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                for m in _T4_OLD_MODULES:
                    if a.name == m or a.name.startswith(m + "."):
                        hits.append((node.lineno, "import 旧坐标 " + a.name, m))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            mod = node.module or ""
            for m in _T4_OLD_MODULES:
                if mod == m or mod.startswith(m + "."):
                    hits.append((node.lineno, "from 旧坐标 import " + mod, m))
            if mod == "yiban.engine":
                for a in node.names:
                    if a.name in _T4_SHORT_TO_OLD:
                        hits.append((node.lineno,
                                     "from yiban.engine import " + a.name + "（旧坐标别名）",
                                     _T4_SHORT_TO_OLD[a.name]))
                    elif a.name == "*":
                        hits.append((node.lineno, "from yiban.engine import *（旧坐标星号）",
                                     "yiban.engine"))
        elif isinstance(node, ast.Attribute):
            d = _t4_dotted(node, bindings)
            if not d:
                continue
            for m in _T4_OLD_MODULES:
                if d == m or d.startswith(m + "."):
                    hits.append((node.lineno, "属性访问 " + d + "（旧坐标）", m))
    return sorted(set(hits))


def t4_old_coordinate_hits(path):
    """返回该文件里引用七坐标的位置：`(行号, 说明, 坐标)` 列表。

    Python：① 旧坐标 import（四种写法，含 `Y.engine.runner.X` 属性访问与星号导入）；
    ② 非 docstring 字符串常量含旧坐标（覆盖打桩目标、`importlib.import_module` 串、
    `python -c` 代码串）；③ 字符串里的别名形态 `"from yiban.engine import runner…"`。
    日志通道名 `yiban.engine.round` 在 `getLogger`/`assertLogs`/`assertNoLogs` 实参位豁免。
    Shell：**非注释行**含旧坐标（字面量或别名形态）。
    注释与 docstring 不算（任务口径：历史叙述不误红）。
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if path.endswith((".sh", ".bash")) or path.endswith("Dockerfile"):
        hits = []
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            for m in _T4_OLD_MODULES:
                if _T4_OLD_LITERALS[m].search(s):
                    hits.append((i, "shell/文本非注释行含 " + m, m))
            for m in _t4_alias_names(s):
                hits.append((i, "shell/文本非注释行含 from yiban.engine import <旧短名>", m))
        return sorted(set(hits))
    tree = ast.parse(text)
    docs = _docstring_const_ids(tree)
    logger_ids = _t4_logger_name_ids(tree)
    hits = list(_t4_import_hits(tree))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs and id(node) not in logger_ids):
            continue
        for m in _T4_OLD_MODULES:
            if _T4_OLD_LITERALS[m].search(node.value):
                hits.append((node.lineno, "非 docstring 字符串常量 " + node.value[:60], m))
                break
        else:
            aliases = _t4_alias_names(node.value)
            if aliases:
                hits.append((node.lineno, "非 docstring 字符串含别名形态 "
                             + node.value[:60], aliases[0]))
    return sorted(set(hits))


def t4_own_face_files(root):
    """自有面的扫描文件清单（.py ＋ .sh ＋ Dockerfile*），按 §7.2 的范围。"""
    out = []
    for d in _T4_OWN_FACE_PY_DIRS:
        for dirpath, dirs, files in os.walk(os.path.join(root, d)):
            dirs[:] = [x for x in dirs if x not in _T4_SKIP_DIRS]
            for name in sorted(files):
                if not name.endswith((".py", ".sh", ".bash")) and not name.startswith("Dockerfile"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), root).replace("\\", "/")
                if rel.startswith(_T4_SKIP_REL_PREFIX):
                    continue
                out.append(os.path.join(dirpath, name))
    for name in _T4_OWN_FACE_SHELL:
        path = os.path.join(root, name)
        if os.path.exists(path):
            out.append(path)
    return out


def t4_out_of_face_files(root):
    """面外（`frontend/**`，跳过 `node_modules` 等）的 Python 文件清单。"""
    out = []
    for d in _T4_OUT_OF_FACE_DIRS:
        for dirpath, dirs, files in os.walk(os.path.join(root, d)):
            dirs[:] = [x for x in dirs if x not in _T4_SKIP_DIRS]
            for name in sorted(files):
                if name.endswith(".py"):
                    out.append(os.path.join(dirpath, name))
    return out


def t4_out_of_face_found(files, root):
    """面外命中的实测值：`({(相对路径, 坐标): 命中行数}, {(相对路径, 坐标): [行号]})`。

    计数单位是**命中行数**（同一条语句上的多个命中只算一行）：链深度或排版变化
    不改数，新增一处读者才改数。行号只进 `where`，供红时点名，不进判据。
    """
    found, where = {}, {}
    for path in files:
        rel = os.path.relpath(path, root).replace("\\", "/")
        by_coord = {}
        for line, _why, coord in t4_old_coordinate_hits(path):
            if coord is None:
                continue
            by_coord.setdefault(coord, set()).add(line)
        for coord, lines in by_coord.items():
            key = (rel, coord)
            found[key] = len(lines)
            where[key] = sorted(lines)
    return found, where


def t4_out_of_face_problems(found, registry=None, where=None):
    """面外残留与登记表对账（纯函数，便于突变自证）：返回问题清单（空＝一致）。

    三条判据（AGENTS §28/§29）：
      · 未登记的新读者 ⇒ 红（`145-E` 删壳时会断，而它以为读者已清零）；
      · 登记的行数与实际不符 ⇒ 红（多一行＝新增残留；少一行＝已处置一半）；
      · 登记整条消失 ⇒ 红（用户那边已处置，必须同批销账，否则登记变成僵尸）。
    """
    registry = _T4_OUT_OF_FACE_REGISTRY if registry is None else registry
    where = where or {}
    problems = []
    for key, n in sorted(found.items()):
        entry = registry.get(key)
        if entry is None:
            problems.append(
                f"{key[0]}:{where.get(key)} 出现未登记的面外旧坐标读者 {key[1]}（{n} 行）——"
                "要么迁到新坐标，要么在 _T4_OUT_OF_FACE_REGISTRY 登记")
        elif n != entry[0]:
            problems.append(
                f"{key[0]}:{where.get(key)} 的旧坐标读者 {key[1]} 实测 {n} 行、登记 {entry[0]} 行"
                "——两侧必须一致（新增残留不许漏登记；已处置的必须同批销账）")
    for key, (n, _why) in sorted(registry.items()):
        if key not in found:
            problems.append(
                f"登记 {key[0]} 的 {key[1]}（{n} 行）已不在代码里——"
                "前端那边已处置，请同批删掉这条登记（防登记变成僵尸）")
    return problems


class EngineCoordinateGateTest(unittest.TestCase):
    """T4：七坐标的自有面读者清零；打桩面零命中；非 Python 载体零命中。"""

    def test_own_face_has_no_old_coordinate(self):
        files = t4_own_face_files(BASE)
        self.assertGreaterEqual(
            len(files), 400,
            f"自有面扫描面过小（{len(files)} 个文件）——扫描根写错或树被移走（扫空不得当绿）")
        bad = []
        for path in files:
            rel = os.path.relpath(path, BASE).replace("\\", "/")
            if rel in _T4_FACE_EXEMPT:
                continue
            for line, why, _c in t4_old_coordinate_hits(path):
                bad.append(f"{rel}:{line}: {why}")
        self.assertEqual(
            bad, [],
            "自有面仍以旧坐标 `yiban.engine.{hrw,round,runner,probe,alerts,state_io,workers}` "
            "作 import 路径、属性访问或字符串目标（壳退役后 import 会断、字符串打桩会静默失效）：\n"
            + "\n".join(bad))

    def test_no_patch_target_uses_old_coordinate(self):
        """打桩面单列一条：`mock.patch("<旧坐标>.…")` 是静默失效的典型形态。"""
        bad = []
        files = t4_own_face_files(BASE)
        self.assertGreaterEqual(len(files), 400, f"扫描面过小（{len(files)}）")
        for path in files:
            rel = os.path.relpath(path, BASE).replace("\\", "/")
            if not rel.endswith(".py") or rel in _T4_FACE_EXEMPT:
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            tree = ast.parse(text)
            docs = _docstring_const_ids(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if fname not in ("patch", "object"):
                    continue
                for a in node.args:
                    if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                            and id(a) not in docs
                            and any(_T4_OLD_LITERALS[m].search(a.value)
                                    for m in _T4_OLD_MODULES)):
                        bad.append(f"{rel}:{node.lineno}: 打桩目标 {a.value}")
        self.assertEqual(
            bad, [],
            "打桩目标仍指旧坐标 `yiban.engine.*`——壳退役后会静默失效"
            "（打桩成功但不控被测路径）：\n" + "\n".join(bad))

    def test_exempt_files_still_exist(self):
        """豁免清单不得腐烂：登记的文件必须还在，且**必须仍命中本门**。"""
        for rel in sorted(_T4_FACE_EXEMPT):
            path = os.path.join(BASE, rel)
            self.assertTrue(os.path.isfile(path),
                            f"豁免清单登记了不存在的文件 {rel}")
            self.assertTrue(
                t4_old_coordinate_hits(path),
                f"豁免文件 {rel} 已不再命中本门——豁免失去理由，须同批删除该豁免")

    def test_logger_name_exemption_is_anchored(self):
        """豁免锚点：日志通道名 `yiban.engine.round` 仍以 logger 名形态在位。

        本片**刻意沿用旧串**做日志通道名（见不变式④）：它是运维取证面，改名属语义变更。
        若该名被改名或删除，本豁免即失去理由——留着它会静默放开该文件的扫描面。
        """
        path = os.path.join(BASE, _T4_LOGGER_ANCHOR_FILE)
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        self.assertTrue(
            _t4_logger_name_ids(tree),
            f"{_T4_LOGGER_ANCHOR_FILE} 里已无 getLogger({_T4_LOGGER_NAME!r})："
            "本门的日志通道名豁免已腐烂，须同批删掉该豁免")

    def test_out_of_face_readers_match_registry(self):
        """面外（`frontend/**`）的残留与登记表逐条对账，两侧都不许静默。

        `frontend/**` 归用户独立完成，本片不得改。但它是**活的消费者**：`145-E` 删壳时
        它会断，而自有面门扫不到它。故残留必须登记；未登记、行数不符、登记消失都判红。
        """
        files = t4_out_of_face_files(BASE)
        self.assertGreaterEqual(
            len(files), 1,
            f"面外扫描面为空（{len(files)} 个文件）——扫描根写错或前端被移走（扫空不得当绿）")
        found, where = t4_out_of_face_found(files, BASE)
        problems = t4_out_of_face_problems(found, where=where)
        self.assertEqual(problems, [], "面外旧坐标残留与登记表不一致：\n" + "\n".join(problems))

    def test_registry_reconciliation_has_teeth(self):
        """突变验证（合成输入）：未登记 ⇒ 红；行数不符 ⇒ 红；登记消失 ⇒ 红；一致 ⇒ 绿。"""
        reg = {("frontend/e2e/server.py", "yiban.engine.state_io"): (3, "登记理由")}
        key = ("frontend/e2e/server.py", "yiban.engine.state_io")
        self.assertEqual(t4_out_of_face_problems({key: 3}, registry=reg), [],
                         "一致时不该报问题（否则三条判据可能是一组恒真断言）")
        self.assertTrue(t4_out_of_face_problems({key: 4}, registry=reg),
                        "行数多于登记未判红：新增残留会被静默放过")
        self.assertTrue(t4_out_of_face_problems({key: 2}, registry=reg),
                        "行数少于登记未判红：处置没做完就被当成已清零")
        self.assertTrue(
            t4_out_of_face_problems(
                {("frontend/e2e/other.py", "yiban.engine.state_io"): 1}, registry=reg),
            "未登记的新读者未判红：本门没有牙")
        self.assertTrue(t4_out_of_face_problems({}, registry=reg),
                        "登记已消失（用户那边改掉了）未判红：登记会变成僵尸")

    def test_scanner_has_teeth(self):
        """突变验证：塞回十二种形态 ⇒ 红；新坐标/docstring/注释/同前缀他模块 ⇒ 不误红。"""
        with tempfile.TemporaryDirectory(prefix="t4-gate-") as tmp:
            py = os.path.join(tmp, "fake_test.py")
            with open(py, "w", encoding="utf-8") as f:
                f.write('"""docstring 提 yiban.engine.runner 不算命中。"""\n'
                        'from yiban.engine import runner\n'                      # ① 别名 ⇒ 命中
                        'from yiban.engine.runner import main\n'                 # ② 旧坐标 ⇒ 命中
                        'from unittest import mock\n'
                        'mock.patch("yiban.engine.alerts._collect_admin_mail")\n'  # ③ 打桩串 ⇒ 命中
                        'P = "*yiban.engine.workers*"\n'                         # ④ 嵌入形态 ⇒ 命中
                        'CODE = "from yiban.engine import runner;print(runner.main([]))"\n'  # ⑤ 串内别名 ⇒ 命中
                        'PROBE = """from yiban.engine import workers as _w"""\n'  # ⑥ 多行串内别名 ⇒ 命中
                        'import importlib\n'
                        'importlib.import_module("yiban.engine.state_io")\n'     # ⑦ 串内坐标 ⇒ 命中
                        'import yiban\n'
                        'yiban.engine.probe.run_probe()\n'                       # ⑧ 属性访问 ⇒ 命中
                        'import yiban as Y\n'
                        'Y.engine.workers.run_fallback_worker([])\n'             # ⑨ 包别名属性访问 ⇒ 命中
                        'import yiban.engine.hrw as hrw\n'                       # ⑩ import 旧坐标 ⇒ 命中
                        'from yiban.engine import *\n'                           # ⑪ 星号导入 ⇒ 命中
                        '# 注释里的 yiban.engine.runner 不算\n'
                        'import yiban.engine.runner_extra\n')                    # ⑫ 同前缀他模块 ⇒ 不命中
            hits = t4_old_coordinate_hits(py)
            lines = sorted({ln for ln, _w, _c in hits})
            for want in (2, 3, 5, 6, 7, 8, 10, 12, 14, 15, 16):
                self.assertIn(want, lines, f"第 {want} 行应命中：{hits}")
            self.assertNotIn(11, lines,
                             f"第 11 行 `import yiban`（包本身）不该命中：{hits}")
            self.assertNotIn(18, lines,
                             f"第 18 行 `yiban.engine.runner_extra` 不该命中（同前缀他模块）：{hits}")

            # shell 载体：**按项判名**的解析器最容易漏的是"收尾不是标识符字符"的形态。
            # 两轮独立审查各抓出一批：先漏引号（列表项成 `workers"`），再漏反引号与
            # 转义引号（成 `hrw"`＋反引号 / `round\`）。故实现改成"取首个标识符"。
            sh = os.path.join(tmp, "fake.sh")
            with open(sh, "w", encoding="utf-8") as f:
                f.write('#!/bin/bash\n'
                        '# 注释里的 yiban.engine.workers 不算\n'
                        'python3 -c "from yiban.engine import workers"\n'             # ① 双引号内一行式 ⇒ 命中
                        "eval 'from yiban.engine import workers'\n"                 # ② 单引号内 ⇒ 命中
                        'python3 -c "import yiban.engine.probe"\n'                  # ③ 引号内点号 ⇒ 命中
                        'python3 -c "from yiban.app import runner"\n'               # ④ 新坐标 ⇒ 不命中
                        'python3 -c "from yiban.engine import workers;print(1)"\n'  # ⑤ 带分号 ⇒ 命中
                        'bt=`python3 -c "from yiban.engine import hrw"`\n'           # ⑥ 反引号命令替换 ⇒ 命中
                        'esc="python3 -c \\"from yiban.engine import round\\""\n')   # ⑦ 转义引号 ⇒ 命中
            sh_lines = sorted({ln for ln, _w, _c in t4_old_coordinate_hits(sh)})
            for want in (3, 4, 5, 7, 8, 9):
                self.assertIn(want, sh_lines,
                              f"shell 第 {want} 行应命中（引号／反引号／转义／点号／分号形态）：{sh_lines}")
            self.assertNotIn(6, sh_lines,
                             f"shell 第 6 行 `from yiban.app import runner` 是新坐标，不该命中：{sh_lines}")

            clean = os.path.join(tmp, "clean.py")
            with open(clean, "w", encoding="utf-8") as f:
                f.write('"""历史叙述：yiban.engine.runner 迁到 yiban.app.runner。"""\n'
                        'from yiban.app import runner\n'
                        'from yiban.core import hrw\n'
                        'from yiban.adapters.notifier import report as alerts\n'
                        'from yiban.adapters.statefiles import state_io\n'
                        'from yiban.workers import workers\n'
                        'from yiban.engine import schedule, executor_v3\n'
                        'import yiban.adapters.persistence.queue_store as qs\n'
                        'A = "yiban.app.runner.main"\n'
                        'B = "from yiban.app import runner"\n'
                        'C = "yiban.engine.runner_extra"\n')
            self.assertEqual(
                t4_old_coordinate_hits(clean), [],
                "新坐标、`yiban.engine.schedule`（他模块）、docstring 被误判成旧坐标")

            # 日志名豁免必须**只**认"作为日志通道名"的那一个：作为打桩目标/普通串仍须命中。
            logger_only = os.path.join(tmp, "logger_only.py")
            with open(logger_only, "w", encoding="utf-8") as f:
                f.write('import logging\n'
                        'L = logging.getLogger("yiban.engine.round")\n'
                        'with self.assertLogs("yiban.engine.round"):\n'
                        '    pass\n'
                        'T = "yiban.engine.round.X"\n')
            got = [ln for ln, _w, _c in t4_old_coordinate_hits(logger_only)]
            self.assertEqual(got, [5],
                             f"日志名豁免面过宽或过窄：应只命中第 5 行打桩目标串，实得 {got}")

            sh = os.path.join(tmp, "fake.sh")
            with open(sh, "w", encoding="utf-8") as f:
                f.write('#!/usr/bin/env bash\n'
                        '# 注释里的 yiban.engine.state_io 不算\n'
                        'python3 -c "from yiban.engine.state_io import fallback_alive"\n'
                        '_s="$(python3 -c \'from yiban.engine import workers;print(1)\')"\n')
            self.assertEqual([ln for ln, _w, _c in t4_old_coordinate_hits(sh)], [3, 4],
                             "shell 非注释行未命中（字面量或别名形态）或注释被误判")


if __name__ == "__main__":
    unittest.main(verbosity=2)
