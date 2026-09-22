# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""部署路径的解析口径与恢复件核验的结论分类。

两条都来自"真实部署者只照 README 做"的演练，且都会给出**错误结论**而不报错：

1. `YIBAN_STATE_DIR` / `YIBAN_LOG_FILE` 写进 `.env` 时，过去只有跑 `run.sh` 的那条路
   生效（脚本自己 export），web 进程与直接调用的脚本静默回落到 `/var/log/yiban`——
   同机第二份部署因此与第一份共用状态目录、锁与磁盘外锚点。
2. 恢复件审计核验用系统 `python3` 跑，并把**退出码 1** 一律当"检出篡改"。解释器缺依赖
   时 Python 抛 ImportError 也以 1 退出，于是合法的恢复演练被报成"审计被改写/删除"。
"""

import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tests"))

from yiban.engine import state_io  # noqa: E402
from yiban.infra import env_io  # noqa: E402


class ResolvePathTest(unittest.TestCase):
    """`env_io.resolve_path`：进程环境 → .env → 默认值，三者优先级钉死。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="resolve-path-")
        self.env_file = os.path.join(self.tmp, ".env")
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_STATE_DIR=/from/env-file\nYIBAN_LOG_FILE=/from/env-file/sign.log\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_file_is_second_source(self):
        self.assertEqual(
            env_io.resolve_path("YIBAN_STATE_DIR", "/fallback", env={}, env_file=self.env_file),
            "/from/env-file")
        self.assertEqual(
            env_io.resolve_path("YIBAN_LOG_FILE", "/fallback", env={}, env_file=self.env_file),
            "/from/env-file/sign.log")

    def test_process_env_wins_over_env_file(self):
        self.assertEqual(
            env_io.resolve_path("YIBAN_STATE_DIR", "/fallback",
                                env={"YIBAN_STATE_DIR": "/from/env"}, env_file=self.env_file),
            "/from/env")

    def test_blank_values_fall_through_to_default(self):
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_STATE_DIR=   \n")
        self.assertEqual(
            env_io.resolve_path("YIBAN_STATE_DIR", "/fallback", env={}, env_file=self.env_file),
            "/fallback")
        self.assertEqual(
            env_io.resolve_path("YIBAN_STATE_DIR", "/fallback",
                                env={"YIBAN_STATE_DIR": "  "},
                                env_file=os.path.join(self.tmp, "missing.env")),
            "/fallback")

    def test_missing_env_file_is_not_fatal(self):
        self.assertEqual(
            env_io.resolve_path("YIBAN_STATE_DIR", "/fallback", env={},
                                env_file=os.path.join(self.tmp, "nope.env")),
            "/fallback")


