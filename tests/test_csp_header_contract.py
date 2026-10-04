# -*- coding: utf-8 -*-
"""CSP 响应头契约 + 注释归因守卫（census P0-4）。

标签：E · Web：认证/权限/API
覆盖：两道面——
    ① **行为面**：`create_app()` 后的任意响应确实带 `Content-Security-Policy`，且关键
       指令取值与实现一致（`script-src 'self' 'unsafe-inline'` /
       `style-src 'self' 'unsafe-inline'` / `object-src 'none'` /
       `frame-ancestors 'none'` / `default-src 'self'`）。census 点名"CSP 是全仓唯一
       零校验者的链"——tests/ 里 nosniff / Content-Security / X-Frame 命中 0，本测试即
       补上这枚校验者。
    ② **归因面**（本刀重点）：`web/app.py::no_cache` 里那段解释"为何不使用 CSP nonce"
       的注释，必须把真因归给**内联 `<script>` 块**（并点名 `theme_boot`），**不得**把
       原因归给内联 `onclick` 事件处理器；同时必须点明**内联事件属性**（onclick /
       onerror）**同样是** nonce 的阻碍，不得写成"不是原因"。实测：模板内联 `<script>`
       块 10 处（theme_boot 独占 4），内联事件属性 6 处（3 onclick + 3 onerror）——注释
       旧文把真因写成"模板含大量内联 onclick 处理器"，照它建议只迁 addEventListener 再
       上 nonce 会直接致坏（内联 `<script>` 块仍被 nonce 拦掉，theme_boot 的 4 块静默
       失效）；把事件属性一概写成"不是原因"同样误导（会漏掉 3 处 onerror 等）。

对应实现：`web/app.py::no_cache`（`Content-Security-Policy` 头 + 其上方注释段）。
关键断言：行为面断言头取值；归因面断言"注释宣称的原因 == 实测"——主载体是内联
    `<script>` 块且不遗漏地把内联事件属性列为第二类阻碍。注释改回旧归因（不点名内联
    `<script>`、或把事件属性写成"不是原因"/漏掉 onerror）必须红。变异验证见
    out/b0/02-csp-fix-report.md。
依赖：纯本地 Flask test client + 临时 .env/SQLite + 文件读取；不触网、无 skip、无新依赖。
"""
import contextlib
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PY = os.path.join(BASE, "web", "app.py")
TEMPLATES = os.path.join(BASE, "web", "templates")

#: 一段内联事件处理器属性（on<word>=，如 onclick / onerror / onchange / onsubmit /
#: oninput / onload / onfocus / onblur / onkeydown / onkeyup …）。HTML 里作为**属性名**
#: 出现（前导空白界定属性位）；刻意用通用 `on[a-z]+` 覆盖全族而非硬编白名单，避免将来
#: 新增事件形态漏计。要求前导空白，以免把 JS 里 `obj.onerror=` 这类属性赋值误算成属性。
_INLINE_EVENT = re.compile(r"\son[a-z]+\s*=", re.I)
#: 任意 <script ...> 开标签；无 src 者即"内联脚本块"。
_SCRIPT_TAG = re.compile(r"<script\b[^>]*>", re.I)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _extract_nonce_comment():
    """取 `web/app.py::no_cache` 函数体开头那段连续 `#` 注释（nonce 归因所在段）。"""
    lines = _read(APP_PY).splitlines()
    for i, ln in enumerate(lines):
        if "def no_cache(resp):" in ln:
            block = []
            for ln2 in lines[i + 1:]:
                s = ln2.strip()
                if s.startswith("#"):
                    block.append(s)
                elif s == "":
                    continue
                else:
                    break
            return "\n".join(block)
    raise AssertionError("web/app.py 里找不到 no_cache(resp) 函数")


def _count_inline_scripts(root):
    """数 root 下所有 html 里的内联 `<script>` 块（有开标签、无 src）。"""
    n = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith((".html", ".htm")):
                continue
            text = _read(os.path.join(dirpath, name))
            for tag in _SCRIPT_TAG.findall(text):
                if "src" not in tag.lower():
                    n += 1
    return n


def _count_inline_events(root):
    """数 root 下所有 html 里的内联事件处理器属性（onclick= / onerror= 等，全 on* 族）。"""
    n = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith((".html", ".htm")):
                continue
            n += len(_INLINE_EVENT.findall(_read(os.path.join(dirpath, name))))
    return n


