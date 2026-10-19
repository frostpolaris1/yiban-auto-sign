# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""路由域装配：把 web.app 的路由按域拆进本包，由 create_app 尾部一次注册。

**功能**
`register_all(app)` 依次调用各域模块的 `register(app)`；各域模块只把自己的路由
注册到 app 上，视图体自足。

**归属**
`web.app.create_app` 的"路由装配"部分。工厂骨架、跨域中间件（限速 / 登录守卫 /
CSRF / 安全响应头）与共享状态仍留在 `web/app.py`。

**复用**
`appmod()` 供各域模块按属性延迟取用 web.app 的模块级名字（打桩面要求）；批 3-a 拆墙后
它只承担 app.py **自有**的那批名字（env 句柄、缓存、SID 签发、窗口常量、`_json_body`
等）；app.py 以 import 形态转发的名字（A 类）已由各域从真源模块直取；
`login_fails()` 是登录失败计数表的唯一取用点，认证与个人两域共用同一份账；
`high_risk_gate()` / `reconfirm_admin_password()` 取回留在 web.app 里的高危门禁闭包，
`admin_delete_limited()` 取回同族的"删除/通道变更"限速判定；
`verify_limits()` / `verify_fails()` 是账号验证配额与冷却的唯一取用点（管理员添加与
用户自助提交共用同一份账），`detail_limits()` 是账号详情读取限速表，`export_limits()`
是日志导出限速表，`read_audit_trace()` / `read_audit_denied_trace()` 是只读面聚合留痕
的两个入口；`page_args()` 是列表接口（账号 / 用户 / 日志）可选分页参数的唯一解析点，
`page_window()` 是列表三域共用的唯一分页切片点，`page_meta()` 是账号 / 用户两域
分页元数据的唯一取用点（日志域的总数沿用既有键名 `total_lines`，故不走它）。

