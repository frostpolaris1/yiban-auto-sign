# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""页面域路由：管理端/用户端页面、站标与爬虫协议、旧路径重定向、登录页与文档页。

**功能**
服务端渲染的全部非 API 路由——管理端六页（数据总览 / 账号 / 签到日志 / 用户 /
系统设置 / 我的账号·日历）、用户端两页（账号与设置 / 签到日历）、登录页、用户协议与
隐私政策页、favicon 与备案图标、robots.txt，以及改版前旧路径到新页面的 302。

**归属**
`web.app.create_app` 的"页面面"。工厂骨架、跨域中间件（限速 / 登录守卫 / CSRF /
安全响应头）与各 API 域仍留在 `web/app.py`。

**复用**
`register(app)` 供 `web.routes.register_all` 装配；`NO_STORE_PAGES`（页面路径禁缓存清单）
由 `web/app.py` 的 `no_cache` 中间件消费——页面路径的唯一登记点在本模块。

**通信**
本模块不持有 app 实例：注册在 `register(app)` 里做，请求期需要 app 自身的东西
（`static_folder` / `response_class`）走 Flask `current_app`。凡引用 web.app 的模块级
名字（`_current_role`、渲染助手、版本与备案常量等）一律经 `web.routes.appmod()` 按属性
取——`mock.patch.object(web.app, …)` 的打桩点必须继续生效（见 `appmod()`）。登录页的
循环计数挂 `current_app.extensions`，保持"每个 app 实例一份"的原工厂局部变量语义。
"""
import os
import time

from flask import (
    abort,
    current_app,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from web.routes import appmod as _appmod
from web.services import signstatus as _signstatus
from yiban import status as _yiban_status
from yiban.infra import env_io as _env_io


# `.env` 行分隔符码点：前端"提交前拒含换行族的输入"必须与后端**同一份清单**
# （`yiban.infra.env_io.ENV_LINE_BREAK_CHARS`），故由后端渲染进页面而不是前端另抄一份。
# 只渲染页面（不引入构建步骤）；核心符号换取时值仍与写入口字符集同源。
def _env_line_break_codes():
    return sorted(ord(ch) for ch in _env_io.ENV_LINE_BREAK_CHARS)


def _calendar_page_context():
    """日历页的状态显示上下文：状态表 + 图例 + 今日门真值（服务端渲染，无构建步骤）。

    **同源方式**：状态显示表是 `yiban.status.DISPLAY`（唯一事实源）。图例由
    `legend_items()` 渲染成 `<li>`；同一份表经 `display_payload()` 序列化进页面的内联
    脚本（`window.YB_CALENDAR_STATE`），供账号卡状态行与日期格消费。日历渲染与图例因此
    消费同一份表——新增状态码只会同时出现在两侧，不再有"渲染认得、图例不认得"的漂移。

    `day_off` 取 `web.app._day_off_reason`（读 `.env` 真值、与引擎同一判据）：急停/周末
    在日历上的口径与引擎实际行为一致，而不是"界面上说没有"。门语义一行未改。
    """
    m = _appmod()
    payload = _yiban_status.display_payload()
    payload["day_off"] = _signstatus.day_off_payload(m._day_off_reason())
    return {"status_legend": _yiban_status.legend_items(), "calendar_state": payload}


def _render_admin_page(template, nav_key, crumbs, extra=None):
    """管理端页面统一上下文：版本 / 备案 / 导航高亮 / 面包屑 / 当前身份。

    身份显式下发（而非模板内读 session），便于侧栏常驻显示当前账号——
    这是防误操作设计：登录错账号后误删数据的代价高。
    `extra` 供单页追加自己的上下文（如日历页的状态表与图例）。
    """
    m = _appmod()
    return render_template(
        template,
        web_version=m.WEB_VERSION,
        app_version=m.APP_VERSION,
        icp_info=m.icp_info(),
        police_info=m.police_info(),
        police_link=m.police_link(),
        nav_active=nav_key,
        crumbs=crumbs,
        current_username=session.get("username", ""),
        current_role=m._current_role() or "",
        env_line_break_codes=_env_line_break_codes(),
    )


def _admin_page_redirect():
    """管理端页面守卫：未登录 → 登录页；非管理员 → 用户页。合规时返回 None。

    返回而非装饰，是因为三处守卫各自需要不同模板/导航键，装饰器会增加一层间接。
    """
    role = _appmod()._current_role()
    if role is None:
        return redirect(url_for("login_page"))
    if role != "admin":
        return redirect(url_for("user_calendar_page"))
    return None


# ---- 页面路径：`组/页面`（数据 / 工作台 / 我的 + 用户端）----
# 分组标题与首段一致，页面与第二段一致，便于按 URL 反推归属。
def dashboard_page():
    blocked = _admin_page_redirect()
    if blocked:
        return blocked
    return _render_admin_page("pages/data_dashboard.html", "data-dashboard", ["数据", "数据总览"])


# 旧路径 → 新路径：书签/分享链接不失效。用 302 而非 308：本项目仍在演进，
# 永久重定向会被浏览器长期缓存，路径再调整时无法纠正。
# 值写**端点名**而不是路径字面量：redirect() 不做 SCRIPT_NAME 拼接，子路径部署
# （/tools/yiban-…/）下写死 "/work/accounts" 会把用户甩回域名根。
_MOVED_PAGES = {
    "/logs": "logs_page",
    "/accounts": "accounts_page",
    "/users": "users_page",
    "/settings": "settings_page",
    "/mine": "my_account_page",
    "/mine/calendar": "my_calendar_page",
    "/user": "user_account_page",
}


def _moved_page_view(endpoint):
    def view():
        # 保留查询串：theme_boot 的版本兜底跳 `/?v=<版本>`，丢掉 ?v= 会让它反复重试
        qs = request.query_string.decode("utf-8", "ignore")
        return redirect(url_for(endpoint) + (("?" + qs) if qs else ""), code=302)
    return view


def favicon_png():
    """站标：优先服务部署者自放的 static/vendor/favicon.png（不入库，与 logo.png 同机制）。

    重写后模板引用带挂载前缀，请求进入应用而非域名层静态目录，须自带路由；
    未放置时 404（浏览器退回默认图标）。短缓存便于部署者换图后及时生效。
    """
    icon_path = os.path.join(current_app.static_folder, "vendor", "favicon.png")
    if not os.path.isfile(icon_path):
        abort(404)
    resp = send_file(icon_path, mimetype="image/png", conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


def gongan_beian_png():
    """页脚备案图标：服务部署者自放的 static/vendor/gongan-beian.png（不入库，未放置 404）。

    模板引用带挂载前缀，须自带路由；短缓存便于换图后及时生效。
    """
    icon_path = os.path.join(current_app.static_folder, "vendor", "gongan-beian.png")
    if not os.path.isfile(icon_path):
        abort(404)
    resp = send_file(icon_path, mimetype="image/png", conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


def robots_txt():
    """爬虫协议：只放行登录页与静态资源，登录后的私有页（/api、/data、/work、/my、
    /user 及各旧路径）一律禁止抓取。

    路径带挂载前缀（子路径部署下 robots.txt 与页面同前缀），否则爬虫会去抓
    前缀之外的地址而拿到 404，反而把私有页当"可抓"。
    """
    root = (request.script_root or "").rstrip("/")
    lines = [
        "User-agent: *",
        f"Allow: {root}/login",
        f"Allow: {root}/static/",
        "Disallow: /",
    ]
    resp = current_app.response_class("\n".join(lines) + "\n", mimetype="text/plain")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


def root_page():
    """根路径：按登录态**一步**转到对应首页。

    不做成到 /data/dashboard 的盲跳：未登录时会多一跳（/ → /data/dashboard → /login），
    而 theme_boot 的版本兜底跳的正是 `/?v=`，链越长越容易在弱网下闪现中间态。
    """
    role = _appmod()._current_role()
    if role is None:
        return redirect(url_for("login_page"))
    if role == "admin":
        return redirect(url_for("dashboard_page"))
    # 普通用户的首页是「签到日历」而不是「账号与设置」：进站第一眼要看到今天的签到结果，
    # 账号本身是低频维护对象（用户 2026-09-13 指定）
    return redirect(url_for("user_calendar_page"))


def _user_page_redirect():
    """用户端页面守卫：未登录 → 登录页；管理员 → 管理端首页。合规时返回 None。

    与管理端守卫同构：返回而非装饰，便于各页按需处理。
    """
    role = _appmod()._current_role()
    if role is None:
        return redirect(url_for("login_page"))
    if role != "user":
        return redirect(url_for("dashboard_page"))
    return None


def _render_user_page(template, nav_key, crumbs, extra=None):
    """用户端页面统一上下文（与管理端同构：版本 / 备案 / 导航高亮 / 面包屑）。

    身份不在此下发：用户端外壳由 core.js 的 /api/me 填充账号区，
    避免服务端再走一次会话取值（管理员页下发的理由见 _render_admin_page）。
    `extra` 的意义同 `_render_admin_page`（日历页的状态表与图例）。
    """
    m = _appmod()
    return render_template(
        template,
        web_version=m.WEB_VERSION,
        app_version=m.APP_VERSION,
        icp_info=m.icp_info(),
        police_info=m.police_info(),
        police_link=m.police_link(),
        nav_active=nav_key,
        crumbs=crumbs,
        **(extra or {}),
    )


def user_account_page():
    blocked = _user_page_redirect()
    if blocked:
        return blocked
    return _render_user_page("pages/user_account.html", "user-account", ["用户中心", "账号与设置"])


def user_calendar_page():
    blocked = _user_page_redirect()
    if blocked:
        return blocked
    return _render_user_page("pages/user_calendar.html", "user-calendar", ["用户中心", "签到日历"],
                             extra=_calendar_page_context())


# 登录页循环检测计数 {ip: (count, first_ts)}：浏览器缓存旧 JS 时可能无限 302 循环，
# 同 IP 短时间频繁访问 /login 超过阈值 → 直接渲染登录页打断循环。
# 存 app 实例上（原为 create_app 局部变量）：测试进程会反复 create_app，
# 进程级共享会让上一个实例的计数漏进下一个。
_LOGIN_LOOP_LIMIT = 1000  # 条目上限，防止内存无限增长


def _login_loop():
    return current_app.extensions.setdefault("yiban_login_loop", {})


def login_page():
    m = _appmod()
    if session.get("auth"):
        ip = m._client_ip()
        now = time.time()
        loop = _login_loop()
        m._ip_store_trim(loop, 60)
        # 条目上限防护：超出时清理最老的 20%
        if len(loop) > _LOGIN_LOOP_LIMIT:
            sorted_ips = sorted(loop, key=lambda k: loop[k][1])
            for old_ip in sorted_ips[:_LOGIN_LOOP_LIMIT // 5]:
                loop.pop(old_ip, None)
        cnt, first = loop.get(ip, (0, now))
        if now - first > 10:
            cnt, first = 0, now
        cnt += 1
        loop[ip] = (cnt, first)
        if cnt < 4:
            return redirect(url_for("dashboard_page") if m._current_role() == "admin" else url_for("user_calendar_page"))
        m.logger.warning("检测到登录页访问循环（IP %s），已打断并渲染登录页", m.db.hash_ip(ip))
    return render_template(
        "login.html",
        web_version=m.WEB_VERSION,
        app_version=m.APP_VERSION,
        icp_info=m.icp_info(),
        police_info=m.police_info(),
        police_link=m.police_link(),
        site_description=m.site_description(),
        site_image=m.site_image(),
        agreement_html=m._read_doc_html("USER_AGREEMENT.md"),
        privacy_html=m._read_doc_html("PRIVACY_POLICY.md"),
    )


def terms_page():
    """用户协议独立页（footer / 隐私链接可指向）。"""
    m = _appmod()
    return m._doc_page("用户协议", m._read_doc_html("USER_AGREEMENT.md"),
                       m.icp_info(), m.police_info(), request.script_root, m.police_link())


def privacy_page():
    """隐私政策独立页（footer / 隐私链接可指向）。"""
    m = _appmod()
    return m._doc_page("隐私政策", m._read_doc_html("PRIVACY_POLICY.md"),
                       m.icp_info(), m.police_info(), request.script_root, m.police_link())


# 管理端各功能页。拆页而非单页 tab：URL 可书签/可分享、刷新不丢状态，
# 且每页只加载自己的脚本（单页方案需一次性加载全部 5 个功能域的 JS）。
def accounts_page():
    blocked = _admin_page_redirect()
    if blocked:
        return blocked
    return _render_admin_page("pages/work_accounts.html", "work-accounts", ["工作台", "账号管理"])


def logs_page():
    blocked = _admin_page_redirect()
    if blocked:
        return blocked
    return _render_admin_page("pages/data_logs.html", "data-logs", ["数据", "签到日志"])


def users_page():
    blocked = _admin_page_redirect()
    if blocked:
        return blocked
    return _render_admin_page("pages/work_users.html", "work-users", ["工作台", "用户管理"])


def settings_page():
    blocked = _admin_page_redirect()
    if blocked:
        return blocked
    return _render_admin_page("pages/work_settings.html", "work-settings", ["工作台", "系统设置"])


def my_account_page():
    blocked = _admin_page_redirect()
    if blocked:
        return blocked
    return _render_admin_page("pages/my_account.html", "my-account", ["我的", "我的账号"])


# 管理员本人的签到日历（与用户端 /user/calendar 同源）；个人域的一部分，
# 数据取本人邮箱归属账号（与 /my/account 同口径，一人一号）。
def my_calendar_page():
    blocked = _admin_page_redirect()
    if blocked:
        return blocked
    return _render_admin_page("pages/my_calendar.html", "my-calendar", ["我的", "我的日历"],
                              extra=_calendar_page_context())


# ---- 页面缓存策略：管理页面禁止缓存（防浏览器缓存旧版 JS 导致登录循环）----
# 需要禁缓存的页面路径：全部页面路由 + 旧的被重定向路径（含根路径）。
# web.app 的 no_cache 中间件消费本清单（页面路径的唯一登记点在此）。
NO_STORE_PAGES = frozenset(_MOVED_PAGES) | {
    "/", "/login", "/terms", "/privacy",
    "/data/dashboard", "/data/logs",
    "/work/accounts", "/work/users", "/work/settings",
    "/my/account", "/my/calendar",
    "/user/account", "/user/calendar",
}


def register(app):
    """在本域注册全部页面路由；endpoint 取函数名（url_for 依赖）。"""
    app.add_url_rule("/data/dashboard", view_func=dashboard_page)

    # 旧路径 302：endpoint 名与原实现一致（moved_<路径段下划线>）
    for old, target in _MOVED_PAGES.items():
        app.add_url_rule(
            old,
            endpoint="moved_" + old.strip("/").replace("/", "_"),
            view_func=_moved_page_view(target),
        )

    app.add_url_rule("/favicon.png", view_func=favicon_png)
    app.add_url_rule("/gongan-beian.png", view_func=gongan_beian_png)
    app.add_url_rule("/robots.txt", view_func=robots_txt)

    app.add_url_rule("/", view_func=root_page)
    app.add_url_rule("/user/account", view_func=user_account_page)
    app.add_url_rule("/user/calendar", view_func=user_calendar_page)
    app.add_url_rule("/login", view_func=login_page)
    app.add_url_rule("/terms", view_func=terms_page)
    app.add_url_rule("/privacy", view_func=privacy_page)

    app.add_url_rule("/work/accounts", view_func=accounts_page)
    app.add_url_rule("/data/logs", view_func=logs_page)
    app.add_url_rule("/work/users", view_func=users_page)
    app.add_url_rule("/work/settings", view_func=settings_page)
    app.add_url_rule("/my/account", view_func=my_account_page)
    app.add_url_rule("/my/calendar", view_func=my_calendar_page)
