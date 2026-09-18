# -*- coding: utf-8 -*-
"""只读面的留痕、限速与"额度勘察"字段分层。

威胁模型：被窃（或本身恶意）的管理员会话。写路径的二次鉴权与审计已经收口，
但读路径此前完全无痕——拿着管理员 Cookie 把整库用户与账号明文凭据拖走，审计表里
一个字都不会多；`idx` 从 0 递增还能无限枚举 `/api/accounts/<idx>/detail`。

本轮口径：
- 读审计按 **(actor, 资源类, 窗口)** 聚合，不逐请求一行。刻意如此：本轮已在
  "被拒"那一侧实测过"被盗会话把拒绝当免费打字机刷审计表"，读侧若做成逐条，
  等于把同一个洞换个口子重开；
- 但首行必落（不能等窗口关闭再 flush，否则"就读这一次"永远没有痕迹）；
- `/api/accounts/<idx>/detail` 加会话级限速，超限 429 且**文案不自报内部阈值**；
- 429 本身也不逐条写审计（每窗口一行）；
- 推送配置的额度勘察字段（cooldown / 两本账 remaining / urgent_daily_max）仅主管理员
  可读，"通道开没开、配没配"必须仍对普通管理员可见。

全程 Flask test_client + 临时 .env/DB，无网络请求。
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

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_KEY = "a" * 64
TRACK_SALT = "read-audit-salt-0123456789"
ADMIN_PASS = "Master-Test-2026!"
SUB_PASS = "Subadmin-Test-2026!"
OWNER = "student@example.cn"
SUB_ADMIN = "sub.admin@example.cn"
# 刻意大于 DETAIL_MAX：整库枚举这条路要能在一个用例里走满
N_ACCOUNTS = 70


def _phone(i):
    return f"138{i:08d}"


def _masked(i):
    return f"138****{i:04d}"


def _owner(i):
    return f"user{i}@example.cn"


# 本轮引入的全部只读留痕动作名（"读面总行数"类断言用它，不靠 LIKE 转义）
_READ_ACTIONS = ("account_detail_read", "account_detail_denied", "users_list_read",
                 "users_deleted_read", "logs_read", "sign_events_read")


class ReadAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-read-audit-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                    "YIBAN_ADMIN_USER=admin\n"
                    f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        for k, v in {
            "YIBAN_ACCOUNTS_KEY": TEST_KEY, "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_ACCOUNTS_FILE": cls.accounts_file, "YIBAN_TRACK_SALT": TRACK_SALT,
            "YIBAN_USERS_FILE": os.path.join(cls.tmp, "users.json"),
            "YIBAN_DB_FILE": cls.db_file, "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
            "YIBAN_DISABLE_PURGE_LOOP": "1",
        }.items():
            os.environ[k] = v
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp_read_audit", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_read_audit"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE", "YIBAN_TRACK_SALT", "YIBAN_DISABLE_PURGE_LOOP"):
            os.environ.pop(k, None)

    def setUp(self):
        from werkzeug.security import generate_password_hash
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
        db.init_db(db_file=self.db_file, migrate_from=self.accounts_file,
                   env_file=self.env_file)
        db.create_user(OWNER, generate_password_hash("Stu-Test-2026!",
                       method=self.webapp.SCRYPT_METHOD),
                       role="user", created_at="2026-09-18 00:00:00", pw_version=1)
        db.create_user(SUB_ADMIN, generate_password_hash(SUB_PASS,
                       method=self.webapp.SCRYPT_METHOD),
                       role="admin", created_at="2026-09-18 00:00:00", pw_version=1)
        # 库里的账号数必须真的多过 DETAIL_MAX，"递增 idx 枚举整库会被挡住"这件事
        # 才是被演示出来的而不是被假设出来的。每用户限一个未删除账号（DB 层唯一
        # 约束），故一个账号配一个归属用户；这些 filler 用户永不登录，口令哈希
        # 用一轮 pbkdf2 即可（scrypt 每轮 ~100ms，70 个会把用例拖成摆设）。
        cheap = generate_password_hash("Filler-1234!", method="pbkdf2:sha256:1")
        for i in range(N_ACCOUNTS):
            db.create_user(_owner(i), cheap, role="user",
                           created_at="2026-09-18 00:00:00", pw_version=1)
            db.add_account({"name": f"号{i}", "phone": _phone(i), "password": "Pass1234!",
                            "phone_model": "", "phone_code": "", "owner": _owner(i),
                            "status": self.webapp.ACCOUNT_STATUS_ACTIVE})
        # 每个用例新建 app：限速与聚合计数都是 create_app 内的闭包字典，互不污染
        self.app = self.webapp.create_app()

    # ---- 助手 ----
    def _login(self, user, pw):
        c = self.app.test_client()
        r = c.post("/api/login", json={"username": user, "password": pw})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def _sub(self):
        return self._login(SUB_ADMIN, SUB_PASS)

    def _master(self):
        return self._login("admin", ADMIN_PASS)

    def _rows(self, *actions):
        conn = db.get_conn()
        marks = ",".join("?" * len(actions))
        return conn.execute(
            f"SELECT username, action, target, detail FROM audit_logs "
            f"WHERE action IN ({marks}) ORDER BY id", actions).fetchall()

    def _read_rows(self):
        return self._rows(*_READ_ACTIONS)

    # ---- 聚合判据本体（突变验证的靶子：改成逐条这里就红）----
    def test_row_due_is_aggregated_not_per_request(self):
        due = self.webapp._read_audit_row_due
        self.assertTrue(due(1), "首次读取必须留痕（不能等窗口关闭才写）")
        for cnt in range(2, 10):
            self.assertFalse(due(cnt), f"窗口内第 {cnt} 次不该各写一行")
        self.assertTrue(due(10) and due(50), "批量档位要补行，否则看不出读了多少")
        self.assertFalse(due(51))
        self.assertTrue(due(200) and due(400))
        self.assertFalse(due(201))

    # ---- ① 连读 50 个详情：有限几行，不是 50 行 ----
    def test_fifty_detail_reads_leave_a_few_rows_not_fifty(self):
        c = self._sub()
        for i in range(50):
            r = c.get(f"/api/accounts/{i}/detail")
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rows = self._rows("account_detail_read")
        self.assertEqual(len(rows), 3, f"聚合口径应为档位 1/10/50 三行，实得 {len(rows)}")
        self.assertNotEqual(len(rows), 50, "读审计绝不允许逐请求一行")
        self.assertTrue(all(r["username"] == SUB_ADMIN for r in rows),
                        "actor 必须是真实会话用户名")
        last = rows[-1]["detail"]
        self.assertIn("50", last, "末行要带窗口内累计次数，否则看不出被读了多少")
        self.assertIn(_masked(0), last, "detail 里应是被读目标的脱敏标识")
        self.assertNotIn(_phone(7), last, "审计里不得出现完整手机号")
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", rows[0]["target"]),
                        "target 走 db.hash_ip 口径（与 forbidden_path 同源）")

    # ---- ② 整库枚举式读：429，且文案不泄露阈值 ----
    def test_enumerating_every_detail_hits_429(self):
        c = self._sub()
        statuses = [c.get(f"/api/accounts/{i % N_ACCOUNTS}/detail").status_code
                    for i in range(N_ACCOUNTS)]
        self.assertIn(429, statuses, "idx 递增枚举整库必须被限速挡住")
        limit = self.webapp.DETAIL_MAX
        self.assertEqual(statuses[:limit], [200] * limit, "限额内不得误伤正常运维")
        self.assertEqual(statuses[limit], 429)
        body = c.get("/api/accounts/0/detail").get_json()["error"]
        self.assertFalse(re.search(r"\d", body), f"429 文案不该自报内部阈值数字：{body}")

    # ---- ④ 被拒的 429 不逐条写审计 ----
    def test_denied_detail_reads_do_not_write_one_row_each(self):
        c = self._sub()
        denied = 0
        for i in range(N_ACCOUNTS + 40):
            if c.get(f"/api/accounts/{i % N_ACCOUNTS}/detail").status_code == 429:
                denied += 1
        self.assertGreater(denied, 40, "本用例要真的打出一批 429")
        rows = self._rows("account_detail_denied")
        self.assertEqual(len(rows), 1,
                         f"拒绝面每窗口只许一行（实得 {len(rows)} 行 / {denied} 次被拒）")
        self.assertLess(len(self._read_rows()), 10,
                        "读面留下的总行数必须是有限几行，不能跟着请求数线性长")

    # ---- 其余四个只读接口同样按窗口聚合 ----
    def test_list_reads_aggregate_per_window(self):
        c = self._sub()
        for path, action in (("/api/users", "users_list_read"),
                             ("/api/users/deleted", "users_deleted_read"),
                             ("/api/logs", "logs_read"),
                             ("/api/admin/sign-events", "sign_events_read")):
            for _ in range(10):
                self.assertEqual(c.get(path).status_code, 200, path)
            rows = self._rows(action)
            self.assertEqual(len(rows), 2, f"{path} 应为档位 1/10 两行，实得 {len(rows)}")
            self.assertNotEqual(len(rows), 10, f"{path} 不得逐请求一行")
            self.assertIn("10", rows[-1]["detail"], f"{path} 末行要能看出累计次数")

    # ---- 详情面之外的读也不许把明文号带进审计 ----
    def test_read_audit_rows_never_carry_raw_phones(self):
        c = self._sub()
        for i in range(5):
            c.get(f"/api/accounts/{i}/detail")
        c.get("/api/admin/sign-events", query_string={"phone": _phone(3)})
        self.assertTrue(self._read_rows(), "读审计必须真的落了行，否则本用例是空跑")
        for row in self._read_rows():
            self.assertIsNone(re.search(r"1[3-9]\d{9}", row["detail"]),
                              f"读审计的 detail 泄漏了完整手机号：{row['detail']}")

    # ---- ③ 额度勘察字段仅主管理员；通道状态字段全体管理员可见 ----
    def test_quota_fields_only_for_builtin_admin(self):
        with open(self.env_file, "a", encoding="utf-8") as f:
            f.write("YIBAN_NOTIFY_COOLDOWN=77\n"
                    "YIBAN_NOTIFY_DAILY_MAX=9\n"
                    "YIBAN_NOTIFY_URGENT_DAILY_MAX=3\n")
        master = self._master().get("/api/notify-config").get_json()
        self.assertEqual(master["cooldown"], 77)
        self.assertEqual(master["urgent_daily_max"], 3)
        self.assertIsNotNone(master["daily_remaining"])
        self.assertIsNotNone(master["urgent_daily_remaining"])

        sub = self._sub().get("/api/notify-config").get_json()
        self.assertEqual(master["quota_visible"], True)
        self.assertEqual(sub["quota_visible"], False,
                         "余量的 null 原意是「不限」，无权查看必须靠这个标记区分")
        for key in self.webapp._NOTIFY_QUOTA_HIDDEN_KEYS:
            self.assertIn(key, sub, "刻意置 null 而非省键：响应形态必须稳定")
            self.assertIsNone(sub[key], f"{key} 属额度勘察字段，普通管理员不该看到")
        self.assertEqual(set(sub), set(master), "两套响应的键集合必须一致")
        # 日常运维要的信息一个字都不能少
        self.assertEqual(sub["daily_max"], master["daily_max"])
        self.assertEqual(sub["urgent_only"], master["urgent_only"])
        # 规则值（上限与节流）属"看得懂规则才能运维"，不是勘察面——两边同值
        self.assertEqual(sub["cooldown"], master["cooldown"])
        self.assertEqual(sub["urgent_daily_max"], master["urgent_daily_max"])
        for key in ("ok", "enabled", "type", "configured", "secret_masked"):
            self.assertIn(key, sub)
        self.assertEqual(sub["daily_max"], 9, "上限本身仍是配置读数，保持可见")

    # ---- 邮件侧：本就没有额度字段可分层，通道状态字段照旧可见 ----
    def test_mail_config_state_fields_visible_to_registered_admin(self):
        sub = self._sub().get("/api/mail-config").get_json()
        self.assertTrue(sub["ok"])
        for key in ("enabled", "admin_notify", "smtp_host", "smtp_port",
                    "user", "admin_to", "smtps"):
            self.assertIn(key, sub, "普通管理员必须看得到邮件通道开没开、配没配")
        self.assertFalse([k for k in sub if k in self.webapp._NOTIFY_QUOTA_HIDDEN_KEYS],
                         "邮件侧没有这两个余量键可分层（发送路径不受推送每日条数与节流约束）")


if __name__ == "__main__":
    unittest.main()
