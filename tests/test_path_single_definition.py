# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""日志与按日状态件的路径事实：单一定义点守卫（census-P2-log-file / sign-day-log /
sign-state）。

标签：B · 状态件与清理

不变量（任何时刻都不该被违反）：
  ① `sign.log` 基名、`sign-<day>.log`、`sign-state-<day>.json`、`sign-daily-<day>.json`
     的形状只在 `yiban/adapters/config/env_io.py` 定义一次；bash 侧的同一形状只在
     `scripts/lib/host_paths.sh` 定义一次。
  ② 每个读者都从定义点派生：把定义函数替换成哨兵值后，读者的产物必须跟着变
     （派生 vs 各写一份，只有这条能分开）。
  ③ 两种语言的产物逐字相同（bash 的宿主脚本与 Python 的引擎写同一个文件）。
  ④ 生产树里再出现一处手写形状 ⇒ 本用例红，并点名 `文件:行`。

扫描面与口径：只扫入库文件（`git ls-files`）里的代码载体（py/sh/yml/tsv），
剥掉注释再匹配（判据是"代码里写了一遍形状"，文档与注释描述文件名不算）。
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from yiban.adapters.config import env_io  # noqa: E402
from yiban.adapters.statefiles import state_gc, state_io  # noqa: E402

BASH = shutil.which("bash")

#: 唯一允许出现"手写形状"的两个定义点（Python / bash 各一份，③ 钉住两者同形）
DEFINITION_FILES = {
    "yiban/adapters/config/env_io.py",
    "scripts/lib/host_paths.sh",
}

#: 代码载体（文档与前端不在判定面：文档是描述，不是写者）
SCAN_SUFFIXES = (".py", ".sh", ".yml", ".yaml", ".tsv")
SCAN_SKIP_PREFIXES = ("tests/", "frontend/", "docs/")
SCAN_ROOTS = ("yiban/", "scripts/", "web/", "docker/", "deploy/")
SCAN_ROOT_FILES = ("run.sh", "run_probe.sh", "docker-compose.yml")

#: 手写形状：由变量拼出文件名，或用通配符描述文件名
SHAPE_PATTERNS = (
    ("按天日志名", re.compile(r"sign-(?:\{|%|\$|\*)")),
    ("按日状态名", re.compile(r"sign-state-(?:\{|%|\$|\*)")),
    ("旧按日状态名", re.compile(r"sign-daily-(?:\{|%|\$|\*)")),
)

#: 模式自检样本：证明正则仍是活的（改定义点后生产树里本来就不该再有命中，
#: 故"活体"只能由样本证明——否则把正则改成永假也能绿）。
SHAPE_PROBES = {
    "按天日志名": 'p = f"sign-{day}.log"',
    "按日状态名": 'p = os.path.join(d, f"sign-state-{day}.json")',
    "旧按日状态名": 'g = "sign-daily-*.json"',
}


def _strip_line_comment(line):
    """剥掉一行里字符串之外的 `#` 注释（近似够用：本仓注释一律在行首或空白后）。"""
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#":
            return line[:i]
        i += 1
    return line


def _code_lines(text):
    """→ [(行号, 剥注释与 docstring 后的代码)]。

    docstring 与 `#` 注释描述文件名不算"写了一遍形状"；代码里的 f-string / glob 才算。
    三引号块按 Python 口径处理（本仓 .sh 里没有三引号块）。
    """
    out = []
    in_doc = None
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw
        if in_doc:
            end = line.find(in_doc)
            if end < 0:
                continue
            line = line[end + 3:]
            in_doc = None
        for quote in ('"""', "'''"):
            start = line.find(quote)
            if start >= 0:
                end = line.find(quote, start + 3)
                if end < 0:
                    in_doc = quote
                    line = line[:start]
                else:
                    line = line[:start] + " " + line[end + 3:]
                break
        code = _strip_line_comment(line)
        if code.strip():
            out.append((no, code))
    return out


