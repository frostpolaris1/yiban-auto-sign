# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""web 服务层：从 `web/app.py` 拆出的模块级辅助（各域一块，唯一真源在各自模块）。

成员：`accounts_data`（账号数据与审核态词表、口令策略）、`env_io`（.env 读写与设置项展示）、
`executor_env`（执行体清单与其 .env 键）、`locks`（进程内锁的唯一定义点）、
`logs`（签到日志族与账号状态族）、`measure`（现场实测的状态文件与冷却判定）、
`signstatus`（签到窗口与运行时段判定、系统信息）、`verify_queue`（在线校验的执行闸门、
异步任务与失败落库）。`web/app.py` 只保留 `create_app` 工厂、跨域中间件、`main()` 与
迁移名的转发/再导出。
"""
