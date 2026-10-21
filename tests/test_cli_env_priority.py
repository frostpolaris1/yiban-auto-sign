# -*- coding: utf-8 -*-
"""CLI 配置视图的优先级：非路径键 `.env` 优先、进程环境只补缺；四个路径键例外（ba-p10-02）。

标签：J · 运维：部署/备份/发布
覆盖：`yiban/entry/cli/__init__.py::_env_view` 与 `_cmd_state` 的取值链。
    M27（2026-10-01 起）规定**非路径键** `.env` 压过进程环境；`docs/dev/cli.md` §1.1
    只给四个路径键（`YIBAN_DB_FILE` / `YIBAN_STATE_DIR` /
    `YIBAN_LOG_FILE`）开"进程环境优先"的口子（工单 71z7）。
对应实现：`_env_view`（合并优先级）、`state_gc.state_dir_from_env` /
    `log_dir_from_env` / `retention_days`（消费视图）。
关键断言：同一份配置下，非路径键取 `.env` 值；路径键取进程环境值；`.env` 缺的键
    由进程环境补缺。不钉任何输出排版。
依赖：`sys.executable -m yiban.entry.cli state --dry-run --json` 子进程；
    临时 `.env` 与两个状态目录，不碰真实部署。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CliEnvPriorityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-cli-env-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.env_state = os.path.join(self.root, "env-state")
        self.dotenv_state = os.path.join(self.root, "dotenv-state")
        self.env_log = os.path.join(self.root, "env-logs", "sign.log")
        self.dotenv_log = os.path.join(self.root, "dotenv-logs", "sign.log")
        for path in (self.env_state, self.dotenv_state,
                     os.path.dirname(self.env_log), os.path.dirname(self.dotenv_log)):
            os.makedirs(path, exist_ok=True)
        self.dotenv = os.path.join(self.root, ".env")
        with open(self.dotenv, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_STATE_DIR={self.dotenv_state}\n"
                f"YIBAN_LOG_FILE={self.dotenv_log}\n"
                "YIBAN_RETENTION_DAYS=30\n"
            )

    def _run(self, extra_env):
        env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
        env.update({
            "PYTHONPATH": BASE,
            "PYTHONIOENCODING": "utf-8",
            "YIBAN_ENV_FILE": self.dotenv,
            "YIBAN_STATE_DIR": self.env_state,
            "YIBAN_LOG_FILE": self.env_log,
            "YIBAN_RETENTION_DAYS": "99",
        })
        env.update(extra_env)
        r = subprocess.run(
            [sys.executable, "-m", "yiban.entry.cli", "state", "--dry-run", "--json"],
            cwd=BASE, env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.DEVNULL, timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_dotenv_wins_for_non_path_keys_and_process_env_for_path_keys(self):
        payload = self._run({})
        # 非路径键：.env 的 30 压过进程环境的 99（M27）
        self.assertEqual(payload["retention_days"]["log"], 30,
                         "非路径键必须 .env 优先（M27）")
        # 路径键：进程环境优先（docs/dev/cli.md §1.1 的例外）
        self.assertEqual(payload["state_dir"], self.env_state,
                         "YIBAN_STATE_DIR 必须进程环境优先")
        self.assertEqual(payload["log_dir"], os.path.dirname(self.env_log),
                         "YIBAN_LOG_FILE 必须进程环境优先")

    def test_process_env_fills_keys_missing_from_dotenv(self):
        payload = self._run({"YIBAN_SNAPSHOT_RETENTION_DAYS": "5"})
        self.assertEqual(payload["retention_days"]["snapshot"], 5,
                         ".env 没有的键必须由进程环境补缺")


if __name__ == "__main__":
    unittest.main()
