# -*- coding: utf-8 -*-
"""批量手动签到冷却（2026-09-01 批次16 用户裁决：30 分钟起步，可配置）。

覆盖：
- 队列完成后冷却窗口内再次触发 → 429（默认 1800s）；
- YIBAN_BATCH_SIGN_COOLDOWN_SEC=0 → 关闭冷却（可立即再次触发）；
- 单账号手动签到不受批量冷却影响。

全程 mock subprocess.Popen（防真实 spawn signin 子进程），纯本地 Flask test client。
用法（项目根目录）：
    python -m pytest tests/test_batch_sign_cooldown_091.py -v
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"


class _FakeProc:
    """fake subprocess.Popen 返回值：wait 立即返回，terminate/kill 空操作。"""

    def __init__(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


class BatchSignCooldownTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-batch-cooldown-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        # 批次16 主审修复：Popen patch 提升到类级——整个测试类（含后台队列线程的
        # 任意调度时刻）都处于 fake Popen 之下。此前测试级 setUp/tearDown patch 在
        # tearDown stop 时若后台线程尚未执行 _spawn_signin_many（全量负载下线程调度
        # 延迟），会真实 spawn signin 子进程并持有 db 连接，下一个测试 setUp 删
        # yiban.db 报 PermissionError（全量 test_single_signin 失败根因）。
        cls._popen_patch = unittest.mock.patch.object(
            cls.webapp.subprocess, "Popen", return_value=_FakeProc())
        cls._popen_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._popen_patch.stop()
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR"):
            os.environ.pop(k, None)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(self.db_file, migrate_from=self.accounts_file,
                   env_file=self.env_file)
        # 清掉 .env 中可能残留的冷却键（默认 1800s）
        self._set_cooldown(None)
        # 插入 3 个 active 账号（owner=admin 直属，不占注册用户配额）
        for i, phone in enumerate(("13800000001", "13800000002", "13800000003")):
            db.add_account({
                "phone": phone, "password": f"accpass{i}",
                "owner": "admin", "name": f"t{i}", "status": "active",
                "phone_code": "",
            })

    def tearDown(self):
        # db 连接由 setUp 与 tearDownClass 管理；这里只释放单测实例资源
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def _set_cooldown(self, value):
        """写/删 .env 的冷却键（None=删键回默认）。"""
        with open(self.env_file, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
        lines = [ln for ln in lines
                 if not ln.strip().startswith("YIBAN_BATCH_SIGN_COOLDOWN_SEC=")]
        if value is not None:
            lines.append(f"YIBAN_BATCH_SIGN_COOLDOWN_SEC={value}".rstrip())
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _login(self, c):
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _trigger_batch(self, c, csrf):
        """触发批量签到（Popen 已在 setUp 级 patch，防真实 spawn 且覆盖后台线程）。"""
        return c.post("/api/signin/batch", json={
            "ids": [0, 1, 2],
            "phones": ["13800000001", "13800000002", "13800000003"],
        }, headers={"X-CSRF-Token": csrf})

    def _trigger_until_batch_done(self, c, csrf, deadline_s=15):
        """触发批量签到，若后台队列尚未完成（429"正在执行"）则轮询重试。

        全量测试负载下后台线程可能晚于固定 sleep 完成——固定 sleep 会让第二次
        触发撞上"队列正在执行"而非"冷却中"，断言错位（批次16 主审修复：时间敏感
        测试改为轮询等待）。队列完成后返回本次触发响应。
        """
        deadline = time.time() + deadline_s
        while True:
            r = self._trigger_batch(c, csrf)
            if r.status_code != 429:
                return r
            err = r.get_json().get("error", "")
            if "正在执行" in err and time.time() < deadline:
                time.sleep(0.5)
                continue
            return r

    # ---- 冷却 ----
    def test_cooldown_blocks_immediate_retry(self):
        """默认冷却 1800s：队列完成后立即再触发 → 429。"""
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        r1 = self._trigger_batch(c, csrf)
        self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True))
        # 轮询等待后台队列线程完成（更新冷却时间戳）后再次触发 → 冷却 429
        r2 = self._trigger_until_batch_done(c, csrf)
        self.assertEqual(r2.status_code, 429, r2.get_data(as_text=True))
        self.assertIn("冷却中", r2.get_json()["error"])

    def test_cooldown_zero_disables(self):
        """YIBAN_BATCH_SIGN_COOLDOWN_SEC=0 → 关闭冷却，可立即再次触发。"""
        self._set_cooldown(0)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        r1 = self._trigger_batch(c, csrf)
        self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True))
        # 冷却关闭：队列完成后可再次触发（200）
        r2 = self._trigger_until_batch_done(c, csrf)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))

    def test_cooldown_short_window(self):
        """自定义短冷却（如 2s）：窗口内 429，窗口过后可再次触发。"""
        self._set_cooldown(2)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        self.assertEqual(self._trigger_batch(c, csrf).status_code, 200)
        # 队列完成后（轮询）再触发 → 2s 冷却窗口内 429
        r = self._trigger_until_batch_done(c, csrf)
        self.assertEqual(r.status_code, 429, "2s 冷却窗口内应拒绝")
        time.sleep(2.5)  # 等窗口过期
        r2 = self._trigger_batch(c, csrf)
        self.assertEqual(r2.status_code, 200, "冷却窗口过后应恢复")

    def test_single_signin_not_blocked_by_cooldown(self):
        """单账号手动签到（/api/signin）不受批量冷却影响（复用冷却时间戳不生效）。"""
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        self.assertEqual(self._trigger_batch(c, csrf).status_code, 200)
        # 等批量队列完成（冷却已挂），再触发单账号签到——不应被冷却拦
        self._trigger_until_batch_done(c, csrf)
        r = c.post("/api/signin", json={"phone": "13800000001"},
                   headers={"X-CSRF-Token": csrf})
        self.assertIn(r.status_code, (200, 404),
                      "单账号签到不应被批量冷却拦截（应返回签到结果或账号校验，而非冷却 429）")


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
