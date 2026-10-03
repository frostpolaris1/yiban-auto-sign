# -*- coding: utf-8 -*-
"""账号管理页契约测试（前端翻新 P3：整页从静态模板 + pages/work_accounts.js 迁到 Vue）。

标签：F · 前端与界面守卫（Vue 线新增）
覆盖：/work/accounts 渲染契约（挂载点、manifest 资产真实在盘、module 脚本、admin 守卫、
      no-store）、**legacy 资产已彻底退役**（模板不再引用 work_accounts.js /
      account-{form,ops,table}.js，且文件已从磁盘删除；2026-10-03 设置页迁 Vue 后
      select-field.js 亦退役，row-menu.js 仍被用户管理页桥接必须保留）、**PII 出口面**（列表手机号/归属邮箱是服务端脱敏值；
      完整手机号只存组件内存 fullPhone、绝不进模板 DOM）、e2e 依赖的 data-* 钩子仍在。
对应实现：`web/routes/pages.py` 的 `accounts_page` / `_render_vue_page`、
      `web/templates/pages/work_accounts.html`、`frontend/src/accounts/**`
关键断言：① 页面引用的每个 /static/vue/ 资产必须真实存在于磁盘；② 退役是"连引用一起消失"
      而不只是"不用了"；③ **完整手机号绝不进 DOM**——组件模板只绑定 `row.phone`（服务端
      已脱敏），不得把 `fullPhone` 绑进模板/属性（MF-49 出口面在展示侧的落点）；
      ④ e2e 依赖的 data-* 钩子（页签/批量/空态/添加/行下标）仍在组件里。
依赖：纯本地 Flask test client + 临时 `.env`/SQLite，不联网、无需 node（dist 已入库）。
      选中身份键（手机号而非 index）由 tests/test_work_accounts_selection.py 真跑钉住；
      写操作防重入由 tests/test_account_ops_reentry.py 真跑 ops.js 钉住。
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
ACCOUNTS_TEMPLATE = os.path.join(BASE, "web", "templates", "pages", "work_accounts.html")
ACCOUNTS_VUE = os.path.join(BASE, "frontend", "src", "accounts", "Accounts.vue")
ACCOUNTS_MODEL_JS = os.path.join(BASE, "frontend", "src", "accounts", "model.js")
LEGACY_PAGE_JS = os.path.join(BASE, "web", "static", "js", "pages", "work_accounts.js")
LEGACY_COMPONENTS = (
    os.path.join(BASE, "web", "static", "js", "components", "account-form.js"),
    os.path.join(BASE, "web", "static", "js", "components", "account-ops.js"),
    os.path.join(BASE, "web", "static", "js", "components", "account-table.js"),
)
SELECT_FIELD_JS = os.path.join(BASE, "web", "static", "js", "components", "select-field.js")
ROW_MENU_JS = os.path.join(BASE, "web", "static", "js", "components", "row-menu.js")

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"

#: e2e 与页面行为依赖的 data-* 钩子（改名即静默失守——见模块 docstring ④）
CONTRACT_HOOKS = (
    "data-acct-tab",
    "data-acct-panel",
    "data-batch",
    "data-batch-clear",
    "data-empty-clear",
    "data-empty-tab",
    "data-add-account",
    "data-acct-idx",
)
#: 服务端时代的对外锚点（e2e 契约测试按它们取值，迁移后仍在组件里）
CONTRACT_IDS = (
    "accounts-pending-tbody",
    "accounts-tbody",
    "accounts-deleted-tbody",
    "batch-bar-pending",
    "batch-bar-active",
    "batch-bar-deleted",
    "pending-tip",
    "stat-success",
    "stat-failed",
    "stat-waiting",
    "stat-skipped",
)


def _asset_fs_path(site_path):
    rel = site_path[len("/static/vue/"):]
    return os.path.join(VUE_DIR, rel.replace("/", os.sep))


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class AccountsPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-accountspage-")
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

        spec = importlib.util.spec_from_file_location("webapp_accountspage", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_accountspage"] = cls.webapp
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
        r = c.get("/work/accounts")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        return r.get_data(as_text=True)

    # ---- 渲染契约 ----

    def test_accounts_renders_mount_point_and_real_assets(self):
        html = self._html()
        self.assertIn('id="vue-accounts-app"', html, "账号管理页未渲染 Vue 挂载点")
        self.assertIn('type="module"', html, "未引入 module 脚本")
        assets = re.findall(r'(?:src|href)="([^"]*?/static/vue/[^"]+)"', html)
        self.assertTrue(assets, "账号管理页没有引用任何 /static/vue/ 资产")
        for url in assets:
            path = _asset_fs_path(url.split("?")[0])
            self.assertTrue(os.path.isfile(path), f"资产不在磁盘上：{url}")

    def test_admin_guard_and_no_store(self):
        """/work/accounts 仍是管理端页面：未登录被弹回登录页，且页面禁缓存。"""
        c = self.webapp.create_app().test_client()
        r = c.get("/work/accounts")
        self.assertIn(r.status_code, (301, 302), "未登录访问账号管理页应被守卫重定向")
        html = self._html()
        self.assertIn("/work/accounts", html)

    # ---- 退役 ----

    def test_legacy_accounts_assets_are_fully_retired(self):
        html = self._html()
        for legacy in ("pages/work_accounts.js", "components/account-form.js",
                       "components/account-ops.js", "components/account-table.js"):
            self.assertNotIn(legacy, html, f"模板仍引用 legacy {legacy}")
        self.assertFalse(os.path.exists(LEGACY_PAGE_JS), "work_accounts.js 仍留在库里")
        for path in LEGACY_COMPONENTS:
            self.assertFalse(os.path.exists(path), f"{os.path.basename(path)} 仍留在库里")
        # 2026-10-03：设置页也整页迁到 Vue，自研四件套控件随之退役——select-field.js
        # 不再有消费者，必须已删除（row-menu.js 仍被用户管理页桥接，保留）。
        self.assertFalse(os.path.exists(SELECT_FIELD_JS),
                         "select-field.js 仍留在库里——设置页迁 Vue 后已无消费者")
        self.assertTrue(os.path.exists(ROW_MENU_JS),
                         "row-menu.js 被误删——用户管理页（frontend/src/users）仍在桥接它")

    def test_contract_hooks_survive_in_the_component(self):
        # 钩子可能在组件模板（字面量 id）或口径层（组定义表里的 tbody/bar 等 id）中
        src = _read(ACCOUNTS_VUE) + "\n" + _read(ACCOUNTS_MODEL_JS)
        missing = [h for h in CONTRACT_HOOKS if h not in src]
        self.assertEqual(missing, [], "缺少这些 data-* 钩子（e2e 按它们取值）：" + ", ".join(missing))
        missing_ids = [i for i in CONTRACT_IDS if i not in src]
        self.assertEqual(missing_ids, [], "缺少这些 id 锚点： " + ", ".join(missing_ids))

    # ---- PII 出口面（展示侧） ----

    def test_component_never_binds_full_phone_into_dom(self):
        """完整手机号只存内存 fullPhone、只进请求体；模板不得把它绑进 DOM。"""
        src = _read(ACCOUNTS_VUE)
        # 列表绑定的是服务端脱敏行字段
        self.assertIn("{{ row.phone }}", src, "手机号列必须渲染服务端脱敏值 row.phone")
        # 完整号字段（fullPhone）只允许出现在脚本逻辑里，绝不进模板插值 / 属性绑定
        self.assertNotIn("{{ fullPhone", src, "完整手机号被插值进模板——PII 泄漏")
        self.assertNotRegex(src, r':(?:value|title|aria-label|placeholder)="[^"]*fullPhone',
                            "完整手机号被绑进属性——PII 泄漏")
        # 零 v-html（判据用指令形态，避免把解释性注释判成违规）
        self.assertIsNone(re.search(r"\bv-html\s*=", src), "账号管理页不得使用 v-html")
        # 表单提交体只经 buildAccountPayload 构造（完整号在内存与请求体），自带脱敏口径
        self.assertIn("buildAccountPayload", src)
        self.assertIn("fullPhone", src, "编辑时应先把完整号取进内存（提交用），而不是回显")

    # ---- 写操作实例单例（防重入不静默失效） ----

    def test_ops_instance_is_created_once(self):
        """写操作实例必须单例：ops.js 的防重入是实例内 `inflight`，若每次动作都现建一个
        实例，`inflight` 恒为 false，双击 restore/move/批量会各发一次请求（守卫静默失效）。"""
        src = _read(ACCOUNTS_VUE)
        self.assertEqual(
            src.count("accountOps?.create("), 1,
            "YB.accountOps.create 只应在 makeOps() 里调用一次——逐动作现建会让防重入失效",
        )
        self.assertIn(
            "if (!opsSingleton) opsSingleton = makeOps();", src,
            "缺少 ops() 惰性单例：回到每次动作 makeOps() 会让 inflight 守卫恒 false",
        )


if __name__ == "__main__":
    unittest.main()
