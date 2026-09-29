# -*- coding: utf-8 -*-
"""MF-60 契约用例（一）：`--json` 在任何退出路径上都必须留下可解析的一整行结构化对象。

标签：J · 运维：部署/备份/发布
覆盖：非法参数/未知子命令/缺子命令/多余参数/互斥开关（一律 rc=2）、`--check-config`
    摘要不得混入 stdout、每个成功 `--json` 都带 `exit_code`、`db --backup` 覆盖守卫
    （目标已存在须 `--force`）、`db --restore` 破坏性守卫（默认 dry-run / 指纹回显 /
    覆盖前副本 / 损坏备份拒绝，仅用自造库与自造副本）、`--help` 列明四个路径环境变量。
对应实现：`yiban/cli.py`（`_emit_json_error` / `_fail` / `_UsageError` / `main` 解析兜底）
    与 `yiban/engine/config_check.print_config_summary`。
关键断言：`--json` 的任意参数组合下 stdout 恰为一行、可 `json.loads`、含 `exit_code`；
    人类可读 usage/摘要仍走 stderr。
依赖：起 `sys.executable -m yiban.cli` 子进程（纯 Python）；临时 STATE/LOG/DB/ENV，
    不碰本机真实 .env；无网络/bash/docker。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 六种非法参数 + 两种无子命令形态（登记原文点名"输出逐字节相同"的那一族）
ILLEGAL_ARGV = (
    ["nope", "--json"],
    ["--json"],
    ["version", "--bogus", "--json"],
    ["db", "--status", "--integrity", "--json"],
    ["state", "--yes", "--dry-run", "--json"],
    ["capacity", "--measure", "--json"],
    ["sign", "--bogus", "--json"],
)


def _cli_env(tmp_path):
    """隔离环境：临时路径四件套 + 剥掉继承的全部 YIBAN_*（防串进真实部署）。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
    env.update({
        "YIBAN_STATE_DIR": os.path.join(tmp_path, "state"),
        "YIBAN_LOG_FILE": os.path.join(tmp_path, "logs", "sign.log"),
        "YIBAN_DB_FILE": os.path.join(tmp_path, "yiban.db"),
        "YIBAN_ENV_FILE": os.path.join(tmp_path, ".env"),
        "YIBAN_ACCOUNTS_KEY": "a" * 64,
        "YIBAN_MAIL_ENABLE": "0",
        "PYTHONPATH": BASE,
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def _run(argv, env, timeout=120, cwd=BASE):
    return subprocess.run([sys.executable, "-m", "yiban.cli", *argv], cwd=cwd, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL, timeout=timeout)


def _one_json_line(testcase, proc):
    """stdout 必须恰为一行、可解析、是对象且含 exit_code（MF-60 的 `--json` 契约）。"""
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    testcase.assertEqual(len(lines), 1, f"stdout 必须恰一行 JSON：{lines!r}")
    payload = json.loads(lines[0])
    testcase.assertIsInstance(payload, dict)
    testcase.assertIn("exit_code", payload, payload)
    return payload


class JsonErrorOnEveryExitPathTest(unittest.TestCase):
    """非法参数（rc=2）路径不得再出现 stdout 零字节：`--json` 必须打一行结构化错误。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-cli-mf60a-")
        self.addCleanup(self.tmp.cleanup)
        self.env = _cli_env(self.tmp.name)

    def test_illegal_args_json_is_single_parseable_line(self):
        for argv in ILLEGAL_ARGV:
            with self.subTest(argv=argv):
                r = _run(argv, self.env)
                self.assertEqual(r.returncode, 2, r.stderr[-400:])
                payload = _one_json_line(self, r)
                self.assertEqual(payload["exit_code"], 2)
                self.assertFalse(payload["ok"], payload)
                self.assertIn("command", payload)
                self.assertTrue(payload.get("errors"), "错误对象必须带 errors 明细")
                self.assertTrue(r.stderr.strip(), "人类可读 usage 仍应走 stderr")

    def test_illegal_args_json_distinguishable(self):
        """六种非法参数 + 缺子命令的输出不得再"逐字节相同"。"""
        seen = {}
        for argv in ILLEGAL_ARGV:
            with self.subTest(argv=argv):
                r = _run(argv, self.env)
                payload = _one_json_line(self, r)
                key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                self.assertNotIn(key, seen, f"{argv} 与 {seen.get(key)} 的 --json 输出相同")
                seen[key] = argv

    def test_success_json_has_exit_code(self):
        """每个维护子命令的成功 `--json` 也带 exit_code（调用方统一按它分支）。"""
        for argv in (["version", "--json"], ["config", "--json"], ["capacity", "--json"],
                     ["state", "--json"], ["db", "--status", "--json"]):
            with self.subTest(argv=argv):
                r = _run(argv, self.env)
                payload = _one_json_line(self, r)
                self.assertEqual(payload["exit_code"], r.returncode)

    def test_check_config_summary_not_on_stdout(self):
        """`sign --check-config --json`：摘要走 stderr，stdout 只剩一行 JSON。"""
        items = [{"phone": "13800001234", "password": "pw", "phone_model": "Pixel 7",
                  "phone_code": "code-1"}]
        env = dict(self.env, YIBAN_ACCOUNTS_JSON=json.dumps(items))
        r = _run(["sign", "--check-config", "--json"], env)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        payload = _one_json_line(self, r)
        self.assertEqual(payload["command"], "sign")
        self.assertIn("账号配置检查", r.stderr, "摘要必须仍可见（走 stderr）")
        self.assertNotIn("138****1234", r.stdout, "摘要不得混进 stdout")


class ReadOnlyPromiseTest(unittest.TestCase):
    """只读子命令跑完必须真的没写盘：库不存在仍不存在、存在则 mtime/内容不变。

    登记原文的验收不变量：任一"只读"子命令跑完后 `git status`/库文件 mtime 不变。
    """

    READONLY_ARGV = (["config", "--json"], ["version", "--json"],
                     ["db", "--status", "--json"], ["db", "--integrity", "--json"])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-cli-mf60b-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        # 造库的独立环境（显式 YIBAN_DB_FILE）；只读命令另用 _cwd_env
        self.db_env = _cli_env(self.root)

    def _cwd_env(self, cwd, **extra):
        """只读命令环境：库路径**不写 YIBAN_DB_FILE**（走 cwd 相对默认 yiban.db）。

        日志/状态目录挪到 cwd 之外——验收不变量排除"既有日志目录"，把日志放进别的
        目录才能让"cwd 里不得出现任何新文件"成为干净判据。
        """
        env = _cli_env(cwd)
        env.pop("YIBAN_DB_FILE", None)
        env["YIBAN_STATE_DIR"] = os.path.join(self.root, "state")
        env["YIBAN_LOG_FILE"] = os.path.join(self.root, "logs", "sign.log")
        env.update(extra)
        return env

    def _entries(self, path):
        return sorted(os.listdir(path))

    def test_readonly_commands_do_not_create_db_in_empty_cwd(self):
        """空目录 + 只有 .env：只读命令不得当场建出 yiban.db（登记原文黑箱复现点）。"""
        work = os.path.join(self.root, "empty")
        os.makedirs(work)
        with open(os.path.join(work, ".env"), "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_JSON=[]\n")
        before = self._entries(work)
        for argv in self.READONLY_ARGV:
            with self.subTest(argv=argv):
                r = _run(argv, self._cwd_env(work), cwd=work)
                self.assertIn(r.returncode, (0, 1), r.stderr[-300:])
                _one_json_line(self, r)
        after = self._entries(work)
        self.assertNotIn("yiban.db", after, "只读命令建出了 yiban.db")
        self.assertEqual(after, before, f"只读命令在 cwd 留下了新文件：{after}")

    def test_readonly_commands_leave_existing_db_untouched(self):
        """已有库：mtime、user_version、文件集合全不变（连 WAL 的 -shm/-wal 都不新增）。"""
        from yiban.store import migrations
        work = os.path.join(self.root, "hasdb")
        os.makedirs(work)
        db_file = os.path.join(work, "yiban.db")
        code = ("import sys; from yiban.store import db;"
                "db.init_db(db_file=sys.argv[1], env_file=sys.argv[2], cleanup=False);"
                "db.get_conn().close()")
        env = dict(self.db_env, YIBAN_DB_FILE=db_file)
        r = subprocess.run([sys.executable, "-c", code, db_file,
                            os.path.join(work, ".env")], cwd=BASE, env=env,
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        before_entries = self._entries(work)
        before_mtime = os.path.getmtime(db_file)
        before_size = os.path.getsize(db_file)

        for argv in self.READONLY_ARGV:
            with self.subTest(argv=argv):
                r = _run(argv, env, cwd=work)
                self.assertIn(r.returncode, (0, 1), r.stderr[-300:])
                _one_json_line(self, r)

        self.assertEqual(os.path.getmtime(db_file), before_mtime, "只读命令改动了库 mtime")
        self.assertEqual(os.path.getsize(db_file), before_size, "只读命令改动了库大小")
        self.assertEqual(self._entries(work), before_entries,
                         "只读命令留下了新文件（含 -shm/-wal）")
        # 内容级：schema 顶与表清单不变
        import sqlite3
        conn = sqlite3.connect(db_file)
        try:
            self.assertEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]),
                             migrations._MIGRATIONS[-1][0])
        finally:
            conn.close()


class BackupOverwriteGuardTest(unittest.TestCase):
    """`db --backup` 不得静默覆盖上一份副本（MF-60）：目标已存在时必须 `--force`。

    验收不变量：同一路径连续 `--backup --yes` 两次，第二份不得覆盖第一份——未带
    `--force` 时拒绝且非 0（零写入），带 `--force` 才允许。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-cli-mf60backup-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.db_file = os.path.join(self.root, "yiban.db")
        self.target = os.path.join(self.root, "copy.db")
        self.env = dict(_cli_env(self.root), YIBAN_DB_FILE=self.db_file)
        self._init_db()

    def _init_db(self):
        code = ("import sys; from yiban.store import db;"
                "db.init_db(db_file=sys.argv[1], env_file=sys.argv[2], cleanup=False);"
                "db.get_conn().close()")
        r = subprocess.run([sys.executable, "-c", code, self.db_file,
                            os.path.join(self.root, ".env")], cwd=BASE, env=self.env,
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])

    def test_second_backup_without_force_is_refused(self):
        first = _run(["db", "--backup", self.target, "--yes", "--json"], self.env)
        self.assertEqual(first.returncode, 0, first.stderr[-400:])
        self.assertTrue(os.path.exists(self.target))
        _one_json_line(self, first)
        before = os.path.getmtime(self.target)

        second = _run(["db", "--backup", self.target, "--yes", "--json"], self.env)
        self.assertEqual(second.returncode, 1, second.stderr[-400:])
        payload = _one_json_line(self, second)
        self.assertFalse(payload["ok"], payload)
        self.assertFalse(payload["overwrite_allowed"], payload)
        self.assertEqual(payload["error_kind"], "runtime_error", payload)
        self.assertEqual(os.path.getmtime(self.target), before,
                         "未带 --force 的第二次备份改动了已存在的副本")

    def test_backup_force_allows_overwrite(self):
        _run(["db", "--backup", self.target, "--yes", "--json"], self.env)
        forced = _run(["db", "--backup", self.target, "--yes", "--force", "--json"], self.env)
        self.assertEqual(forced.returncode, 0, forced.stderr[-400:])
        payload = _one_json_line(self, forced)
        self.assertTrue(payload["overwrite_allowed"], payload)

    def test_backup_dry_run_reports_force_needed_without_writing(self):
        _run(["db", "--backup", self.target, "--yes", "--json"], self.env)
        before = os.path.getmtime(self.target)
        plan = _run(["db", "--backup", self.target, "--json"], self.env)
        self.assertEqual(plan.returncode, 0, plan.stderr[-400:])
        payload = _one_json_line(self, plan)
        self.assertTrue(payload["dry_run"], payload)
        self.assertFalse(payload["overwrite_allowed"], payload)
        self.assertEqual(os.path.getmtime(self.target), before, "dry-run 不得写盘")


class RestoreGuardTest(unittest.TestCase):
    """`db --restore`：破坏性操作必须默认 dry-run、`--yes` 回显指纹、恢复前留副本。

    只用**自造库与自造副本**，绝不碰任何真实生产备份。验收不变量：不带 `--yes` 不改动
    任何库文件；备份损坏时拒绝并给非 0 码；成功恢复后 `db --integrity` ok 且
    `user_version` 与备份一致；覆盖前自动留一份副本。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-cli-mf60restore-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.db_file = os.path.join(self.root, "yiban.db")
        self.backup = os.path.join(self.root, "copy.db")
        self.env = dict(_cli_env(self.root), YIBAN_DB_FILE=self.db_file)
        self._sqlite(self.db_file,
                     "import sys; from yiban.store import db;"
                     "db.init_db(db_file=sys.argv[1], env_file=sys.argv[2], cleanup=False);"
                     "db.get_conn().close()", os.path.join(self.root, ".env"))
        self.assertEqual(
            _run(["db", "--backup", self.backup, "--yes", "--json"], self.env).returncode, 0)

    def _sqlite(self, db_file, code, *extra):
        r = subprocess.run([sys.executable, "-c", code, db_file, *extra], cwd=BASE,
                           env=self.env, capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])

    def _user_version(self, path):
        import sqlite3
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()

    def _fingerprint(self):
        r = _run(["db", "--restore", self.backup, "--json"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        return _one_json_line(self, r)["fingerprint"]

    def test_dry_run_does_not_touch_live_db(self):
        """不带 --yes：只报告计划 + 指纹，当前库零改动、目录无新文件。"""
        before_entries = sorted(os.listdir(self.root))
        before_mtime = os.path.getmtime(self.db_file)
        r = _run(["db", "--restore", self.backup, "--json"], self.env)
        payload = _one_json_line(self, r)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertTrue(payload["dry_run"], payload)
        self.assertTrue(payload["fingerprint"], payload)
        self.assertEqual(os.path.getmtime(self.db_file), before_mtime, "dry-run 改动了当前库")
        self.assertEqual(sorted(os.listdir(self.root)), before_entries, "dry-run 留下了新文件")

    def test_yes_without_fingerprint_is_refused(self):
        before_mtime = os.path.getmtime(self.db_file)
        r = _run(["db", "--restore", self.backup, "--yes", "--json"], self.env)
        self.assertEqual(r.returncode, 2, r.stderr[-400:])
        payload = _one_json_line(self, r)
        self.assertEqual(payload["error_kind"], "confirmation_required", payload)
        self.assertEqual(os.path.getmtime(self.db_file), before_mtime, "拒绝路径改动了当前库")

    def test_wrong_fingerprint_is_refused(self):
        r = _run(["db", "--restore", self.backup, "--yes", "--fingerprint", "PURGE-deadbeef",
                  "--json"], self.env)
        self.assertEqual(r.returncode, 2, r.stderr[-400:])
        self.assertEqual(_one_json_line(self, r)["error_kind"], "confirmation_required")

    def test_yes_restores_and_keeps_pre_restore_copy(self):
        """成功恢复：内容回到备份态、user_version 一致、覆盖前副本留下旧内容。"""
        import sqlite3
        self._sqlite(self.db_file,
                     "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]);"
                     "c.execute('PRAGMA user_version=777');"
                     "c.execute(\"INSERT INTO app_meta(key, value) VALUES('restore-probe','1')\");"
                     "c.commit(); c.close()")
        fp = self._fingerprint()
        r = _run(["db", "--restore", self.backup, "--yes", "--fingerprint", fp, "--json"],
                 self.env)
        payload = _one_json_line(self, r)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertTrue(payload["integrity_ok"], payload)
        self.assertEqual(self._user_version(self.db_file), payload["backup_user_version"])
        pre = payload["pre_restore_copy"]
        self.assertTrue(pre and os.path.exists(pre), payload)
        self.assertEqual(self._user_version(pre), 777, "覆盖前副本必须保留旧库内容")
        # 恢复后库里没有那条探针行
        conn = sqlite3.connect(self.db_file)
        try:
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key='restore-probe'").fetchone()
        finally:
            conn.close()
        self.assertIsNone(row, "恢复后仍残留旧库的探针行")

    def test_corrupt_backup_is_refused(self):
        corrupt = os.path.join(self.root, "corrupt.db")
        with open(corrupt, "wb") as f:
            f.write(b"this is not a sqlite database")
        before_mtime = os.path.getmtime(self.db_file)
        r = _run(["db", "--restore", corrupt, "--yes", "--json"], self.env)
        self.assertEqual(r.returncode, 1, r.stderr[-400:])
        payload = _one_json_line(self, r)
        self.assertEqual(payload["error_kind"], "runtime_error", payload)
        self.assertEqual(os.path.getmtime(self.db_file), before_mtime, "拒绝路径改动了当前库")

    def test_backup_without_accounts_table_is_refused(self):
        foreign = os.path.join(self.root, "foreign.db")
        self._sqlite(foreign, "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]);"
                              "c.execute('CREATE TABLE t(x)'); c.commit(); c.close()")
        r = _run(["db", "--restore", foreign, "--yes", "--json"], self.env)
        self.assertEqual(r.returncode, 1, r.stderr[-400:])
        self.assertIn("accounts", _one_json_line(self, r)["errors"][0])

    def test_restore_source_equal_target_is_refused(self):
        r = _run(["db", "--restore", self.db_file, "--yes", "--json"], self.env)
        self.assertEqual(r.returncode, 1, r.stderr[-400:])
        self.assertIn("同一个文件", _one_json_line(self, r)["errors"][0])


class PathHelpTest(unittest.TestCase):
    """路径变量必须可见：根与 `config`/`db` 子命令的 `--help` 列明四个 `YIBAN_*` 路径键。

    登记原文：唯一改道变量 `YIBAN_DB_FILE` 不在任何 help 里 ⇒ `.env` 写了账号仍报
    "未配置任何账号"却同时回显 `env_file:.env`。这里只钉"事实写进 help"，不改解析行为。
    """

    VARS = ("YIBAN_ENV_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-cli-mf60help-")
        self.addCleanup(self.tmp.cleanup)
        self.env = _cli_env(self.tmp.name)

    def test_path_vars_listed_in_help(self):
        for argv in (["--help"], ["config", "--help"], ["db", "--help"]):
            with self.subTest(argv=argv):
                r = _run(argv, self.env)
                self.assertEqual(r.returncode, 0, r.stderr[-300:])
                for var in self.VARS:
                    self.assertIn(var, r.stdout, f"{argv} 的 --help 未列明 {var}")
                self.assertIn("当前工作目录", r.stdout, f"{argv} 未说明相对路径按 cwd 解析")


class ExitKindFamilyTest(unittest.TestCase):
    """退出码分族：`--json` 错误对象带机读 `error_kind`，既有码含义不动。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-cli-mf60c-")
        self.addCleanup(self.tmp.cleanup)
        self.env = _cli_env(self.tmp.name)

    def test_illegal_args_carry_distinguishable_error_kind(self):
        """非法参数 + 缺子命令：`error_kind` 覆盖用法族，且每种的取值可区分。"""
        expected = {
            ("nope",): "usage",
            (): "usage_no_command",
            ("version", "--bogus"): "usage_extra_args",
            ("db", "--status", "--integrity"): "usage",
            ("state", "--yes", "--dry-run"): "usage_conflict",
            # `capacity` 已无在线实测开关，`--measure` 是不认得的选项（未知参数族），
            # 不再是当初的「与 --json 互斥」冲突族；退出码仍是 2。
            ("capacity", "--measure"): "usage_extra_args",
            ("sign", "--bogus"): "usage_engine",
        }
        for argv, kind in expected.items():
            with self.subTest(argv=argv):
                r = _run([*argv, "--json"], self.env)
                self.assertEqual(r.returncode, 2, r.stderr[-300:])
                payload = _one_json_line(self, r)
                self.assertEqual(payload["error_kind"], kind, payload)
        self.assertGreaterEqual(
            len(set(expected.values())), 5,
            "用法错误的 error_kind 取值太少，六种非法参数无法按族区分")

    def test_exit_code_family_unchanged(self):
        """`docs/dev/cli.md` §3 既有码的机读分族：0/1/2/3/4/10 只增不改。"""
        from yiban.engine import cli_support
        self.assertEqual(cli_support.exit_kind(0), "ok")
        self.assertEqual(cli_support.exit_kind(1), "failure")
        self.assertEqual(cli_support.exit_kind(2), "skipped")
        self.assertEqual(cli_support.exit_kind(3), "locked")
        self.assertEqual(cli_support.exit_kind(cli_support.EXIT_SCHEMA_MIGRATION),
                         "schema_migration")
        self.assertEqual(cli_support.exit_kind(10), "second_run_check")
        self.assertEqual(cli_support.exit_kind(99), "unknown")

    def test_config_error_kind_is_config_error(self):
        r = _run(["config", "--json"], self.env)   # 零账号 = 配置错误(1)
        self.assertEqual(r.returncode, 1, r.stderr[-300:])
        payload = _one_json_line(self, r)
        self.assertEqual(payload["error_kind"], "config_error")

    def test_sign_and_probe_json_expose_error_kind(self):
        """sign/probe 的机器可读输出是退出码；`--json` 下补同族的 error_kind。"""
        r = _run(["sign", "--json"], self.env)     # 零账号 = failure(1)
        self.assertEqual(r.returncode, 1, r.stderr[-300:])
        self.assertEqual(_one_json_line(self, r)["error_kind"], "failure")
        r = _run(["probe", "--json"], self.env)    # 零账号 = 无需执行(0)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertEqual(_one_json_line(self, r)["error_kind"], "ok")


class ProbeExitFamilyTest(unittest.TestCase):
    """probe 判码分族：跳过 / 撞锁 / 真跑失败三者可区分且非 0（登记原文验收不变量）。

    本类在**进程内**调 `runner.main(["--probe"])` 并打桩引擎外部依赖（网络/锁/挂钟），
    理由同 `tests/test_run_lock_fail_closed.py`：探针是真实登录，进程级真跑会打真网络；
    "判码映射"这一步与网络无关，打桩边界正好落在它外面。
    """

    def setUp(self):
        from types import SimpleNamespace
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-cli-mf60d-")
        self.addCleanup(self.tmp.cleanup)
        self._ns = SimpleNamespace
        patcher = mock.patch.dict(os.environ, {
            "YIBAN_STATE_DIR": self.tmp.name,
            "YIBAN_LOG_FILE": os.path.join(self.tmp.name, "sign.log"),
        }, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _acc(self):
        return self._ns(phone="13800138000", user_paused=False, owner="",
                        password="p", account_id=0)

    def _run_probe_main(self, probe_result, lock_exc=None):
        from yiban.engine import runner
        lock = (mock.patch.object(runner.cli_support, "_acquire_run_lock",
                                  side_effect=lock_exc) if lock_exc is not None
                else mock.patch.object(runner.cli_support, "_acquire_run_lock",
                                       return_value=object()))
        with mock.patch.object(runner.cli_support, "_setup_cli_logging"), \
                mock.patch.object(runner.accounts_mod, "load_accounts",
                                  return_value=[self._acc()]), \
                mock.patch.object(runner.schedule_mod, "day_off", return_value=None), \
                mock.patch.object(runner.probe, "run_probe",
                                  return_value=probe_result), \
                lock:
            return runner.main(["--probe"])

    def test_skip_and_failure_are_distinct_and_nonzero(self):
        from yiban.engine import cli_support
        skipped = self._run_probe_main(None)          # 未开启/未到点：没做检查
        healthy = self._run_probe_main(0)             # 真跑全绿
        failed = self._run_probe_main(2)              # 真跑有失败
        locked = self._run_probe_main(0, lock_exc=cli_support._RunLockHeld())
        self.assertEqual(skipped, 2, "跳过必须是 2（非 0）")
        self.assertEqual(failed, 1, "真跑有失败必须是 1")
        self.assertNotEqual(skipped, failed, "跳过与真跑失败必须可区分")
        self.assertNotEqual(skipped, 0)
        self.assertNotEqual(failed, 0)
        self.assertEqual(healthy, 0, "真跑全绿才是 0")
        self.assertEqual(locked, 3, "撞锁沿用既有 3（队列忙）")

    def test_zero_accounts_probe_is_noop_success(self):
        """零账号 = 无需执行（0）；探针分支先于零账号守卫，不落 ERROR。"""
        from yiban.engine import runner
        with mock.patch.object(runner.cli_support, "_setup_cli_logging"), \
                mock.patch.object(runner.accounts_mod, "load_accounts", return_value=[]), \
                mock.patch.object(runner.schedule_mod, "day_off", return_value=None), \
                mock.patch.object(runner.probe, "run_probe") as m_probe:
            rc = runner.main(["--probe"])
        self.assertEqual(rc, 0)
        m_probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
