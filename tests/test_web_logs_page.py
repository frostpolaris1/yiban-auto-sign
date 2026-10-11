# -*- coding: utf-8 -*-
"""签到日志页契约测试（前端翻新 P1b：整页从静态模板 + data_logs.js 迁到 Vue）。

标签：F · 前端与界面守卫（Vue 线新增）
覆盖：/data/logs 渲染契约（挂载点、manifest 资产真实在盘、module 脚本、no-store、
      admin 守卫）、**legacy 资产已彻底退役**（HTML 不再引用 data_logs.js / date-field.js，
      分区改由客户端渲染，服务端不再出 tab 标记）、/api/logs 响应键契约
对应实现：`web/routes/pages.py` 的 `logs_page` / `_render_vue_page`、
      `web/templates/pages/data_logs.html`、`frontend/src/logs/**`、
      `web/routes/data.py::api_logs`
关键断言：① 页面引用的每个 /static/vue/ 资产必须真实存在于磁盘；② 退役是"连引用一起消失"
      而不只是"不用了"——否则旧脚本会继续被下载、也无人发现它已成死代码；
      ③ /api/logs 的信封键必须覆盖 `frontend/src/logs/format.ts::LogsPayload`
      消费的字段（跨语言契约，任一侧改名即红）
依赖：纯本地 Flask test client + 临时 `.env`/SQLite，不联网、无需 node（dist 已入库）
"""
import contextlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VUE_DIR = os.path.join(BASE, "web", "static", "vue")

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "UserPass123!"
USER_EMAIL = "logs-page-user@example.com"

# format.ts::LogsPayload 消费的键（缺任何一个，页面就会渲染空/报错）
PAYLOAD_KEYS = (
    "logs", "total_lines", "returned", "truncated", "dropped_lines", "q",
    "level", "collapsed_lines",
    "log_file", "date", "is_today", "probe_events", "sign_events",
    "recent_log_date", "recent_probe_date", "recent_sign_date",
)

db = None  # setUpClass 装载（裸模块名，pyproject pythonpath 已含 scripts）


def _asset_fs_path(site_path):
    """/static/vue/assets/x.js → 磁盘绝对路径。"""
    rel = site_path[len("/static/vue/"):]
    return os.path.join(VUE_DIR, rel.replace("/", os.sep))


class LogsPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-logspage-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        cls.state_dir = os.path.join(cls.tmp, "state")
        cls.log_file = os.path.join(cls.tmp, "sign.log")
        os.makedirs(cls.state_dir, exist_ok=True)
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        with open(cls.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)

        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.state_dir
        os.environ["YIBAN_LOG_FILE"] = cls.log_file

        global db
        import db  # 裸模块名：pyproject 的 pythonpath 已含 scripts

        spec = importlib.util.spec_from_file_location("webapp_logspage", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_logspage"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

        db.init_db(cls.db_file, migrate_from=cls.accounts_file, env_file=cls.env_file)
        db.create_user(USER_EMAIL, cls.webapp.generate_password_hash(USER_PASS))

        # 当天的日志文件与事件：**必须种**——否则 /api/logs 的三元组与事件列表都为空，
        # 契约断言会退化成"对合成字面量校验"（永远为真，既抓不到字段改名也抓不到搬漏）。
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        with open(cls.webapp.log_path_for(today), "w", encoding="utf-8") as fh:
            fh.write(chr(10).join([
                f"[{today} 06:31:01] [INFO] yiban: [13800138001] 签到成功",
                f"[{today} 06:31:02] [WARNING] yiban: [13800138001] 单次尝试耗时偏长",
            ]) + chr(10))
        db.add_sign_event(f"{today} 06:31:01", "13800138001", "success", "签到成功", stage="sign", attempt=1)
        db.add_sign_event(f"{today} 06:35:00", "13800138001", "ok", "探测正常", stage="probe", attempt=1)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in (
            "YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
            "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE",
        ):
            os.environ.pop(k, None)

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        return c

    def _user_client(self):
        u = db.find_user(USER_EMAIL)
        self.assertIsNotNone(u, "setUpClass 应已创建日志页测试用户")
        c = self.webapp.create_app().test_client()
        with c.session_transaction() as s:
            s["auth"] = True
            s["role"] = "user"
            s["username"] = USER_EMAIL.lower()
            s["auth_source"] = "user"
            s["pw_version"] = u.get("pw_version", 1)
            s["login_ts"] = int(time.time())
            s["sid"] = "0" * 32
        return c

    # ---- 渲染契约 ----

    def test_logs_renders_mount_point_and_real_assets(self):
        c = self._admin_client()
        r = c.get("/data/logs")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        html = r.get_data(as_text=True)
        self.assertIn('id="vue-logs-app"', html, "挂载点缺失")

        scripts = [m for m in re.findall(r'<script type="module" src="([^"]+)">', html) if "/static/vue/" in m]
        self.assertTrue(scripts, "日志页必须引入 /static/vue/ 下的 module 脚本")
        referenced = scripts + re.findall(r'href="(/static/vue/[^"]+)"', html)
        for site_path in referenced:
            self.assertTrue(
                os.path.isfile(_asset_fs_path(site_path)),
                f"页面引用的 Vue 资产不存在：{site_path}（dist 与 manifest 不同步？）",
            )

    def test_logs_page_is_no_store(self):
        c = self._admin_client()
        self.assertEqual(c.get("/data/logs").headers.get("Cache-Control"), "no-store")

    def test_legacy_assets_are_fully_retired(self):
        """退役必须是"连引用一起消失"：HTML 不再引旧脚本，服务端也不再出分区标记。

        只删文件、留着引用会让浏览器 404、也让人以为旧实现还在；反过来只删引用、留着
        文件则留下死代码（本仓对死文件有清理纪律）。
        """
        c = self._admin_client()
        html = c.get("/data/logs").get_data(as_text=True)
        for legacy in ("static/js/pages/data_logs.js", "static/js/components/date-field.js"):
            self.assertNotIn(legacy, html, f"页面仍在引用已退役的 {legacy}")
        self.assertNotIn("data-tab-id", html, "分区标记应由 Vue 渲染，服务端不再出")
        # 文件本身也该已删除（防"引用没了、文件还在"的半退役状态）
        self.assertFalse(os.path.exists(os.path.join(BASE, "web", "static", "js", "pages", "data_logs.js")))
        self.assertFalse(os.path.exists(os.path.join(BASE, "web", "static", "js", "components", "date-field.js")))

    def test_logs_data_is_client_rendered_only(self):
        c = self._admin_client()
        html = c.get("/data/logs").get_data(as_text=True)
        self.assertEqual(html.count('id="vue-logs-app"'), 1, "挂载点应恰好一处")
        self.assertNotIn("<pre", html, "日志正文必须由 Vue 客户端渲染，不得服务端出 pre")

    # ---- 守卫 ----

    def test_logs_redirects_anonymous_to_login(self):
        c = self.webapp.create_app().test_client()
        r = c.get("/data/logs")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers.get("Location", "").endswith("/login"))

    def test_logs_redirects_user_role_away(self):
        c = self._user_client()
        r = c.get("/data/logs")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers.get("Location", "").endswith("/user/calendar"))

    # ---- vue_assets 契约（第三个入口）----

    def test_vue_assets_resolves_logs_entry(self):
        from web.services.vue_assets import vue_assets

        assets = vue_assets("logs.html")
        self.assertTrue(assets["js"], "manifest 存在时应能解析出日志页入口 js")
        self.assertTrue(assets["css"], "日志页应带自己的样式文件")
        for group in ("js", "preloads", "css"):
            for path in assets[group]:
                self.assertTrue(path.startswith("/static/vue/"), f"{group} 路径前缀不对：{path}")
                self.assertTrue(os.path.isfile(_asset_fs_path(path)), f"清单资产不在盘：{path}")

    # ---- 跨边界契约：响应键 ↔ Vue 消费字段 ----

    def test_logs_api_envelope_covers_frontend_fields(self):
        """`/api/logs` 的响应必须覆盖 `format.ts::LogsPayload` 消费的每个键。

        两侧单测都覆盖不到这一段：Python 侧只测自己返回什么，Vitest 只测纯函数；
        任一侧给字段改名而另一侧没跟上，就是白屏或读到 undefined。
        """
        c = self._admin_client()
        r = c.get("/api/logs")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        body = r.get_json()
        for key in PAYLOAD_KEYS:
            self.assertIn(key, body, f"/api/logs 缺少前端消费的键：{key}")
        self.assertIsInstance(body["probe_events"], list)
        self.assertIsInstance(body["sign_events"], list)
        # 事件行字段（el-table 的列 prop 直接绑定这些名字）——对**真实行**校验，不用合成字面量
        self.assertTrue(body["sign_events"], "夹具应已种入一条签到事件")
        self.assertTrue(body["probe_events"], "夹具应已种入一条探针事件")
        for kind in ("sign_events", "probe_events"):
            row = body[kind][0]
            for ev_key in ("time", "phone", "status", "message"):
                self.assertIn(ev_key, row, f"{kind} 事件行契约缺 {ev_key}")
            # 脱敏：手机号不得是完整号（服务端 _mask_phone 出口）
            self.assertNotEqual(row["phone"], "13800138001", f"{kind} 事件手机号未脱敏")
            self.assertIn("*", row["phone"])

    def test_logs_date_filter_rejects_bad_format(self):
        c = self._admin_client()
        r = c.get("/api/logs?date=2026-13-99")
        self.assertEqual(r.status_code, 400)
        self.assertIn("日期格式不正确", r.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