class CspHeaderContractTest(unittest.TestCase):
    """① 行为面：真实响应带 CSP 头且关键指令取值符合实现口径。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-csp-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                "YIBAN_ACCOUNTS_KEY=" + "c" * 64 + "\n"
                "YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD=MasterPass#2026\n"
            )
        os.environ["YIBAN_ACCOUNTS_KEY"] = "c" * 64
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp_csp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_csp"] = cls.webapp
        spec.loader.exec_module(cls.webapp)
        db.init_db(cls.db_file, env_file=cls.env_file)
        cls.app = cls.webapp.create_app()

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        sys.modules.pop("webapp_csp", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_response_carries_the_csp_header_with_expected_directives(self):
        client = self.app.test_client()
        resp = client.get("/")
        csp = resp.headers.get("Content-Security-Policy")
        self.assertIsNotNone(csp, "任意响应都必须带 Content-Security-Policy（after_request 全站下发）")
        for directive in (
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self'",
            "font-src 'self'",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
        ):
            self.assertIn(directive, csp,
                          f"CSP 缺少/改动了关键指令 {directive!r}（当前: {csp!r}）")


class CspCommentAttributionTest(unittest.TestCase):
    """② 归因面：注释真因 == 实测主要载体（内联 `<script>` 块），不得归给 onclick。"""

    def test_measured_main_carrier_is_inline_script_not_onclick(self):
        """先钉住实测事实：内联 `<script>` 块是主要载体，内联事件属性只是少数。"""
        scripts = _count_inline_scripts(TEMPLATES)
        events = _count_inline_events(TEMPLATES)
        self.assertGreater(scripts, events,
                           "实测主要载体应是内联 <script> 块（实测：内联 <script> 10 处 vs "
                           "内联事件属性 6 处 = 3 onclick + 3 onerror）；"
                           f"当前实测 scripts={scripts} events={events}")

    def test_comment_attributes_blocker_to_inline_script_blocks(self):
        """注释必须把 nonce 不可用的原因归给内联 `<script>` 块，并点名 theme_boot。"""
        comment = _extract_nonce_comment()
        self.assertRegex(comment, r"内联\s*<\s*script",
                         "注释必须把真因点成『内联 <script> 块』（census P0-4）——"
                         f"当前 nonce 注释段: {comment!r}")
        self.assertIn("theme_boot", comment,
                      "注释必须点名关键载体 theme_boot（其独占 4 处内联 <script> 块，"
                      f"是致坏面最大的那块）——当前: {comment!r}")

    def test_comment_does_not_blame_inline_onclick(self):
        """注释不得把原因归给内联 `onclick` 事件处理器（旧文的错误归因）。"""
        comment = _extract_nonce_comment()
        self.assertNotRegex(comment, r"内联\s*onclick",
                            "注释不得把非 nonce 的原因归给『内联 onclick』——真因是内联 "
                            "<script> 块（onclick 只是 6 处内联事件属性中的 3 处）"
                            f"——当前: {comment!r}")

    def test_comment_flags_inline_event_attributes_as_blocker(self):
        """注释必须点明内联事件属性（onclick / onerror）同样是 nonce 的阻碍。

        旧文把这句写成"真因是内联 <script> 块，**不是**内联事件属性（onclick 仅 3 处）"，
        与实测不符：内联事件属性共 6 处（3 onclick + 3 onerror），且 nonce/hash 对
        事件处理器属性一律不适用——撤掉 'unsafe-inline' 后这 6 处同样失效。写成"不是
        原因"会误导后人以为"只迁 onclick 就能上 nonce"。
        """
        comment = _extract_nonce_comment()
        self.assertRegex(comment, r"内联事件属性",
                         "注释必须点出『内联事件属性』这一类同样是 nonce 的阻碍"
                         f"——当前: {comment!r}")
        self.assertRegex(comment, r"onerror",
                         "注释必须点名 onerror（此前被漏的一类，真树 3 处），否则会误导"
                         f"后人以为只有 onclick 需处理——当前: {comment!r}")

    def test_inline_event_regex_covers_onerror(self):
        """事件计数正则必须覆盖 onerror（真树 3 处），否则测量面漏掉一整类阻碍。"""
        self.assertTrue(_INLINE_EVENT.search(' onerror="x"'),
                        "事件正则未覆盖 onerror——真树 3 处 onerror 会被漏计，测量面变窄")
        self.assertTrue(_INLINE_EVENT.search(' onclick="x"'),
                        "事件正则必须仍覆盖 onclick（真树 3 处）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
