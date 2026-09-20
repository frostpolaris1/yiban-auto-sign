# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""web 服务层：从 `web/app.py` 拆出的模块级辅助（各域一块，唯一真源在各自模块）。

**功能**
按域提供 `web/app.py` 拆出的模块级辅助：`accounts_data`（账号数据与审核态词表、口令
策略）、`capacity`（容量核计、配额判定与触顶告警）、`channel_health`（告警通道健康判据、
状态行与每日健康日报）、`env_io`（.env 读写与设置项展示）、`executor_env`（执行体清单与
其 .env 键）、`locks`（进程内锁的唯一定义点）、`logs`（签到日志族与账号状态族）、
`manual_sign`（手动签到子进程的等待回收、队列超时缩放与退出码留痕）、`measure`（现场实测
的状态文件与冷却判定）、`notify_mail`（通知与告警邮件族：正文净化、变更/审核邮件与双通道
发送）、`signstatus`（签到窗口与运行时段判定、系统信息）、`verify_queue`（在线校验的执行
闸门、异步任务与失败落库）。`web/app.py` 只保留 `create_app` 工厂、跨域中间件、`main()`
与迁移名的转发/再导出。

**归属**
`web.app.create_app` 的"模块级辅助"部分。包本身不含实现，只声明域划分；每个域的唯一定义
点在各自模块。工厂骨架、跨域中间件（限速 / 登录守卫 / CSRF / 安全响应头）与进程级共享
状态仍留在 `web/app.py`。

**复用**
域间依赖单向、直接取真源：`locks` 的 `_file_lock` / `_rate_lock` 是进程内锁的唯一定义点
（`security`、路由与 `web.app` 共用同一把）；`accounts_data` 的账号读取与口令策略被
`capacity` / `verify_queue` / `security` 复用；`env_io` 的 .env 读写被 `executor_env`
复用；`notify_mail` 的账本标签与收件人解析被 `channel_health` 复用。需要 app 侧打桩的
模块级名字（`ENV_FILE` / `read_env` / `send_notification` / `_sign_window` 等）一律由调用方
在调用时刻传入，域模块不自持同名绑定。

**通信**
`web.app` 在导入区按域取用并再导出，保持 `m.*` 可达（routes 经 `web.routes.appmod()` 取
用）；各域**不反向导入** `web.app`——依赖 app 模块级状态时按参数注入，既避免与 `app.py`
形成导入环，也避免另持绑定让打桩静默失效。
"""
