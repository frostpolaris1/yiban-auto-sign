# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""清理量随体检/日报/取证 CLI 出箱。

本机自校验防不住**本机时钟**：时钟跳变守卫的参照点每次成功都会推进，容差内每天小幅
拨快即可在真实时间数十天内合法清掉整段保留期审计，且不触发任何告警。因此"累计有留痕
的审计清理条数"与"最近一次清理的截止点"必须离开本机——日报邮件是这条通道之一，
取证 CLI 是另一条。本文件把这三处的输出形状钉住。
"""

import contextlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
AUDIT_KEY = "b" * 64


def _load_webapp(tag):
    spec = importlib.util.spec_from_file_location(
        f"webapp_{tag}", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"webapp_{tag}"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class _CleanupFixture(unittest.TestCase):
    """临时库 + 临时锚点文件；`_run_cleanup` 走真实清理路径（删除与留痕同事务）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-cleanup-vis-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls._old_env = {}
        for k, v in (("YIBAN_ACCOUNTS_KEY", TEST_KEY), ("YIBAN_AUDIT_KEY", AUDIT_KEY),
                     ("YIBAN_ENV_FILE", cls.env_file), ("YIBAN_DB_FILE", cls.db_file),
                     ("YIBAN_ACCOUNTS_FILE", os.path.join(cls.tmp, "accounts.json")),
                     ("YIBAN_STATE_DIR", cls.tmp)):
            cls._old_env[k] = os.environ.get(k)
            os.environ[k] = v
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(self.db_file + suffix)
        self.anchor = db.audit_anchor_path()
        with contextlib.suppress(OSError):
            os.remove(self.anchor)
        db.init_db(cleanup=False)

    def _seed_expired_and_cleanup(self, old_rows=3, fresh_rows=2):
        """造 old_rows 条超期审计 + fresh_rows 条当日审计，再跑一次真实保留期清理。"""
        conn = db.get_conn()
        for i in range(old_rows):
            conn.execute(
                "INSERT INTO audit_logs (ts, username, action, target, detail, prev_hash, hash) "
                "VALUES ('2020-01-01 00:00:00','tester','old',?,?,'','')", (f"t{i}", f"d{i}"))
            conn.commit()
        db._rechain_audit_logs(conn)
        for i in range(fresh_rows):
            db.audit("tester", "seed", f"f{i}", "d")
        conn = db.get_conn()
        db._audit_cleanup(conn)


class HealthAndAlertTest(_CleanupFixture):
    """audit_health 暴露清理量；日报事实清单把它带出本机。"""

    def test_health_exposes_purge_total_and_last_cleanup(self):
        self._seed_expired_and_cleanup(old_rows=3)
        h = db.audit_health(path=self.anchor)
        self.assertEqual(h["purge_total"], 3, "累计留痕的删除条数应等于真实删除量")
        self.assertIsNotNone(h["last_cleanup"], "清理过就必须能在体检结果里看到")
        self.assertEqual(h["last_cleanup"]["deleted"], 3)
        self.assertTrue(h["last_cleanup"]["cutoff"], "必须带出这次清理用的截止点")

    def test_health_without_cleanup_reports_zero(self):
        db.audit("tester", "seed", "t", "d")
        h = db.audit_health(path=self.anchor)
        self.assertEqual(h["purge_total"], 0)
        self.assertIsNone(h["last_cleanup"])

    def test_alert_facts_carry_cleanup_volume(self):
        self._seed_expired_and_cleanup(old_rows=3)
        webapp = _load_webapp("cleanup_vis")
        facts = dict(webapp._audit_alert_facts(db.audit_health(path=self.anchor)))
        self.assertEqual(facts["累计留痕的审计清理条数"], 3)
        self.assertIn("截止", facts["最近一次审计清理"], "日报要带出最近清理的截止点")
        self.assertIn("删除 3 条", facts["最近一次审计清理"])

    def test_alert_facts_without_cleanup_say_none(self):
        db.audit("tester", "seed", "t", "d")
        webapp = _load_webapp("cleanup_vis_none")
        facts = dict(webapp._audit_alert_facts(db.audit_health(path=self.anchor)))
        self.assertEqual(facts["累计留痕的审计清理条数"], 0)
        self.assertEqual(facts["最近一次审计清理"], "（无）")


class CliOutputTest(_CleanupFixture):
    """取证 CLI 的输出必须带清理量（异机核对靠它，不靠本机自校验）。"""

    def _run_cli(self):
        env = dict(os.environ)
        # 子进程 stdout 走管道时按平台本地编码，中文会按 cp936 写出而读侧按 UTF-8 解码；
        # 显式钉住子进程编码，测试才不会在 Windows 上随机红。
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, os.path.join(BASE, "scripts", "audit_verify.py"),
             "--db", self.db_file, "--env", self.env_file, "--anchor", self.anchor],
            capture_output=True, text=True, encoding="utf-8", env=env,
            cwd=BASE, timeout=120,
        )

    def test_cli_prints_cleanup_volume(self):
        self._seed_expired_and_cleanup(old_rows=3)
        db.record_audit_anchor(self.anchor)  # 锚点反映清理后的合法状态
        r = self._run_cli()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("累计留痕的审计清理条数：3 条", r.stdout)
        self.assertIn("最近一次审计清理：", r.stdout)
        self.assertIn("删除 3 条", r.stdout)

    def test_cli_prints_none_when_never_cleaned(self):
        db.audit("tester", "seed", "t", "d")
        db.record_audit_anchor(self.anchor)
        r = self._run_cli()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("累计留痕的审计清理条数：0 条", r.stdout)
        self.assertIn("最近一次审计清理：（无）", r.stdout)

    def test_cli_refuses_foreign_db_without_explicit_anchor(self):
        """指向别的库时不得沿用本部署的锚点：应中止（无法定论），不得报"被篡改"。

        锚点路径来自部署的状态目录、与 `--db` 无关。拿它去比另一个库（取证副本、
        恢复出来的备份）时，"条数少了"只说明两套数据不是一套，说明不了篡改；报 exit 1
        会让运维按真实失陷响应。
        """
        self._seed_expired_and_cleanup(old_rows=3)
        db.record_audit_anchor(self.anchor)
        other_dir = tempfile.mkdtemp(prefix="yiban-foreign-", dir=self.tmp)
        foreign = os.path.join(other_dir, "yiban.db")
        shutil.copyfile(self.db_file, foreign)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        r = subprocess.run(
            [sys.executable, os.path.join(BASE, "scripts", "audit_verify.py"),
             "--db", foreign, "--env", self.env_file],
            capture_output=True, text=True, encoding="utf-8", env=env,
            cwd=BASE, timeout=120,
        )
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("无法推断它对应的锚点文件", r.stdout)
        self.assertNotIn("被篡改", r.stdout)


if __name__ == "__main__":
    unittest.main()
