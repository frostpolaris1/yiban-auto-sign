# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`__clear__` 哨兵折算为清空语义：4 个消费点统一经 `fold_phone_code`，进 SET 不再 pop。

背景：哨兵原在进 SET 前被 `pop` 掉 ⇒ "清除设备识别码"是静默空操作而接口回 200，
用户看到"已保存"、库里值原封不动；且各消费点自行解读协议令牌，添加路径还可能把
`__clear__` 当字面量送进易班验证或落库。现在管理端编辑 / 用户端编辑 / 两条添加路径
统一折算——哨兵 ⇒ `""` 随 UPDATE 进 SET，库行真被清空；正常值与留空（保持不变）
路径零回归。

标签：E · Web：认证/权限/API
覆盖：fold_phone_code 折算纯函数（哨兵/留空/正常值/添加口无旧值）、管理端与用户端
    哨兵清空的库行断言与返回如实、两条添加路径哨兵不落字面量、正常值改写与留空
    保持的零回归、grep 级"进 SET 前不再 pop、折算单点接入四个消费点"
对应实现：`web/services/accounts_data.py` 的 `CLEAR_SENTINEL`/`fold_phone_code`、
    `web/routes/accounts_api.py` 的 api_account_update/api_account_add、
    `web/routes/my.py` 的 api_my_account_update/api_my_account_add
关键断言：`__clear__` ⇒ 库行 `phone_code` 变 `""` 且响应 `has_phone_code` 如实为假；
    哨兵绝不作为字面量落库或送去验证；留空=保持旧值；`pop("phone_code")` 全仓为零、
    `"__clear__"` 字面量在 py 侧只有一处真源、路由内 `fold_phone_code(` 恰 4 处
依赖：纯本地 Flask test client + 临时 `.env`/SQLite，不联网、不访问真实易班接口；无需
    node。每个用例新建 app——高危额度与门禁计数是 create_app 工厂局部状态。
用法（项目根目录）：
    python -m pytest tests/test_creds_write_accounting.py -v
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "UserPass123!"
PHONE = "13800000000"
PHONE2 = "13900000001"
OWNER = "u1@test.local"
CODE = "code-9988"
NEW_CODE = "code-new-1"
SENTINEL = "__clear__"


def _load_webapp():
    """独立名字加载 web/app.py（共用模块对象会读到别的测试的 .env 常量快照）。"""
    spec = importlib.util.spec_from_file_location(
        "webapp_credsw", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_credsw"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class _CredsBase(unittest.TestCase):
    """临时 .env/DB + 档位（TIER，None=缺省 risk）；告警出口打桩防外发。"""

    TIER = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-credsw-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_ACCOUNTS_FILE": cls.accounts_file,
            "YIBAN_USERS_FILE": cls.users_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
            "YIBAN_DISABLE_PURGE_LOOP": "1",
        })
        cls.webapp = _load_webapp()

    @classmethod
    def tearDownClass(cls):
        import db
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE", "YIBAN_DISABLE_PURGE_LOOP"):
            os.environ.pop(k, None)

    def setUp(self):
        self._write_env()
        self._reset_db()
        self.alerts = []
        p1 = mock.patch.object(
            self.webapp, "send_notification",
            side_effect=lambda t, c, urgent=False, force=False, ledger=None:
                self.alerts.append((t, c, urgent)),
        )
        p1.start()
        self.addCleanup(p1.stop)

    def _write_env(self):
        lines = [
            f"YIBAN_ACCOUNTS_KEY={TEST_KEY}",
            "YIBAN_ADMIN_USER=admin",
            f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}",
            "YIBAN_MAIL_ADMIN_TO=admin@test.local",
        ]
        if self.TIER:
            lines.append(f"YIBAN_PW_GATE={self.TIER}")
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.webapp.migrate_admin_password_to_hash(self.env_file)

    def _reset_db(self):
        import db
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with io.open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c, c.get("/api/me").get_json()["csrf_token"]

    def _seed_account(self, phone=PHONE, code=CODE, owner=OWNER):
        import db
        db.add_account({"name": "A", "phone": phone, "password": "pw-1",
                        "phone_model": "", "phone_code": code,
                        "owner": owner, "status": "active"})
        return db.load_accounts()[0]

    def _row(self, phone=PHONE):
        import db
        rows = [a for a in db.load_accounts() if a["phone"] == phone]
        self.assertEqual(len(rows), 1, f"应恰有一行 {phone}，实际 {len(rows)}")
        return rows[0]


class FoldPhoneCodeTest(unittest.TestCase):
    """折算纯函数：哨兵 ⇒ ""（键保留随 UPDATE 进 SET）、留空 ⇒ 保旧、正常值原样。"""

    def setUp(self):
        import web.services.accounts_data as ad
        self.ad = ad

    def test_哨兵折算成空串且键仍在(self):
        clean = {"phone_code": self.ad.CLEAR_SENTINEL}
        self.assertEqual(self.ad.fold_phone_code(clean, "OLD"), "")
        self.assertIn("phone_code", clean, "键被摘掉 = 不进 SET = 静默空操作")

    def test_留空回填旧值(self):
        clean = {"phone_code": ""}
        self.assertEqual(self.ad.fold_phone_code(clean, "OLD"), "OLD")

    def test_正常值原样保留(self):
        clean = {"phone_code": "NEW"}
        self.assertEqual(self.ad.fold_phone_code(clean, "OLD"), "NEW")

    def test_添加口无旧值可保哨兵同样折空(self):
        clean = {"phone_code": self.ad.CLEAR_SENTINEL}
        self.assertEqual(self.ad.fold_phone_code(clean), "")
        clean2 = {"phone_code": ""}
        self.assertEqual(self.ad.fold_phone_code(clean2), "")


