# -*- coding: utf-8 -*-
"""`yiban.store`：数据访问层（SQLite 表级 CRUD）。

事实源是本包的 `yiban.store.db`（连接、锁与兼容再导出都在那，各域定义点见同包子模块）；
本包其余模块按域把"单表的读写与状态机"逐步搬出来，`yiban.store.db` 保留同名再导出以兼容旧
调用方，`scripts/db.py` 只剩兼容壳。当前分工：`connection` 管连接单例与路径，`migrations`
管 schema 版本迁移与建表，`audit_chain` 管审计哈希链，`events` 管签到事件表，`accounts` 管
账号表 CRUD、行加解密与有效性判定，`verify_jobs` 管在线校验任务表的状态机，`users` 管用户表
与注销生命周期，`cleanup` 管每日清理编排。
"""
