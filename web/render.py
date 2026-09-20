# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""页面渲染层：合规文档 Markdown 渲染与站点展示配置读取。

**功能**
把仓库根目录的合规文档（用户协议 / 隐私政策）渲染成 HTML 片段或独立页面
（`_render_md` / `_read_doc_html` / `_doc_page`），并提供站点展示族的取值：
备案信息、分享摘要/配图、签到窗口前后裁剪秒数，以及注册邮箱域名的可用性审查。

**归属**
`web/app.py` 的原模块级渲染与展示辅助，唯一真源在本模块；`web/app.py` 只保留
转发（`# noqa: F401` 名字面 + 少量注入胶水），不再持有实现。

**复用**
Markdown 渲染子集只被合规文档族共用（`_inline_md` 是 `_render_md` 的行内格式唯一入口）；
站点展示族被页面路由与模板上下文共用，`edge_front_sec` 是 `edge_config` 的便捷入口。

**通信**
本模块不反向导入 `web.app`（本仓测试以别名加载 `app.py`，普通 import 会再执行一份
副本模块）。读 `web.app` 模块级状态的三项——`.env` 路径、文档根 `_REPO_ROOT`、渲染缓存
`_doc_cache`——以显式参数接收：它们可被测试改写、也会随运行方式不同而不同，本模块
另持一份绑定会让改写静默失效，故一律由调用方在调用时刻现取后传入。
"""

import html
import logging
import os
import re
import sys

# 包导入引导：本模块按**文件路径**被直接导入时（`web/app.py` 的别名加载、部署入口的
# 独立探针等），`sys.path[0]` 只是该文件所在目录，仓库根与 scripts/ 都不在上面。
# 本模块 `import email_policy`（scripts/ 下的域名审查实现）与 `from yiban import ...`
# 取共享包，故**两段都要**先入 sys.path——缺任一段都会在无引导环境里 ModuleNotFoundError。
# 已存在则不重复插入。本模块**刻意不叫 `_REPO_ROOT`**：该名字是 web.app 注入本模块的
# 文档根参数名，render 拆分契约禁止本模块持有同名绑定（防「另存一份绑定」以更隐蔽的
# 形态回归），引导元变量因此另起一名。
_PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_PACKAGE_ROOT, "scripts"), _PACKAGE_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import email_policy  # noqa: E402

from yiban import window as yb_window  # noqa: E402

# 与 web.app 同名的日志通道：合规文档渲染失败的告警落回既有通道，便于运维沿用同一处过滤
logger = logging.getLogger("web")

# 合规文档（隐私政策 / 用户协议）渲染：从仓库根目录的 .md 文件读取并转为 HTML，
# 供注册页弹窗与 /privacy、/terms 独立页共用，避免多份副本漂移。
_DOC_FILES = {"USER_AGREEMENT.md", "PRIVACY_POLICY.md"}


# 行内链接协议白名单（防存储型 XSS）：javascript:/data:/vbscript: 等协议一律降级为纯文本。
# 文档由部署者维护，但内容常从第三方模板/网文粘贴，协议不校验会把可执行链接投放到公开页面。
_SAFE_LINK_SCHEMES = ("http://", "https://", "mailto:")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _inline_md(text):
    """行内格式：先转义 HTML，再处理 **粗体**、`代码`、[文本](链接)。"""
    s = html.escape(text, quote=True)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)

    def _safe_link(m):
        # 仅放行 http/https/mailto；其余（javascript:/data:/vbscript: 等）按纯文本渲染，
        # 不输出 href，避免把可执行协议注入 <a> 标签。
        inner, url = m.group(1), m.group(2)
        if url.strip().lower().startswith(_SAFE_LINK_SCHEMES):
            return f'<a href="{url}" target="_blank" rel="noopener">{inner}</a>'
        return inner

    s = _LINK_RE.sub(_safe_link, s)
    return s


def _render_md(md_text):
    """极简 Markdown → HTML（仅支持本项目合规文档用到的子集，无第三方依赖）。

    支持：#~#### 标题、--- 分隔线、> 引用、有序/无序列表、- 段落合并。
    """
    lines = md_text.split("\n")
    out, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        if line.strip() == "":
            i += 1
            continue
        if line.lstrip().startswith("<!--"):
            # 跳过 HTML 注释：部署者模板说明留在文件中供编辑者阅读，但不渲染到页面。
            # 未闭合（到文件末尾仍无 -->）时只跳过注释起始行并告警，正文继续渲染——
            # 避免少写一个 --> 导致其后全部正文被吞、整份文档静默回退"尚未发布"。
            if "-->" in line:
                i += 1  # 单行注释
                continue
            j = i
            while j < n and "-->" not in lines[j]:
                j += 1
            if j >= n:
                logger.warning("合规文档存在未闭合的 <!-- 注释（起始行 %d），仅跳过该行", i + 1)
                i += 1
            else:
                i = j + 1  # 多行注释：跳过整块（含闭合行）
            continue
        if line.strip() == "---":
            out.append("<hr>")
            i += 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline_md(m.group(2))}</h{lvl}>")
            i += 1
            continue
        if line.lstrip().startswith(">"):
            buf = []
            while i < n and lines[i].lstrip().startswith(">"):
                buf.append(lines[i].lstrip()[1:].strip())
                i += 1
            out.append("<blockquote>" + _inline_md(" ".join(s for s in buf if s)) + "</blockquote>")
            continue
        if re.match(r"^[-*]\s+", line):
            items = []
            while i < n and re.match(r"^[-*]\s+", lines[i]):
                items.append("<li>" + _inline_md(re.sub(r"^[-*]\s+", "", lines[i])) + "</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        if re.match(r"^\d+\.\s+", line):
            items = []
            while i < n and re.match(r"^\d+\.\s+", lines[i]):
                items.append("<li>" + _inline_md(re.sub(r"^\d+\.\s+", "", lines[i])) + "</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue
        para = []
        while (
            i < n
            and lines[i].strip() != ""
            and lines[i].strip() != "---"
            and not lines[i].lstrip().startswith(">")
            and not re.match(r"^(?:#{1,4})\s+", lines[i])
            and not re.match(r"^[-*]\s+", lines[i])
            and not re.match(r"^\d+\.\s+", lines[i])
        ):
            para.append(_inline_md(lines[i]))
            i += 1
        out.append("<p>" + " ".join(para) + "</p>")
    return "\n".join(out)


def _read_doc_html(filename, repo_root, cache):
    """读取仓库根目录的合规文档并渲染为 HTML；缺失/空模板/出错均回退兜底提示。

    渲染结果按 (mtime_ns, size) 缓存在调用方传入的 cache 里：登录页为公开高频入口，
    每次请求读盘+全量正则渲染会放大 I/O 与 DoS 面；部署者更新文件后 key 变化自动失效。
    repo_root / cache 由调用方按调用时刻传入——它们是 web.app 上可被测试或运行方式
    改写的模块级名字，另持绑定会让改写静默失效。
    """
    if filename not in _DOC_FILES:
        return "<p>未知文档。</p>"
    path = os.path.join(repo_root, filename)
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
        cached = cache.get(filename)
        if cached is not None and cached[0] == key:
            return cached[1]
        with open(path, "r", encoding="utf-8") as f:
            rendered = _render_md(f.read())
        # 模板未填（只剩注释/空白）时回退中性占位文案，不回显面向编辑者的开发注释
        if not re.sub(r"<[^>]+>", "", rendered).strip():
            out = "<p>该文档尚未发布，请联系运营者。</p>"
        else:
            out = rendered
        cache[filename] = (key, out)
        return out
    except Exception as exc:  # 文件缺失/编码异常不应拖垮页面
        logger.warning("读取合规文档失败 %s: %s", filename, exc)
        return "<p>文档暂时无法加载，请联系运营者。</p>"


def _doc_page(title, body_html, icp_text="", police_text="", base_path="", police_link="https://beian.mps.gov.cn/", description=""):
    """把渲染后的合规文档包成独立 HTML 页面（footer / 链接用）。
    base_path：挂载前缀（子路径部署如 /tools/yiban-auto-sign/demo，根路径为空串），
    由调用方（路由内 request.script_root）传入，避免本函数脱离请求上下文时访问 request。
    description：分享/搜索摘要（调用方负责在留空时取 site_description() 默认文案）。

    （反射型 XSS 防护）：base_path 来自 request.script_root——攻击者可构造
    形如 /x"><script>…/privacy 的任意前缀路径，未转义时脚本原样落进 href 与正文；
    icp/police 文本与 police_link 均来自 .env，含引号/尖括号时同样破坏 HTML 结构。
    四者统一
    html.escape(quote=True)（同时覆盖文本与属性两种上下文）后才拼入模板，
    转义收敛在本函数内，调用点（含传 request.script_root 的两处）无需各自处理。"""
    base_path = html.escape(str(base_path), quote=True)
    icp_text = html.escape(str(icp_text), quote=True)
    police_text = html.escape(str(police_text), quote=True)
    police_link = html.escape(str(police_link), quote=True)
    # 摘要同样来自 .env（可配置），与上面四项同口径转义后才进 content 属性
    desc_attr = html.escape(str(description), quote=True)
    icp_block = f'<p class="doc-icp"><a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener">{icp_text}</a></p>' if icp_text else ""
    police_block = f'<p class="doc-icp"><a href="{police_link}" target="_blank" rel="noopener"><img src="{base_path}/gongan-beian.png" alt="" width="12" height="14" style="vertical-align:-2px;margin-right:4px"> {police_text}</a></p>' if police_text else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- 站标与主外壳同源：独立页（/terms /privacy）从新标签打开，此前无 icon 而不显示标签页图标 -->
<link rel="icon" type="image/png" href="{base_path}/favicon.png">
<link rel="apple-touch-icon" href="{base_path}/favicon.png">
<title>{title} · 易班自动签到</title>
<meta name="description" content="{desc_attr}">
<meta property="og:title" content="{title} · 易班自动签到">
<meta property="og:description" content="{desc_attr}">
<style>
  /* 协议/隐私文档页（Tailwind 默认配色；卡片容器与圆角为结构优化，随图标/圆角体系保留） */
  /* 独立内联页不引 app.css，字体用系统中文栈 */
  body {{ font-family: system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         max-width: 800px; margin: 40px auto; padding: 0 16px; color: #18181b; line-height: 1.75;
         background: #fafafa; }}
  .doc-card {{ background: #ffffff; border: 1px solid #e4e4e7; border-radius: 14px;
               padding: 32px 36px; box-shadow: 0 1px 2px rgba(0,0,0,.04), 0 6px 20px -6px rgba(0,0,0,.08); }}
  h1 {{ font-size: 26px; margin-bottom: 8px; }}
  h2 {{ font-size: 19px; margin-top: 30px; border-left: 4px solid #2563eb; padding-left: 10px;
       border-radius: 2px; }}
  h3 {{ font-size: 16px; margin-top: 22px; }}
  h4 {{ font-size: 15px; }}
  a {{ color: #2563eb; }}
  blockquote {{ border-left: 3px solid #d4d4d8; margin: 14px 0; padding: 6px 14px;
               color: #52525b; background: #fafafa; border-radius: 8px; }}
  code {{ background: #f4f4f5; padding: 1px 5px; border-radius: 5px; font-size: 0.92em; }}
  hr {{ border: none; border-top: 1px solid #e4e4e7; margin: 28px 0; }}
  .doc-back {{ margin-top: 36px; padding-top: 16px; border-top: 1px solid #e4e4e7; }}
  .doc-icp {{ text-align: center; color: #a1a1aa; font-size: 12px; margin-top: 8px; }}
</style>
<script>
  // 长文档页默认从顶部开始：关闭浏览器滚动位置记忆，页面每次出现（含回退/bfcache 恢复）都回到顶部
  history.scrollRestoration = 'manual';
  addEventListener('pageshow', () => window.scrollTo(0, 0));
</script>
</head>
<body>
<div class="doc-card">
<h1>{title}</h1>
{body_html}
<p class="doc-back"><a href="{base_path}/login">&larr; 返回登录页</a></p>
</div>
{icp_block}
{police_block}
</body>
</html>"""