class AdminSentinelClearTest(_CredsBase):
    """管理端 `__clear__`：库行真清空、返回如实（off 档专测清空机制本身）。"""

    TIER = "off"

    def test_哨兵清空库行真变空且返回如实(self):
        self._seed_account()
        c, t = self._admin_client()
        r = c.put("/api/accounts/0",
                  json={"name": "A", "phone": PHONE, "phone_code": SENTINEL},
                  headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._row()["phone_code"], "", "哨兵必须真清掉库行")
        self.assertFalse(r.get_json()["accounts"][0]["has_phone_code"],
                         "返回的列表要如实反映已清空，不得以 200 掩盖空操作")

    def test_正常值改写与留空保持不变零回归(self):
        self._seed_account(code="base")
        c, t = self._admin_client()
        r = c.put("/api/accounts/0",
                  json={"name": "A", "phone": PHONE, "phone_code": NEW_CODE},
                  headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._row()["phone_code"], NEW_CODE)
        r = c.put("/api/accounts/0",
                  json={"name": "A2", "phone": PHONE},
                  headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._row()["phone_code"], NEW_CODE, "留空=保持不变不得被清空")
        self.assertEqual(self._row()["name"], "A2")

    def test_对已为空的字段发哨兵结果语义仍是空(self):
        self._seed_account(code="")
        c, t = self._admin_client()
        r = c.put("/api/accounts/0",
                  json={"name": "A", "phone": PHONE, "phone_code": SENTINEL},
                  headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._row()["phone_code"], "")


class UserSideClearTest(_CredsBase):
    """用户端编辑与提交口：同一折算，哨兵清空是真、哨兵永不落字面量。"""

    def _user_client(self, email=OWNER):
        import db
        if db.find_user(email) is None:
            db.create_user(email, self.webapp.generate_password_hash(USER_PASS))
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": email, "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c, c.get("/api/me").get_json()["csrf_token"]

    def test_用户侧哨兵真清空且返回如实(self):
        self._seed_account()
        c, t = self._user_client()
        mine = c.get("/api/my-accounts").get_json()["accounts"]
        self.assertEqual(len(mine), 1)
        r = c.put(f"/api/my-accounts/{mine[0]['index']}",
                  json={"name": "n", "phone": PHONE, "phone_code": SENTINEL},
                  headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["msg"], "已保存")
        self.assertEqual(self._row()["phone_code"], "", "清除必须是真清空，不是空操作")

    def test_用户提交哨兵不落字面量(self):
        c, t = self._user_client()
        r = c.post("/api/my-accounts",
                   json={"name": "n", "phone": PHONE, "password": "pw",
                         "phone_code": SENTINEL},
                   headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._row()["phone_code"], "",
                         "协议令牌绝不允许当设备码落库")

    def test_管理添加口哨兵不落字面量(self):
        c, t = self._admin_client()
        r = c.post("/api/accounts",
                   json={"name": "A", "phone": PHONE2, "password": "pw",
                         "phone_code": SENTINEL},
                   headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._row(PHONE2)["phone_code"], "")


class SentinelSingleSourceTest(unittest.TestCase):
    """grep 级钉：哨兵折算只有一个真源，路由不再自行 pop/比较。"""

    def _sources(self):
        for root in ("web", "yiban", "scripts"):
            for dirpath, _dirs, files in os.walk(os.path.join(BASE, root)):
                if "static" in dirpath or "__pycache__" in dirpath:
                    continue
                for fn in files:
                    if fn.endswith(".py"):
                        yield os.path.join(dirpath, fn)

    def test_进SET前不再pop识别码(self):
        hits = [p for p in self._sources()
                if 'pop("phone_code"' in io.open(p, encoding="utf-8").read()]
        self.assertEqual(hits, [], "哨兵一旦在进 SET 前被摘掉，清除就退化为静默空操作")

    def test_哨兵字面量只在账号数据族一处定义(self):
        hits = [p for p in self._sources()
                if '"__clear__"' in io.open(p, encoding="utf-8").read()]
        self.assertEqual([os.path.basename(p) for p in hits], ["accounts_data.py"],
                         "多处定义会漂移；前端 JS 字面量由折算单点在 Python 侧收敛语义")

    def test_四个消费点全部接入折算(self):
        route_dir = os.path.join(BASE, "web", "routes")
        n = 0
        for fn in os.listdir(route_dir):
            if fn.endswith(".py"):
                n += io.open(os.path.join(route_dir, fn), encoding="utf-8").read(
                    ).count("fold_phone_code(")
        self.assertEqual(n, 4, "管理端编辑/用户端编辑/两条添加路径各一处，缺一处即一处空操作")


if __name__ == "__main__":
    unittest.main(verbosity=2)
