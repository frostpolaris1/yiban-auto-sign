# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""「立即执行一轮」请求记录的状态文件与防抖判定（A5 端点专用）。

**功能**
请求记录状态文件的路径 `_round_request_path`、读取 `_read_round_request`、原子写入
`_write_round_request`、距下次可请求的剩余秒数 `_round_request_cooldown_remaining`。

**归属**
原 `web/app.py` 的模块级辅助（与实测族的 `web/services/measure.py` 同形），唯一真源在
本模块；`web/app.py` 只保留名字面与转发，把它自己持有、而本模块需要的模块级名字——
状态目录 `STATE_DIR`、原子落盘 `_atomic_write`——在调用时刻现取后注入。

**复用**
跨进程串行沿用 `signin._state_file_lock`（由调用方持锁），与实测冷却同一把机制，
不自建第二套；状态文件的"缺失/损坏按从未请求过"容错链与 `_measure_cooldown_remaining`
逐字同源（同一个 `datetime.strptime` + 整秒向上取整改动）。

**通信**
本模块不反向导入 `web.app`（本仓测试以别名加载 `app.py`，普通 import 会再执行一份副本
模块）。状态目录与原子落盘都作为显式参数接收：`web.app` 上这两个名字既会被测试改写、
也会随 `--config` 变化，本模块另持一份绑定会让那些改写静默失效。

**边界（重要）**
本模块只**登记**一次"执行一轮"的请求，它**不拉起任何进程、不发起任何网络请求**。
真正跑一轮签到属引擎面（`yiban/engine/**`），本批不动引擎，故端点只落记录 + 留审计，
由部署侧调度器读取该记录后拉起。防抖与并发闸因此是"登记层"的闸，不是"引擎在跑"的闸。
"""

import json
import logging
import math
import os
from datetime import datetime

from yiban import clock

# 与 web.app 同名的日志通道：状态文件不可写的告警落回既有通道，便于运维沿用同一处过滤
logger = logging.getLogger("web")

#: 请求记录状态文件（单条记录，固定名，故不随时间增长）。落状态目录而非进程内存：
#: web 重启后防抖窗口仍然有效（否则重启一次就能把防抖清零）。
ROUND_REQUEST_FILE = "round-request.json"


def _round_request_path(state_dir):
    """请求记录状态文件的路径。

    状态目录由调用方传入（`web.app` 的 `STATE_DIR`）——它会被测试赋值改写，
    也会随 `--config` 变化。
    """
    return os.path.join(state_dir, ROUND_REQUEST_FILE)


def _read_round_request(path):
    """读请求记录（缺失/损坏 → `{}`：按"从未请求过"处理，不阻断）。"""
    try:
        # utf-8-sig：容错 Windows 记事本/工具写入的 BOM（与其余状态文件同口径）
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_round_request(path, payload, atomic_write):
    """原子写请求记录（创建即 0600）。失败只告警——记录不该阻断请求本身。

    原子落盘实现由调用方传入（`web.app` 的 `_atomic_write`）：它既是"每一次落盘"的
    观测点（测试在此打桩），也负责权限位与替换重试。
    """
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        atomic_write(path, json.dumps(payload, ensure_ascii=False), chmod_priv=True)
    except OSError:
        logger.warning("执行一轮请求记录不可写（本次请求仍已受理，但防抖可能不生效）")


def _round_request_cooldown_remaining(state, cooldown_sec, now=None):
    """距下次可请求的剩余**整秒**（0 = 现在就可以请求）。`state` 是已解析的状态文件。

    判据只认状态文件里的时刻，**不按会话/IP**：多管理员叠加点击必须共用一个防抖，
    否则一次连点就把"执行一轮"的登记刷成一串。时刻读不出（旧文件/损坏）按"可以请求"
    处理——防抖是为了省掉无意义的重复登记，不该因为文件坏了就让功能永久不可用。
    """
    if cooldown_sec <= 0:
        return 0
    try:
        at = datetime.strptime(str(state.get("at") or ""), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return 0
    remaining = cooldown_sec - ((now or clock.now()) - at).total_seconds()
    return math.ceil(remaining) if remaining > 0 else 0
