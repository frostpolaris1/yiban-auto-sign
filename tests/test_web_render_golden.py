# -*- coding: utf-8 -*-
"""Web 模板渲染「金标准」回归测试（2026-09-10 前端模块化护栏）。

## 为什么需要它

本仓前端**零自动化覆盖**：`tests/` 全是 Python，没有任何模板渲染断言。而
`web/templates/index.html` 原本是 4036 行的单文件（内联 JS 2722 行），现已经历
A1（内联 CSS/JS 外提为 `static/css/app.css` 与 `static/js/app.js`）与
A3（5 个 tab + 3 个模态拆成 `templates/tabs|partials` 的 include）；后面还有
P4（Tailwind v4）、U1（类名解耦）、V1–V4（daisyUI 视觉替换）等刀。
没有回归网时每一步都只能靠肉眼冒烟。

本测试立一条不变量：**页面结构指纹零变化**。
用 Flask `test_client` 真渲染三个页面 → 归一化 → 与 `tests/golden/*.rendered.html` 比对。
第二个不变量是**静态资源引用清单**（见 `test_asset_manifest_golden`）；它单独存在是因为
"CSS/JS 外提"这类改动会被结构指纹悄悄抹平（外提后标签位置不变），必须让它显式可见。

## 归一化规则（逐条 + 理由，2026-09-10 实证）

实测方法：三个页面各渲染两次并逐字符比对，结果在剥离 script/style 内容后**完全一致**
（无 CSRF、无时间戳混入标记 —— csrf token 走 `/api/me` 接口，不渲染进页面）。
因此归一化规则保持最小集：

| # | 规则 | 理由 |
|---|---|---|
| N1 | `<script ...>…</script>` 内容清空为 `<script ...></script>`（保留标签与属性） | JS 外提是既定计划；脚本内容与其存放位置之间没有结构语义。标签本身与其属性保留，资源清单由 assets.json 单独断言 |
| N2 | `<style ...>…</style>` 同理 | CSS 外提同上 |
| N3 | `?v=<任意非空白/引号内容>` → `?v=*` | `web_version` 每次发版都变；它不表达任何结构信息，参与比对会在每次发版时误报 |
| N4 | 连续空白（含换行）压成单个空格并去首尾 | Jinja `include` 拆分与 `base.html` 引入会改变缩进与换行，属排版噪音。真正的结构变化仍会被捕获——标签序列变了 |

**不要为了让测试通过而新增归一化规则。** 新增规则前先证明该差异是"非确定性/排版噪音"
而非"结构变化"，并在本表补一条含理由的记录。

## 有意变更结构时如何更新快照

    UPDATE_GOLDEN=1 python -m pytest tests/test_web_render_golden.py -q

**仅在结构确实有意变化时使用**，且必须在同一次提交里说明为什么结构变了。
若无脑重刷快照，这条护栏就失效了。

## 运行

    python -m pytest tests/test_web_render_golden.py -q
"""
import contextlib
import difflib
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
GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
USER_PASS = "UserPass123!"
USER_EMAIL = "golden-user@example.com"

# ---- N1 / N2 ----
_SCRIPT_CONTENT_RE = re.compile(r"(<script\b[^>]*>).*?(</script\s*>)", re.I | re.S)
_STYLE_CONTENT_RE = re.compile(r"(<style\b[^>]*>).*?(</style\s*>)", re.I | re.S)
# ---- N3 ----
_VERSION_QUERY_RE = re.compile(r"\?v=[^\"'&\s>]*")
# ---- 资源清单抽取 ----
_ASSET_TAG_RE = re.compile(r"<(?:script|link)\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"\b(src|href|rel)\s*=\s*\"([^\"]*)\"")


def normalize(html):
    """按模块 docstring 的 N1–N4 归一化；规则增减必须同步更新那张表。"""
    html = _SCRIPT_CONTENT_RE.sub(r"\1\2", html)
    html = _STYLE_CONTENT_RE.sub(r"\1\2", html)
    html = _VERSION_QUERY_RE.sub("?v=*", html)
    html = re.sub(r"\s+", " ", html)
    return html.strip()


def asset_manifest(html):
    """按文档顺序抽取 <script src> / <link href> 清单（N3 同样作用于其中）。

    返回字符串列表，形如 `script src=/static/vendor/tailwind.js?v=*`，
    保证顺序敏感 —— 加载顺序本身就是我们要守护的东西。
    """
    out = []
    for m in _ASSET_TAG_RE.finditer(html):
        tag = m.group(0)
        name = "script" if tag.lower().startswith("<script") else "link"
        attrs = dict(_ATTR_RE.findall(tag))
        if name == "script":
            if "src" not in attrs:
                continue  # 内联脚本不计入资源清单
            out.append(f"script src={_VERSION_QUERY_RE.sub('?v=*', attrs['src'])}")
        else:
            if "href" not in attrs:
                continue
            rel = attrs.get("rel", "")
            out.append(f"link rel={rel} href={_VERSION_QUERY_RE.sub('?v=*', attrs['href'])}")
    return out


