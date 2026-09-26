# -*- coding: utf-8 -*-
"""个人提交判重预检的"在册确认"收口回归：会话配额 + 每次命中审计留痕。

标签：E · Web：认证/权限/API
覆盖：`POST /api/my-accounts` 预检判重命中路径的四件事——400 语义与文案原样（含
    "不泄露归属"口径与遮罩回显不变）、每次命中落一条审计（目标为遮罩号、detail 带
    窗口内第 N 次）、连续命中达 DUPCHECK_MAX 后改答 429 且被拒留痕每窗口至多一行、
    未重号的正常提交不占额度不写命中审计；另钉窗口翻窗后额度复位、按会话隔离计数、
    单账号/容量拦截仍排在判重之前（错误优先级不变）。
对应实现：`web/routes/my.py` 的 `api_my_account_add` 预检分支；限速表
    `web.routes.dupcheck_limits()`（app 实例 extensions，键为会话用户名）；配额原语
    `web.security._bump_window_count` / `_ip_store_trim`；被拒留痕复用
    `web.app` 的 `_read_audit_denied_trace`（limit=1）；常量 `DUPCHECK_WINDOW` /
    `DUPCHECK_MAX` 在 `web/app.py`。
关键断言：预检命中是"该号码在册"的对外确认——可达者拿它零成本、零留痕地定向探号
    等于免检预言机；配额只计命中（不伤正常路径），留痕必须可归因（actor + 遮罩号），
    拒绝面不能反过来把审计表刷成打字机（2 次 429 只许 1 行被拒审计）。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite（`import db` 走 scripts 门面），
    不联网、不起子进程、无需 node/bash；`YIBAN_ACCOUNT_VERIFY` 不开（预筛路径不触
    外呼）；翻窗用例以 `mock.patch` 拨 `web.routes.my` 的 `time.time`。
"""
import contextlib
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "c" * 64
ADMIN_USER = "admin"
ADMIN_PASS = "MasterPass#2026"
USER_PASS = "secret1"

PHONE_TAKEN = "13800000000"   # 假号（遮罩形态 138****0000）
PHONE_FREE = "13800000007"
PHONE_FREE2 = "13800000008"
ACTOR = "u1@test.local"
OTHER = "u2@test.local"
OWNER_OTHER = "someone@test.local"

DUP_HIT = "my_account_add_dup_hit"
DUP_DENIED = "my_account_add_dup_denied"


def _post_account(c, csrf, phone):
    return c.post("/api/my-accounts",
                  json={"name": "回归", "phone": phone, "password": "AbcdEfghij",
                        "phone_model": "", "phone_code": ""},
                  headers={"X-CSRF-Token": csrf})


