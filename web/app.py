#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""易班自动签到网页管理系统（服务器端）。

在浏览器中替代 TUI 面板：管理员登录后，可在任意设备（手机/平板/电脑）
查看和管理签到任务。功能与 TUI 对齐：

- 账号管理：列表 / 添加 / 编辑 / 删除 / 排序（决定顺序打卡顺序）
- 签到日志：解析 sign.log 展示最近记录与今日各账号状态图标
- 手动签到：单账号后台执行 scripts/signin.py --only
- 系统设置：随机延迟开关（写入 .env）、连通性检测、服务器时间/签到窗口状态

运行：
    python3 -m web                 # 默认 127.0.0.1:17892（仅回环；生产用 systemd/gunicorn 模板）
    python3 -m web --port 9000     # 自定义端口

管理员账号：首次启动自动生成 SECRET_KEY 并写入 .env；
在 .env 配置 YIBAN_ADMIN_USER / YIBAN_ADMIN_PASSWORD 后即可登录。
"""

import argparse
import calendar
import contextlib
import hashlib
import html
import ipaddress
import json
import logging
import os
import random
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

import requests
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

# 共享模块（web/ 与 scripts/ 同级）：加密模块 + SQLite 数据访问层 + 子进程环境构造
_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# 合规文档（隐私政策 / 用户协议）渲染：从仓库根目录的 .md 文件读取并转为 HTML，
# 供注册页弹窗与 /privacy、/terms 独立页共用，避免多份副本漂移。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
            # 避免少写一个 --> 导致其后全部正文被吞、整份文档静默回退"尚未发布"（0.21.2 审查修复）。
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


# 合规文档渲染缓存（0.21.2 审查修复）：登录页为公开高频入口，每次请求读盘+全量正则渲染
# 会放大 I/O 与 DoS 面。按 (mtime_ns, size) 缓存，部署者更新文件后自动失效。
_doc_cache = {}  # filename -> ((mtime_ns, size), html)


def _read_doc_html(filename):
    """读取仓库根目录的合规文档并渲染为 HTML；缺失/空模板/出错均回退兜底提示。"""
    if filename not in _DOC_FILES:
        return "<p>未知文档。</p>"
    path = os.path.join(_REPO_ROOT, filename)
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
        cached = _doc_cache.get(filename)
        if cached is not None and cached[0] == key:
            return cached[1]
        with open(path, "r", encoding="utf-8") as f:
            rendered = _render_md(f.read())
        # 模板未填（只剩注释/空白）时回退中性占位文案，不回显面向编辑者的开发注释
        if not re.sub(r"<[^>]+>", "", rendered).strip():
            out = "<p>该文档尚未发布，请联系运营者。</p>"
        else:
            out = rendered
        _doc_cache[filename] = (key, out)
        return out
    except Exception as exc:  # 文件缺失/编码异常不应拖垮页面
        logger.warning("读取合规文档失败 %s: %s", filename, exc)
        return "<p>文档暂时无法加载，请联系运营者。</p>"


def _doc_page(title, body_html, icp_text="", police_text="", base_path=""):
    """把渲染后的合规文档包成独立 HTML 页面（footer / 链接用）。
    base_path：挂载前缀（子路径部署如 /tools/yiban-auto-sign/demo，根路径为空串），
    由调用方（路由内 request.script_root）传入，避免本函数脱离请求上下文时访问 request。"""
    icp_block = f'<p class="doc-icp"><a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener">{icp_text}</a></p>' if icp_text else ""
    police_block = f'<p class="doc-icp"><a href="https://beian.mps.gov.cn/#/query/webSearch?code=32110202000847" target="_blank" rel="noopener"><img src="/gongan-beian.png" alt="" width="12" height="14" style="vertical-align:-2px;margin-right:4px"> {police_text}</a></p>' if police_text else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - 易班自动签到</title>
<style>
  /* 协议/隐私文档页（Tailwind 默认配色；卡片容器与圆角为结构优化，随图标/圆角体系保留） */
  /* 正文原版字体栈；标题不使用专属字体（2026-08-22 性能回退，与 web/templates 一致） */
  body {{ font-family: "MiSans", system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
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
import db  # noqa: E402
import child_env  # noqa: E402
import email_policy  # noqa: E402  邮箱域名黑白名单审查：注册写入前拦截占位/一次性域名
import env_lock  # noqa: E402
import mailer  # noqa: E402  # A 线：管理员告警邮件（SMTP，零依赖；不配置则不启用）
import signin  # noqa: E402  # 探针/注册验证：只读健康检查（登录+拉任务，不提交签到）

# 默认路径（与 tui/app.py / run.sh 保持一致，可用参数覆盖）
ACCOUNTS_DEFAULT = os.environ.get("YIBAN_ACCOUNTS_FILE", "accounts.json")
# 按日状态文件目录（signin.py 写入 sign-daily-YYYY-MM-DD.json，网页日历读取）
STATE_DIR_DEFAULT = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
LOG_DEFAULT = os.environ.get("YIBAN_LOG_FILE", "/var/log/yiban/sign.log")
ENV_DEFAULT = os.environ.get("YIBAN_ENV_FILE", ".env")
DB_DEFAULT = os.environ.get("YIBAN_DB_FILE", "yiban.db")
# 模块级路径（gunicorn 走 create_app() 不执行 main()，需在此初始化；main() 用 --config 等参数覆盖）
ACCOUNTS_FILE = ACCOUNTS_DEFAULT  # 仅作 JSON→SQLite 自动迁移来源（迁移后改名 .bak；users.json 同目录推断，无需单独路径）
LOG_FILE = LOG_DEFAULT
ENV_FILE = ENV_DEFAULT
STATE_DIR = STATE_DIR_DEFAULT
DB_FILE = DB_DEFAULT

# 普通用户账号的审核状态（2026-08-16 审查轮：原 STATUS_PENDING/ACTIVE/REJECTED 与签到状态码
# STATUS_* 同名异义（历史遗留），改名为 ACCOUNT_STATUS_* 彻底分离命名空间）
ACCOUNT_STATUS_PENDING = "pending"  # 待审核（不参与定时签到）
ACCOUNT_STATUS_ACTIVE = "active"  # 已生效（参与定时签到）
ACCOUNT_STATUS_REJECTED = "rejected"  # 已拒绝（附理由，用户可编辑重新提交）

# 软删除保留期（天）：管理员删除的账号进入待删除状态，超期自动彻底清除。
# 唯一来源在 db.py（SOFT_DELETE_RETENTION_DAYS），此处仅引用防双源漂移（2026-08-15 审查）
DELETED_RETENTION_DAYS = db.SOFT_DELETE_RETENTION_DAYS

# 密码策略：至少 10 位且包含大写/小写/数字/符号中至少两类（只对新建/修改生效，存量密码不受影响）
PASSWORD_MIN_LEN = 10
# 口令哈希算法（werkzeug scrypt，OWASP 推荐参数；check_password_hash 对旧哈希自动兼容）
SCRYPT_METHOD = "scrypt:65536:8:1"

# 账号编辑时识别码清空哨兵值（收到该值 = 显式删除设备识别码字段）
CLEAR_SENTINEL = "__clear__"

# 单次批量操作上限（2026-08-29 由 100 收紧为 10）：批量通过/删除/设管理员/重置密码
# 与「清除已注销用户」共用同一上限——被盗管理员会话即使一个请求，一次最多影响 10 条，
# 降低误操作与滥用影响范围。三处接口共用本常量，防单处调整后其他路径遗漏（批次7 A2/A3 收紧）。
BATCH_OP_LIMIT = 10

# 状态图标（与 tui/app.py 一致；前端渲染使用，后端仅用于日志解析）
SIGN_START = (6, 30)
SIGN_END = (7, 50)


def _sign_window():
    """签到窗口（调度 v2：支持 .env 覆盖 YIBAN_SIGN_START/END，非法回退默认）。"""
    start = SIGN_START
    end = SIGN_END
    env = read_env(ENV_FILE)
    for key in ("YIBAN_SIGN_START", "YIBAN_SIGN_END"):
        raw = env.get(key, "").strip()
        try:
            h, m = raw.split(":")
            parsed = (int(h), int(m))
            if 0 <= parsed[0] <= 23 and 0 <= parsed[1] <= 59:
                if key.endswith("START"):
                    start = parsed
                else:
                    end = parsed
        except (ValueError, AttributeError):
            pass
    if start >= end:
        start, end = SIGN_START, SIGN_END
    return start, end

# 登录时延拉平占位哈希：用户名/账号不存在时也执行一次等价 scrypt 比对，
# 消除「响应耗时差异」造成的用户枚举时序侧信道（占位哈希无需真实有效，比对恒为 False）。
_dummy_pw_hash = None


def _constant_time_dummy(password):
    """对不存在的账号执行一次与真实校验等价的 scrypt 比对（耗时拉平）。"""
    global _dummy_pw_hash
    if _dummy_pw_hash is None:
        _dummy_pw_hash = generate_password_hash("dummy-placeholder", method=SCRYPT_METHOD)
    check_password_hash(_dummy_pw_hash, password)


# 可信第一跳代理（nginx 反代）：仅当请求来自这些地址时才信任转发头。
# 生产部署：yiban-web 监听 127.0.0.1，nginx 反代并以 `proxy_set_header X-Forwarded-For $remote_addr`
# 覆盖设置（客户端伪造的 XFF 会被丢弃），故此处读取的 XFF 即真实客户端 IP。
TRUSTED_PROXIES = ("127.0.0.1", "::1")

# 启动断言：TRUSTED_PROXIES 必须仅为回环地址，防止配置被改为非回环地址导致 XFF 伪造绕过速率限制
assert all(p in ("127.0.0.1", "::1", "localhost") for p in TRUSTED_PROXIES), \
    f"TRUSTED_PROXIES 必须仅为回环地址，当前值: {TRUSTED_PROXIES}"


def _stale_idx_guard(acc, data):
    """防错位校验（2026-08-20 对抗性审查 P1）：mutation 按 idx 寻址时，客户端
    携带的 phone 与服务端 idx 解析结果不一致 → 账号列表在视图快照后已漂移
    （并发删除/移动等），放行会静默操作错误对象。返回 True 表示错位，调用方
    应返回 409 引导刷新。未携带 phone 的请求（旧客户端/测试）保持兼容不校验。

    比对前双侧 _mask_phone 归一：/api/accounts 出站即脱敏（mask_account），
    浏览器回传的是 138****8000 形态；_mask_phone 幂等（含 * 原样返回），直连
    API 发全号的旧客户端/测试同样归一可比；伪造他人号码仍因不等被拦。
    """
    phone = data.get("phone") if isinstance(data, dict) else None
    if phone is None:
        return False
    return _mask_phone(str(phone).strip()) != _mask_phone(str(acc.get("phone", "")))


def _json_body():
    """安全解析 JSON 请求体：
    - 空 body → {}
    - 非法 JSON / 非对象 JSON → 400（API 语义清晰，不静默按空请求处理）
    """
    if not request.data:
        return {}
    data = request.get_json(silent=True)
    if data is None:
        abort(400, description="请求体不是合法 JSON")
    if not isinstance(data, dict):
        abort(400, description="请求体应为 JSON 对象")
    return data


def _client_ip():
    """真实客户端 IP（限速/锁定/审计按真实 IP 隔离，防反代后全站共享同一桶）。

    - 反代场景：remote_addr 为代理地址且第一跳可信 → 取 X-Forwarded-For 首个值（nginx 已覆盖，不可伪造）；
    - 直连场景（无转发头/首跳不可信）：回退 remote_addr。
    注意：本函数假设应用不直接暴露公网（17892 仅监听回环 + 防火墙放行 22/443）。
    """
    r = request.remote_addr or "?"
    if r in TRUSTED_PROXIES:
        xff = request.headers.get("X-Forwarded-For", "")
        first = xff.split(",")[0].strip() if xff else ""
        if first and first != r:
            return first
    return r


def _delete_grace_remaining(deleted_at):
    """注销冷却剩余秒数：deleted_at + 宽限期 − now；非冷却中（无时间/已过期/解析失败）返回 0。

    登录即恢复（2026-08-16 用户裁决）与已注销用户视图共用此判定。
    兼容两种格式：主格式 strftime("%Y-%m-%d %H:%M:%S")（新写入），存量 ISO 格式自动回退。
    """
    if not deleted_at:
        return 0
    try:
        d = datetime.strptime(deleted_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            d = datetime.fromisoformat(str(deleted_at))
        except (ValueError, TypeError):
            return 0
    remain = (d + timedelta(days=DELETE_GRACE_DAYS)) - datetime.now()
    return remain.total_seconds() if remain.total_seconds() > 0 else 0

# 随机延迟默认上限（与 signin.py 一致）
DEFAULT_START_DELAY_MAX = 60
DEFAULT_ACCOUNT_GAP_MAX = 10

# 登录失败限速：同一 IP 连续失败超过阈值后锁定
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 300
# 批次7 P3-8：账号恢复的每 IP 聚合失败窗口（跨邮箱喷洒防护——单邮箱 5 次锁定
# 只约束单账号，攻击者可换邮箱继续；命中恢复即接管该账号与其易班凭据）
RESTORE_FAIL_MAX = 30
RESTORE_FAIL_WINDOW = 600
# 连续失败告警阈值：达到后通过 YIBAN_NOTIFY_URL 通知管理员（每轮锁定只告警一次）
LOGIN_FAIL_NOTIFY = 3

# IP 计数 dict（限速/登录失败/注册）的条目上限与最长保留：防公网扫描器多 IP 打爆内存
_IP_STORE_LIMIT = 10000
_IP_STORE_MAX_AGE = 3600

# API 请求限速（防脚本轰炸）：每 IP 窗口内最多 RATE_MAX 次 /api/* 请求
RATE_WINDOW = 10  # 窗口（秒）
RATE_MAX = 60  # 窗口内最大 API 请求数（正常用户远低于此）
# 注册限速（防邮箱批量注册）：每 IP 窗口内最多 REGISTER_MAX 次成功注册
REGISTER_WINDOW = 600  # 窗口（秒）= 10 分钟
REGISTER_MAX = 5  # 窗口内最大成功注册数

# 账号验证尝试限频（2026-08-27 P1-2）：每用户窗口内网络验证次数上限。
# 预验证 = 服务器代发真实易班登录，必须在资格预筛之外再加用户维度节流。
VERIFY_MAX = 6  # 每用户窗口内最大验证尝试次数（正常添加流程远用不到）
VERIFY_WINDOW = 600  # 窗口（秒）= 10 分钟

# 注销账号冷却（防批量注销，user_delete_requests 表计数，v5）：
# 每用户 60 秒内最多 1 次、每 IP 60 秒内最多 DELETE_MAX_REQUESTS_PER_IP 次；
# 超限返回 429 且不暴露冷却秒数（信息分层，防恶意用户据此规划批量节奏）
DELETE_COOLDOWN_SEC = 60

# 会话绝对过期上限默认天数（2026-08-27 P2-5）：实际值在 create_app 内按
# YIBAN_SESSION_ABS_DAYS 解析并钳制到 [1,30]；此处为 create_app 前引用兜底。
SESSION_ABS_DAYS_DEFAULT = 7
SESSION_ABS_TTL_SECONDS = SESSION_ABS_DAYS_DEFAULT * 86400
DELETE_MAX_REQUESTS_PER_IP = 5
# 注销宽限期（天）：软删除冷却期，与账号软删除保留期对齐（7 天，安全审查 2026-08-16）；
# 与 db.purge_deleted_users 默认一致；已注销用户视图按此计算剩余天数
# 2026-08-28 审查 C-1：原实现硬编码 7，与 db.SOFT_DELETE_RETENTION_DAYS（账号保留期
# 唯一事实源）及 db.purge_deleted_users 默认值形成三份互不相干的"7"——运维按注释去调
# SOFT_DELETE_RETENTION_DAYS 时，账号会被提前物理清除而恢复宽限期仍按 7 天，用户点
# 恢复会看到"成功"实际账号已消失（静默数据丢失）。现统一取同一常量，并加启动自检
# 防再次漂移（对齐 TRUSTED_PROXIES 的 assert 惯例）。
DELETE_GRACE_DAYS = db.SOFT_DELETE_RETENTION_DAYS
assert DELETE_GRACE_DAYS == db.SOFT_DELETE_RETENTION_DAYS, (
    f"DELETE_GRACE_DAYS 必须与 db.SOFT_DELETE_RETENTION_DAYS 同源: "
    f"{DELETE_GRACE_DAYS} != {db.SOFT_DELETE_RETENTION_DAYS}"
)

# 容量上限（2026-08-15 对抗性审查补：注册/使用人数超负载兜底）：
# 注册用户上限默认 200（一人一号 ≈ 200 账号，远超班级/社团规模）；账号总数上限默认 500
# （调度窗口 80min ÷ 单账号平均 8s ≈ 600 理论上限，留裕量防 web 解密/轮询劣化）。
# 0 = 不限。可用 .env 的 YIBAN_MAX_USERS / YIBAN_MAX_ACCOUNTS 调整。
DEFAULT_MAX_USERS = 200
DEFAULT_MAX_ACCOUNTS = 500

# 自选时间片切换冷却（2026-08-15 用户反馈 → 弹性冷却）：
# 60 秒窗口内前 TIME_PREF_COOLDOWN_FREE 次切换完全自由（浏览式"全点一遍再定"属正常行为）；
# 超出后冷却递增：基础 × 2^(超限次数)，封顶 TIME_PREF_COOLDOWN_MAX（持续高频才被压制）。
# 高频切换本质是自我惩罚（updated_at 变晚 → 先到先得排后），冷却只为防连点/防刷屏噪音。
# 0 = 关闭。可用 .env 的 YIBAN_TIME_PREF_COOLDOWN_SEC 调整基础值（默认 30）。
TIME_PREF_COOLDOWN_SEC = 30
TIME_PREF_COOLDOWN_FREE = 20        # 60 秒窗口内自由切换次数（覆盖"全点一遍"16 片+选定）
TIME_PREF_COOLDOWN_MAX = 300        # 弹性封顶（秒）
TIME_PREF_COOLDOWN_WINDOW = 60      # 计数窗口（秒）

# 暂停签到冷却（2026-08-16 调整）：恢复不受限；暂停采用弹性冷却——
# 60 秒窗口内前 PAUSE_COOLDOWN_FREE 次完全自由（好奇地暂停/恢复/再暂停不会被误杀），
# 超出后冷却递增（基础 × 2^(超限次数)，封顶 PAUSE_COOLDOWN_MAX）。
# 防脚本刷审计/状态显示抖动，但不惩罚正常手快用户。0=关闭。
PAUSE_COOLDOWN_SEC = 30
PAUSE_COOLDOWN_FREE = 3         # 60 秒窗口内自由暂停次数（覆盖"试一下"）
PAUSE_COOLDOWN_MAX = 120        # 弹性封顶（秒）
PAUSE_COOLDOWN_WINDOW = 60      # 计数窗口（秒）

# 普通用户邮箱格式校验（用户名部分（@ 前）限 32 字符：防超长用户名破坏界面显示）
# 批次7 P4：re.ASCII——str 模式的 \w 匹配 Unicode 字母，同形字/IDN 域名可绕过
# 一次性域名黑名单的字面匹配；限 ASCII 后此类注册直接被格式校验拦截
EMAIL_RE = re.compile(r"^[\w.+-]{1,32}@[\w-]+(\.[\w-]+)+$", re.ASCII)
EMAIL_USER_MAX = 32  # 邮箱用户名部分（@ 前）最大长度
# 手机号格式（易班登录账号为中国 11 位手机号；恶意字符可注入前端事件与日志）
PHONE_RE = re.compile(r"^1\d{10}$")

# 手动签到防抖：同一账号两次触发的最小间隔（秒）
SIGN_MIN_INTERVAL = 30  # 手动签到防抖窗口（秒）；注释口径见 _spawn_signin docstring

# 日志格式（与 signin.py / tui/app.py 相同）
# 行格式: [2026-08-07 06:40:04] [INFO] yiban: [手机号] ✅ 签到成功
SIGN_LOG_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}) [\d:]+\] \[(\w+)\] (\w+): (.*)")

# 签到状态码（signin.py 写 sign-state 文件，为状态显示的事实源）与图标/文案映射
STATUS_SUCCESS = "success"
STATUS_ALREADY = "already"
STATUS_NO_TASK = "no_task"
STATUS_FAILED = "failed"
STATUS_RETRYING = "retrying"
STATUS_SKIPPED_WINDOW = "skipped_window"
STATUS_SKIPPED_NORANGE = "skipped_norange"
STATUS_PAUSED = "paused"  # 账密异常暂停（signin 熔断器）
STATUS_USER_CANCELLED = "user_cancelled"  # 用户自暂停签到（调度 v2）
STATUS_PENDING = "pending"  # 待签（未执行/无记录）；账号审核态已改名为 ACCOUNT_STATUS_PENDING（2026-08-16），命名空间已分离

STATUS_ICON = {
    STATUS_SUCCESS: "✅", STATUS_ALREADY: "✅", STATUS_NO_TASK: "➖",
    STATUS_FAILED: "❌", STATUS_RETRYING: "🔄",
    STATUS_SKIPPED_WINDOW: "⛔", STATUS_SKIPPED_NORANGE: "⛔",
    STATUS_PAUSED: "⏸️", STATUS_USER_CANCELLED: "⏹️", STATUS_PENDING: "⏳",
}
STATUS_TEXT = {
    STATUS_SUCCESS: "签到成功", STATUS_ALREADY: "已签到", STATUS_NO_TASK: "无需签到",
    STATUS_FAILED: "签到失败", STATUS_RETRYING: "重试中",
    STATUS_SKIPPED_WINDOW: "时段外", STATUS_SKIPPED_NORANGE: "窗口缺失",
    STATUS_PAUSED: "暂停", STATUS_USER_CANCELLED: "已取消", STATUS_PENDING: "待签",
}


def clear_fuse_pause(phone):
    """账号凭据变更（改密码/编辑）后清除熔断暂停记录，使其立即恢复签到。

    2026-08-15 命名审查：原名 clear_cred_state 误导（"cred"易被理解为清除凭据/密钥，
    实际只删 cred-state.json 里的熔断暂停条目）；现名体现真实行为。
    """
    try:
        path = os.path.join(STATE_DIR, "cred-state.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or phone not in data:
            return
        del data[phone]
        # 唯一临时名：防与 signin 收尾 _save_cred_state 跨进程并发碰撞（对抗性审查 F5）
        tmp = f"{path}.tmp{secrets.token_hex(4)}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except (OSError, ValueError) as e:
        # 留痕（2026-08-27 审查）：裸吞会让"改密后仍暂停"无从排查
        logger.warning("清除账密熔断暂停状态失败，该账号可能仍处暂停: %s [%s]", _mask_phone(phone), e)


def load_sign_state(date_str=None):
    """读取按日结构化状态文件：{phone: {status, message, time, task}}。

    缺失/损坏/目录不存在时回退读旧格式按日文件（sign-daily，符号 → 状态码）：
    覆盖部署过渡期（sign-state 尚未生成）与历史日期查看场景。
    两者都无 → 返回空 dict（前端回退显示待签 ⏳）。
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(STATE_DIR, f"sign-state-{date_str}.json")
    try:
        # utf-8-sig：兼容 Windows 记事本/手工编辑可能写入的 UTF-8 BOM（BOM 会让 json.load 抛错）
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict) and data:
            return data
    except (OSError, ValueError):
        pass
    # 回退：sign-daily（旧版符号 ✅/❌/➖）→ 状态码
    daily_path = os.path.join(STATE_DIR, f"sign-daily-{date_str}.json")
    try:
        with open(daily_path, encoding="utf-8-sig") as f:
            daily = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(daily, dict):
        return {}
    sym_map = {"✅": STATUS_SUCCESS, "❌": STATUS_FAILED, "➖": STATUS_NO_TASK}
    return {
        phone: {"status": sym_map.get(sym, STATUS_PENDING), "message": "", "task": "default"}
        for phone, sym in daily.items()
    }

logger = logging.getLogger("web")


# ---------------------------------------------------------------------------
# 签到日志解析（与 tui/app.py parse_sign_log 保持一致）
# ---------------------------------------------------------------------------
_LOG_TAIL_BYTES = 2 * 1024 * 1024  # 日志倒读上限 2MB（约 2 万行）


def _tail_lines(path, max_bytes=_LOG_TAIL_BYTES):
    """从文件尾部读取最多 max_bytes 的完整文本行：大日志避免整读入内存。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    try:
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # 丢弃首个不完整行
                raw = f.read()
            else:
                raw = f.read()
    except OSError:
        return []
    return raw.decode("utf-8", errors="replace").splitlines()


def parse_sign_log(path):
    """解析签到日志：返回最近日志行列表（yiban 非 DEBUG 行）。

    2026-08-16 审查轮：原返回值 (states, recent) 的 states（日志符号 → 图标）从未被
    正确消费——账号状态的事实源是 sign-state 文件（load_sign_state，/api/accounts），
    日志符号与前端状态码语义不符，曾被 /api/logs 透传污染前端图标/统计卡（历史遗留）。
    现与 tui 同构：仅返回 recent 行。
    """
    recent = []
    for line in _tail_lines(path):
        m = SIGN_LOG_RE.match(line.strip())
        if not m:
            continue
        _date, level, logger_name, _msg = m.groups()
        if logger_name != "yiban" or level == "DEBUG":
            continue
        recent.append(line.strip())
    return recent


def _is_valid_date_str(s):
    """YYYY-MM-DD 格式且为真实日历日期（2026-13-99 这类非法值拒绝）。"""
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def log_path_for(date_str=None):
    """按天日志文件路径：{LOG_FILE 目录}/sign-YYYY-MM-DD.log（date_str 缺省=今天）。

    2026-08-16 日志按天分文件：每天一个文件，按日期查看 = 直接读对应文件；
    run.sh / signin.py / 手动签到子进程均写入当天文件（保留 LOG_FILE 配置的目录）。
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    return os.path.join(os.path.dirname(LOG_FILE), f"sign-{date_str}.log")


def _log_lines_for(date_str):
    """读取指定日期日志的行（行首日期过滤防跨天残留；仅 yiban 非 DEBUG 行）。

    文件缺失/不可读返回空列表（历史日期无日志是正常状态，不报错）。
    """
    prefix = f"[{date_str} "
    out = []
    for line in _tail_lines(log_path_for(date_str)):
        if not line.startswith(prefix):
            continue
        m = SIGN_LOG_RE.match(line.strip())
        if not m:
            continue
        _, level, logger_name, _msg = m.groups()
        if logger_name != "yiban" or level == "DEBUG":
            continue
        out.append(line.strip())
    return out


# 最近日志日期缓存 {date: "YYYY-MM-DD"}：轮询每 10s 调用，避免每次都扫描 30 天文件
_most_recent_log_cache = {"history_date": None, "checked_day": ""}


def _today_has_logs():
    """今天的日志文件是否存在且含 yiban 日志行（每次调用都检查，开销小）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    path = log_path_for(today)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    for line in _tail_lines(path, max_bytes=4096):
        m = SIGN_LOG_RE.match(line.strip())
        if m and m.group(3) == "yiban":
            return True
    return False


def _most_recent_log_date(max_days=30):
    """查找最近有日志的日期（从今天往前最多 max_days 天）。返回 YYYY-MM-DD。

    每次先检查今天（开销小，今天有新日志立即生效）；无日志时用历史缓存（每天只扫一次）。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    # 今天有日志 → 直接返回今天（并更新缓存）
    if _today_has_logs():
        _most_recent_log_cache["history_date"] = today
        return today
    # 今天无日志：跨天重置缓存，重新扫描历史
    if _most_recent_log_cache["checked_day"] != today:
        _most_recent_log_cache["checked_day"] = today
        _most_recent_log_cache["history_date"] = None
        for i in range(1, max_days + 1):
            d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            path = log_path_for(d)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                for line in _tail_lines(path, max_bytes=4096):
                    m = SIGN_LOG_RE.match(line.strip())
                    if m and m.group(3) == "yiban":
                        _most_recent_log_cache["history_date"] = d
                        break
                if _most_recent_log_cache["history_date"]:
                    break
    return _most_recent_log_cache["history_date"] or today


