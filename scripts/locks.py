# SPDX-License-Identifier: AGPL-3.0-only
"""跨进程文件锁原语（**唯一实现**，勿再各写一份）。

本项目此前有四份各自为政的锁，Windows 行为还不一致：

| 位置 | POSIX | Windows |
|------|-------|---------|
| `env_lock._acquire_file_lock` | `fcntl.flock` | `msvcrt.locking`（唯一有真锁的） |
| `signin._state_file_lock` | `fcntl.flock` | **no-op 且无告警** → 5 处状态文件读-改-写失去原子性 |
| `yiban.logging_ext.FlockFileHandler` | `fcntl.flock` | no-op（日志行交错，观感问题） |
| `notify._state_file_lock` | `fcntl.flock` | **no-op 且无告警** → 账本/节流跨进程互斥失效 |

统一到本模块；底层实现交给 `portalocker`（BSD-3-Clause，零强制依赖，纯 Python）。
**为什么用库而不是自己写**：自写版本一次尝试失败就降级为进程内锁——Windows 上
`msvcrt.LK_LOCK` 抢不到锁约 10 秒后放弃，于是"降级"在多执行体竞争下会真的发生，
跨进程互斥静默失效。portalocker 的 `Lock` 带 `timeout`/`check_interval` **重试循环**，
会等到超时才报错；它还修过一类自写很难发现的缺陷（锁的字节区间从任意文件位置起算，
导致两个持有者锁的不是同一区间、互斥实际不成立），并提供共享锁与完备的异常分类。

本模块保留项目既有的对外契约（下表的语义**不变**，只是不再依赖平台实现是否完备）：
- 锁文件独立为 `<key>.lock`，**不与被保护文件本身的读写句柄混用**；
- 每路径一把进程内 `RLock`，同线程可重入（重入时不再加文件锁——同一进程内对同一文件
  二次加锁的语义不可依赖）；
- 锁文件以 0600 创建，路径按 `abspath` 归一，避免同一文件不同写法产生两把锁；
- **拿不到锁不阻断业务**：重试到 `_LOCK_TIMEOUT` 后仍失败，则告警留痕并降级为进程内锁
  （降级意味着跨进程互斥失效，必须可见；静默降级是既往的缺陷根源）。
"""
import contextlib
import logging
import os
import threading

import portalocker

logger = logging.getLogger("yiban.locks")

# 拿锁的重试总时长（秒）。portalocker 会以 check_interval 为间隔重试到此刻。
# 取值权衡：状态文件/账本的临界区都是毫秒级，正常竞争远小于此；给足 30s 是为了
# 覆盖"另一进程正在做长事务"的场景（如批量写库），避免不必要的降级。
_LOCK_TIMEOUT = 30.0

# per-path 进程内 RLock：键为绝对路径
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
# 当前线程已持有的路径集合：同线程重入时跳过重复加锁/加文件锁
_HELD = threading.local()


def lock_kind():
    """当前平台的文件锁后端描述（诊断/测试用）。

    返回 "posix" 或 "win"；**返回 None 表示本平台无法做跨进程文件锁**（portalocker
    在无 fcntl/msvcrt 的平台上锁会失败）。不返回 None 即表示跨进程互斥真实生效。
    """
    try:
        import fcntl  # noqa: F401
        return "posix"
    except ImportError:
        pass
    try:
        import msvcrt  # noqa: F401
        return "win"
    except ImportError:
        return None


def _held_paths():
    paths = getattr(_HELD, "paths", None)
    if paths is None:
        paths = set()
        _HELD.paths = paths
    return paths


def _get_rlock(path):
    with _LOCKS_GUARD:
        lock = _LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[path] = lock
        return lock


def _mk_lock_file_0600(path, flags):
    """锁文件的 open 钩子：创建即 0600（锁文件名可能泄露被保护对象的路径信息）。

    portalocker 的 `Lock` 自己开文件（收路径而非句柄），故用 `opener` 注入权限位；
    已存在的锁文件保持原权限，不做收紧。
    """
    return os.open(path, flags, 0o600)


def _acquire(path, timeout):
    """取跨进程独占锁，返回句柄；重试超时后告警并返回 None（降级为进程内锁）。

    降级必须留痕：跨进程互斥失效会让并发读-改-写互相覆盖（丢安全开关、丢账本额度），
    这是既往静默降级踩过的坑。
    """
    try:
        lock = portalocker.Lock(
            path + ".lock",
            mode="a+b",
            timeout=timeout,
            check_interval=0.05,
            # **非阻塞 + timeout** 而非阻塞标志：portalocker 在阻塞模式下会忽略 timeout
            # （内核调用自己等），而 Windows 的阻塞锁 msvcrt.LK_LOCK 约 10 秒就放弃——
            # 那样"重试到超时"的收益在 Windows 上根本拿不到。非阻塞 + timeout 让重试
            # 循环在两个平台上都真实生效（这也是 portalocker 默认 flags 的形态）。
            flags=portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING,
            opener=_mk_lock_file_0600,
        )
        fh = lock.acquire()  # 返回已打开并加锁的文件句柄
    except Exception as e:  # 平台无锁后端 / 重试超时 / 文件系统不支持
        logger.warning("文件锁获取失败（%s: %s），退化为进程内锁，跨进程互斥失效: %s.lock",
                       type(e).__name__, e, path)
        return None
    return (fh, lock)


def _release(handle):
    if not handle:
        return
    fh, lock = handle
    with contextlib.suppress(Exception):
        lock.release()  # 解锁并关闭句柄（portalocker 的 Lock.release 语义）
    with contextlib.suppress(Exception):
        if not fh.closed:
            fh.close()


@contextlib.contextmanager
def file_lock(key, timeout=None):
    """获取跨进程独占文件锁（contextmanager）。

    `key` 是被保护对象的路径（不含 `.lock` 后缀；本函数自行拼 `<key>.lock`，路径按
    abspath 归一）。**同线程可重入**：嵌套调用直接放行，不重复获取进程锁或文件锁。
    """
    path = os.path.abspath(key)
    held = _held_paths()
    if path in held:
        yield
        return

    lock = _get_rlock(path)
    with lock:
        held.add(path)
        handle = _acquire(path, _LOCK_TIMEOUT if timeout is None else timeout)
        try:
            yield
        finally:
            _release(handle)
            held.remove(path)
