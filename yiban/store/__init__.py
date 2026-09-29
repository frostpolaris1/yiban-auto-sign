# -*- coding: utf-8 -*-
"""`yiban.store`：数据访问层（SQLite 表级 CRUD）。

每张表的**唯一定义点在本包对应的域模块**；`yiban.store.db` 是门面，只自持连接编排
（`init_db`）、写事务入口与时钟守卫等跨域助手，并把各域名字再导出以兼容旧调用方
（`db.load_accounts()` 一类调用继续可用）——**依赖方向是 db → 域模块**，不是域模块迁进 db。
再导出多数按原名，`claims` / `verify_jobs` 两域按重命名别名（逐条别名见 `yiban.store.db`
绑定处的行尾注释，故 `db.try_claim` 一类原名不存在）。`scripts/db.py` 只是指向门面的兼容壳。
当前分工：`connection` 管连接单例与路径，`migrations` 管 schema 版本迁移与建表，
`audit_chain` 管审计哈希链，`events` 管签到事件表与 audit_logs
上的暂停冷却查询，`accounts` 管
账号表 CRUD、行加解密与有效性判定，`session_cache` 管会话凭据缓存的读写与有效期判定，
`time_prefs` 管自选时间片表的读写、拥挤度统计与保存冷却查询，
`clock_meta` 管时钟守卫告警的留痕与读取、app_meta 通用单键读写，
`tracking` 管追踪盐（YIBAN_TRACK_SALT）的取用/落盘与 IP、手机号加盐哈希，
`verify_jobs` 管在线校验任务表的状态机，`queue_store` 管持久化任务队列
（`sign_tasks`）的批量领取/收尾/重排与当日计数，`users` 管用户表
与注销生命周期，`cleanup` 管每日清理编排。
"""
