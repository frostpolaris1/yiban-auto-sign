# -*- coding: utf-8 -*-
"""出口令牌桶复位的受支持入口：`python -m yiban.cli egress`（工单 `yiban-auto-sign-zggs`）。

标签：J · 运维：部署/备份/发布
覆盖：`egress --status` 只读列出 `egress_state` 各行；`egress --reset <出口>` 与
   `--reset-all` 默认只报告、加 `--yes` 才写；复位后速率逐行可核对；未知出口、越界速率、
   互斥开关、多余参数各自落到既有的退出码家族；`--json` 的每条退出路径都是一整行对象；
   库不存在时只读面不失败。另钉"哪些行可能被活进程覆盖"的事实在输出里在场。
对应实现：yiban/engine/egress_admin.py（实现）、yiban/cli.py（子命令注册与分派）、
   yiban/engine/token_bucket.py（速率域名与出厂速率唯一真值源）、
   yiban/store/queue_store.py（`egress_state` 唯一持久化路径）、yiban/clock.py（时间域）。
关键断言：复位把被误判砍过的出口速率写回**引擎出厂速率**（或 `--rate` 显式值），且只改
   `rate` 一列（`burst`/`tat` 逐字保留）；不加 `--yes` 时逐字不动。用途是给运维一条
   "受支持的撤销"——否则误报降档只能直接改库，或等约 8 个干净轮让 AIMD 爬回。
依赖：起 `sys.executable -m yiban.cli` 子进程 + 临时库（真 schema）；不联网。
"""
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from yiban import config_loader

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 出口键与生产同形：`<角色槽位>@<主机名>`（`egress.worker_owner` / `fallback_owner`）。
STALE_EGRESS = "fallback@test-host"
FRESH_EGRESS = "worker-2@test-host"
#: 命令行只认角色+槽位标签（出口键含主机名，属部署信息，不回原串）。
STALE_SELECTOR = "fallback"
FRESH_SELECTOR = "worker-2"
#: 陈旧落库时刻：远早于"活进程持有"的判定窗，用来钉 `held_by_live_process` 的假侧。
STALE_STAMP = "2026-10-09 07:43:35"
#: 引擎出厂速率：从配置名册读，不抄字面量（名册改了这条用例不会假装还绿）。
ENGINE_DEFAULT_RATE = float(config_loader.default_of("YIBAN_EGRESS_RATE"))


