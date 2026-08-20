# -*- coding: utf-8 -*-
"""合规文档渲染（_render_md / _read_doc_html / _doc_page）回归测试。

背景：
- 此前 _render_md 的段落合并循环把以 `*`/`-` 开头但并非列表项的行
  （如 `**生效日期**：【...】`）误判为列表开始，导致 i 永不前进、渲染死循环。
  现用带超时的线程守卫确保不再复发。
- 仓库内 PRIVACY_POLICY.md / USER_AGREEMENT.md 为「只含注释的空模板」：
  未填写时 _read_doc_html 应回退中性占位文案，且绝不把 `<!-- -->` 开发注释
  泄漏到页面。完整版供运营者本地使用（deploy-local/，不入库），测试仅在
  本机存在时做内容断言，避免在 CI 上依赖被 gitignore 的文件。
"""
import os
import sys
import threading
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import web.app as web  # noqa: E402

DEPLOY_LOCAL = os.path.join(BASE, "deploy-local")


def render_with_timeout(doc_name, seconds=5):
    """在子线程渲染；超时视为失败（捕获死循环回归）。"""
    box = {}

    def _worker():
        box["html"] = web._read_doc_html(doc_name)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise AssertionError(
            f"_read_doc_html({doc_name!r}) 超过 {seconds}s 未返回，疑似渲染死循环"
        )
    return box["html"]


