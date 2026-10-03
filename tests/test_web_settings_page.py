# -*- coding: utf-8 -*-
"""系统设置页契约测试（前端翻新 P3：整页从静态模板 + pages/work_settings.js 迁到 Vue）。

标签：F · 前端与界面守卫（Vue 线新增）
覆盖：/work/settings 渲染契约（挂载点、manifest 资产真实在盘、module 脚本、admin 守卫、
      no-store、`.env` 行分隔符常量仍由后端下发）、**legacy 资产已彻底退役**（模板不再引用
      work_settings.js / settings-*.js / 自研四件套控件，且文件已从磁盘删除）、
      **e2e 依赖的稳定锚点仍在组件里**
对应实现：`web/routes/pages.py` 的 `settings_page` / `_render_vue_page`、
      `web/templates/pages/work_settings.html`、`frontend/src/settings/**`
关键断言：① 页面引用的每个 /static/vue/ 资产必须真实存在于磁盘；② 退役是"连引用一起消失"
      而不只是"不用了"；③ 行分隔符码点常量仍由后端渲染进页面（`env_line_break_codes`），
      前端不另抄第二份；④ e2e 依赖的服务端时代 id（调度/公告/容量/健康/执行体/开关）仍在
      组件源码里。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite，不联网、无需 node（dist 已入库）。
      `edgeMaxMin` / `norm` / `resolvePaintValue` 等纯函数由 test_schedule_edge_limit_js /
      test_time_field_norm / test_env_line_break_frontend 真跑钉住；写链由
      test_delay_ack_frontend 钉住。
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
VUE_DIR = os.path.join(BASE, "web", "static", "vue")
SETTINGS_TEMPLATE = os.path.join(BASE, "web", "templates", "pages", "work_settings.html")
SETTINGS_SRC = os.path.join(BASE, "frontend", "src", "settings")
LEGACY_PAGE_JS = os.path.join(BASE, "web", "static", "js", "pages", "work_settings.js")
LEGACY_COMPONENTS = tuple(
    os.path.join(BASE, "web", "static", "js", "components", name)
    for name in (
        "settings-schedule.js", "settings-dist-viz.js", "settings-health.js",
        "settings-notify.js", "settings-mail.js", "settings-quota.js",
        "settings-switches.js", "settings-executors.js",
        "select-field.js", "multiselect-field.js", "range-field.js", "time-field.js",
    )
)

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"

#: e2e 与页面行为依赖的服务端时代 id（改名即静默失守——见模块 docstring ④）。
#: 分布在组件源码里（口径层/模板），故对 SETTINGS_SRC 全量聚合后断言。
CONTRACT_IDS = (
    "ss-order", "ss-dist", "ss-gap", "ss-save", "ss-reset", "ss-dirty",
    "ss-sat", "ss-sun", "ss-time-pref",
    "set-announcement", "set-ann-dirty", "set-ann-save", "set-ann-publish",
    "sn-type", "sn-secret", "sn-save", "sm-to", "sm-save", "sm-global",
    "set-max-users", "set-max-accounts", "set-cap-save", "set-cap-tip",
    "sh-verify", "sh-probe-enable", "sh-save",
    "set-exec-table", "set-exec-solo", "set-exec-row-add",
    "set-gp-pause", "set-rp-pause",
    "set-panel-schedule", "set-panel-announcement", "set-panel-notify",
    "set-panel-quota", "set-panel-health", "set-panel-executors", "set-panel-switches",
)


def _asset_fs_path(site_path):
    rel = site_path[len("/static/vue/"):]
    return os.path.join(VUE_DIR, rel.replace("/", os.sep))


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _settings_source_blob():
    parts = []
    for name in sorted(os.listdir(SETTINGS_SRC)):
        if name.endswith((".vue", ".js", ".ts")):
            parts.append(_read(os.path.join(SETTINGS_SRC, name)))
    return "\n".join(parts)


class SettingsPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-settingspage-")
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
        cls._old_env = {k: os.environ.get(k) for k in (
            "YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
            "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE",
        )}
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.state_dir
        os.environ["YIBAN_LOG_FILE"] = cls.log_file

        spec = importlib.util.spec_from_file_location("webapp_settingspage", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_settingspage"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _html(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        r = c.get("/work/settings")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        return r.get_data(as_text=True)

    # ---- 渲染契约 ----

    def test_settings_renders_mount_point_and_real_assets(self):
        html = self._html()
        self.assertIn('id="vue-settings-app"', html, "设置页未渲染 Vue 挂载点")
        self.assertIn('type="module"', html, "未引入 module 脚本")
        assets = re.findall(r'(?:src|href)="([^"]*?/static/vue/[^"]+)"', html)
        self.assertTrue(assets, "设置页没有引用任何 /static/vue/ 资产")
        for url in assets:
            path = _asset_fs_path(url.split("?")[0])
            self.assertTrue(os.path.isfile(path), f"资产不在磁盘上：{url}")

    def test_admin_guard_and_no_store(self):
        """/work/settings 仍是管理端页面：未登录被弹回登录页，且页面禁缓存。"""
        c = self.webapp.create_app().test_client()
        r = c.get("/work/settings")
        self.assertIn(r.status_code, (301, 302), "未登录访问设置页应被守卫重定向")
        c2 = self.webapp.create_app().test_client()
        c2.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        r2 = c2.get("/work/settings")
        self.assertIn("/work/settings", r2.get_data(as_text=True))
        self.assertIn("no-store", r2.headers.get("Cache-Control", ""))

    def test_env_line_break_constant_still_rendered_by_backend(self):
        """`.env` 行分隔符码点仍由后端下发（前端不另抄第二份字符清单）。"""
        html = self._html()
        self.assertIn("YB_ENV_LINE_BREAK_CODES", html)
        self.assertIn("window.YB_ENV_LINE_BREAK_CODES", html)
        tpl = _read(SETTINGS_TEMPLATE)
        self.assertIn("env_line_break_codes", tpl)

    # ---- 退役 ----

    def test_legacy_settings_assets_are_fully_retired(self):
        html = self._html()
        for legacy in ("pages/work_settings.js", "components/settings-schedule.js",
                       "components/settings-dist-viz.js", "components/select-field.js",
                       "components/range-field.js", "components/time-field.js",
                       "components/multiselect-field.js"):
            self.assertNotIn(legacy, html, f"模板仍引用 legacy {legacy}")
        self.assertFalse(os.path.exists(LEGACY_PAGE_JS), "work_settings.js 仍留在库里")
        for path in LEGACY_COMPONENTS:
            self.assertFalse(os.path.exists(path), f"{os.path.basename(path)} 仍留在库里")

    # ---- e2e 锚点 ----

    def test_contract_ids_survive_in_the_components(self):
        src = _settings_source_blob()
        missing = [i for i in CONTRACT_IDS if i not in src]
        self.assertEqual(missing, [], "缺少这些 id 锚点（e2e 按它们取值）：" + ", ".join(missing))

    def test_no_v_html_in_settings_components(self):
        for name in sorted(os.listdir(SETTINGS_SRC)):
            if not name.endswith(".vue"):
                continue
            src = _read(os.path.join(SETTINGS_SRC, name))
            self.assertIsNone(re.search(r"\bv-html\s*=", src), f"{name} 使用了 v-html")

    def test_secrets_never_backfilled_into_dom(self):
        """密钥/授权码只作 placeholder：模板不得把脱敏值绑进 input 的 value。"""
        src = _settings_source_blob()
        # sn-secret 输入框必须是空 value（v-model 到本地空串），不能绑定服务端 secret
        self.assertIn('v-model="pushForm.secret"', src)
        self.assertNotIn("secret_masked", _read(os.path.join(SETTINGS_SRC, "NotifyCard.vue")).split("<template>")[-1],
                         "模板里出现 secret_masked —— 脱敏密钥不得进 DOM")


if __name__ == "__main__":
    unittest.main()