def _cli_env(root, extra=None):
    """隔离环境：临时路径四件套 + 剥掉进程里继承的全部 YIBAN_*（防串真实部署）。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
    env.update({
        "YIBAN_STATE_DIR": os.path.join(root, "state"),
        "YIBAN_LOG_FILE": os.path.join(root, "logs", "sign.log"),
        "YIBAN_DB_FILE": os.path.join(root, "yiban.db"),
        "YIBAN_ENV_FILE": os.path.join(root, ".env"),
        "YIBAN_ACCOUNTS_KEY": "a" * 64,
        "PYTHONPATH": BASE,             # `python -m yiban.cli` 需要仓库根在导入路径上
        "PYTHONIOENCODING": "utf-8",    # JSON 里的中文在任意平台都可解码
    })
    env.update(extra or {})
    return env


def _run(argv, env, timeout=120):
    return subprocess.run([sys.executable, "-m", "yiban.cli", *argv], cwd=BASE,
                          env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL, timeout=timeout)


class _EgressCliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-cli-egress-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        os.makedirs(self.root / "state", exist_ok=True)
        self.env = _cli_env(str(self.root))

    # ---- 夹具 ----

    def _seed(self, rows=()):
        """建真 schema 的临时库并写入若干 `egress_state` 行（走数据层，绝不碰别的库）。"""
        code = (
            "import sys;from yiban.store import db, queue_store;"
            "db.init_db(db_file=sys.argv[1], env_file=sys.argv[2], cleanup=False);"
            "i=3\n"
            "while i < len(sys.argv):\n"
            "    queue_store.save_egress_state(sys.argv[i], float(sys.argv[i+1]),"
            " float(sys.argv[i+2]), float(sys.argv[i+3]), sys.argv[i+4])\n"
            "    i += 5\n"
            "db.get_conn().close()")
        argv = [sys.executable, "-c", code, str(self.root / "yiban.db"),
                str(self.root / ".env")]
        for egress, rate, burst, tat, stamp in rows:
            argv += [egress, str(rate), str(burst), str(tat), stamp]
        r = subprocess.run(argv, cwd=BASE, env=self.env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(r.returncode, 0, f"临时库/行写入失败: {r.stdout}{r.stderr}")

    def _row(self, egress):
        """直读库里的那一行（不信 CLI 自报的值：两者不一致才算断到东西）。"""
        conn = sqlite3.connect(str(self.root / "yiban.db"))
        try:
            row = conn.execute(
                "SELECT rate, burst, tat, updated_at FROM egress_state WHERE egress=?",
                (egress,)).fetchone()
        finally:
            conn.close()
        return row

    def _row_count(self):
        conn = sqlite3.connect(str(self.root / "yiban.db"))
        try:
            return int(conn.execute("SELECT COUNT(*) FROM egress_state").fetchone()[0])
        finally:
            conn.close()

    def _one_json(self, r):
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 1, f"stdout 必须只有一行 JSON：{lines!r}")
        return json.loads(lines[0])

    def _fresh_stamp(self):
        """当前北京时间戳串——与引擎落库同一时间域（`yiban.clock`）。"""
        code = "from yiban import clock;print(clock.ts())"
        r = subprocess.run([sys.executable, "-c", code], cwd=BASE, env=self.env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        return r.stdout.strip()

    # ---- 用例 ----

    def test_status_lists_rows_and_writes_nothing(self):
        """只读面：列出各行速率、给出出厂速率，且不写库；输出不回含主机名的原键。"""
        self._seed([
            (STALE_EGRESS, 0.25, 6.0, 3735447.86549844, STALE_STAMP),
            (FRESH_EGRESS, 1.0, 6.0, 0.0, self._fresh_stamp()),
        ])
        before = self._row(STALE_EGRESS)
        r = _run(["egress", "--status", "--json"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        payload = self._one_json(r)
        self.assertEqual(payload["command"], "egress")
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dry_run"], "只读面必须自报 dry_run")
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["target_rate"], ENGINE_DEFAULT_RATE,
                         "目标速率默认 = 引擎出厂速率（唯一真值源）")
        rows = {row["selector"]: row for row in payload["rows"]}
        self.assertEqual(set(rows), {STALE_SELECTOR, FRESH_SELECTOR})
        self.assertAlmostEqual(rows[STALE_SELECTOR]["rate"], 0.25, places=9)
        # 人类可读汇总要走 stderr，且带上可核对的速率
        self.assertIn("0.25", r.stderr)
        # "会不会被活进程覆盖"的事实必须在输出里：陈旧行=无人持有，刚写过的行=有人持有
        self.assertFalse(rows[STALE_SELECTOR]["held_by_live_process"],
                         "4 小时没落库的行不可能有活进程持有")
        self.assertTrue(rows[FRESH_SELECTOR]["held_by_live_process"],
                        "刚落库的行极可能有活执行体在持有（复位会被 10s 落库覆盖）")
        # 出口键含主机名，属部署信息：任何输出都不得回原串
        self.assertNotIn("test-host", r.stdout + r.stderr,
                         "含主机名的出口原键不得出现在任何输出里")
        self.assertEqual(self._row(STALE_EGRESS), before, "只读面一字不写")

    def test_reset_without_yes_reports_only(self):
        """默认 dry-run：报告将要写什么，库一字不动。"""
        self._seed([(STALE_EGRESS, 0.25, 6.0, 3735447.86549844, STALE_STAMP)])
        before = self._row(STALE_EGRESS)
        r = _run(["egress", "--reset", STALE_SELECTOR, "--json"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        payload = self._one_json(r)
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["applied"])
        changes = payload["changes"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["tag"], "[%s]" % STALE_SELECTOR,
                         "变更行按角色+槽位标签报出，不回含主机名的原键")
        self.assertAlmostEqual(changes[0]["rate_before"], 0.25, places=9)
        self.assertAlmostEqual(changes[0]["rate_after"], ENGINE_DEFAULT_RATE, places=9)
        self.assertNotIn("test-host", r.stdout + r.stderr,
                         "含主机名的出口原键不得出现在任何输出里")
        self.assertEqual(self._row(STALE_EGRESS), before, "不加 --yes 必须一字不写")

    def test_reset_with_yes_restores_engine_default_rate(self):
        """受支持的撤销：`--yes` 把速率写回出厂值，且只改 rate 一列。"""
        self._seed([(STALE_EGRESS, 0.25, 6.0, 3735447.86549844, STALE_STAMP)])
        r = _run(["egress", "--reset", STALE_SELECTOR, "--yes", "--json"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        payload = self._one_json(r)
        self.assertFalse(payload["dry_run"])
        self.assertTrue(payload["applied"])
        # 可核对：JSON 里的 rate_after 必须等于库里真值
        self.assertAlmostEqual(payload["changes"][0]["rate_after"],
                               ENGINE_DEFAULT_RATE, places=9)
        rate, burst, tat, _stamp = self._row(STALE_EGRESS)
        self.assertAlmostEqual(rate, ENGINE_DEFAULT_RATE, places=9)
        self.assertAlmostEqual(burst, 6.0, places=9, msg="burst 不得被复位顺手改掉")
        self.assertAlmostEqual(tat, 3735447.86549844, places=9,
                               msg="tat 不得被复位顺手改掉（装载时会按新速率夹住）")
        self.assertIn("1.000", r.stderr, "复位后的速率要打在人类可读出口上")
        self.assertNotIn("test-host", r.stdout + r.stderr)

    def test_reset_all_with_explicit_rate(self):
        """`--reset-all` + `--rate`：全体复位到显式速率。"""
        self._seed([
            (STALE_EGRESS, 0.25, 6.0, 0.0, STALE_STAMP),
            (FRESH_EGRESS, 0.5, 6.0, 0.0, STALE_STAMP),
        ])
        r = _run(["egress", "--reset-all", "--rate", "2.5", "--yes", "--json"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        payload = self._one_json(r)
        self.assertEqual(payload["target_rate"], 2.5)
        self.assertEqual(sorted(c["tag"] for c in payload["changes"]),
                         ["[%s]" % STALE_SELECTOR, "[%s]" % FRESH_SELECTOR])
        for egress in (STALE_EGRESS, FRESH_EGRESS):
            self.assertAlmostEqual(self._row(egress)[0], 2.5, places=9)

    def test_unknown_egress_fails_and_creates_no_row(self):
        """未知出口是失败（不静默建行、不动别的行）。"""
        self._seed([(STALE_EGRESS, 0.25, 6.0, 0.0, STALE_STAMP)])
        r = _run(["egress", "--reset", "nope", "--yes", "--json"], self.env)
        self.assertEqual(r.returncode, 1, r.stderr[-400:])
        payload = self._one_json(r)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_kind"], "runtime_error")
        self.assertEqual(self._row_count(), 1, "不得为不存在的出口新建行")
        self.assertAlmostEqual(self._row(STALE_EGRESS)[0], 0.25, places=9)

    def test_rate_out_of_range_is_rejected(self):
        """越界/非数值的 `--rate` 响亮拒绝：不夹、不猜、不写。"""
        self._seed([(STALE_EGRESS, 0.25, 6.0, 0.0, STALE_STAMP)])
        for bad in ("abc", "0", "99"):
            with self.subTest(rate=bad):
                r = _run(["egress", "--reset", STALE_SELECTOR, "--rate", bad,
                          "--yes", "--json"], self.env)
                self.assertEqual(r.returncode, 1, r.stderr[-400:])
                payload = self._one_json(r)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error_kind"], "config_error")
                self.assertAlmostEqual(self._row(STALE_EGRESS)[0], 0.25, places=9)

    def test_status_on_missing_db_is_not_a_failure(self):
        """库不存在 = 还没有任何桶状态；只读面报空表并返回 0（不因未配置失败）。"""
        r = _run(["egress", "--status", "--json"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        payload = self._one_json(r)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["rows"], [])

    def test_reset_on_missing_db_creates_no_file(self):
        """库不存在时复位是失败，且**不得**建出一个空库（维护命令不建库）。"""
        db_path = self.root / "yiban.db"
        r = _run(["egress", "--reset", STALE_SELECTOR, "--yes", "--json"], self.env)
        self.assertEqual(r.returncode, 1, r.stderr[-400:])
        self.assertEqual(self._one_json(r)["error_kind"], "runtime_error")
        self.assertFalse(db_path.exists(),
                         "维护命令不得顺手建库（同 db_maintenance 的红线）")

    def test_illegal_argument_shapes_land_in_family_codes(self):
        """互斥开关与多余参数落既有用法族：rc=2，`error_kind` 可区分。"""
        for argv, kind in (
            (["egress", "--status", "--reset", STALE_SELECTOR], "usage"),
            (["egress", "--status", "--reset-all"], "usage"),
            (["egress", "bogus"], "usage_extra_args"),
            (["egress", "--status", "--rate", "2"], "usage_conflict"),
        ):
            with self.subTest(argv=argv):
                r = _run([*argv, "--json"], self.env)
                self.assertEqual(r.returncode, 2, r.stderr[-400:])
                self.assertEqual(self._one_json(r)["error_kind"], kind)


if __name__ == "__main__":
    unittest.main(verbosity=2)