def email_domain_error(email, env):
    """邮箱域名可用性审查（白名单/黑名单）。通过返回 None，否则返回用户可读错误。

    名单配置走 .env：YIBAN_EMAIL_DOMAIN_ALLOWLIST（可选白名单，逗号分隔，
    非空则仅名单内域名可注册）、YIBAN_EMAIL_DOMAIN_BLOCKLIST_EXTRA（追加
    黑名单）。内置保留域名与一次性邮箱域名数据在 email_policy 模块内维护。
    """
    return email_policy.review_email(
        email,
        allowlist=env.get("YIBAN_EMAIL_DOMAIN_ALLOWLIST", ""),
        blocklist_extra=env.get("YIBAN_EMAIL_DOMAIN_BLOCKLIST_EXTRA", ""),
    )


def icp_info(env):
    """网站 ICP 备案信息（可选）：.env 的 YIBAN_ICP_INFO，留空不显示。

    解耦设计：未配置时模板 `{% if icp_info %}` 块不输出，footer 保持旧样式；
    配置后所有页面底部显示该文本（模板经 Jinja autoescape 转义，无 XSS）。
    """
    return env.get("YIBAN_ICP_INFO", "").strip()


def police_info(env):
    """公安备案信息（可选）：.env 的 YIBAN_POLICE_INFO，留空不显示。

    与 ICP 备案分开独立预留位；未配置时模板 `{% if police_info %}` 块不输出。
    """
    return env.get("YIBAN_POLICE_INFO", "").strip()


