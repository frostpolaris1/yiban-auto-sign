# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""工作队列页渲染契约（lcpx 试点 2026-10-25，管理端 /work/queue）。

标签：F · 前端与界面守卫
覆盖：`/work/queue` 的渲染契约——Vue 挂载点、manifest 资产真实在盘、module 脚本、
      管理员守卫、页面禁缓存。
对应实现：`web/routes/pages.py` 的 `queue_page` / `_render_vue_page`、
      `web/templates/pages/work_queue.html`、`frontend/src/queue/**`。
关键断言：① 页面引用的每个 `/static/vue/` 资产必须真实存在于磁盘（dist 已入库，
      缺资产时视图层应 404——这里判"引用了就一定要在盘"）；② 未登录被守卫弹回登录页、
      普通用户被弹回自己的首页；③ 页面在 `NO_STORE_PAGES` 里，响应 `Cache-Control: no-store`
      （防浏览器缓存旧 HTML/JS 造成登录循环）。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite；不联网、无需 node。

为什么要有它（审查 3）：新页面一旦漏登记 `NO_STORE_PAGES`、或挂载点与 dist 入口的
id 不一致，`/work/queue` 会渲染成空白壳且无人察觉。
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

from werkzeug.security import generate_password_hash

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VUE_DIR = os.path.join(BASE, "web", "static", "vue")
TEMPLATE = os.path.join(BASE, "web", "templates", "pages", "work_queue.html")
MOUNT_ID = 'id="vue-queue-app"'
TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "secret1"
USER_EMAIL = "user1@test.local"


def _asset_fs_path(site_path):
    rel = site_path[len("/static/vue/"):]
    return os.path.join(VUE_DIR, rel.replace("/", os.sep))


class QueuePageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-queuepage-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        cls.log_file = os.path.join(cls.tmp, "sign.log")
        with open(cls.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        for k, v in (("YIBAN_ACCOUNTS_KEY", TEST_KEY), ("YIBAN_ENV_FILE", cls.env_file),
                     ("YIBAN_ACCOUNTS_FILE", cls.accounts_file),
                     ("YIBAN_USERS_FILE", cls.users_file),
                     ("YIBAN_DB_FILE", cls.db_file), ("YIBAN_STATE_DIR", cls.tmp),
                     ("YIBAN_LOG_FILE", cls.log_file)):
            os.environ[k] = v
        global db
        import db  # 裸模块名：sys.path 已含 scripts（conftest 保证）
        cls.db = db
        spec = importlib.util.spec_from_file_location(
            "webapp_queuepage", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_queuepage"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        assert callable(getattr(cls.webapp, "create_app", None)), \
            "web/app.py 加载失败（exec_module 异常被抑制）"

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    def setUp(self):
        if self.db._conn is not None:
            with contextlib.suppress(Exception):
                self.db._conn.close()
            self.db._conn = None
        for suffix in ("", "-wal", "-shm"):
            path = self.db_file + suffix
            if os.path.exists(path):
                os.remove(path)
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        self.db.init_db(self.db_file, migrate_from=self.accounts_file,
                        env_file=self.env_file)
        self.db.create_user(USER_EMAIL, generate_password_hash(USER_PASS))

    def _admin_response(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200)
        r = c.get("/work/queue")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        return r

    # ---- 渲染契约 ----
    def test_renders_mount_point_and_real_assets(self):
        resp = self._admin_response()
        html = resp.get_data(as_text=True)
        self.assertIn(MOUNT_ID, html, "队列页未渲染 Vue 挂载点（模板 id 与 dist 入口不一致）")
        self.assertIn('type="module"', html, "未引入 module 脚本")
        assets = re.findall(r'(?:src|href)="([^"]*?/static/vue/[^"]+)"', html)
        self.assertTrue(assets, "队列页没有引用任何 /static/vue/ 资产")
        for url in assets:
            path = _asset_fs_path(url.split("?")[0])
            self.assertTrue(os.path.isfile(path), f"资产不在磁盘上：{url}")

    def test_template_mount_id_matches_vue_entry(self):
        """模板挂载点与前端入口选择器必须逐字一致（改一处漏一处就是空白页）。"""
        with open(TEMPLATE, encoding="utf-8") as fh:
            self.assertIn(MOUNT_ID, fh.read())
        entry = os.path.join(BASE, "frontend", "src", "queue", "main.ts")
        with open(entry, encoding="utf-8") as fh:
            self.assertIn('"#vue-queue-app"', fh.read())

    # ---- 守卫与缓存 ----
    def test_admin_guard_redirects_anonymous_and_normal_user(self):
        anon = self.webapp.create_app().test_client()
        self.assertIn(anon.get("/work/queue").status_code, (301, 302),
                      "未登录访问队列页应被守卫重定向")
        c = self.webapp.create_app().test_client()
        self.assertEqual(
            c.post("/api/login", json={"username": USER_EMAIL, "password": USER_PASS})
            .status_code, 200)
        c.get("/api/me")
        r = c.get("/work/queue")
        self.assertIn(r.status_code, (301, 302), "普通用户不应能打开管理端队列页")
        self.assertIn("/user/calendar", r.headers["Location"],
                      "普通用户应被弹回自己的首页（签到日历）")

    def test_page_is_not_cached(self):
        resp = self._admin_response()
        self.assertEqual(resp.headers.get("Cache-Control"), "no-store",
                         "/work/queue 必须在 NO_STORE_PAGES 里（防缓存旧 HTML 造成登录循环）")


if __name__ == "__main__":
    unittest.main()
