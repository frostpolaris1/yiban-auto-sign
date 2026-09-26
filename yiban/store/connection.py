# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""连接层地基：SQLite 连接单例、进程内互斥锁、库/密钥来源路径。

本模块是下列名字的**唯一定义点**（连接状态的真身在此；`yiban/store/db.py` 再导出这些
名字，既有调用方与 `scripts/db.py` 兼容壳照旧用 `db.xxx`）：

- `_conn`：模块级单例连接（`db.init_db` 建、调用方自行关闭后置空）
- `_conn_lock`：进程内 RLock，所有读写串行化；定义后**永不重绑**
- `_db_file` / `_env_file`：最近一次 `init_db(...)` 的库路径 / .env 路径
- `DB_DEFAULT`：库路径默认值（`YIBAN_DB_FILE` 或 `"yiban.db"`）
- `get_conn()` / `is_initialized()` / `current_db_file()` / `pool_db_declared()`

**为什么 `init_db` 不在这里**：`tests/test_store_db_move.py` 钉住"真正的 `init_db` 定义
只能在 `yiban/store/db.py`"（建连与建表/迁移同属启动序列，还要与冻结的历史迁移函数
共存）。故本模块对外只多一组**显式读写 API**（`current`/`set_conn`/`reset_conn`/
`set_db_file`/`set_env_file`），由 `db.init_db` 逐分支调用；迁移异常路径用
`reset_conn()` 清空单例（连接由调用方关闭），与正常路径共用同一套 API。

**读写纪律**：`db` 层经本模块访问这几个量（`_connection._db_file` 等）；`db._conn` /
`db._db_file` / `db._env_file` 的读取与**写入**都由 `yiban/store/db.py` 的模块级转发落到
本模块（全仓 190+ 处测试收尾 `db._conn = None` 依赖这一点，否则它们只会写在一份陈旧
副本上、真连接关不掉——详见 db.py 里 `__getattr__` 与 `_StateForwardingModule` 的说明）。
"""
import os
import sqlite3
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


def current_db_file():
    """当前单例连接**实际**指向的库文件（`PRAGMA database_list` 的 main）；取不到 → ""。

    与 `_db_file` 的分工：`_db_file` 是"最近一次 `init_db` 声明的路径"，而 `init_db`
    在单例连接已存在时会刷新 `_db_file` 却直接复用旧连接——此时两者不一致。凡"必须
    写进目标库"的调用（如清库留痕）都要按**实际连接**判定，否则审计会落到另一个库。
    """
    if _conn is None:
        return ""
    try:
        for row in _conn.execute("PRAGMA database_list"):
            if row[1] == "main":
                return row[2] or ""
    except sqlite3.Error:
        return ""
    return ""


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


#: "部署是否声明了领取池库路径"的解析结果缓存：键 = 解析出的路径串（空串=未声明）。
#: 为什么要缓存：`round._claim` 每个账号每次尝试都会问一次，而解析要读 .env；缓存使
#: "同一条配置每次问一遍"变成一次。键取实际值，故 .env 改路径后下一次即重算。
_pool_declared_cache = {}


def pool_db_declared(env=None, env_file=None):
    """部署是否**声明**了领取池库路径（`YIBAN_DB_FILE` 在进程环境或 .env 里非空）。

    执行侧要区分"部署未配库（纯状态文件形态，照旧放行）"与"配了库但当前不可用
    （必须拒跑——放行等于让两个执行体同时登录同一账号）"。两种形态的现场都是
    `is_initialized()` 为假，能分开它们的只有**部署声明的路径**这一条事实。

    为什么判据不是别的：
    - "磁盘上有没有库文件"会被开发机/旧部署留在工作目录里的 `yiban.db` 误判成池部署
      （默认路径恰是 `yiban.db`），把纯状态文件部署打成拒跑；
    - "本进程曾连上过库"分不开"连接被人为关闭"与"库真的不可用"，而后者才需要拒跑。

    只做一次只读解析（**不传 default**，故未声明时得到空串），不建连接、不建库、不建表。
    """
    from yiban.infra import env_io
    path = env_io.resolve_path("YIBAN_DB_FILE", "", env=env, env_file=env_file)
    if path not in _pool_declared_cache:
        _pool_declared_cache[path] = bool(path)
    return _pool_declared_cache[path]