def police_link(env):
    """公安备案查询链接（可选）：.env 的 YIBAN_POLICE_LINK。

    备案号属于运营者身份信息，不入仓库：模板与文档页外壳回落到公安部通用
    门户，真实查询链接（含备案号）由部署方在 .env 配置。
    scheme 白名单（复用 _SAFE_LINK_SCHEMES）：该值未经转义直接进公开页
    href，配置 javascript:/data: 等即点击型 XSS——不在白名单（含空值）一律
    回落公安部通用门户。
    """
    link = env.get("YIBAN_POLICE_LINK", "").strip()
    if link and link.lower().startswith(_SAFE_LINK_SCHEMES):
        return link
    return "https://beian.mps.gov.cn/"


def site_description(env, default):
    """站点简介（分享预览/搜索引擎摘要用）：.env 的 YIBAN_SITE_DESCRIPTION 优先。

    全站此前无 meta description 与 og:* 标签，分享链接解析出的卡片只有标题、
    没有摘要。本值经 Jinja autoescape 进 content 属性，无注入面。
    """
    return env.get("YIBAN_SITE_DESCRIPTION", "").strip() or default


def site_image(env):
    """分享预览配图绝对地址（可选）：.env 的 YIBAN_SITE_IMAGE，留空不输出 og:image。

    必须为绝对 https 地址（分享抓取方无法解析相对路径）；不在 https 白名单
    一律忽略——该值进公开页 meta content，配置 javascript:/data: 无意义且有害。
    """
    url = env.get("YIBAN_SITE_IMAGE", "").strip()
    return url if url.lower().startswith("https://") else ""


# 掐头去尾（前后独立，秒级，0.5 分钟=30s 粒度）：
# 新键 YIBAN_WINDOW_EDGE_FRONT_SEC / _BACK_SEC 优先；旧键 YIBAN_WINDOW_EDGE_SEC（前后对称）
# 存在时映射为 front=back=旧值，保证升级前配置行为不变。范围 0~300 秒。
def edge_config(env):
    """返回 (front_sec, back_sec)：签到窗口前后裁剪秒数（解析见 yiban.window.parse_edges）。"""
    return yb_window.parse_edges(env)


def edge_front_sec(env):
    """前裁秒数（兼容旧调用的便捷入口）。"""
    return edge_config(env)[0]
