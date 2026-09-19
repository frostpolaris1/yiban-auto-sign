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
`appmod()` 供各域模块按属性延迟取用 web.app 的模块级名字（打桩面要求）；
`login_fails()` 是登录失败计数表的唯一取用点，认证与个人两域共用同一份账；
`high_risk_gate()` / `reconfirm_admin_password()` 取回留在 web.app 里的高危门禁闭包，
`admin_delete_limited()` 取回同族的"删除/通道变更"限速判定；
`verify_limits()` / `verify_fails()` 是账号验证配额与冷却的唯一取用点（管理员添加与
用户自助提交共用同一份账），`detail_limits()` 是账号详情读取限速表，`export_limits()`
是日志导出限速表，`read_audit_trace()` / `read_audit_denied_trace()` 是只读面聚合留痕
的两个入口。

**通信**
`web.app` 在 create_app 尾部 `register_all(app)` 一次接入；本包不在导入期反向导入
web.app——`appmod()` 在调用时才解析，避免与 `web/app.py` 形成导入环。
"""
import sys

from flask import current_app


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
    """装配全部路由域。顺序与原定义顺序一致；路径冲突会在启动时直接报错。"""
    from web.routes import (
        accounts_api,
        auth,
        data,
        me,
        my,
        notify,
        pages,
        settings_api,
        signin_api,
        users_api,
    )
    pages.register(app)
    auth.register(app)
    me.register(app)
    notify.register(app)
    accounts_api.register(app)
    my.register(app)
    data.register(app)
    users_api.register(app)
    settings_api.register(app)
    signin_api.register(app)
