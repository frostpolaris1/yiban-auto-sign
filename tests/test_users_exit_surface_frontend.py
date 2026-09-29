# -*- coding: utf-8 -*-
"""MF-49 出口面·Task 2-9b：前端出口 node 真跑钉（禁纯文本断言，照 2-8/对拍先例）。

钉的出口（每条都是**真函数体在 node 里执行**，桩只给浏览器环境）：
  ② `core.js::hydrateIdentity`——/api/me 不再经 apiCached：真跑后 sessionStorage 必须零写入；
  ② `core.js::loadNavBadges`——nav-users 缓存的是**标量投影**（计数），整表明文邮箱
     不落 sessionStorage；60s 窗口的请求收敛语义不变；
  ② `core.js::doLogout`——退出**无条件先清**外壳缓存：logout 请求失败（catch 路径）
     也带走 `yiban-cache:*`，非缓存键不误伤；
  ③ `user-ops.js` 单条 role/password/delete——path 只含不透明 id（不含 `@`），
     邮箱只出现在 batch/purge 的请求体；id 缺失拒绝发请求（不回落邮箱编 path）；
  ① `account-form.js::loadAvailableUsers`——下拉文本消费服务端 `display`，
     **不再** `email.split("@")[0]` 自算（号形态直出即泄漏）；
  ③ `work_users.js::fetchUsers`——state 记录携带 id（单条定位链路的起点）。

标签：F · 前端与界面守卫
覆盖：上述 ①②③ 各前端口径一个真跑用例
（批 6c3-C C-2 注：旧 `StaticNoSecondSplitTest` 静态防回潮断言为「精确源码 grep」
兜底，行为面由 `test_dropdown_uses_server_display` 真跑消费服务端 `display` 钉住，
已按对表裁撤。）
对应实现：`web/static/js/core.js`、`web/static/js/components/user-ops.js`、
`web/static/js/components/account-form.js`、`web/static/js/pages/work_users.js`
关键断言：断言打在 node 子进程的真实输出上（sessionStorage 影子对象、提交的 path/body
捕获），"函数存在"不算过；遮罩形态与明文反例成对钉。
依赖：**需要 node**（取不到整类 skip）；无网络、无浏览器。
"""

import json
import os
import shutil
import subprocess
import unittest

from test_web_mask_email_parity import _extract_js_function

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_JS = os.path.join(BASE, "web", "static", "js", "core.js")
USER_OPS_JS = os.path.join(BASE, "web", "static", "js", "components", "user-ops.js")
ACCOUNT_FORM_JS = os.path.join(BASE, "web", "static", "js", "components", "account-form.js")
WORK_USERS_JS = os.path.join(BASE, "web", "static", "js", "pages", "work_users.js")
NODE = shutil.which("node")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# 浏览器影子环境：sessionStorage 用可检查的 Map 替身；fetch 层用 api 桩记录请求。
# 被抽的真函数只认这些名字——桩是"浏览器"，不是"被测逻辑"。
_HARNESS = """
var store = {};
var sessionStorage = {
  getItem: function (k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
  setItem: function (k, v) { store[k] = String(v); },
  removeItem: function (k) { delete store[k]; },
  key: function (i) { return Object.keys(store)[i]; },
  get length() { return Object.keys(store).length; }
};
var apiCalls = [];
var badges = [];
var domTexts = [];
var logoutFails = false;
var csrfToken = "";
var me = null;
function url(p) { return "/base" + p; }
var location = { href: "" };
function setText(sel, v) { domTexts.push([sel, String(v)]); }
function setNavBadge(k, v) { badges.push([k, v]); }
function api(method, path) {
  apiCalls.push([method, path]);
  if (path === "/api/users") {
    return Promise.resolve({users: [
      {id: 1, email: "13800000000@qq.com", display: "138****0000", role: "user",
       created_at: "x", account_count: 1, pending_count: 0, review_count: 2},
      {id: 2, email: "b@q.com", display: "***", role: "user",
       created_at: "x", account_count: 0, pending_count: 0, review_count: 0}
    ], builtin_admin: "admin"});
  }
  if (path === "/api/accounts") return Promise.resolve({accounts: []});
  if (path === "/api/me") {
    return Promise.resolve({username: "admin", email: "admin@test.local",
                            csrf_token: "tok-abc", role: "admin", is_builtin_admin: true});
  }
  if (path === "/api/logout") {
    return logoutFails ? Promise.reject(new Error("network down")) : Promise.resolve({ok: true});
  }
  return Promise.resolve({});
}
"""


