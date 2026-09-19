# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""web 服务层：从 `web/app.py` 拆出的模块级辅助（各域一块，唯一真源在各自模块）。

成员：`env_io`（.env 读写与设置项展示）、`executor_env`（执行体清单与其 .env 键）、
`locks`（进程内锁的唯一定义点）。`web/app.py` 只保留 `create_app` 工厂、跨域中间件、
`main()` 与迁移名的转发/再导出。
"""
