# -*- coding: utf-8 -*-
"""MF-60 契约用例（一）：`--json` 在任何退出路径上都必须留下可解析的一整行结构化对象。

标签：J · 运维：部署/备份/发布
覆盖：非法参数/未知子命令/缺子命令/多余参数/互斥开关（一律 rc=2）、`--check-config`
    摘要不得混入 stdout、每个成功 `--json` 都带 `exit_code`。
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


if __name__ == "__main__":
    unittest.main()
