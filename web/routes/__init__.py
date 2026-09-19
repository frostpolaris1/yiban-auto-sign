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
`appmod()` 供各域模块按属性延迟取用 web.app 的模块级名字（打桩面要求）。

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


def register_all(app):
    """装配全部路由域。顺序与原定义顺序一致；路径冲突会在启动时直接报错。"""
    from web.routes import auth, pages
    pages.register(app)
    auth.register(app)