def _flush(script_obj_expr):
    """统一收口：微任务/短定时器跑完 Promise 链后打印结果 JSON。"""
    return (
        "\nsetTimeout(function () {\n"
        "  console.log('RESULT' + JSON.stringify(%s));\n"
        "}, 60);\n" % script_obj_expr
    )


def _run_node(script, tag):
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                          timeout=30, encoding="utf-8")
    if proc.returncode != 0:
        raise AssertionError("node 执行失败(%s)：%s\n%s" % (tag, proc.stderr, script[:400]))
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")]
    if not line:
        raise AssertionError("node 无结果输出(%s)：%r" % (tag, proc.stdout))
    return json.loads(line[-1][len("RESULT"):])


@unittest.skipUnless(NODE, "node 不可用：跳过 Task 2-9b 前端出口真跑")
class CoreCacheLifecycleTest(unittest.TestCase):
    """② /api/me 与 nav-users 不进 sessionStorage；doLogout 失败路径也清缓存。"""

    @classmethod
    def setUpClass(cls):
        src = _read(CORE_JS)
        import re
        decl = re.search(r'var CACHE_PREFIX = "[^"]*";', src)
        assert decl, "core.js 的 CACHE_PREFIX 声明找不到：抽取环境失效（红不是假绿）"
        cls.fns = decl.group(0) + "\n" + "\n".join(
            _extract_js_function(src, name)
            for name in ("forEach", "cacheGet", "cacheSet", "cacheClearAll",
                         "apiCached", "hydrateIdentity", "loadNavBadges", "doLogout"))

    def test_identity_never_touches_session_storage(self):
        # hydrateIdentity 真跑：/api/me 响应含 csrf_token 与登录邮箱——
        # 落 sessionStorage 即为失守面。断言存储为零、内存态拿到了 token。
        driver = "hydrateIdentity();\n"
        out = _run_node(_HARNESS + "\n" + self.fns + "\n" + driver + _flush(
            "{store: Object.keys(store), csrf: csrfToken, calls: apiCalls}"),
            "identity")
        self.assertEqual(out["store"], [], "身份数据不得写入 sessionStorage")
        self.assertEqual(out["csrf"], "tok-abc", "token 仍须取回（缓存化不是砍掉读取）")
        self.assertIn(["GET", "/api/me"], out["calls"])

    def test_nav_users_caches_scalar_projection_only(self):
        # loadNavBadges 真跑：缓存键里只允许躺计数；整表（含明文邮箱）不得落存储。
        # 60s 窗口的收敛语义保留：首次装载落缓存，第二次（缓存已写入）命中不发网络。
        # 两次调用**跨 tick**（首次 cacheSet 是异步），模拟 MPA 切页时 sessionStorage 已在。
        driver = ("loadNavBadges({role: 'admin'});\n"
                  "setTimeout(function(){ loadNavBadges({role: 'admin'}); }, 20);\n")
        out = _run_node(_HARNESS + "\n" + self.fns + "\n" + driver + _flush(
            "{store: store, calls: apiCalls, badges: badges}"), "nav-users")
        users_calls = [c for c in out["calls"] if c[1] == "/api/users"]
        self.assertEqual(len(users_calls), 1, "60s 窗口内两次装载只许发一次请求")
        cached = out["store"].get("yiban-cache:nav-users")
        self.assertIsNotNone(cached, "计数缓存语义保留（否则切页重新打表）")
        self.assertNotIn("@", json.dumps(out["store"]), "sessionStorage 不得出现邮箱形态")
        self.assertNotIn("13800000000", json.dumps(out["store"]))
        self.assertIn(["work-users", 1], out["badges"], "徽标计数=有待处理账号的用户数（口径不变）")

    def _logout_case(self, fails):
        driver = (
            "store['yiban-cache:nav-accounts'] = '{\"email\":\"a@test.local\"}';\n"
            "store['yiban-cache:nav-users'] = '3';\n"
            "store['yiban-announce-dismiss'] = 'keepme';\n"
            "logoutFails = %s;\n"
            "doLogout();\n" % ("true" if fails else "false"))
        return _run_node(_HARNESS + "\n" + self.fns + "\n" + driver + _flush(
            "{store: Object.keys(store), href: location.href}"), "logout")

    def test_logout_clears_cache_on_success(self):
        out = self._logout_case(fails=False)
        self.assertEqual(out["store"], ["yiban-announce-dismiss"],
                         "外壳缓存必须清空，非缓存键（公告关闭态）不误伤")
        self.assertEqual(out["href"], "/base/login")

    def test_logout_clears_cache_when_request_fails(self):
        # 登记点名的失败路径：POST /api/logout 打不出去（网络/5xx）也要带走缓存——
        # 用户意愿已终止会话，失败不得让明文邮箱表滞留到标签页关闭。
        out = self._logout_case(fails=True)
        self.assertEqual(out["store"], ["yiban-announce-dismiss"],
                         "logout 失败路径同样必须清缓存")
        self.assertEqual(out["href"], "/base/login", "失败也要离开管理页")