# ---------------------------------------------------------------------------
# .env 读写（与 tui/app.py 保持一致）
# ---------------------------------------------------------------------------
def read_env(env_path):
    """读取 .env 全部键值，返回 dict。

    utf-8-sig：兼容带 BOM 的 .env（Windows 记事本等工具保存时会带 BOM，
    否则首个键名会带上 \ufeff 前缀导致读不到，管理员登录/改密会静默失败）。
    """
    result = {}
    try:
        with open(env_path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    result[key.strip()] = value.strip()
    except OSError:
        pass
    return result


def load_env_int(env_path, key, default):
    """读取 .env 中的整数配置，缺失/非法回退默认值。"""
    try:
        return max(0, int(read_env(env_path).get(key, "")))
    except (TypeError, ValueError):
        return default


def email_domain_error(email):
    """邮箱域名可用性审查（白名单/黑名单）。通过返回 None，否则返回用户可读错误。

    名单配置走 .env：YIBAN_EMAIL_DOMAIN_ALLOWLIST（可选白名单，逗号分隔，
    非空则仅名单内域名可注册）、YIBAN_EMAIL_DOMAIN_BLOCKLIST_EXTRA（追加
    黑名单）。内置保留域名与一次性邮箱域名数据在 email_policy 模块内维护。
    """
    env = read_env(ENV_FILE)
    return email_policy.review_email(
        email,
        allowlist=env.get("YIBAN_EMAIL_DOMAIN_ALLOWLIST", ""),
        blocklist_extra=env.get("YIBAN_EMAIL_DOMAIN_BLOCKLIST_EXTRA", ""),
    )


def icp_info():
    """网站 ICP 备案信息（可选）：.env 的 YIBAN_ICP_INFO，留空不显示。

    解耦设计：未配置时模板 `{% if icp_info %}` 块不输出，footer 保持旧样式；
    配置后所有页面底部显示该文本（模板经 Jinja autoescape 转义，无 XSS）。
    """
    return read_env(ENV_FILE).get("YIBAN_ICP_INFO", "").strip()


def police_info():
    """公安备案信息（可选）：.env 的 YIBAN_POLICE_INFO，留空不显示。

    与 ICP 备案分开独立预留位；未配置时模板 `{% if police_info %}` 块不输出。
    """
    return read_env(ENV_FILE).get("YIBAN_POLICE_INFO", "").strip()


# 掐头去尾（0.22.0 起前后独立，秒级，0.5 分钟=30s 粒度）：
# 新键 YIBAN_WINDOW_EDGE_FRONT_SEC / _BACK_SEC 优先；旧键 YIBAN_WINDOW_EDGE_SEC（前后对称）
# 存在时映射为 front=back=旧值，保证升级前配置行为不变。范围 0~300 秒。
def edge_config():
    """返回 (front_sec, back_sec)：签到窗口前后裁剪秒数。"""
    env = read_env(ENV_FILE)
    old = env.get("YIBAN_WINDOW_EDGE_SEC", "")
    def _get(key):
        try:
            v = int(env.get(key, "").strip())
        except (TypeError, ValueError):
            return None
        return v if 0 <= v <= 300 else None
    front = _get("YIBAN_WINDOW_EDGE_FRONT_SEC")
    back = _get("YIBAN_WINDOW_EDGE_BACK_SEC")
    if front is None or back is None:
        try:
            legacy = int(old) if old.strip() else None
        except ValueError:
            legacy = None
        if legacy is not None and not (0 <= legacy <= 300):
            legacy = None
        if front is None:
            front = legacy if legacy is not None else 60
        if back is None:
            back = legacy if legacy is not None else 60
    return front, back


def edge_front_sec():
    """前裁秒数（兼容旧调用的便捷入口）。"""
    return edge_config()[0]


def write_env_int(env_path, key, value):
    """把整数配置写入 .env：value<=0 删除该行，>0 写入；保留其他行。"""
    write_env_key(env_path, key, str(value) if value > 0 else "")


def write_env_key(env_path, key, value):
    """把任意键值写入 .env：value 为空删除该行，否则写入；保留注释与其他行。

    写锁（_env_write_lock）：并发保存设置/公告时读-改-写互斥，防跨 worker 丢更新。
    安全约束（安全审查 2026-08）：.env 为逐行键值格式，键或值含换行符会注入出
    新的配置行（如经公告文本写入 YIBAN_ADMIN_PASSWORD_HASH 覆盖主管理员哈希提权）。
    此处为兜底硬校验（调用方应先自行校验并返回友好错误），违规直接抛 ValueError。
    """
    if "\n" in key or "\r" in key or "\n" in value or "\r" in value:
        raise ValueError(f"write_env_key 拒绝包含换行符的键值（.env 单行格式）: {key}")
    with _env_write_lock(env_path):
        lines = []
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8-sig") as f:  # utf-8-sig：兼容带 BOM 的 .env
                lines = f.read().splitlines()
        out = [ln for ln in lines if not ln.strip().startswith(f"{key}=")]
        if value:
            out.append(f"{key}={value}")
        _atomic_write(env_path, "\n".join(out) + "\n", chmod_priv=True)


def write_env_batch(env_path, updates):
    """批量写入多个键值（原子操作）：读取一次，修改多个键，写入一次。
    避免多次独立 write_env_key 调用时进程崩溃导致配置不一致。
    updates: dict {key: value}，value 为空字符串则删除该键。"""
    with _env_write_lock(env_path):
        for key, value in updates.items():
            if "\n" in key or "\r" in key or "\n" in value or "\r" in value:
                raise ValueError(f"write_env_batch 拒绝包含换行符的键值: {key}")
        lines = []
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8-sig") as f:
                lines = f.read().splitlines()
        # 保留注释行和非更新键；过滤被更新键的旧行后追加新值
        out = [ln for ln in lines if not ln.strip().startswith(tuple(f"{k}=" for k in updates))]
        for key, value in updates.items():
            if value:
                out.append(f"{key}={value}")
        _atomic_write(env_path, "\n".join(out) + "\n", chmod_priv=True)


def ensure_secret_key(env_path):
    """确保 .env 中存在 YIBAN_SECRET_KEY（缺失时自动生成随机值）。

    .env 不可写时降级为进程内随机密钥并告警（服务可用，重启后会话失效）——
    与 migrate_admin_password_to_hash 的降级策略一致（对抗性审查 F4）。
    """
    with _env_write_lock(env_path):
        env = read_env(env_path)
        key = env.get("YIBAN_SECRET_KEY", "").strip()
        if key:
            return key
        key = secrets.token_hex(32)
        lines = []
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8-sig") as f:  # utf-8-sig：兼容带 BOM 的 .env
                lines = f.read().splitlines()
        if not any(ln.strip().startswith("YIBAN_SECRET_KEY=") for ln in lines):
            lines.append(f"YIBAN_SECRET_KEY={key}")
        try:
            _atomic_write(env_path, "\n".join(lines) + "\n", chmod_priv=True)
        except OSError as e:
            logger.warning(
                "无法写入 %s（%s）：YIBAN_SECRET_KEY 仅本次进程生效（重启后会话将失效），"
                "请修复目录权限或手动配置密钥",
                env_path, e,
            )
            return key
        logger.info("已自动生成 YIBAN_SECRET_KEY 并写入 %s", env_path)
        return key


def migrate_admin_password_to_hash(env_path):
    """启动时安全迁移：检测到管理员口令以明文（YIBAN_ADMIN_PASSWORD）存储且无哈希时，
    自动生成 scrypt 哈希写入 YIBAN_ADMIN_PASSWORD_HASH 并清空明文。

    说明：仅改变口令的存储形态（明文 → 哈希），口令本身不变；已有哈希则跳过；
    明文回退比对路径（verify_admin）保留以兼容未迁移的存量部署。
    迁移失败（如 .env 对进程不可写）只告警不阻断启动——明文回退仍可登录。

    批次7 A1（SSH 追回路径堵漏）：检测到「明文与现存哈希不一致」——即运维通过
    SSH 重设了 YIBAN_ADMIN_PASSWORD（主管理员被盗后的追回操作）——重迁移哈希的
    同时递增 YIBAN_ADMIN_PW_VERSION，使全部被盗旧会话立即失效。原实现哈希存在
    即跳过，导致重设的明文被忽略（verify_admin 哈希优先）、攻击者旧密码 + 旧
    cookie 双通道继续掌控。
    """
    rotated = False
    try:
        env = read_env(env_path)
        existing_hash = env.get("YIBAN_ADMIN_PASSWORD_HASH", "").strip()
        plain = env.get("YIBAN_ADMIN_PASSWORD", "").strip()
        if not plain:
            return
        updates = {
            "YIBAN_ADMIN_PASSWORD_HASH": generate_password_hash(plain, method=SCRYPT_METHOD),
            "YIBAN_ADMIN_PASSWORD": "",
        }
        if existing_hash:
            try:
                same = check_password_hash(existing_hash, plain)
            except (ValueError, TypeError):
                same = False
            if same:
                return  # 明文与哈希一致（重复启动），无需任何写入
            cur_pwv = load_env_int(env_path, "YIBAN_ADMIN_PW_VERSION", 1)
            updates["YIBAN_ADMIN_PW_VERSION"] = str(cur_pwv + 1)
            rotated = True
        write_env_batch(
            env_path,
            updates,
        )
    except OSError as e:
        logger.warning(
            "管理员口令明文迁移失败（%s 不可写？）：%s；将暂时回退明文比对，"
            "请修复权限后重启或手动改密",
            env_path,
            e,
        )
        return
    if rotated:
        logger.warning(
            "检测到管理员口令被外部更改（%s，明文与现存哈希不一致）：已重迁移哈希"
            "并递增 YIBAN_ADMIN_PW_VERSION，全部旧会话已失效（SSH 追回场景）",
            env_path,
        )
    else:
        logger.warning(
            "检测到管理员口令明文存储（%s），已自动迁移为 scrypt 哈希并清空明文；"
            "口令本身未变更，请确认其强度足够（弱口令仍可被猜测）",
            env_path,
        )


def _atomic_write(path, content, chmod_priv=False):
    """原子写文件：先写临时文件再替换，避免半写状态（cron 并发读取安全）。

    chmod_priv=True 时写完后收紧为 0600（含密钥/口令的 .env 场景），
    防止默认 umask 下产生同主机其他用户可读的宽松权限。
    """
    tmp = f"{path}.tmp{secrets.token_hex(4)}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())  # 落盘再替换：极端掉电场景不丢数据
    os.replace(tmp, path)
    if chmod_priv:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)  # 仅属主可读写（Windows 无实际效果，忽略失败）


# ---------------------------------------------------------------------------
# 数据读写（SQLite：db 层单行事务 + WAL，天然原子，无需 TTL 缓存）
# RLock：进程内"读→检查→写"操作级序列互斥（防呆判定与写入之间不被同进程请求交错；
# 跨进程一致性由 SQLite 事务与 UNIQUE 约束保证）
# ---------------------------------------------------------------------------
_file_lock = threading.RLock()
# 限速/失败计数 dict 的进程内读改写锁（H7：单 worker + 锁内原子更新；
# scrypt 校验不持锁，避免长时间阻塞其他请求）
_rate_lock = threading.Lock()
# 每日清理线程只允许同一进程启动一次（测试多次 create_app 时避免并发访问共享 SQLite 单例）
_purge_loop_started = False
_purge_loop_lock = threading.Lock()


def _bump_window_count(store, key, now, window, limit=None):
    """锁内递增窗口计数，返回 (count, window_start, allowed)。

    - limit 为 None：总是递增，allowed 恒为 True；
    - limit 非 None：达到 limit 后不再递增并返回 allowed=False（用于“先判断再递增”的限速语义，
      例如登录频率限制允许第 10 次、拒绝第 11 次）。
    H7：限速计数 dict 的读改写统一走这里，避免并发请求丢失更新。
    """
    with _rate_lock:
        cnt, start = store.get(key, (0, now))
        if now - start > window:
            cnt, start = 0, now
        if limit is not None and cnt >= limit:
            return cnt, start, False
        cnt += 1
        store[key] = (cnt, start)
        return cnt, start, True


def _bump_login_failure(store, key, now):
    """锁内递增失败计数，返回递增后的次数。

    H7：登录/改密/注销/恢复共用失败计数的读改写统一走这里。
    """
    with _rate_lock:
        fails, _, _ = store.get(key, (0, 0, 0))
        fails += 1
        store[key] = (fails, 0, now)
        return fails


def _verify_attempt_allowed(store, username):
    """账号验证尝试配额（2026-08-27 对抗性审查 P1-2）。

    「注册/添加账号即时验证」会让服务器代用户向易班发起真实登录，必须防止
    被当作凭据试探的免费代理：在真正发起网络验证前按「会话用户名」扣减配额，
    超过 VERIFY_MAX 次 / VERIFY_WINDOW 秒即拒绝。全局 IP 限速之外的账号维度
    补充；计数语义与登录频率限制一致（先判后增）。store 由调用方传入
    （create_app 内的 _verify_limits，随应用生命周期存在于内存）。
    """
    _, _, allowed = _bump_window_count(
        store,
        (username or "?").lower(),
        time.time(),
        VERIFY_WINDOW,
        limit=VERIFY_MAX,
    )
    return allowed


def _wait_signin_proc(proc, timeout=300):
    """等待手动签到子进程；超时则终止并回收，避免批量签到队列被卡死。

    M8：原 proc.wait(timeout=300) 超时抛出 TimeoutExpired 后未回收子进程，
    队列仍会继续触发下一个账号，造成并发签到。超时后先 terminate，再等待
    回收；仍不退则 kill 兜底。
    """
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@contextlib.contextmanager
def _env_write_lock(env_path):
    """.env 写互斥：复用 scripts/env_lock.py 的共享锁。

    修复（对抗性审查 2026-08-15 实证）：并发保存设置/公告时 read-modify-write
    丢更新——gunicorn 多 worker 跨进程写 .env 需文件锁；同一把锁也供
    TUI 与密钥生成使用，避免各写各的锁文件。
    """
    with env_lock.env_write_lock(env_path):
        yield

# 启动缓存（与数据无关）：CHANGELOG 部署重启自然失效；公告保存时同步更新
_changelog_cache = [None]  # [文本]
_announcement_cache = [None]  # [公告文本]


def load_accounts():
    """全部账号（SQLite，password/phone_code 已解密为明文，按 sort_order 升序）。

    _file_lock：与写操作同锁，避免读到同一连接上未提交事务的部分结果
    （批量操作进行中，并发读可能看到半成品状态；RLock 可重入，写操作内调用无死锁）。
    """
    with _file_lock:
        return db.load_accounts()


def load_users():
    """全部用户（SQLite）。"""
    with _file_lock:
        return db.load_users()


def _mask_phone(p):
    """日志/列表脱敏：11 位手机号 → 138****8000；已脱敏（含 *）或非 11 位原样返回（幂等）。"""
    p = str(p)
    if "*" in p:
        return p
    return p[:3] + "****" + p[7:] if len(p) == 11 else p


def _mask_log_phones(line):
    """日志行内全部 [11 位手机号] 脱敏（/api/logs 与 /api/my-logs 共用，防展示层漏出 PII）。

    覆盖 signin.py 的行格式 `[13800138000] 结果`；其他格式（如 `账号: 138...`）
    不进日志（通知内容不落盘），单一格式正则足够——见日志审查 P3。
    """
    return re.sub(r"\[(\d{11})\]", lambda m: "[" + _mask_phone(m.group(1)) + "]", line)


def _mask_email(e):
    """日志/列表脱敏：邮箱 → abc***@example.com（保留域名）；已脱敏或非邮箱原样返回（幂等）。"""
    e = str(e)
    if "*" in e:
        return e
    i = e.find("@")
    if i <= 0:
        return e
    return e[: min(3, i)] + "***" + e[i:]


def _password_policy_error(password):
    """校验密码强度：至少 10 位且包含大写/小写/数字/符号中至少两类。返回错误信息 or None。"""
    if len(password) < PASSWORD_MIN_LEN:
        return f"密码至少 {PASSWORD_MIN_LEN} 位，且需包含两类以上字符"
    classes = sum(
        bool(re.search(pat, password)) for pat in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
    )
    if classes < 2:
        return "密码需包含大写字母/小写字母/数字/符号中的至少两类"
    return None


def _owner_display_of(owner_email):
    """把账号归属邮箱映射为展示名（后台归属列用）：普通用户显示邮箱前缀（@ 前）。"""
    if owner_email in ("admin", ""):
        return "管理员"
    return owner_email.split("@")[0] if "@" in owner_email else owner_email


def _slot_to_label(slot_min):
    """自选片窗口内分钟数 → "HH:MM"（调度 v2，与 signin 的 slot 口径一致：06:30 → 390）。"""
    if slot_min is None:
        return None
    sw = _sign_window()
    base = sw[0][0] * 60 + sw[0][1]
    m = base + int(slot_min)
    return f"{m // 60:02d}:{m % 60:02d}"


