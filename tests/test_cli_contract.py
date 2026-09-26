# -*- coding: utf-8 -*-
"""`yiban.cli` 的契约用例（`docs/dev/cli.md` §2/§3 的可执行版本）。

标签：J · 运维：部署/备份/发布
覆盖：子命令与 `--help`、`--json` 单行可解析、致命错误的 stderr+JSON 双通道、
    `state` 默认不动手（dry-run）、`db --status` 只读且带 `user_version`、
    `db backup/integrity` 需 `--yes` 且拒绝目标等于源、`version` 与 `yiban.__version__`
    同源、`capacity` 建议值与网页同公式、未知/空子命令退 2 且 stdout 为空、不读 stdin。
对应实现：`yiban/cli.py`（转发到 `yiban/store/migrations.py`、`yiban/engine` 等）。
关键断言：退出码、stdout/stderr 分流、"默认不动手"三条都是进程级契约——断的是真实
    子进程的行为，不是 `main()` 的返回值。
依赖：起 `sys.executable -m yiban.cli` 子进程（纯 Python，不需 bash/docker/网络）；
    全部用临时 STATE/LOG/DB/ENV，不碰本机真实 .env；`capacity --measure` 一条在
    `CAPACITY_PROBE` 文件不存在时 skipTest。

口径（为什么全部起子进程）：退出码、stdout/stderr 分流、stdin 行为在测试进程里调
`main()` 会被测成另一回事（尤其 stdin 与 stdout 编码）。
**唯一例外**：`capacity --measure` 的转发用例改用打桩 `subprocess.run`，理由见该用例。
"""
import json
import os
import sqlite3
import subprocess
import sys
import unittest

from yiban.store import migrations

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 期望的 schema 顶（迁移登记表的最后一项）：新增迁移时本文件不必再改
SCHEMA_TOP = migrations._MIGRATIONS[-1][0]

#: 七个子命令（`docs/dev/cli.md` §1 的目标形态）
SUBCOMMANDS = ("sign", "probe", "config", "capacity", "state", "db", "version")

#: `--json` 的期望退出码：临时环境里 0 账号（config 判配置错误=1）、sign 同理=1
JSON_EXPECTED_RC = {
    "sign": 1, "probe": 0, "config": 1, "capacity": 0, "state": 0, "db": 0, "version": 0,
}

EXPIRED_STATE_FILE = "sched-run-2020-01-01.json"


