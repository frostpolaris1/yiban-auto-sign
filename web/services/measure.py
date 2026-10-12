# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""现场实测的状态文件与冷却判定（单账号耗时实测端点专用）。

**功能**
实测冷却状态文件的路径 `_measure_state_path`、读取 `_read_measure_state`、原子写入
`_write_measure_state`、距下次可实测的剩余秒数 `_measure_cooldown_remaining`，以及
可实测账号的挑选 `_pick_measure_account`。

**归属**
原 `web/app.py` 的模块级实测辅助，唯一真源在本模块；`web/app.py` 只保留名字面与转发，
把它自己持有、而本模块需要的模块级名字——状态目录 `STATE_DIR`、原子落盘 `_atomic_write`
——在调用时刻现取后注入。实测冷却时长 `MEASURE_COOLDOWN_SEC` 与端点的其余判据（仅主管理员、
签到窗口内拒绝、跨进程持锁）留在 `web.app` 与路由：前者是路由经 `m.*` 取用的配置常量，
后两者是请求上下文里的权限与顺序约束。

**复用**
"账号是否可实测"只有本模块一处判据（`_pick_measure_account`，与容量口径同源，都经
`yiban.store.db.account_signs_in`）；状态文件的读写与"缺失/损坏按从未实测"的容错链也只有
一处，避免实测端点与展示各自解析同一份状态。跨进程冷却串行沿用 `signin._state_file_lock`
（由调用方持锁），不自建第二套。

**通信**
本模块不反向导入 `web.app`（本仓测试以别名加载 `app.py`，普通 import 会再执行一份副本
模块）。状态目录与原子落盘都作为显式参数接收：它们在 `web.app` 上是会被测试改写、也会随
`--config` 变化的模块级名字（既有测试直接赋值 `web.app.STATE_DIR`、又在 `web.app` 上打桩
`_atomic_write`），本模块另持一份绑定会让这些改写静默失效。
"""

import json
import logging
import math
import os
from datetime import datetime

from yiban import clock
from yiban.store import db

# 与 web.app 同名的日志通道：实测状态文件不可写的告警落回既有通道，便于运维沿用同一处过滤
logger = logging.getLogger("web")

#: 实测冷却状态文件（单条记录，固定名，故不随时间增长）。落状态目录而非进程内存：
#: web 重启后冷却仍有效。
MEASURE_STATE_FILE = "capacity-measure.json"


def _measure_state_path(state_dir):
    """实测冷却状态文件的路径。

    状态目录由调用方传入（`web.app` 的 `STATE_DIR`）——它会被测试赋值改写，
    也会随 `--config` 变化。
    """
    return os.path.join(state_dir, MEASURE_STATE_FILE)


def _read_measure_state(path):
    """读实测状态（缺失/损坏 → `{}`：按"从未实测"处理，不阻断）。"""
    try:
        # utf-8-sig：容错 Windows 记事本/工具写入的 BOM（与其余状态文件同口径）
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_measure_state(path, payload, atomic_write):
    """原子写实测状态（创建即 0600）。失败只告警——状态文件不该阻断实测本身。

    原子落盘实现由调用方传入（`web.app` 的 `_atomic_write`）：它既是"每一次落盘"的
    观测点（测试在此打桩），也负责权限位与替换重试，本模块另持绑定会让打桩静默失效。
    """
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        atomic_write(path, json.dumps(payload, ensure_ascii=False), chmod_priv=True)
    except OSError:
        logger.warning("实测状态文件不可写（本次实测结果仍会返回，但冷却可能不生效）")


def _measure_cooldown_remaining(state, cooldown_sec, now=None):
    """距下次可实测的剩余**整秒**（0 = 现在可以实测）。`state` 是已解析的状态文件。

    判据只认状态文件里的时刻，**不按会话/IP**：冷却必须是全局的，否则多管理员叠加
    点击就绕开了限频（每次点击都会真实登录一个账号）。时刻读不出（旧文件/损坏）
    按"可以实测"处理——限频是为了省请求，不该因为文件坏了就让功能永久不可用。
    """
    if cooldown_sec <= 0:
        return 0
    try:
        at = datetime.strptime(str(state.get("at") or ""), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return 0
    remaining = cooldown_sec - ((now or clock.now()) - at).total_seconds()
    return math.ceil(remaining) if remaining > 0 else 0


def _pick_measure_account(accounts, phone=None):
    """挑一个可实测的账号：**不删、审核通过、未被用户暂停**（与容量口径同源）。

    实测会真实登录易班，故不允许拿已删/未过审/用户自暂停的账号去跑——它们按设计
    不该产生任何易班请求。`phone` 非空时只在该号码上找，找不到返回 None。

    ⚠ **默认（不带 `phone`）取的是列表里第一个满足条件的账号**，通常是某个**真实用户**
    的账号（很可能不是管理员自己的），而且账号列表顺序稳定 ⇒ **每次实测默认都是同一个账号**。
    也就是说：这个功能是"拿一位用户的账号替全站做一次真实登录"，那位用户并不知情。
    这是把风控暴露与不便转嫁给单点，所以本端点必须保持：仅主管理员、全局冷却、
    窗口内拒绝，且页面文案要如实说明"会用一个真实账号访问易班一次"。
    """
    wanted = str(phone or "").strip()
    for acc in accounts:
        if not db.account_signs_in(acc) or acc.get("user_paused"):
            continue
        if wanted and acc.get("phone") != wanted:
            continue
        return acc
    return None
