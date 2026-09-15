# SPDX-License-Identifier: AGPL-3.0-only
"""共享 .env 写锁：跨进程文件锁 + 进程内 per-path RLock。

**低层实现已统一到 `locks.file_lock`**（POSIX `fcntl.flock` / Windows `msvcrt.locking`，
两条降级路径均告警留痕）。本模块只保留 .env 特有的语义：路径按 `abspath` 归一，
锁粒度是"整个 .env 文件"。

所有 .env 的读-改-写替换路径都应通过 `env_write_lock(env_path)` 进入，
避免 web / 密钥轮换等多进程并发时互相覆盖（后到者胜 → 先入库的密文永久不可解；
丢 `YIBAN_GLOBAL_PAUSE` 等安全开关）。
"""
import os

from locks import file_lock


def env_write_lock(env_path):
    """获取 .env 写锁（contextmanager）。

    同线程可重入：嵌套调用直接放行，不重复获取进程锁或文件锁。
    """
    return file_lock(os.path.abspath(env_path))
