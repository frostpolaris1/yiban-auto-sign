# -*- coding: utf-8 -*-
"""内置主管理员（.env 账号）的服务端会话吊销面。

被修的现实缺陷：`/api/logout` 只为 `auth_source == "user"` 轮换 sid，而
`_effective_role` 对内置主管理员**只比 `pw_version`、从不比 sid**——于是主管理员会话
没有任何服务端吊销面：被盗 Cookie 能用满 7 天绝对期，本人登出也踢不掉攻击者，唯一
手段是改口令（连带自己也要重登）或换 `YIBAN_SECRET_KEY`（全站重登）。

现口径（与 `users.sid` 逐字同构，包括存量兼容）：
- 登录成功为内置会话签发 `YIBAN_ADMIN_SID` 写 `.env`（与 `YIBAN_ADMIN_PW_VERSION`
  同口径：原子替换、0600、值来自 `secrets.token_hex` 故不可能含行分隔符）；
- `_effective_role` 对内置会话增加 sid 比对：`sid and session != sid` —— `.env` 里
  **没有该键 = 从未签发 = 不吊销**（升级日不强制重登，与注册用户的空串口径一致）；
- 登出 / 自助改密 / SSH 追回（明文与哈希不一致触发的重迁移）都轮换 sid；
- `.env` 不可写时**降级为"未签发"**：登录照常成功，只是暂时拿不到这条吊销面
  （与 `ensure_secret_key` / 口令迁移的降级策略一致），不静默把管理员锁在门外。

`.env` 写入频率极低：只在内置管理员**登录成功**与**登出**、以及改密/追回时写。

全程 mock / 纯本地（Flask test client），无任何网络请求。
用法（项目根目录）：
    python -m pytest tests/test_builtin_admin_sid.py -v
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
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_USER = "admin"
ADMIN_PASS = "TestPass1234!"
NEW_PASS = "RotatedPass9876!"
SID_KEY = "YIBAN_ADMIN_SID"
SID_RE = re.compile(r"^[0-9a-f]{32,64}$")


def _load_webapp():
    """**独立名字**加载 web/app.py：它在导入期把 ENV_FILE 等读成模块级常量，与别的
    测试文件共用同一模块对象会读到另一个 `.env`（单跑绿、全量红的老坑）。"""
    spec = importlib.util.spec_from_file_location(
        "webapp_admin_sid", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_admin_sid"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class _AdminSidBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-admin-sid-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER={ADMIN_USER}\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
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
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        global db
        import db
        cls.webapp = _load_webapp()

    @classmethod
    def tearDownClass(cls):
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
        # 每例从"口令 = ADMIN_PASS、未签发过 sid"的存量部署形态起步：
        # 改密/追回两类用例会留下新哈希与新 sid，不重置就会串味（登录直接 401）。
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                    f"YIBAN_ADMIN_USER={ADMIN_USER}\n"
                    f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n")
        # 明文→哈希与真实首启同源（verify_admin 对"只有明文"是 fail-closed 拒绝）；
        # 这条路径刻意不轮换 sid（口令本身没变），签发留给登录。
        self.webapp.migrate_admin_password_to_hash(self.env_file)
        self.webapp.write_env_batch(self.env_file, {SID_KEY: ""})
        patcher = mock.patch.object(self.webapp, "send_notification")
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---- 工具 ----
    def _env(self):
        return self.webapp.read_env(self.env_file)

    def _env_lines(self, key):
        with open(self.env_file, encoding="utf-8-sig") as f:
            return [ln for ln in f.read().splitlines() if ln.strip().startswith(f"{key}=")]

    def _login(self, app=None, password=None):
        app = app or self.webapp.create_app()
        c = app.test_client()
        r = c.post("/api/login", json={
            "username": ADMIN_USER, "password": password or ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def _client_with_cookie(self, name, val):
        c = self.webapp.create_app().test_client()
        for kwargs in (
            {"key": name, "value": val, "domain": "localhost", "path": "/"},
            {"server_name": "localhost", "key": name, "value": val, "path": "/"},
        ):
            try:
                c.set_cookie(**kwargs)
                return c
            except TypeError:
                continue
        self.fail("当前 Werkzeug 版本无法注入测试 cookie")

    def _session_cookie(self, c):
        for header in (c.get("/api/me").headers.getlist("Set-Cookie") or []):
            name_val = header.split(";", 1)[0]
            name, _, val = name_val.partition("=")
            if name.strip() and val:
                return name.strip(), val
        self.fail("未找到会话 cookie")


class IssueOnLoginTest(_AdminSidBase):
    """登录成功签发/轮换 sid。"""

    def test_login_writes_single_valid_sid_line(self):
        c = self._login()
        sid = self._env().get(SID_KEY, "")
        self.assertTrue(sid, "登录后 .env 应出现 YIBAN_ADMIN_SID")
        self.assertRegex(sid, SID_RE, "sid 只可能是 token_hex 输出（不含行分隔符）")
        self.assertEqual(len(self._env_lines(SID_KEY)), 1, "不得留下重复键影子行")
        self.assertEqual(c.get("/api/me").status_code, 200, "会话本身照常可用")

    def test_each_login_rotates_the_sid(self):
        """后一次登录换发新 sid（与注册用户"单会话有效"同口径）。"""
        self._login()
        first = self._env().get(SID_KEY)
        c2 = self._login()
        second = self._env().get(SID_KEY)
        self.assertNotEqual(first, second, "登录必须轮换")
        self.assertEqual(c2.get("/api/me").status_code, 200)

    def test_sid_rotation_touches_nothing_else(self):
        """轮换只动这一个键：口令哈希/版本/其他配置不得被顺手改写。

        先做一次登录把启动期写入（明文迁移成哈希、补 YIBAN_SECRET_KEY）吃掉，
        再比较第二次登录前后的快照。
        """
        self._login()
        before = dict(self._env())
        self._login()
        after = dict(self._env())
        self.assertEqual(
            {k for k, v in after.items() if before.get(k) != v}, {SID_KEY},
            "除 sid 外不得有任何变化")


class RevocationTest(_AdminSidBase):
    """`_effective_role` 对内置会话增加 sid 比对（④）。"""

    def test_logout_kills_stolen_cookie(self):
        """④主管理员登出后，攻击者手上那份旧 Cookie 立即失效。"""
        c = self._login()
        name, val = self._session_cookie(c)
        self.assertEqual(self._client_with_cookie(name, val).get("/api/me").status_code,
                         200, "前置：被窃副本此刻还有效")
        token = c.get("/api/me").get_json()["csrf_token"]
        r = c.post("/api/logout", headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        stolen = self._client_with_cookie(name, val)
        self.assertEqual(stolen.get("/api/me").status_code, 401,
                         "登出必须吊销内置主管理员的旧会话（原来毫无吊销面）")
        self.assertEqual(
            stolen.post("/api/settings", json={"sunday_sign": 1},
                        headers={"X-CSRF-Token": token}).status_code,
            401, "被吊销的会话不得还能写配置")

    def test_owner_session_dead_after_logout_too(self):
        """登出对本人会话同样有效（sid 轮换 + session.clear 双重）。"""
        c = self._login()
        token = c.get("/api/me").get_json()["csrf_token"]
        self.assertEqual(c.post("/api/logout",
                               headers={"X-CSRF-Token": token}).status_code, 200)
        self.assertEqual(c.get("/api/me").status_code, 401)

    def test_password_change_rotates_sid(self):
        c = self._login()
        name, val = self._session_cookie(c)
        before = self._env()[SID_KEY]
        token = c.get("/api/me").get_json()["csrf_token"]
        r = c.post("/api/me/password", json={
            "old_password": ADMIN_PASS, "new_password": NEW_PASS,
            "confirm_password": NEW_PASS}, headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertNotEqual(self._env()[SID_KEY], before, "改密须轮换 sid")
        self.assertEqual(self._client_with_cookie(name, val).get("/api/me").status_code,
                         401, "改密后旧 Cookie 失效")

    def test_ssh_recovery_rotation_revokes(self):
        """SSH 追回路径（运维手改明文 → 启动时重迁移 + 递增 PW_VERSION）一并轮换 sid。"""
        c = self._login()
        name, val = self._session_cookie(c)
        before = self._env()[SID_KEY]
        self.assertEqual(self._client_with_cookie(name, val).get("/api/me").status_code,
                         200, "前置：被窃副本此刻还有效")
        # 模拟运维在 SSH 里重设明文口令（哈希仍是旧的）；sid 那行原样留着
        self.webapp.write_env_batch(self.env_file,
                                    {"YIBAN_ADMIN_PASSWORD": "RecoveredPass5555!"})
        self.webapp.migrate_admin_password_to_hash(self.env_file)
        self.assertNotEqual(self._env()[SID_KEY], before, "追回必须换发新 sid")
        self.assertEqual(self._client_with_cookie(name, val).get("/api/me").status_code,
                         401)

    def test_mismatched_sid_in_env_revokes_without_relogin(self):
        """手工把 .env 的 sid 改掉 = 立即吊销在场内置会话（应急可用，不必改口令）。"""
        c = self._login()
        self.assertEqual(c.get("/api/me").status_code, 200)
        token = c.get("/api/me").get_json()["csrf_token"]
        self.webapp.write_env_key(self.env_file, SID_KEY, "deadbeef" * 6)
        self.assertEqual(c.get("/api/me").status_code, 401,
                         "sid 不匹配的内置会话必须视为未登录")
        r = c.post("/api/settings", json={"sunday_sign": 1},
                   headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 401)


class LegacyCompatTest(_AdminSidBase):
    """存量兼容（⑤）：`.env` 没有该键 = 从未签发 = 不吊销。"""

    def test_unissued_sid_does_not_revoke(self):
        c = self._login()
        name, val = self._session_cookie(c)
        # 抹掉该键 = 回到"升级日之前的存量部署从来没签发过 sid"的形态
        self.webapp.write_env_key(self.env_file, SID_KEY, "")
        self.assertNotIn(SID_KEY, self._env())
        stolen = self._client_with_cookie(name, val)
        self.assertEqual(stolen.get("/api/me").status_code, 200,
                         "未签发（键不存在）时不得吊销，否则升级日全体强制重登")

    def test_unwritable_env_degrades_without_breaking_login(self):
        """.env 写不进去时登录照常成功（只是暂时签发不出 sid）。"""
        with mock.patch.object(self.webapp, "write_env_key",
                               side_effect=OSError("Read-only file system")):
            c = self._login()
        self.assertEqual(c.get("/api/me").status_code, 200,
                         "签发失败绝不能把管理员锁在登录门外")
        self.assertNotIn(SID_KEY, self._env(), "写不成就不该留下半个 sid")

    def test_failed_reissue_keeps_the_stored_sid_matchable(self):
        """已签发过 sid、之后 .env 变只读时的换发失败：会话必须沿用**库里现存值**。

        若失败时返回那个没写进去的新值，会话里的 sid 与 .env 里的旧值必然不匹配
        ——管理员刚登录成功就被自己的凭据吊销，等于把一次磁盘故障放大成锁死。
        """
        c0 = self._login()
        stored = self._env()[SID_KEY]
        self.assertEqual(c0.get("/api/me").status_code, 200)
        with mock.patch.object(self.webapp, "write_env_key",
                               side_effect=OSError("Read-only file system")):
            c1 = self._login()
        self.assertEqual(stored, self._env()[SID_KEY], "写失败时 .env 必须原样未动")
        self.assertEqual(c1.get("/api/me").status_code, 200,
                         "换发失败的这次登录不得把自己吊销掉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
