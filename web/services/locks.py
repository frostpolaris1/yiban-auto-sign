# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""web 服务层共用的进程内锁（**唯一定义点**：勿在别处再建一把同名语义的锁）。

**功能**
持有 `_file_lock`——账号 / 用户等 SQLite「读-改-写」序列的进程内互斥锁（RLock，
可重入：写操作内再取一次不会自锁）；`_rate_lock`——限速 / 失败计数 dict 的进程内
读改写锁（普通 Lock：scrypt 校验等慢动作刻意留在锁外，避免长时间阻塞其他请求）。

`_file_lock` 另带一条纪律的执行点：持锁期间不得发生网络 I/O（SMTP、Webhook 都算）。
`run_after_file_lock(fn, *args, **kwargs)` 是这条纪律的**唯一汇合点**。调用点在锁内
登记动作，动作在**最外层 `with` 退出、锁已释放**之后执行。实参在登记那一刻求值，
所以"发什么内容"仍在锁内定稿，只有"发"这个网络动作被挪出锁。

**归属**
原 `web/app.py` 的模块级锁对象，唯一真源在本模块；`web/app.py` 以
`from web.services.locks import _file_lock, _rate_lock` 再导出，故 `web.app.<锁>`
与 `m.<锁>`（路由）拿到的是**同一个对象**。跨进程/跨 worker 的一致性不靠它，
而靠 SQLite 事务与 UNIQUE 约束。

**复用**
路由（`web/routes/*.py` 经 `m._file_lock` / `m._rate_lock`）、app.py 的账号读族
（`load_accounts` / `load_accounts_raw` / `load_users`）与安全域
（`web/security.py` 的 `_bump_window_count` / `_bump_login_failure` / 账号校验配额与
冷却）以及后续按域迁出的服务模块共用这两把锁；各模块自建一把会让"同进程内互斥"
静默失效——`_rate_lock` 上尤其成立：同一个计数 dict 被登录、改密、注销、恢复与
门禁多条路径读写，锁不是同一把就等于没有互斥。

**通信**
本模块不导入任何业务模块（也不反向导入 `web.app`），只依赖标准库；因此任何层都可以
直接 import 它而不引入循环依赖。名字沿用历史名 `_file_lock` / `_rate_lock`：路由与既有
打桩面都按这两个名字取用，改名会静默破坏 `web.app.<名字>` 的兼容面。
`run_after_file_lock` 同样由 `web/app.py` 再导出，路由写 `m.run_after_file_lock(...)`。

**为什么汇合点绑在锁上、不绑在请求上**
登记与汇合按**当前线程**的持锁层数判定。RLock 可重入：嵌套 `with` 时内层退出把层数
减到 0 才算最外层，因此登记表恰好执行一次——不重发、不漏发。绑定请求终点做不到这一
点：`web/services/capacity.py` 的容量去重旗必须在锁内落定，否则两个请求会各登记一封、
出锁后双发。锁释放先于执行：登记动作运行时本线程已不再持锁。
"""
import logging
import threading

logger = logging.getLogger("web")

#: RLock 的类型（C 实现；纯 Python 回退实现同样取得到），供 `_FileLock` 继承。
#: 必须是**实例的类型**而不是 `threading.RLock`——后者是工厂函数，不能被继承。
_RLockBase = type(threading.RLock())


class _ThreadState(threading.local):
    """每线程一份：当前持锁层数，与出锁后要执行的动作清单。"""

    def __init__(self):
        self.depth = 0
        self.after_lock = []


_state = _ThreadState()


class _FileLock(_RLockBase):
    """全局账号/用户读改写锁（RLock 子类）+ 网络动作的唯一汇合点。

    `__exit__` 的顺序是有意的：先减层数并摘走登记表，再释放锁，最后执行登记的
    动作。动作执行时锁已放开，别的读者拿得到它（工单 ba-p05-01 的判据）。
    登记表先摘后执行：动作里若再取锁、再登记，也不会被这一轮重复执行。
    """

    def __enter__(self):
        entered = super().__enter__()
        _state.depth += 1
        return entered

    def __exit__(self, exc_type, exc_value, tb):
        _state.depth = max(0, _state.depth - 1)
        outermost = _state.depth == 0
        pending = []
        if outermost:
            pending = _state.after_lock
            _state.after_lock = []
        released = super().__exit__(exc_type, exc_value, tb)
        for fn, args, kwargs in pending:
            try:
                fn(*args, **kwargs)
            except Exception as e:
                # 单条登记失败不带走其余登记，也不许盖掉 with 体本身正在传播的异常
                # （那会让一次发信缺陷伪装成别的 500）
                logger.warning("出锁后执行登记动作失败（业务已落盘，仅本条未生效）: %s",
                               type(e).__name__)
        return released


#: 账号 / 用户读-改-写序列的进程内互斥锁（RLock：写操作内嵌调用可重入）。
#: 只护住"读→检查→写"这一段，解密、哈希等 CPU 密集动作刻意留在锁外；
#: 网络 I/O 一律经 `run_after_file_lock` 登记后执行（见模块头）。
_file_lock = _FileLock()

#: 限速 / 失败计数 dict 的进程内读改写锁（H7：单 worker + 锁内原子更新）。
#: 普通 Lock 而非 RLock：持锁期间只做 dict 读写，scrypt 校验等慢动作在锁外；
#: 各表在锁内先 trim 再计数，`_ip_store_trim` 自身不再取锁（可重入会掩盖死锁）。
_rate_lock = threading.Lock()


def run_after_file_lock(fn, *args, **kwargs):
    """执行 `fn(*args, **kwargs)`：当前线程持 `_file_lock` 时排到出锁后，否则立即执行。

    行为口径：
    - 实参在本调用点求值（锁内定稿内容），只有网络动作的执行时刻被推迟到最外层出锁；
    - 立即执行时返回 `fn` 的返回值；登记时返回 None（登记路径拿不到结果——锁内的
      发信落点今天全部丢弃返回值：15 处无一处使用）；
    - 出锁后执行时抛出的异常只记一行 warning：业务已落盘，一封通知的发送异常
      不得把已完成的操作改写成 500，也不得吞掉同批其余通知（工单口径：原本会发的
      一封都不许不发）。
    """
    if _state.depth > 0:
        _state.after_lock.append((fn, args, kwargs))
        return None
    return fn(*args, **kwargs)