@unittest.skipUnless(NODE, "node 不可用：跳过 Task 2-9b 前端出口真跑")
class UserOpsOpaqueIdTest(unittest.TestCase):
    """③ user-ops 单条操作只按不透明 id 编 path；邮箱只进 batch/purge 请求体。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(USER_OPS_JS)
        cls.mask_email_js = _extract_js_function(_read(CORE_JS), "maskEmail")

    def test_single_ops_use_id_and_batch_keeps_body_emails(self):
        script = (
            self.mask_email_js + "\n"
            "var submitted = [], dialogs = [], errors = [], successes = [];\n"
            "var window = {YB: {"
            "  maskEmail: maskEmail,"
            "  confirmDialog: function (o) { dialogs.push({title: o.title, body: o.body}); return Promise.resolve(true); },"
            "  dangerousSubmit: function (o) { submitted.push({path: o.path, body: o.body, desc: o.desc, delayDesc: o.delayDesc || ''}); return Promise.resolve({ok: true, msg: 'x'}); },"
            "  toast: {error: function (m) { errors.push(m);}, success: function (m) { successes.push(m); }}"
            "}};\n"
            + self.src + "\n"
            "var records = {\n"
            "  'u1': {id: 42, email: 'trail-user@test.local'},\n"
            "  'uNoid': {email: 'noid@test.local'}\n"
            "};\n"
            "var ctx = {busy: function () {}, refresh: function () { return Promise.resolve(); },\n"
            "           resolve: function (uid) { return records[uid]; }};\n"
            "var ops = window.YB.userOps.create(ctx);\n"
            "ops.role('u1', 'admin');\n"
            "ops.resetPassword('u1', 'NewPass#123');\n"
            "ops.deleteUser('u1', 'full');\n"
            "ops.purge('u1');\n"
            "ops.batchReset(['u1'], 'NewPass#123');\n"
            "ops.batchDelete(['u1']);\n"
            "ops.role('uNoid', 'admin');\n"     # id 缺失：拒绝发请求，不回落邮箱编 path
            + _flush("{submitted: submitted, dialogs: dialogs, errors: errors, successes: successes}"))
        out = _run_node(script, "user-ops")
        paths = sorted(s["path"] for s in out["submitted"])
        # role/delete/purge 先弹确认（异步），password/batchReset 直发（同步），
        # 完成顺序不固定——按重数比较，钉的是"每个操作各一枪且 path 形态正确"。
        self.assertEqual(paths, sorted([
            "/api/users/42/role",
            "/api/users/42/password",
            "/api/users/42/delete",
            "/api/users/deleted/purge",
            "/api/users/batch",
            "/api/users/batch",
        ]), "单条三个端点按 id 定位；purge/batch 走既有 body 契约")
        # 一般判据：任何提交 path 都不得含 `@`（@ 只可能来自邮箱编 path）
        self.assertTrue(all("@" not in s["path"] for s in out["submitted"]),
                        "URL path 绝不允许出现邮箱（@ 只可能来自邮箱编 path）")
        # 弹窗文案仍是遮罩形态（内存态邮箱不外显）
        joined = json.dumps(out["dialogs"] + [s["desc"] + s["delayDesc"] for s in out["submitted"]])
        self.assertNotIn("trail-user@test.local", joined, "确认/复核文案不得含完整邮箱")
        self.assertIn("tra***@test.local", joined, "文案用遮罩形态仍可辨对象")
        # 邮箱只在 batch/purge 的 body 里（请求体不落 nginx path）
        bodies_with_emails = sorted(s["path"] for s in out["submitted"]
                                    if isinstance(s["body"], dict) and "emails" in s["body"])
        self.assertEqual(bodies_with_emails, sorted(
            ["/api/users/deleted/purge", "/api/users/batch", "/api/users/batch"]))
        self.assertEqual(len(out["submitted"]), 6, "uNoid（无 id）不得发出请求")
        # batch/purge 的 msg 只含数量（'x' 即后端 msg）：必须上屏，否则真实计数与跳过数
        # 被本地 fallback 吞掉；单目标 role/password/delete 走本地无 PII 文案，不碰后端 msg。
        self.assertEqual(
            sorted(m for m in out["successes"] if m == "x"),
            ["x", "x", "x"],
            "purge / batchReset / batchDelete 必须使用后端计数型 msg")
        self.assertNotIn("trail-user@test.local", json.dumps(out["successes"]),
                         "成功提示不得出现完整邮箱")


@unittest.skipUnless(NODE, "node 不可用：跳过 Task 2-9b 前端出口真跑")
class AccountFormOwnerDisplayTest(unittest.TestCase):
    """① 归属下拉消费服务端 display，禁第二套 split("@")[0]。"""

    @classmethod
    def setUpClass(cls):
        src = _read(ACCOUNT_FORM_JS)
        cls.body = "\n".join(_extract_js_function(src, name)
                             for name in ("emailBaseItems", "loadAvailableUsers"))

    def test_dropdown_uses_server_display(self):
        script = (
            "var options = null;\n"
            "var window = {YB: {"
            "  api: function () { return Promise.resolve({users: ["
            "    {email: '13800000000@qq.com', display: '138****0000', account_count: 0},"
            "    {email: 'alice@qq.com', display: 'ali***', account_count: 0},"
            "    {email: 'occupied@qq.com', display: 'occ***', account_count: 5}"
            "  ]}); },"
            "  selectField: {setOptions: function (id, items) { options = items; }}"
            "}};\n"
            "var YB = window.YB;\n"
            + self.body + "\nloadAvailableUsers();\n"
            + _flush("{options: options}"))
        out = _run_node(script, "account-form")
        texts = [i.get("t", "") for i in out["options"] if "t" in i]
        self.assertIn("138****0000", texts, "号形态本地部必须以遮罩态出现在下拉文本")
        self.assertIn("ali***", texts)
        # 文本面（下拉可见项）不得含完整本地部/手机号；`v` 里的完整邮箱是既有提交契约
        self.assertNotIn("13800000000", json.dumps(texts, ensure_ascii=False),
                         "下拉文本里不得出现完整本地部/手机号")
        self.assertNotIn("occupied@qq.com", json.dumps(texts, ensure_ascii=False),
                         "已有账号的用户不进下拉")


@unittest.skipUnless(NODE, "node 不可用：跳过 Task 2-9b 前端出口真跑")
class WorkUsersStateCarriesIdTest(unittest.TestCase):
    """③ 页面 state 记录必须携带 id（不透明定位链路的起点）。"""

    @classmethod
    def setUpClass(cls):
        src = _read(WORK_USERS_JS)
        cls.body = "\n".join(_extract_js_function(src, name)
                             for name in ("uidFor", "fetchUsers"))

    def test_fetch_users_maps_id_and_display(self):
        script = (
            _HARNESS + "\n"
            "var state = {builtin: '', users: [], byUid: {}, uidSeq: 0, uidOf: {}};\n"
            "var YB = {api: api};\n"
            + self.body + "\nfetchUsers();\n"
            + _flush("{recs: Object.keys(state.byUid).map(function (k) { return state.byUid[k]; })}"))
        out = _run_node(script, "work_users")
        recs = out["recs"]
        self.assertEqual(len(recs), 2)
        ids = sorted(r["id"] for r in recs)
        self.assertEqual(ids, [1, 2], "state 记录必须带服务端 id——单条定位靠它，不靠邮箱")
        phone_rec = next(r for r in recs if r["email"] == "13800000000@qq.com")
        self.assertEqual(phone_rec["display"], "138****0000")


if __name__ == "__main__":
    unittest.main(verbosity=2)
