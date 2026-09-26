# -*- coding: utf-8 -*-
"""手动签到在 spawn 前剔除已自暂停账号（批量计数不虚高）。

标签：E · Web：认证/权限/API
覆盖：`POST /api/signin` 与 `POST /api/signin/batch` 在派发子进程**之前**剔除 `user_paused` 账号；单条给出明确文案，批量给出"N 个已自暂停，跳过"计数且返回计数 == 实际 spawn 的账号数；全选皆自暂停时不派发
对应实现：`web/routes/signin_api.py` 的 `_spawn_signin` / `api_signin_batch`；被跳过的账号不进入 `signin.py --only` 的账号串
关键断言：响应计数（`count` / `skipped_paused`）与 `Popen` 实际收到的 `--only` 账号串**逐字对得上**；已自暂停的号根本不出现在 argv 里，因此引擎侧不会再为它打印"⏹️ 用户已取消签到"（引擎侧 `round.py` 的拦截是第二道保险，保持不动）
依赖：纯本地 Flask test client + 临时 `.env`/SQLite，`subprocess.Popen` 打桩记录 argv（不真的拉起签到子进程）；不联网、不访问真实易班接口；无需 node/bash

**为什么需要**：登记表 MF-101——两处派发点都不看 `user_paused`，一个号被用户自己暂停后
仍会被计进"N 个账号"，子进程起来后由引擎侧 `round.py` 拦下并写"用户已取消签到"。
用户看到的是"已触发 N 个"而实际一个都没签，批量审计计数也是虚的。急停/周末对手动腿的
豁免是**设计**（`tests/test_global_pause.py` 锁定），本文件一行门语义都不碰：只把
"用户自暂停"这一条**实际拦得住**的门提前到派发之前，计数与提示据实。
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

TEST_KEY = "a" * 64
ADMIN_PASS = "MasterPass#2026"


class _RecordingProc:
    """假子进程：只回答 wait/poll，不真的执行任何东西。"""

    def __init__(self, argv, store):
        self.argv = argv
        self.store = store
        self.returncode = 0
        self.pid = 4242
        store.append(list(argv))

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


class PausedSpawnFilterTest(unittest.TestCase):
    """两处派发点都在 spawn 前剔除自暂停账号。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-paused-spawn-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                    f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n")
        with io.open(cls.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp

        global db
        import db

        spec = importlib.util.spec_from_file_location(
            "webapp_paused_spawn", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_paused_spawn"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

        cls.popen_argv = []
        cls._popen_patch = mock.patch.object(
            cls.webapp.subprocess, "Popen",
            side_effect=lambda argv, **kw: _RecordingProc(argv, cls.popen_argv))
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

    # ---- 夹具 ----
    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        self.popen_argv.clear()
        self._seed_accounts()

    def tearDown(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    def _seed_accounts(self):
        """三个账号：0/2 正常，1 已自暂停（三者的顺序即 `load_accounts()` 的下标序）。"""
        self.phones = ["13800000001", "13800000002", "13800000003"]
        for i, phone in enumerate(self.phones):
            db.add_account({"phone": phone, "password": f"pw{i}", "owner": "admin",
                            "name": f"t{i}", "status": "active", "phone_code": ""})
        rows = {a["phone"]: a["id"] for a in db.load_accounts_raw()}
        db.set_user_paused(rows[self.phones[1]], 1)
        self.loaded = self.webapp.load_accounts()
        self.ids = list(range(len(self.loaded)))
        self.phones_in_order = [str(a.get("phone", "")) for a in self.loaded]
        self.paused_phone = self.phones[1]
        self.active_phones = [p for p in self.phones_in_order if p != self.paused_phone]

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        token = c.get("/api/me").get_json()["csrf_token"]
        return c, token

    def _wait_popen(self, n=1, timeout=5.0):
        """等后台批量队列线程真正派发（批量端点立即返回，spawn 在线程里）。"""
        deadline = time.time() + timeout
        while time.time() < deadline and len(self.popen_argv) < n:
            time.sleep(0.02)
        return len(self.popen_argv)

    # ---- 单条 ----
    def test_single_paused_account_is_rejected_before_spawn(self):
        """单条：已自暂停号在 spawn 前被拒（400 + 明确文案），不派发子进程。

        当前红：派发点不看 `user_paused`，请求返回 200「已触发」而子进程起来后由引擎侧
        拦下并写"用户已取消签到"——用户侧得到的是一次假成功。
        """
        c, token = self._admin_client()
        r = c.post("/api/signin", json={"phone": self.paused_phone},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("已自暂停", r.get_json().get("error", ""))
        self.assertEqual(self.popen_argv, [], "被自暂停的账号不得派发子进程")

    def test_single_active_account_still_spawns(self):
        """正常账号不受影响（改动不得把正常手动签到也挡住）。"""
        c, token = self._admin_client()
        r = c.post("/api/signin", json={"phone": self.active_phones[0]},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(self.popen_argv), 1)
        self.assertEqual(self.popen_argv[0][-1], self.active_phones[0])

    # ---- 批量 ----
    def test_batch_count_matches_actual_spawn(self):
        """批量：混入自暂停号 ⇒ 计数 == 实际 spawn 数，且被跳过数明说。"""
        c, token = self._admin_client()
        with mock.patch.object(self.webapp.db, "audit") as m_audit:
            r = c.post("/api/signin/batch",
                       json={"ids": self.ids, "phones": self.phones_in_order},
                       headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["count"], len(self.active_phones))
        self.assertEqual(body["skipped_paused"], 1)
        self.assertIn("1 个已自暂停，跳过", body["msg"])
        self.assertEqual(self._wait_popen(1), 1, "批量应合并为单队列子进程")
        only = self.popen_argv[0][-1]
        spawned = only.split(",")
        self.assertEqual(len(spawned), body["count"], "响应计数必须等于实际 spawn 数")
        self.assertEqual(sorted(spawned), sorted(self.active_phones))
        self.assertNotIn(self.paused_phone, spawned,
                         "被自暂停的号根本没 spawn，引擎侧不会再为它打印“用户已取消签到”")
        batch_audits = [c_ for c_ in m_audit.call_args_list
                        if c_.args and c_.args[1] == "signin_batch"]
        self.assertEqual(len(batch_audits), 1)
        self.assertIn(f"批量签到 {body['count']} 个", batch_audits[0].args[3])

    def test_batch_all_paused_reports_and_does_not_spawn(self):
        """全选皆自暂停：如实说明并拒绝，不派发空队列子进程。"""
        c, token = self._admin_client()
        idx = self.phones_in_order.index(self.paused_phone)
        r = c.post("/api/signin/batch",
                   json={"ids": [idx], "phones": [self.paused_phone]},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("已自暂停", r.get_json().get("error", ""))
        self.assertEqual(self.popen_argv, [])

    def test_batch_without_paused_accounts_is_unchanged(self):
        """没有自暂停号时行为不变（既不报跳过，也不改计数）。"""
        c, token = self._admin_client()
        ids = [i for i, p in enumerate(self.phones_in_order) if p != self.paused_phone]
        r = c.post("/api/signin/batch",
                   json={"ids": ids,
                         "phones": [self.phones_in_order[i] for i in ids]},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["count"], len(ids))
        self.assertEqual(body["skipped_paused"], 0)
        self.assertNotIn("已自暂停", body["msg"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