def _first_diff_lines(expected, actual, context=200):
    """给出首个差异位置的可读上下文，便于定位是哪一段结构变了。"""
    sm = difflib.SequenceMatcher(None, expected, actual, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        return (
            f"首个差异 [{tag}] 于 expected[{i1}:{i2}] / actual[{j1}:{j2}]\n"
            f"  expected: …{expected[max(0, i1 - context):i2 + context]}…\n"
            f"  actual  : …{actual[max(0, j1 - context):j2 + context]}…"
        )
    return "（未找到差异片段 —— 可能仅长度不同）"


class WebRenderGoldenTest(unittest.TestCase):
    """三页渲染结构指纹 + 静态资源清单的金标准回归。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-golden-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        cls.state_dir = os.path.join(cls.tmp, "state")
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

        global db
        import db  # 裸模块名：pyproject 的 pythonpath 已含 scripts

        spec = importlib.util.spec_from_file_location("webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

        db.init_db(cls.db_file, migrate_from=cls.accounts_file, env_file=cls.env_file)
        db.create_user(USER_EMAIL, cls.webapp.generate_password_hash(USER_PASS))

        os.makedirs(GOLDEN_DIR, exist_ok=True)
        cls.update_golden = os.environ.get("UPDATE_GOLDEN") == "1"

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in (
            "YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
            "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
        ):
            os.environ.pop(k, None)

    # ---- 会话构造 ----

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        return c

    def _user_client(self):
        """`/user` 要求 role == "user"。_current_role 会回查数据库，故用户须真实存在。"""
        u = db.find_user(USER_EMAIL)
        self.assertIsNotNone(u, "setUpClass 应已创建 golden 用户")
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

    def _render(self, page):
        client = {
            "login": lambda: self.webapp.create_app().test_client(),
            "index": self._admin_client,
            "user": self._user_client,
        }[page]()
        path = {"login": "/login", "index": "/", "user": "/user"}[page]
        r = client.get(path)
        self.assertEqual(r.status_code, 200, f"{page} 渲染失败：{r.status_code}")
        return r.get_data(as_text=True)

    # ---- 金标准读写 ----

    def _golden_path(self, name):
        return os.path.join(GOLDEN_DIR, name)

    def _assert_golden(self, name, actual):
        path = self._golden_path(name)
        if self.update_golden or not os.path.exists(path):
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(actual)
        self.assertTrue(
            os.path.exists(path),
            f"金标准快照缺失：{path}\n首次建立请运行 UPDATE_GOLDEN=1 python -m pytest {__file__} -q",
        )
        with open(path, encoding="utf-8") as f:
            expected = f.read()
        if actual != expected:
            self.fail(
                f"{name} 的结构指纹与金标准不一致。\n"
                f"如果这是**有意的结构变更**：确认无误后运行\n"
                f"    UPDATE_GOLDEN=1 python -m pytest tests/test_web_render_golden.py -q\n"
                f"并在同一次提交里说明为什么结构变了。\n"
                f"如果这是**无意的回归**：这就是它要抓的东西。\n\n"
                f"{_first_diff_lines(expected, actual)}"
            )

    # ---- 用例 ----

    def test_login_structure_golden(self):
        self._assert_golden("login.rendered.html", normalize(self._render("login")))

    def test_index_structure_golden(self):
        self._assert_golden("index.rendered.html", normalize(self._render("index")))

    def test_user_structure_golden(self):
        self._assert_golden("user.rendered.html", normalize(self._render("user")))

    def test_asset_manifest_golden(self):
        """静态资源引用清单（顺序敏感）。

        存在的理由：CSS/JS 外提后标签位置不变，结构指纹察觉不到 —— 那类改动必须在这里被看见。
        """
        actual = {p: asset_manifest(self._render(p)) for p in ("index", "login", "user")}
        path = self._golden_path("assets.json")
        if self.update_golden or not os.path.exists(path):
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(actual, f, ensure_ascii=False, indent=2)
                f.write("\n")
        with open(path, encoding="utf-8") as f:
            expected = json.load(f)
        if actual != expected:
            self.fail(
                "静态资源引用清单与金标准不一致（页面引用的 CSS/JS 变了）。\n"
                "有意外提/新增/删除资源时，确认无误后更新 assets.json 并在提交里说明。\n"
                f"  expected: {json.dumps(expected, ensure_ascii=False, indent=2)}\n"
                f"  actual  : {json.dumps(actual, ensure_ascii=False, indent=2)}"
            )


if __name__ == "__main__":
    unittest.main()
