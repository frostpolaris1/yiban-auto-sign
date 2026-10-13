# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""执行体健康卡只读路由：`GET /api/admin/executor-health`（管理员面）。

**功能**
本端点给巡检面一块**执行体健康卡**：每个执行体一行，回答三件事——心跳新鲜度、
最后活动时刻、吞吐（名下了结数 ÷ 有效签到窗口长度）。数据来自状态目录里的心跳
文件与 `sign_tasks` 台账的当日归属计数，两者都由既有唯一实现读出（见「复用」）。

**为什么单独开一个端点，而不是复用 `GET /api/scheduler/executors`**
那条端点是**设置域**的读接口，会顺带做执行体清单的一次性迁移写回（读 `.env` 时
按旧三键补写清单键）。巡检面是只读面：翻一眼日志页不该改 `.env`。本端点只读清单
（`yiban.egress.manifest_state`，不写回）、只读心跳文件、只读台账计数。

**入库契约（改动即破坏 Vue 线对接，须同步前端）**
1. 查询键白名单：**无查询参数**；出现任何查询键即 400 JSON
   （`{"error": "不支持的过滤字段: X"}`）。
2. 身份只认心跳文件名（`state_io.worker_alive_identity`）：记录体自称的 `role` /
   `index` 不可信（工单 ba-p03-09，生产实测兜底身份的心跳文件内容写着
   `{"role": "worker", "index": 0}`）。卡片回 `identity` / `role` / `index` /
   `heartbeat_file`，四者同源。
3. 心跳新鲜度由 `state_io.worker_alive_facts` 给出：`state` 是既有四态
   （`running` / `finished` / `idle` / `stale`），`age_sec` 与 `heartbeat_at` 随附；
   无记录时三者均为 `null`（页面据此显式写"无心跳"，不留白、不显示 0）。
4. 吞吐口径单义：分子＝当日该执行体名下**了结**的账号数，分母＝有效签到窗口长度
   （秒）。与 `run_events` 的事件计数（不去重）不是同一口径。
5. 脱敏：只回执行体身份（角色 + 槽位 + 中文标签）与聚合计数，**不回** owner 原串、
   主机名、进程号，也不出逐账号明细（与执行体面既有纪律一致）。
6. 只读：不写 `.env`（不触发执行体清单的迁移写回）、不写锚点文件、**不调全表哈希
   校验**（照 audit_api 与 run_events_api 的先例）。访问留痕复用 `read_audit_trace()`
   ——它按只读面既有惯例写一条聚合审计行（`executor_health_read`），轨迹只含条数。
7. 错误一律 JSON：400/401/403/500 全 JSON。

**归属**
`web.app.create_app` 的"数据"面。权限复用登录守卫：本路径不在任何放行清单内，
故匿名请求得 401、普通用户得 403（登录守卫本体见 `web/app.py`）。

**复用**
卡片装配住在 `web/services/executor_env.py` 的 `_executor_health_cards`（执行体展示
族的同一份实现）；窗口口径复用 `_executors_window`（与容量换算同一份）；四态与
新鲜度复用 `yiban.engine.state_io`。

**通信**
视图体经 `web.routes.appmod()` 取 web.app 的模块级名字（`logger`、
`_executor_health_cards`、`_executors_window`），避免与 `web/app.py` 形成导入环。
"""

from flask import jsonify, request

from web.routes import appmod as _appmod
from web.routes import read_audit_trace

_ALLOWED_QUERY_KEYS: set[str] = set()


def api_executor_health():
    """执行体健康卡（管理员）。入库契约见模块 docstring。"""
    m = _appmod()
    for key in request.args:
        if key not in _ALLOWED_QUERY_KEYS:
            return jsonify({"error": f"不支持的过滤字段: {key}"}), 400

    try:
        # 窗口口径与容量换算同源（`_executors_window` = `_sign_window` + `window.bounds`）
        bounds = m._executors_window()
        window_sec = bounds.full_sec() if bounds else 0
        body = m._executor_health_cards(m.ENV_FILE, window_sec)
    except Exception as e:  # 展示面不得因一个读点失败把整页打成 5xx
        m.logger.warning("执行体健康卡读取失败: %s", e)
        return jsonify({"error": "执行体健康卡读取失败"}), 500

    # 只读面留痕：与 users/logs 同口径。target 只放执行体数与状态计数，不含行内容。
    read_audit_trace()("executor_health_read", f"{len(body['executors'])} 个执行体")
    body["ok"] = True
    return jsonify(body)


def register(app):
    """在本域注册一条只读路由；endpoint 取函数名。"""
    app.add_url_rule("/api/admin/executor-health", view_func=api_executor_health)
