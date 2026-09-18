# -*- coding: utf-8 -*-
"""全站公告「双人发布」的后端行为契约。

被修的现实缺陷：`YIBAN_ANNOUNCEMENT` 会显示在**全体学生**的页面顶横幅与登录页，
而改动前任意管理员（注册管理员的凭据泄露面远大于主管理员）一次 `PUT` 就能直接换掉它
——无口令、无第二人参与，等于内部人或被盗会话可以把伪造通知包装成官方公告发出去。

双人发布后的口径（本文件逐条钉住）：

1. `PUT /api/announcement` 只写**草稿**（`YIBAN_ANNOUNCEMENT_DRAFT` + 作者/时间元数据），
   现有限制（200 字、无行分隔符）逐条保留；**已发布键与公告缓存一律不碰**。
2. `GET /api/announcement` 的公开形态一字不动：匿名与普通用户仍是 `ok` + `text` 两键；
   只有管理员会话额外看到 `draft` / `draft_by` / `draft_at`，元数据被手改坏时按
   「元数据不可用」处理而非让 GET 抛错。
3. 发布是独立动作 `POST /api/announcement/publish`：仅主管理员 + **当次口令**
   （短时豁免不适用），把「正式=草稿、草稿清空」压进**一次** `write_env_batch`，
   成功后审计 + 紧急告警（含发布人、内容前 80 字、差异是新发布还是覆盖）。
4. 无草稿时发布给 400，不得静默成功。

全程 mock / 纯本地（Flask test client），无任何网络请求。
用法（项目根目录）：
    python -m pytest tests/test_announcement_publish_94.py -v
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
ADMIN_PASS = "TestPass1234!"
REG_ADMIN = "reg-admin@test.local"
REG_PASS = "RegPass5678!"
USER_EMAIL = "stud@test.local"
USER_PASS = "StudPass123!"

DRAFT_RE = re.compile(r"^[^|\n]{1,64}\|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class _AnnBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-ann-")
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
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        global db
        import db
        # 独立名字加载 web/app.py：它在导入期把 ENV_FILE 等读成模块级常量，与别的测试
        # 文件共用同一模块对象会读到另一个 .env（单跑绿、全量红的老坑）
        spec = importlib.util.spec_from_file_location(
            "webapp_ann_publish", os.path.join(BASE, "web", "app.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["webapp_ann_publish"] = mod
        with contextlib.suppress(Exception):
            spec.loader.exec_module(mod)
        cls.webapp = mod

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
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        self.webapp.ENV_FILE = self.env_file
        # 公告缓存是模块级全局：不清零会把上一例的已发布文本带进本例
        self.webapp._announcement_cache[0] = None
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        self.alerts = []
        patcher = mock.patch.object(
            self.webapp, "send_notification",
            side_effect=lambda t, c, urgent=False, force=False, ledger=None:
            self.alerts.append((t, c, urgent, force)))
        patcher.start()
        self.addCleanup(patcher.stop)
        # .env 每一次原子落盘的全文快照（判定"发布是否一次写完"用）
        self.writes = []
        real_atomic = self.webapp._atomic_write
        wp = mock.patch.object(
            self.webapp, "_atomic_write",
            side_effect=lambda path, text, **kw: (
                self.writes.append(text), real_atomic(path, text, **kw))[0])
        wp.start()
        self.addCleanup(wp.stop)

    # ---- 会话与探针 ----
    def _login(self, username, password):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        c.csrf = c.get("/api/me").get_json()["csrf_token"]
        return c

    def _master(self):
        return self._login("admin", ADMIN_PASS)

    def _reg_admin(self):
        db.create_user(REG_ADMIN, self.webapp.generate_password_hash(REG_PASS), role="admin")
        return self._login(REG_ADMIN, REG_PASS)

    def _user(self):
        db.create_user(USER_EMAIL, self.webapp.generate_password_hash(USER_PASS))
        return self._login(USER_EMAIL, USER_PASS)

    def _anon(self):
        return self.webapp.create_app().test_client()

    def _hdr(self, c):
        return {"X-CSRF-Token": c.csrf}

    def _put_draft(self, c, text):
        return c.put("/api/announcement", json={"text": text}, headers=self._hdr(c))

    def _publish(self, c, password=ADMIN_PASS):
        body = {"confirm_password": password} if password is not None else {}
        return c.post("/api/announcement/publish", json=body, headers=self._hdr(c))

    def _env(self):
        return self.webapp.read_env(self.env_file)

    def _raw_env(self):
        with open(self.env_file, encoding="utf-8-sig") as f:
            return f.read()

    def _audit(self, action):
        return [dict(r) for r in db.get_conn().execute(
            "SELECT username, action, target, detail FROM audit_logs WHERE action=?",
            (action,)).fetchall()]


class DraftWriteTest(_AnnBase):
    """PUT = 写草稿：管理员权限与既有校验不变，已发布面一分不动。"""

    def test_put_writes_draft_not_published(self):
        c = self._reg_admin()
        r = self._put_draft(c, "服务器今晚 23:00 维护")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["ok"])
        env = self._env()
        self.assertEqual(env.get("YIBAN_ANNOUNCEMENT_DRAFT"), "服务器今晚 23:00 维护")
        self.assertNotIn("YIBAN_ANNOUNCEMENT", env)
        self.assertRegex(env.get("YIBAN_ANNOUNCEMENT_DRAFT_META", ""), DRAFT_RE)

    def test_put_keeps_published_text_and_cache_untouched(self):
        m = self._master()
        self._put_draft(m, "旧公告")
        self.assertEqual(self._publish(m).status_code, 200)
        published_cache_before = self.webapp._announcement_cache[0]
        sub = self._reg_admin()
        self.assertEqual(self._put_draft(sub, "新公告").status_code, 200)
        # 已发布键、公告缓存、匿名可见文本三者都必须仍是旧公告
        self.assertEqual(self._env().get("YIBAN_ANNOUNCEMENT"), "旧公告")
        self.assertEqual(self.webapp._announcement_cache[0], published_cache_before)
        self.assertEqual(self.webapp._announcement_cache[0], "旧公告")
        self.assertEqual(self._anon().get("/api/announcement").get_json()["text"], "旧公告")

    def test_put_validation_still_rejects_too_long_and_breaks(self):
        c = self._reg_admin()
        r = self._put_draft(c, "长" * 201)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("过长", r.get_json()["error"])
        for ch in ("\n", "\r", "\v", "\x1c", "\x85", "\u2028", "\u2029"):
            with self.subTest(ch=hex(ord(ch))):
                r = self._put_draft(c, f"正常{ch}YIBAN_ADMIN_PASSWORD_HASH=scrypt:fake")
                self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
                err = r.get_json()["error"]
                self.assertIn("行分隔符", err, f"400 须来自行分隔符守卫：{err}")
                self.assertNotIn("过长", err)
        self.assertNotIn("YIBAN_ANNOUNCEMENT_DRAFT", self._raw_env())
        self.assertNotIn("scrypt:fake", self._raw_env())
        self.assertEqual(self._raw_env().count("YIBAN_ADMIN_PASSWORD_HASH="), 1)

    def test_put_clears_draft_and_meta_together(self):
        c = self._reg_admin()
        self._put_draft(c, "待发布内容")
        self.assertEqual(self._put_draft(c, "").status_code, 200)
        self.assertNotIn("YIBAN_ANNOUNCEMENT_DRAFT", self._raw_env())
        self.assertNotIn("YIBAN_ANNOUNCEMENT_DRAFT_META", self._raw_env())

    def test_put_alert_says_pending_and_is_not_urgent(self):
        c = self._reg_admin()
        self._put_draft(c, "维护通知")
        self.assertEqual(len(self.alerts), 1, f"草稿写入应恰好一条告警，实际 {self.alerts}")
        title, body, urgent, force = self.alerts[0]
        self.assertEqual(title, "公告变更告警")
        self.assertFalse(urgent, "草稿不是对外可见状态，不得占用紧急账")
        self.assertFalse(force)
        self.assertIn("公告草稿已更新", body)
        self.assertIn("待主管理员发布", body)
        self.assertIn(REG_ADMIN, body)

    def test_draft_audit_marks_pending(self):
        c = self._reg_admin()
        self._put_draft(c, "维护通知")
        rows = self._audit("announcement_draft_save")
        self.assertEqual(len(rows), 1, f"草稿写入须留一条审计，实际 {self._audit('announcement')}")
        self.assertEqual(rows[0]["username"], REG_ADMIN)
        self.assertIn("待发布", rows[0]["detail"])
        self.assertIn("维护通知", rows[0]["detail"])
        self.assertEqual(self._audit("announcement_publish"), [], "写草稿不得记成发布")


class GetShapeTest(_AnnBase):
    """GET 的公开形态是契约；草稿只对管理员会话可见。"""

    def test_anonymous_response_has_only_ok_and_text(self):
        self._put_draft(self._reg_admin(), "草稿内容")   # 最坏情形：确有草稿待发布
        cases = {"匿名": self._anon(), "普通用户": self._user()}
        for label, c in cases.items():
            with self.subTest(label):
                body = c.get("/api/announcement").get_json()
                self.assertEqual(set(body), {"ok", "text"},
                                 f"{label}侧响应键集合必须不变（草稿键不得外泄）")
                self.assertIs(body["ok"], True)
                self.assertEqual(body["text"], "")

    def test_admin_get_sees_draft_keys(self):
        sub = self._reg_admin()
        self._put_draft(sub, "草稿内容")
        m = self._master()
        body = m.get("/api/announcement", headers=self._hdr(m)).get_json()
        self.assertEqual(set(body), {"ok", "text", "draft", "draft_by", "draft_at"})
        self.assertEqual(body["text"], "")
        self.assertEqual(body["draft"], "草稿内容")
        self.assertEqual(body["draft_by"], REG_ADMIN)
        self.assertRegex(body["draft_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_draft_never_leaks_into_public_text_even_when_published_is_empty(self):
        self._put_draft(self._reg_admin(), "只有草稿")
        self.assertEqual(self._anon().get("/api/announcement").get_json()["text"], "")

    def test_broken_meta_degrades_to_unavailable(self):
        c = self._reg_admin()
        self._put_draft(c, "草稿内容")
        for bad in ("", "no-pipe", "a|b|c", "x@y.local|2026-13-45 99:99:99",
                    "x@y.local|昨天", "\x00|x"):
            with self.subTest(bad=bad):
                self.webapp.write_env_batch(
                    self.env_file, {"YIBAN_ANNOUNCEMENT_DRAFT_META": bad})
                r = c.get("/api/announcement", headers=self._hdr(c))
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
                body = r.get_json()
                self.assertEqual(body["draft"], "草稿内容", "草稿正文与元数据解析互不牵连")
                self.assertEqual(body["draft_by"], "")
                self.assertEqual(body["draft_at"], "")


class PublishAuthorizationTest(_AnnBase):
    """发布面：仅主管理员 + 当次口令。"""

    def test_anonymous_and_user_and_regular_admin_denied(self):
        self._put_draft(self._reg_admin(), "草稿")
        anon = self._anon()
        self.assertEqual(anon.post("/api/announcement/publish", json={}).status_code, 401)
        u = self._user()
        self.assertEqual(self._publish(u, None).status_code, 403)
        sub = self._reg_admin()
        r = self._publish(sub, REG_PASS)   # 普通管理员带自己的正确口令
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertNotIn("YIBAN_ANNOUNCEMENT", self._env(), "越界发布不得落盘")
        self.assertEqual([a for a in self.alerts if a[0] == "公告发布告警"], [],
                         "被拒的越界发布不得发出发布告警（草稿写入那条变更告警是应有之义）")
        self.assertEqual(self._audit("announcement_publish"), [])

    def test_master_without_password_denied_even_right_after_exempt_save(self):
        m = self._master()
        self._put_draft(m, "草稿")
        # 先走一次"可豁免"的配置类复核，令会话进入短时豁免窗口
        r = m.post("/api/settings", json={"sign_order": "random",
                                          "confirm_password": ADMIN_PASS},
                   headers=self._hdr(m))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(m.get("/api/announcement", headers=self._hdr(m)))
        r = self._publish(m, None)
        self.assertEqual(r.status_code, 403,
                         f"发布必须吃当次口令，短时豁免不得覆盖：{r.get_data(as_text=True)}")
        self.assertNotIn("YIBAN_ANNOUNCEMENT", self._env())
        r = self._publish(m, "wrong-password")
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertNotIn("YIBAN_ANNOUNCEMENT", self._env())


class PublishEffectTest(_AnnBase):
    """发布 = 一次原子写：正式=草稿、草稿清空。"""

    def test_publish_promotes_draft_in_a_single_atomic_write(self):
        self._put_draft(self._reg_admin(), "今晚 23:00 维护")
        m = self._master()
        # 口令复核会读 .env，但不写；发布这一次是本轮唯一的落盘
        self.assertEqual(m.get("/api/announcement", headers=self._hdr(m)).status_code, 200)
        before = len(self.writes)
        r = self._publish(m)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["ok"])
        mine = self.writes[before:]
        self.assertEqual(len(mine), 1,
                         f"发布必须一次 write_env_batch 完成，实际 {len(mine)} 次落盘")
        self.assertIn("YIBAN_ANNOUNCEMENT=今晚 23:00 维护", mine[0])
        self.assertNotIn("YIBAN_ANNOUNCEMENT_DRAFT", mine[0],
                         "落盘内容里仍留草稿键 = 存在「正式已清、草稿未落」的中间态")
        env = self._env()
        self.assertEqual(env.get("YIBAN_ANNOUNCEMENT"), "今晚 23:00 维护")
        self.assertNotIn("YIBAN_ANNOUNCEMENT_DRAFT", env)
        self.assertNotIn("YIBAN_ANNOUNCEMENT_DRAFT_META", env)
        self.assertEqual(self._anon().get("/api/announcement").get_json()["text"],
                         "今晚 23:00 维护")
        self.assertEqual(self.webapp._announcement_cache[0], "今晚 23:00 维护")

    def test_publish_overwrites_previous_and_draft_gone_afterwards(self):
        m = self._master()
        self._put_draft(m, "第一条")
        self.assertEqual(self._publish(m).status_code, 200)
        self.assertEqual(self._raw_env().count("YIBAN_ANNOUNCEMENT="), 1,
                         "覆盖不得留下第二行已发布键")
        self._put_draft(m, "第二条")
        self.assertEqual(self._publish(m).status_code, 200)
        self.assertEqual(self._env().get("YIBAN_ANNOUNCEMENT"), "第二条")
        self.assertEqual(self._anon().get("/api/announcement").get_json()["text"], "第二条")
        # 再点一次发布：草稿已被清空 → 400，正式内容保持不动
        self.assertEqual(self._publish(m).status_code, 400)
        self.assertEqual(self._env().get("YIBAN_ANNOUNCEMENT"), "第二条")

    def test_publish_without_draft_is_400_not_silent_success(self):
        m = self._master()
        r = self._publish(m)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("草稿", r.get_json()["error"])
        self.assertNotIn("YIBAN_ANNOUNCEMENT", self._env())
        self.assertEqual(self.alerts, [])
        self.assertEqual(self._audit("announcement_publish"), [])
        # 草稿被普通管理员清空后同样算"无草稿"
        self._put_draft(self._reg_admin(), "内容")
        self._put_draft(self._reg_admin(), "")
        self.assertEqual(self._publish(m).status_code, 400)

    def test_publish_alert_is_urgent_and_describes_the_change(self):
        m = self._master()
        self._put_draft(self._reg_admin(), "全体注意：今晚维护")
        self._publish(m)
        rows = [a for a in self.alerts if a[0] == "公告发布告警"]
        self.assertEqual(len(rows), 1, f"发布应恰好一条发布告警，实际 {self.alerts}")
        title, body, urgent, force = rows[0]
        self.assertEqual(title, "公告发布告警",
                         "发布与草稿变更必须不同标题，否则邮件同类节流会互相吞")
        self.assertTrue(urgent, "对外可见内容变更必须走紧急账")
        self.assertTrue(force, "发布告警不得被同类节流吞掉")
        self.assertIn("admin", body)
        self.assertIn("全体注意：今晚维护", body)
        self.assertIn("新发布", body)
        # 覆盖场景：正文要说清是被替换掉一条旧公告，且只截前 80 字
        self._put_draft(m, "长" * 200)
        self._publish(m)
        body2 = [a for a in self.alerts if a[0] == "公告发布告警"][-1][1]
        self.assertIn("覆盖", body2)
        self.assertIn("长" * 80, body2)
        self.assertNotIn("长" * 81, body2)
        self.assertNotIn("\n长", body2)

    def test_publish_audit_records_publisher_and_diff(self):
        m = self._master()
        self._put_draft(self._reg_admin(), "维护通知")
        self._publish(m)
        rows = self._audit("announcement_publish")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["username"], "admin", "发布人必须是点发布的主管理员")
        self.assertIn("维护通知", rows[0]["detail"])
        self.assertIn("新发布", rows[0]["detail"])


class DraftMetaHelperTest(_AnnBase):
    """元数据解析单源且 fail-safe：坏值只降级为「元数据不可用」，绝不抛。"""

    def test_parse_accepts_the_documented_shape(self):
        by, at = self.webapp._parse_announcement_draft_meta(
            "someone@test.local|2026-09-19 10:20:30")
        self.assertEqual((by, at), ("someone@test.local", "2026-09-19 10:20:30"))

    def test_parse_rejects_anything_else(self):
        for bad in ("", "   ", "no-pipe", "a@test.local|", "|2026-09-19 10:20:30",
                    "a@test.local|x|y", "a@test.local|2026-09-19",
                    "a@test.local|2026-13-45 99:99:99",
                    "a@test.local|2026-09-19T10:20:30"):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self.webapp._parse_announcement_draft_meta(bad), ("", ""),
                    f"非法元数据必须解析为不可用：{bad!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