**通信**
`web.app` 在 create_app 尾部 `register_all(app)` 一次接入；本包不在导入期反向导入
web.app——`appmod()` 在调用时才解析，避免与 `web/app.py` 形成导入环。
"""
import sys

from flask import current_app, request

#: 列表接口分页参数的上下限（沿用日志列表的 1–200 口径）。
PAGE_LIMIT_MIN = 1
PAGE_LIMIT_MAX = 200
#: limit 缺省（参数缺失或非法时取它）；与日志列表 sign_events 的缺省同值。
PAGE_LIMIT_DEFAULT = 100


def appmod():
    """取"正在服务的那个 web.app 模块对象"（函数内延迟解析，避免导入环）。

    必须按 app 认领而不是 `import web.app` 直取：测试把 app.py 以别名加载
    （`webapp` / `webapp_xxx`，见 tests/*），此时 `import web.app` 会**再执行一份**
    app.py 副本，副本的模块级状态（WEB_VERSION、被打桩的 ENV_FILE 等）与在跑的
    那份不是同一个——路由体读到副本上，打桩就静默失效。
    `Flask(__name__)` 把 app.py 的模块名记在 `app.import_name` 上，据此取回本尊；
    该名字不在 sys.modules 时（非标准加载）回退到包导入。
    """
    mod = sys.modules.get(getattr(current_app, "import_name", ""))
    if mod is None:
        import web.app
        mod = web.app
    return mod


def page_args():
    """解析列表接口的可选分页参数 `limit` / `offset`（唯一取用点）。

    返回三元组 (limit, offset, paged)：
      - 两个参数都未出现时：paged=False、limit=None、offset=0。调用方必须返回
        **与分页前逐字相同**的整表响应（向后兼容，旧调用方不受影响）。
      - 出现任一参数时：paged=True。limit 夹取到 [1, 200]，offset 夹取到 >= 0。
      - 参数存在但不是十进制整数时回落到缺省（limit=100 / offset=0），不抛异常。
        这一口径与仓内日志列表（`web.routes.data.api_admin_sign_events` 的 limit）
        的处理一致：客户端把参数写坏时仍拿到有界的一页，而不是 500。
    上限 200 沿用日志列表的 1–200 口径，也落在 124 §B.2.3 的
    "limit=100 → 约 45 KiB/页"外推以内。两域（账号 / 用户）共用本函数，避免各写一份。
    """
    args = request.args
    if "limit" not in args and "offset" not in args:
        return None, 0, False
    try:
        limit = int(args.get("limit", PAGE_LIMIT_DEFAULT))
    except (TypeError, ValueError):
        limit = PAGE_LIMIT_DEFAULT
    limit = min(max(limit, PAGE_LIMIT_MIN), PAGE_LIMIT_MAX)
    try:
        offset = int(args.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(offset, 0)
    return limit, offset, True


def page_window(items, limit, offset):
    """列表分页的唯一切片点：返回 `[offset, offset+limit)`。

    账号 / 用户 / 日志三域共用本函数。切片表达式只有一处，故"任一 offset 都不许
    返回越界行或重复行"这条不变式只有一份承重实现。`offset` 已由 `page_args()`
    夹到 >= 0；`limit` 已由 `page_args()` 夹到 [1, 200]。切片超出末尾返回空列表
    （不报错），与"空页/越界"的常规工程语义一致。
    """
    return items[offset:offset + limit]


def page_meta(total, limit, offset, returned):
    """分页元数据四键（`total`/`limit`/`offset`/`returned`，账号 / 用户两域同形）。

    日志域的总数沿用既有键名 `total_lines`，不另立 `total`，故不走本函数。
    只在分页时下发；不传分页参数时不得出现（向后兼容，旧调用方不受影响）。
    `total` 是**整表**条数（不随页窗口变化），`returned` 是本页实际条数。
    """
    return {"total": total, "limit": limit, "offset": offset, "returned": returned}


def login_fails():
    """登录失败计数表：create_app 登记在 extensions，保每 app 实例一份。

    `_rate_lock` 保护的同一个 dict 同时被登录、改密、注销、恢复四条路径读写
    （安全语义依赖同一份账），故取用点收在本包一处，各域不再各持别名。
    """
    return current_app.extensions["yiban_login_fails"]


def high_risk_gate():
    """高危动作统一门禁：先二次鉴权，通过后才占用高危限速额度（每 app 实例一份闭包）。

    实现留在 `web.app.create_app`（它闭包依赖工厂局部的限速计数表，做成模块级会跨
    app 实例串额度），create_app 在建 app 时把该闭包登记进 extensions；本函数是其
    唯一取用点，通知配置域经此调用。
    """
    return current_app.extensions["yiban_high_risk_gate"]


def reconfirm_admin_password():
    """高危二次鉴权入口（校验走门禁的独立计数，不碰登录失败表；每 app 实例一份闭包）。

    与 `high_risk_gate()` 同源：门禁闭包留在 `web.app.create_app`，按 app 实例登记。
    """
    return current_app.extensions["yiban_reconfirm_admin_password"]


def sensitive_password_gate():
    """敏感操作口令复核的唯一入口（每 app 实例一份闭包）。

    `POST /api/settings` 的系统开关、`/api/announcement/publish` 与执行体写操作三处
    共用同一份失败计数与冷却，故取用点收在本包一处；实现留在 `web.app.create_app`
    （闭包依赖工厂局部的敏感口令计数表，做成模块级会跨 app 实例串账）。
    """
    return current_app.extensions["yiban_sensitive_password_gate"]


def admin_delete_limited():
    """高危删除/告警通道变更的窗口限速判定：超限返回 True（每 app 实例一份闭包）。

    与 `high_risk_gate()` 同源（它内部也调这一个判定）；"删数据"与"拆报警器"共用同一
    份计数，不只是否可逆区分前台入口，故取用点收在本包一处。
    """
    return current_app.extensions["yiban_admin_delete_limited"]


def verify_limits():
    """账号验证尝试配额表 {username: (count, window_start)}（每 app 实例一份）。

    管理员添加账号与用户自助提交两条路径共用同一份配额，故取用点收在本包一处。
    """
    return current_app.extensions["yiban_verify_limits"]


def verify_fails():
    """账号验证认证失败冷却表 {phone: (fails, window_start, cooldown_until)}（每 app 实例一份）。

    与 `verify_limits()` 同源：两条提交路径共用同一份冷却账。
    """
    return current_app.extensions["yiban_verify_fails"]


def detail_limits():
    """账号详情读取限速表 {actor: (count, window_start)}（每 app 实例一份）。

    按会话而非 IP 计数（校园网出口共享，按 IP 会把两个管理员的运维互相挡死）。
    """
    return current_app.extensions["yiban_detail_limits"]


def dupcheck_limits():
    """个人提交判重预检命中限速表 {actor: (count, window_start)}（每 app 实例一份）。

    按会话而非 IP 计数（校园网出口共享，按 IP 会把同出口的正常用户互相挡死）；
    只计"判重命中"，未重号的正常提交不占额度。
    """
    return current_app.extensions["yiban_dupcheck_limits"]


def export_limits():
    """日志导出限速表 {ip: (count, window_start)}（每 app 实例一份）。

    仅日志导出域使用；取用点收在本包一处，与其余限速表同一形态。
    """
    return current_app.extensions["yiban_export_limits"]


def read_audit_trace():
    """只读面聚合留痕：窗口内聚合成一行，不逐请求写（每 app 实例一份闭包）。"""
    return current_app.extensions["yiban_read_audit_trace"]


def read_audit_denied_trace():
    """只读面"超限被拒"留痕：每窗口至多一行（每 app 实例一份闭包）。"""
    return current_app.extensions["yiban_read_audit_denied_trace"]


def register_all(app):
    """装配全部路由域。各域注册顺序不参与路由判定（无同路径同方法重叠）。"""
    from web.routes import (
        accounts_api,
        audit_api,
        auth,
        data,
        executor_health_api,
        me_api,
        my_api,
        notify,
        pages,
        run_events_api,
        settings_api,
        signin_api,
        stream_api,
        users_api,
    )
    pages.register(app)
    auth.register(app)
    me_api.register(app)
    notify.register(app)
    accounts_api.register(app)
    my_api.register(app)
    data.register(app)
    users_api.register(app)
    audit_api.register(app)
    run_events_api.register(app)
    executor_health_api.register(app)
    stream_api.register(app)
    settings_api.register(app)
    signin_api.register(app)
