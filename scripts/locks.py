# SPDX-License-Identifier: AGPL-3.0-only
"""跨进程文件锁原语（**唯一实现**，勿再各写一份）。

本项目此前有四份各自为政的锁，Windows 行为还不一致：

| 位置 | POSIX | Windows |
|------|-------|---------|
| `env_lock._acquire_file_lock` | `fcntl.flock` | `msvcrt.locking`（**唯一有真锁的**） |
| `signin._state_file_lock` | `fcntl.flock` | **no-op 且无告警** → 5 处状态文件读-改-写失去原子性 |
| `signin._FlockFileHandler` | `fcntl.flock` | no-op（日志行交错，观感问题） |
| `notify._state_file_lock` | `fcntl.flock` | **no-op 且无告警** → 账本/节流跨进程互斥失效 |

统一到本模块后：POSIX 用 `fcntl.flock`，Windows 用 `msvcrt.locking`；**两者都不可用时
明确告警后降级为进程内锁，绝不静默**（静默降级意味着并发读-改-写互相覆盖且无人察觉）。

**不引入第三方依赖**：`portalocker` 想解决的问题（Windows 有真锁）已由本实现覆盖，
而本项目对运行时依赖极克制（`requirements.lock` 手工维护、镜像按 lock 安装）。

实现要点：
- 锁文件独立为 `<key>.lock`，**不与被保护文件本身的读写句柄混用**；
- 每路径一把进程内 `RLock`，保证同线程重入不阻塞（且重入时不再加文件锁——
  同一进程内对同一文件二次 `flock` 的语义不可依赖）；
- 锁文件以 0600 创建，路径按 `abspath` 归一，避免同一文件不同写法产生两把锁。
"""
import contextlib
import logging
import os
import threading

logger = logging.getLogger("yiban.locks")

# per-path 进程内 RLock：键为绝对路径
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
# 当前线程已持有的路径集合：同线程重入时跳过重复加锁/加文件锁
_HELD = threading.local()


def lock_kind():
    """当前平台可用的文件锁后端："posix" / "win" / None（None 表示只能进程内互斥）。

    供诊断与测试使用——**不返回 None 即表示跨进程互斥真实生效**。
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


def _acquire_file_lock(key):
    """跨进程文件锁，返回 (kind, fd) 句柄；失败返回 None（退化为进程内锁）。

    每条降级路径记一次 warning：降级意味着跨进程失去互斥，读-改-写并发可互相覆盖
    （如丢安全开关、丢账本额度），必须留痕可排查。
    """
    try:
        fd = os.open(key + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        logger.warning("文件锁打开失败（%s: %s），退化为进程内锁，跨进程互斥失效: %s.lock",
                       type(e).__name__, e, key)
        return None
    try:
        import fcntl
    except ImportError:
        try:
            import msvcrt
        except ImportError:
            logger.warning("fcntl 与 msvcrt 均不可用，文件锁退化为进程内锁，跨进程互斥失效: %s", key)
            os.close(fd)
            return None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        except OSError as e:
            logger.warning("msvcrt 区域锁加锁失败（%s: %s），退化为进程内锁: %s",
                           type(e).__name__, e, key)
            os.close(fd)
            return None
        return ("win", fd)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as e:
        logger.warning("flock 加锁失败（%s: %s），退化为进程内锁: %s",
                       type(e).__name__, e, key)
        os.close(fd)
        return None
    return ("posix", fd)


def _release_file_lock(handle):
    if not handle:
        return
    kind, fd = handle
    try:
        if kind == "win":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except (OSError, ValueError):
        pass
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


@contextlib.contextmanager
def file_lock(key):
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
        handle = _acquire_file_lock(path)
        try:
            yield
        finally:
            _release_file_lock(handle)
            held.remove(path)
