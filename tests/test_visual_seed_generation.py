# -*- coding: utf-8 -*-
"""演示种子脚本的状态覆盖与失败响亮（工单 sjem / ba-p11-07 / ba-p11-08）。

标签：J · 运维：部署/备份/发布
覆盖：
  · `scripts/dev_visual_seed.py` 用随机密钥建演示环境（不再用随仓库公开、且已被
    `account_crypto` 拉黑的演示钥），重跑复用同一把钥；
  · 种子数据的状态取值直接来自 `yiban.core.status`（`ALL_STATUSES` / `DISPLAY`），
    `global_paused` 等低频码不会漏；日志/事件/按日状态三处口径一致；
  · `web/app.py` 的执行失败不再被 `contextlib.suppress` 吞掉（AST 判据：文件里
    不存在 suppress，且 exec_module 调用不在任何 `with` 里）。
关键断言：钉"演示钥不公开、状态覆盖不靠手抄清单、导入期失败不静默"三条性质。
依赖：起 `sys.executable` 子进程调 `seed()`（临时目录，不碰真实 .env/DB）。
"""
import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "scripts", "dev_visual_seed.py")
PUBLISHED_DEMO_KEY = "7f3a9c1e5b2d8046af17c3e9b5d2084c6ea93f7b1d5c8042a6e93f1b7d5c2084"

_SEED_CODE = (
    "import importlib.util, json, sys\n"
    "spec = importlib.util.spec_from_file_location('dev_visual_seed', sys.argv[1])\n"
    "mod = importlib.util.module_from_spec(spec)\n"
    "sys.modules['dev_visual_seed'] = mod\n"
    "spec.loader.exec_module(mod)\n"
    "info = mod.seed(sys.argv[2])\n"
    "print(json.dumps({'accounts': info['accounts'], 'events': info['events']}))\n"
)


def _run_seed(tmp):
    env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
    env.update({"PYTHONPATH": BASE, "PYTHONIOENCODING": "utf-8"})
    r = subprocess.run(
        [sys.executable, "-c", _SEED_CODE, SCRIPT, tmp],
        cwd=BASE, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", stdin=subprocess.DEVNULL, timeout=240,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _read_env_key(tmp):
    with open(os.path.join(tmp, ".env"), encoding="utf-8-sig") as f:
        for line in f:
            if line.startswith("YIBAN_ACCOUNTS_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


class VisualSeedKeyTest(unittest.TestCase):
    def test_random_key_is_generated_and_reused(self):
        with tempfile.TemporaryDirectory(prefix="yiban-visual-key-") as tmp:
            info = _run_seed(tmp)
            key = _read_env_key(tmp)
            self.assertRegex(key, r"^[0-9a-f]{64}$")
            self.assertNotEqual(key, PUBLISHED_DEMO_KEY,
                                "不得再使用随仓库公开、且已被 account_crypto 拉黑的演示钥")
            self.assertGreater(info["accounts"], 0,
                               "种子账号必须真的写进库（旧演示钥会在加密第一步就被拒）")
            # 重跑（不带 reset）：同一把钥，否则旧库密文解不开
            _run_seed(tmp)
            self.assertEqual(_read_env_key(tmp), key,
                             "重跑必须复用既有 .env 的钥，否则旧库密文不可解")


class VisualSeedStatusCoverageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory(prefix="yiban-visual-status-")
        cls.tmp = cls.tmpdir.name
        _run_seed(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def test_every_display_symbol_appears_in_daily_state(self):
        import yiban.core.status as status
        expected = {entry["symbol"] for entry in status.DISPLAY.values()}
        found = set()
        state_dir = os.path.join(self.tmp, "state")
        for name in os.listdir(state_dir):
            if not (name.startswith("sign-daily-") and name.endswith(".json")):
                continue
            with open(os.path.join(state_dir, name), encoding="utf-8") as f:
                found.update(json.load(f).values())
        self.assertEqual(found, expected,
                         "近 35 天的按日状态必须覆盖 DISPLAY 全部符号"
                         f"（缺 {sorted(expected - found)}）")
        self.assertIn(status.SYMBOL[status.STATUS_GLOBAL_PAUSED], found,
                      "global_paused 必须有出处（ba-p11-08 漏的正是它）")

    def test_sign_events_cover_all_statuses(self):
        import yiban.core.status as status
        db_file = os.path.join(self.tmp, "yiban.db")
        conn = sqlite3.connect(db_file)
        try:
            found = {r[0] for r in conn.execute("SELECT DISTINCT status FROM sign_events")}
        finally:
            conn.close()
        self.assertEqual(found, set(status.ALL_STATUSES),
                         "签到事件的状态取值必须覆盖 ALL_STATUSES"
                         f"（缺 {sorted(set(status.ALL_STATUSES) - found)}）")


class VisualSeedNoSilentImportFailureTest(unittest.TestCase):
    def test_no_contextlib_suppress_and_exec_module_is_not_wrapped(self):
        with open(SCRIPT, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("contextlib.suppress", src,
                         "种子脚本不得用 contextlib.suppress 吞掉导入/写库失败（ba-p11-07）")
        tree = ast.parse(src)
        exec_calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "exec_module"
        ]
        self.assertTrue(exec_calls, "脚本必须显式执行 web/app.py")
        wrapped = set()
        for parent in ast.walk(tree):
            if isinstance(parent, ast.With):
                inner = {id(n) for stmt in parent.body for n in ast.walk(stmt)}
                for call in exec_calls:
                    if id(call) in inner:
                        wrapped.add(call.lineno)
        self.assertEqual(wrapped, set(),
                         f"exec_module 不得包在 with 里（静默吞异常）：行 {sorted(wrapped)}")


if __name__ == "__main__":
    unittest.main()
