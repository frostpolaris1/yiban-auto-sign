# -*- coding: utf-8 -*-
"""`yiban.store`：数据访问层（SQLite 表级 CRUD）。

事实源是本包的 `yiban.store.db`（连接、锁与兼容再导出都在那，各域定义点见同包子模块）；
本包其余模块按域把"单表的读写与状态机"逐步搬出来，`yiban.store.db` 保留同名再导出以兼容旧
调用方，`scripts/db.py` 只剩兼容壳。当前分工：`connection` 管连接单例与路径，`migrations`
管 schema 版本迁移与建表，`audit_chain` 管审计哈希链，`events` 管签到事件表与 audit_logs
上的暂停冷却查询，`accounts` 管
账号表 CRUD、行加解密与有效性判定，`session_cache` 管会话凭据缓存的读写与有效期判定，
`time_prefs` 管自选时间片表的读写、拥挤度统计与保存冷却查询，
`clock_meta` 管时钟守卫告警的留痕与读取、app_meta 通用单键读写，
`tracking` 管追踪盐（YIBAN_TRACK_SALT）的取用/落盘与 IP、手机号加盐哈希，
`verify_jobs` 管在线校验任务表的状态机，`queue_store` 管持久化任务队列
（`sign_tasks`）的批量领取/收尾/重排与当日计数，`users` 管用户表
与注销生命周期，`cleanup` 管每日清理编排。
"""
