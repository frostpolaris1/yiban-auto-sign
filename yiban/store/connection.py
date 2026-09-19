# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""连接层地基：SQLite 连接单例、进程内互斥锁、库/密钥来源路径。

本模块是下列名字的**唯一定义点**（2026-09-19 db.py 按域拆分的第一刀；此前它们与
`init_db` 一起挤在 4400 余行的 `yiban/store/db.py` 顶部）：

- `_conn`：模块级单例连接（`db.init_db` 建、调用方自行关闭后置空）
- `_conn_lock`：进程内 RLock，所有读写串行化；定义后**永不重绑**
- `_db_file` / `_env_file`：最近一次 `init_db(...)` 的库路径 / .env 路径
- `DB_DEFAULT`：库路径默认值（`YIBAN_DB_FILE` 或 `"yiban.db"`）
- `get_conn()` / `is_initialized()`

**为什么 `init_db` 不在这里**：`tests/test_store_db_move.py` 钉住"真正的 `init_db` 定义
只能在 `yiban/store/db.py`"（建连与建表/迁移同属启动序列，还要与冻结的历史迁移函数
共存）。故本模块对外只多一组**显式读写 API**（`current`/`set_conn`/`reset_conn`/
`set_db_file`/`set_env_file`），由 `db.init_db` 逐分支调用——拆分前后每条分支语义等价，
包括迁移异常路径的 `reset_conn()`（对应原先的 `_conn = None`）。

**读写纪律**：`db` 层经本模块访问这几个量（`_connection._db_file` 等）；`db._conn` /
`db._db_file` / `db._env_file` 的读取与**写入**都由 `yiban/store/db.py` 的模块级转发落到
本模块（全仓 190+ 处测试收尾 `db._conn = None` 依赖这一点，否则它们只会写在一份陈旧
副本上、真连接关不掉——详见 db.py 里 `__getattr__` 与 `_StateForwardingModule` 的说明）。
"""
import os
import threading

# 模块级共享（web 通过环境变量注入路径后调用 init_db）
DB_DEFAULT = os.environ.get("YIBAN_DB_FILE", "yiban.db")

_conn = None
# RLock：所有读写操作统一串行化（SQLite 连接非线程安全，多线程并发裸 execute
# 会触发 "cannot start a transaction" / InterfaceError misuse——2026-08-15 本地并发验证暴露）
_conn_lock = threading.RLock()
_db_file = DB_DEFAULT
# .env 路径（加密密钥来源）：None = 未显式指定，由 _resolve_key_env_file 按
# YIBAN_ENV_FILE → 当前工作目录 ".env" 回落（此时若回落值来自 cwd
# 且文件不存在，生成新密钥会被 _assert_key_source_certain 拒绝，避免游离密钥）
_env_file = None


def current():
    """当前连接；未初始化时为 None。**不触发**隐式初始化（与 is_initialized 同口径）。"""
    return _conn


def get_conn():
    if _conn is None:
        # 函数内延迟导入：依赖方向单向（db → connection），本模块只在确需初始化时
        # 反向取一次 db.init_db。按**属性**取而不是模块级 from-import：既有的
        # `mock.patch.object(db, "init_db", …)`（如 test_scheduler_gate 的 db_export
        # 用例）必须在隐式初始化路径上仍然生效。
        from yiban.store import db as _db
        _db.init_db()
    return _conn


def is_initialized():
    """db 层是否已显式初始化（不触发隐式 init_db）。

    供 signin 判断会话缓存可用性：环境变量账号模式（CI 等）未初始化 db，
    不启用缓存——避免 get_conn 隐式 init 在工作目录创建空库。
    """
    return _conn is not None


def set_conn(conn):
    """登记单例连接（`db.init_db` 建连后立即调用，早于建表/迁移）。"""
    global _conn
    _conn = conn


def reset_conn():
    """清空单例连接（仅初始化失败路径；正常关闭连接由调用方负责）。"""
    global _conn
    _conn = None


def set_db_file(path):
    """刷新当前库路径（`init_db` 入口**无条件**调用，即使连接已存在）。"""
    global _db_file
    _db_file = path


def set_env_file(path):
    """刷新 .env 路径（`init_db` 入口**无条件**调用，即使连接已存在）。"""
    global _env_file
    _env_file = path
