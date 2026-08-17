# SPDX-License-Identifier: AGPL-3.0-only
"""共享 .env 写锁：跨进程 fcntl.flock + 进程内 per-path RLock。

- POSIX：使用 `fcntl.flock(env_path + ".lock")` 做跨进程互斥；
  同一进程内再用 per-path RLock 保证同线程重入不阻塞。
- Windows：无 fcntl 时退化为进程内 per-path RLock（本地开发足够）。

所有 .env 的读-改-写替换路径都应通过 `env_write_lock(env_path)` 进入，
避免 web / tui / 密钥生成多进程并发时互相覆盖。
"""
import contextlib
import os
import threading

# per-path 进程内 RLock：键为绝对路径，避免同一文件不同写法产生两把锁
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
# 当前线程已持有的路径集合：用于同线程重入时跳过重复加锁/加 flock
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


@contextlib.contextmanager
def env_write_lock(env_path):
    """获取 .env 写锁（contextmanager）。

    同线程可重入：嵌套调用直接放行，不重复获取进程锁或 flock。
    """
    key = os.path.abspath(env_path)
    held = _held_paths()
    if key in held:
        yield
        return

    lock = _get_rlock(key)
    with lock:
        held.add(key)
        try:
            try:
                import fcntl
            except ImportError:
                # Windows / 无 fcntl 环境：进程内 per-path RLock 已足够
                yield
                return

            fd = open(key + ".lock", "a+")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                fd.close()
        finally:
            held.remove(key)