class LegalDocRenderTest(unittest.TestCase):
    def test_render_bold_line_no_infinite_loop(self):
        # 回归：`**生效日期**：...` 不能被误判成列表而卡死
        html = web._render_md("**生效日期**：【2026-01-01】\n\n下一段。")
        self.assertIn("<strong>生效日期</strong>", html)
        self.assertIn("下一段。", html)

    def test_render_basic_blocks(self):
        md = "# 标题\n\n- a\n- b\n\n> 引用\n\n1. 一\n2. 二\n\n正文。"
        html = web._render_md(md)
        self.assertIn("<h1>标题</h1>", html)
        self.assertIn("<ul><li>a</li><li>b</li></ul>", html)
        self.assertIn("<blockquote>引用</blockquote>", html)
        self.assertIn("<ol><li>一</li><li>二</li></ol>", html)
        self.assertIn("<p>正文。</p>", html)

    def test_render_inline_format(self):
        html = web._render_md("**粗体** 与 `代码` 与 [链接](https://example.com)")
        self.assertIn("<strong>粗体</strong>", html)
        self.assertIn("<code>代码</code>", html)
        self.assertIn('<a href="https://example.com" target="_blank" rel="noopener">链接</a>', html)

    def test_html_comment_lines_not_rendered(self):
        # 单行与多行 <!-- --> 注释都应被跳过，不进入渲染结果
        single = web._render_md("<!-- 部署者模板说明 -->\n\n正文。")
        self.assertNotIn("<!--", single)
        self.assertNotIn("部署者模板说明", single)
        self.assertIn("正文。", single)

        multi = web._render_md(
            "<!--\n第一行说明\n第二行说明\n-->\n\n# 标题"
        )
        self.assertNotIn("<!--", multi)
        self.assertNotIn("第一行说明", multi)
        self.assertIn("<h1>标题</h1>", multi)

    def test_repo_templates_render_fallback_without_comment_leak(self):
        # 仓库内两份文档是空模板：渲染应快速返回占位文案，且绝不泄漏 <!-- 注释
        for doc in ("PRIVACY_POLICY.md", "USER_AGREEMENT.md"):
            html = render_with_timeout(doc)
            self.assertIn("该文档尚未发布", html, doc)
            self.assertNotIn("<!--", html, doc)

    def test_read_unknown_doc_returns_fallback(self):
        html = web._read_doc_html("未知.md")
        self.assertIn("<p>未知文档。</p>", html)

    def test_render_link_scheme_whitelist(self):
        # 协议白名单回归（0.21.2 审查发现）：javascript:/data:/vbscript: 链接降级为纯文本，
        # 不得输出可执行 href；http/https/mailto 正常渲染。
        blocked = web._render_md("[点我](javascript:alert(1))")
        self.assertNotIn("href", blocked, "javascript: 链接不应输出 href")
        self.assertIn("点我", blocked, "链接文本应以纯文本保留")

        blocked_data = web._render_md("[img](data:text/html,<script>alert(1)</script>)")
        self.assertNotIn('href="data:', blocked_data)
        self.assertNotIn("<script>", blocked_data, "data: 负载中的标签必须被转义")

        blocked_vb = web._render_md("[x](vbscript:msgbox(1))")
        self.assertNotIn('href="vbscript:', blocked_vb)

        ok_https = web._render_md("[链接](https://example.com/a?x=1&y=2)")
        self.assertIn('<a href="https://example.com/a?x=1&amp;y=2" target="_blank" rel="noopener">链接</a>', ok_https)

        ok_mailto = web._render_md("[邮箱](mailto:admin@example.com)")
        self.assertIn('<a href="mailto:admin@example.com"', ok_mailto)

    def test_local_full_docs_render_if_present(self):
        # 完整版仅供运营者本地/服务器使用（deploy-local/ 被 gitignore，CI 上不存在时直接跳过）
        full = os.path.join(DEPLOY_LOCAL, "PRIVACY_POLICY.md")
        if not os.path.exists(full):
            return
        html = web._render_md(open(full, "r", encoding="utf-8").read())
        self.assertIn("AES-GCM", html)
        self.assertIn("scrypt", html)
        self.assertIn("nginx", html)
        self.assertIn("14 周岁", html)

    def test_doc_page_wrapper(self):
        page = web._doc_page("用户协议", "<p>正文</p>")
        self.assertIn("<h1>用户协议</h1>", page)
        self.assertIn("<p>正文</p>", page)
        self.assertIn('href="/login"', page)
        self.assertIn("返回登录页", page)

    def test_unclosed_comment_skips_only_start_line(self):
        # 0.21.2 审查修复：未闭合 <!-- 只跳过起始行，其后正文必须继续渲染（此前会整份吞掉）
        html = web._render_md("<!-- 说明未闭合\n\n正文仍然可见。\n\n# 标题")
        self.assertIn("<p>正文仍然可见。</p>", html)
        self.assertIn("<h1>标题</h1>", html)
        self.assertNotIn("<!--", html)

    def test_single_line_comment_skips_comment_only(self):
        # 单行 <!-- ... --> 注释整行跳过；闭合块后的正文正常
        html = web._render_md("<!-- 单行说明 -->\n\n正文。")
        self.assertIn("<p>正文。</p>", html)
        self.assertNotIn("单行说明", html)

    def test_doc_page_icp_block(self):
        # 0.21.2 审查修复：#7 独立协议页显示备案信息（配置时）
        page = web._doc_page("隐私政策", "<p>正文</p>", "京ICP备00000000号-1")
        self.assertIn('<p class="doc-icp">京ICP备00000000号-1</p>', page)
        page_empty = web._doc_page("隐私政策", "<p>正文</p>", "")
        self.assertNotIn('class="doc-icp"', page_empty)

    def test_doc_html_cached_and_invalidated(self):
        # 0.21.2 审查修复：#6 渲染结果按 (mtime, size) 缓存；修改文件后 key 变化自动失效。
        # 仓库模板为空模板（渲染结果恒为占位），故用缓存 key 而非渲染内容判断失效。
        target = os.path.join(BASE, "USER_AGREEMENT.md")
        with open(target, "r", encoding="utf-8") as f:
            orig = f.read()
        try:
            web._doc_cache.clear()
            web._read_doc_html("USER_AGREEMENT.md")
            self.assertIn("USER_AGREEMENT.md", web._doc_cache, "首次渲染应写入缓存")
            key1 = web._doc_cache["USER_AGREEMENT.md"][0]
            web._read_doc_html("USER_AGREEMENT.md")
            self.assertEqual(web._doc_cache["USER_AGREEMENT.md"][0], key1, "未变更时应命中缓存")
            with open(target, "a", encoding="utf-8") as f:
                f.write("\n<!-- 临时增量注释 -->\n")
            web._read_doc_html("USER_AGREEMENT.md")
            key2 = web._doc_cache["USER_AGREEMENT.md"][0]
            self.assertNotEqual(key1, key2, "文件变更后缓存 key 应变化并重新渲染")
        finally:
            with open(target, "w", encoding="utf-8") as f:
                f.write(orig)


if __name__ == "__main__":
    unittest.main()
