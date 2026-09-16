# -*- coding: utf-8 -*-
"""`yiban.store`：数据访问层（SQLite 表级 CRUD）。

事实源是本包的 `yiban.store.db`（连接、迁移、锁都在那），本包其余模块按表逐步把
"单表的读写与状态机"搬出来，`yiban.store.db` 保留同名再导出以兼容旧调用方。
`scripts/db.py` 只剩兼容壳（转发到 `yiban.store.db`）；最终目标：`yiban.store.db`
继续按表拆薄，且不再有超长文件。
"""