def _tracked_files():
    r = subprocess.run(["git", "ls-files"], cwd=BASE, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git ls-files 失败: {r.stderr}")
    return [p.replace("\\", "/") for p in r.stdout.splitlines() if p.strip()]


def _scan_face():
    files = []
    for rel in _tracked_files():
        if rel.startswith(SCAN_SKIP_PREFIXES):
            continue
        if not rel.endswith(SCAN_SUFFIXES):
            continue
        if rel in SCAN_ROOT_FILES or rel.startswith(SCAN_ROOTS):
            files.append(rel)
    return files


class DefinitionPointTest(unittest.TestCase):
    """① 定义点存在且形状与既有值逐字相同（改值 = 改口径，必须显式改这里）。"""

    def test_log_names(self):
        self.assertEqual("sign.log", env_io.LOG_BASENAME)
        self.assertEqual("sign-2026-01-02.log", env_io.day_log_name("2026-01-02"))

    def test_state_names(self):
        self.assertEqual("sign-state-2026-01-02.json",
                         os.path.basename(env_io.sign_state_path("2026-01-02", state_dir="/tmp/x")))
        self.assertEqual("sign-daily-2026-01-02.json",
                         os.path.basename(env_io.sign_daily_path("2026-01-02", state_dir="/tmp/x")))

    def test_log_path_follows_log_file_dir(self):
        self.assertEqual(os.path.join("/srv/logs", "sign-2026-01-02.log"),
                         env_io.day_log_path("2026-01-02", log_file="/srv/logs/sign.log"))

    def test_default_log_file_lives_under_state_dir(self):
        env = {"YIBAN_STATE_DIR": "/srv/state"}
        self.assertEqual(os.path.join("/srv/state", "sign.log"),
                         env_io.log_file_path(env=env, env_file=os.devnull))
        self.assertEqual(os.path.join("/srv/state", "sign-2026-01-02.log"),
                         env_io.day_log_path("2026-01-02", env=env, env_file=os.devnull))

    def test_log_dir_follows_log_file(self):
        env = {"YIBAN_LOG_FILE": "/srv/logs/custom.log"}
        self.assertEqual("/srv/logs", env_io.log_dir_path(env=env, env_file=os.devnull))


class PythonReadersDeriveTest(unittest.TestCase):
    """② 读者从定义点派生：替换定义函数 ⇒ 读者产物跟着变。"""

    def test_logging_handler_uses_definition(self):
        from yiban import logging_ext
        tmp = tempfile.mkdtemp(prefix="logdef-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.object(env_io, "day_log_name", return_value="SENTINEL.log"):
            handler = logging_ext.DailyFlockFileHandler(tmp)
        try:
            self.assertTrue(handler.baseFilename.endswith("SENTINEL.log"),
                            f"日志 handler 没走定义点：{handler.baseFilename}")
        finally:
            handler.close()

    def test_web_log_path_uses_definition(self):
        from web.services import logs as logs_svc
        old = dict(logs_svc._hooks)
        self.addCleanup(lambda: (logs_svc._hooks.clear(), logs_svc._hooks.update(old)))
        logs_svc.configure(log_file=lambda: "/srv/logs/sign.log",
                           state_dir=lambda: "/srv/state",
                           tail_lines=lambda _p: [])
        with mock.patch.object(env_io, "day_log_name", return_value="SENTINEL.log"):
            got = logs_svc.log_path_for("2026-01-02")
        self.assertEqual(os.path.join("/srv/logs", "SENTINEL.log"), got,
                         f"web 日志路径没走定义点：{got}")

    def test_web_state_readers_use_definition(self):
        from web.services import logs as logs_svc
        sentinel = []

        def _fake_state(day, state_dir=None):
            sentinel.append(("state", day, state_dir))
            return os.path.join(state_dir, "SENTINEL-state.json")

        def _fake_daily(day, state_dir=None):
            sentinel.append(("daily", day, state_dir))
            return os.path.join(state_dir, "SENTINEL-daily.json")

        with mock.patch.object(env_io, "sign_state_path", side_effect=_fake_state), \
                mock.patch.object(env_io, "sign_daily_path", side_effect=_fake_daily):
            logs_svc.load_sign_state_ex("/srv/state", "2026-01-02")
        self.assertEqual([("state", "2026-01-02", "/srv/state"),
                          ("daily", "2026-01-02", "/srv/state")], sentinel,
                         "web 按日状态读取没走定义点")

    def test_state_io_delegates(self):
        with mock.patch.object(env_io, "sign_state_path", return_value="/x/SENTINEL.json") as fn:
            self.assertEqual("/x/SENTINEL.json",
                             state_io.sign_state_path("2026-01-02", state_dir="/x"))
        fn.assert_called_once()
        self.assertIn("/x", fn.call_args.kwargs.get("state_dir", ""),
                      "state_io 没把 state_dir 传给定义点")
        # 缺省目录仍走 `_state_dir()`（既有的测试打桩面），但形状仍由 env_io 给。
        with mock.patch.object(state_io, "_state_dir", return_value="/y"):
            self.assertEqual("sign-state-2026-01-02.json",
                             os.path.basename(state_io.sign_state_path("2026-01-02")))

    def test_ledger_check_uses_definition(self):
        import importlib.util
        path = os.path.join(BASE, "scripts", "ledger_check.py")
        spec = importlib.util.spec_from_file_location("ledger_check_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        tmp = tempfile.mkdtemp(prefix="ledgerdef-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        seen = []

        def _fake(day, state_dir=None):
            seen.append((day, state_dir))
            return os.path.join(state_dir, "SENTINEL.json")

        with mock.patch.object(env_io, "sign_state_path", side_effect=_fake):
            mod._read_terminals(tmp, "2026-01-02")
        self.assertEqual([("2026-01-02", tmp)], seen,
                         "ledger_check 没走 sign-state 定义点")

    def test_state_gc_roster_uses_definition(self):
        self.assertEqual(env_io.DAY_LOG_PREFIX, state_gc.ARTIFACTS[0].prefix)
        self.assertEqual(env_io.DAY_LOG_SUFFIX, state_gc.ARTIFACTS[0].suffix)
        arts = {a.prefix: a for a in state_gc.ARTIFACTS}
        self.assertEqual(env_io.SIGN_STATE_SUFFIX, arts[env_io.SIGN_STATE_PREFIX].suffix)
        self.assertEqual(env_io.SIGN_DAILY_SUFFIX, arts[env_io.SIGN_DAILY_PREFIX].suffix)

    def test_runner_daily_writer_uses_definition(self):
        from yiban.app import runner
        tmp = tempfile.mkdtemp(prefix="dailydef-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        seen = []

        def _fake(day, state_dir=None):
            seen.append((day, state_dir))
            return os.path.join(state_dir, "SENTINEL-daily.json")

        with mock.patch.object(env_io, "sign_daily_path", side_effect=_fake):
            runner._write_sign_daily(tmp, [], {})
        self.assertEqual(1, len(seen), "runner 没走 sign-daily 定义点")
        self.assertEqual(tmp, seen[0][1])


@unittest.skipIf(BASH is None, "需要 bash（Git Bash / WSL）")
class CrossLanguageShapeTest(unittest.TestCase):
    """③ bash 宿主脚本与 Python 引擎算出同一个文件路径。"""

    def _run(self, state_dir, log_file):
        tmp = tempfile.mkdtemp(prefix="hostpaths-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        stub = os.path.join(tmp, "python3")
        with open(stub, "w", encoding="utf-8", newline="\n") as f:
            f.write('#!/bin/sh\necho 2026-01-02\n')
        os.chmod(stub, 0o755)
        script = (
            'set -eu; APP_DIR=%s; PY=%s; STATE_DIR=%s; '
            'YIBAN_LOG_FILE=%s; '
            '. "$APP_DIR/scripts/lib/host_paths.sh"; '
            'yiban_day_log_file "$STATE_DIR"\n'
            % (_shq(BASE), _shq(stub), _shq(state_dir), _shq(log_file))
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
        r = subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=60)
        self.assertEqual(0, r.returncode, r.stderr)
        return r.stdout.strip()

    def test_custom_log_file(self):
        got = self._run("/srv/state", "/srv/logs/custom.log")
        self.assertEqual(os.path.join("/srv/logs", "sign-2026-01-02.log"), got,
                         f"bash 侧与定义点不同形：{got}")

    def test_default_log_file(self):
        got = self._run("/srv/state", "")
        self.assertEqual(os.path.join("/srv/state", "sign-2026-01-02.log"), got,
                         f"bash 侧默认日志名与定义点不同形：{got}")


def _shq(value):
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


class RepoRootSingleKeyTest(unittest.TestCase):
    """census-P2-repo-root：部署根只认一个键（YIBAN_APP_DIR）＋一个解析器。

    宿主脚本要么 source `scripts/lib/host_paths.sh`（检出内脚本按自身位置反推），
    要么读唯一环境键（安装到检出外的脚本，如 backup.sh）。旧名 `APP_DIR` /
    `YIBAN_APP_ROOT` 只许作别名出现在同一个赋值表达式里。
    """

    #: 允许出现旧键名的文件（唯一解析器 + 安装到检出外、只能读环境键的脚本）
    ALIAS_FILES = {  # noqa: RUF012 —— 类常量映射，测试内只读
        "scripts/lib/host_paths.sh": "唯一解析器：唯一键 + 两个旧名别名",
        "scripts/check-cron-provenance.sh": "生产检出根（环境键，不是自身位置）",
        "scripts/backup.sh": "安装到 /usr/local/sbin，不能按自身位置反推",
        "scripts/yiban-backup-sentinel.sh": "同上（安装件）",
        "scripts/backup_sentinel.py": "同上（安装件的实现）",
    }

    def test_app_dir_assignments_use_single_key_or_resolver(self):
        offenders = []
        for rel in _scan_face():
            if not rel.endswith((".sh", ".py")):
                continue
            with open(os.path.join(BASE, rel), encoding="utf-8", errors="replace") as f:
                code = "\n".join(line for _no, line in _code_lines(f.read()))
            if "APP_DIR=" not in code and "APP_DIR =" not in code:
                continue
            if "host_paths.sh" in code or "YIBAN_APP_DIR" in code:
                continue
            offenders.append(rel)
        self.assertEqual([], offenders,
                         "以下脚本自己另起一套部署根解析（必须走 host_paths.sh 或唯一键）：\n  "
                         + "\n  ".join(offenders))

    def test_old_key_names_only_appear_in_registered_alias_files(self):
        # 只判"读环境键"的形态：`${APP_DIR...}`（shell）与 `"APP_DIR"`（Python）。
        # 各入口把解析结果赋给名叫 APP_DIR 的**普通变量**不算读旧键。
        read_forms = {
            "YIBAN_APP_ROOT": re.compile(r"YIBAN_APP_ROOT"),
            # 只认"带默认值的环境键展开"：`$APP_DIR` 是各入口的普通变量，不是读旧键
            "APP_DIR(env)": re.compile(r"\$\{APP_DIR:-"),
            "APP_DIR(py env)": re.compile(r"""[(_]env\(\s*["']APP_DIR["']"""),
        }
        hits = []
        for rel in _scan_face():
            if not rel.endswith((".sh", ".py")):
                continue   # 名册 tsv 里的模式列不是载体
            with open(os.path.join(BASE, rel), encoding="utf-8", errors="replace") as f:
                code = "\n".join(line for _no, line in _code_lines(f.read()))
            for label, rx in read_forms.items():
                if rx.search(code) and rel not in self.ALIAS_FILES:
                    hits.append(f"{label}  {rel}")
        self.assertEqual([], hits,
                         "旧键名出现在未登记的载体里（新读者应读 YIBAN_APP_DIR）：\n  "
                         + "\n  ".join(sorted(set(hits))))


class ProxySingleReadTest(unittest.TestCase):
    """census-P2-yiban-proxy：`YIBAN_PROXY` 只由 egress.resolve 读取。

    客户端构造期若再裸读环境键，就多出第二套读取：清单/兜底/空位直连的解析规则
    一变，这条路径静默用旧值。这里替换 `egress.resolve` 的返回值，客户端必须跟着变。
    """

    def test_client_uses_egress_resolve(self):
        import types

        from yiban import client as client_mod
        from yiban import egress
        account = types.SimpleNamespace(
            phone="13800000000", password="x", phone_model="m", phone_code="c")
        with mock.patch.object(egress, "resolve", return_value="http://sentinel:3128") as fn:
            c = client_mod.YibanClient(account)
        fn.assert_called_once()
        self.assertEqual(egress.ROLE_SINGLE, fn.call_args.args[0],
                         "客户端没按单执行体角色取出口")
        self.assertEqual({"http": "http://sentinel:3128", "https": "http://sentinel:3128"},
                         c.session.proxies, "客户端没用解析结果设置 session 代理")

    def test_no_second_bare_reader_in_production(self):
        offenders = []
        for rel in _scan_face():
            if not rel.endswith(".py") or rel == "yiban/egress.py":
                continue
            with open(os.path.join(BASE, rel), encoding="utf-8", errors="replace") as f:
                code = "\n".join(line for _no, line in _code_lines(f.read()))
            if re.search(r"""(?:environ\.get|getenv|environ)\s*[\[(]\s*["']YIBAN_PROXY["']""", code):
                offenders.append(rel)
        self.assertEqual([], offenders,
                         "以下模块裸读 YIBAN_PROXY，必须改走 egress.resolve：\n  "
                         + "\n  ".join(offenders))


class RequestsPerAccountTest(unittest.TestCase):
    """census-P2-req6：每账号请求数是具名常量，不是散在散文里的 6。"""

    def test_constant_matches_the_chain_split(self):
        from yiban import platform
        self.assertEqual(platform.LOGIN_CHAIN_REQUESTS + platform.SIGNIN_CHAIN_REQUESTS,
                         platform.REQUESTS_PER_ACCOUNT)
        self.assertEqual(6, platform.REQUESTS_PER_ACCOUNT,
                         "六跳链路（登录 4 + 签到 2）的值变了就必须显式改这条断言")


class FakeServerTraceNameTest(unittest.TestCase):
    """census-P2-fake-server：假上游 JSONL 的基名只有一个定义点。"""

    def test_trace_path_uses_the_basename(self):
        from yiban.adapters import protocol_fake
        self.assertEqual(os.path.join("/tmp/x", protocol_fake.TRACE_BASENAME),
                         protocol_fake.trace_path("/tmp/x"))

    def test_no_foreign_literal_in_production(self):
        offenders = []
        for rel in _scan_face():
            if rel == "yiban/adapters/protocol_fake/__init__.py":
                continue
            with open(os.path.join(BASE, rel), encoding="utf-8", errors="replace") as f:
                code = "\n".join(line for _no, line in _code_lines(f.read()))
            # `c7-mock.jsonl` 是演练证据副本名（不同的件），不算假上游轨迹名
            if re.search(r"(?<![\w-])mock\.jsonl", code):
                offenders.append(rel)
        self.assertEqual([], offenders,
                         "以下位置另写了假上游轨迹名，必须引用 protocol_fake.TRACE_BASENAME：\n  "
                         + "\n  ".join(offenders))


class SignDailySingleChannelTest(unittest.TestCase):
    """census-P2-sign-daily：符号表双向可解 ＋ 写者只走私有写单通道。"""

    def test_every_written_symbol_reverses_to_a_real_status(self):
        from yiban.core import status as st  # noqa: I001 —— 与被导入模块同组，ruff 的排序规则不适用
        from web.services import logs as logs_svc
        # `runner._write_sign_daily` 允许写出的状态集合（与它自身的白名单同源）
        emitted = (st.STATUS_SUCCESS, st.STATUS_ALREADY, st.STATUS_NO_TASK,
                   st.STATUS_FAILED, st.STATUS_NO_POSITION, st.STATUS_SUPPLEMENTING)
        for code in emitted:
            with self.subTest(status=code):
                sym = st.SYMBOL[code]
                back = logs_svc._SYM_TO_STATUS.get(sym)
                self.assertIsNotNone(back, f"符号 {sym} 无法反查（会落成 pending）")
                self.assertNotEqual(st.STATUS_PENDING, back,
                                    f"符号 {sym} 反查成 pending = 面板假信号")

    def test_demo_writer_uses_the_private_channel(self):
        path = os.path.join(BASE, "scripts", "dev_visual_seed.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("private_json.write_private_json(", src,
                      "演示脚本的按日状态件没走状态文件私有写单通道")
        self.assertNotIn("_atomic_write(env_io.sign_daily_path(", src,
                         "演示脚本仍用通用原子写落按日状态件")


class NoForeignHandwrittenShapeTest(unittest.TestCase):
    """④ 生产树再出现一处手写形状 ⇒ 红、点名。"""

    def test_only_definition_files_spell_the_shape(self):
        # 模式自检：正则必须能在样本上开火（否则永假正则也是"绿"）
        for name, pattern in SHAPE_PATTERNS:
            self.assertIsNotNone(pattern.search(SHAPE_PROBES[name]),
                                 f"判定模式已失效：{name}")
        files = _scan_face()
        self.assertGreaterEqual(len(files), 50,
                                f"扫描面太小（{len(files)}），门可能没扫到东西")
        hits = []
        for rel in files:
            with open(os.path.join(BASE, rel), encoding="utf-8", errors="replace") as f:
                for no, code in _code_lines(f.read()):
                    for name, pattern in SHAPE_PATTERNS:
                        if pattern.search(code) and rel not in DEFINITION_FILES:
                            hits.append(f"{name}  {rel}:{no}  {code.strip()}")
        self.assertEqual([], hits,
                         "以下位置各写了一遍路径形状，必须改走 env_io / host_paths.sh：\n  "
                         + "\n  ".join(hits))


if __name__ == "__main__":
    unittest.main(verbosity=2)
