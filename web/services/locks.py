# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""web 服务层共用的进程内锁（**唯一定义点**：勿在别处再建一把同名语义的锁）。

**功能**
持有 `_file_lock`——账号 / 用户等 SQLite「读-改-写」序列的进程内互斥锁（RLock，
可重入：写操作内再取一次不会自锁）。

**归属**
原 `web/app.py` 的模块级锁对象，唯一真源在本模块；`web/app.py` 以
`from web.services.locks import _file_lock` 再导出，故 `web.app._file_lock` 与
`m._file_lock`（路由）拿到的是**同一个对象**。跨进程/跨 worker 的一致性不靠它，
而靠 SQLite 事务与 UNIQUE 约束。

**复用**
路由（`web/routes/*.py` 经 `m._file_lock`）、app.py 的账号读族
（`load_accounts` / `load_accounts_raw` / `load_users`）以及后续按域迁出的服务模块
共用这一把锁；各模块自建一把会让"同进程内读写互斥"静默失效。

**通信**
本模块不导入任何业务模块（也不反向导入 `web.app`），只依赖标准库；因此任何层都可以
直接 import 它而不引入循环依赖。名字沿用历史名 `_file_lock`：路由与既有打桩面都按这个
名字取用，改名会静默破坏 `web.app.<名字>` 的兼容面。
"""
import threading

#: 账号 / 用户读-改-写序列的进程内互斥锁（RLock：写操作内嵌调用可重入）。
#: 只护住"读→检查→写"这一段，解密、哈希等 CPU 密集动作刻意留在锁外。
_file_lock = threading.RLock()
