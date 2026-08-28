# SPDX-License-Identifier: AGPL-3.0-only
"""共享 .env 写锁：跨进程文件锁 + 进程内 per-path RLock。

- POSIX：fcntl.flock(env_path + ".lock") 跨进程互斥；
- Windows：msvcrt.locking 区域锁（LK_LOCK，约 10 秒内重试）——批次7 P2-3：
  原实现 Windows 完全无跨进程互斥，web 与 signin 同时首启会各自生成不同密钥
  并互相覆盖（os.replace 后到者胜），先入库的密文永久不可解；
  同一进程内再用 per-path RLock 保证同线程重入不阻塞。
- 文件锁获取失败（目录不可写等）退化为进程内锁，不阻断业务。

所有 .env 的读-改-写替换路径都应通过 `env_write_lock(env_path)` 进入，
避免 web / tui / 密钥生成多进程并发时互相覆盖。
"""
import contextlib
import os
import threading

# per-path 进程内 RLock：键为绝对路径，避免同一文件不同写法产生两把锁
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
# 当前线程已持有的路径集合：用于同线程重入时跳过重复加锁/加文件锁
_HELD = threading.local()


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
    """跨进程文件锁，返回 (kind, fd) 句柄；失败返回 None（退化为进程内锁）。"""
    try:
        fd = os.open(key + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        import fcntl
    except ImportError:
        try:
            import msvcrt
        except ImportError:
            os.close(fd)
            return None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        except OSError:
            os.close(fd)
            return None
        return ("win", fd)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
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
def env_write_lock(env_path):
    """获取 .env 写锁（contextmanager）。

    同线程可重入：嵌套调用直接放行，不重复获取进程锁或文件锁。
    """
    key = os.path.abspath(env_path)
    held = _held_paths()
    if key in held:
        yield
        return

    lock = _get_rlock(key)
    with lock:
        held.add(key)
        handle = _acquire_file_lock(key)
        try:
            yield
        finally:
            _release_file_lock(handle)
            held.remove(key)
