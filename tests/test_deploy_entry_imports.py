# -*- coding: utf-8 -*-
"""部署入口脚本的导入条件：只在 `scripts/` 入 sys.path 时也必须能导入。

标签：J · 运维：部署/备份/发布
覆盖：`scripts/signin.py` 与 `docker/scheduler.py` 在无 PYTHONPATH 下的真实导入、
    凡导入 `yiban` 的运行时脚本必须自带包导入引导、引导必须**先于**首个 yiban 导入。
对应实现：`scripts/signin.py`、`docker/scheduler.py`（镜像里被复制为
    `scripts/container_scheduler.py`）及各入口的 `sys.path` 引导块。
关键断言：导入失败以非零码退出、进入主循环则超时——两种情况可区分；空文件/截断文件
    同样能 import 成功，故额外要求模块真的装配出 `main_loop`。
依赖：起 `sys.executable` 子进程跑仓库内脚本（清空 PYTHONPATH、cwd=仓库根、临时
    STATE/LOG/DB/ENV）；不需 bash/docker CLI；读的是仓库源文件而非镜像。

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
# 来历（工单 `1229`，施工方案 `docs/refactor/145` §1.7 与 §4.1 风险 2）：
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
RUNTIME_PY_DIRS = ("yiban", "scripts", "docker")
RUNTIME_SHELL_NAMES = ("run.sh",)
OLD_CLI_MODULE = "yiban.cli"

#: 会拉起子进程的函数名（`-m <模块>` 起进程的宿主）。
_LAUNCH_FUNCS = frozenset((
    "Popen", "run", "call", "check_call", "check_output", "system",
    "spawnv", "spawnve", "spawnvp", "spawnl", "spawnle", "spawnlp",
    "execv", "execve", "execvp", "execl", "execle", "execlp",
))


def _runtime_files(root):
    """运行期载体文件清单（.py 与 .sh）。"""
    out = []
    for d in RUNTIME_PY_DIRS:
        for dirpath, dirs, files in os.walk(os.path.join(root, d)):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            for name in sorted(files):
                if name.endswith((".py", ".sh")):
                    out.append(os.path.join(dirpath, name))
    for name in RUNTIME_SHELL_NAMES:
        path = os.path.join(root, name)
        if os.path.exists(path):
            out.append(path)
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


def old_coordinate_hits(path):
    """返回该文件里引用旧坐标的位置：`(行号, 说明)` 列表。

    Python：① 进程拉起调用的命令行字面量含旧模块名；② 非 docstring 的字符串常量
    恰为旧模块名（覆盖 importlib / 变量间接形态）。
    Shell：非注释行出现旧模块名。
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    hits = []
    if path.endswith(".sh"):
        pat = re.compile(r"(?<![\w.-])" + re.escape(OLD_CLI_MODULE) + r"(?![\w.-])")
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if pat.search(s):
                hits.append((i, "shell 命令行含 " + OLD_CLI_MODULE))
        return hits
    tree = ast.parse(text)
    docs = _docstring_const_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if fname in _LAUNCH_FUNCS and OLD_CLI_MODULE in _command_literals(node):
                hits.append((node.lineno, "启动命令含 " + OLD_CLI_MODULE))
        if isinstance(node, ast.Constant) and node.value == OLD_CLI_MODULE \
                and id(node) not in docs:
            hits.append((node.lineno, "非 docstring 字符串常量 " + OLD_CLI_MODULE))
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

            clean = os.path.join(tmp, "clean.py")
            with open(clean, "w", encoding="utf-8") as f:
                f.write('"""-m yiban.cli 与 yiban.cli 都只是历史叙述。"""\n'
                        '# -m yiban.cli\n'
                        'X = 1\n')
            self.assertEqual(old_coordinate_hits(clean), [], "注释/docstring 被误判成启动点")


if __name__ == "__main__":
    unittest.main(verbosity=2)