def _estimate_slot(phone):
    """预计签到时段（调度 v2 2.1，docs/design/plan-scheduler-v2.md）：
    顺序排序 = 可预期（线性填块区间 / 锚点中心 / 小人数确定性等分）；
    随机排序 = 每天重排，返回 None + 提示文案。
    返回 (estimated_str|None, note_str)。
    """
    env = read_env(ENV_FILE)
    mode = env.get("YIBAN_SIGN_MODE", "").strip().lower()
    order = env.get("YIBAN_SIGN_ORDER", "").strip().lower() or (
        "random" if mode == "random" else "sequence")
    dist = env.get("YIBAN_SIGN_DIST", "").strip().lower() or (
        "normal" if mode == "normal" else "uniform")
    if order != "sequence":
        return None, "随机模式每日重排，签到时间当天 06:31 后可见"
    accounts = load_accounts()
    # 与 build_schedule 一致：user_paused 账号不参与调度（零占位），预计时段按实际参与人计算
    live = [a for a in accounts if not a.get("user_paused")]
    idx = next((i for i, a in enumerate(live) if a.get("phone") == phone), None)
    if idx is None or not live:
        return None, ""
    sw = _sign_window()
    front_min, back_min = edge_config()[0] / 60.0, edge_config()[1] / 60.0
    start_min = sw[0][0] * 60 + sw[0][1]
    end_min = sw[1][0] * 60 + sw[1][1]
    eff_lo = start_min + front_min
    eff_hi = end_min - back_min
    span = eff_hi - eff_lo

    def fmt(m):
        m = int(m)
        return f"{m // 60:02d}:{m % 60:02d}"

    if dist == "uniform":
        # 线性填块（与 signin._schedule_blocks 同口径：块从窗口起点步进 5、裁到有效窗口、
        # 被缓冲吃掉的无效块跳过；压缩模式等极端场景按末块估算）
        k = load_env_int(ENV_FILE, "YIBAN_BLOCK_CAP", 15)
        valid = []
        b = start_min
        while b < end_min:
            lo = max(b, eff_lo)
            hi = min(b + 5, eff_hi)
            if hi > lo:
                valid.append((lo, hi))
            b += 5
        if not valid:
            return None, ""
        bi = min(idx // k, len(valid) - 1)
        lo, hi = valid[bi]
        return f"{fmt(lo)}~{fmt(hi)}", "（每日固定时段，块内时刻每天略有抖动）"
    # 顺序 × 正态：锚点 z 固定 → 预期中心（μ 中值 50%、σ 中值 20%）
    z = random.Random(str(phone)).gauss(0, 1)
    center = max(eff_lo, min(eff_hi, eff_lo + span * 0.5 + span * 0.20 * z))
    return f"约 {fmt(center)}", "（每日波动约 ±10 分钟）"


def mask_account(acc, index, masked=True):
    """账号展示序列化（列表默认脱敏手机号/归属邮箱，网络层不泄露完整 PII）。

    masked=False 时返回完整信息（仅详情接口使用，按需取完整号用于编辑/签到等操作）。
    密码始终不下发（has_password 布尔）；设备识别码始终不下发（has_phone_code 布尔）。
    """
    phone = acc.get("phone", "")
    owner = acc.get("owner", "admin")
    return {
        "index": index,
        "name": acc.get("name", ""),
        "phone": _mask_phone(phone) if masked else phone,
        "phone_model": acc.get("phone_model", ""),
        "has_password": bool(acc.get("password")),
        "has_phone_code": bool(acc.get("phone_code")),
        "display_name": acc.get("name") or f"账号{index + 1}",
        # 普通用户体系：owner=提交者邮箱（'admin'=管理员添加），status=待审核/已生效
        "owner": _mask_email(owner) if masked else owner,
        "owner_display": _owner_display_of(owner),
        "status": acc.get("status", ACCOUNT_STATUS_ACTIVE),
        "reject_reason": acc.get("reject_reason", ""),
        "user_paused": bool(acc.get("user_paused", False)),  # 用户自暂停（调度 v2）
        # 软删除：管理员删除后进入待删除状态（保留期内可恢复）
        "deleted": bool(acc.get("deleted")),
        "deleted_at": acc.get("deleted_at", ""),
    }


def find_account_index(accounts, phone):
    """按手机号查账号下标（手动签到用）。"""
    for i, acc in enumerate(accounts):
        if acc.get("phone") == phone:
            return i
    return None


def _duplicate_phone_error(accounts, phone, email):
    """重提手机号冲突的差异化文案（v10 用户删除软删化）。

    冲突行是本人刚删除的账号（deleted_by=本人）时给撤销指引；其余统一口径，
    不向调用方泄露该手机号的归属信息。返回 None 表示非本人待删除行冲突。
    """
    conflict = next((a for a in accounts if a.get("phone") == phone), None)
    if (
        conflict is not None
        and conflict.get("owner") == email
        and conflict.get("deleted")
        and conflict.get("deleted_by", "") == email
    ):
        return "该手机号对应你刚删除的账号，可先撤销删除；或等 7 天自动清除后再提交"
    return None


def _owner_has_other_live(accounts, acc):
    """归属用户（非 admin）名下是否已有其他未删除账号（每人限 1 个，恢复/添加时校验）。"""
    owner = acc.get("owner", "admin")
    if not owner or owner == "admin":
        return False
    return any(
        a.get("owner") == owner and not a.get("deleted") and a is not acc for a in accounts
    )


def validate_account(data, require_password):
    """校验账号字段。require_password=True 时密码必填；返回 (错误信息 or None, 清洗后的账号 dict)。"""
    name = str(data.get("name", "")).strip()
    if len(name) > 50:
        return "名称过长（最多 50 字）", None
    phone = str(data.get("phone", "")).strip()
    password = str(data.get("password", "")).strip()
    if not phone:
        return "手机号为必填项", None
    if not PHONE_RE.match(phone):
        return "手机号格式不正确（应为 1 开头的 11 位数字）", None
    if require_password and not password:
        return "密码为必填项", None
    phone_model = str(data.get("phone_model", "")).strip()
    if len(phone_model) > 50:
        return "设备型号过长（最多 50 字）", None
    phone_code = str(data.get("phone_code", "")).strip()
    if len(phone_code) > 128:
        return "设备识别码过长", None
    return None, {
        "name": name,
        "phone": phone,
        "password": password,
        "phone_model": phone_model,
        "phone_code": phone_code,
    }


def _account_verify_enabled():
    """注册/添加账号时是否做即时验证（YIBAN_ACCOUNT_VERIFY=1，任意管理员可开关）。"""
    env = read_env(ENV_FILE)
    return env.get("YIBAN_ACCOUNT_VERIFY", "").strip().lower() in ("1", "true", "on", "yes")


def _verify_account_clean(clean):
    """对清洗后的账号字段做只读验证（复用 signin.verify_account：登录+拉任务，不提交签到）。

    返回错误信息 or None（验证通过）。message 来自 signin（已脱敏），此处再转义换行防注入。
    """
    try:
        ok_v, msg_v = signin.verify_account(
            signin.Account(
                phone=clean["phone"],
                password=clean.get("password", ""),
                phone_model=clean.get("phone_model", ""),
                phone_code=clean.get("phone_code", ""),
            )
        )
    except Exception as e:
        return f"账号验证异常：{str(e).replace(chr(10), ' ').replace(chr(13), ' ')}"
    if not ok_v:
        return f"账号验证未通过：{str(msg_v).replace(chr(10), ' ').replace(chr(13), ' ')}"
    return None


# ---------------------------------------------------------------------------
# 管理员认证
# ---------------------------------------------------------------------------
def check_admin_configured():
    """管理员账号是否已在 .env 配置（口令哈希或旧明文任一即可）。"""
    env = read_env(ENV_FILE)
    return bool(
        env.get("YIBAN_ADMIN_USER", "").strip()
        and (
            env.get("YIBAN_ADMIN_PASSWORD_HASH", "").strip()
            or env.get("YIBAN_ADMIN_PASSWORD", "").strip()
        )
    )


def verify_admin(username, password):
    """校验管理员账号（每次登录实时读 .env，修改立即生效）。

    口令哈希（YIBAN_ADMIN_PASSWORD_HASH，scrypt）优先；哈希缺失而明文仍在
    （启动迁移失败态）按 M1 fail-closed 直接拒绝——明文比对路径已停用，
    修复 .env 权限重启即自动补齐哈希。
    注意：compare_digest 不支持非 ASCII 直接比较，先编码为 UTF-8 字节。
    """
    env = read_env(ENV_FILE)
    admin_user = env.get("YIBAN_ADMIN_USER", "").strip()
    # 2026-08-20 对抗性审查修复（P1）：凭据未配置完整时直接拒绝——
    # 原实现 admin_user/admin_pass 均为空串时 compare_digest(b"", b"") 恒真，
    # "只配了用户名没配密码"（或完全未配置）的部署可用空口令登录管理员。
    # 该状态 check_admin_configured() 明确判定为"未配置"，此处口径对齐。
    if not admin_user or not (
        env.get("YIBAN_ADMIN_PASSWORD_HASH", "").strip()
        or env.get("YIBAN_ADMIN_PASSWORD", "").strip()
    ):
        _constant_time_dummy(password)  # 时延拉平：与真实比对等开销
        return False
    # 批次7 P3-7：用户名比较统一小写——登录成功后 session 存小写（历史修复），
    # 而此处大小写敏感比对导致混合大小写 YIBAN_ADMIN_USER 永远无法自助改密，
    # 且会被失败计数锁定（管理员被自己的改密界面锁死）
    if not secrets.compare_digest(
        username.strip().lower().encode("utf-8"), admin_user.strip().lower().encode("utf-8")
    ):
        _constant_time_dummy(password)  # 时延拉平：防用户名枚举（与真实比对等开销）
        return False
    pw_hash = env.get("YIBAN_ADMIN_PASSWORD_HASH", "").strip()
    if pw_hash:
        return check_password_hash(pw_hash, password)
    admin_pass = env.get("YIBAN_ADMIN_PASSWORD", "").strip()
    if admin_pass:
        # M1 明文回退 fail-closed：走到这里 = 哈希缺失而明文仍在，即启动迁移
        # （migrate_admin_password_to_hash）写 .env 失败的降级态。此前回退明文比对，
        # 意味着迁移失败被静默容忍、明文口令路径长期可用；直接拒绝并指导修复，
        # 修复 .env 权限/可写性后重启即自动补齐哈希（迁移逻辑本身不变）。
        logger.error(
            "管理员口令仍为明文存储（%s 缺 YIBAN_ADMIN_PASSWORD_HASH，启动迁移失败态），"
            "明文比对已停用（fail-closed）。请修复 .env 的属主/权限（属主可写）后重启服务，"
            "迁移会自动将口令转为哈希；或手工设置 YIBAN_ADMIN_PASSWORD_HASH",
            ENV_FILE,
        )
        _constant_time_dummy(password)  # 时延拉平：与哈希比对等开销
        return False
    # 2026-08-27 审查 P3：此处两种既有分支均已返回——
    # 哈希存在 → 已比对返回；明文存在 → M1 fail-closed 返回 False。
    # 能走到这 = 哈希与明文都为空（配置在两次 read_env 之间被清空的极端竞态），
    # 原 compare_digest 行在该态对空密码恒真，属理论上的失效盲区，显式拒绝。
    return False


# ---------------------------------------------------------------------------
# 系统信息
# ---------------------------------------------------------------------------
def sign_status(now=None):
    """基于服务器时间计算签到状态（与 tui/app.py _sign_status 保持一致）。

    返回 (显示文本, 颜色)。颜色为原版配色（东京夜蓝系，深浅页面背景均可读）；
    文案不含 emoji（UI 图标统一走前端 SVG 图标系统）。
    """
    now = now or datetime.now()
    if now.weekday() == 6 and not load_env_int(ENV_FILE, "YIBAN_SUNDAY_SIGN", 0):
        # 周日：仅当「周日签到」开启时走正常窗口逻辑，否则提示无需打卡
        return "今日无需打卡（周日）", "#a1a1aa"
    if now.weekday() == 5 and not load_env_int(ENV_FILE, "YIBAN_SATURDAY_SIGN", 1):
        # 周六：默认开启（周六照常签到）；关闭后周六提示无需打卡
        return "今日无需打卡（周六）", "#a1a1aa"
    sw = _sign_window()  # 单次读取（每次调用都会重读 .env，避免重复解析）
    start_h, start_m = sw[0]
    end_h, end_m = sw[1]
    start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if now < start:
        return f"未到签到时间（{start_h:02d}:{start_m:02d} 开始）", "#7aa2f7"
    if now <= end:
        return f"签到窗口进行中（~{end_h:02d}:{end_m:02d} 结束）", "#9ece6a"
    return "今日签到已结束", "#e0af68"


def check_connectivity():
    """连通性检测：不登录，仅检查易班 API 可达性。返回 (ok, detail)。"""
    try:
        resp = requests.get(
            "https://api.uyiban.com/base/c/auth/yiban",
            timeout=6,
            headers={
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
            },
        )
        ok = resp.status_code < 500
        detail = f"HTTP {resp.status_code}"
    except Exception as e:
        ok = False
        detail = str(e)[:60]
    return ok, detail


def _is_safe_notify_url(url):
    """L-01 通知 URL 白名单：必须 https:// 且主机非回环/内网/链路本地。

    YIBAN_NOTIFY_URL 携带告警内容（IP/用户名/时间）出站；http 明文或指向
    回环/内网（127/8、10/8、172.16/12、192.168/16、169.254/16、::1、
    localhost 字面量）的目标会变成 SSRF 探测跳板或明文外带通道，一律拒绝。
    域名目标放行（不做解析级阻断，DNS rebinding 由出站请求侧超时兜底）。
    """
    from urllib.parse import urlparse

    try:
        o = urlparse(url)
    except ValueError:
        return False
    if o.scheme != "https" or not o.hostname:
        return False
    host = o.hostname.strip().lower()
    if host == "localhost":
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # 域名：非 IP 字面量，放行
    return not (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified)


def _nl_safe(value):
    """告警正文插值净化（2026-08-27 对抗性审查 P2-4）：压平 CR/LF。

    外部可控字段（用户名/邮箱/IP 等）拼进邮件或通知正文前转义换行为字面量，
    防止请求体夹带换行在告警正文中伪造额外行。对齐 signin._sanitize_text 的
    换行纪律；正常值不含换行，显示语义不变。
    """
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def send_notification(title, content):
    """通过 YIBAN_NOTIFY_URL webhook 发送告警通知（.env 优先，静默失败）。

    与 scripts/signin.py 的 send_notification 同款渠道：Server 酱 / Bark / 企业微信。
    L-01：发送前过 _is_safe_notify_url 白名单（https + 非回环/内网），
    违规拒绝发送并告警（不回显完整 URL，防 sendkey 泄露进日志）。
    A 线邮箱通知与 webhook 并存：即使未配置 YIBAN_NOTIFY_URL，配置了
    YIBAN_MAIL_* 仍会发管理员告警邮件（mailer 内部静默失败，不影响本流程）。
    收件人 = ADMIN_TO（按个人开关过滤）+ 所有开启接收的管理员用户邮箱：
    普通管理员自动获得告警收件权，关闭 mail_notify 后从收件人剔除；
    主管理员关闭 YIBAN_MAIL_ADMIN_NOTIFY 后不再收 ADMIN_TO 邮件。
    """
    extra = mailer.admin_recipients() if mailer.admin_notify_enabled() else []
    recipients = db.admin_mail_recipients(extra)
    if recipients:
        mailer.send_admin_alert(title, content, to=",".join(recipients))
    env = read_env(ENV_FILE)
    url = env.get("YIBAN_NOTIFY_URL", "").strip() or os.environ.get("YIBAN_NOTIFY_URL", "").strip()
    if not url:
        return
    if not _is_safe_notify_url(url):
        from urllib.parse import urlparse

        logger.warning(
            "YIBAN_NOTIFY_URL 未通过白名单校验（需 https:// 且主机非回环/内网），已拒绝发送: host=%s",
            urlparse(url).hostname,
        )
        return
    try:
        requests.post(url, json={"title": title, "content": content}, timeout=10)
        logger.info("告警通知已发送: %s", title)
    except Exception as e:
        logger.warning("告警通知发送失败: %s", e)


# 容量告警去重（进程内）：首次触顶通知一次，之后静默拒绝（防通知风暴；重启后重置）
_capacity_alerts = {"users": False, "accounts": False}


def _notify_capacity_once(kind, limit, label):
    """容量触顶通知（每进程每种资源只发一次）：管理员知情且不刷屏。"""
    if _capacity_alerts.get(kind):
        return
    _capacity_alerts[kind] = True
    logger.warning("%s已达上限 %d，已拒绝新注册/添加", label, limit)
    send_notification(
        f"{label}已达上限",
        f"{label}已达上限（{limit}），新的注册/添加已被拒绝。\n"
        f"如需扩容请在 .env 调整 YIBAN_MAX_USERS / YIBAN_MAX_ACCOUNTS。",
    )


# ---------------------------------------------------------------------------
# Flask 应用
# ---------------------------------------------------------------------------
# 应用版本号（页面底部显示；每次修改按语义递增：修复 +0.0.1 / 功能 +0.1.0 / 大版本 +1.0.0）
# 2026-08-16 运维体系收尾：备份含日志/状态清理/设置审计/耗时记录/缓存优化（0.19.7）
# 2026-08-17 清理任务权限事故修复：cleanup 独立日志 + cron 改 yiban 用户（0.20.8）
# 2026-08-17 全量审查第一批修复：事务锁+时间戳+宽限期+密码泄露（0.20.9）
# 2026-08-17 全量审查第二批修复：AES弱密钥检测+CSP nonce+systemd加固+flock路径+migrate_v5+备份加密+测试补齐（0.20.10）
# 2026-08-17 全量审查第三+四批修复：中/低严重度问题全面清理（0.20.11）
# 2026-08-17 全量审查 0.21.0 修复：版本号更新
# 2026-08-17 全局暂停签到 + 备案信息预留区（0.21.1）
# 2026-08-17 合规文档接入网页 + 部署者模板化 + 渲染修复（0.21.2）
# 2026-08-20 审查修复（XSS/同意校验/缓存/状态语义）+ 掐头去尾前后独立可配（0.21.3）
# 2026-08-21 对抗性审查修复：空凭据管理员登录 + idx 防错位 + 读路径清理外移 + 审计链
#           BEGIN IMMEDIATE + 注册时延拉平 + my-* 单快照/日志脱敏 + HSTS/Permissions-Policy
#           + --host 默认回环（0.21.4）
# 2026-08-23 新增 Docker 部署能力（0.22.0）
# 2026-08-23 系统设置页容量统计口径修正（0.22.1）
# 2026-08-24 邮箱通知（SMTP）：管理员告警邮件 A 线 + 用户签到失败邮件 B 线 + 用户端开关（0.23.0）
# 2026-08-26 界面动效审查修复：过渡属性收敛、抽屉遮罩淡入与曲线、登录页切换统一、Toast 动效、reduced-motion 支持（0.24.1）
APP_VERSION = "0.26.0"
# 页面失效版本：每次启动变化，供前端"版本失效自动刷新"兜底（防止缓存旧页面）
WEB_VERSION = datetime.now().strftime("%Y%m%d%H%M%S")


def _is_loopback_host(host):
    """判断监听地址是否为回环（用于 H6 非回环 Secure Cookie 强警告）。"""
    h = str(host or "").strip().lower()
    return h in ("127.0.0.1", "::1", "localhost") or h.startswith("127.")


# ---------------------------------------------------------------------------
# 子路径 / 独立子域 前缀自适应中间件（2026-08-23）
# ---------------------------------------------------------------------------
# 背景：本应用可部署在域名根、独立子域、或主站子路径（如 /tools/yiban-auto-sign/demo/）下。
# 部署契约：反向代理只需把完整 URI【原样透传】（proxy_pass 后面不要加 "/" 去剥前缀），
# 本中间件即可自动感知挂载前缀并重写 SCRIPT_NAME / PATH_INFO，使：
#   · url_for() 自动带上前缀（服务端 redirect 改用 url_for 即可，见页面路由）；
#   · 应用内部 request.path 仍是干净路径（现有 /static/、/api/ 判断无需改动）；
#   · Flask 的 strict_slashes 补斜杠跳转自动带前缀。
# 前缀判定优先级：代理已传 SCRIPT_NAME（WSGI 契约，直接放行）> 环境变量 YIBAN_BASE_PATH > 自动探测。
# 自动探测采用【最短（首个）命中】前缀：优先把 /api/、/static/、页面路由等应用自身路由留在
# 剩余路径里（如 /tools/yiban-auto-sign/demo/api/login 应切成前缀 + /api/login，而非 .../api + /login）。
# 若挂载前缀本身恰好含 /api、/static 或页面名等会与应用路由撞车的段，自动探测可能切错，
# 此时请用 YIBAN_BASE_PATH 显式指定前缀（见 deploy 文档）。
# 约定：子路径首页请带尾斜杠访问（.../demo/，url_for 生成的首页地址即带斜杠）；
# 不带尾斜杠的裸路径无法可靠区分“子路径首页”与“根路径 404”，按 404 处理（防误伤根部署）。
class BasePathMiddleware:
    # 应用的扁平路由标记（新增顶层页面 / 接口前缀需同步追加）
    _ROOT_MARKERS = ("/login", "/user", "/terms", "/privacy", "/api/", "/static/")

    def __init__(self, wsgi_app, base_path=None):
        self.wsgi_app = wsgi_app
        self.base_path = base_path  # 显式前缀（优先于自动探测）；None 则自动

    def __call__(self, environ, start_response):
        # WSGI 契约：代理已设 SCRIPT_NAME 时，PATH_INFO 已相对该脚本路径，直接放行
        if (environ.get("SCRIPT_NAME") or "").strip("/"):
            return self.wsgi_app(environ, start_response)
        path = environ.get("PATH_INFO", "/")
        prefix = self._resolve_prefix(environ, path)
        if prefix:
            rest = path[len(prefix):]
            if not rest:
                rest = "/"
            environ["SCRIPT_NAME"] = prefix
            environ["PATH_INFO"] = rest
        return self.wsgi_app(environ, start_response)

    def _resolve_prefix(self, environ, path):
        # 显式配置（构造参数 > 环境变量 YIBAN_BASE_PATH）；仅当路径确实以该前缀开头才生效
        configured = (self.base_path or os.environ.get("YIBAN_BASE_PATH", "") or "").strip().strip("/")
        if configured:
            configured = "/" + configured
            if path == configured or path.startswith(configured + "/"):
                return configured
        # 自动探测（零配置默认路径）
        return self._detect_prefix(path)

    @classmethod
    def _detect_prefix(cls, path):
        # 根路径部署：本身就是首页或已知路由 → 无前缀
        if path == "/" or path in cls._ROOT_MARKERS:
            return ""
        if path.startswith("/api/") or path.startswith("/static/"):
            return ""
        # 按 "/" 边界切分，取【首个】命中：剩余部分为 "/"（子路径首页带尾斜杠）、
        # 已知路由、或 /api/、/static/ 路由前缀时，切掉的部分即前缀（最短=最先命中）
        pos = path.find("/", 1)
        while pos != -1:
            rest = path[pos:]
            if (rest == "/" or rest in cls._ROOT_MARKERS
                    or rest.startswith("/api/") or rest.startswith("/static/")):
                return path[:pos]
            pos = path.find("/", pos + 1)
        return ""


# 批次12 B12-13：仓库公开模板（.env.docker.example）自带的字面量默认口令。
# 随仓库公开 = 众所周知字符串，忘改即后台口令为公开知识。
_DEFAULT_ADMIN_LITERALS = frozenset((
    "请修改为强密码",
    "admin123",
    "admin888",
    "123456",
    "12345678",
    "password",
    "admin",
))
_DEFAULT_ADMIN_MIN_LEN = 8


def reject_default_admin_password(env_path):
    """启动检测：内置管理员仍为公开模板默认字面量/弱口令时拒绝启动（fail-closed）。

    批次12 B12-13：仓库公开，忘改 YIBAN_ADMIN_PASSWORD 即主管理员口令为众所周
    知字符串，且此前应用侧无任何检测。仅检查明文口令（纯哈希部署无法逆向检查；
    迁移会清空明文，故本检测必须在 migrate_admin_password_to_hash 之前执行）。
    抛 SystemExit 使 gunicorn worker 退出——supervisor/systemd 会带清晰日志重启，
    运维按提示改口令即可，宁可起不来也不能带着公开口令上线。
    """
    try:
        plain = str(read_env(env_path).get("YIBAN_ADMIN_PASSWORD", "") or "").strip()
    except Exception:  # noqa: BLE001 —— .env 不可读时交由既有启动路径处理
        return
    if not plain:
        return
    if plain in _DEFAULT_ADMIN_LITERALS or len(plain) < _DEFAULT_ADMIN_MIN_LEN:
        logger.critical(
            "拒绝启动：YIBAN_ADMIN_PASSWORD 为公开模板默认字面量或长度不足 %d 位。"
            "请编辑 .env 将其改为强密码（或在设置哈希 YIBAN_ADMIN_PASSWORD_HASH 后"
            "清空明文），再重启服务。",
            _DEFAULT_ADMIN_MIN_LEN,
        )
        raise SystemExit(2)


def create_app(host=None):
    global _purge_loop_started
    # 批次12 B12-13：默认/弱口令启动检测（必须在口令明文→哈希迁移之前，
    # 迁移会把明文清空导致无从检查）
    reject_default_admin_password(ENV_FILE)
    # 启动安全迁移：管理员口令明文 → scrypt 哈希（幂等，多 worker 并发写同口令哈希无害）
    migrate_admin_password_to_hash(ENV_FILE)
    # SQLite 数据层初始化：首次启动自动迁移 accounts.json/users.json → yiban.db（幂等，
    # JSON 改名 .bak 保留逃生门）；多 worker 各自调用幂等（模块级连接缓存）
    db.init_db(DB_FILE, migrate_from=ACCOUNTS_FILE, env_file=ENV_FILE)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = ensure_secret_key(ENV_FILE)
    app.config["SESSION_COOKIE_NAME"] = "yiban_admin"
    app.config["SESSION_COOKIE_HTTPONLY"] = True  # JS 不可读 session cookie（防 XSS 窃取）
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # 跨站请求不携带 cookie（防 CSRF）
    # H6：Secure 标志可由 YIBAN_COOKIE_SECURE 显式开启（env 文件或环境变量，1/true/yes 开，
    # 默认关）。默认关保持本机 HTTP 直连演示可用；生产 systemd 模板置 1（Task 7）。
    cookie_secure_raw = os.environ.get("YIBAN_COOKIE_SECURE")
    if cookie_secure_raw is None:
        cookie_secure_raw = read_env(ENV_FILE).get("YIBAN_COOKIE_SECURE", "")
    cookie_secure = str(cookie_secure_raw).strip().lower() in ("1", "true", "yes", "on")
    app.config["SESSION_COOKIE_SECURE"] = cookie_secure
    # 批次7 P3-6：HTTPS 反代自动升级 Secure——请求经 https（X-Forwarded-Proto）
    # 到达而 Secure 未显式开启时，粘性开启会话 Cookie 的 Secure 标志（首次 https
    # 请求即生效，无需重启）；显式配置 YIBAN_COOKIE_SECURE=0 的部署保持原行为。
    _secure_auto_upgrade = {"done": not cookie_secure}  # 显式关闭时不自动升级

    @app.before_request
    def _auto_secure_on_https():
        if not _secure_auto_upgrade["done"]:
            if (
                request.headers.get("X-Forwarded-Proto", "").strip().lower() == "https"
                or request.is_secure
            ):
                app.config["SESSION_COOKIE_SECURE"] = True
                _secure_auto_upgrade["done"] = True
                logger.info("检测到 HTTPS 反代（X-Forwarded-Proto=https），会话 Cookie 已自动启用 Secure")
    if host is not None and not _is_loopback_host(host) and not cookie_secure:
        logger.warning(
            "YIBAN_COOKIE_SECURE 未开启：当前监听地址 %s 非回环，生产环境请设置 "
            "YIBAN_COOKIE_SECURE=1（.env 或环境变量），否则登录 Cookie 可能在 HTTPS 下被浏览器拒绝",
            host,
        )
    app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 14  # 14 天（折中：安全与管理员便利平衡）

    # ---- 会话绝对过期上限（2026-08-27 对抗性审查 P2-5）----
    # 滑动续期防不了「被盗 Cookie 永久续命」：任何会话自登录起最多存活 N 天，
    # 到期硬失效需重新登录。YIBAN_SESSION_ABS_DAYS 可配，越界回退默认并告警
    # （风格对齐 M13 会话缓存 TTL 钳制）。判定逻辑见 _current_role。
    global SESSION_ABS_TTL_SECONDS
    _abs_days = load_env_int(ENV_FILE, "YIBAN_SESSION_ABS_DAYS", SESSION_ABS_DAYS_DEFAULT)
    if _abs_days < 1 or _abs_days > 30:
        logger.warning(
            "YIBAN_SESSION_ABS_DAYS=%s 越界（允许 1~30 天），回退默认 %d 天",
            _abs_days, SESSION_ABS_DAYS_DEFAULT,
        )
        _abs_days = SESSION_ABS_DAYS_DEFAULT
    SESSION_ABS_TTL_SECONDS = _abs_days * 86400

    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # 请求体上限 64KB

    # 登录失败记录 {ip: [fail_count, lock_until]}
    _login_fails = {}
    # 全局限速记录 {ip: [count, window_start]}
    _rate_limits = {}
    # 注册限速记录 {ip: [count, window_start]}
    _register_limits = {}
    # 登录频率限制 {ip: [count, window_start]}：比全局限速更严，防换用户名密码喷洒
    _login_rate = {}
    # 批次7 P3-8：账号恢复的每 IP 聚合失败窗口 {ip: [count, window_start]}
    _restore_fail_rate = {}
    # 账号验证尝试配额 {username.lower(): (count, window_start)}（2026-08-27 P1-2）
    _verify_limits = {}

    def _ip_store_trim(store, max_age):
        """IP 计数 dict 超限时清理过期条目：仅当长度超上限才遍历，避免每请求开销。

        各 store 的值为二元/三元组，末位统一是时间戳；防止公网扫描器用海量
        不同 IP 打爆内存（无界增长 DoS）。
        """
        if len(store) <= _IP_STORE_LIMIT:
            return
        now = time.time()
        stale = [k for k, v in store.items() if now - v[-1] > max_age]
        for k in stale:
            store.pop(k, None)

    # ---- 全局限速：防脚本轰炸 API（2026-08-16 用户决策：只对 /api/* 限速，
    # 页面/静态放宽，避免 302+200 双请求导致正常页面浏览被误伤）----
    @app.before_request
    def rate_limit():
        if not request.path.startswith("/api/"):
            return
        ip = _client_ip()
        now = time.time()
        with _rate_lock:
            _ip_store_trim(_rate_limits, _IP_STORE_MAX_AGE)
        cnt, _start, _allowed = _bump_window_count(_rate_limits, ip, now, RATE_WINDOW)
        if cnt > RATE_MAX:
            return jsonify({"error": "请求过于频繁，请稍后再试"}), 429

    # ---- 认证守卫：/api/* 需登录；普通用户仅限 my-* 与 clock ----
    @app.before_request
    def require_login():
        if not request.path.startswith("/api/"):
            return
        if request.path in ("/api/login", "/api/register", "/api/me/restore"):
            return
        # 公告/更新日志读取对所有用户开放（含未登录，登录页也显示）
        if request.path in ("/api/announcement", "/api/changelog") and request.method == "GET":
            return
        role = _current_role()
        if role is None:
            return jsonify({"error": "未登录"}), 401
        if role == "admin":
            return
        # 普通用户：只能操作自己的账号（/api/my-*）、读取时钟、查询身份与登出
        if request.path.startswith("/api/my-") or request.path in (
            "/api/clock",
            "/api/me",
            "/api/logout",
            "/api/me/password",
            "/api/me/delete",
        ):
            return
        # 批次12 B12-14：越权尝试留痕——已登录普通用户命中管理面路径是盗号/滥用
        # 的最高信号之一，此前 403 零留痕。IP 经 hash_ip 匿名化；频次天然受
        # /api/* 全局限速约束，且普通用户正常操作不会触达本分支。
        db.audit(
            (session.get("username") or "?")[:64],
            "forbidden_path",
            db.hash_ip(_client_ip()),
            request.path[:120],
        )
        return jsonify({"error": "无权限"}), 403

    # ---- CSRF 防护：登录后所有写请求（POST/PUT/DELETE）必须携带与 session 匹配的 token ----
    # 登录/注册无需 token（未登录态，跨站表单攻击由 SameSite=Lax 已基本阻断）；
    # 已登录用户的写操作由 token 双重校验（借鉴 flask-wtf 的 Session 方案，自实现零依赖）。
    def get_csrf_token():
        """惰性生成并返回当前会话的 CSRF token。"""
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(32)
        return session["csrf_token"]

    def _is_same_origin():
        """登录/注册等未登录写接口的同源校验：跨站表单提交的 POST 必然携带 Origin 头。

        浏览器同源 fetch POST 也携带 Origin；无 Origin 的请求（同站导航、curl）放行。
        """
        origin = request.headers.get("Origin")
        if not origin:
            return True
        from urllib.parse import urlparse

        try:
            o = urlparse(origin)
        except ValueError:
            return False
        return (o.scheme, o.netloc) == (request.scheme, request.host)

    # NEW-M1（反代转发头误诊断）：Origin 为 https 而应用侧 request.scheme 为 http
    # 时，说明反向代理的 X-Forwarded-Proto 未生效（nginx 缺 proxy_set_header，或
    # gunicorn 未信任代理头），同源校验会把全部正常登录/注册误判为跨站拒绝。
    # 首次命中输出一次性 ERROR 指引排查（模块级标记，重启后重置）。
    _forwarded_proto_mismatch_logged = False

    def _log_forwarded_proto_mismatch_once():
        """同源校验拒绝时附带 NEW-M1 一次性诊断（Origin=https / scheme=http）。"""
        nonlocal _forwarded_proto_mismatch_logged
        if _forwarded_proto_mismatch_logged:
            return
        origin = request.headers.get("Origin", "")
        from urllib.parse import urlparse

        try:
            o = urlparse(origin)
        except ValueError:
            return
        if o.scheme == "https" and request.scheme == "http":
            _forwarded_proto_mismatch_logged = True
            logger.error(
                "NEW-M1：请求 Origin 为 https 但应用侧 scheme 为 http——反向代理转发头"
                "未生效，同源校验将拒绝所有正常登录/注册。请检查：nginx 配置需含 "
                "proxy_set_header X-Forwarded-Proto $scheme；gunicorn 需信任代理头"
                "（forwarded_allow_ips 包含代理地址，如 --forwarded-allow-ips=127.0.0.1）"
            )

    @app.before_request
    def check_csrf():
        if request.method not in ("POST", "PUT", "DELETE"):
            return
        if not request.path.startswith("/api/"):
            return
        if request.path in ("/api/login", "/api/register", "/api/me/restore"):
            # 未登录态无 session token：用同源校验阻断跨站 CSRF（与登录/注册同等级）
            if not _is_same_origin():
                _log_forwarded_proto_mismatch_once()  # NEW-M1 反代头未生效诊断
                logger.warning(
                    "跨站登录/注册被拒绝: ip=%s path=%s origin=%s",
                    _client_ip(),
                    request.path,
                    request.headers.get("Origin"),
                )
                return jsonify({"error": "请求来源异常，请刷新页面后重试"}), 403
            return
        token = request.headers.get("X-CSRF-Token", "")
        sess_token = session.get("csrf_token", "")
        # 批次7 P4-1：非 ASCII token 会让 compare_digest 抛 TypeError → 500；
        # fail-closed 语义不变，但改为显式 403 且不刷异常日志
        if not token or not token.isascii() or not secrets.compare_digest(token, sess_token):
            logger.warning(
                "CSRF 校验失败: ip=%s path=%s token_len=%d session_token_len=%d",
                _client_ip(),
                request.path,
                len(token),
                len(sess_token),
            )
            return jsonify({"error": "请求校验失败，请刷新页面后重试"}), 403

    # ---- 页面（服务端按登录态重定向，避免未登录时先渲染后台造成闪烁）----
    @app.route("/")
    def index_page():
        role = _current_role()
        if role is None:
            return redirect(url_for("login_page"))
        if role != "admin":
            return redirect(url_for("user_page"))
        return render_template("index.html", web_version=WEB_VERSION, app_version=APP_VERSION, icp_info=icp_info(), police_info=police_info())

    @app.route("/user")
    def user_page():
        role = _current_role()
        if role is None:
            return redirect(url_for("login_page"))
        if role != "user":
            return redirect(url_for("index_page"))
        return render_template("user.html", web_version=WEB_VERSION, app_version=APP_VERSION, icp_info=icp_info(), police_info=police_info())

    # 登录页循环检测 {ip: [count, first_ts]}：浏览器缓存旧 JS 时可能无限 302 循环，
    # 同 IP 短时间频繁访问 /login 超过阈值 → 直接渲染登录页打断循环
    _login_loop = {}
    _LOGIN_LOOP_LIMIT = 1000  # 条目上限，防止内存无限增长

    @app.route("/login")
    def login_page():
        if session.get("auth"):
            ip = _client_ip()
            now = time.time()
            _ip_store_trim(_login_loop, 60)
            # 条目上限防护：超出时清理最老的 20%
            if len(_login_loop) > _LOGIN_LOOP_LIMIT:
                sorted_ips = sorted(_login_loop, key=lambda k: _login_loop[k][1])
                for old_ip in sorted_ips[:_LOGIN_LOOP_LIMIT // 5]:
                    _login_loop.pop(old_ip, None)
            cnt, first = _login_loop.get(ip, (0, now))
            if now - first > 10:
                cnt, first = 0, now
            cnt += 1
            _login_loop[ip] = (cnt, first)
            if cnt < 4:
                return redirect(url_for("index_page") if _current_role() == "admin" else url_for("user_page"))
            logger.warning("检测到登录页访问循环（IP %s），已打断并渲染登录页", ip)
        return render_template(
            "login.html",
            web_version=WEB_VERSION,
            app_version=APP_VERSION,
            icp_info=icp_info(),
            police_info=police_info(),
            agreement_html=_read_doc_html("USER_AGREEMENT.md"),
            privacy_html=_read_doc_html("PRIVACY_POLICY.md"),
        )

    @app.route("/terms")
    def terms_page():
        """用户协议独立页（footer / 隐私链接可指向）。"""
        return _doc_page("用户协议", _read_doc_html("USER_AGREEMENT.md"), icp_info(), police_info(), request.script_root)

    @app.route("/privacy")
    def privacy_page():
        """隐私政策独立页（footer / 隐私链接可指向）。"""
        return _doc_page("隐私政策", _read_doc_html("PRIVACY_POLICY.md"), icp_info(), police_info(), request.script_root)

    # ---- 页面缓存策略：管理页面禁止缓存（防浏览器缓存旧版 JS 导致登录循环）----
    @app.after_request
    def no_cache(resp):
        # 全站安全头（所有响应，含 API）：防 MIME 嗅探 / 点击劫持 / 泄露来源 / XSS 与注入面
        # 注意：不使用 CSP nonce——模板含大量内联 onclick 处理器（无法加 nonce），
        # nonce 存在时 'unsafe-inline' 会被浏览器忽略导致全部处理器失效（2026-08-17 线上事故）。
        # 后续可将内联事件迁移到 addEventListener 后再启用 nonce 防护。
        resp.headers["X-Content-Type-Options"] = "nosniff"
        # 与边缘 nginx 保持一致（SAMEORIGIN）：防止子路径(经 nginx 反代)下出现
        # "应用 DENY / nginx SAMEORIGIN" 双头取值不一致。SAMEORIGIN 仍防点击劫持，
        # 且对同源内嵌场景更兼容。
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        # 批次7 P4-6：补 COOP，收敛跨窗口攻面（CSP/XFO 之外的最后一块）
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        # 与 nginx 对齐：strict-origin-when-cross-origin（同源保留 referer，跨源最小化）
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # HSTS 移至边缘 nginx 统一下发（http_transport 一致性，疑自签过渡期阶段）。
        # 本应用不再重复下发，避免与 nginx 的 max-age 取值不一致造成双头歧义。
        # 注：若部署不经 nginx（如本地直连远程调试），可在此按需补回
        #   if request.is_secure: resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # 关闭无关能力面（2026-08-20 对抗性审查 P3 补；payment 与 nginx 对齐）
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self'; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'; object-src 'none'"
        )
        if request.path in ("/", "/login", "/user", "/terms", "/privacy"):
            resp.headers["Cache-Control"] = "no-store"
        elif request.path.startswith("/static/") and resp.status_code < 400:
            # 静态资源长缓存 30 天（版本变化由 ?v= 兜底）；404 等错误响应不缓存（防浏览器缓存 404）
            resp.headers["Cache-Control"] = "public, max-age=2592000"
        return resp

    # ---- 数据层错误保护：SQLite 读写/密文解密失败 → 明确 500（防静默降级或返回错误数据）----
    @app.errorhandler(RuntimeError)
    def _handle_data_error(e):
        logger.error("数据层错误: %s", e)  # 详细信息只入日志，不回显客户端（防内部路径/字段泄露）
        return jsonify({"error": "服务器内部错误，请稍后重试或联系管理员"}), 500

    # ---- 认证 API ----
    @app.route("/api/login", methods=["POST"])
    def api_login():
        """登录：管理员（.env 配置）或普通用户（users 表注册）。返回 role。"""
        ip = _client_ip()
        now = time.time()
        data = _json_body()
        username = str(
            data.get("username", "")
        ).strip()  # 管理员用户名保持原样；邮箱仅用户登录时小写
        password = str(data.get("password", ""))
        # 失败计数按 (IP, 用户名) 组合：同一出口 IP 的用户不因他人爆破尝试被连带锁定
        # 值三元组 (count, lock_until, last_ts)：last_ts 供超限清理
        fail_key = (ip, username.lower())
        with _rate_lock:
            _ip_store_trim(_login_fails, LOGIN_LOCK_SECONDS + _IP_STORE_MAX_AGE)
            _fails, lock_until, _ = _login_fails.get(fail_key, (0, 0, 0))
            if now < lock_until:
                # 不显示剩余秒数：避免向用户暴露锁定窗口参数（信息分层，2026-08-15）
                return jsonify({"error": "密码错误次数过多，请稍后再试"}), 429
        # 登录频率限制（60 秒窗口 10 次/IP，比全局限速更严）：防换用户名密码喷洒。
        # 先清理过期条目再按“先判断后递增”的旧语义计数：允许第 10 次，第 11 次 429。
        with _rate_lock:
            _ip_store_trim(_login_rate, 60 + _IP_STORE_MAX_AGE)
        _lcnt, _lstart, allowed = _bump_window_count(_login_rate, ip, now, 60, limit=10)
        if not allowed:
            return jsonify({"error": "登录尝试过于频繁，请稍后再试"}), 429

        role = None
        pw_version = None
        auth_source = None
        recoverable = False
        # 1) 内置管理员（.env，兜底超级管理员）
        if verify_admin(username, password):
            role = "admin"
            auth_source = "builtin"
            pw_version = load_env_int(ENV_FILE, "YIBAN_ADMIN_PW_VERSION", 1)  # 改密后旧会话失效
        else:
            # 2) 普通用户（users，邮箱登录，不区分大小写；role 支持多管理员）
            email = username.lower()
            u = db.find_user(email)
            if u is None:
                _constant_time_dummy(password)  # 时延拉平：防邮箱枚举（与真实比对等开销）
                # 登录即恢复（2026-08-16 用户裁决）：冷静期（7 天）内密码正确的已注销账号
                # 不放行登录，返回 recoverable 标记，由前端引导恢复（受冷却限速）
                du = db.find_user_any(email)
                if (
                    du is not None
                    and du.get("deleted")
                    and _delete_grace_remaining(du.get("deleted_at", "")) > 0
                    and check_password_hash(du.get("password_hash", ""), password)
                ):
                    recoverable = True
            elif check_password_hash(u.get("password_hash", ""), password):
                role = "admin" if u.get("role") == "admin" else "user"
                auth_source = "user"  # 注册用户（含提升的管理员）统一记为 user
                pw_version = u.get("pw_version", 1)
        if role:
            with _rate_lock:
                _login_fails.pop(fail_key, None)
            # 批次7 A6：登录成功写入审计（原实现成功登录零留痕，被盗会话无法还原
            # 会话何时建立、来自哪个 IP；IP 经 hash_ip 匿名化，与审计侧口径一致）。
            # 失败登录已有阈值邮件告警，不重复写审计（避免爆破刷爆审计表）。
            db.audit(
                username.lower(), "login", db.hash_ip(ip),
                f"登录成功（{auth_source}）",
            )
            # S1/复审：auth_source 记录实际认证来源，不用 role+邮箱反推
            # 防 session 固定：登录成功先清空再重建会话
            session.clear()
            session.permanent = True
            session["auth"] = True
            session["role"] = role
            # 会话用户名统一小写（2026-08-27）：注册用户邮箱库内小写存储，而
            # /api/me、邮件开关等接口以 session username 做大小写敏感的
            # find_user/update_user 精确匹配——存原始大小写会静默失效。
            # 内置管理员不受影响（_is_builtin_admin_session/_effective_role 比对端自行小写）。
            session["username"] = username.lower()
            session["auth_source"] = auth_source
            session["pw_version"] = pw_version  # 密码版本（注册用户改密/被重置后旧会话失效）
            # 会话绝对过期基准（P2-5）：自此刻起最多 SESSION_ABS_TTL_SECONDS
            session["login_ts"] = int(time.time())
            # 批次7 P3-5 服务端会话吊销：注册用户登录签发 sid 并落库——登出/被
            # 重置密码/被踢时轮换，被盗 cookie 重放即失效。内置管理员走 .env 的
            # PW_VERSION 吊销机制，无需 sid。
            if auth_source == "user":
                sid = secrets.token_hex(16)
                session["sid"] = sid
                db.set_user_sid(username.lower(), sid)
            return jsonify({"ok": True, "role": role})
        if recoverable:
            # 冷静期账号：密码正确但不建立会话，前端引导恢复（/api/me/restore）
            return jsonify({"ok": True, "recoverable": True, "msg": "账号已注销，7 天内可恢复"})
        fails = _bump_login_failure(_login_fails, fail_key, now)
        # 批次12 B12-14：失败登录留痕审计链——原仅内存计数+日志，"被盗号溯源"
        # 场景无法从审计还原爆破片段。刻意不在每次失败都写（防爆破刷爆审计表），
        # 与阈值邮件/锁定同节奏：达到告警阈值（3 次）与锁定阈值（5 次）各留痕一条，
        # IP 经 hash_ip 匿名化（与登录成功审计同口径）。用户名截断防长串刷审计。
        if fails in (LOGIN_FAIL_NOTIFY, LOGIN_MAX_FAILS):
            db.audit(
                (username.lower() or "?")[:64],
                "login_failed",
                db.hash_ip(ip),
                f"连续失败 {fails} 次（阈值留痕）",
            )
        if fails >= LOGIN_MAX_FAILS:
            with _rate_lock:
                _login_fails[fail_key] = (0, now + LOGIN_LOCK_SECONDS, now)
            logger.warning("登录失败次数过多，IP %s 锁定 %s 秒", ip, LOGIN_LOCK_SECONDS)
            return jsonify(
                {"error": f"密码错误次数过多，已锁定 {LOGIN_LOCK_SECONDS // 60} 分钟"}
            ), 429
        # 连续失败达到阈值时告警（每轮锁定只发一次），提示可能为暴力破解
        if fails == LOGIN_FAIL_NOTIFY:
            send_notification(
                "登录失败告警",
                f"IP {_nl_safe(ip)} 连续 {fails} 次登录失败"
                f"（尝试用户名: {_nl_safe(username)}）\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"如非本人操作，请检查是否有人尝试暴力破解",
            )
        return jsonify({"error": "用户名或密码错误"}), 401

    @app.route("/api/register", methods=["POST"])
    def api_register():
        """开放注册普通用户：邮箱 + 密码（哈希存储）。

        邮箱格式校验；邮箱全局唯一；不做验证码服务。无昵称体系（一人一号，账号备注名在账号表单中填写）。
        """
        data = _json_body()
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        # 后端同意校验：必须显式勾选《用户协议》与《隐私政策》（前端勾选仅为 UX，此处为强制）。
        # 严格布尔判断（is not True）：真值判断会让 "0"/"false"/"no" 等非空字符串绕过同意校验。
        if data.get("agree") is not True:
            return jsonify({"error": "请先阅读并同意《用户协议》和《隐私政策》"}), 400
        if len(email.split("@")[0]) > EMAIL_USER_MAX:
            return jsonify({"error": f"邮箱用户名部分过长（最多 {EMAIL_USER_MAX} 字符）"}), 400
        if not EMAIL_RE.match(email) or len(email) > 64:
            return jsonify({"error": "请输入有效的邮箱地址"}), 400
        # 邮箱域名审查（2026-08-28）：占位/一次性域名在写库前排除，防挤占用户池
        dom_err = email_domain_error(email)
        if dom_err:
            return jsonify({"error": dom_err}), 400
        pw_err = _password_policy_error(password)
        if pw_err:
            return jsonify({"error": pw_err}), 400
        # S1：内置管理员邮箱保留给 .env 主管理员，开放注册/自动注册均不得占用
        if email.strip().lower() == _builtin_admin_email().strip().lower():
            return jsonify({"error": "内置管理员邮箱不可注册"}), 400
        # 注册限速：同 IP 窗口内成功注册次数超限则拒绝（防邮箱批量注册）
        ip = _client_ip()
        now = time.time()
        with _rate_lock:
            _ip_store_trim(_register_limits, REGISTER_WINDOW)
            rcnt, rstart = _register_limits.get(ip, (0, now))
            if now - rstart > REGISTER_WINDOW:
                rcnt, rstart = 0, now
            if rcnt >= REGISTER_MAX:
                # 不暴露限速窗口分钟数（防恶意用户据此规划批量注册节奏，信息分层 2026-08-15）
                return jsonify({"error": "注册过于频繁，请稍后再试"}), 429
        # 操作级锁：邮箱唯一性检查与写入原子（UNIQUE 约束兜底并发注册）
        with _file_lock:
            # 容量兜底：注册总人数上限（防分布式注册无限膨胀 users 表，对抗性审查补）
            max_users = load_env_int(ENV_FILE, "YIBAN_MAX_USERS", DEFAULT_MAX_USERS)
            if max_users > 0 and len(db.load_users()) >= max_users:
                _notify_capacity_once("users", max_users, "注册人数")
                return jsonify({"error": "注册人数已达上限，请联系管理员"}), 403
            if db.find_user(email) is not None:
                # 时延拉平（2026-08-20 对抗性审查 P2）：已注册邮箱在此提前返回，
                # 跳过了后方的 scrypt 哈希（约百毫秒），响应时序差可被用于批量
                # 枚举"哪些邮箱是本站注册用户"。与登录/恢复的 _constant_time_dummy 惯例对齐。
                _constant_time_dummy(password)
                return jsonify({"error": "该邮箱已注册"}), 400
            # 冷却期邮箱保护（安全审查 2026-08-16）：已注销账号 7 天冷却期内禁止同邮箱注册，
            # 否则恢复权会被新注册抢占（登录即恢复形同虚设）；宽限期结束后邮箱正常释放
            du = db.find_user_any(email)
            if (
                du is not None
                and du.get("deleted")
                and _delete_grace_remaining(du.get("deleted_at", "")) > 0
            ):
                _constant_time_dummy(password)  # 时延拉平：同上，防探测"近期注销"邮箱
                return jsonify({"error": "该邮箱账号正在注销冷却期（7 天内可登录恢复）"}), 400
            try:
                created = db.create_user(
                    email,
                    generate_password_hash(password, method=SCRYPT_METHOD),
                    role="user",
                    created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    pw_version=1,  # 密码版本：改密时递增，旧会话随之失效
                )
            except sqlite3.IntegrityError:
                return jsonify({"error": "该邮箱已注册"}), 400  # 并发注册兜底
            if not created:
                return jsonify({"error": "该邮箱已注册"}), 400  # M10：OR IGNORE 未实际创建
            db.audit(email, "user_register", email, "开放注册")
        # 成功注册计数：原子重读后递增，避免并发注册丢失计数
        _bump_window_count(_register_limits, ip, now, REGISTER_WINDOW)
        logger.info("新用户注册: %s", _mask_email(email))
        return jsonify({"ok": True})

    @app.route("/api/logout", methods=["POST"])
    def api_logout():
        # 批次7 P3-5：登出轮换服务端 sid——此前仅 session.clear()，此前被窃取的
        # cookie 副本在登出后重放依然有效。轮换后所有旧会话（含当前）即时失效；
        # 内置管理员走 PW_VERSION 机制，无需轮换。
        if (
            session.get("auth_source") == "user"
            and session.get("username")
        ):
            with contextlib.suppress(Exception):
                db.set_user_sid(
                    session["username"].strip().lower(), secrets.token_hex(16)
                )
        session.clear()
        return jsonify({"ok": True})

    @app.route("/api/me/password", methods=["POST"])
    def api_me_password():
        """所有用户自助修改自己的密码（账号不可修改）。

        内置管理员（.env）验证当前口令后写入新哈希（YIBAN_ADMIN_PASSWORD_HASH，scrypt），
        并清理旧明文；注册用户（含提升的管理员）验证当前密码后更新 users 表密码哈希。
        失败计数与登录共用限速：达阈值（LOGIN_MAX_FAILS）锁定，超阈值返回 429；
        旧会话失效由 pw_version 递增实现（_effective_role 实时校验）。
        """
        data = _json_body()
        old_password = str(data.get("old_password", ""))
        new_password = str(data.get("new_password", ""))
        if new_password != str(data.get("confirm_password", "")):
            return jsonify({"error": "两次输入的新密码不一致"}), 400
        pw_err = _password_policy_error(new_password)
        if pw_err:
            return jsonify({"error": f"新密码不符合要求：{pw_err}"}), 400
        username = session.get("username", "")
        ip = _client_ip()
        now = time.time()
        # 失败计数键与登录一致：按 (IP, 用户名) 组合
        fail_key = (ip, username.strip().lower())
        with _rate_lock:
            _fails, lock_until, _ = _login_fails.get(fail_key, (0, 0, 0))
            if now < lock_until:
                # 不显示剩余秒数（信息分层，2026-08-15）
                return jsonify({"error": "尝试次数过多，请稍后再试"}), 429

        def _handle_failed_login():
            """当前密码校验失败：递增失败计数，达阈值锁定（与 api_login 一致）。

            2026-08-15 命名审查：原名 _pw_failed 读作"记录失败"，实际返回 429/400 响应。
            """
            nfails = _bump_login_failure(_login_fails, fail_key, now)
            if nfails >= LOGIN_MAX_FAILS:
                with _rate_lock:
                    _login_fails[fail_key] = (0, now + LOGIN_LOCK_SECONDS, now)
                logger.warning("改密失败次数过多，IP %s 锁定 %s 秒", ip, LOGIN_LOCK_SECONDS)
                # 不暴露锁定时长分钟数（信息分层，2026-08-15）
                return jsonify({"error": "密码错误次数过多，请稍后再试"}), 429
            if nfails == LOGIN_FAIL_NOTIFY:
                send_notification(
                    "改密失败告警",
                    f"IP {_nl_safe(ip)} 连续 {nfails} 次修改密码失败"
                    f"（用户名: {_nl_safe(username)}）\n"
                    f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"如非本人操作，请检查是否有人尝试暴力破解",
                )
            return jsonify({"error": "当前密码不正确"}), 400

        # 内置管理员：验证 .env 当前口令后更新（同邮箱注册用户不进入此分支）
        if _is_builtin_admin_session():
            if not verify_admin(username, old_password):
                return _handle_failed_login()
            write_env_batch(
                ENV_FILE,
                {
                    "YIBAN_ADMIN_PASSWORD_HASH": generate_password_hash(new_password, method=SCRYPT_METHOD),
                    "YIBAN_ADMIN_PASSWORD": "",  # 清理旧明文口令，改由哈希校验
                    "YIBAN_ADMIN_PW_VERSION": str(load_env_int(ENV_FILE, "YIBAN_ADMIN_PW_VERSION", 1) + 1),
                },
            )
            with _rate_lock:
                _login_fails.pop(fail_key, None)
            # 审计留痕（2026-08-20 对抗性审查 P2 补）：主管理员改密是最高权限的
            # 关键事件，此前注册用户分支有审计而本分支缺席，防篡改链上无法追责
            db.audit(
                username or "builtin-admin",
                "admin_password",
                _mask_email(username) if username else "-",
                "内置管理员自助改密",
            )
            # 批次12 B12-8：主管理员即时告警——改密是「被盗号接管」最强信号
            #（本人会话随 PW_VERSION 失效，攻击者以新密重登），此前仅审计+日志，
            # 告警渠道存在却未接（批次11 N6 漏了本分支）。对齐注册用户分支口径。
            send_notification(
                "账号安全事件告警",
                f"内置主管理员（{_mask_email(username) if username else 'builtin-admin'}）"
                f"密码已通过自助改密修改，时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}。\n"
                "如非本人操作，请立即按 README「主管理员权限追回」流程处理"
                "（SSH 重写 YIBAN_ADMIN_PASSWORD + PW_VERSION 递增）。",
            )
            logger.info("内置管理员密码已更新")
            return jsonify({"ok": True, "msg": "密码已更新，下次登录使用新密码"})
        # 注册用户（含提升的管理员）：db 单行更新（事务内，防并发覆盖）
        with _file_lock:
            u = db.find_user(username.strip().lower())
            if u is not None:
                if not check_password_hash(u.get("password_hash", ""), old_password):
                    return _handle_failed_login()
                db.update_user(
                    u["email"],
                    {
                        "password_hash": generate_password_hash(new_password, method=SCRYPT_METHOD),
                        "pw_version": u.get("pw_version", 1) + 1,  # 旧会话随之失效
                    },
                )
                db.audit(username, "user_password", username, "自助改密")
                # 批次7 P3-5：自助改密轮换 sid——当前会话保持有效（同步 session），
                # 此前被窃取的 cookie 副本随旧 sid 失效
                new_sid = secrets.token_hex(16)
                db.set_user_sid(username.strip().lower(), new_sid)
                session["sid"] = new_sid
                with _rate_lock:
                    _login_fails.pop(fail_key, None)
                # 批次11 N6：改密是核心安全事件——本人邮件（直接 send_user 绕过
                # mail_notify 开关：开关本身可被攻击者关闭）+ 管理员告警（被盗号
                # 改密时的可感知信号，审计之外的第一时间渠道）
                mailer.send_user(
                    username,
                    "【易班签到】您的账号密码已被修改",
                    "您的账号密码刚刚通过自助改密被修改。\n"
                    f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    "如非本人操作，请立即联系管理员重置密码并检查账号安全。",
                )
                send_notification(
                    "账号安全事件告警",
                    f"用户 {_mask_email(username)} 自助修改密码，"
                    f"时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                )
                logger.info("用户 %s 已修改自己的密码", _mask_email(username))
                return jsonify({"ok": True, "msg": "密码已更新，下次登录使用新密码"})
        return jsonify({"error": "用户不存在"}), 404

    @app.route("/api/me/delete", methods=["POST"])
    def api_me_delete():
        """用户自助注销（软删除 + 7 天宽限期，数据库 v5；安全审查 2026-08-16 3→7 天对齐）。

        安全设计（docs/design/plan-frontend-user-deregistration.md）：
        - 登录要求 + CSRF：全局写请求校验（X-CSRF-Token）自动覆盖；
        - 防 IDOR：只从 session 取当前用户，请求体任何目标参数一律忽略；
        - 密码确认：防"离开电脑被恶意页面直接注销"；
        - 防批量：每用户 60s 1 次 + 每 IP 60s 5 次（user_delete_requests 表计数，
          成功进入注销流程才记录；试密码已由 _login_fails 限速，两层防护不重叠），
          超限 429 且不暴露冷却秒数；
        - 管理员保护：内置管理员（.env 主管理员）不可注销；最后一个注册管理员
          不可注销（is_last_registered_admin，防失去全部管理入口）；
        - 审计：user_self_delete_request / user_self_delete_confirm，detail 脱敏；
        - 注销即清会话（软删除后 find_user 查无此人，_effective_role 同步失效）。
        """
        if not session.get("auth") or not session.get("username"):
            return jsonify({"error": "未登录"}), 401
        data = _json_body()
        password = str(data.get("password", ""))
        if not password:
            return jsonify({"error": "请输入当前密码"}), 400
        username = session.get("username", "")
        email = username.strip().lower()
        ip = _client_ip()
        now = time.time()
        # 防批量冷却：计数基于 user_delete_requests 表（kind=delete，v7 分流：
        # 恢复记录不占注销冷却，允许"恢复后立即再注销"）
        since_ts = (datetime.now() - timedelta(seconds=DELETE_COOLDOWN_SEC)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        if (
            db.count_user_delete_requests(
                username=email, since_ts=since_ts, kind="delete"
            )
            >= 1
        ):
            return jsonify({"error": "操作过于频繁，请稍后再试"}), 429
        ip_hash = hashlib.sha256(ip.encode("utf-8")).hexdigest()
        if (
            db.count_user_delete_requests(
                ip_hash=ip_hash, since_ts=since_ts, kind="delete"
            )
            >= DELETE_MAX_REQUESTS_PER_IP
        ):
            return jsonify({"error": "操作过于频繁，请稍后再试"}), 429
        # 内置管理员（.env 主管理员）不可自助注销：系统兜底账号，不落 users 表；
        # 同邮箱注册用户不受此限制（可正常自助注销）
        if _is_builtin_admin_session():
            return jsonify({"error": "当前账号不可注销"}), 400
        # 密码确认 + 失败锁定（与登录/改密共用 _login_fails 计数，达阈值锁定）
        fail_key = (ip, email)
        with _rate_lock:
            _fails, lock_until, _ = _login_fails.get(fail_key, (0, 0, 0))
            if now < lock_until:
                return jsonify({"error": "尝试次数过多，请稍后再试"}), 429

        def _handle_failed_login():
            """当前密码校验失败：递增失败计数，达阈值锁定（与 api_me_password 一致）。"""
            nfails = _bump_login_failure(_login_fails, fail_key, now)
            if nfails >= LOGIN_MAX_FAILS:
                with _rate_lock:
                    _login_fails[fail_key] = (0, now + LOGIN_LOCK_SECONDS, now)
                logger.warning("注销密码失败次数过多，IP %s 锁定 %s 秒", ip, LOGIN_LOCK_SECONDS)
                # 不暴露锁定时长（信息分层，2026-08-15）
                return jsonify({"error": "密码错误次数过多，请稍后再试"}), 429
            if nfails == LOGIN_FAIL_NOTIFY:
                send_notification(
                    "注销密码失败告警",
                    f"IP {_nl_safe(ip)} 连续 {nfails} 次注销密码验证失败"
                    f"（用户名: {_nl_safe(username)}）\n"
                    f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"如非本人操作，请检查是否有人尝试注销该账号",
                )
            return jsonify({"error": "当前密码不正确"}), 400

        with _file_lock:
            u = db.find_user(email)
            if u is None:
                return jsonify({"error": "用户不存在"}), 404
            if not check_password_hash(u.get("password_hash", ""), password):
                return _handle_failed_login()
            # 最后一个注册管理员不可注销（无内置管理员时会失去全部管理入口）
            if u.get("role") == "admin" and db.is_last_registered_admin(email):
                return jsonify({"error": "当前账号不可注销（系统最后一个管理员）"}), 400
            # 审计 + 软注销（db 单事务：账号/time_prefs 清除 + 用户标记）
            db.audit(username, "user_self_delete_request", email, "用户发起注销申请")
            try:
                _deleted = db.soft_delete_user_with_accounts(email)
            except db.LastAdminError:
                # C-M3（2026-08-28）：事务内复核兜底——跨进程并发注销时
                # 上面的 is_last_registered_admin 预检可能双双通过，db 层
                # 复核拦截后转 400（原路径会 500）
                return jsonify({"error": "当前账号不可注销（系统最后一个管理员）"}), 400
            if not _deleted:
                return jsonify({"error": "注销失败，请稍后再试"}), 500
            # 防批量计数仅在注销成功后才记录（低项：失败不占冷却额度）
            db.record_user_delete_request(email, ip_hash=ip_hash, kind="delete")
            db.audit(username, "user_self_delete_confirm", email, "注销已确认（软删除，7 天宽限期）")
            with _rate_lock:
                _login_fails.pop(fail_key, None)
            logger.info("用户 %s 已注销账号（7 天宽限期）", _mask_email(username))
            # 2026-08-16 用户裁决：自助注销不发管理员通知（正常操作，避免通知轰炸）；
            # 管理员在「用户管理 → 已注销用户」区块主动查看（/api/users/deleted）
        session.clear()
        return jsonify({"ok": True, "msg": "账号已注销，7 天内可撤销"})

    @app.route("/api/me/restore", methods=["POST"])
    def api_me_restore():
        """冷静期账号恢复（2026-08-16 用户裁决：仅登录即恢复，不做注册引导）。

        未登录可调（冷静期用户无会话；CSRF 走同源校验，与登录同等级）；
        密码验证 + 冷却限速（复用 user_delete_requests 计数：每邮箱 60s 1 次、
        每 IP 60s 5 次——与注销同一套底层冷却系统）；
        成功后 restore_user（联动恢复易班账号）+ 建立会话 + 审计 user_self_delete_restore。
        """
        data = _json_body()
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        ip = _client_ip()
        if not email or not password:
            return jsonify({"error": "邮箱和密码为必填项"}), 400
        # 防批量冷却（与注销同一张计数表，但按 kind 分流——v7 修复：
        # 注销动作自身的记录不再阻断 60s 内的恢复请求，"注销后立即反悔"路径畅通）
        since_ts = (datetime.now() - timedelta(seconds=DELETE_COOLDOWN_SEC)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        if (
            db.count_user_delete_requests(
                username=email, since_ts=since_ts, kind="restore"
            )
            >= 1
        ):
            return jsonify({"error": "操作过于频繁，请稍后再试"}), 429
        ip_hash = hashlib.sha256(ip.encode("utf-8")).hexdigest()
        if (
            db.count_user_delete_requests(
                ip_hash=ip_hash, since_ts=since_ts, kind="restore"
            )
            >= DELETE_MAX_REQUESTS_PER_IP
        ):
            return jsonify({"error": "操作过于频繁，请稍后再试"}), 429
        # 密码失败锁定预检（2026-08-17）：与登录/注销共用 (ip, email) 计数与锁定窗口
        fail_key = (ip, email)
        with _rate_lock:
            _ip_store_trim(_login_fails, LOGIN_LOCK_SECONDS + _IP_STORE_MAX_AGE)
            _fails, lock_until, _ = _login_fails.get(fail_key, (0, 0, 0))
            if time.time() < lock_until:
                return jsonify({"error": "密码错误次数过多，请稍后再试"}), 429
        u = db.find_user_any(email)
        in_grace = (
            u is not None
            and u.get("deleted")
            and _delete_grace_remaining(u.get("deleted_at", "")) > 0
        )
        verified = in_grace and check_password_hash(u.get("password_hash", ""), password)
        if not in_grace:
            # 时延拉平：账号不存在/过期时也做等开销 dummy 比对，防时序探测
            _constant_time_dummy(password)
        if not verified:
            # 密码失败锁定（2026-08-17 安全审查补齐）：与登录/注销共用 _login_fails
            # （键 (ip, email) 相同）——此前试错完全不计数，同 IP 可无限爆破冷却期
            # 账号密码，命中即恢复并建立会话；共用计数后登录侧锁定同样约束本接口
            now2 = time.time()
            nfails = _bump_login_failure(_login_fails, fail_key, now2)
            # 批次7 P3-8：补每 IP 聚合失败窗口（30 次/10 分钟）——单邮箱 5 次锁定
            # 只约束单账号，攻击者可跨邮箱喷洒（总速率仅受全局限速约束）；
            # 命中即获得该冷静期账号的完整会话与其易班凭据，须有聚合闸门
            _rc, _rs, ip_allowed = _bump_window_count(
                _restore_fail_rate, ip, now2, RESTORE_FAIL_WINDOW, limit=RESTORE_FAIL_MAX
            )
            if not ip_allowed:
                logger.warning("恢复密码尝试过于频繁（每 IP 聚合），IP %s 临时限制", ip)
                return jsonify({"error": "尝试过于频繁，请稍后再试"}), 429
            if nfails >= LOGIN_MAX_FAILS:
                with _rate_lock:
                    _login_fails[fail_key] = (0, now2 + LOGIN_LOCK_SECONDS, now2)
                logger.warning("恢复密码失败次数过多，IP %s 锁定 %s 秒", ip, LOGIN_LOCK_SECONDS)
                return jsonify({"error": "密码错误次数过多，请稍后再试"}), 429
            if nfails == LOGIN_FAIL_NOTIFY:
                send_notification(
                    "恢复密码失败告警",
                    f"IP {_nl_safe(ip)} 连续 {nfails} 次恢复密码验证失败"
                    f"（邮箱: {_nl_safe(email)}）\n"
                    f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"如非本人操作，请检查是否有人尝试冒充恢复已注销账号",
                )
            # 统一文案（2026-08-17 安全审查）：不区分"账号不存在/已过期"与"密码错误"，
            # 防无凭探测"哪些邮箱正处于注销冷却期"（注销用户警惕性低，是钓鱼高价值目标）
            return jsonify({"error": "邮箱或密码错误，或账号已过恢复期"}), 400
        try:
            _restored = db.restore_user(email)
        except sqlite3.IntegrityError:
            # 2026-08-28 审查 M7：并发注册抢注同邮箱（db 层已用写锁串行化，
            # 此处为防御纵深）——提示占用而非笼统"恢复失败"
            return jsonify({"error": "该邮箱已被注册，无法恢复"}), 409
        if not _restored:
            return jsonify({"error": "恢复失败，请稍后再试"}), 500
        # 防批量计数仅在恢复成功后才记录（低项：失败不占冷却额度）
        db.record_user_delete_request(email, ip_hash=ip_hash, kind="restore")
        with _rate_lock:
            _login_fails.pop(fail_key, None)
        db.audit(email, "user_self_delete_restore", email, "冷静期内恢复账号")
        # 恢复即登录：与 api_login 同款会话建立（防 session 固定）
        role = "admin" if u.get("role") == "admin" else "user"
        session.clear()
        session.permanent = True
        session["auth"] = True
        session["role"] = role
        session["username"] = email
        session["auth_source"] = "user"
        session["pw_version"] = u.get("pw_version", 1)
        # 会话绝对过期基准（P2-5），与 api_login 同口径
        session["login_ts"] = int(time.time())
        # 批次11 N1：恢复即登录须与 api_login 同样签发 sid 并落库。注销与恢复
        # （db.restore_user）均不轮换 sid，库内保留注销前登录签发的旧值——
        # 此处不签发则新会话无 sid、与库内旧值不匹配，恢复成功后下个请求即 401；
        # 且注销前被窃取的旧 cookie 会在恢复后原样复活，绕过整套 sid 吊销设计。
        sid = secrets.token_hex(16)
        session["sid"] = sid
        db.set_user_sid(email, sid)
        logger.info("用户 %s 已恢复注销账号", _mask_email(email))
        return jsonify({"ok": True, "role": role})

    @app.route("/api/me")
    def api_me():
        # admin 字段为旧版前端兼容（早期前端检查 me.admin；新版用 role）——
        # 防止浏览器缓存旧页面时误判未登录导致刷新循环
        role = _current_role()
        username = session.get("username") or ""
        # 邮箱通知开关（B 线：用户签到失败提醒，默认开；用户端可关）
        _me = db.find_user(username) if username else None
        mail_notify = bool(_me.get("mail_notify", 1)) if _me else True
        # 调度 v2：排序×分布模式与自选开关同步给用户（只读展示）
        env = read_env(ENV_FILE)
        mode = env.get("YIBAN_SIGN_MODE", "").strip().lower()
        sign_order = env.get("YIBAN_SIGN_ORDER", "").strip().lower() or (
            "random" if mode == "random" else "sequence"
        )
        sign_dist = env.get("YIBAN_SIGN_DIST", "").strip().lower() or (
            "normal" if mode == "normal" else "uniform"
        )
        sw = _sign_window()
        return jsonify(
            {
                "ok": True,
                "auth": bool(session.get("auth")),
                "role": role,
                "username": username,
                "email": username,  # 普通用户顶部显示邮箱前缀（管理员为用户名）
                "admin": role == "admin",
                "mail_notify": mail_notify,  # B 线：用户签到失败邮件开关
                "is_builtin_admin": _is_builtin_admin_session(),  # 仅 .env 主管理员会话为 True
                "csrf_token": get_csrf_token(),
                # 调度 v2（docs/design/plan-scheduler-v2.md 2.1/2.2）
                "sign_order": sign_order,
                "sign_dist": sign_dist,
                "time_pref_allowed": load_env_int(ENV_FILE, "YIBAN_ALLOW_TIME_PREF", 0) == 1,
                "sign_window": f"{sw[0][0]:02d}:{sw[0][1]:02d} ~ {sw[1][0]:02d}:{sw[1][1]:02d}",
            }
        )

    # ---- 我的邮箱通知开关（B 线：用户签到失败邮件）----
    @app.route("/api/my-mail-notify")
    def api_my_mail_notify():
        """读取当前用户邮箱通知开关。未登录默认视为开启（前端展示用）。"""
        email = session.get("username") or ""
        user = db.find_user(email) if email else None
        return jsonify(
            {"ok": True, "mail_notify": bool(user.get("mail_notify", 1)) if user else True}
        )

    @app.route("/api/my-mail-notify", methods=["PUT"])
    def api_my_mail_notify_save():
        """保存当前用户邮箱通知开关：{enabled: bool}。CSRF 由 before_request 统一校验。"""
        email = session.get("username") or ""
        if not email:
            return jsonify({"error": "未登录"}), 401
        data = _json_body()
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return jsonify({"error": "取值无效"}), 400
        affected = db.update_user(email, {"mail_notify": 1 if enabled else 0})
        if affected == 0:
            # C-M1（2026-08-28）：update_user 对不存在的邮箱是静默 no-op。
            # 内置管理员（.env 账号，不在 users 表）原会收到 ok:true 但刷新后
            # 开关弹回开启，还写入一条不存在的变更审计——改为明确 404 且不审计
            return jsonify({
                "error": "当前账号不支持此设置（内置管理员请用管理员邮件配置）"
            }), 404
        db.audit(email, "mail_notify", email, "on" if enabled else "off")
        if not enabled:
            # 批次11 N6：关闭通知本身是"先静默关通知再作案"攻击链的一环——
            # 确认邮件直接 send_user 绕过刚被关闭的开关，让本人知情
            mailer.send_user(
                email,
                "【易班签到】签到失败邮件通知已被关闭",
                "您的签到失败邮件通知已被关闭（本人操作确认）。\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                "如非本人操作，请立即联系管理员（账号可能已被他人控制）。",
            )
        return jsonify({"ok": True, "mail_notify": enabled})

    # ---- 邮件通知配置（全局开关，仅主管理员）----
    @app.route("/api/mail-config")
    def api_mail_config():
        """邮件通知配置状态（脱敏：授权码不回显，地址打码），供管理后台显示。"""
        cfg = mailer.get_config()
        enabled = str(cfg.get("enable", "")).strip().lower() in ("1", "true", "on", "yes")
        return jsonify({
            "ok": True,
            "enabled": enabled,
            "admin_notify": bool(cfg.get("admin_notify", True)),
            "smtp_host": cfg.get("host", ""),
            "smtp_port": cfg.get("port", 465),
            "user": cfg.get("user", ""),
            "admin_to": cfg.get("admin_to", ""),
        })

    @app.route("/api/mail-config", methods=["PUT"])
    def api_mail_config_save():
        """主管理员：切换邮件配置开关（写 .env）。

        支持：enabled（全局 YIBAN_MAIL_ENABLE）/ admin_notify（主管理员个人
        接收 YIBAN_MAIL_ADMIN_NOTIFY）。两者可单独或同时提交，均为 bool。
        """
        if not _is_builtin_admin_session():
            return jsonify({"error": "仅主管理员可操作"}), 403
        data = _json_body()
        resp = {"ok": True}
        changed = False
        if "enabled" in data:
            v = data["enabled"]
            if not isinstance(v, bool):
                return jsonify({"error": "取值无效"}), 400
            write_env_key(ENV_FILE, "YIBAN_MAIL_ENABLE", "1" if v else "0")
            resp["enabled"] = v
            changed = True
        if "admin_notify" in data:
            v = data["admin_notify"]
            if not isinstance(v, bool):
                return jsonify({"error": "取值无效"}), 400
            write_env_key(ENV_FILE, "YIBAN_MAIL_ADMIN_NOTIFY", "1" if v else "0")
            resp["admin_notify"] = v
            changed = True
        if not changed:
            return jsonify({"error": "缺少有效配置项"}), 400
        db.audit(
            session.get("username") or "?",
            "mail_config", "mail_config",
            json.dumps({k: v for k, v in resp.items() if k != "ok"}, ensure_ascii=False),
        )
        # 批次11 N6：邮件配置是全部安全告警的送达通道，变更即时告警——
        # 注意此时 .env 已写入新值，若管理员改劫持收件地址，本告警（按变更后
        # 配置发送）可能到不了运营者，故 webhook 通知与审计为主要留痕手段
        send_notification(
            "邮件配置变更告警",
            f"邮件通知配置已变更: {json.dumps({k: v for k, v in resp.items() if k != 'ok'}, ensure_ascii=False)}，"
            f"操作者 {session.get('username', '?')}，时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        )
        return jsonify(resp)

    # ---- 账号管理 ----
    @app.route("/api/accounts")
    def api_accounts():
        accounts = load_accounts()
        # 附带今日签到状态（键脱敏与 /api/logs 一致）：前端账号表格状态图标不再依赖
        # 单独的日志轮询（logs/accounts tab 各自可见时才请求对应接口，减少无效轮询）
        # 状态来源：signin.py 写的结构化状态文件（status 码），前端做图标映射
        states = load_sign_state()
        # 用户自暂停账号：状态直接呈现"已取消"（⏹️）——无需等下次签到执行写状态文件，
        # 管理员面板立即反映（2026-08-15 修复：此前仅 sign-state 有该状态时才会显示）
        for acc in accounts:
            if acc.get("user_paused"):
                states[acc.get("phone", "")] = {
                    "status": STATUS_USER_CANCELLED,
                    "message": "用户已取消签到",
                }
        # 调度 v2：自选时间（管理员查看每个用户选的片；slot_min → "HH:MM" + 首尾标记）
        prefs = {p: v["slot_min"] for p, v in db.get_time_prefs().items()}
        sw = _sign_window()
        _span_min = (sw[1][0] * 60 + sw[1][1]) - (sw[0][0] * 60 + sw[0][1])

        def _edge_mark(slot):
            if slot is None:
                return None
            if slot == 0:
                return "first"
            if slot >= _span_min - 5:
                return "last"
            return None

        return jsonify(
            {
                "ok": True,
                "accounts": [
                    {
                        **mask_account(a, i),
                        "time_pref": _slot_to_label(prefs.get(a["phone"])),
                        "time_pref_edge": _edge_mark(prefs.get(a["phone"])),
                    }
                    for i, a in enumerate(accounts)
                ],
                # states 值压成状态码字符串（前端图标映射用）
                "states": {
                    _mask_phone(k): (v.get("status", STATUS_PENDING) if isinstance(v, dict) else STATUS_PENDING)
                    for k, v in states.items()
                },
                # 状态原因/计划（如"计划 06:42"），前端表格 title 展示
                "state_msgs": {
                    _mask_phone(k): (v.get("message", "") if isinstance(v, dict) else "")
                    for k, v in states.items()
                },
                # 单次签到耗时秒数（P6）：表格状态 title 展示"耗时 xx s"；无记录为 None
                "state_durs": {
                    _mask_phone(k): (v.get("dur") if isinstance(v, dict) else None)
                    for k, v in states.items()
                },
                "config_file": os.path.basename(DB_FILE),
            }
        )

    @app.route("/api/accounts/<int:idx>/detail")
    def api_account_detail(idx):
        """账号完整信息（仅管理员；列表接口已脱敏，编辑/签到等操作按需取完整号）。"""
        accounts = load_accounts()
        if not 0 <= idx < len(accounts):
            return jsonify({"error": "账号不存在"}), 404
        return jsonify({"ok": True, "account": mask_account(accounts[idx], idx, masked=False)})

    @app.route("/api/accounts", methods=["POST"])
    def api_account_add():
        """添加账号。

        - 不填邮箱：管理员自有账号（owner=admin，直接生效）
        - 填用户邮箱：账号归属该用户并进入待审核（仍需管理员点"通过"）；
          邮箱未注册时自动创建网站用户（生成临时密码，需告知用户）。
        """
        # 操作级锁：手机号唯一/每人限 1/自动注册检查与写入原子（防并发重复添加与覆盖丢失）
        data = _json_body()
        err, clean = validate_account(data, require_password=True)
        if err:
            return jsonify({"error": err}), 400
        # P1-2 预筛（同 api_my_account_add，2026-08-27）：容量/手机号占用/内置邮箱
        # 占用先拦，注定失败的添加不再消耗易班网络验证。权威校验仍在下方写锁内。
        with _file_lock:
            accounts_pre = load_accounts()
            max_accounts_pre = load_env_int(ENV_FILE, "YIBAN_MAX_ACCOUNTS", DEFAULT_MAX_ACCOUNTS)
            if max_accounts_pre > 0 and len(accounts_pre) >= max_accounts_pre:
                _notify_capacity_once("accounts", max_accounts_pre, "账号数量")
                return jsonify({"error": f"账号数量已达上限（{max_accounts_pre}），请联系管理员扩容"}), 403
            if find_account_index(accounts_pre, clean["phone"]) is not None:
                return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400
            email_screen = str(data.get("email", "")).strip().lower()
            if email_screen and email_screen == _builtin_admin_email():
                return jsonify({"error": "内置管理员邮箱不可注册"}), 400
            # 域名预筛（2026-08-28）：注定被拒的占位/一次性域名不消耗易班验证配额
            if email_screen:
                dom_err = email_domain_error(email_screen)
                if dom_err:
                    return jsonify({"error": dom_err}), 400
        # R1：添加账号即时验证（管理员开启 YIBAN_ACCOUNT_VERIFY 后生效，验证失败当场打回）；
        # 验证尝试受每用户配额限制（P1-2）
        if _account_verify_enabled():
            if not _verify_attempt_allowed(
                    _verify_limits, str(session.get("username", ""))):
                return jsonify({"error": "账号验证尝试过于频繁，请稍后再试"}), 429
            verify_err = _verify_account_clean(clean)
            if verify_err:
                return jsonify({"error": verify_err}), 400
        email = str(data.get("email", "")).strip().lower()
        initial_hash = None  # 锁外预计算（scrypt ~100ms 不阻塞其他请求）
        if email:
            initial = str(data.get("initial_password", ""))
            if initial:
                pw_err = _password_policy_error(initial)
                if pw_err:
                    return jsonify({"error": f"初始密码不符合要求：{pw_err}"}), 400
                initial_hash = generate_password_hash(initial, method=SCRYPT_METHOD)
        with _file_lock:
            accounts = load_accounts()
            # 容量兜底：账号总数上限（防账号无限增长拖垮解密/轮询性能，对抗性审查补）
            max_accounts = load_env_int(ENV_FILE, "YIBAN_MAX_ACCOUNTS", DEFAULT_MAX_ACCOUNTS)
            if max_accounts > 0 and len(accounts) >= max_accounts:
                _notify_capacity_once("accounts", max_accounts, "账号数量")
                return jsonify({"error": f"账号数量已达上限（{max_accounts}），请联系管理员扩容"}), 403
            if find_account_index(accounts, clean["phone"]) is not None:
                return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400

            if email:
                if len(email.split("@")[0]) > EMAIL_USER_MAX:
                    return jsonify({"error": f"邮箱用户名部分过长（最多 {EMAIL_USER_MAX} 字符）"}), 400
                if not EMAIL_RE.match(email) or len(email) > 64:
                    return jsonify({"error": "用户邮箱格式不正确"}), 400
                # 邮箱域名审查（2026-08-28）：与开放注册同规则，防自动注册路径绕过
                dom_err = email_domain_error(email)
                if dom_err:
                    return jsonify({"error": dom_err}), 400
                # S1：内置管理员邮箱不可被自动注册占用
                if email.strip().lower() == _builtin_admin_email().strip().lower():
                    return jsonify({"error": "内置管理员邮箱不可注册"}), 400
                # 该用户已有账号（每人限 1 个）则拒绝（软删除的不占名额，与用户端一致）
                if any(a.get("owner") == email and not a.get("deleted") for a in accounts):
                    return jsonify({"error": f"{email} 已有一个账号，无需重复添加"}), 400
                # 自动注册：邮箱未注册则创建网站用户（初始密码由管理员在表单中设置，不生成明文临时密码）
                if db.find_user(email) is None:
                    if initial_hash is None:
                        return jsonify({"error": f"{email} 尚未注册，请填写「初始密码」为其创建首登密码"}), 400
                    # H14：自动注册同样受用户容量与注销冷却期约束
                    max_users = load_env_int(ENV_FILE, "YIBAN_MAX_USERS", DEFAULT_MAX_USERS)
                    if max_users > 0 and len(db.load_users()) >= max_users:
                        _notify_capacity_once("users", max_users, "注册人数")
                        return jsonify({"error": "注册人数已达上限，请联系管理员"}), 403
                    du = db.find_user_any(email)
                    if (
                        du is not None
                        and du.get("deleted")
                        and _delete_grace_remaining(du.get("deleted_at", "")) > 0
                    ):
                        return jsonify({"error": "该邮箱账号正在注销冷却期（7 天内可登录恢复）"}), 400
                    try:
                        created = db.create_user(
                            email, initial_hash, "user",
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 1,
                        )
                    except sqlite3.IntegrityError:
                        return jsonify({"error": "该邮箱已注册"}), 400  # 并发注册兜底
                    if not created:
                        return jsonify({"error": "该邮箱已注册"}), 400  # M10：OR IGNORE 未实际创建
                    logger.info("为邮箱 %s 自动注册用户（管理员设置初始密码）", _mask_email(email))
                clean["owner"] = email
                clean["status"] = ACCOUNT_STATUS_PENDING
            else:
                clean["owner"] = "admin"
                clean["status"] = ACCOUNT_STATUS_ACTIVE
            try:
                db.add_account(clean)
            except db.DuplicatePhoneError:
                return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400
            except db.DuplicateOwnerError:
                return jsonify({"error": "该用户已有一个账号，无需重复添加"}), 400
            except sqlite3.IntegrityError:
                return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400  # 并发重复兜底
            db.audit(
                session.get("username") or "?",
                "account_add",
                _mask_phone(clean["phone"]),
                f"归属 {_mask_email(clean['owner'])} 状态 {clean['status']}",
            )
            accounts = load_accounts()  # 重读（含新行，返回前端列表）
        logger.info(
            "添加账号 %s（归属 %s，状态 %s）",
            _mask_phone(clean["phone"]),
            _mask_email(clean["owner"]),
            clean["status"],
        )
        return jsonify(
            {
                "ok": True,
                "msg": "已添加，等待审核通过后参与签到",
                "accounts": [mask_account(a, i) for i, a in enumerate(accounts)],
            }
        )

    @app.route("/api/accounts/<int:idx>", methods=["PUT"])
    def api_account_update(idx):
        with _file_lock:
            accounts = load_accounts()
            if not 0 <= idx < len(accounts):
                return jsonify({"error": "账号不存在"}), 404
            old = accounts[idx]
            # 软删除账号禁止编辑（防编辑流程绕过软删除，恢复需走 restore 接口）
            if old.get("deleted"):
                return jsonify({"error": "账号已删除，请先恢复"}), 400
            data = _json_body()
            # 乐观锁：请求携带编辑打开时的账号快照（JSON 字符串），与库内当前值
            # 不一致 → db 返回 False → 409，防多管理员/多标签页并发编辑互相覆盖
            snapshot = None
            snapshot_raw = data.get("_snapshot") or ""
            if snapshot_raw:
                try:
                    snapshot = (
                        json.loads(snapshot_raw) if isinstance(snapshot_raw, str) else snapshot_raw
                    )
                except json.JSONDecodeError:
                    snapshot = None
            err, clean = validate_account(data, require_password=False)
            if err:
                return jsonify({"error": err}), 400
            # 手机号变更时检查冲突（排除自己）
            if (
                clean["phone"] != old.get("phone")
                and find_account_index(accounts, clean["phone"]) is not None
            ):
                return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400
            # 密码留空 = 保持不变（密码明文永不下发前端）
            if not clean["password"]:
                clean["password"] = old.get("password", "")
            # 设备识别码：__clear__ = 显式清空该字段；留空 = 保持不变（表单不预填防误清空）
            if clean["phone_code"] == CLEAR_SENTINEL:
                clean.pop("phone_code", None)
            elif not clean["phone_code"]:
                clean["phone_code"] = old.get("phone_code", "")
            # 归属与审核状态保持不变（管理员编辑不改变提交者与生效状态）
            clean["owner"] = old.get("owner", "admin")
            clean["status"] = old.get("status", ACCOUNT_STATUS_ACTIVE)
            try:
                result = db.update_account(
                    old["id"],
                    clean,
                    expect_snapshot=snapshot if isinstance(snapshot, dict) else None,
                )
            except db.DuplicatePhoneError:
                return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400
            except db.DuplicateOwnerError:
                return jsonify({"error": "该用户已有一个账号，无需重复添加"}), 400
            except sqlite3.IntegrityError:
                return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400  # 并发改号兜底
            if result is False:
                return jsonify({"error": "账号已被其他管理员修改，请刷新后重试"}), 409
            if result is None:
                return jsonify({"error": "账号不存在"}), 404
            # M11：手机号变更 → 旧号自选时间片失效，必须在 update_account 成功后再清，
            # 避免更新失败时误删旧号自选（防孤儿 pref 占容量，对抗性审查补）
            if clean["phone"] != old.get("phone"):
                db.clear_time_pref(old.get("phone", ""))
            # 凭据变更（改密码/识别码）后清除熔断暂停，立即恢复签到
            clear_fuse_pause(clean["phone"])
            db.audit(
                session.get("username") or "?",
                "account_update",
                _mask_phone(clean["phone"]),
                "编辑账号",
            )
            accounts = load_accounts()
            logger.info("编辑账号 %s", _mask_phone(clean["phone"]))
            return jsonify(
                {"ok": True, "accounts": [mask_account(a, i) for i, a in enumerate(accounts)]}
            )

    @app.route("/api/accounts/batch", methods=["POST"])
    def api_accounts_batch():
        """批量操作账号（批量多选功能）：approve/reject 审核，purge 彻底删除。

        body: {"action": ..., "ids": [...], "reason": "批量拒绝理由"}
        Phase 1：整体事务，失败全部回滚；无效项软跳过。
        """
        with _file_lock:
            accounts = load_accounts()
            data = _json_body()
            action = data.get("action")
            ids = data.get("ids") or []
            if action not in ("approve", "reject", "purge", "restore", "delete"):
                return jsonify({"error": "未知操作"}), 400
            if not isinstance(ids, list) or not ids:
                return jsonify({"error": "请选择要操作的账号"}), 400
            # 批次7 A3 + 2026-08-29 收紧：单次批量上限——被盗 admin 会话原本可用一个
            # 请求清空全部账号（≤500）；与 /api/users/deleted/purge 共用 BATCH_OP_LIMIT
            if len(ids) > BATCH_OP_LIMIT:
                return jsonify({"error": f"单次批量操作最多 {BATCH_OP_LIMIT} 个账号"}), 400
            reason = str(data.get("reason", "")).strip()[:100]
            if action == "reject" and not reason:
                return jsonify({"error": "批量拒绝需要填写理由"}), 400
            # 2026-08-20 对抗性审查 P1：idx 寻址防错位——客户端随 ids 携带对齐的
            # phones 数组，与服务端当前列表逐一比对，不一致整体拒绝（409 引导刷新）。
            # 另修 bool 混淆：isinstance(True, int) 为真，ids 里的 JSON true 会被
            # 当作索引 1，改用 type(i) is int 严格判定。
            valid = sorted(
                {i for i in ids if type(i) is int and 0 <= i < len(accounts)}
            )
            if not valid:
                return jsonify({"error": "所选账号不存在"}), 404
            phones_in = data.get("phones")
            if isinstance(phones_in, list) and len(phones_in) == len(ids):
                expect = {
                    i: str(phones_in[k]).strip()
                    for k, i in enumerate(ids)
                    if type(i) is int
                }
                # 双侧 _mask_phone 归一（出站为脱敏号，见 _stale_idx_guard 注释）
                if any(
                    i in expect
                    and _mask_phone(expect[i]) != _mask_phone(str(accounts[i].get("phone", "")))
                    for i in valid
                ):
                    return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409

            ops = []
            batch_targets = []  # 批次7 B3：审计留目标清单（脱敏截断）
            purge_targets = []  # 批次7 B4：高危操作（物理删除）即时告警汇总
            # 内存中跟踪每个 owner 当前是否有未删除账号，用于恢复防呆
            live_owners = {
                a.get("owner", "")
                for a in accounts
                if a.get("owner") and not a.get("deleted")
            }
            for i in valid:
                acc = accounts[i]
                if action == "approve":
                    # 软删除账号不可被审核通过（deleted 账号不参与审核流转）
                    if not acc.get("deleted") and acc.get("status") in (
                        ACCOUNT_STATUS_PENDING,
                        ACCOUNT_STATUS_REJECTED,
                    ):
                        ops.append(("update_status", acc["id"], ACCOUNT_STATUS_ACTIVE, ""))
                elif action == "reject":
                    if acc.get("status") in (ACCOUNT_STATUS_PENDING, ACCOUNT_STATUS_REJECTED):
                        ops.append(("update_status", acc["id"], ACCOUNT_STATUS_REJECTED, reason))
                elif action == "purge":
                    # 仅允许彻底删除「已软删除」账号（与单个彻底删除一致，防误删正常账号）
                    if acc.get("deleted"):
                        ops.append(("purge", acc["id"]))
                        purge_targets.append(_mask_phone(str(acc.get("phone", ""))))
                elif action == "restore":
                    if acc.get("deleted"):
                        owner = acc.get("owner", "")
                        if owner and owner != "admin" and owner in live_owners:
                            return jsonify(
                                {
                                    "error": f"账号「{acc.get('name', '')}」的归属用户已有生效账号，无法恢复（每人限 1 个）"
                                }
                            ), 400
                        ops.append(("set_deleted", acc["id"], 0, ""))
                        if owner:
                            live_owners.add(owner)
                elif action == "delete" and not acc.get("deleted"):
                    # 软删除：进入待删除列表（保留期内可恢复），与单个删除一致
                    ops.append(
                        ("set_deleted", acc["id"], 1, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    )
                batch_targets.append(acc.get("phone", ""))
            done = len(ops)
            if ops:
                try:
                    db.batch_account_ops(ops)
                    if purge_targets:
                        # 批次7 B4：高危操作即时告警（不等每日审计体检）
                        send_notification(
                            "高危管理操作告警",
                            f"批量彻底删除账号 ×{len(purge_targets)}: "
                            f"{', '.join(purge_targets[:20])}，"
                            f"操作者 {session.get('username', '?')}，时间 "
                            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                        )
                except db.DuplicateOwnerError:
                    db.audit(
                        session.get("username") or "?",
                        "account_batch",
                        action,
                        "失败，已回滚（该用户已有一个账号）",
                    )
                    return jsonify({"error": "批量操作失败，已全部回滚（该用户已有一个账号）"}), 400
                except Exception as e:
                    logger.error("批量%s账号失败: %s（已回滚）", action, e)
                    db.audit(
                        session.get("username") or "?",
                        "account_batch",
                        action,
                        "失败，已回滚",
                    )
                    return jsonify({"error": "批量操作失败，已全部回滚"}), 500
            db.audit(
                session.get("username") or "?",
                "account_batch",
                action,
                # 批次7 B3：批量操作留目标清单（脱敏截断），破坏事后可从审计还原"动了谁"
                (f"处理 {done} 个: " + ",".join(
                    _mask_phone(str(p)) for p in (batch_targets or [])[:20]
                ))[:200],
            )
            accounts = load_accounts()
            logger.info("批量%s账号 %d 个", action, done)
            msg = {
                "approve": f"已通过 {done} 个账号",
                "reject": f"已拒绝 {done} 个账号",
                "purge": f"已彻底删除 {done} 个账号",
                "restore": f"已恢复 {done} 个账号",
                "delete": f"已删除 {done} 个账号（可恢复）",
            }[action]
            return jsonify(
                {
                    "ok": True,
                    "msg": msg,
                    "accounts": [mask_account(a, i) for i, a in enumerate(accounts)],
                }
            )

    @app.route("/api/accounts/<int:idx>", methods=["DELETE"])
    def api_account_delete(idx):
        """删除账号（软删除）：进入待删除状态，保留期内可恢复，超期自动彻底清除。"""
        with _file_lock:
            accounts = load_accounts()
            if not 0 <= idx < len(accounts):
                return jsonify({"error": "账号不存在"}), 404
            acc = accounts[idx]
            if _stale_idx_guard(acc, _json_body()):
                return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
            db.set_account_deleted(
                acc["id"], 1, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                deleted_by="admin",
            )
            db.audit(
                session.get("username") or "?",
                "account_delete",
                _mask_phone(acc.get("phone", "")),
                "软删除",
            )
            accounts = load_accounts()
            logger.info(
                "软删除账号 %s（%s 天内可恢复）", _mask_phone(acc.get("phone", "")), DELETED_RETENTION_DAYS
            )
            return jsonify(
                {
                    "ok": True,
                    "msg": f"已删除「{acc.get('name', '')}」，{DELETED_RETENTION_DAYS} 天内可在待删除列表恢复",
                    "accounts": [mask_account(a, i) for i, a in enumerate(accounts)],
                }
            )

    @app.route("/api/accounts/<int:idx>/restore", methods=["POST"])
    def api_account_restore(idx):
        """恢复待删除账号：撤销软删除，回到删除前状态。"""
        with _file_lock:
            accounts = load_accounts()
            if not 0 <= idx < len(accounts):
                return jsonify({"error": "账号不存在"}), 404
            acc = accounts[idx]
            if _stale_idx_guard(acc, _json_body()):
                return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
            if not acc.get("deleted"):
                return jsonify({"error": "该账号不在待删除状态"}), 400
            # 防呆：归属用户名下已有其他未删除账号则拒绝恢复（每人限 1 个，防恢复后重复）
            if _owner_has_other_live(accounts, acc):
                return jsonify(
                    {"error": "该用户已有生效账号，无法恢复（每人限 1 个）"}
                ), 400
            db.set_account_deleted(acc["id"], 0)
            db.audit(
                session.get("username") or "?",
                "account_restore",
                _mask_phone(acc.get("phone", "")),
                "撤销软删除",
            )
            accounts = load_accounts()
            logger.info("恢复账号 %s", _mask_phone(acc.get("phone", "")))
            return jsonify(
                {
                    "ok": True,
                    "msg": f"已恢复「{acc.get('name', '')}」",
                    "accounts": [mask_account(a, i) for i, a in enumerate(accounts)],
                }
            )

    @app.route("/api/accounts/<int:idx>/purge", methods=["POST"])
    def api_account_purge(idx):
        """彻底删除待删除账号：立即物理清除，不可恢复。"""
        with _file_lock:
            accounts = load_accounts()
            if not 0 <= idx < len(accounts):
                return jsonify({"error": "账号不存在"}), 404
            acc = accounts[idx]
            if _stale_idx_guard(acc, _json_body()):
                return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
            if not acc.get("deleted"):
                return jsonify({"error": "该账号不在待删除状态"}), 400
            db.purge_account(acc["id"])
            db.audit(
                session.get("username") or "?",
                "account_purge",
                _mask_phone(acc.get("phone", "")),
                "彻底删除",
            )
            accounts = load_accounts()
            logger.info("彻底删除账号 %s", _mask_phone(acc.get("phone", "")))
            return jsonify(
                {
                    "ok": True,
                    "msg": f"已彻底删除「{acc.get('name', '')}」",
                    "accounts": [mask_account(a, i) for i, a in enumerate(accounts)],
                }
            )

    @app.route("/api/accounts/<int:idx>/review", methods=["POST"])
    def api_account_review(idx):
        """审核普通用户提交的账号：
        approve=生效参与定时签到；reject=标记拒绝并附理由（用户可编辑后重新提交）。
        """
        with _file_lock:
            accounts = load_accounts()
            if not 0 <= idx < len(accounts):
                return jsonify({"error": "账号不存在"}), 404
            data = _json_body()
            action = data.get("action")
            acc = accounts[idx]
            if _stale_idx_guard(acc, data):
                return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
            if action == "approve":
                # 软删除账号不可被审核通过（deleted 账号不参与审核流转）
                if acc.get("deleted") or acc.get("status") not in (ACCOUNT_STATUS_PENDING, ACCOUNT_STATUS_REJECTED):
                    return jsonify({"error": "该账号无需审核"}), 400
                db.update_account_status(acc["id"], ACCOUNT_STATUS_ACTIVE, reject_reason="")
                db.audit(
                    session.get("username") or "?",
                    "account_review",
                    _mask_phone(acc.get("phone", "")),
                    "approve",
                )
                logger.info("审核通过账号 %s（提交者 %s）", _mask_phone(acc.get("phone", "")), _mask_email(acc.get("owner", "")))
                # 回显脱敏（与列表口径一致，防响应混入完整 PII；管理员详情页可取完整号）
                return jsonify({"ok": True, "msg": f"已通过 {_mask_phone(acc.get('phone', ''))}，将参与定时签到"})
            if action == "reject":
                if acc.get("status") not in (ACCOUNT_STATUS_PENDING, ACCOUNT_STATUS_REJECTED):
                    return jsonify({"error": "该账号无需拒绝"}), 400
                # 理由清洗：换行/控制字符 → 空格（防日志注入伪造日志行）
                reason = (
                    str(data.get("reason", ""))
                    .strip()[:100]
                    .replace("\r", " ")
                    .replace("\n", " ")
                )
                db.update_account_status(acc["id"], ACCOUNT_STATUS_REJECTED, reason)
                db.audit(
                    session.get("username") or "?",
                    "account_review",
                    _mask_phone(acc.get("phone", "")),
                    "reject" + (f" {reason[:60]}" if reason else ""),
                )
                logger.info(
                    "拒绝账号 %s（提交者 %s，理由: %s）",
                    _mask_phone(acc.get("phone", "")),
                    _mask_email(acc.get("owner", "")),
                    reason or "无",
                )
                return jsonify({"ok": True, "msg": "已拒绝，用户可查看理由并重新提交"})
        return jsonify({"error": "未知操作"}), 400

    @app.route("/api/accounts/<int:idx>/move", methods=["POST"])
    def api_account_move(idx):
        """上移/下移账号：调整顺序模式下的打卡顺序。body: {"dir": -1|1}"""
        with _file_lock:
            accounts = load_accounts()
            if not 0 <= idx < len(accounts):
                return jsonify({"error": "账号不存在"}), 404
            data = _json_body()
            acc = accounts[idx]
            if _stale_idx_guard(acc, data):
                return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
            try:
                direction = int(data.get("dir", 0))
            except (TypeError, ValueError):
                return jsonify({"error": "无法移动"}), 400
            if direction not in (-1, 1):
                return jsonify({"error": "无法移动"}), 400
            if not db.move_account(acc["id"], direction):
                return jsonify({"error": "无法移动"}), 400
            db.audit(
                session.get("username") or "?",
                "account_move",
                _mask_phone(acc.get("phone", "")),
                f"dir {direction}",
            )
            accounts = load_accounts()
            return jsonify(
                {"ok": True, "accounts": [mask_account(a, i) for i, a in enumerate(accounts)]}
            )

    # ---- 普通用户：我的账号（提交 / 查看 / 编辑 / 删除，仅限本人）----
    def _my_account_indices_of(accounts):
        """按账号列表快照计算当前用户的账号下标（锁内调用，避免重复读文件）。

        管理员：内置管理员（.env）显示 owner 'admin' + 本人邮箱；注册管理员仅本人邮箱
        （不显示他人/内置管理员添加的账号）；均不含待删除。
        普通用户：本人邮箱（含待删除，用于展示「已删除」状态；单账号限制在提交处另行排除）。
        """
        email = session.get("username", "").lower()
        if _current_role() == "admin":
            if _is_builtin_admin_session():
                return [
                    i
                    for i, a in enumerate(accounts)
                    if a.get("owner") in ("admin", email) and not a.get("deleted")
                ]
            return [
                i for i, a in enumerate(accounts) if a.get("owner") == email and not a.get("deleted")
            ]
        return [i for i, a in enumerate(accounts) if a.get("owner") == email]

    def _my_account_indices():
        return _my_account_indices_of(load_accounts())

    def _my_account_view(accounts, indices):
        """用户视图：账号脱敏 + 今日状态（结构化状态文件）+ 审核状态 + 最近相关日志 + 排队信息。

        排队说明：开启签到调度时按当日计划时间执行，否则按账号列表顺序（队列重试）；
        queue_ahead = 自己账号之前、今日尚未了结（未 success/already/no_task）的已生效账号数。
        """
        recent = parse_sign_log(log_path_for())  # 最近日志仅用于「最近签到记录」展示（按天文件 = 今天）
        states = load_sign_state()  # 今日状态事实源（signin.py 写入）
        # 参与排队队列的账号：已生效（active，pending 不参与签到）且未软删除、未自暂停
        active = [
            a for a in accounts
            if a.get("status") == ACCOUNT_STATUS_ACTIVE and not a.get("deleted")
            and not a.get("user_paused", False)
        ]
        # 执行顺序（调度 v2，2026-08-15 改进）：优先按今日计划时间（sign-state scheduled 字段，
        # cron 生成后即真实执行顺序——覆盖自选/正态/随机模式）；计划未生成（06:31 前）回退列表顺序。
        # scheduled 为 "HH:MM:SS" 字符串，字典序即时间序；无计划者排在有计划者之后（列表序兜底）。
        def _exec_order_key(a):
            st = states.get(a.get("phone", ""), {})
            sched = st.get("scheduled", "") if isinstance(st, dict) else ""
            return (0 if sched else 1, sched, a.get("sort_order", 0))

        active_sorted = sorted(active, key=_exec_order_key)
        # 排队位置预计算（单次遍历累计，替代每个账号 O(pos) 切片求和）
        queue_before = {}
        running = 0
        for a in active_sorted:
            queue_before[a.get("phone", "")] = running
            st_status = states.get(a.get("phone", ""), {}).get("status", STATUS_PENDING)
            if st_status not in (STATUS_SUCCESS, STATUS_ALREADY, STATUS_NO_TASK):
                running += 1
        # 今日前缀：账号卡片「最近签到记录」只显示今天的日志（日志文件跨多天时避免混入历史）
        today_prefix = f"[{datetime.now().strftime('%Y-%m-%d')} "
        result = []
        for i, real_idx in enumerate(indices):
            acc = accounts[real_idx]
            phone = acc.get("phone", "")
            my_logs = [
                line for line in recent
                if line.startswith(today_prefix) and f"[{phone}]" in line
            ]
            # 排队：按今日计划时间排序的队列中，自己之前未了结的账号数（含自暂停排除）
            queue_ahead = 0
            if acc.get("status") == ACCOUNT_STATUS_ACTIVE and not acc.get("user_paused", False):
                queue_ahead = queue_before.get(phone, 0)
            st = states.get(phone, {})
            st_status = st.get("status", STATUS_PENDING) if isinstance(st, dict) else STATUS_PENDING
            result.append(
                {
                    "index": i,
                    "name": acc.get("name", ""),
                    "display_name": acc.get("name") or f"账号{i + 1}",
                    "phone": phone,
                    "phone_model": acc.get("phone_model", ""),
                    "status": acc.get("status", ACCOUNT_STATUS_ACTIVE),
                    "reject_reason": acc.get("reject_reason", ""),
                    "state_icon": STATUS_ICON.get(st_status, "⏳"),
                    "state_status": st_status,  # 状态码（前端按码映射文案）
                    "state_message": st.get("message", "") if isinstance(st, dict) else "",
                    "queue_ahead": queue_ahead,
                    # 出站脱敏（2026-08-20 对抗性审查 P3）：与 /api/my-logs、/api/logs
                    # 统一口径——当前 signin.py 日志每行仅含本人手机号，但口径不设防时，
                    # 未来出现一行多号的日志格式会把他人号码原样下发普通用户
                    "logs": [_mask_log_phones(ln) for ln in my_logs[-5:]],
                    "deleted": bool(acc.get("deleted")),
                    "deleted_at": acc.get("deleted_at", ""),
                    # v10 用户删除可撤销：仅本人自删行（deleted_by=本人）前端展示撤销入口
                    "deleted_by_me": bool(acc.get("deleted"))
                    and acc.get("deleted_by", "")
                    == (session.get("username", "") or "").strip().lower(),
                    "user_paused": bool(acc.get("user_paused", False)),  # 用户自暂停（调度 v2）
                    # 2026-08-15 用户确认：管理员账号（owner=admin）不支持自暂停——
                    # 暂停是普通用户管理自己账号的能力；该字段仅管理员视图可见（前端据此隐藏按钮）
                    "pause_forbidden": acc.get("owner", "admin") == "admin" and not acc.get("deleted"),
                }
            )
        return result

    @app.route("/api/my-accounts")
    def api_my_accounts():
        # 单快照（2026-08-20 对抗性审查 P3 修复）：原实现 load_accounts() 后
        # _my_account_indices() 内部再次读取，两次读之间其他线程的物理删除会使
        # S2 的下标套在 S1 上错位，极端时短暂展示他人账号（含完整手机号）
        accounts = load_accounts()
        indices = _my_account_indices_of(accounts)
        return jsonify({"ok": True, "accounts": _my_account_view(accounts, indices)})

    # ---- 用户自选时间片（调度 v2，docs/design/plan-scheduler-v2.md 2.2）----
    def _my_phone():
        """当前用户的自选绑定账号（2026-08-15 修复：与「我的账号」视图同口径）。

        普通用户=本人账号；内置管理员=归属 admin/本人邮箱的账号；注册管理员=归属本人邮箱的账号。
        ——此前 admin 分支硬编码 owner='admin'，导致注册管理员也绑定到内置管理员的账号，
        选片显示/保存互相覆盖（用户实测报告）。
        仅 status=active（正式进入签到列表）才算——pending/rejected 的"注册但未生效"
        用户不可查看/选择时间片（GET 返回 has_account=False → 前端整卡隐藏；PUT 400 兜底）。
        """
        accounts = load_accounts()
        for idx in _my_account_indices_of(accounts):
            acc = accounts[idx]
            if not acc.get("deleted") and acc.get("status") == ACCOUNT_STATUS_ACTIVE:
                return acc.get("phone", "")
        return None

    def _pref_slots(sw):
        """窗口内 5 分钟片（时钟对齐）：[{slot_min, label, disabled, edge_note}]。

        0.22.0 掐头去尾前后独立：完全落入裁剪区（裁剪 >= 5 分钟覆盖整块）的片标记
        disabled（前端灰色不可选）；部分落入（如前裁 2 分钟 → 首片剩 3 分钟可用）的片
        标记 edge_note 提示且仍可点选（调度在可用部分内安排）。返回全部片（含 disabled），
        前端据此渲染，保证"满 5 分钟才完全灰掉、不足时提示"的需求语义。
        """
        start_min = sw[0][0] * 60 + sw[0][1]
        end_min = sw[1][0] * 60 + sw[1][1]
        span = end_min - start_min
        front_min = edge_config()[0] / 60.0
        back_min = edge_config()[1] / 60.0
        slots = []
        for b in range(start_min, end_min, 5):
            off = b - start_min  # 片起点相对窗口起点的分钟偏移
            lo = max(off, front_min)
            hi = min(off + 5, span - back_min)
            disabled = hi <= lo  # 整片在裁剪区内（裁剪值 >= 5 分钟覆盖）
            note = ""
            if not disabled and (off < front_min or off + 5 > span - back_min):
                # 部分裁剪：提示"开头/结尾保留 X 分钟"，仍可选
                if off < front_min:
                    note = f"开头 {front_min:g} 分钟保留"
                else:
                    note = f"结尾 {back_min:g} 分钟保留"
            slots.append({
                "slot_min": off,
                "label": f"{b // 60:02d}:{b % 60:02d}",
                "disabled": disabled,
                "edge_note": note,
            })
        return slots

    @app.route("/api/my-time-pref")
    def api_my_time_pref():
        """我的自选 + 拥挤度 + 预计签到时段（选片卡片数据；总开关关时仍可预配置，调度侧不激活）。

        拥挤度防调研（2026-08-15 用户决策）：普通用户端只下发「已选百分比」（整数，四舍五入），
        不下发真实人数/块容量——不知道 K 无法反推人数；管理端 stats 接口保留精确计数。
        """
        sw = _sign_window()
        phone = _my_phone()
        pref = db.get_time_pref(phone) if phone else None
        stats = {s["slot_min"]: s["count"] for s in db.time_pref_stats()}
        cap = load_env_int(ENV_FILE, "YIBAN_BLOCK_CAP", 15)
        slots = []
        for s in _pref_slots(sw):
            count = stats.get(s["slot_min"], 0)
            # 粗粒度 10% 档（对抗性审查补）：精确百分比 + 已知默认 K 可反推人数；
            # 未满封顶 90、满员恰好 100——前端 pct>=100 判满精确（19/20=95% 不会再被
            # 四舍五入成 100 误报"已选满"，与后端 count>=cap 口径一致）
            if cap > 0:
                pct = 100 if count >= cap else min(90, round(count * 100 / cap / 10) * 10)
            else:
                pct = 0
            slots.append({
                "slot_min": s["slot_min"],
                "label": s["label"],
                "pct": pct,
                "disabled": s["disabled"],     # 完全在裁剪区 → 灰色不可选（0.22.0）
                "edge_note": s["edge_note"],   # 部分裁剪提示（如"开头 2 分钟保留"）
            })
        estimated, estimate_note = _estimate_slot(phone) if phone else (None, "")
        front_sec, back_sec = edge_config()
        return jsonify({
            "ok": True,
            "pref": _slot_to_label(pref["slot_min"]) if pref else None,
            "pref_slot": pref["slot_min"] if pref else None,
            "slots": slots,
            "allowed": load_env_int(ENV_FILE, "YIBAN_ALLOW_TIME_PREF", 0) == 1,
            "window": f"{sw[0][0]:02d}:{sw[0][1]:02d} ~ {sw[1][0]:02d}:{sw[1][1]:02d}",
            "edge_sec": front_sec,                    # 兼容旧前端（=前裁）
            "edge_front_sec": front_sec,              # 0.22.0 前后独立
            "edge_back_sec": back_sec,
            "has_account": bool(phone),
            "estimated": estimated,        # 预计签到时段（顺序排序可预期；随机为 None）
            "estimate_note": estimate_note,
        })

    @app.route("/api/my-time-pref", methods=["PUT"])
    def api_my_time_pref_save():
        """保存/清除自选：{slot_min: int|null}。校验 5 对齐、窗口内；生效按分界时刻提示。"""
        # F3 对抗性审查（TOCTOU）：read-check-write 整段持 _file_lock 原子化——
        # 并发 purge 删号时不再重新插入孤儿 pref（已删 phone 残留→重占号被新账号继承泄漏）；
        # 冷却检查与写入原子化（防并发双请求绕过冷却）；满员统计与写入原子化（防超容写入）
        with _file_lock:
            phone = _my_phone()
            if not phone:
                # 2026-08-15 用户反馈：非正式用户不可选时间片——区分"未提交"与"已提交未生效"，
                # 提示不给待审核用户误导（信息分层，不暴露审核细节）
                # 低项：has_submitted 统一走 _my_account_indices，避免管理员/普通用户双口径漂移
                # 单快照读取（2026-08-21 对抗性审查 P3：消除双读间列表漂移的越界/错判窗口）
                _accounts_now = load_accounts()
                has_submitted = any(
                    not _accounts_now[i].get("deleted")
                    for i in _my_account_indices_of(_accounts_now)
                )
                if has_submitted:
                    return jsonify({"error": "账号审核通过后即可选择签到时间"}), 400
                return jsonify({"error": "请先提交易班账号"}), 400
            data = _json_body()
            slot = data.get("slot_min")
            if slot is None:
                db.clear_time_pref(phone)
                db.audit(session.get("username", "?"), "time_pref_clear", db.hash_phone(phone), "")
                return jsonify({"ok": True, "msg": "已清除自选，恢复自动分配"})
            # M1 对抗性审查：严格类型校验——bool（False→0）与小数（5.9→5）截断不得误入合法槽位
            if isinstance(slot, bool) or (isinstance(slot, float) and not slot.is_integer()):
                return jsonify({"error": "时间片取值无效"}), 400
            try:
                slot = int(slot)
            except (TypeError, ValueError):
                return jsonify({"error": "时间片取值无效"}), 400
            sw = _sign_window()
            span = (sw[1][0] * 60 + sw[1][1]) - (sw[0][0] * 60 + sw[0][1])
            front_min = edge_config()[0] / 60.0
            back_min = edge_config()[1] / 60.0
            # 0.22.0 前后独立裁剪：部分落入裁剪区的片（如首片剩 3 分钟）允许保存，
            # 调度会在可用部分内安排；完全落入裁剪区（前端已置灰）拒绝。
            if slot % 5 != 0 or not (0 <= slot < span) or not (
                slot + 5 > front_min and slot < span - back_min
            ):
                # 不暴露"5 分钟对齐"等调度机制细节（信息分层，2026-08-15）
                return jsonify({"error": "所选时间片不在可选范围内，请重新选择"}), 400
            # 弹性切换冷却（2026-08-15 用户反馈）：60s 窗口内自由次数内完全放行（浏览式
            # "全点一遍再定"正常）；超出后冷却随超限次数递增（30s→60s→120s→…封顶 300s），
            # 持续高频才被压制。按被选账号计价（H3 多管理员共享全局生效；H4 新号豁免）。
            # 时长可配（YIBAN_TIME_PREF_COOLDOWN_SEC 基础值，默认 30；0=关闭）
            base_cd = load_env_int(ENV_FILE, "YIBAN_TIME_PREF_COOLDOWN_SEC", TIME_PREF_COOLDOWN_SEC)
            if base_cd > 0:
                now_ts = datetime.now()
                since = (now_ts - timedelta(seconds=TIME_PREF_COOLDOWN_WINDOW)
                         ).strftime("%Y-%m-%d %H:%M:%S")
                count = db.time_pref_set_count_since(phone, since)
                if count >= TIME_PREF_COOLDOWN_FREE:
                    # 弹性冷却 = 基础 × 2^(超限次数)，封顶
                    cooldown = min(base_cd * (2 ** (count - TIME_PREF_COOLDOWN_FREE + 1)),
                                   TIME_PREF_COOLDOWN_MAX)
                    last_ts = db.last_time_pref_set_at(phone)
                    if last_ts:
                        try:
                            last_dt = datetime.strptime(str(last_ts), "%Y-%m-%d %H:%M:%S")
                            elapsed = (now_ts - last_dt).total_seconds()
                            # 负间隔（时钟回拨）视为已过冷却，不误伤（对抗性审查第三轮）
                            if 0 <= elapsed < cooldown:
                                # 不暴露冷却时长（信息分层）
                                return jsonify({"error": "切换过于频繁，请稍后再试"}), 429
                        except ValueError:
                            # ts 格式异常（写坏）：保守按冷却生效拦截（M3：防 fail-open 绕过）
                            return jsonify({"error": "切换过于频繁，请稍后再试"}), 429
            # 满员提示（对抗性审查补，2026-08-15 用户决策：可继续选+提示会顺延）：
            # 该片已选人数 ≥ 块容量时仍允许保存（先到先得+溢出顺延语义），但明确告知；
            # 提示不暴露真实人数/容量（防调研，与用户端 pct 口径一致）
            cap = load_env_int(ENV_FILE, "YIBAN_BLOCK_CAP", 15)
            stats = {s["slot_min"]: s["count"] for s in db.time_pref_stats()}
            count = stats.get(slot, 0)
            cur = db.get_time_pref(phone)
            if cur and cur.get("slot_min") == slot:
                count = max(0, count - 1)  # 排除自己已占的位（换片/保留不误报）
            # 低项：cap=0（不限容量）时不应提示“已选满”
            full_notice = "，该时段已选满，将就近安排到附近时段" if (cap > 0 and count >= cap) else ""
            # updated_at 带微秒（M2 对抗性审查）：同秒保存的"先到先得"可区分先后，
            # 不再退化为按 phone 顺序的不可预期平局（字典序定宽，旧秒级数据兼容为更早）
            db.set_time_pref(phone, slot, datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"))
            db.audit(session.get("username", "?"), "time_pref_set", db.hash_phone(phone), _slot_to_label(slot))
            # 生效分界（2026-08-15 用户反馈：卡点缓冲）：
            # 优先用当日调度快照标记（signin 构建调度后写入 sched-snapshot-YYYY-MM-DD.json，
            # 精确等于 cron 实际读取自选表的时刻）——改选在快照后必为"明日生效"，提示与实际 100% 一致；
            # 标记不存在（当日 cron 未运行/自选未激活）回退"窗口起点 + 1 分钟"兜底
            now = datetime.now()
            boundary = None
            try:
                snap_path = os.path.join(STATE_DIR, f"sched-snapshot-{now.strftime('%Y-%m-%d')}.json")
                with open(snap_path, encoding="utf-8") as f:
                    snap = json.load(f)
                boundary = datetime.strptime(
                    f"{now.strftime('%Y-%m-%d')} {snap['snapshot_at']}", "%Y-%m-%d %H:%M:%S"
                )
                # H1 对抗性审查：快照标记晚于当前时刻（时钟偏移/写坏）→ 视为无效回退兜底，
                # 避免"提示今日生效但实际不可能"（改选时 cron 早已建表）
                if boundary > now:
                    boundary = None
            except (OSError, ValueError, KeyError, TypeError):
                boundary = None
            if boundary is None:
                try:
                    boundary = now.replace(hour=sw[0][0], minute=sw[0][1], second=0, microsecond=0)
                except ValueError:
                    boundary = now
                boundary += timedelta(minutes=1)
            when = "今日生效" if now < boundary else "明日生效"
            return jsonify({"ok": True, "msg": f"已保存自选 {_slot_to_label(slot)}，{when}{full_notice}"})

    @app.route("/api/time-prefs/stats")
    def api_time_prefs_stats():
        """每片已选人数（拥挤度，管理员；用户端由 my-time-pref 附带，不单独暴露）。"""
        sw = _sign_window()
        stats = {s["slot_min"]: s["count"] for s in db.time_pref_stats()}
        cap = load_env_int(ENV_FILE, "YIBAN_BLOCK_CAP", 15)
        return jsonify({
            "ok": True,
            "slots": [{**s, "count": stats.get(s["slot_min"], 0), "cap": cap}
                      for s in _pref_slots(sw)],
        })

    @app.route("/api/my-accounts", methods=["POST"])
    def api_my_account_add():
        """提交自己的易班账号：每个用户仅限 1 套，写入 accounts 表状态 pending（待审核）。

        操作级锁：单账号限制与手机号唯一检查 + 写入原子（防并发双提交互相覆盖）。
        """
        data = _json_body()
        err, clean = validate_account(data, require_password=True)
        if err:
            return jsonify({"error": err}), 400
        # P1-2 预筛（2026-08-27 对抗性审查）：资格校验全部前置到网络验证之前，
        # 杜绝「先向易班发起真实登录、再发现根本没资格」的凭据试探滥用面。
        # 权威校验仍保留在下方写入临界区（预筛通过≠最终名额，双检以锁内为准）。
        email_pre = str(session.get("username", "")).lower()
        with _file_lock:
            accounts_pre = load_accounts()
            max_accounts_pre = load_env_int(ENV_FILE, "YIBAN_MAX_ACCOUNTS", DEFAULT_MAX_ACCOUNTS)
            if max_accounts_pre > 0 and len(accounts_pre) >= max_accounts_pre:
                _notify_capacity_once("accounts", max_accounts_pre, "账号数量")
                # 不向普通用户暴露容量数字（信息分层，2026-08-15）
                return jsonify({"error": "账号数量已达上限，请联系管理员"}), 403
            if any(a.get("owner") == email_pre and not a.get("deleted") for a in accounts_pre):
                return jsonify({"error": "每个用户只能提交一个账号，可编辑或删除后重新提交"}), 400
            if find_account_index(accounts_pre, clean["phone"]) is not None:
                err = _duplicate_phone_error(accounts_pre, clean["phone"], email_pre)
                if err:
                    return jsonify({"error": err}), 400
                return jsonify({"error": f"手机号 {clean['phone']} 已被使用"}), 400
        # R1：用户提交账号即时验证（管理员开启 YIBAN_ACCOUNT_VERIFY 后生效，验证失败当场打回）
        # 放锁外：verify 为网络操作，不阻塞其他请求；验证尝试受每用户配额限制（P1-2）
        if _account_verify_enabled():
            if not _verify_attempt_allowed(_verify_limits, email_pre):
                return jsonify({"error": "账号验证尝试过于频繁，请稍后再试"}), 429
            verify_err = _verify_account_clean(clean)
            if verify_err:
                return jsonify({"error": verify_err}), 400
        with _file_lock:
            accounts = load_accounts()
            # 容量兜底：账号总数上限（用户提交同样受限，对抗性审查补）
            max_accounts = load_env_int(ENV_FILE, "YIBAN_MAX_ACCOUNTS", DEFAULT_MAX_ACCOUNTS)
            if max_accounts > 0 and len(accounts) >= max_accounts:
                _notify_capacity_once("accounts", max_accounts, "账号数量")
                # 不向普通用户暴露容量数字（信息分层，2026-08-15）
                return jsonify({"error": "账号数量已达上限，请联系管理员"}), 403
            # 单账号限制：已有未删除提交（含待审核/已生效）则拒绝；待删除（管理员已删）不占名额
            email = session.get("username", "").lower()
            has_live = any(a.get("owner") == email and not a.get("deleted") for a in accounts)
            if has_live:
                return jsonify({"error": "每个用户只能提交一个账号，可编辑或删除后重新提交"}), 400
            if find_account_index(accounts, clean["phone"]) is not None:
                err = _duplicate_phone_error(accounts, clean["phone"], email)
                if err:
                    return jsonify({"error": err}), 400
                return jsonify({"error": f"手机号 {clean['phone']} 已被使用"}), 400
            # 管理员提交的账号归属 'admin'（后台添加账号同理），直接生效免审核
            clean["owner"] = (
                "admin" if _current_role() == "admin" else session.get("username", "").lower()
            )
            clean["status"] = ACCOUNT_STATUS_PENDING if _current_role() != "admin" else ACCOUNT_STATUS_ACTIVE
            try:
                db.add_account(clean)
            except sqlite3.IntegrityError:
                return jsonify({"error": f"手机号 {clean['phone']} 已被使用"}), 400  # 并发提交兜底
            db.audit(
                clean["owner"],
                "my_account_add",
                _mask_phone(clean["phone"]),
                f"用户提交 状态 {clean['status']}",
            )
            logger.info("用户 %s 提交账号 %s（待审核）", _mask_email(clean["owner"]), _mask_phone(clean["phone"]))
            return jsonify({"ok": True, "msg": "已提交，等待管理员审核后参与签到"})

    @app.route("/api/my-calendar")
    def api_my_calendar():
        """我的账号月历：返回指定月份（YYYY-MM）每天每账号的签到状态（✅/❌/空字符串）。"""
        month = str(request.args.get("month", "")).strip()
        try:
            year, mon = map(int, month.split("-"))
            if not (2000 <= year <= 2100 and 1 <= mon <= 12):
                raise ValueError
        except Exception:
            return jsonify({"error": "月份格式不正确，应为 YYYY-MM"}), 400
        accounts = load_accounts()
        indices = _my_account_indices_of(accounts)  # 单快照：防两次读取间列表漂移（同 api_my_accounts）
        phones = [str(accounts[i].get("phone", "")) for i in indices]
        days_in_month = calendar.monthrange(year, mon)[1]
        result = {f"{year:04d}-{mon:02d}-{d:02d}": {} for d in range(1, days_in_month + 1)}
        # 聚合读取：单次目录遍历取本月全部日文件（替代每天一次 exists+open 共 30 次 IO）
        prefix = f"sign-daily-{year:04d}-{mon:02d}-"
        try:
            for entry in os.scandir(STATE_DIR):
                if entry.name.startswith(prefix):
                    date = entry.name[len("sign-daily-") : -len(".json")]
                    try:
                        with open(entry.path, encoding="utf-8") as f:
                            daily = json.load(f)
                    except Exception:
                        daily = {}
                    # setdefault：异常文件名（非 YYYY-MM-DD）不落入本月键时自动补空，防 KeyError 500
                    result.setdefault(date, {}).update({p: daily.get(p, "") for p in phones})
        except OSError:
            pass  # STATE_DIR 不存在等：按无记录返回
        return jsonify({
            "ok": True,
            "month": month,
            "days": result,
            "sunday_sign": load_env_int(ENV_FILE, "YIBAN_SUNDAY_SIGN", 0),  # 前端据此决定周日是否置灰/可查
            "saturday_sign": load_env_int(ENV_FILE, "YIBAN_SATURDAY_SIGN", 1),  # 前端据此决定周六是否置灰/可查
        })

    @app.route("/api/my-logs")
    def api_my_logs():
        """我的账号指定日期（YYYY-MM-DD）的日志（按手机号过滤，最多 50 条）。

        2026-08-16 起读按天文件（sign-YYYY-MM-DD.log）：历史日期同样可查
        （此前只读当前 sign.log，轮转后历史日期恒为空）。
        """
        date = str(request.args.get("date", "")).strip()
        if date and not _is_valid_date_str(date):
            return jsonify({"error": "日期格式不正确，应为 YYYY-MM-DD"}), 400
        # 默认：今天有日志则显示今天，否则找最近有日志的一天（_most_recent_log_date 内部先查今天）
        if not date:
            date = _most_recent_log_date()
        accounts = load_accounts()
        indices = _my_account_indices_of(accounts)  # 单快照：防两次读取间列表漂移（同 api_my_accounts）
        phones = [str(accounts[i].get("phone", "")) for i in indices]
        out = []
        for line in _log_lines_for(date):
            if any(f"[{p}]" in line for p in phones):
                out.append(line.strip())
        # 脱敏后再截断：与 /api/logs 同口径（日志行内 [手机号] 不落完整号）
        return jsonify({"ok": True, "date": date, "logs": [_mask_log_phones(ln) for ln in out[-50:]]})

    @app.route("/api/my-accounts/<int:idx>", methods=["PUT"])
    def api_my_account_update(idx):
        """编辑自己提交的账号：密码/识别码留空=保留；不影响已生效状态。"""
        with _file_lock:
            accounts = load_accounts()
            indices = _my_account_indices_of(accounts)
            if not 0 <= idx < len(indices):
                return jsonify({"error": "账号不存在"}), 404
            real_idx = indices[idx]
            old = accounts[real_idx]
            # 软删除账号禁止编辑（防编辑流程绕过软删除；恢复由管理员操作）
            if old.get("deleted"):
                return jsonify({"error": "账号已删除，请先恢复"}), 400
            data = _json_body()
            err, clean = validate_account(data, require_password=False)
            if err:
                return jsonify({"error": err}), 400
            if (
                clean["phone"] != old.get("phone")
                and find_account_index(accounts, clean["phone"]) is not None
            ):
                return jsonify({"error": f"手机号 {clean['phone']} 已被使用"}), 400
            if not clean["password"]:
                clean["password"] = old.get("password", "")
            # 设备识别码：__clear__ = 显式清空该字段；留空 = 保持不变
            if clean["phone_code"] == CLEAR_SENTINEL:
                clean.pop("phone_code", None)
            elif not clean["phone_code"]:
                clean["phone_code"] = old.get("phone_code", "")
            clean["owner"] = old.get("owner", "")
            # 被拒绝的账号编辑后 = 重新提交审核（回 pending，清除拒绝理由）
            clean["status"] = (
                ACCOUNT_STATUS_PENDING
                if old.get("status") == ACCOUNT_STATUS_REJECTED
                else old.get("status", ACCOUNT_STATUS_PENDING)
            )
            if clean["status"] == ACCOUNT_STATUS_PENDING:
                # 2026-08-16 审查轮修复：原 clean.pop("reject_reason") 对不存在的键是空操作，
                # 导致重新提交后旧拒绝理由残留（注释意图与实际不符）；显式置空随 update 落库
                clean["reject_reason"] = ""
            try:
                db.update_account(old["id"], clean)
            except sqlite3.IntegrityError:
                return jsonify({"error": f"手机号 {clean['phone']} 已被使用"}), 400  # 并发改号兜底
            # M11：手机号变更 → 旧号自选时间片失效，必须在 update_account 成功后再清
            if clean["phone"] != old.get("phone"):
                db.clear_time_pref(old.get("phone", ""))
            db.audit(
                clean["owner"],
                "my_account_update",
                _mask_phone(clean["phone"]),
                "用户编辑",
            )
            # 用户改密码/识别码后清除熔断暂停，立即恢复签到
            clear_fuse_pause(clean["phone"])
            logger.info("用户 %s 编辑账号 %s", _mask_email(clean["owner"]), _mask_phone(clean["phone"]))
            if old.get("status") == ACCOUNT_STATUS_REJECTED:
                return jsonify({"ok": True, "msg": "已重新提交，等待管理员审核"})
            return jsonify({"ok": True, "msg": "已保存"})

    @app.route("/api/my-accounts/<int:idx>", methods=["DELETE"])
    def api_my_account_delete(idx):
        """用户删除自己的账号：软删除进入 7 天宽限期，可在本页撤销恢复，超期自动清除。

        2026-08-28 用户裁决（批次7）：原实现为立即物理清除（无反悔），与
        「注销登录账号 7 天可恢复」的产品语义相反；现统一为软删 + 可撤销。
        deleted_by 留痕操作者（v10）：仅本人自删行可自行撤销，管理员删除的
        账号仍由管理员恢复/清除，防清退账号被用户一键复活。
        """
        with _file_lock:
            accounts = load_accounts()
            indices = _my_account_indices_of(accounts)
            if not 0 <= idx < len(indices):
                return jsonify({"error": "账号不存在"}), 404
            removed = accounts[indices[idx]]
            if removed.get("deleted"):
                return jsonify({"error": "该账号已在待删除状态，可在本页撤销恢复"}), 400
            db.set_account_deleted(
                removed["id"],
                1,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                deleted_by=(session.get("username", "") or "").strip().lower(),
            )
            db.audit(
                session.get("username", "") or "?",
                "my_account_delete",
                _mask_phone(removed.get("phone", "")),
                "用户删除（软删除，宽限期内可撤销）",
            )
            logger.info(
                "用户 %s 删除账号 %s（软删除）",
                session.get("username", ""),
                _mask_phone(removed.get("phone", "")),
            )
            # 批次11 N6：删号（软删）给本人留痕邮件（绕过 mail_notify 开关）
            mailer.send_user(
                session.get("username", ""),
                "【易班签到】您的易班账号已删除（7 天内可撤销）",
                f"您的易班账号（{_mask_phone(removed.get('phone', ''))}）已被删除，"
                "进入 7 天宽限期。\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                "宽限期内可在「我的账号」页自行撤销恢复；如非本人操作，"
                "请立即联系管理员。",
            )
            return jsonify({"ok": True, "msg": "已删除，7 天内可在本页撤销恢复，超期自动清除"})

    @app.route("/api/my-accounts/<int:idx>/restore", methods=["POST"])
    def api_my_account_restore(idx):
        """用户撤销删除自己的账号：仅限本人自删（deleted_by=本人）且名下无其他生效账号。

        与管理员 /api/accounts/<idx>/restore 同构，但收窄授权：
        - 普通用户视图（_my_account_indices_of）含本人待删除行，idx 口径与展示一致；
        - deleted_by != 本人（管理员删除/系统连带）一律 403，防清退账号被复活；
        - 「每人限 1」防呆与管理员侧同源（_owner_has_other_live）。
        """
        with _file_lock:
            accounts = load_accounts()
            indices = _my_account_indices_of(accounts)
            if not 0 <= idx < len(indices):
                return jsonify({"error": "账号不存在"}), 404
            acc = accounts[indices[idx]]
            if not acc.get("deleted"):
                return jsonify({"error": "该账号不在待删除状态"}), 400
            if acc.get("deleted_by", "") != (session.get("username", "") or "").strip().lower():
                return jsonify({"error": "该账号由管理员删除，如需恢复请联系管理员"}), 403
            if _owner_has_other_live(accounts, acc):
                return jsonify(
                    {"error": "你已有生效账号，无法恢复（每人限 1 个）"}
                ), 400
            if _stale_idx_guard(acc, _json_body()):
                return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
            db.set_account_deleted(acc["id"], 0)
            db.audit(
                session.get("username") or "?",
                "my_account_restore",
                _mask_phone(acc.get("phone", "")),
                "用户撤销删除",
            )
            accounts = load_accounts()
            logger.info(
                "用户 %s 撤销删除账号 %s",
                session.get("username", ""),
                _mask_phone(acc.get("phone", "")),
            )
            return jsonify(
                {
                    "ok": True,
                    "msg": f"已恢复「{acc.get('name', '')}」",
                    "accounts": _my_account_view(
                        accounts, _my_account_indices_of(accounts)
                    ),
                }
            )

    @app.route("/api/my-accounts/<int:idx>/pause", methods=["PUT"])
    def api_my_account_pause(idx):
        """用户自暂停/恢复签到（调度 v2）：暂停后主程序自动跳过，状态显示红底"已取消"。"""
        with _file_lock:
            accounts = load_accounts()
            indices = _my_account_indices_of(accounts)
            if not 0 <= idx < len(indices):
                return jsonify({"error": "账号不存在"}), 404
            acc = accounts[indices[idx]]
            if acc.get("deleted"):
                return jsonify({"error": "账号已删除，请先恢复"}), 400
            data = _json_body()
            paused = 1 if str(data.get("paused", "")).strip().lower() in ("1", "true", "on", "yes") else 0
            # 2026-08-15 用户确认：管理员不能暂停自己账号（owner=admin 为系统/管理员账号；
            # 暂停是普通用户管理自己账号的能力，管理端界面本无此入口，防 /user 页绕过）。
            # 恢复放行（幂等无害；该状态本不可达，仅保持接口一致性）
            if paused and acc.get("owner", "admin") == "admin":
                return jsonify({"error": "管理员账号不支持自暂停"}), 403
            # 暂停冷却（2026-08-15 用户裁决）：仅"暂停"计冷却（固定间隔，默认 30s），
            # "恢复"不受限——恢复是紧迫正向操作，绝不该被挡。冷却防连点/防审计噪音，
            # 非安全边界。按用户计价（多管理员共享账号各自独立，可接受）。时长可配
            # （YIBAN_PAUSE_COOLDOWN_SEC，默认 30；0=关闭）。不暴露时长（信息分层）。
            if paused:
                base_cd = load_env_int(ENV_FILE, "YIBAN_PAUSE_COOLDOWN_SEC", PAUSE_COOLDOWN_SEC)
                if base_cd > 0:
                    # 弹性冷却：60s 窗口内前 PAUSE_COOLDOWN_FREE 次完全自由，
                    # 超出后冷却 = 基础 × 2^(超限次数)，封顶 PAUSE_COOLDOWN_MAX。
                    now_ts = datetime.now()
                    since = (now_ts - timedelta(seconds=PAUSE_COOLDOWN_WINDOW)
                             ).strftime("%Y-%m-%d %H:%M:%S")
                    pause_count = db.pause_count_since(
                        session.get("username", "") or "", since
                    )
                    if pause_count >= PAUSE_COOLDOWN_FREE:
                        cooldown = min(
                            base_cd * (2 ** (pause_count - PAUSE_COOLDOWN_FREE + 1)),
                            PAUSE_COOLDOWN_MAX,
                        )
                        last_ts = db.last_pause_at(session.get("username", "") or "")
                        if last_ts:
                            try:
                                last_dt = datetime.strptime(str(last_ts), "%Y-%m-%d %H:%M:%S")
                                # 负间隔（时钟回拨）视为已过冷却，不误伤
                                if 0 <= (now_ts - last_dt).total_seconds() < cooldown:
                                    return jsonify({"error": "操作过于频繁，请稍后再试"}), 429
                            except ValueError:
                                # ts 格式异常（写坏）：保守按冷却生效拦截
                                return jsonify({"error": "操作过于频繁，请稍后再试"}), 429
            db.set_user_paused(acc["id"], paused)
            db.audit(
                session.get("username", "") or "?",
                "my_account_pause" if paused else "my_account_resume",
                _mask_phone(acc.get("phone", "")),
                "用户自暂停" if paused else "用户恢复",
            )
            logger.info(
                "用户 %s %s 账号 %s",
                session.get("username", ""), "暂停" if paused else "恢复",
                _mask_phone(acc.get("phone", "")),
            )
            return jsonify({
                "ok": True,
                "msg": "已暂停签到，主程序将自动跳过" if paused else "已恢复签到",
                "paused": bool(paused),
            })

    # ---- 用户管理（仅管理员；路径不在普通用户白名单，自动 403）----
    def _builtin_admin_email():
        """内置管理员（.env）标识（小写），用于防呆比较：不可改角色/删除。"""
        env = read_env(ENV_FILE)
        return env.get("YIBAN_ADMIN_USER", "").strip().lower()

    def _builtin_admin_display():
        """内置管理员显示名（保留 .env 原始大小写，仅用于界面展示）。"""
        env = read_env(ENV_FILE)
        return env.get("YIBAN_ADMIN_USER", "").strip() or "admin"

    def _is_builtin_admin_session():
        """当前会话是否确实是内置管理员（.env）登录，而非同邮箱注册用户/注册管理员。"""
        return (
            session.get("auth_source") == "builtin"
            and str(session.get("username") or "").strip().lower() == _builtin_admin_email()
        )

    def _effective_role(username, pw_version=None):
        """实时角色判定（每次请求读取，不依赖登录时固化的 session）：
        内置管理员 → admin；注册用户 → users 表的 role；查无此人 → None。
        管理员变更角色后，已登录用户的下一次请求立即生效，无需重新登录；
        被删除/取消权限的用户旧会话随之失效（None 视为未登录）；
        注册用户密码被重置/修改后（pw_version 递增）旧会话随之失效；
        内置管理员改密后（.env 版本递增）旧会话同样失效。
        """
        if not username:
            return None
        # S1/复审：所有会话都必须带 auth_source；旧会话（无该字段）一律视为未登录
        if not session.get("auth_source"):
            return None
        if (
            username.strip().lower() == _builtin_admin_email()
            and session.get("auth_source") == "builtin"
        ):
            # 内置管理员：必须是 builtin 登录来源且 session 版本与当前 .env 版本一致；
            # auth_source == "user" 的同名注册用户继续按普通用户判定，不借内置邮箱提权
            cur = load_env_int(ENV_FILE, "YIBAN_ADMIN_PW_VERSION", 1)
            return "admin" if pw_version == cur else None
        email = username.strip().lower()
        u = db.find_user(email)
        if u is not None:
            # 旧数据（无 pw_version 字段）不做会话吊销校验，兼容存量会话
            if "pw_version" in u and pw_version != u.get("pw_version", 1):
                return None
            # 批次7 P3-5 服务端会话吊销：users.sid 为该用户当前唯一有效会话标识，
            # 登录时签发、登出/被重置密码/被踢时轮换——被盗 cookie 重放即失效。
            # sid 为空串视为未签发（升级日存量会话兼容），签发后不匹配即失效。
            sid = u.get("sid", "")
            if sid and session.get("sid") != sid:
                return None
            return "admin" if u.get("role") == "admin" else "user"
        return None

    def _current_role():
        """当前登录会话的实时角色；未登录 → None。

        会话绝对过期（2026-08-27 审查修复 P2-5）：滑动续期（14 天）之外另设
        「自登录起最多 N 天」硬上限，防止被盗 Cookie 永久续命。时间戳在登录/
        恢复时写入 session["login_ts"]；存量旧会话无该字段则就地补记当下
        （升级日不强制全体重新登录）。超限即清空会话视为未登录。
        """
        if not session.get("auth"):
            return None
        ts = session.get("login_ts")
        now = time.time()
        if not isinstance(ts, (int, float)) or ts <= 0:
            session["login_ts"] = int(now)
        elif now - ts > SESSION_ABS_TTL_SECONDS:
            session.clear()
            return None
        return _effective_role(session.get("username"), session.get("pw_version"))

    @app.route("/api/users")
    def api_users():
        """用户列表（完整邮箱/角色/注册时间/账号数/待审核账号数）+ 内置管理员信息。"""
        users = load_users()
        accounts = load_accounts()
        # 性能优化：单次遍历 accounts 预计算每个 owner 的计数，避免 O(用户数×账号数)
        owner_account_count = {}
        owner_pending_count = {}
        for a in accounts:
            if a.get("deleted"):
                continue
            owner = a.get("owner", "")
            owner_account_count[owner] = owner_account_count.get(owner, 0) + 1
            if a.get("status") == ACCOUNT_STATUS_PENDING:
                owner_pending_count[owner] = owner_pending_count.get(owner, 0) + 1
        result = [
            {
                "email": u.get("email", ""),
                "role": u.get("role", "user"),
                "created_at": u.get("created_at", ""),
                # 计数排除软删除账号（删除后不占账号数/待审核数）
                "account_count": owner_account_count.get(u.get("email", ""), 0),
                "pending_count": owner_pending_count.get(u.get("email", ""), 0),
            }
            for u in users
        ]
        return jsonify(
            {
                "ok": True,
                "users": result,
                "builtin_admin": _builtin_admin_display(),
            }
        )

    @app.route("/api/users/deleted")
    def api_users_deleted():
        """已注销用户列表（仅管理员，v0.20.1）：软删除冷却中/待清除用户 + 剩余天数。

        用户裁决 2026-08-16：自助注销不发管理员通知，改为本视图主动查看；
        时间按天粒度（remaining_days 整天向下取整，0 = 不足一天），无秒级计算。
        require_login 已限定仅管理员（普通用户白名单外 → 403）。
        """
        items = []
        for u in db.load_users(include_deleted=True):
            if not u.get("deleted"):
                continue
            deleted_at = str(u.get("deleted_at") or "")
            remain_sec = _delete_grace_remaining(deleted_at)
            status = "cooling" if remain_sec > 0 else "purge_pending"
            items.append(
                {
                    "email": u["email"],
                    "deleted_at": deleted_at,
                    "remaining_days": int(remain_sec // 86400) if remain_sec > 0 else 0,
                    "status": status,
                }
            )
        items.sort(key=lambda x: x["deleted_at"])  # 最早到期在前（ISO 字符串字典序 = 时间序）
        return jsonify({"ok": True, "items": items})

    @app.route("/api/users/deleted/purge", methods=["POST"])
    def api_users_deleted_purge():
        """主管理员手动物理清除已注销用户（2026-08-17 需求：不留存已注销用户信息）。

        body: {"emails": [...]}——只清 deleted=1 的用户（db 层再校验，活跃用户传入即跳过）；
        冷却期内的用户也可被清除（管理员裁决权高于 7 天宽限承诺，审计留痕可追溯）。
        连带清理其全部易班账号行（含软删）与 time_prefs；单事务失败全部回滚。

        批次11 N3：收归主管理员专属——物理清除不可逆且剥夺用户 7 天反悔权，
        与角色变更/邮件配置等 master-only 口径对齐；普通管理员此前可绕过宽限
        承诺清除用户（批次11 实测 200），现 403。过期清理由系统每日清理自动完成，
        不受影响。同步即时告警（批次11 N6 缺口②）。
        """
        if not _is_builtin_admin_session():
            return jsonify({"error": "仅主管理员可操作"}), 403
        data = _json_body()
        emails = data.get("emails") or []
        if not isinstance(emails, list) or not emails:
            return jsonify({"error": "请选择要清除的用户"}), 400
        if len(emails) > BATCH_OP_LIMIT:
            return jsonify({"error": f"单次最多清除 {BATCH_OP_LIMIT} 个用户"}), 400
        if any(not isinstance(e, str) or len(e) > 64 for e in emails):
            return jsonify({"error": "邮箱格式不正确"}), 400
        with _file_lock:
            purged = db.purge_deleted_users_hard(emails)
            if purged:
                admin = session.get("username") or "admin"
                db.audit(
                    admin,
                    "user_deleted_purge",
                    ",".join(purged),
                    f"管理员手动清除 {len(purged)} 个已注销用户（含其易班账号与自选时间）",
                )
        skipped = [e for e in emails if e not in purged]
        logger.info("主管理员手动清除已注销用户: 成功 %d 个", len(purged))
        if purged:
            # 批次11 N6：物理清除不可逆，与批量删除用户同级即时告警
            send_notification(
                "高危管理操作告警",
                f"物理清除已注销用户 ×{len(purged)}: "
                f"{', '.join(_mask_email(e) for e in purged[:20])}，"
                f"操作者 {session.get('username', '?')}，时间 "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            )
        return jsonify({
            "ok": True,
            "purged": purged,
            "skipped": skipped,
            "msg": f"已彻底清除 {len(purged)} 个用户"
            + (f"，跳过 {len(skipped)} 个（非已注销状态）" if skipped else ""),
        })

    @app.route("/api/users/batch", methods=["POST"])
    def api_users_batch():
        """批量操作注册用户：set_admin/unset_admin/reset_password/delete。

        body: {"action": ..., "emails": [...], "password": "批量重置的新密码"}
        set_admin/unset_admin 仅主管理员（.env 内置管理员）可用；set_admin 仅限正式用户。
        Phase 1：整体事务，失败全部回滚；无效项软跳过。
        """
        data = _json_body()
        action = data.get("action")
        emails = data.get("emails") or []
        if action not in ("set_admin", "unset_admin", "reset_password", "delete"):
            return jsonify({"error": "未知操作"}), 400
        if not isinstance(emails, list) or not emails:
            return jsonify({"error": "请选择要操作的用户"}), 400
        if len(emails) > BATCH_OP_LIMIT:
            # 批次7 A2 + 2026-08-29 收紧：单次批量上限——被盗 admin 会话原本可用一个
            # 请求物理删除全部用户；与 accounts/batch 共用 BATCH_OP_LIMIT
            return jsonify({"error": f"单次批量操作最多 {BATCH_OP_LIMIT} 个用户"}), 400
        if any(not isinstance(e, str) or len(e) > 64 for e in emails):
            return jsonify({"error": "邮箱格式不正确"}), 400
        password = str(data.get("password", ""))
        reset_hash = None
        if action == "reset_password":
            pw_err = _password_policy_error(password)
            if pw_err:
                return jsonify({"error": f"新密码不符合要求：{pw_err}"}), 400
            # 在 _file_lock 外预计算 scrypt 哈希，避免长时间占用进程锁
            reset_hash = generate_password_hash(password, method=SCRYPT_METHOD)
        # 权限：权限变更与"操作管理员目标"仅主管理员
        # （普通管理员可重置密码/删除普通用户，不可改权限、不可重置/删除其他管理员）
        if action in ("set_admin", "unset_admin") and not _is_builtin_admin_session():
            return jsonify({"error": "仅主管理员可修改管理员权限"}), 403

        with _file_lock:
            users = load_users()
            builtin = _builtin_admin_email()
            accounts = load_accounts() if action == "set_admin" else None

            # 内存模拟用户表，保持动态管理员数量判断
            sim_users = {u["email"]: dict(u) for u in users}
            ops = []
            for email in emails:
                target = sim_users.get(email)
                if not target or email == builtin:  # 内置管理员不可批量操作
                    continue
                if (
                    action in ("reset_password", "delete")
                    and target.get("role") == "admin"
                    and not _is_builtin_admin_session()
                ):
                    # 安全审查 2026-08：普通管理员不可重置/删除其他管理员（与单条 403 同口径；
                    # 批量沿用"无效项软跳过"惯例，与内置管理员跳过一致）
                    continue
                if action == "set_admin":
                    # 只能将正式用户（有生效账号且无待审核）设为管理员；
                    # 正式用户判定仅 status==active 算（rejected 不算），且软删除不算
                    has_pending = any(
                        a.get("owner") == email
                        and a.get("status") == ACCOUNT_STATUS_PENDING
                        and not a.get("deleted")
                        for a in accounts
                    )
                    has_active = any(
                        a.get("owner") == email
                        and a.get("status") == ACCOUNT_STATUS_ACTIVE
                        and not a.get("deleted")
                        for a in accounts
                    )
                    if not has_active or has_pending:
                        continue
                    ops.append(("update_user", email, {"role": "admin"}))
                    sim_users[email]["role"] = "admin"
                elif action == "unset_admin":
                    if target.get("role") != "admin":
                        continue
                    # 每次循环内动态重算 admins：前一个被取消后，后续目标以最新模拟列表判定
                    admins = [u for u in sim_users.values() if u.get("role") == "admin"]
                    # 防呆：内置管理员不存在且这是最后一个注册管理员时跳过
                    if len(admins) <= 1 and not builtin:
                        continue
                    ops.append(("update_user", email, {"role": "user"}, bool(builtin)))
                    sim_users[email]["role"] = "user"
                elif action == "reset_password":
                    ops.append(
                        (
                            "update_user",
                            email,
                            {
                                "password_hash": reset_hash,
                                "pw_version": target.get("pw_version", 1) + 1,
                            },
                        )
                    )
                    sim_users[email]["pw_version"] = target.get("pw_version", 1) + 1
                elif action == "delete":
                    # 防呆：目标为管理员时校验至少保留 1 个管理员
                    # （内置管理员存在时允许删除最后一个注册管理员，与单条路径一致）
                    if target.get("role") == "admin":
                        admins = [u for u in sim_users.values() if u.get("role") == "admin"]
                        if len(admins) <= 1 and not builtin:
                            continue
                    ops.append(("delete_user_with_accounts", email, bool(builtin)))
                    sim_users.pop(email, None)
            done = len(ops)
            if ops:
                try:
                    db.batch_user_ops(ops)
                except db.LastAdminError:
                    # 批次12 B12-7：db 事务内复核兜底（跨进程竞态时整体回滚转 400）
                    db.audit(
                        session.get("username") or "?",
                        "users_batch",
                        action,
                        "被拒：批量操作命中最后一个注册管理员保护，已整体回滚",
                    )
                    return jsonify({"error": "批量操作包含最后一个注册管理员的删除/降权，已整体回滚"}), 400
                except Exception as e:
                    logger.error("批量%s用户失败: %s（已回滚）", action, e)
                    db.audit(
                        session.get("username") or "?",
                        "users_batch",
                        action,
                        "失败，已回滚",
                    )
                    return jsonify({"error": "批量操作失败，已全部回滚"}), 500
                if action == "delete":
                    # 批次7 B4：批量物理删除用户为不可逆高危操作，即时告警
                    send_notification(
                        "高危管理操作告警",
                        f"批量删除用户 ×{done}: "
                        f"{', '.join(_mask_email(e) for e in (emails or [])[:20])}，"
                        f"操作者 {session.get('username', '?')}，时间 "
                        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    )
                # 批次7 P3-5：批量重置密码后轮换各目标 sid（吊销被盗旧会话）
                if action == "reset_password":
                    for e in emails:
                        with contextlib.suppress(Exception):
                            db.set_user_sid(e.strip().lower(), secrets.token_hex(16))
                    # 批次11 N6：批量重置密码即时告警
                    send_notification(
                        "密码重置告警",
                        f"批量重置密码 ×{done}: "
                        f"{', '.join(_mask_email(e) for e in (emails or [])[:20])}，"
                        f"操作者 {session.get('username', '?')}，时间 "
                        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    )
                if action in ("set_admin", "unset_admin"):
                    # 批次11 N6：提降权即时告警（权限面变更应可感知）
                    send_notification(
                        "权限变更告警",
                        f"批量{'提权' if action == 'set_admin' else '降权'} ×{done}: "
                        f"{', '.join(_mask_email(e) for e in (emails or [])[:20])}，"
                        f"操作者 {session.get('username', '?')}，时间 "
                        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    )
            db.audit(
                session.get("username") or "?",
                "users_batch",
                action,
                # 批次7 B3：批量操作留目标清单（脱敏截断），破坏事后可从审计还原"动了谁"
                (f"处理 {done} 个: " + ",".join(
                    _mask_email(e) for e in (emails or [])[:20]
                ))[:200],
            )
            logger.info("批量%s用户 %d 个", action, done)
            msg = {
                "set_admin": f"已设为管理员 {done} 个用户",
                "unset_admin": f"已取消管理员 {done} 个用户",
                "reset_password": f"已重置密码 {done} 个用户",
                "delete": f"已删除 {done} 个用户",
            }[action]
            return jsonify({"ok": True, "msg": msg})

    @app.route("/api/users/<email>/role", methods=["POST"])
    def api_user_role(email):
        """设为管理员 / 取消管理员。仅主管理员（.env 内置管理员）可操作；
        只能将「正式用户」（有生效账号且无待审核）设为管理员；
        防呆：内置管理员不可改；至少保留 1 个管理员。"""
        # 权限：仅主管理员（普通管理员无管理员权限变更权）
        username = (session.get("username") or "").strip().lower()
        if not _is_builtin_admin_session():
            return jsonify({"error": "仅主管理员可修改管理员权限"}), 403
        data = _json_body()
        new_role = data.get("role")
        if new_role not in ("admin", "user"):
            return jsonify({"error": "未知角色"}), 400
        # 内置管理员（.env）不可修改角色
        if email.strip().lower() == _builtin_admin_email().strip().lower():
            return jsonify({"error": "内置管理员不可修改角色"}), 400
        with _file_lock:
            target = db.find_user(email)
            if not target:
                return jsonify({"error": "用户不存在"}), 404
            if new_role == "admin":
                # 只能将正式用户（有生效账号且无待审核）设为管理员；
                # 正式用户判定仅 status==active 算（rejected 不算），且软删除不算
                accounts = load_accounts()
                has_pending = any(
                    a.get("owner") == email
                    and a.get("status") == ACCOUNT_STATUS_PENDING
                    and not a.get("deleted")
                    for a in accounts
                )
                has_active = any(
                    a.get("owner") == email
                    and a.get("status") == ACCOUNT_STATUS_ACTIVE
                    and not a.get("deleted")
                    for a in accounts
                )
                if not has_active or has_pending:
                    return jsonify({"error": "仅正式用户可设为管理员（需有已生效账号且无待审核）"}), 400
            if new_role == "user" and target.get("role") == "admin":
                admins = [u for u in load_users() if u.get("role") == "admin"]
                # 内置管理员（.env）也是管理员且不可被移除——存在时允许取消 users 表中的最后一个管理员
                if len(admins) <= 1 and not _builtin_admin_email():
                    return jsonify({"error": "至少保留 1 个管理员"}), 400
            # 批次12 B12-7：改走事务内复核的 set_user_role——进程内预检挡不住
            # 跨进程并发（web+TUI/多实例）同时把最后一个注册管理员降权
            try:
                changed = db.set_user_role(
                    email, new_role, allow_last_admin=bool(_builtin_admin_email())
                )
            except db.LastAdminError:
                return jsonify({"error": "至少保留 1 个管理员"}), 400
            if changed == 0:
                # 批次7 P4-4(C-M1 收尾)：0 行 = 目标已被并发删除，不得谎报成功
                return jsonify({"error": "用户不存在"}), 404
            db.audit(
                username,
                "user_role",
                _mask_email(email),
                f"角色 → {new_role}",
            )
            logger.info("主管理员 %s 将用户 %s 角色 → %s", _mask_email(username), _mask_email(email), new_role)
            # 批次11 N6：提降权即时告警（权限面变更应可感知）
            send_notification(
                "权限变更告警",
                f"用户 {_mask_email(email)} 角色 → {new_role}，"
                f"操作者 {username}，时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            )
            return jsonify(
                {
                    "ok": True,
                    "msg": f"{email} 已{'设为管理员' if new_role == 'admin' else '取消管理员'}",
                }
            )

    @app.route("/api/users/<email>/password", methods=["POST"])
    def api_user_password(email):
        """重置用户密码（管理员无法查看原密码，只能设置新密码）。
        安全审查 2026-08：目标为注册管理员时仅主管理员可操作（防普通管理员横向接管）。"""
        data = _json_body()
        password = str(data.get("password", ""))
        pw_err = _password_policy_error(password)
        if pw_err:
            return jsonify({"error": f"新密码不符合要求：{pw_err}"}), 400
        is_master = _is_builtin_admin_session()
        with _file_lock:
            target = db.find_user(email)
            if not target:
                return jsonify({"error": "用户不存在"}), 404
            if target.get("role") == "admin" and not is_master:
                return jsonify({"error": "仅主管理员可重置管理员密码"}), 403
            if db.update_user(
                email,
                {
                    "password_hash": generate_password_hash(password, method=SCRYPT_METHOD),
                    "pw_version": target.get("pw_version", 1) + 1,  # 被重置用户的旧会话随之失效
                },
            ) == 0:
                # 批次7 P4-4(C-M1 收尾)：0 行 = 目标已被并发删除
                return jsonify({"error": "用户不存在"}), 404
            # 批次7 P3-5：轮换目标 sid，被盗 cookie 即便未因 pw_version 失效（如
            # 旧版本客户端）也双重确保吊销
            db.set_user_sid(email.strip().lower(), secrets.token_hex(16))
            db.audit(
                session.get("username") or "?",
                "user_password_reset",
                _mask_email(email),
                "管理员重置密码",
            )
            logger.info("已重置用户 %s 密码", _mask_email(email))
            # 批次11 N6：重置他人密码即时告警（被盗号会话中的静默接管信号）
            send_notification(
                "密码重置告警",
                f"用户 {_mask_email(email)} 的密码已被管理员重置，"
                f"操作者 {session.get('username', '?')}，"
                f"时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            )
            return jsonify({"ok": True, "msg": f"{email} 密码已重置"})

    @app.route("/api/users/<email>/delete", methods=["POST"])
    def api_user_delete(email):
        """删除用户：mode=accounts_only 仅清空其易班账号（保留用户可重新提交）；
        mode=full 完全删除用户及其账号。
        安全审查 2026-08：目标为注册管理员时仅主管理员可操作（与 role/密码重置口径一致）。"""
        data = _json_body()
        mode = data.get("mode", "full")
        if mode not in ("accounts_only", "full"):
            return jsonify({"error": "未知操作"}), 400
        if email.strip().lower() == _builtin_admin_email().strip().lower():
            return jsonify({"error": "内置管理员不可删除"}), 400
        is_master = _is_builtin_admin_session()
        with _file_lock:
            target = db.find_user(email)
            if not target:
                return jsonify({"error": "用户不存在"}), 404
            if target.get("role") == "admin" and not is_master:
                return jsonify({"error": "仅主管理员可删除管理员"}), 403
            if mode == "full" and target.get("role") == "admin":
                admins = [u for u in load_users() if u.get("role") == "admin"]
                # 内置管理员（.env）兜底存在时可删除 users 表中的最后一个管理员
                if len(admins) <= 1 and not _builtin_admin_email():
                    return jsonify({"error": "至少保留 1 个管理员"}), 400
            # 删除其提交的易班账号（full 模式用单事务组合函数，防崩溃窗口不一致）
            if mode == "full":
                # 批次12 B12-7：事务内复核最后一个注册管理员（allow 与原预检同语义：
                # 内置管理员存在时允许删掉 users 表最后一个注册管理员）
                try:
                    db.delete_user_with_accounts(
                        email, allow_last_admin=bool(_builtin_admin_email())
                    )
                except db.LastAdminError:
                    return jsonify({"error": "至少保留 1 个管理员"}), 400
            else:
                db.delete_accounts_by_owner(email)
            db.audit(
                session.get("username") or "?",
                "user_delete",
                _mask_email(email),
                f"mode={mode}",
            )
            if mode == "full":
                logger.info("完全删除用户 %s（含易班账号）", _mask_email(email))
                # 批次7 B4：完全删除（物理、不可逆）为高危操作，即时告警
                send_notification(
                    "高危管理操作告警",
                    f"完全删除用户 {_mask_email(email)} 及其全部易班账号，"
                    f"操作者 {session.get('username', '?')}，时间 "
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                )
                return jsonify({"ok": True, "msg": f"{email} 已完全删除"})
            logger.info("清空用户 %s 的易班账号（保留用户）", _mask_email(email))
            return jsonify({"ok": True, "msg": f"{email} 的易班账号已清空（用户保留，可重新提交）"})

    # ---- 手动签到 ----
    _last_trigger = {}  # phone -> 上次触发时间戳
    _signin_procs = {}  # phone -> Popen（新触发时终止仍在运行的旧进程，防重复签到触发风控）
    _signin_lock = threading.Lock()  # 防抖检查+赋值原子化（TOCTOU 竞态防护）
    _batch_signin_running = False  # 批量签到队列互斥：同时只允许一个在跑
    _batch_signin_lock = threading.Lock()

    # ---- 手动签到（单个 / 批量）----
    def _signin_run_lock_busy():
        """非阻塞探测 signin 运行锁是否被其他进程持有（批次7 P2-9）。

        全量签到/cron 运行期间，--only 子进程会拿锁失败并 exit 3 静默退出——
        原实现 web 照样返回"已触发"，用户侧无感。现在 spawn 前先探测，忙时直接
        429 如实提示（POSIX flock 试探；Windows 无 fcntl 返回 False 走旧行为，
        与 _acquire_run_lock 的降级策略一致）。
        """
        path = os.path.join(STATE_DIR, "signin-run.lock")
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return False
        try:
            try:
                import fcntl
            except ImportError:
                return False
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True  # 被其他签到进程持有
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)

    def _reap_signin(phone, proc):
        """M9：子进程结束后在锁内从 _signin_procs 移除同对象（防僵尸记录/重复 terminate）。"""
        try:
            proc.wait()
        finally:
            with _signin_lock:
                if _signin_procs.get(phone) is proc:
                    _signin_procs.pop(phone, None)

    def _spawn_signin(phone, accounts=None):
        """触发单账号手动签到子进程（signin.py --only）。

        防抖：60 秒内同账号不重复触发（SIGN_MIN_INTERVAL）；仍在运行的旧进程先终止。
        返回 (ok: bool, msg: str)。
        """
        accounts = accounts if accounts is not None else load_accounts()
        idx = find_account_index(accounts, phone)
        if idx is None:
            return False, f"账号 {phone} 不在配置中"
        acc = accounts[idx]
        if acc.get("deleted") or acc.get("status") != ACCOUNT_STATUS_ACTIVE:
            return False, f"账号 {phone} 不可手动签到（未生效或已删除）"
        if _signin_run_lock_busy():
            return False, "签到队列忙（定时签到进行中），请稍后再试"
        with _signin_lock:  # 原子检查+占位：并发请求不能同时通过防抖
            now = time.time()
            if phone in _last_trigger and now - _last_trigger[phone] < SIGN_MIN_INTERVAL:
                remain = int(SIGN_MIN_INTERVAL - (now - _last_trigger[phone]))
                return False, f"账号 {phone} 正在签到，请 {remain} 秒后再试"
            old = _signin_procs.get(phone)
            if old and old.poll() is None:
                old.terminate()  # 仍在运行 → 终止旧进程，防止同账号并发签到
            _last_trigger[phone] = now

        # 项目根目录（web 的上一级），与 TUI action_manual_sign 一致
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = os.path.join(base, "scripts", "signin.py")
        # 批次7 P2-10：与定时签到同口径——进程环境为底座、.env 的 YIBAN_* 键覆盖注入。
        # 原实现只继承 gunicorn 启动快照，管理员事后在 .env 配的 YIBAN_PROXY /
        # YIBAN_NOTIFY_URL / 登录方式对手动签到不生效（两路行为分叉，排障困难）。
        env = child_env.build_child_env(ENV_FILE, base=dict(os.environ))
        # 单账号手动签到：关闭随机延迟，避免等待
        env["YIBAN_START_DELAY_MAX"] = "0"
        env["YIBAN_ACCOUNT_GAP_MAX"] = "0"
        # 子进程读取与主进程相同的数据库（--db 自定义路径时保持一致）
        env["YIBAN_DB_FILE"] = DB_FILE
        # 2026-08-21 对抗性审查修复：传 .env 路径而非密钥本身——此前把
        # YIBAN_ACCOUNTS_KEY 明文注入子进程环境（同 uid 进程可读 /proc/<pid>/environ）；
        # signin.py 现按 YIBAN_ENV_FILE 自行从 .env 读取密钥
        env["YIBAN_ENV_FILE"] = ENV_FILE
        log_fh = None
        with contextlib.suppress(OSError):
            log_fh = open(
                log_path_for(), "a", encoding="utf-8", buffering=1
            )  # 日志不可写时丢弃，不影响签到执行（按天文件：sign-当天.log）
        try:
            proc = subprocess.Popen(
                [sys.executable, script, "--only", phone],
                cwd=base,
                env=env,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )
            with _signin_lock:
                _signin_procs[phone] = proc  # 记录子进程，供下次触发时终止旧进程
            # M9：daemon 回收线程，进程退出后自动从 _signin_procs 移除同对象
            threading.Thread(target=_reap_signin, args=(phone, proc), daemon=True).start()
        except FileNotFoundError:
            with _signin_lock:
                _last_trigger.pop(phone, None)
            return False, f"账号 {phone} 手动签到启动失败，请稍后重试"
        finally:
            if log_fh is not None:
                log_fh.close()  # 父进程关闭句柄（子进程已继承自身副本），防句柄累积
        logger.info("触发手动签到: %s", _mask_phone(phone))
        return True, f"已触发 {phone} 手动签到（后台执行，日志约 30 秒内刷新）"

    @app.route("/api/signin", methods=["POST"])
    def api_signin():
        """手动签到指定账号：子进程执行 signin.py --only（与 TUI M 键一致）。"""
        data = _json_body()
        phone = str(data.get("phone", "")).strip()
        ok, msg = _spawn_signin(phone)
        if not ok:
            if "不在配置中" in msg:
                return jsonify({"error": msg}), 404
            if "不可手动签到" in msg:
                return jsonify({"error": msg}), 400
            if "正在签到" in msg or "签到队列忙" in msg:
                return jsonify({"error": msg}), 429
            return jsonify({"error": msg}), 500
        db.audit(session.get("username") or "?", "signin_manual", _mask_phone(phone), "手动签到")
        return jsonify({"ok": True, "msg": msg})

    @app.route("/api/signin/batch", methods=["POST"])
    def api_signin_batch():
        """批量手动签到：顺序逐个触发（与自动签到同语义，防风控）。

        全局互斥（同时只允许一个批量队列在跑）；防抖冲突的账号自动跳过。
        接口立即返回，实际执行在后台线程。
        """
        data = _json_body()
        ids = data.get("ids")
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "请先勾选要签到的账号"}), 400
        accounts = load_accounts()
        if _signin_run_lock_busy():
            return jsonify({"error": "签到队列忙（定时签到进行中），请稍后再试"}), 429
        # 防错位 + bool 混淆修复（同 /api/accounts/batch，2026-08-20 对抗性审查 P1）：
        # 签到用错位下标取到的会是他人账号的凭据，危害比管理操作更直接
        phones_in = data.get("phones")
        expect = {}
        if isinstance(phones_in, list) and len(phones_in) == len(ids):
            expect = {
                i: str(phones_in[k]).strip() for k, i in enumerate(ids) if type(i) is int
            }
        phones = []
        for i in ids:
            if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(accounts):
                continue
            acc = accounts[i]
            # 双侧 _mask_phone 归一（出站为脱敏号，见 _stale_idx_guard 注释）
            if i in expect and _mask_phone(expect[i]) != _mask_phone(str(acc.get("phone", ""))):
                return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
            if acc.get("deleted") or acc.get("status") != ACCOUNT_STATUS_ACTIVE:
                continue
            phone = str(acc.get("phone", "")).strip()
            if phone:
                phones.append(phone)
        if not phones:
            return jsonify({"error": "选中的账号均不可手动签到（未生效或已删除）"}), 400
        nonlocal _batch_signin_running
        with _batch_signin_lock:
            if _batch_signin_running:
                return jsonify({"error": "已有批量签到正在执行，请稍后再试"}), 429
            _batch_signin_running = True

        def _run_batch():
            try:
                ok_n = skip_n = 0
                for phone in phones:
                    ok, _ = _spawn_signin(phone, accounts=accounts)
                    if ok:
                        ok_n += 1
                        proc = _signin_procs.get(phone)
                        if proc:
                            # M8：超时后 terminate + 回收，避免遗留子进程造成并发签到
                            _wait_signin_proc(proc)
                    else:
                        skip_n += 1
                logger.info("批量手动签到完成: 触发 %s 个，跳过 %s 个", ok_n, skip_n)
            finally:
                nonlocal _batch_signin_running
                with _batch_signin_lock:
                    _batch_signin_running = False

        threading.Thread(target=_run_batch, daemon=True).start()
        db.audit(
            session.get("username") or "?",
            "signin_batch",
            ",".join(_mask_phone(p) for p in phones),
            f"批量签到 {len(phones)} 个",
        )
        return jsonify({
            "ok": True,
            "msg": f"已加入批量签到队列（{len(phones)} 个账号，将按顺序逐个执行，日志约几分钟内刷新）",
        })

    # ---- 日志与状态 ----
    @app.route("/api/logs")
    def api_logs():
        """签到日志与今日状态。

        ?date=YYYY-MM-DD（可选，仅管理员）：缺省=最近有日志的一天（优先今天）；
        指定日期时 logs 为该日日志、states 仍为今日状态（账号表格图标语义不随历史日期变化）。
        """
        date = str(request.args.get("date", "")).strip()
        if date and not _is_valid_date_str(date):
            return jsonify({"error": "日期格式不正确，应为 YYYY-MM-DD"}), 400
        # 默认：今天有日志则显示今天，否则找最近有日志的一天（_most_recent_log_date 内部先查今天）
        if not date:
            date = _most_recent_log_date()
        logs = _log_lines_for(date)
        # 探针结构化事件（v0.24.4）：此前 stage="probe" 只落库无任何可见出口，
        # 现随日志接口附带当日探测记录（独立字段，不混入签到文本流；
        # 手机号打码，条数封顶）。
        probe_events = []
        try:
            for ev in db.probe_events_on(date, limit=100):
                msg = str(ev.get("message") or "")
                probe_events.append({
                    "time": str(ev.get("ts", ""))[-8:] if ev.get("ts") else "",
                    "phone": signin._mask_phone(str(ev.get("phone") or "")),
                    "status": str(ev.get("status") or ""),
                    "message": (msg[:160] + "…") if len(msg) > 160 else msg,
                })
        except Exception as e:
            logger.warning("probe_events 查询失败（不影响日志页）: %s", e)
            probe_events = []
        # 批次12 裁决：当日签到事件（stage="sign"）——sign_events 表此前主流程
        # 零写入、读取函数零调用方（基础设施空转）；写入已在 signin 侧补齐，
        # 此处与探针记录同口径脱敏展示（手机号打码、条数封顶）。
        sign_events = []
        try:
            for ev in db.sign_events_on(date, limit=100):
                msg = str(ev.get("message") or "")
                sign_events.append({
                    "time": str(ev.get("ts", ""))[-8:] if ev.get("ts") else "",
                    "phone": signin._mask_phone(str(ev.get("phone") or "")),
                    "status": str(ev.get("status") or ""),
                    "attempt": int(ev.get("attempt") or 0),
                    "message": (msg[:160] + "…") if len(msg) > 160 else msg,
                })
        except Exception as e:
            logger.warning("sign_events 查询失败（不影响日志页）: %s", e)
            sign_events = []
        # 响应层脱敏：日志行内 [手机号] 不落完整号（前端 maskPhone 幂等兼容）。
        # 注意：不返回 states——账号表格图标的事实源是 /api/accounts（sign-state 文件），
        # 日志符号（✅/❌）与状态码（success/failed）语义不同，曾造成前端图标/统计卡被
        # 符号污染（2026-08-16 审查轮修复）。
        return jsonify(
            {
                "ok": True,
                "logs": [_mask_log_phones(ln) for ln in logs[-80:]],
                "log_file": f"sign-{date}.log",  # 只暴露文件名，不暴露服务器路径
                "date": date,
                "is_today": date == datetime.now().strftime("%Y-%m-%d"),
                "probe_events": probe_events,
                "sign_events": sign_events,
            }
        )

    # ---- 签到事件查询（管理员；批次12 裁决：sign_events 补消费端）----
    @app.route("/api/admin/sign-events")
    def api_admin_sign_events():
        """sign_events 结构化查询：单账号时间线 / 实时事件流 / 按天统计。

        权限：路径不在普通用户白名单，require_login 统一 403（普通用户越权访问
        会另记 forbidden_path 审计）。手机号一律打码后返回，条数封顶 200。
        """
        phone = str(request.args.get("phone", "")).strip()
        try:
            days = min(max(int(request.args.get("days", 7)), 1), 90)
        except (TypeError, ValueError):
            days = 7
        try:
            limit = min(max(int(request.args.get("limit", 100)), 1), 200)
        except (TypeError, ValueError):
            limit = 100

        def _mask(ev):
            msg = str(ev.get("message") or "")
            return {
                "ts": str(ev.get("ts", "")),
                "phone": signin._mask_phone(str(ev.get("phone") or "")),
                "status": str(ev.get("status") or ""),
                "message": (msg[:160] + "…") if len(msg) > 160 else msg,
                "stage": str(ev.get("stage") or ""),
                "attempt": int(ev.get("attempt") or 0),
                "dur_sec": ev.get("dur_sec"),
            }

        events = []
        if phone:
            events = [_mask(ev) for ev in db.sign_events_by_phone(phone, days=days)][:limit]
        else:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            events = [_mask(ev) for ev in db.sign_events_since(cutoff, limit=limit)]
        stats = db.sign_event_stats(days=days)
        return jsonify(
            {
                "ok": True,
                "days": days,
                "count": len(events),
                "events": events,
                "daily_stats": stats,
            }
        )

    # ---- 设置（随机延迟，写入 .env）----
    @app.route("/api/settings")
    def api_settings():
        env = read_env(ENV_FILE)
        mode = env.get("YIBAN_SIGN_MODE", "").strip().lower()
        sw = _sign_window()
        # 容量口径（2026-08-23 修正）：
        #   账号 = 全部非删除账号（含 admin 直属账号，排除软删/注销宽限账号）
        #   用户 = 至少持有 1 个非删除账号的活跃注册用户
        _live_accts = [a for a in load_accounts() if not a["deleted"]]
        _owners_with_account = {a.get("owner") for a in _live_accts if a.get("owner")}
        return jsonify(
            {
                "ok": True,
                "start_delay_max": load_env_int(ENV_FILE, "YIBAN_START_DELAY_MAX", 0),
                "gap_max": load_env_int(ENV_FILE, "YIBAN_ACCOUNT_GAP_MAX", 0),
                "default_start_delay_max": DEFAULT_START_DELAY_MAX,
                "default_gap_max": DEFAULT_ACCOUNT_GAP_MAX,
                # 签到模式：sequence（列表顺序，默认）/ random（列表随机打散）
                "sign_mode": mode or "sequence",
                # 调度 v2：排序×分布二级开关 + 首尾缓冲 + 自选总开关 + 窗口
                "sign_order": env.get("YIBAN_SIGN_ORDER", "").strip().lower() or (
                    "random" if mode == "random" else "sequence"),
                "sign_dist": env.get("YIBAN_SIGN_DIST", "").strip().lower() or (
                    "normal" if mode == "normal" else "uniform"),
                # 掐头去尾（0.22.0 前后独立，秒；window_edge_sec 兼容旧前端 = 前裁）
                "window_edge_sec": edge_config()[0],
                "edge_front_sec": edge_config()[0],
                "edge_back_sec": edge_config()[1],
                "allow_time_pref": load_env_int(ENV_FILE, "YIBAN_ALLOW_TIME_PREF", 0),
                "sign_window": f"{sw[0][0]:02d}:{sw[0][1]:02d} ~ {sw[1][0]:02d}:{sw[1][1]:02d}",
                # 容量状态：注册/账号上限与当前使用量（管理员知情）
                "capacity": {
                    "users": sum(1 for u in db.load_users() if u["email"] in _owners_with_account),
                    "users_max": load_env_int(ENV_FILE, "YIBAN_MAX_USERS", DEFAULT_MAX_USERS),
                    "accounts": len(_live_accts),
                    "accounts_max": load_env_int(ENV_FILE, "YIBAN_MAX_ACCOUNTS", DEFAULT_MAX_ACCOUNTS),
                },
                # 周日签到：1=开启（周日也尝试签到），0=关闭（默认）
                "sunday_sign": load_env_int(ENV_FILE, "YIBAN_SUNDAY_SIGN", 0),
                # 周六签到：1=开启（默认，周六照常签到），0=关闭（周六暂停）
                "saturday_sign": load_env_int(ENV_FILE, "YIBAN_SATURDAY_SIGN", 1),
                # 全局暂停（一键暂停签到）：1=暂停（下一轮 cron 跳过），0=正常
                "global_pause": load_env_int(ENV_FILE, "YIBAN_GLOBAL_PAUSE", 0),
                # 批量多选：前端会话级开关（不持久化，每次进入页面默认关闭）
                "batch_mode": False,
                # 注册账号验证 + 探针模式（v0.23.x，任意管理员可改）
                "account_verify": 1 if env.get("YIBAN_ACCOUNT_VERIFY", "").strip().lower() in ("1", "true", "on", "yes") else 0,
                "probe_enable": 1 if env.get("YIBAN_PROBE_ENABLE", "").strip().lower() in ("1", "true", "on", "yes") else 0,
                "probe_time": env.get("YIBAN_PROBE_TIME", "20:00").strip() or "20:00",
                "probe_interval": env.get("YIBAN_PROBE_INTERVAL_DAYS", "1").strip() or "1",
            }
        )

    @app.route("/api/settings", methods=["POST"])
    def api_settings_save():
        data = _json_body()
        # 调度权限（2026-08-15 确认）：仅主管理员可改调度字段（排序/分布/缓冲/自选/窗口/旧版模式）；
        # 批次7 A5：随机延迟（start_delay_max/gap_max）同为调度核心参数——注册管理员
        # 拉满 3600s 可把几乎全部账号挤出签到窗口（事实性停签），一并收归主管理员。
        # 普通管理员可改周日/公告等低风险项。
        # sign_mode 为遗留字段（已无 UI 控件），但 signin.py 在未设 sign_order 时以其为回退，
        # 普通管理员改之可间接变更调度排序 → 同样仅主管理员可写（安全审查 2026-08）。
        is_master = _is_builtin_admin_session()
        if not is_master and any(
            k in data for k in ("sign_order", "sign_dist", "window_edge_sec",
                                "edge_front_sec", "edge_back_sec",
                                "allow_time_pref", "sign_window", "sign_mode",
                                "global_pause", "start_delay_max", "gap_max")
        ):
            return jsonify({"error": "仅主管理员可修改调度设置"}), 403
        # 批次7 A4：字段携带才写——原实现缺省即 0 且无条件写两个键，
        # "只改周日开关"之类的部分更新会把已配置的延迟静默清零
        has_start = "start_delay_max" in data
        has_gap = "gap_max" in data
        try:
            start = int(data.get("start_delay_max", 0))
            gap = int(data.get("gap_max", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "延迟秒数必须是整数"}), 400
        # 上限 1 小时：防止误填超大值破坏签到随机延迟
        start = min(max(start, 0), 3600)
        gap = min(max(gap, 0), 3600)
        # 安全审查 2026-08：先全量校验、再统一写入——此前边校验边写，
        # 后续字段非法返回 400 时前面的字段已落盘（"报错但设置变了"的部分写入）。
        # 签到模式（sequence/random）：写入 .env，cron 的 run.sh 加载后 signin.py 生效
        sign_mode = str(data.get("sign_mode", "")).strip().lower()
        if sign_mode and sign_mode not in ("sequence", "random"):
            return jsonify({"error": "签到模式取值应为 sequence 或 random"}), 400
        # 调度 v2：排序×分布二级开关（替代旧三选一，旧值自动映射兼容）
        sign_order = str(data.get("sign_order", "")).strip().lower()
        sign_dist = str(data.get("sign_dist", "")).strip().lower()
        if sign_order and sign_order not in ("sequence", "random"):
            return jsonify({"error": "排序方式取值应为 sequence 或 random"}), 400
        if sign_dist and sign_dist not in ("uniform", "normal"):
            return jsonify({"error": "分布方式取值应为 uniform 或 normal"}), 400
        # 掐头去尾（0.22.0 前后独立）：window_edge_sec 兼容旧前端（对称写）；
        # edge_front_sec / edge_back_sec 各自独立（秒，0~300，30 的倍数 = 0.5 分钟粒度）。
        # 0 是合法值（不裁切），不能用 write_env_int（其语义为 <=0 删除行）。
        edge_front = edge_back = None
        edge_raw = data.get("window_edge_sec")
        if edge_raw is not None:
            try:
                edge = int(edge_raw)
            except (TypeError, ValueError):
                return jsonify({"error": "首尾缓冲必须是整数秒"}), 400
            if not (0 <= edge <= 300 and edge % 30 == 0):
                return jsonify({"error": "首尾缓冲应为 0~300 秒且为 30 的倍数（0.5 分钟粒度）"}), 400
            edge_front = edge_back = edge
        for _key, _field in (("edge_front_sec", "front"), ("edge_back_sec", "back")):
            if _key in data:
                try:
                    v = int(data[_key])
                except (TypeError, ValueError):
                    return jsonify({"error": f"{_field}裁剪秒数必须是整数"}), 400
                if not (0 <= v <= 300 and v % 30 == 0):
                    return jsonify({"error": f"{_field}裁剪应为 0~300 秒且为 30 的倍数（0.5 分钟粒度）"}), 400
                if _key == "edge_front_sec":
                    edge_front = v
                else:
                    edge_back = v
        # 用户自选总开关（0/1；0 同样需显式写入）
        pref_raw = data.get("allow_time_pref")
        pref = None
        if pref_raw is not None:
            pref = 1 if str(pref_raw).strip().lower() in ("1", "true", "on", "yes") else 0
        # 签到窗口（HH:MM，管理员可调；校验非法拒绝）
        win = str(data.get("sign_window", "")).strip()
        win_start_str = win_end_str = None
        if win:
            try:
                w_start, w_end = win.split("~")
                sh, sm = (int(x) for x in w_start.strip().split(":"))
                eh, em = (int(x) for x in w_end.strip().split(":"))
            except (ValueError, AttributeError):
                return jsonify({"error": "签到窗口格式应为 HH:MM ~ HH:MM"}), 400
            if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59 and (sh, sm) < (eh, em)):
                return jsonify({"error": "签到窗口非法（需 HH:MM 且开始早于结束）"}), 400
            win_start_str = f"{sh:02d}:{sm:02d}"
            win_end_str = f"{eh:02d}:{em:02d}"
        # 周日签到开关（1=开启/0=关闭）：仅请求携带时才更新，避免保存其他设置时误关
        sunday_sign = None
        if "sunday_sign" in data:
            sunday_sign = 1 if str(data.get("sunday_sign", "")).strip().lower() in ("1", "true", "on", "yes") else 0
        # 周六签到开关（1=开启/0=关闭）：默认开启，仅请求携带时才更新，避免保存其他设置时误关
        saturday_sign = None
        if "saturday_sign" in data:
            saturday_sign = 1 if str(data.get("saturday_sign", "")).strip().lower() in ("1", "true", "on", "yes") else 0
        # 全局暂停（一键暂停签到）：仅主管理员可写（上方 403 已拦），下一轮 cron 生效。
        # 1=暂停（signin.py 检测后 exit(2) 跳过），0/空=正常。
        global_pause = None
        if "global_pause" in data:
            global_pause = 1 if str(data.get("global_pause", "")).strip().lower() in ("1", "true", "on", "yes") else 0
        # ---- 注册账号验证 + 探针模式（任意管理员可改；v0.23.x）----
        account_verify = None
        if "account_verify" in data:
            account_verify = 1 if str(data.get("account_verify", "")).strip().lower() in ("1", "true", "on", "yes") else 0
        probe_enable = None
        if "probe_enable" in data:
            probe_enable = 1 if str(data.get("probe_enable", "")).strip().lower() in ("1", "true", "on", "yes") else 0
        probe_time = None
        if "probe_time" in data:
            pt = str(data.get("probe_time", "")).strip()
            try:
                ph, pm = (int(x) for x in pt.split(":"))
            except (ValueError, AttributeError):
                return jsonify({"error": "探针触发时间应为 HH:MM 格式"}), 400
            if not (0 <= ph <= 23 and 0 <= pm <= 59):
                return jsonify({"error": "探针触发时间非法（需 HH:MM）"}), 400
            probe_time = f"{ph:02d}:{pm:02d}"
        probe_interval = None
        if "probe_interval" in data:
            pi = str(data.get("probe_interval", "")).strip()
            if pi.lower() == "once":
                probe_interval = "once"
            else:
                try:
                    n = int(pi)
                except (TypeError, ValueError):
                    return jsonify({"error": "探针触发频率应为正整数（每 N 天）或 once（单次）"}), 400
                if n <= 0:
                    return jsonify({"error": "探针触发频率应为正整数（每 N 天）或 once（单次）"}), 400
                probe_interval = str(n)
        # ---- 全部校验通过，批量原子写入（避免多次独立写导致配置不一致）----
        # 批次7 A4：仅请求携带的字段才写入（缺失不重置）
        updates = {}
        if has_start:
            updates["YIBAN_START_DELAY_MAX"] = str(start) if start > 0 else ""
        if has_gap:
            updates["YIBAN_ACCOUNT_GAP_MAX"] = str(gap) if gap > 0 else ""
        if sign_mode:
            updates["YIBAN_SIGN_MODE"] = sign_mode
        if sign_order:
            updates["YIBAN_SIGN_ORDER"] = sign_order
        if sign_dist:
            updates["YIBAN_SIGN_DIST"] = sign_dist
        if edge_front is not None or edge_back is not None:
            # 只写其中一个时保持另一个现值；写入新键并删除旧键（迁移）
            if edge_front is None:
                edge_front = edge_config()[0]
            if edge_back is None:
                edge_back = edge_config()[1]
            updates["YIBAN_WINDOW_EDGE_FRONT_SEC"] = str(edge_front)
            updates["YIBAN_WINDOW_EDGE_BACK_SEC"] = str(edge_back)
            updates["YIBAN_WINDOW_EDGE_SEC"] = ""  # 旧键删除（前后对称语义已拆分为两键）
        if pref is not None:
            updates["YIBAN_ALLOW_TIME_PREF"] = str(pref)
        if win_start_str is not None:
            updates["YIBAN_SIGN_START"] = win_start_str
            updates["YIBAN_SIGN_END"] = win_end_str
        if sunday_sign is not None:
            updates["YIBAN_SUNDAY_SIGN"] = "1" if sunday_sign else ""
        if saturday_sign is not None:
            # 周六默认开启：关闭必须显式写 0（不能像周日那样删键——缺省会读回默认 1=开启）
            updates["YIBAN_SATURDAY_SIGN"] = "1" if saturday_sign else "0"
        if global_pause is not None:
            updates["YIBAN_GLOBAL_PAUSE"] = "1" if global_pause else ""
        if account_verify is not None:
            updates["YIBAN_ACCOUNT_VERIFY"] = "1" if account_verify else ""
        if probe_enable is not None:
            updates["YIBAN_PROBE_ENABLE"] = "1" if probe_enable else ""
        if probe_time is not None:
            updates["YIBAN_PROBE_TIME"] = probe_time
        if probe_interval is not None:
            updates["YIBAN_PROBE_INTERVAL_DAYS"] = probe_interval
        write_env_batch(ENV_FILE, updates)
        sunday_display = "不变" if sunday_sign is None else sunday_sign
        saturday_display = "不变" if saturday_sign is None else saturday_sign
        pause_display = "不变" if global_pause is None else ("暂停" if global_pause else "恢复")
        if edge_front is None and edge_back is None:
            edge_display = "不变"
        elif edge_front == edge_back:
            edge_display = f"各{edge_front / 60:g}分钟"
        else:
            edge_display = f"前{edge_front / 60:g}后{edge_back / 60:g}分钟"
        # 批量多选为前端会话级开关，不写入配置
        probe_display = "不变" if (probe_enable is None and probe_time is None and probe_interval is None) else \
            f"启={'1' if probe_enable else '0'}/时={probe_time or '-'}/频={probe_interval or '-'}"
        logger.info(
            "更新设置: 启动=%s 间隔=%s 签到模式=%s 排序=%s 分布=%s 掐头去尾=%s 自选=%s 窗口=%s 周日=%s 周六=%s 暂停=%s 账号验证=%s 探针=%s",
            start, gap, sign_mode or "不变", sign_order or "不变", sign_dist or "不变",
            edge_display, pref_raw if pref_raw is not None else "不变",
            win or "不变", sunday_display, saturday_display, pause_display,
            "不变" if account_verify is None else ("开" if account_verify else "关"),
            probe_display,
        )
        # 设置变更审计（2026-08-16 补 P8：此前调度/系统设置保存无留痕，与其他管理操作不一致）
        db.audit(
            session.get("username") or "?",
            "settings_save",
            "settings",
            f"启动延迟={start} 间隔={gap} 模式={sign_mode or '-'} 排序={sign_order or '-'} "
            f"分布={sign_dist or '-'} 掐头去尾={edge_display} "
            f"自选={pref_raw if pref_raw is not None else '-'} 窗口={win or '-'} 周日={sunday_display} "
            f"周六={saturday_display} "
            f"全局暂停={pause_display} 账号验证={'开' if account_verify else '关'} "
            f"探针={probe_display}",
        )
        return jsonify({"ok": True, "msg": "设置已保存（cron 下次触发自动生效）"})

    # ---- 全局公告（所有页面顶部显示；GET 公开，PUT 仅管理员）----
    @app.route("/api/changelog")
    def api_changelog():
        """更新日志：读取项目根 CHANGELOG.md（公开，无需登录；启动后缓存，部署重启自然失效）。"""
        if _changelog_cache[0] is None:
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.join(base, "CHANGELOG.md")
            try:
                with open(path, encoding="utf-8") as f:
                    _changelog_cache[0] = f.read()
            except OSError:
                _changelog_cache[0] = "暂无更新日志"
        return jsonify({"ok": True, "text": _changelog_cache[0]})

    @app.route("/api/announcement", methods=["GET"])
    def api_announcement():
        # 公告缓存：首次读取 .env，保存公告时更新（write 接口同步 _announcement_cache）
        if _announcement_cache[0] is None:
            _announcement_cache[0] = read_env(ENV_FILE).get("YIBAN_ANNOUNCEMENT", "").strip()
        return jsonify({"ok": True, "text": _announcement_cache[0]})

    @app.route("/api/announcement", methods=["PUT"])
    def api_announcement_save():
        data = _json_body()
        text = str(data.get("text", "")).strip()
        if len(text) > 200:  # 后端长度限制（与前端 maxlength=200 一致）
            return jsonify({"error": "公告内容过长（最多 200 字）"}), 400
        if "\n" in text or "\r" in text:
            # 安全审查 2026-08：公告存入 .env 单行键值，换行会注入新配置行
            # （如 YIBAN_ADMIN_PASSWORD_HASH），普通管理员即可借此提权为主管理员。
            # 前端为 textarea 但展示端换行本就折叠，直接拒绝（write_env_key 另有兜底）。
            return jsonify({"error": "公告内容不能包含换行（单行存储）"}), 400
        write_env_key(ENV_FILE, "YIBAN_ANNOUNCEMENT", text)
        _announcement_cache[0] = text  # 同步内存缓存
        db.audit(
            session.get("username") or "?",
            "announcement_save",
            "announcement",
            text or "（已清除）",
        )
        logger.info("公告已更新: %s", text[:50] or "（已清除）")
        # 批次11 N6：公告变更是普通管理员可用的对外触达渠道（社工面），变更可感知
        send_notification(
            "公告变更告警",
            f"全局公告已{'更新' if text else '清空'}，"
            f"操作者 {session.get('username', '?')}，"
            f"内容: {text[:80] or '（空）'}，"
            f"时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        )
        return jsonify({"ok": True, "msg": "公告已更新" if text else "公告已清除"})

    # ---- 连通性检测 ----
    @app.route("/api/ping", methods=["POST"])
    def api_ping():
        ok, detail = check_connectivity()
        return jsonify({"ok": True, "reachable": ok, "detail": detail})

    # ---- 时钟与签到状态 ----
    @app.route("/api/clock")
    def api_clock():
        text, color = sign_status()
        try:
            tz_offset_min = int(datetime.now().astimezone().utcoffset().total_seconds() // 60)
        except Exception:
            tz_offset_min = 0
        return jsonify(
            {
                "ok": True,
                "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "server_ts": int(time.time()),  # 服务器 epoch 秒，供前端平滑走秒与校准
                "tz_offset_min": tz_offset_min,  # 服务器本地时区相对 UTC 的分钟偏移
                "sign_status": text,
                "color": color,
            }
        )

    # ---- 每日自动清除超期注销用户（2026-08-17：修复"仅启动时清除一次"的隐患）----
    # 此前 purge_deleted_users 只在 db 连接初始化（服务启动）时执行，长期不重启的
    # 服务会让超期注销用户数据（邮箱、软删易班账密）无限留存，与页面"系统定期
    # 物理清除"的承诺不符。后台 daemon 线程：启动 60s 后先跑一次，此后每 24h 一次。
    def _daily_purge_loop():
        # 首轮延迟：避开启动高峰（迁移/预热），且此时 init_db 已跑过一次 purge，
        # 延迟不会造成额外的清除延迟（下一轮 24h 内必然覆盖）
        time.sleep(60)
        while True:
            try:
                # 每日集中清理（2026-08-28 审查 M6 收口）：审计/事件旧数据 +
                # 过期软删账号 + 过期注销用户 + 注销请求记录，统一走 db.run_daily_cleanup()。
                # 此前 _audit_cleanup/_event_cleanup（全表 DELETE）只挂在 init_db 上，
                # 而 signin 子进程每天 2~3 次 init_db 也各跑一轮，与 web 8 线程争锁；
                # 现清理只在 web 每日线程执行，signin 侧 init_db(cleanup=False)。
                db.run_daily_cleanup()
            except Exception as e:
                logger.warning("每日自动清除注销用户失败: %s", e)
            # 审计可追溯性每日校验（2026-08-28 审查 B-3）：
            # 此前 verify_audit_chain 生产环境从不调用、外部锚点只写不读——审计写入
            # 可静默丢（B-1）、删前缀/删尾/清空验不出（B-2）也无人知晓。
            # 现每日流程：先校验（链自洽 + 库外锚点比对 + 写入失败计数），任一异常
            # 即告警；校验通过且清理已发生后，再追加新锚点（使锚点反映清理后的
            # 合法状态，且记录 min_id/max_id 以覆盖删尾/清空检测，原两段格式不具备）。
            try:
                # 批次12 B12-4：显式传入锚点路径——原调用走 db.audit_anchor_path()
                # 默认解析（env 或 cwd），裸机部署下与本进程写锚点的 STATE_DIR 分裂，
                # 造成"每日误报锚点被删 + 真实锚点从未参与校验"的双重失效
                _health = db.audit_health(path=os.path.join(STATE_DIR, "audit-anchor.log"))
                if not _health["healthy"]:
                    _alert = (
                        "审计可追溯性校验失败："
                        f"链自洽={_health['chain_ok']}(broken={_health['broken']}) "
                        f"锚点={_health['anchor_ok']}（{_health['anchor_msg']}） "
                        f"审计写入失败次数={_health['write_failures']}。"
                        "审计记录可能被篡改/删除，或存在未留痕的管理操作，请立即核查！"
                    )
                    logger.error("审计链异常告警: %s", _alert)
                    send_notification("审计链异常告警", _alert)
                elif _health["anchor_msg"]:
                    # 非异常的提示性信息（如保留期清理回收了最早记录），记录即可
                    logger.info("审计链提示: %s", _health["anchor_msg"])
                # 批次12 B12-9：时钟守卫拦截后的持续告警——守卫拦截会把清理永久
                # 冻结（人工重置前不恢复），每日线程在此读 app_meta 留痕并发邮件，
                # 直到管理员运行 scripts/clock_guard_reset.py 重置为止（每日重发
                # 是刻意的：冻结状态必须保持可见，防止静默腐烂）
                _cg = db.clock_guard_alert()
                if _cg:
                    _cg_alert = (
                        "时钟跳变守卫告警：系统时间异常跳变已被拦截，全部物理清理"
                        f"处于冻结状态（告警时间 {_cg.get('ts', '?')}）。\n"
                        f"{_cg.get('note', '')}\n"
                        "请核实系统时间与 NTP；确认正确后运行 "
                        "python3 scripts/clock_guard_reset.py --confirm 重置。"
                    )
                    logger.error("时钟守卫告警: %s", _cg.get("note", ""))
                    send_notification("时钟跳变守卫告警", _cg_alert)
                db.record_audit_anchor(os.path.join(STATE_DIR, "audit-anchor.log"))
            except Exception as e:
                logger.warning("审计链每日校验/锚点写入失败: %s", e)
            time.sleep(24 * 3600)

    # 测试环境通过 YIBAN_DISABLE_PURGE_LOOP=1 禁止启动该线程（全量 pytest 会反复 create_app，
    # 大量 60s 后唤醒的线程并发访问共享 SQLite 单例有 access violation 风险）；
    # 生产默认不设置该变量，仍按原逻辑启动，且同一进程最多一个 daily-purge。
    if os.environ.get("YIBAN_DISABLE_PURGE_LOOP") != "1":
        with _purge_loop_lock:
            if not _purge_loop_started:
                _purge_loop_started = True
                threading.Thread(
                    target=_daily_purge_loop, daemon=True, name="daily-purge"
                ).start()

    # 前缀自适应：把 WSGI 层包一层（app 本身仍是 Flask 对象，.run()/gunicorn 调用不受影响）。
    # 支持子路径 / 独立子域 / 根路径三种部署；详见 BasePathMiddleware 类注释。
    app.wsgi_app = BasePathMiddleware(app.wsgi_app)

    return app


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    global ACCOUNTS_FILE, LOG_FILE, ENV_FILE, STATE_DIR, DB_FILE
    parser = argparse.ArgumentParser(description="易班自动签到网页管理系统")
    # 2026-08-20 对抗性审查 P2：默认改回环——werkzeug 开发服务器不应默认暴露
    # 全网卡（明文 HTTP + 无反代防护）。生产走 systemd/gunicorn 模板不受影响；
    # 确需直连局域网时显式传 --host 0.0.0.0（自担风险）。
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，仅回环）")
    # 非常见端口（默认 17892）：避开 8000/5000/3000 等常见端口，防止与其他部署冲突
    parser.add_argument("--port", type=int, default=17892, help="监听端口（默认 17892）")
    parser.add_argument(
        "--config", default=ACCOUNTS_DEFAULT, help=f"JSON 数据文件路径（迁移来源，默认: {ACCOUNTS_DEFAULT}）"
    )
    parser.add_argument("--log", default=LOG_DEFAULT, help=f"签到日志路径（默认: {LOG_DEFAULT}）")
    parser.add_argument("--env", default=ENV_DEFAULT, help=f".env 路径（默认: {ENV_DEFAULT}）")
    parser.add_argument(
        "--db", default=DB_DEFAULT, help=f"SQLite 数据库路径（默认: {DB_DEFAULT}）"
    )
    parser.add_argument("--debug", action="store_true", help="Flask 调试模式")
    args = parser.parse_args()
    ACCOUNTS_FILE = args.config
    LOG_FILE = args.log
    ENV_FILE = args.env
    DB_FILE = args.db
    STATE_DIR = STATE_DIR_DEFAULT

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger.info(
        "启动网页管理系统: http://%s:%d（数据库: %s / 日志: %s / .env: %s）",
        args.host,
        args.port,
        DB_FILE,
        LOG_FILE,
        ENV_FILE,
    )
    if not check_admin_configured():
        logger.warning(
            "未配置管理员账号：请在 %s 中设置 YIBAN_ADMIN_USER / YIBAN_ADMIN_PASSWORD", ENV_FILE
        )

    app = create_app(host=args.host)
    # 生产模式：debug 关闭（Werkzeug 单进程即可；如部署用 systemd 更稳）
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
