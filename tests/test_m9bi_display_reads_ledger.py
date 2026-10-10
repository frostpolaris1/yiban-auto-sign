# -*- coding: utf-8 -*-
"""m9bi 校验者：三个用户可见面的计划时刻只读台账 `sign_tasks.run_at`。

标签：E · Web：认证/权限/API
覆盖：显示层改读台账后，三条读路径与执行计划同源——
    ① GET /api/my-time-pref 的 `estimated`；
    ② GET /api/my-accounts 的 `state_message` 与排队键（`queue_ahead` 的排序依据）；
    ③ GET /api/accounts 的 `state_msgs`。
对应实现：`yiban/store/queue_store.run_at_by_phone`（台账按日批量读）、
    `web/services/accounts_data._estimate_slot`、`web/routes/my.py`、
    `web/routes/accounts_api.py`、`yiban/engine/runner.py`（停写 v2 scheduled）。
关键断言：三端点取值 == `sign_tasks.run_at`（|差| < 60 秒 = 1 个分片宽）；当日无计划行
    时显示「待生成」而非任何钟点；重试/重签改写过的 `run_at` 显示为当前台账值；
    my-accounts 的排队位置按台账时刻排序。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite；不联网、不访问真实易班接口；无需 node。

**为什么需要**：显示来源（v2 线性填块）与执行计划来源（v3 分层抖动）是两个算法，
uniform 下实测差 47 分钟（工单 m9bi）。判据只有一条：显示取值 == 台账 `run_at`。
本文件就是钉住这条判据的守卫——改动任一读点、或让显示回退 v2，这里必须红。
"""
import contextlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

from yiban import clock  # noqa: E402
from yiban.masking import mask_phone  # noqa: E402
from yiban.store import queue_store  # noqa: E402

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"

#: 分片宽 = 1 分钟 = 60s（工单判据里的"1 个分片宽度"）。
SLICE_SEC = 60


def _parse_clock(text):
    """从展示串里抽第一个 `HH:MM`（或 `HH:MM:SS`）→ 当日 datetime；抽不到返回 None。"""
    m = re.search(r"\b(\d{2}):(\d{2})(?::(\d{2}))?\b", str(text or ""))
    if not m:
        return None
    hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    day = clock.today()
    return datetime.strptime(f"{day} {hh:02d}:{mm:02d}:{ss:02d}", "%Y-%m-%d %H:%M:%S")


def _diff_sec(a, b):
    return abs((a - b).total_seconds())


