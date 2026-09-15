# -*- coding: utf-8 -*-
"""`yiban.store`：数据访问层（SQLite 表级 CRUD）。

现有事实源仍是 `scripts/db.py`（连接、迁移、锁都在那），本包按表逐步把
"单表的读写与状态机"搬出来，`scripts/db.py` 保留同名再导出以兼容旧调用方。
最终目标：db.py 拆为 store/*，且不再有超长文件。
"""