def _cli_env(tmp_path, env_extra=None):
    """隔离环境：临时路径四件套 + 去掉进程里继承的全部 YIBAN_*（防串到真实部署）。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}  #整批剥掉 YIBAN_*：一继承本机 .env 的键，用例就是在真实部署的口径上跑
    env.update({
        "YIBAN_STATE_DIR": str(tmp_path / "state"),
        "YIBAN_LOG_FILE": str(tmp_path / "logs" / "sign.log"),
        "YIBAN_DB_FILE": str(tmp_path / "yiban.db"),
        "YIBAN_ENV_FILE": str(tmp_path / ".env"),
        "YIBAN_ACCOUNTS_KEY": "a" * 64,   # 免得某条路径顺手生成密钥写盘
        "YIBAN_MAIL_ENABLE": "0",
        "PYTHONPATH": BASE,               # `python -m yiban.cli` 需要仓库根在导入路径上
        "PYTHONIOENCODING": "utf-8",      # JSON 里的中文在任意平台都可解码
    })
    env.update(env_extra or {})
    return env


def _run(argv, env, stdin=subprocess.DEVNULL, timeout=120):
    """跑一次 CLI（cwd=仓库根）；返回 CompletedProcess。"""
    return subprocess.run([sys.executable, "-m", "yiban.cli", *argv], cwd=BASE, env=env,  #-m 而非脚本路径：直跑文件会绕过包导入引导，与真实部署跑法不同
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=stdin, timeout=timeout)


def _make_db(tmp_path, env):
    """造一个真实 schema 的临时库（走数据层的初始化，绝不碰别的库）。"""
    code = ("import sys; from yiban.store import db;"
            "db.init_db(db_file=sys.argv[1], env_file=sys.argv[2], cleanup=False);"
            "db.get_conn().close()")
    r = subprocess.run([sys.executable, "-c", code, str(tmp_path / "yiban.db"),
                        str(tmp_path / ".env")], cwd=BASE, env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0:
        raise AssertionError(f"临时库初始化失败: {r.stdout}{r.stderr}")


class CliContractTest(unittest.TestCase):
    def setUp(self):
        import pathlib
        import tempfile

        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-cli-contract-")
        self.root = pathlib.Path(self.tmp.name)
        self.state_dir = str(self.root / "state")
        os.makedirs(self.state_dir)
        self.env = _cli_env(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _user_version(self):
        conn = sqlite3.connect(str(self.root / "yiban.db"))
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])  #直接读 PRAGMA，不信 CLI 自报的迁移版本：两者不一致才算断到东西
        finally:
            conn.close()

    def _add_account(self):
        """往临时库写一个 active 账号（走数据层，密码按密钥加密）。"""
        code = ("import sys; from yiban.store import db;"
                "db.init_db(db_file=sys.argv[1], env_file=sys.argv[2], cleanup=False);"
                "db.add_account({'name': 'A', 'phone': '13800138000', 'password': 'p1',"
                " 'status': 'active', 'owner': 'admin'})")
        r = subprocess.run([sys.executable, "-c", code, str(self.root / "yiban.db"),
                            str(self.root / ".env")], cwd=BASE, env=self.env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
        if r.returncode != 0:
            raise AssertionError(f"临时账号写入失败: {r.stdout}{r.stderr}")

    # ---- ① 子命令存在且 --help 可用 ----

    def test_every_subcommand_has_help(self):
        root = _run(["--help"], self.env)
        self.assertEqual(root.returncode, 0, root.stderr[-400:])
        for cmd in SUBCOMMANDS:
            with self.subTest(command=cmd):
                r = _run([cmd, "--help"], self.env)
                self.assertEqual(r.returncode, 0, f"{cmd} --help 退出码应为 0：{r.stderr[-300:]}")
                self.assertIn(cmd, r.stdout)
        # 根用法里要列全七个子命令（agent 靠它发现能力）
        for cmd in SUBCOMMANDS:
            with self.subTest(usage=cmd):
                self.assertIn(cmd, root.stdout)

    # ---- ② --json 是单行可 json.loads 的对象，stdout 无多余行 ----

    def test_json_output_is_single_line_object(self):
        for cmd, want_rc in JSON_EXPECTED_RC.items():
            with self.subTest(command=cmd):
                r = _run([cmd, "--json"], self.env)
                self.assertEqual(r.returncode, want_rc, r.stderr[-400:])
                lines = r.stdout.splitlines()
                self.assertEqual(len(lines), 1, f"stdout 必须只有一行 JSON：{lines!r}")
                payload = json.loads(lines[0])
                self.assertIsInstance(payload, dict)
                self.assertEqual(payload["command"], cmd)

    # ---- ②b 致命配置错误：stderr 一行摘要 + --json 的 error 详情（F2） ----

    def test_sign_no_accounts_error_reaches_stderr_and_json(self):
        """F2：`sign` 零账号（未配置任何账号）退出码 1 时，错误必须打到 stderr。

        2026-09-21 测试机 47 无上下文 CLI E2E：该失败模式下 stdout/stderr 全空，
        错误只进按天日志文件；`--json` 也只有 exit_code 没有详情。
        """
        # 人类模式：stderr 一行摘要；stdout 保持"只有结果"（空）
        r = _run(["sign"], self.env)
        self.assertEqual(r.returncode, 1, r.stderr[-400:])
        self.assertEqual(r.stdout, "", "stdout 只放结果，不得混入人类可读输出")
        self.assertIn("未配置任何账号", r.stderr, "致命错误摘要必须打到 stderr")

        # --json 模式：stdout 单行 JSON 带 error 详情（不能只有光秃秃的 exit_code）
        r = _run(["sign", "--json"], self.env)
        self.assertEqual(r.returncode, 1, r.stderr[-400:])
        payload = json.loads(r.stdout.splitlines()[0])
        self.assertEqual(payload["exit_code"], 1)
        self.assertIn("error", payload, "--json 必须带失败详情")
        self.assertIn("未配置任何账号", payload["error"])
        self.assertIn("未配置任何账号", r.stderr, "--json 模式下 stderr 摘要同样应可见")

    def test_sign_config_load_failure_error_reaches_stderr_and_json(self):
        """F2 同族：配置加载失败（坏 JSON）同样是 stderr 摘要 + --json error。"""
        env = dict(self.env, YIBAN_ACCOUNTS_JSON="{not-json")
        r = _run(["sign", "--json"], env)
        self.assertEqual(r.returncode, 1, r.stderr[-400:])
        payload = json.loads(r.stdout.splitlines()[0])
        self.assertIn("配置加载失败", payload.get("error", ""))
        self.assertIn("配置加载失败", r.stderr)

    def test_fatal_error_not_leaked_between_in_process_runs(self):
        """F2 附属：进程内多次调用 main 时，上一轮的致命原因不得附到本轮失败上。

        真实 CLI 一轮一进程，这条兜底的是测试/进程内复用：第二轮
        `--second-run-check` 只读判定（退出码 10，无致命错误）之后，
        `last_fatal_error()` 必须已被入口清空。
        """
        import unittest.mock as mock

        from yiban.engine import cli_support, runner
        with mock.patch.object(runner.accounts_mod, "load_accounts",
                               side_effect=RuntimeError("boom")), \
                mock.patch.object(runner.cli_support, "_setup_cli_logging"):
            rc = runner.main([])
        self.assertEqual(rc, 1)
        self.assertIn("配置加载失败", cli_support.last_fatal_error())

        with mock.patch.object(runner.state_io, "need_second_run", return_value=True), \
                mock.patch.object(runner.cli_support, "_setup_cli_logging"):
            rc = runner.main(["--second-run-check"])
        self.assertEqual(rc, 10)
        self.assertIsNone(cli_support.last_fatal_error(),
                          "上一轮的致命原因被带到了本轮（应已在 main 入口清空）")

    # ---- ③ state 默认 dry-run，--yes 需回显目标指纹才动手 ----

    def test_state_defaults_to_dry_run_and_yes_deletes_with_fingerprint(self):
        expired = os.path.join(self.state_dir, EXPIRED_STATE_FILE)
        with open(expired, "w", encoding="utf-8") as f:
            f.write("{}")
        _make_db(self.root, self.env)          # 留痕要写进部署库的审计链
        r = _run(["state", "--json"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        payload = json.loads(r.stdout)
        self.assertTrue(payload["dry_run"], "默认必须是 dry-run")
        self.assertEqual(payload["removed"], 0)
        self.assertGreaterEqual(payload["candidates"], 1)
        self.assertIn(EXPIRED_STATE_FILE, " ".join(payload["detail"]))
        self.assertTrue(os.path.exists(expired), "dry-run 不得删文件")
        fingerprint = payload["fingerprint"]
        self.assertTrue(fingerprint, "dry-run 必须打印目标指纹供 --yes 回显")

        # 缺指纹（或指纹不符）的 --yes 一律拒绝且零删除
        r = _run(["state", "--yes", "--json"], self.env)
        self.assertNotEqual(r.returncode, 0, "state --yes 缺目标指纹必须拒绝")
        self.assertTrue(os.path.exists(expired), "拒绝路径不得删文件")

        r = _run(["state", "--yes", "--fingerprint", fingerprint, "--json"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        payload = json.loads(r.stdout)
        self.assertFalse(payload["dry_run"])
        self.assertGreaterEqual(payload["removed"], 1)
        self.assertFalse(os.path.exists(expired), "--yes 回显指纹后应当真删")

    def test_state_reports_missing_dir_loudly(self):
        env = _cli_env(self.root, {"YIBAN_STATE_DIR": str(self.root / "nope")})
        r = _run(["state", "--json"], env)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(json.loads(r.stdout)["ok"])
        self.assertIn("状态目录不存在", r.stderr)

    # ---- ④ db --status 在临时库上返回 0 且带 user_version ----

    def test_db_status_on_temp_db(self):
        _make_db(self.root, self.env)
        r = _run(["db", "--status", "--json"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        payload = json.loads(r.stdout)
        self.assertEqual(payload["user_version"], SCHEMA_TOP, "临时库与生产同 schema")
        self.assertIn("accounts", payload["tables"])
        self.assertGreater(payload["size_bytes"], 0)
        # 非 --json 时 stdout 必须干净（人话走 stderr）
        r = _run(["db", "--status"], self.env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "", "人类可读输出不得进 stdout")
        self.assertIn(f"user_version={SCHEMA_TOP}", r.stderr)

    def test_db_integrity_and_backup_need_yes(self):
        _make_db(self.root, self.env)
        r = _run(["db", "--integrity", "--json"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertTrue(json.loads(r.stdout)["integrity_ok"])
        backup = str(self.root / "copy.db")
        r = _run(["db", "--backup", backup, "--json"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertTrue(json.loads(r.stdout)["dry_run"])
        self.assertFalse(os.path.exists(backup), "不加 --yes 不得写盘")
        r = _run(["db", "--backup", backup, "--yes", "--json"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertTrue(os.path.exists(backup), "加 --yes 应写出副本")
        self.assertEqual(json.loads(r.stdout)["user_version"], SCHEMA_TOP)

    def test_db_backup_rejects_target_equals_source(self):
        """Low-2：`db --backup <源库>` 目标==源库必须拒绝。

        原实现不设防：WAL 库"报成功但副本就是活库本身"（误导）；非 WAL 库
        `src.backup(dst)` **无限阻塞**（运维命令挂死）。修复后按 realpath 归一
        判定同源即报错，--yes 与 dry-run 都拦。
        """
        _make_db(self.root, self.env)
        src = str(self.root / "yiban.db")
        # 先量源库完整性基线
        r0 = _run(["db", "--status", "--json"], self.env)
        self.assertEqual(r0.returncode, 0)
        # 目标==源（显式传源路径）：必须失败
        r = _run(["db", "--backup", src, "--yes", "--json"], self.env)
        self.assertNotEqual(r.returncode, 0, "目标==源库必须拒绝")
        self.assertFalse(json.loads(r.stdout)["ok"])
        self.assertIn("目标", r.stderr + r.stdout)
        # dry-run 分支同样拦（不能只报告"计划"放行）
        r = _run(["db", "--backup", src, "--json"], self.env)
        self.assertNotEqual(r.returncode, 0, "dry-run 也应拦目标==源")

    # ---- ⑤ version 与 yiban.__version__ 同源 ----

    def test_version_matches_package(self):
        from yiban import __version__

        r = _run(["version", "--json"], self.env)
        self.assertEqual(r.returncode, 0)
        payload = json.loads(r.stdout)
        self.assertEqual(payload["version"], __version__)
        self.assertIn("python", payload)
        self.assertIn("user_version", payload)
        # 库不存在时 user_version 报 null 但**照常返回 0**（查版本不该因没建库而失败）
        self.assertIsNone(payload["user_version"])
        _make_db(self.root, self.env)
        self.assertEqual(json.loads(_run(["version", "--json"], self.env).stdout)["user_version"],
                         SCHEMA_TOP)

    # ---- capacity：建议值口径与网页 /api/scheduler/executors 同源 ----

    def test_capacity_recommendation_matches_web_formula(self):
        with open(str(self.root / ".env"), "w", encoding="utf-8") as f:
            f.write("YIBAN_CAPACITY_MEASURED=90\nYIBAN_ACCOUNT_GAP_MAX=10\n")
        r = _run(["capacity", "--json"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        payload = json.loads(r.stdout)
        self.assertEqual(payload["measured_per_executor"], 90)
        # 实测 × 2/3（与网页建议值、容量基准工具的 --ratio 同一余量口径）
        self.assertEqual(payload["recommended_per_executor"], 60)
        self.assertEqual(payload["executors_needed"], 0, "0 个账号 → 0 个执行体")
        self.assertGreater(payload["capacity_per_executor"], 0, "有效窗口容量取自共享引擎函数")

    # ---- ⑥ 未知/空子命令：stderr 用法 + 退出码 2 + stdout 为空 ----

    def test_unknown_and_empty_subcommand(self):
        for argv in ([], ["nope"], ["sign-extra"]):
            with self.subTest(argv=argv):
                r = _run(argv, self.env)
                self.assertEqual(r.returncode, 2)
                self.assertEqual(r.stdout, "", "用法/错误信息不得进 stdout")
                self.assertTrue(r.stderr.strip())
        # 用法错误（多余参数）同样 2
        r = _run(["version", "--bogus"], self.env)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        # 互斥开关也是用法错误（默认就是 dry-run）
        r = _run(["state", "--yes", "--dry-run"], self.env)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")

    def test_config_reports_missing_field_as_config_error_not_traceback(self):
        """账号缺必填字段（phone/password 为空）抛的是 ValueError：

        配置入口必须把它落成"配置加载失败 + 退出码 1 + 一行可解析 JSON"，
        不得变成裸 traceback（此前只捕 RuntimeError）。"""
        env = dict(self.env, YIBAN_ACCOUNTS_JSON='[{"phone": "13800000001"}]')
        r = _run(["config", "--json"], env)
        self.assertEqual(r.returncode, 1, r.stderr[-400:])
        self.assertNotIn("Traceback", r.stderr)
        payload = json.loads(r.stdout.splitlines()[0])
        self.assertFalse(payload["ok"])
        self.assertTrue(any("配置加载失败" in m for m in payload["errors"]), payload)

    def test_config_and_check_config_do_not_migrate_target_db(self):
        """F3：宣称"脱敏、不联网/只读"的配置检查不得对目标库跑迁移（写库）。

        2026-09-21 测试机 47 E2E：`config` 经 `load_accounts() → db.init_db(migrate=True)`
        把目标库迁到了当时的 schema 顶（v17，现为 v19）。本用例用 user_version=13 的旧库（E2E 前 n360.db 的形态）
        钉住"不迁移"：跑完 `config` 与 `sign --check-config`，user_version 必须原样不动。
        对照组（直接 `init_db`，缺省 migrate=True）证明该库确实可被迁移——否则用例
        什么也没测到。
        """
        _make_db(self.root, self.env)
        self._add_account()
        # 伪装成旧库（E2E 前的 n360.db：user_version 13）
        conn = sqlite3.connect(str(self.root / "yiban.db"))
        try:
            conn.execute("PRAGMA user_version=13")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._user_version(), 13, "前置条件：旧库")

        r = _run(["config", "--json"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        payload = json.loads(r.stdout.splitlines()[0])
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["accounts"], 1, "只读模式下账号仍应正常列出")
        self.assertEqual(self._user_version(), 13, "config 不得对目标库跑迁移")

        r = _run(["sign", "--check-config"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertEqual(self._user_version(), 13, "sign --check-config 不得跑迁移")

        # 对照组：真实路径（init_db 缺省 migrate=True）会把这个库迁到当前版本
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; from yiban.store import db;"
             "db.init_db(db_file=sys.argv[1], env_file=sys.argv[2], cleanup=False)",
             str(self.root / "yiban.db"), str(self.root / ".env")],
            cwd=BASE, env=self.env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertEqual(self._user_version(), SCHEMA_TOP,
                         "对照组失败：该库本可被迁移，上面的断言没测到东西")

    def test_capacity_measure_forwards_extra_args(self):
        """`--measure` 之后的多余参数属于工具自己的开关，不是 CLI 的用法错误。

        ⚠ 本用例**不起真进程**（与文件头"全部起子进程"的口径为例外，理由充分）：
        转发目标是真的容量基准工具，在 Linux 上它会真做完整基准（分钟级），
        那就是"单元测试里跑压测"。这里只验证**转发本身**——argv 拼对了、
        参数没被 CLI 拦下——用打桩 `subprocess.run` 即可。
        """
        import unittest.mock as mock

        import yiban.cli as cli  #本条是文件内少数进程内用例：只验 argv 拼装，不起真压测
        if not os.path.isfile(cli.CAPACITY_PROBE):
            self.skipTest("容量基准工具不在仓库里")
        with mock.patch.object(cli.subprocess, "run") as m_run:
            m_run.return_value = mock.Mock(returncode=0)
            rc = cli.main(["capacity", "--measure", "--repo", "."])
        self.assertEqual(rc, 0, "转发未发生（退出码不是子进程的）")
        cmd = m_run.call_args.args[0]
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[1], cli.CAPACITY_PROBE)
        self.assertEqual(cmd[2:], ["--repo", "."], "工具自己的开关必须原样透传")

    # ---- ⑦ 不读 stdin：stdin 关掉/空管道都能跑完 ----

    def test_never_reads_stdin(self):
        for cmd in ("version", "config", "db", "state"):
            with self.subTest(command=cmd):
                # 关掉的 stdin（写端立刻关闭）：若真去读会 EOF 后继续，若等待输入则卡住
                proc = subprocess.Popen(
                    [sys.executable, "-m", "yiban.cli", cmd], cwd=BASE, env=self.env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8")
                proc.stdin.close()
                try:
                    proc.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    self.fail(f"{cmd} 在 stdin 关闭时阻塞——命令不得读取 stdin")
        # state 的破坏性开关也不做 y/N 确认：不加 --yes 就只报告（绝不问）
        r = _run(["state"], self.env, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
