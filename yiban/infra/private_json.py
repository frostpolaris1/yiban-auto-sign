# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""状态 JSON 的**私有写单通道**：tmp 创建即 0600，终文件模式不押在进程 umask 上。

状态文件（按日状态 / 熔断 / 告警额度 / 账本 / 节流 / 心跳 / 调度标记）可能含明
手机号或本机部署节律；内置 `open()` 建的 tmp 经 `os.replace` 后**继承创建时模式**，
"创建即 0600"的契约只有走本模块才成立——"补 chmod"追不上崩溃残留的 tmp，也劣于
"所有站点只走一个通道"。systemd unit 的 `UMask=` 与入口 `os.umask(0o077)` 只是
纵深，不再是任何站点模式的唯一来源。

**唯一临时名** `<path>.tmp<pid>-<线程id>`：跨进程按 pid 隔离（cron 主轮 / 手动
`--only` / 探针 / web 常驻可能同 pid 空间接力），进程内按线程 id 隔离（web 多线程
在文件锁之外还可能有旁路写者）。残留半成品由 `state_gc` 按 `.tmp` 子串 + mtime
（≤1 天）清扫；本模块在写失败时尽力清掉自己的 tmp 再原样抛出——调用方的
except 分支只需决定"写失败怎么办"，不必各自持有临时名。

**归属**：`yiban.infra`（无业务依赖的横切层），`engine/state_io.py` 的
`_write_private_json` 是本模块同名入口的既有锚点。

**复用**：所有状态文件写盘（state_io / alerts / runner / cred_state / notify
ledger / probe / 容器调度器标记与心跳）；`.env` 的私有写另有
`infra/env_io._atomic_replace_env`（多 fsync 与行级语义，属不同契约，不并轨）。
"""
import contextlib
import json
import os
import threading


def write_private_json(path, payload, ensure_dir=True):
    """原子写 JSON、**创建即 0600**。失败清掉本方 tmp 后原样抛出 OSError。

    `ensure_dir=False`：跳过建目录——旁路观测件（容器心跳）不得把已清理的目录
    "复活"，目录不存在时让它自然失败在 `os.open` 上。
    """
    if ensure_dir:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}-{threading.get_ident()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        # 半成品不该留给读者（替换失败路径上终文件仍是旧内容），清 tmp 是尽力而为
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