class DisplayReadsLedgerE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-m9bi-")
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
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp_m9bi", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_m9bi"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE", "YIBAN_USERS_FILE",
                  "YIBAN_DB_FILE"):
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
            f.write("[]")
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        # 状态文件目录必须干净：上一用例残留的 sign-state 会让 v20 补账读出"当日已有计划"。
        for name in os.listdir(self.tmp):
            if name.startswith("sign-state-"):
                os.remove(os.path.join(self.tmp, name))

    # ---- 夹具 ----
    def _new_user(self, email, phone):
        db.create_user(email, self.webapp.generate_password_hash(USER_PASS))
        db.add_account({
            "name": "测试账号",
            "phone": phone,
            "password": "p1",
            "phone_model": "",
            "phone_code": "",
            "owner": email,
            "status": "active",
            "reject_reason": "",
        })

    def _add_plan(self, phone, hhmmss, day=None):
        """往台账写一行真实计划（vshard>=0、state=pending），run_at 用当日给定时刻。"""
        day = day or clock.today()
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, "
            "state, attempts, lease_until, result, epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (phone, day, 0, "", f"{day} {hhmmss}", 5, "pending", 0, "", "", 0,
             f"{day} 00:00:00"),
        )
        conn.commit()

    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    # ---- ① /api/my-time-pref.estimated ----
    def test_time_pref_estimated_matches_ledger(self):
        phone = "13800138001"
        self._new_user("u1@test.local", phone)
        self._add_plan(phone, "07:20:00")
        c = self.webapp.create_app().test_client()
        self._login(c, "u1@test.local", USER_PASS)
        data = c.get("/api/my-time-pref").get_json()
        estimated = data.get("estimated")
        shown = _parse_clock(estimated)
        self.assertIsNotNone(shown, f"estimated 必须给出钟点：{estimated!r}")
        want = datetime.strptime(f"{clock.today()} 07:20:00", "%Y-%m-%d %H:%M:%S")
        self.assertLess(_diff_sec(shown, want), SLICE_SEC,
                        f"estimated={estimated!r} 与台账 run_at 差超 1 个分片宽")

    # ---- ② /api/my-accounts 的 state_message 与排队键 ----
    def test_my_accounts_state_message_matches_ledger(self):
        phone = "13800138002"
        self._new_user("u2@test.local", phone)
        self._add_plan(phone, "07:20:00")
        c = self.webapp.create_app().test_client()
        self._login(c, "u2@test.local", USER_PASS)
        rows = c.get("/api/my-accounts").get_json()["accounts"]
        target = next(r for r in rows if r["phone"] == phone)
        self.assertEqual(target["state_message"], "计划 07:20",
                         "state_message 必须由台账 run_at 生成")

    def test_my_accounts_queue_order_follows_ledger(self):
        """排队位置按台账时刻：先到点者排前，后到点者的 queue_ahead 计数含前者。

        用内置管理员的裸账号（owner='admin'）——普通用户一人一号，凑不出两条队列。
        """
        early, late = "13800138003", "13800138004"
        for phone in (late, early):  # late 先入库（列表序在前），early 的 run_at 更早
            db.add_account({
                "name": f"账号{phone}", "phone": phone, "password": "p2",
                "phone_model": "", "phone_code": "", "owner": "admin",
                "status": "active", "reject_reason": "",
            })
        self._add_plan(late, "07:20:00")
        self._add_plan(early, "06:31:00")
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        rows = {r["phone"]: r for r in c.get("/api/my-accounts").get_json()["accounts"]}
        self.assertEqual(rows[early]["queue_ahead"], 0,
                         "台账时刻早者必须排第一（前方 0 个未了结）")
        self.assertEqual(rows[late]["queue_ahead"], 1,
                         "台账时刻晚者前方应计到 1 个未了结账号")

    # ---- ③ /api/accounts 的 state_msgs（管理员面） ----
    def test_accounts_state_msgs_matches_ledger(self):
        phone = "13800138005"
        self._new_user("u4@test.local", phone)
        self._add_plan(phone, "07:20:00")
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        data = c.get("/api/accounts").get_json()
        self.assertEqual(data["state_msgs"].get(mask_phone(phone)), "计划 07:20",
                         "管理员面 state_msgs 必须由台账 run_at 生成")

    # ---- ③ 自选（pinned）账号显示当前台账值 ----
    def test_pinned_account_shows_ledger_value(self):
        """自选片只影响计划落点，不改变显示来源：pinned 账号仍按台账 run_at 显示。"""
        phone = "13800138009"
        self._new_user("u8@test.local", phone)
        self._add_plan(phone, "07:20:00")
        db.set_time_pref(phone, 0, clock.now().strftime("%Y-%m-%d %H:%M:%S"))
        c = self.webapp.create_app().test_client()
        self._login(c, "u8@test.local", USER_PASS)
        data = c.get("/api/my-time-pref").get_json()
        self.assertEqual(data["estimated"], "07:20", "pinned 账号仍按台账 run_at 显示")
        rows = c.get("/api/my-accounts").get_json()["accounts"]
        target = next(r for r in rows if r["phone"] == phone)
        self.assertEqual(target["state_message"], "计划 07:20")

    # ---- ③ 重排改写 run_at 后显示当前台账值（经真实 requeue_task 改写） ----
    def test_requeued_run_at_is_displayed(self):
        """经生产重排路径 `queue_store.requeue_task` 改写 run_at 后，显示必须跟到新值。

        先落初始计划 06:31，再走 `requeue_task` 把 run_at 改写成重试时刻 06:55:10；
        显示须为 06:55——证明显示读的是台账当前值，不是初始计划。
        """
        phone = "13800138006"
        self._new_user("u5@test.local", phone)
        self._add_plan(phone, "06:31:00")
        changed = queue_store.requeue_task(
            phone, clock.today(), f"{clock.today()} 06:55:10")
        self.assertEqual(changed, 1, "requeue_task 必须改写 1 行（夹具或谓词失效）")
        c = self.webapp.create_app().test_client()
        self._login(c, "u5@test.local", USER_PASS)
        rows = c.get("/api/my-accounts").get_json()["accounts"]
        target = next(r for r in rows if r["phone"] == phone)
        self.assertEqual(target["state_message"], "计划 06:55",
                         "重排后的 run_at 必须如实显示为当前台账值")
        data = c.get("/api/my-time-pref").get_json()
        shown = _parse_clock(data.get("estimated"))
        self.assertIsNotNone(shown, f"estimated 必须给出钟点：{data.get('estimated')!r}")
        want = datetime.strptime(f"{clock.today()} 06:55:10", "%Y-%m-%d %H:%M:%S")
        self.assertLess(_diff_sec(shown, want), SLICE_SEC)

    # ---- 跨端点等值：同一账号同一输入，两端点状态文案必须相等 ----
    def test_cross_endpoint_state_message_agrees(self):
        """`/api/my-accounts` 与 `/api/accounts` 的状态文案对同一账号必须相等。

        两条端点共用 `accounts_data.plan_state_message`。本断言钉住"谓词只此一处"：
        任一端谓词改歪，三条支路里必有一条不等。（自证见交付报告：改歪一侧 ⇒ 红。）
        """
        planned, noplan, concluded = "13800138010", "13800138011", "13800138012"
        for phone in (planned, noplan, concluded):
            db.add_account({
                "name": f"账号{phone}", "phone": phone, "password": "p3",
                "phone_model": "", "phone_code": "", "owner": "admin",
                "status": "active", "reject_reason": "",
            })
        self._add_plan(planned, "07:20:00")
        self._add_plan(concluded, "06:31:00")
        state_path = os.path.join(self.tmp, f"sign-state-{clock.today()}.json")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({concluded: {"status": "failed", "message": "签到失败",
                                   "time": "06:40:00"}}, f)
        c_admin = self.webapp.create_app().test_client()
        self._login(c_admin, "admin", ADMIN_PASS)
        mine = {r["phone"]: r["state_message"]
                for r in c_admin.get("/api/my-accounts").get_json()["accounts"]}
        admin_msgs = c_admin.get("/api/accounts").get_json()["state_msgs"]
        for phone in (planned, noplan, concluded):
            self.assertEqual(mine.get(phone), admin_msgs.get(mask_phone(phone)),
                             f"{phone} 两端点状态文案必须相等")
        self.assertEqual(mine[planned], "计划 07:20")
        self.assertEqual(mine[noplan], "待生成")
        self.assertEqual(mine[concluded], "签到失败", "有结论账号保留结论文案")

    # ---- None 哨兵路线级守卫：读不通时两端点均落「待生成」 ----
    def test_none_sentinel_route_guards(self):
        """桩 `db.task_run_at_by_phone=None`：两端点均落「待生成」，不得空/异常。"""
        phone = "13800138013"
        self._new_user("u11@test.local", phone)
        with mock.patch.object(self.webapp.db, "task_run_at_by_phone", return_value=None):
            c_user = self.webapp.create_app().test_client()
            self._login(c_user, "u11@test.local", USER_PASS)
            rows = c_user.get("/api/my-accounts").get_json()["accounts"]
            target = next(r for r in rows if r["phone"] == phone)
            self.assertEqual(target["state_message"], "待生成",
                             "读不通时 /api/my-accounts 必须落「待生成」")
            self.assertIsNone(_parse_clock(target["state_message"]))
            c_admin = self.webapp.create_app().test_client()
            self._login(c_admin, "admin", ADMIN_PASS)
            msgs = c_admin.get("/api/accounts").get_json()["state_msgs"]
            self.assertEqual(msgs.get(mask_phone(phone)), "待生成",
                             "读不通时 /api/accounts 必须落「待生成」")
            self.assertIsNone(_parse_clock(msgs.get(mask_phone(phone))))

    # ---- ② 无计划行：显示「待生成」，不回退任何钟点 ----
    def test_no_plan_row_shows_pending_generation(self):
        phone = "13800138007"
        self._new_user("u6@test.local", phone)
        # 不写台账行（当日计划尚未生成）
        c = self.webapp.create_app().test_client()
        self._login(c, "u6@test.local", USER_PASS)
        data = c.get("/api/my-time-pref").get_json()
        combined = f"{data.get('estimated') or ''}{data.get('estimate_note') or ''}"
        self.assertIsNone(_parse_clock(combined),
                          f"无计划行不得显示任何钟点：{combined!r}")
        self.assertIn("待生成", combined, "无计划行必须显示「待生成」")
        rows = c.get("/api/my-accounts").get_json()["accounts"]
        target = next(r for r in rows if r["phone"] == phone)
        self.assertEqual(target["state_message"], "待生成",
                         "无计划行时 state_message 必须是「待生成」而非钟点")
        self.assertIsNone(_parse_clock(target["state_message"]))

    def test_no_plan_row_admin_state_msgs_shows_pending_generation(self):
        phone = "13800138008"
        self._new_user("u7@test.local", phone)
        c = self.webapp.create_app().test_client()
        self._login(c, "admin", ADMIN_PASS)
        data = c.get("/api/accounts").get_json()
        self.assertEqual(data["state_msgs"].get(mask_phone(phone)), "待生成",
                         "管理员面无计划行时必须是「待生成」")


if __name__ == "__main__":
    unittest.main()