class StateDirHonoursEnvFileTest(unittest.TestCase):
    """引擎与 web 的路径常量都必须认 .env（不只认进程环境）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="state-dir-env-")
        self.state = os.path.join(self.tmp, "custom-state")
        os.makedirs(self.state, exist_ok=True)
        self.env_file = os.path.join(self.tmp, ".env")
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_STATE_DIR={self.state}\n"
                    f"YIBAN_LOG_FILE={self.state}/sign.log\n")
        self._old = {k: os.environ.get(k) for k in
                     ("YIBAN_ENV_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE")}
        for k in ("YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)
        os.environ["YIBAN_ENV_FILE"] = self.env_file

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_engine_state_dir_follows_env_file(self):
        self.assertEqual(state_io._state_dir(), self.state,
                         "引擎的状态目录必须认 .env 里的 YIBAN_STATE_DIR")

    def test_process_env_still_wins(self):
        os.environ["YIBAN_STATE_DIR"] = "/from/process-env"
        try:
            self.assertEqual(state_io._state_dir(), "/from/process-env")
        finally:
            os.environ.pop("YIBAN_STATE_DIR", None)

    def test_web_module_constants_follow_env_file(self):
        """web 是模块级常量，必须在导入期就解析对（gunicorn 走 create_app() 不跑 main）。"""
        import contextlib
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "webapp_state_dir", os.path.join(BASE, "web", "app.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["webapp_state_dir"] = mod
        with contextlib.suppress(Exception):
            spec.loader.exec_module(mod)
        if not hasattr(mod, "STATE_DIR"):
            raise AssertionError("web/app.py 未能加载，路径常量无从比对")
        self.assertEqual(os.path.normpath(mod.STATE_DIR), os.path.normpath(self.state))
        self.assertEqual(os.path.normpath(mod.LOG_FILE),
                         os.path.normpath(os.path.join(self.state, "sign.log")))


@unittest.skipIf(shutil.which("bash") is None, "需要 bash")
class RestoreVerdictTest(unittest.TestCase):
    """恢复件核验：崩溃 / 检出篡改 / 通过 / 无法核验，四态互不混淆。"""

    SCRIPT = os.path.join(BASE, "scripts", "backup.sh")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="restore-verdict-")
        self.app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(self.app, "scripts"), exist_ok=True)
        os.makedirs(os.path.join(self.app, ".venv", "bin"), exist_ok=True)
        # 桩"部署自带解释器"：行为由 STUB_MODE 决定，用来分别模拟崩溃与真实结论
        stub = os.path.join(self.app, ".venv", "bin", "python")
        with io.open(stub, "w", encoding="utf-8", newline="\n") as f:
            f.write('#!/bin/bash\n'
                    'case "${STUB_MODE:-ok}" in\n'
                    '  ok) echo "链内自洽：通过"; echo "审计可追溯性校验通过" ; exit 0 ;;\n'
                    '  tamper) echo "锚点比对：失败"; '
                    'echo "审计可追溯性校验失败：审计记录可能被篡改/删除"; exit 1 ;;\n'
                    '  crash) echo "Traceback (most recent call last):"; '
                    'echo "ModuleNotFoundError: No module named \'Crypto\'" >&2; exit 1 ;;\n'
                    'esac\n')
        os.chmod(stub, 0o755)
        with io.open(os.path.join(self.app, "scripts", "audit_verify.py"),
                     "w", encoding="utf-8") as f:
            f.write("# 桩：真实脚本由上面的解释器桩替代\n")
        # 备份包：含库 + .env + 锚点
        self.archive = os.path.join(self.tmp, "pkg.tar.gz")
        src = os.path.join(self.tmp, "src")
        os.makedirs(os.path.join(src, "data"))
        os.makedirs(os.path.join(src, "state"))
        # 必须是**真**的 SQLite 库：恢复路径要跑 PRAGMA integrity_check，假文件会以
        # "读取失败"把通过路径也判成失败（测的就不是核验分类了）。
        import sqlite3
        conn = sqlite3.connect(os.path.join(src, "data", "yiban.db"))
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        with io.open(os.path.join(src, "data", ".env"), "w", encoding="utf-8") as f:
            f.write("YIBAN_AUDIT_KEY=" + "b" * 64 + "\n")
        with io.open(os.path.join(src, "state", "audit-anchor.log"), "w",
                     encoding="utf-8") as f:
            f.write("2026-09-18 00:00:00 1 1 1 0 " + "a" * 64 + " " + "0" * 64 + "\n")
        with tarfile.open(self.archive, "w:gz") as t:
            t.add(src, arcname=".")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _restore(self, mode):
        env = dict(os.environ)
        env["APP_DIR"] = "app"          # 相对 cwd：Git Bash 的 tar 不认 C:\ 形式
        env["STUB_MODE"] = mode
        r = subprocess.run(["bash", self.SCRIPT, "--restore", "pkg.tar.gz",
                            "restored-" + mode],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, cwd=self.tmp, timeout=180)
        return r, (r.stdout or "") + (r.stderr or "")

    def test_clean_verdict_passes(self):
        r, out = self._restore("ok")
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("审计校验通过", out)

    def test_tamper_verdict_is_reported_as_tamper(self):
        r, out = self._restore("tamper")
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("检出异常", out)
        self.assertNotIn("无法定论", out)

    def test_tool_crash_is_not_reported_as_tamper(self):
        """解释器缺依赖（ImportError，退出码同样是 1）绝不能报成"被篡改"。"""
        r, out = self._restore("crash")
        self.assertEqual(r.returncode, 2, out)
        self.assertIn("无法定论", out)
        self.assertNotIn("链被改写/删除", out)

    def test_missing_tooling_is_not_a_pass(self):
        """包内含库但机器上没有可用解释器/脚本：不能算作"恢复成功"。"""
        shutil.rmtree(os.path.join(self.app, ".venv"))
        shutil.rmtree(os.path.join(self.app, "scripts"))
        env = dict(os.environ)
        env["APP_DIR"] = "app"
        r = subprocess.run(["bash", self.SCRIPT, "--restore", "pkg.tar.gz", "restored-none"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, cwd=self.tmp, timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(r.returncode, 2, out)
        self.assertIn("未经审计核验", out)


@unittest.skipIf(shutil.which("bash") is None, "需要 bash")
class BackupLogDirTest(unittest.TestCase):
    """备份抓的日志目录必须跟随配置，而不是硬编码 /var/log/yiban。"""

    SCRIPT = os.path.join(BASE, "scripts", "backup.sh")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backup-logdir-")
        self.app = os.path.join(self.tmp, "app")
        self.logs = os.path.join(self.tmp, "my-logs")
        os.makedirs(self.logs, exist_ok=True)
        os.makedirs(self.app, exist_ok=True)
        self.marker = "sign-2026-01-01.log"
        with io.open(os.path.join(self.logs, self.marker), "w", encoding="utf-8") as f:
            f.write("本轮签到日志\n")
        with io.open(os.path.join(self.app, ".env"), "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + "a" * 64 + "\n"
                    f"YIBAN_LOG_FILE={self.logs}/sign.log\n")
        self.backups = os.path.join(self.tmp, "backups")
        os.makedirs(self.backups, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_backup_follows_configured_log_dir(self):
        env = dict(os.environ)
        env.pop("YIBAN_LOG_FILE", None)          # 只从 .env 取，模拟 cron 直跑
        env["APP_DIR"] = "app"
        env["BACKUP_DIR"] = "backups"
        env["BACKUP_PLAINTEXT"] = "1"            # 免 gpg，只验抓哪个目录
        r = subprocess.run(["bash", self.SCRIPT], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env,
                           cwd=self.tmp, timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        archives = [os.path.join(self.backups, n) for n in os.listdir(self.backups)
                    if n.endswith(".tar.gz")]
        self.assertTrue(archives, f"没有产出归档：{out}")
        with tarfile.open(archives[0]) as t:
            names = t.getnames()
        self.assertTrue(any(n.endswith(self.marker) for n in names),
                        f"归档未包含配置目录里的日志：{names}")
        self.assertFalse([n for n in names if n.endswith(".log")
                          and self.marker not in n and "sign-" in n],
                         f"归档混入了配置目录之外的签到日志：{names}")


SCRIPT = os.path.join(BASE, "scripts", "pull-prod-backup.sh")


class PullProdBackupContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = shutil.which("bash")
        with io.open(SCRIPT, encoding="utf-8") as f:
            cls.src = f.read()

    def _bash(self, *args):
        return subprocess.run([self.bash, SCRIPT, *args], capture_output=True)

    def test_bash_syntax_ok(self):
        if not self.bash:
            self.skipTest("无 bash 环境")
        r = subprocess.run([self.bash, "-n", SCRIPT], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))

    def test_unknown_arg_exits_2(self):
        if not self.bash:
            self.skipTest("无 bash 环境")
        r = self._bash("--nope")
        self.assertEqual(r.returncode, 2, "未知参数应返回 2（用法错误）")

    def test_help_prints_usage(self):
        if not self.bash:
            self.skipTest("无 bash 环境")
        r = self._bash("-h")
        self.assertEqual(r.returncode, 0)
        self.assertIn("用法", r.stdout.decode("utf-8", "replace"))

    def test_readonly_contract_documented(self):
        """对生产只读是硬契约，必须写在脚本里（远端只允许 ls / sha256sum + scp 下载）。"""
        self.assertIn("对生产完全只读", self.src)
        self.assertIn("ls", self.src)
        self.assertIn("sha256sum", self.src)

    def test_refuses_to_run_on_production_host(self):
        """防呆：别在生产机自己身上"做异机副本"。"""
        self.assertIn("/opt/yiban-auto-sign", self.src)
        self.assertIn("异机副本必须在另一台机器上拉取", self.src)

    def test_only_pulls_ciphertext(self):
        """只搬密文（.tar.gz.gpg），绝不把明文 tar.gz 拉回工作站。"""
        self.assertIn("yiban-*.tar.gz.gpg", self.src)
        self.assertNotIn("yiban-*.tar.gz\"", self.src.replace("yiban-*.tar.gz.gpg", ""))

    def test_three_way_hash_check(self):
        """远端哈希 / 下载后复算 / 随行清单三处一致才入库，失败改名 .bad 保留证据。"""
        self.assertIn("_remote_sha256", self.src)
        self.assertIn(".bad", self.src)

    def test_freshness_selfcheck_present(self):
        """新鲜度自检：本地落后于远端、或远端本身停更 → 非 0 退出（供定时任务告警）。"""
        self.assertIn("freshness_check", self.src)
        self.assertIn("STALE_DAYS", self.src)
        self.assertIn("生产的每日备份链路疑似中断", self.src)
        self.assertIn("--status", self.src)

    def test_incremental_backfill_documented(self):
        """补齐式增量是"开机时间不定"能成立的前提，必须写进脚本说明。"""
        self.assertIn("增量", self.src)
        self.assertIn("补齐", self.src)

    def test_default_mirror_dir_is_neutral(self):
        """默认镜像目录不得写入某个运维者的个人目录布局（本仓库公开）。

        2026-09-10 复检：脚本首版把 Windows 默认写成 D:/code/backups/...（本机路径），
        属个人环境信息泄漏到公开仓库；现改为跨平台中性的 $HOME/yiban-prod-mirror，
        需要换盘由 LOCAL_MIRROR_DIR 覆盖。此断言防止回退。
        """
        self.assertIn('LOCAL_MIRROR_DIR="${LOCAL_MIRROR_DIR:-$HOME/yiban-prod-mirror}"',
                      self.src)
        self.assertNotIn("D:/code/", self.src)
        self.assertNotIn("C:/Users/", self.src)


if __name__ == "__main__":
    unittest.main()