class DupcheckOracleTest(unittest.TestCase):
    """预检命中 = 可定向确认的出口：必须吃会话配额、必须留可归因审计。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-dupcheck-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER={ADMIN_USER}\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp_dupcheck", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_dupcheck"] = cls.webapp
        spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        sys.modules.pop("webapp_dupcheck", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # 每用例一份全新库 + 全新 app：限速表挂 extensions，实例不复用即无跨用例串额
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file)
        db.create_user(ACTOR, self.webapp.generate_password_hash(USER_PASS))
        db.create_user(OTHER, self.webapp.generate_password_hash(USER_PASS))
        db.add_account({"name": "occupied", "phone": PHONE_TAKEN, "password": "p",
                        "phone_model": "", "phone_code": "", "status": "active",
                        "owner": OWNER_OTHER})

    # ---- 助手 ----
    def _app(self):
        return self.webapp.create_app()

    def _login(self, c, email):
        r = c.post("/api/login", json={"username": email, "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _audit(self, action):
        conn = sqlite3.connect(self.db_file)
        try:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT username, action, target, detail FROM audit_logs "
                "WHERE action=? ORDER BY id", (action,))]
        finally:
            conn.close()

    # ---- 1. 命中契约：400 文案原样 + 每次命中一条遮罩审计 ----
    def test_dup_hit_keeps_400_wording_and_audits_masked(self):
        app = self._app()
        c = app.test_client()
        token = self._login(c, ACTOR)
        r = _post_account(c, token, PHONE_TAKEN)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("已被使用", r.get_json().get("error", ""))
        rows = self._audit(DUP_HIT)
        self.assertEqual(len(rows), 1, "每次预检命中必须落一条可归因审计")
        self.assertEqual(rows[0]["username"], ACTOR)
        self.assertEqual(rows[0]["target"], "138****0000")
        for v in rows[0].values():
            self.assertNotIn(PHONE_TAKEN, str(v), "审计行不得含完整号码")

    def test_own_deleted_guidance_unchanged_and_counted(self):
        """"你刚删除的账号"差异化 400 也走命中记账——确认面不因文案分叉而免检。"""
        app = self._app()
        c = app.test_client()
        token = self._login(c, ACTOR)
        # 本人提交成功 → 软删 → 再提交同号：命中"撤销删除"指引分支
        r = _post_account(c, token, PHONE_FREE)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        acc = next(a for a in db.load_accounts() if a["phone"] == PHONE_FREE)
        db.set_account_deleted(acc["id"], True, deleted_at="2026-09-20 06:00:00",
                               deleted_by=ACTOR)
        r = _post_account(c, token, PHONE_FREE)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("刚删除", r.get_json().get("error", ""))
        self.assertEqual(len(self._audit(DUP_HIT)), 1)

    # ---- 2. 连续命中 ⇒ 配额 429，被拒留痕每窗口至多一行 ----
    def test_consecutive_hits_quota_then_429_single_denied_row(self):
        app = self._app()
        c = app.test_client()
        token = self._login(c, ACTOR)
        quota = self.webapp.DUPCHECK_MAX
        codes = [_post_account(c, token, PHONE_TAKEN).status_code
                 for _ in range(quota)]
        self.assertEqual(codes, [400] * quota, "前 DUPCHECK_MAX 次命中维持原 400")
        r = _post_account(c, token, PHONE_TAKEN)
        self.assertEqual(r.status_code, 429, r.get_data(as_text=True))
        self.assertNotIn("已被使用", r.get_json().get("error", ""),
                         "超限后不得继续给出'在册'确认")
        self.assertEqual(len(self._audit(DUP_HIT)), quota)
        _post_account(c, token, PHONE_TAKEN)  # 第二次被拒
        denied = self._audit(DUP_DENIED)
        self.assertEqual(len(denied), 1, "拒绝面每窗口至多一行，不得刷审计表")
        self.assertEqual(denied[0]["username"], ACTOR)

    # ---- 3. 正常路径零回归：未重号不吃额度、不写命中审计；按会话隔离；翻窗复位 ----
    def test_unique_submission_unbilled_and_actor_isolated(self):
        app = self._app()
        c = app.test_client()
        token = self._login(c, ACTOR)
        r = _post_account(c, token, PHONE_FREE)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("已提交", r.get_json().get("msg", ""))
        self.assertEqual(self._audit(DUP_HIT), [], "未重号不得写命中审计")
        # 已占名额的用户再提交别的号：单账号拦截仍排在判重之前（优先级不变）
        r = _post_account(c, token, PHONE_TAKEN)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("每个用户只能提交一个账号", r.get_json().get("error", ""))
        # u1 的命中/提交不消耗 u2 的额度：u2 独享整份 DUPCHECK_MAX
        c2 = app.test_client()
        token2 = self._login(c2, OTHER)
        codes = [_post_account(c2, token2, PHONE_TAKEN).status_code
                 for _ in range(self.webapp.DUPCHECK_MAX)]
        self.assertEqual(codes, [400] * self.webapp.DUPCHECK_MAX,
                         "计数按会话隔离")
        self.assertEqual(_post_account(c2, token2, PHONE_TAKEN).status_code, 429)

    def test_window_rollover_restores_quota(self):
        app = self._app()
        c = app.test_client()
        token = self._login(c, ACTOR)
        quota = self.webapp.DUPCHECK_MAX
        t0 = time.time()
        with mock.patch("web.routes.my.time.time", side_effect=lambda: t0):
            for _ in range(quota):
                self.assertEqual(_post_account(c, token, PHONE_TAKEN).status_code, 400)
            self.assertEqual(_post_account(c, token, PHONE_TAKEN).status_code, 429)
        # 拨过 DUPCHECK_WINDOW 后额度复位：回到 400 契约并继续留痕
        with mock.patch("web.routes.my.time.time",
                        side_effect=lambda: t0 + self.webapp.DUPCHECK_WINDOW + 1):
            r = _post_account(c, token, PHONE_TAKEN)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        hits = self._audit(DUP_HIT)
        self.assertEqual(len(hits), quota + 1)
        self.assertIn("第 1 次", hits[-1]["detail"], "新窗口重新从 1 计数")


if __name__ == "__main__":
    unittest.main()
