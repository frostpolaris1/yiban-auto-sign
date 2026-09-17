# -*- coding: utf-8 -*-
"""`yiban.store`：数据访问层（SQLite 表级 CRUD）。

事实源是本包的 `yiban.store.db`（连接、迁移、锁都在那）；本包其余模块按表把"单表的读写
与状态机"逐步搬出来，`yiban.store.db` 保留同名再导出以兼容旧调用方，`scripts/db.py` 只剩
兼容壳。当前分工：`accounts` 管账号有效性判定与孤儿会话缓存清理，`verify_jobs` 管在线
校验任务表的状态机。
"""
