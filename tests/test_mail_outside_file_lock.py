# -*- coding: utf-8 -*-
"""全局 `_file_lock` 临界区内不得发生网络 I/O（工单 ba-p05-01）。

标签：E · Web：并发/通知
覆盖：批量拒信、容量触顶告警这两族发信落点的**发信时刻**；嵌套 `with` 只汇合一次；
未持锁时立即发送；`run_after_file_lock` 这条汇合点本身。
关键断言：发信发生的当下，另一个线程能立刻拿到 `_file_lock`（拿不到即"SMTP 阻塞
全站读快照"的真身）；一封都不许少发；容量告警的去重旗在锁内落定、发信在出锁后。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite，不联网；SMTP 出口打桩。

复现口径说明：判据打在**被测对象**（`web.services.locks._file_lock` 与路由本身）上——
探测线程对真锁做非阻塞 acquire，不是在桩里自证。
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"


def _probe_other_thread_can_take(lock):
    """另起一个线程对 `lock` 做非阻塞 acquire，能拿到返回 True。

    必须换线程：RLock 对同线程可重入，用发信线程自己探测永远拿得到，测不出持锁。
    """
    box = {}

    def probe():
        box["got"] = lock.acquire(blocking=False)
        if box["got"]:
            lock.release()

    t = threading.Thread(target=probe)
    t.start()
    t.join(5)
    return box.get("got", False)


class FileLockSendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-locksend-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        spec = importlib.util.spec_from_file_location(
            "webapp_locksend", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_locksend"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        cls.db = cls.webapp.db
        # 真锁对象本身（与路由经 m._file_lock 取到的是同一枚）
        from web.services import locks as locks_mod
        cls.lock = locks_mod._file_lock

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        sys.modules.pop("webapp_locksend", None)

    def setUp(self):
        if self.db._conn is not None:
            with contextlib.suppress(Exception):
                self.db._conn.close()
            self.db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        self.db.init_db(self.db_file, migrate_from=self.accounts_file,
                        env_file=self.env_file)
        # 容量告警去重旗是进程级：逐例复位，否则上一例的旗把本例的告警吞掉
        self._saved_flags = dict(self.webapp._capacity_alerts)
        for k in self.webapp._capacity_alerts:
            self.webapp._capacity_alerts[k] = False

    def tearDown(self):
        for k, v in self._saved_flags.items():
            self.webapp._capacity_alerts[k] = v

    # ---- 工具 ----
    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _submit(self, email, phone):
        c = self.webapp.create_app().test_client()
        token = self._login(c, email, USER_PASS)
        r = c.post("/api/my-accounts",
                   json={"name": "测试账号", "phone": phone, "password": "p1"},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def _admin(self):
        c = self.webapp.create_app().test_client()
        token = self._login(c, "admin", ADMIN_PASS)
        return c, token

    def test_batch_reject_mail_is_not_sent_while_lock_held(self):
        """批量拒绝的每封拒信必须在**锁已释放**之后发出。

        今天（工单 ba-p05-01）这枚循环整体位于 `with m._file_lock:` 内：SMTP 一旦
        阻塞，load_accounts / load_users / 所有读快照一并排队。判据打在真锁上。
        """
        owners = [("u1@test.local", "13800138001"),
                  ("u2@test.local", "13800138002"),
                  ("u3@test.local", "13800138003")]
        for email, phone in owners:
            self.db.create_user(email, self.webapp.generate_password_hash(USER_PASS))
            self._submit(email, phone)
        c, token = self._admin()
        accounts = c.get("/api/accounts").get_json()["accounts"]
        self.assertEqual(len(accounts), 3, "前置条件：3 户各 1 个待审账号")
        seen = {}

        def fake_send_user(owner, subject, body):
            seen[owner] = _probe_other_thread_can_take(self.lock)
            return True

        with mock.patch.object(self.webapp.mailer, "send_user", side_effect=fake_send_user):
            r = c.post("/api/accounts/batch",
                       json={"action": "reject",
                             "ids": [a["index"] for a in accounts],
                             "phones": [a["phone"] for a in accounts],
                             "reason": "批量复核不符"},
                       headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(seen), 3, "一封都不许少发")
        self.assertEqual([v for v in seen.values()], [True, True, True],
                         "发信时刻别的线程必须能拿到 _file_lock（拿到=锁没被发信占住）")

    def test_capacity_alert_flag_in_lock_send_after_release(self):
        """容量触顶：去重旗在锁内落定（并发不双发），发信本身排在出锁后。"""
        sent = []
        cap = self.webapp._capacity

        def fake_send_notification(title, content, urgent=False, **kw):
            sent.append((title, _probe_other_thread_can_take(self.lock)))

        with self.lock:
            cap._notify_capacity_once("users", 5, "注册上限",
                                     send_notification=fake_send_notification)
            self.assertEqual(sent, [], "持锁期间不得真的发信")
        self.assertEqual(len(sent), 1, "出锁后必须发出去，一封都不许少")
        self.assertTrue(sent[0][1], "发信时刻锁必须已释放")
        with self.lock:
            cap._notify_capacity_once("users", 5, "注册上限",
                                     send_notification=fake_send_notification)
        self.assertEqual(len(sent), 1, "每进程每种资源仍只发一次（去重旗不许被挪出锁）")

    def test_nested_lock_flushes_registry_exactly_once(self):
        """RLock 嵌套 `with` 时汇合只能在最外层退出发生一次：不重发、不漏发。

        内层退出只把 RLock 计数减 1，锁仍被本线程持有；此刻若发信就是"锁内网络
        I/O"，正是工单要消灭的形状。故内层退出后登记表必须原样留着。
        """
        ran = []
        with self.lock:
            with self.lock:
                self.webapp.run_after_file_lock(ran.append, "a")
                self.assertEqual(ran, [], "内层退出不得提前发")
            self.assertEqual(ran, [], "内层退出后锁仍被持有，同样不得发")
            self.webapp.run_after_file_lock(ran.append, "b")
        self.assertEqual(ran, ["a", "b"], "最外层退出按登记顺序各发一次")

    def test_registration_without_lock_sends_immediately(self):
        """未持锁的登记路径保持今天的语义：当场发送（锁外调用点行为不变）。"""
        ran = []
        self.webapp.run_after_file_lock(ran.append, "now")
        self.assertEqual(ran, ["now"])


if __name__ == "__main__":
    unittest.main()
