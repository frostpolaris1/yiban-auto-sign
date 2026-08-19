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


if __name__ == "__main__":
    unittest.main()
