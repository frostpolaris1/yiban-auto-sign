# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""schema 版本迁移域：按 PRAGMA user_version 顺序演进的建表、补列与数据修复。

**功能**
- 基线建表 `_create_tables`：accounts / users / audit_logs / time_prefs /
  user_delete_requests 五张表与其索引（幂等 IF NOT EXISTS）；
- 迁移项 `migrate_v1..v20`：每项对应一个已发布、且**不可再修改**的 schema 版本
  （MF-40 修复批次按 brief 明示豁免了该纪律做缺陷修复——v5 崩溃重跑/原子提交/标识符
  转义、v13 转义/守恒、v17-v19 档位与 v20 目录可读性；可观测成功路径逐条保持原样，
  反例测试见 tests/test_migrations_fail_closed.py）；
- 迁移助手 `_table_columns` / `_ensure_column` / `_ensure_index` 与表名白名单
  `_ALLOWED_TABLES`（助手对白名单外的表名直接拒绝，防拼接 SQL 的注入面）；
- 编排 `_run_migrations`：读 user_version、每项包进 BEGIN IMMEDIATE、核心迁移失败抛出阻断
  启动、可选迁移失败或延后只置 blocked 且不提升版本（下次启动重试）。框架持事务期间，
  助手与各 `migrate_*` 体内的提交点一律走 `_commit_if_free`，不提前落盘——"整段迁移
  原子"由此成立（结构守卫 `tests/test_migrations_schema_drift.py::NoBareCommitGuardTest`
  禁止迁移体再出现裸 `conn.commit()`）；`_MIGRATIONS`
  是「版本号 → 名称 → 函数 → 是否核心」的登记表（v17/v18/v19 为核心档：v3 领取路径
  把它们的产物当硬编列名用，缺了整条路径静默拒跑，见登记表注释）；
- 迁移完成记录表 `schema_migrations`：bump 分支按 **INSERT 记录 → `PRAGMA user_version`
  → commit** 的固定顺序在**同一事务**落库（失败/未提升 ⇒ 无记录；顺序不可倒——
  直调路径里迁移体自己已提交过，而 `PRAGMA` 不自开事务，放最前就按 autocommit 抢先
  提升版本，与记录写入之间崩溃即留下"版本已提升、记录没写上"、下次启动被完整性门
  拒启且继承回填救不回）。存量库（记录表上线前升的级）首见时按继承回填。
  链尾 `_verify_migration_integrity` fail-closed 校验：版本已过某迁移却无记录、或
  该档在 `_ARTIFACTS` 登记的产物（表/列）缺失 ⇒ 抛 `MigrationIntegrityError` 点名该
  迁移并拒绝启动（产物核对不分核心/可选档；MF-40：不接受"版本声称过了、产物没落地"
  的库继续跑）；
- `MigrationDeferred`：可选迁移遇到需人工处理的数据时主动延后；
- JSON → SQLite 自动导入 `_maybe_migrate` / `_rename_backup`：库仍为空且 JSON 存在时把
  accounts/users 导入 SQLite（幂等），两个文件都成功后一起改名 `.bak` 保留逃生门。

**归属**
`yiban/store/db.py` 的 `init_db` 启动序列：建连后 `_create_tables` → `_run_migrations`
（`migrate=False` 时整段跳过）→ `_maybe_migrate`（仅在给了 `migrate_from` 时）。迁移历史
是一份**按版本号冻结的时间序列**——已发布的迁移函数不可再改（改了对已升级的库无效，还会
给介于两个版本之间的库制造新的失败路径），故整块按「同一份 schema 演进史」放在一起，
不按版本段再拆。JSON 自动导入是这条启动序列里"把旧文件形态搬进 schema"的一步，与迁移
共用同一个入口编排，故并入本模块。

**复用**
`yiban.store.db` 把本模块全部名字按原样再导出：既有 `db.migrate_v10(...)` /
`db._ensure_column(...)` / `db._create_tables(...)` / `db._maybe_migrate(...)` 调用面不变。
`_MIGRATIONS` 是**可变登记表**（测试用 `db._MIGRATIONS = [...]` 缩窄或替换迁移集），由 db
侧模块类读写转发到本模块——快照式再导出会让缩窄静默失效（编排仍读真表）。
`terminal_task_state` 与 `_JSON_TERMINAL_TO_TASK_STATE` 是「JSON 状态 → 池状态」判定的
唯一定义处，`scripts/ledger_check.py` 的对账判定引用它。

**通信**
迁移函数一律接收调用方传入的 `conn`（事务由 `_run_migrations` 经写事务入口开启），本模块
从不自取连接。跨域调用——写事务入口 `_begin_immediate`、审计链升级回填
`_backfill_audit_hashes`、JSON 导入的进程内写锁 `_conn_lock`——经 `_facade()`
按属性延迟取 `yiban.store.db`：函数内导入避免导入环，按属性取保证 `db.<名字> = 替身`
一类打桩可见。库与密钥来源路径直接读连接模块（`_connection._env_file`；`db._env_file =
path` 的写入由 db 门面转发落到那里），账号域的加密值判定按父提交同形直取
`_accounts._is_encrypted_value`。
"""
import contextlib
import json
import logging
import os
import re
from datetime import timedelta

from yiban import clock
from yiban.infra import account_crypto, env_io
from yiban.masking import mask_phone
from yiban.masking import sanitize_text as _sanitize_text
from yiban.store import accounts as _accounts
from yiban.store import connection as _connection

logger = logging.getLogger("yiban.store.migrations")


def _facade():
    """db 门面（函数内延迟导入，避免与 `yiban.store.db` 形成导入环）。

    打桩可见性见模块说明：必须按**属性**取而不是模块级 from-import，`db.<名字> = 替身`
    才会在函数体里生效。
    """
    from yiban.store import db
    return db


class MigrationDeferred(Exception):
    """迁移暂缓：本次不应用，下次启动重试（用于可选迁移遇到需人工处理的数据）。"""


class MigrationIntegrityError(Exception):
    """迁移完整性校验失败 ⇒ **拒绝启动**（fail-closed，MF-40）。

    抛出即说明 `user_version` 与库的实际 schema 脱节：版本声称已过了某条迁移，但该迁移
    的完成记录缺失、或该档在 `_ARTIFACTS` 登记的产物（表/列）不存在（产物核对不分
    核心/可选档）。典型成因：旧链"可选失败只告警"
    时代留下的半升级库、有人手工拨 PRAGMA、绕过链直接改 schema。让 `try_claim` /
    执行体在这种库上继续跑只会把静默拒跑变成运维事故，故启动即拒、异常文本点名是哪条
    迁移缺了什么。调用面：`yiban.engine.runner` / `workers` 捕获后按退出码 4 退出
    （见 `docs/dev/cli.md` §3）。
    """


# 迁移完成记录表：成功提升与写记录在同一事务（`_run_migrations` 的 bump 分支，
# 先 INSERT 记录、后 PRAGMA user_version，见该分支注释的原因），
# 表本身在整链开跑前建好；记录缺失 ⇒ `MigrationIntegrityError`。
_SCHEMA_MIGRATIONS_TABLE = "schema_migrations"

# 迁移产物登记表（值为 ((表, 列|None=只要表), ...)）。链尾按这张表核对："user_version
# 过了这档 ⇒ 这档的产物还在库里"。缺一个就拒启。
# 为什么这几档必须在册：它们的迁移体里曾有裸 `conn.commit()`（ba-p01-02）。可选档失败
# 只告警、不抛异常，框架照写记录、照提版本——回滚吃掉的 DDL 从此没人看得见。v17/v18/v19
# 是领取/队列路径的硬编引用（缺了整条 v3 路径静默失效），原先就在册。
# 不在册的三档，逐档理由：
# - v14：它的产物是"page_visits / server_metrics 被删掉"。存在性核对说不出"必须没有"；
#   残表无正确性代价，把它变成拒启会逼存量库人工 DROP 才能重启。
# - v20：只补 sign_tasks 的数据行，没有 schema 产物。"补 0 行"是全新部署的常态。
# - v4/v6 里的 page_visits 与 server_metrics（含 v6 补的 user_id）：v14 会删掉它们，
#   登记即自打。
# 第 4 元（核心/可选）只决定**执行失败时**阻断不阻断启动；产物核对不分档，缺了都拒启。
_ARTIFACTS = {
    4: (("sign_events", None),),                          # migrate_v4 建表
    6: (("sign_events", "account_id"),                    # migrate_v6 三处 _ensure_column
        ("sign_events", "dur_sec"),
        ("sign_events", "finished_at")),
    8: (("session_cache", None),                          # migrate_v8 建两张表
        ("app_meta", None)),
    12: (("app_meta", None),),                            # migrate_v12 幂等补建
    15: (("verify_jobs", None),),                         # migrate_v15 建表
    16: (("verify_jobs", "prev_status"),),                # migrate_v16 补列
    17: (("sign_claims", None),),                         # migrate_v17 建表
    18: (("sign_tasks", None), ("egress_state", None)),   # migrate_v18 建两表
    19: (("sign_claims", "epoch"), ("sign_tasks", "epoch")),  # migrate_v19 补两列
    21: (("run_events", None),),                          # migrate_v21 建表
}


def _quote_ident(name):
    """SQLite 双引号标识符转义（表/列名里的 `"` 双写），防 f-string SQL 被打断。"""
    return '"' + str(name).replace('"', '""') + '"'


def _quote_str_literal(text):
    """SQL 单引号字符串参数转义（如 PRAGMA index_info('...') 的名字入参）。"""
    return str(text).replace("'", "''")


# 登记表框架事务标志：`_run_migrations` 在 BEGIN IMMEDIATE 里执行迁移函数时置真。
# 期间每一个提交点（`_ensure_column` / `_ensure_index` 与各 `migrate_*` 体内）**一律
# 不提前落盘**——原子性承诺的兑现点。登记表之外直调（旧调用面/测试直接调迁移函数）时
# 标志为假，提交点照旧自提，行为与历史完全一致。单线程写路径（init_db 全程持 `_conn_lock`）。
_framework_txn = False


def _commit_if_free(conn):
    """迁移体内的提交点：框架持事务时交给框架末尾统一提交，直调时照旧自提。"""
    if not _framework_txn:
        conn.commit()


# ---------------------------------------------------------------------------
# 表结构
# ---------------------------------------------------------------------------
def _create_tables(conn):
    # 不用 executescript——其隐式 COMMIT 会把调用方已开启的事务
    # （_run_migrations 的 BEGIN IMMEDIATE）提前提交，击穿迁移原子性；逐条
    # execute 让 DDL 落在事务内，中途失败可整体回滚。
    conn.execute(
        "CREATE TABLE IF NOT EXISTS accounts ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "sort_order INTEGER NOT NULL, "
        "name TEXT NOT NULL DEFAULT '', "
        "phone TEXT NOT NULL UNIQUE, "
        "password TEXT NOT NULL DEFAULT '', "
        "phone_model TEXT NOT NULL DEFAULT '', "
        "phone_code TEXT NOT NULL DEFAULT '', "
        "owner TEXT NOT NULL DEFAULT 'admin', "
        "status TEXT NOT NULL DEFAULT 'pending', "
        "reject_reason TEXT NOT NULL DEFAULT '', "
        "deleted INTEGER NOT NULL DEFAULT 0, "
        "deleted_at TEXT NOT NULL DEFAULT '', "
        "deleted_by TEXT NOT NULL DEFAULT '', "
        "user_paused INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_owner ON accounts(owner)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_sort ON accounts(sort_order)")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "email TEXT NOT NULL, "
        "password_hash TEXT NOT NULL, "
        "role TEXT NOT NULL DEFAULT 'user', "
        "created_at TEXT NOT NULL DEFAULT '', "
        "pw_version INTEGER NOT NULL DEFAULT 1, "
        "deleted INTEGER NOT NULL DEFAULT 0, "
        "deleted_at TEXT NOT NULL DEFAULT '', "
        "mail_notify INTEGER NOT NULL DEFAULT 1"
        ")"
    )
    # 注意：idx_users_email_live（依赖 users.deleted）由 migrate_v5 创建，
    # 不能放在基线建表里——旧库（0.19.8，users 无 deleted 列）升级时会在
    # 迁移执行前崩溃。

    conn.execute(
        "CREATE TABLE IF NOT EXISTS audit_logs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts TEXT NOT NULL, "
        "username TEXT NOT NULL, "
        "action TEXT NOT NULL, "
        "target TEXT NOT NULL DEFAULT '', "
        "detail TEXT NOT NULL DEFAULT ''"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action_target ON audit_logs(action, target, id)")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS time_prefs ("
        "phone TEXT PRIMARY KEY, "
        "slot_min INTEGER NOT NULL, "
        "updated_at TEXT NOT NULL"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_time_prefs_slot ON time_prefs(slot_min)")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_delete_requests ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "username TEXT NOT NULL, "
        "ip_hash TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_delete_requests_user ON user_delete_requests(username)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_delete_requests_ip ON user_delete_requests(ip_hash)")
    conn.commit()


# ---------------------------------------------------------------------------
# 通用幂等迁移框架
# ---------------------------------------------------------------------------
# 允许操作的表名白名单（防止 f-string SQL 注入）
# page_visits / server_metrics 已由 migrate_v14 删除，条目保留是必需的：
# 冻结的 migrate_v6 仍对它们调用 _ensure_column / _ensure_index，全新库的执行序
# 是 migrate_v4 建表 → migrate_v6 补列 → migrate_v14 删表。迁移只增不改。
# 作用域：只被三个助手查名——`_table_columns` / `_ensure_column`（两者对白名单外的
# 表名直接 raise ValueError）与 `db._table_min_max`（取证留痕的表名参数）。既不
# 约束本模块的建表 DDL，也不许作为"这张表可以随便查"的凭据：其它访问层各按自己的
# 表名白名单/字面量走。故新表若要走上面三个助手，必须在此登记。
_ALLOWED_TABLES = {"accounts", "users", "audit_logs", "time_prefs", "user_delete_requests",
                   "sign_events", "page_visits", "server_metrics", "session_cache",
                   "verify_jobs", "sign_claims", "sign_tasks",
                   "egress_state", "app_meta", "run_events"}


def _table_columns(conn, table):
    """返回表的所有列名（PRAGMA table_info）。标识符转义后入参（MF-40 修法 6）。"""
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"非法表名: {table!r}")
    return {r["name"] for r in conn.execute(
        f"PRAGMA table_info({_quote_ident(table)})").fetchall()}


def _ensure_column(conn, table, column, type_decl):
    """缺列才 ALTER TABLE ADD COLUMN（幂等）。

    type_decl 是**纯类型声明**（如 "TEXT NOT NULL DEFAULT ''"），不含列名——
    本函数自己拼 `ADD COLUMN {column} {type_decl}`。历史实现把列名一并写进了
    type_decl，生成 `deleted_by deleted_by TEXT` 这类重复列名声明（SQLite 宽容
    接受、亲和性碰巧不变，但 schema 可读性差、.dump 会把畸形带进新库）；
    v13 迁移修复存量库，此处加断言防复发（见 migrate_v13）。
    """
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"非法表名: {table!r}")
    # 列名白名单：仅允许字母数字下划线，防止注入
    if not column.isidentifier() or not column.replace("_", "").isalnum():
        raise ValueError(f"非法列名: {column!r}")
    # 防复发：type_decl 不得以列名开头（那正是历史畸形形态）
    if str(type_decl).split()[0].lower() == column.lower():
        raise ValueError(
            f"_ensure_column type_decl 不应重复列名: {column!r} / {type_decl!r}"
        )
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_decl}")
        _commit_if_free(conn)


def _ensure_index(conn, create_sql):
    """按给定 CREATE INDEX / CREATE UNIQUE INDEX 语句幂等创建（依赖 IF NOT EXISTS）。"""
    conn.execute(create_sql)
    _commit_if_free(conn)


def migrate_v1(conn):
    """v1：补齐 accounts.user_paused 列（现状基线迁移）。"""
    _ensure_column(conn, "accounts", "user_paused", "INTEGER NOT NULL DEFAULT 0")


def migrate_v2(conn):
    """v2：为普通用户“每人限 1 账号”创建部分唯一索引（可选/延后）。

    若存在历史重复数据，抛出 MigrationDeferred，不创建索引、不 bump 版本；
    人工清理后下次启动自动重试。
    """
    rows = conn.execute(
        "SELECT owner, COUNT(*) AS cnt FROM accounts "
        "WHERE deleted=0 AND owner NOT IN ('', 'admin') "
        "GROUP BY owner HAVING COUNT(*) > 1"
    ).fetchall()
    if rows:
        dup = ", ".join(f"{r['owner']}({r['cnt']})" for r in rows)
        logger.warning("检测到重复 owner，跳过唯一索引创建（需人工清理后重启重试）: %s", dup)
        raise MigrationDeferred("存在重复 owner，唯一索引延后创建")
    _ensure_index(
        conn,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_owner_live "
        "ON accounts(owner) WHERE deleted=0 AND owner != '' AND owner != 'admin'",
    )


def migrate_v3(conn):
    """v3：审计日志加 prev_hash/hash 列，并对存量数据回填哈希链。

    升级正确性：旧库（`user_version < 3`）的历史审计行在补出两列后 hash 仍为空，
    必须用**当前密钥**把整条链算出来，否则升级后 `verify_audit_chain` 每次都判断链。
    回填本体（单事务、失败整段回滚）见 `_backfill_audit_hashes`；本函数只负责
    "有缺口才补"。
    """
    _ensure_column(conn, "audit_logs", "prev_hash", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "audit_logs", "hash", "TEXT NOT NULL DEFAULT ''")
    empty = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_logs WHERE hash=''"
    ).fetchone()["n"]
    if empty:
        _facade()._backfill_audit_hashes(conn)


def migrate_v4(conn):
    """v4：创建可视化三表（可选迁移，失败只告警不阻断启动）。"""
    # 逐条 execute 替代 executescript（隐式 COMMIT 击穿
    # _run_migrations 的 BEGIN IMMEDIATE，失败时前半段 DDL 已提交无法回滚）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sign_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts TEXT NOT NULL, "
        "phone TEXT NOT NULL, "
        "status TEXT NOT NULL, "
        "message TEXT NOT NULL DEFAULT '', "
        "stage TEXT NOT NULL DEFAULT '', "
        "attempt INTEGER NOT NULL DEFAULT 0, "
        "account_id INTEGER, "
        "dur_sec REAL, "
        "finished_at TEXT"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sign_events_ts ON sign_events(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sign_events_phone ON sign_events(phone)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sign_events_phone_ts ON sign_events(phone, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sign_events_account_ts ON sign_events(account_id, ts)")

    # page_visits / server_metrics 已废弃：由 migrate_v14 删除。本段保留是必需的
    # （已发布迁移不可修改；migrate_v6 还依赖这两张表存在）——见 migrate_v14 说明。
    conn.execute(
        "CREATE TABLE IF NOT EXISTS page_visits ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts TEXT NOT NULL, "
        "role TEXT NOT NULL DEFAULT '', "
        "path TEXT NOT NULL, "
        "ip_hash TEXT NOT NULL DEFAULT '', "
        "ua TEXT NOT NULL DEFAULT '', "
        "dur_ms INTEGER NOT NULL DEFAULT 0, "
        "user_id INTEGER"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_page_visits_ts ON page_visits(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_page_visits_role ON page_visits(role)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_page_visits_path_ts ON page_visits(path, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_page_visits_role_ts ON page_visits(role, ts)")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS server_metrics ("
        "ts TEXT NOT NULL, "
        "cpu REAL, "
        "mem_pct REAL, "
        "disk_pct REAL, "
        "net_in REAL, "
        "net_out REAL, "
        "load1 REAL, "
        "load5 REAL, "
        "load15 REAL, "
        "proc_count INTEGER"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_server_metrics_ts ON server_metrics(ts)")
    _commit_if_free(conn)


def _assert_rebuild_preserved(conn, table, expected_count, expected_cols):
    """重建型迁移（CREATE→INSERT→DROP→RENAME）的守恒断言。

    行数必须与重建前一致，列集必须覆盖重建前的列集（列清单由 PRAGMA 现场读取，
    不硬编码——硬编码会在后续迁移补列后自己打自己）。不守恒说明重建丢数据/丢列，
    这种半途形态**不得**被记为迁移成功：抛错让核心档阻断启动，可选档下次重跑。
    """
    after_count = conn.execute(
        f"SELECT COUNT(*) FROM {_quote_ident(table)}").fetchone()[0]
    # 不走 `_table_columns`：那层白名单只覆盖建表域助手，v13 重建的是 sqlite_master
    # 现场发现的任意表（可含白名单外的历史表），表名已按 `_quote_ident` 转义入参。
    after_cols = {r["name"] for r in conn.execute(
        f"PRAGMA table_info({_quote_ident(table)})").fetchall()}
    missing = sorted(set(expected_cols) - after_cols)
    if after_count != expected_count or missing:
        raise RuntimeError(
            f"{table} 表重建守恒断言失败：行数 {expected_count}→{after_count}，"
            f"缺失列 {missing}（拒绝提交半程 schema）")


def migrate_v5(conn):
    """v5：用户注销支持——users 增加 deleted/deleted_at，邮箱唯一改为活跃唯一，新增注销请求表。"""
    orig_cols = _table_columns(conn, "users")
    cols = orig_cols
    if "deleted" not in cols:
        # 使用 ALTER TABLE ADD COLUMN 而非表重建，避免崩溃窗口数据丢失
        conn.execute("ALTER TABLE users ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE users ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
        _commit_if_free(conn)
    # 旧库 users.email TEXT UNIQUE 会生成 sqlite_autoindex_users_N 全局唯一索引，
    # 与“同邮箱可有一个活跃 + 多个已注销”的部分唯一索引冲突，必须先移除。
    # SQLite 不允许直接 DROP 与 UNIQUE 列约束关联的自动索引，因此重建 users 表
    # 去掉 email UNIQUE 约束（保留数据）；idx_users_email_live 本身保留不动。
    old_email_indexes = []
    for idx in conn.execute("PRAGMA index_list('users')").fetchall():
        name = idx["name"]
        if name == "idx_users_email_live" or not idx["unique"]:
            continue
        # 索引名里的单引号必须转义后入参：不转义时 f-string 拼出的 SQL 被打断，
        # 最坏直接 OperationalError 卡死启动（MF-40 修法 6）。
        info = conn.execute(
            f"PRAGMA index_info('{_quote_str_literal(name)}')").fetchall()
        if [r["name"] for r in info] == ["email"]:
            old_email_indexes.append(name)
    if old_email_indexes:
        # 列清单由 schema 驱动（MF-40 修法 6）：新表 DDL 按旧表 PRAGMA table_info
        # 现场生成，携带各列的类型/非空/默认值；email 上的旧 UNIQUE 约束不在
        # table_info 里，随重建自然消失（这正是重建的目的）。硬编码列清单会把
        # 基线/其他迁移已存在的列（如 mail_notify）"成功重建"成静默丢列点。
        old_cols = conn.execute("PRAGMA table_info(users)").fetchall()
        col_list = ", ".join(_quote_ident(c["name"]) for c in old_cols)
        defs = []
        for c in old_cols:
            qname = _quote_ident(c["name"])
            if c["pk"]:
                # id 主键按原语义重建为 AUTOINCREMENT（历史 users 表皆为此形态）
                defs.append(f"{qname} INTEGER PRIMARY KEY AUTOINCREMENT")
                continue
            piece = f"{qname} {c['type'] or 'TEXT'}"
            if c["notnull"]:
                piece += " NOT NULL"
            if c["dflt_value"] is not None:
                piece += f" DEFAULT {c['dflt_value']}"
            defs.append(piece)
        before_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        # 半程崩溃的残留（上次 CREATE 了没改名）必须先清：否则重跑必抛
        # "table users_new already exists" ⇒ 启动永久阻断（MF-40 现象③）。
        conn.execute("DROP TABLE IF EXISTS users_new")
        conn.execute(f"CREATE TABLE users_new ({', '.join(defs)})")
        conn.execute(
            f"INSERT INTO users_new ({col_list}) SELECT {col_list} FROM users"
        )
        conn.execute("DROP TABLE users")
        conn.execute("ALTER TABLE users_new RENAME TO users")
        # 守恒：行数不变 + 列集覆盖重建前（另加本迁移补的 deleted*，若走到这必已含）
        _assert_rebuild_preserved(
            conn, "users", before_count, orig_cols | {"deleted", "deleted_at"})
        _commit_if_free(conn)
    _ensure_index(
        conn,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_live "
        "ON users(email) WHERE deleted = 0",
    )
    # 逐条 execute 替代 executescript（同上，保持迁移事务原子）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_delete_requests ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "username TEXT NOT NULL, "
        "ip_hash TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_delete_requests_user ON user_delete_requests(username)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_delete_requests_ip ON user_delete_requests(ip_hash)")
    _commit_if_free(conn)


def migrate_v6(conn):
    """v6：WebUI 统计/监控补齐——sign_events 增加 account_id/dur_sec/finished_at，
    page_visits 增加 user_id，并补索引。可选迁移，失败不阻断启动。

    回填 UPDATE 走命名保存点：它失败时只回滚自己，不回滚上面刚补的列与索引。
    但保存点只覆盖"语句级失败"这一半：磁盘满/IO 错误这一族错误码会让 SQLite 把
    **整条事务**自动回滚，保存点连同它之前刚补的列一起消失。故失败后复核产物，
    产物不在就抛 `MigrationDeferred`——延后本档换取"下次启动整段重跑"，
    绝不带着"产物缺失"去提升版本。
    """
    _ensure_column(conn, "sign_events", "account_id", "INTEGER")
    _ensure_column(conn, "sign_events", "dur_sec", "REAL")
    _ensure_column(conn, "sign_events", "finished_at", "TEXT")
    _ensure_column(conn, "page_visits", "user_id", "INTEGER")
    _ensure_index(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_sign_events_phone_ts "
        "ON sign_events(phone, ts)",
    )
    _ensure_index(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_sign_events_account_ts "
        "ON sign_events(account_id, ts)",
    )
    _ensure_index(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_page_visits_path_ts "
        "ON page_visits(path, ts)",
    )
    _ensure_index(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_page_visits_role_ts "
        "ON page_visits(role, ts)",
    )
    # 回填走命名保存点，失败只回滚这一条 UPDATE。
    # 为什么不能用整段 rollback：框架持事务时上面补的三列四索引还滞留在
    # `_run_migrations` 的 BEGIN IMMEDIATE 里（`_ensure_column`/`_ensure_index`
    # 经 `_commit_if_free` 不提前落盘）。旧写法 `conn.rollback()` 把它们一起吃掉，
    # 而 v6 不抛异常 ⇒ 框架照写记录、照把 user_version 提到 6 ⇒「版本说过了 v6、
    # schema 里没有」，此后 events 的两条写入路径永久报 no such column（ba-p01-02）。
    _backfill_sp = "v6_backfill_account_id"
    try:
        conn.execute(f"SAVEPOINT {_backfill_sp}")
        conn.execute(
            "UPDATE sign_events SET account_id = ("
            "SELECT id FROM accounts WHERE accounts.phone = sign_events.phone LIMIT 1"
            ") WHERE account_id IS NULL"
        )
        conn.execute(f"RELEASE SAVEPOINT {_backfill_sp}")
        _commit_if_free(conn)
    except Exception as e:
        # 撤销的只是这条 UPDATE；刚补的列与索引留在事务里，由框架末尾一并提交。
        with contextlib.suppress(Exception):
            conn.execute(f"ROLLBACK TO SAVEPOINT {_backfill_sp}")
            conn.execute(f"RELEASE SAVEPOINT {_backfill_sp}")
        # 复核产物还在不在。SQLITE_FULL / SQLITE_IOERR / SQLITE_NOMEM / SQLITE_INTERRUPT
        # 这一族错误让 SQLite 自动回滚**整条事务**：保存点随之消失（上面两条都会失败），
        # 本迁移刚补的列一并消失。此时若照旧返回，框架仍会写记录、把 user_version
        # 提到 6 ⇒ 重演"版本说过了 v6、schema 里没有"，链尾门拒启且只能人工修库。
        # 抛延后则版本不提升，下次启动整段重跑（本迁移幂等）。
        missing = [f"{t}.{c}" for t, c in _ARTIFACTS[6]
                   if c is not None and c not in _table_columns(conn, t)]
        if missing:
            raise MigrationDeferred(
                "回填 sign_events.account_id 失败，且本迁移的产物已不在库中"
                f"（{'、'.join(missing)}，底层事务被整体回滚）: "
                f"{type(e).__name__}: {e}"
            ) from e
        logger.warning(
            "回填 sign_events.account_id 未完成: %s——本迁移补的列与索引已生效，"
            "只有存量行的 account_id 仍为 NULL（可视化按账号聚合会退化成按手机号）", e)


def migrate_v7(conn):
    """v7：注销请求表加 kind 列（delete=注销 / restore=恢复）。

    注销与恢复共用计数时，注销动作本身写入的记录会让随后 60 秒内的恢复请求全部
    429（"注销后立即反悔"路径必现），故按动作分列。旧数据无 kind → 默认 'delete'
    （历史记录均为注销）。
    """
    _ensure_column(
        conn, "user_delete_requests", "kind", "TEXT NOT NULL DEFAULT 'delete'"
    )


def migrate_v8(conn):
    """v8：会话 Cookie 缓存表（OAuth 会话复用，降低登录频率 = 降低风控触发面）。

    缓存对象为序列化 cookie jar + csrf（login_killyiban 完成后的完整认证态）；
    cookies_ct 为 AES-GCM 密文 JSON 串（AAD=phone，复用 account_crypto），
    库内绝不落明文 cookie。
    """
    # 逐条 execute 替代 executescript（同上，保持迁移事务原子）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS session_cache ("
        "phone        TEXT PRIMARY KEY, "
        "cookies_ct   TEXT NOT NULL, "
        "csrf         TEXT NOT NULL, "
        "created_at   TEXT NOT NULL, "
        "updated_at   TEXT NOT NULL"
        ")"
    )
    # 应用元数据：purge 时钟跳变保护的单调参照等
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_meta ("
        "key   TEXT PRIMARY KEY, "
        "value TEXT NOT NULL"
        ")"
    )
    _commit_if_free(conn)


def migrate_v9(conn):
    """v9：用户邮箱通知开关（users.mail_notify，默认开启接收签到结果邮件）。

    1=接收（默认）；0=关闭（不接收用户签到失败邮件 B 线）。
    管理员告警邮件（A 线）不受此开关影响。旧库补列时默认置 1。
    """
    _ensure_column(conn, "users", "mail_notify", "INTEGER NOT NULL DEFAULT 1")


def migrate_v10(conn):
    """v10：软删除操作者留痕（accounts.deleted_by）。

    区分删除来源，支撑「用户自删可撤销、管理员删除仅管理员可恢复」：
    - 用户自行删除：deleted_by = 用户邮箱（宽限期内可在用户页自行撤销）；
    - 管理员删除：deleted_by = 'admin'；
    - 系统连带（注销联动 soft_delete_user_with_accounts 等）：置空串。
    旧数据/旧路径默认空串 = 用户不可自行撤销（fail-closed，防越权恢复管理员清退的账号）。
    """
    _ensure_column(conn, "accounts", "deleted_by", "TEXT NOT NULL DEFAULT ''")


def migrate_v11(conn):
    """v11：服务端会话吊销（users.sid）。

    sid 为该用户当前唯一有效会话标识：登录时签发，登出/被重置密码/被踢时轮换；
    会话内 sid 与库内不一致即视为未登录。空串=未签发（升级日存量兼容）。
    """
    _ensure_column(conn, "users", "sid", "TEXT NOT NULL DEFAULT ''")


def migrate_v12(conn):
    """v12：幂等补建 app_meta 表（修复历史部署缺表）。

    app_meta 的建表挂在 migrate_v8，但 v8 早已随 session_cache 发布——user_version
    已 ≥8 的旧部署不会再重跑 v8，缺表会让时钟守卫 / 每日清理 / 审计锚点元数据全部报错。
    新增本迁移幂等补建，旧库自动补齐；新库 v8 已建则 IF NOT EXISTS 空操作。
    教训：新表/新列必须新增迁移版本，不得修改已发布的旧迁移。
    """
    # 逐条 execute 替代 executescript（同上，保持迁移事务原子）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_meta ("
        "key   TEXT PRIMARY KEY, "
        "value TEXT NOT NULL"
        ")"
    )
    _commit_if_free(conn)


# 畸形列声明：`col col TYPE ...`（列名被重复写进类型声明）。
# 形如 accounts.deleted_by 声明为 `deleted_by deleted_by TEXT NOT NULL DEFAULT ''`。
# 成因见 _ensure_column 文档串（历史调用方把列名一并传进 type_decl）。
_MALFORMED_COL_RE = re.compile(r"(?<=[(,])\s*([A-Za-z_][A-Za-z0-9_]*)\s+\1\b\s+")


def _malformed_schema_tables(conn):
    """返回声明类型重复列名的表 [(表名, 原始 DDL), ...]（跳过 sqlite_ 内部表）。"""
    out = []
    for row in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
    ):
        name, sql = row["name"], row["sql"]
        if name.startswith("sqlite_"):
            continue
        if _MALFORMED_COL_RE.search(sql):
            out.append((name, sql))
    return out


def migrate_v13(conn):
    """v13：修复畸形列声明（`col col TYPE`），重建受影响表。

    成因：调用方误用 _ensure_column，把列名一并写进 type_decl（见其文档串），
    使列名在 DDL 里出现两次。

    影响：SQLite 对类型声明取子串匹配算亲和性，畸形声明与正确声明的亲和性一致，
    typeof() 与值强制转换亦相同，索引 / 约束 / 审计链均不受影响。故本迁移**不是修
    故障，而是修 schema 可读性与可移植性**（.dump 会把畸形带进新库，外部工具按
    table_info 生成的 DDL 也会继承）。

    做法：按 sqlite_master 里的 DDL 去掉重复列名后重建表 + 回填数据 + 重建索引
    （CREATE TABLE → INSERT SELECT → DROP → RENAME，同 migrate_v5 的模式）。
    SQLite 不允许改列声明，只能重建。**幂等**：无畸形表时空操作。
    索引 DDL 从 sqlite_master 原样取回，不硬编码（避免与建表处漂移）。
    AUTOINCREMENT 计数由 sqlite_sequence 随表名迁移保留。
    """
    bad = _malformed_schema_tables(conn)
    if not bad:
        return
    for name, sql in bad:
        fixed = _MALFORMED_COL_RE.sub(r" \1 ", sql)
        # 索引 DDL 先取回：DROP TABLE 会连带删除其索引，重建表后按原样重建
        indexes = [
            r["sql"]
            for r in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                (name,),
            )
        ]
        # 表名/列名一律转义后入 SQL：名字里含 `"` 时裸插值会把语句打断，
        # 最坏 OperationalError 卡死启动（MF-40 修法 6）。
        cols = [r["name"] for r in conn.execute(
            f"PRAGMA table_info({_quote_ident(name)})").fetchall()]
        before_count = conn.execute(
            f"SELECT COUNT(*) FROM {_quote_ident(name)}").fetchone()[0]
        tmp = f"{name}__v13"
        conn.execute(f"DROP TABLE IF EXISTS {_quote_ident(tmp)}")
        # 只替换首个表名出现处：DDL 里其余位置可能含同名子串（如索引名）。
        # 检索串按转义形态构造，与 sqlite_master 里的原样书写一致（带引号的
        # 特殊名存储时引号已双写；裸名书写时 `_quote_ident` 串匹配不上，落兜底分支）。
        fixed_tmp = fixed.replace(f"TABLE {_quote_ident(name)}",
                                  f"TABLE {_quote_ident(tmp)}", 1)
        if fixed_tmp == fixed:
            fixed_tmp = fixed.replace(f"TABLE {name}", f"TABLE {tmp}", 1)
        conn.execute(fixed_tmp)
        collist = ", ".join(_quote_ident(c) for c in cols)
        conn.execute(f"INSERT INTO {_quote_ident(tmp)} ({collist}) "
                     f"SELECT {collist} FROM {_quote_ident(name)}")
        conn.execute(f"DROP TABLE {_quote_ident(name)}")
        conn.execute(f"ALTER TABLE {_quote_ident(tmp)} RENAME TO {_quote_ident(name)}")
        for idx_sql in indexes:
            conn.execute(idx_sql)
        _assert_rebuild_preserved(conn, name, before_count, cols)
        logger.info("schema 修复：重建表 %s（%d 列，%d 索引）", name, len(cols), len(indexes))
    _commit_if_free(conn)


def migrate_v14(conn):
    """v14：删除从未接线的统计表（可选迁移，失败只告警不阻断启动）。

    page_visits / server_metrics 是 v4 建、v6 补列的一整套"页面访问统计 + 服务器
    采样"能力，但生产侧**零写入方、零读取方、零 UI**：web/ 全目录无任何引用，唯一
    写入者是 scripts/generate_demo_data.py（演示数据生成器）。保留它们只会让每次
    启动多跑两条全表 DELETE，并让两张空表与八个空索引常驻 schema。

    **只删表，不改 migrate_v4 / migrate_v6 原文**——已发布迁移不可修改（改了对
    存量库无效，对介于 v4~v6 之间的库反而会制造"表不存在"的失败路径）。因此全新库
    的执行序是 v4 建表 → v6 补列 → 本迁移删表，一次性的"建了又删"换取迁移历史不变；
    _ALLOWED_TABLES 保留这两个表名也是因为冻结的 v6 仍会引用它们。
    """
    conn.execute("DROP TABLE IF EXISTS page_visits")
    conn.execute("DROP TABLE IF EXISTS server_metrics")
    _commit_if_free(conn)


def migrate_v15(conn):
    """v15：在线校验异步任务表。可选迁移，失败只告警不阻断启动。

    独立小表，**不污染 accounts.status 枚举**：任务态（排队/在跑/完成/被拒）与
    账号审核态（pending/active/rejected）是两件事，前者可短期清理，后者是业务状态。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS verify_jobs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "account_id INTEGER, "
        "phone TEXT NOT NULL, "
        "owner_email TEXT NOT NULL DEFAULT '', "
        "status TEXT NOT NULL DEFAULT 'pending', "
        "error TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL, "
        "started_at TEXT, "
        "finished_at TEXT"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_verify_jobs_owner_created "
        "ON verify_jobs(owner_email, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_verify_jobs_status ON verify_jobs(status)"
    )
    _commit_if_free(conn)


def _create_verify_jobs_table(conn):
    """建 verify_jobs（含 prev_status）——v15 之后新增列的迁移复用点。

    v15 的 DDL 已冻结不再改动（已发布迁移不可变），故此处重述一遍：
    带上 prev_status 的建表语句是幂等的，v16 在"v15 尚未落地"的库上
    也能自给自足地建出正确结构。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS verify_jobs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "account_id INTEGER, "
        "phone TEXT NOT NULL, "
        "owner_email TEXT NOT NULL DEFAULT '', "
        "status TEXT NOT NULL DEFAULT 'pending', "
        "prev_status TEXT NOT NULL DEFAULT 'pending', "
        "error TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL, "
        "started_at TEXT, "
        "finished_at TEXT"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_verify_jobs_owner_created "
        "ON verify_jobs(owner_email, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_verify_jobs_status ON verify_jobs(status)"
    )


def migrate_v16(conn):
    """v16：verify_jobs 记录任务建立时的账号状态（可选迁移，失败只告警不阻断）。

    用途：异步校验结果**不得覆盖人工决定**。校验任务建库时账号是 pending
    （用户提交）或 active（管理员的裸账号），任务失败只允许在账号仍处于
    建库时那个状态时置 rejected——否则管理员在任务执行期间点了"审核通过"，
    迟到的校验结果会把管理员的决定静默回滚。

    旧行 prev_status 取默认 'pending'：存量未结任务罕见，且默认值只会让
    "账号已是 active 时不覆盖"，与人工决定优先的方向一致。
    """
    _create_verify_jobs_table(conn)
    _ensure_column(conn, "verify_jobs", "prev_status", "TEXT NOT NULL DEFAULT 'pending'")
    _commit_if_free(conn)


def migrate_v17(conn):
    """v17：签到领取池 `sign_claims`（多执行体协调）。**核心迁移**（MF-40 改判）：
    领取路径（claims.try_claim / queue_store）把本表当硬编表名用，缺表 ⇒ 整条 v3
    路径 fail-closed 拒跑 ⇒ 失败必须阻断启动，不得只告警。

    为什么独立成表：多执行体的分工靠"原子领取 + 租约"而不是静态分片——静态分片下
    最慢的那一份决定全天成败。领取记录同时承担"当日是否了结"的判据（state）。

    `UNIQUE(phone, day)` 是**并发正确性的基础**：一个账号一天只可能有一行，
    领取走 upsert，故不存在两个执行体同时"新插入"同一个账号的窗口。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sign_claims ("
        "phone TEXT NOT NULL, "
        "day TEXT NOT NULL, "
        "owner TEXT NOT NULL, "
        "claimed_at TEXT NOT NULL, "
        "heartbeat_at TEXT NOT NULL, "
        "state TEXT NOT NULL DEFAULT 'claimed', "
        "result TEXT NOT NULL DEFAULT '', "
        "attempts INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY (phone, day)"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sign_claims_day_state "
        "ON sign_claims(day, state)"
    )
    _commit_if_free(conn)


def migrate_v18(conn):
    """v18：持久化任务队列（sign_tasks）+ 出口令牌桶状态（egress_state）。

    核心迁移（is_core=True）：执行体的批领/待办闸门全建在
    `sign_tasks` 上，缺表等于当日没有队列 ⇒ 失败阻断启动。重跑（版本未提升的下次
    启动）必须幂等，故建表用 IF NOT EXISTS。
    **单池化后本迁移不再从 `sign_claims` 平移任何行**（旧表冻结保留，其存量行是当日
    临时的领取记录，切换后下一轮自然在 `sign_tasks` 重建；不做数据迁移）。
    `vshard=-1` 仍是 `planner.write_plan` 显式接管的**历史标记行**形态（v20 补账/旧
    平移遗留），它们不进任何分片集、不被批领、不计入 `pending_count`。

    `sign_tasks` 一行 = 一个账号在一个业务日的计划、当前状态与**跨执行体共享**的
    尝试数。`state` 取值：

    | state | 含义 |
    |-------|------|
    | `pending` | 待领取（`run_at` 到点后被批量领取） |
    | `claimed` | 已被某执行体领取、任务级租约未过期 |
    | `done` / `skipped` | 已完成（成功/已签到/今日无任务/窗口外跳过） |
    | `failed` / `stolen` | 未了结：可重排（`run_at` 后退）或按分片接管 |

    `vshard` 是把账号划分给执行体的确定性哈希分工所用的虚分片槽位（0..255，
    256 个槽，故增加执行体时既有计划不必重排）；`vshard=-1` 表示"不参与该分工的
    历史行"（其 state 非 pending 时不会命中批领，故历史行不会被重新领取）。
    `owner` 一列同时承担"计划归属的执行体"与"当前持有者"，`epoch` 是 fencing token
    （写入侧的单调序号，用于拒绝被接管者迟到的写）——列在 v18 一次建齐（schema
    变更此刻最便宜），其自增与终态写的 WHERE 守卫由领取/收尾路径实现。

    **耐久性**：本表回答"当日是否已登录"，终态被回滚等于对同一账号再登录一次
    （上游风控红线），故连接必须是 FULL——WAL+NORMAL 会丢最近提交。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sign_tasks ("
        "phone TEXT NOT NULL, "
        "day TEXT NOT NULL, "
        "vshard INTEGER NOT NULL, "
        "owner TEXT NOT NULL DEFAULT '', "
        "run_at TEXT NOT NULL, "
        "priority INTEGER NOT NULL DEFAULT 5, "
        "state TEXT NOT NULL DEFAULT 'pending', "
        "attempts INTEGER NOT NULL DEFAULT 0, "
        "lease_until TEXT NOT NULL DEFAULT '', "
        "epoch INTEGER NOT NULL DEFAULT 0, "
        "result TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL, "
        "PRIMARY KEY (phone, day)"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_pickup "
        "ON sign_tasks(day, vshard, state, run_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_lease ON sign_tasks(state, lease_until)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS egress_state ("
        "egress TEXT PRIMARY KEY, "
        "rate REAL NOT NULL /* 单位 = 账号尝试/s（attempt/s）：1 = 单出口每秒 1 次账号尝试"
        "（单账号 = 6 次 HTTP 请求） */, "
        "burst REAL NOT NULL DEFAULT 0, "
        "tat REAL NOT NULL DEFAULT 0, "
        "updated_at TEXT NOT NULL"
        ")"
    )
    # 提交点与 v17 同形：迁移体不做事务管理，框架持事务时交给框架末尾统一提交。
    # **单池化后不再平移 `sign_claims`**：本表是唯一生产台账，旧表的存量行是当日临时的
    # 领取记录，切换后下一轮自然在本表重建；旧表与其 `epoch` 列冻结保留（见 `claims`
    # 模块头），不做数据迁移。
    _commit_if_free(conn)
    # 耐久级必须在**事务外**改：SQLite 对事务内的 PRAGMA synchronous 直接报
    # "Safety level may not be changed inside a transaction"。直调路径上面已经提交，
    # 这里立刻生效；框架持事务时事务还没关，本条改由 `_run_migrations` 在整链提交后
    # 补一次（连接级设置，重复执行幂等）。PRAGMA 若仍失败，blocked 路径下次启动整段
    # 重跑（幂等）收敛。
    if not conn.in_transaction:
        conn.execute("PRAGMA synchronous = FULL")


def migrate_v19(conn):
    """v19：`sign_claims` 补 fencing token 列 `epoch`。

    **核心迁移**（MF-40 改判）：`try_claim`/`settle`/`claim_batch` 把 `epoch` 当
    硬编列名引用——缺列时领取整表 fail-closed 返回"不可执行"，全天零签到却只留下
    warning。失败必须阻断启动并点名，产物缺失同样拒启（`_ARTIFACTS`）。

    为什么需要：账号级租约 900s 的判据是"心跳时间串"，而执行体会被 STW 停顿/容器
    挂起卡住数分钟——它醒来后仍以为自己持有该账号，会把迟到的结论写进去，覆盖接管者
    的结论。故每次领取自增一个单调序号（fencing token），收尾写的 WHERE 带上它，
    存储端主动拒绝"token 后退的写"（Kleppmann：只给领取侧发号而不校验等于没做）。

    存量行取默认 0（= 从未被领取过），**NOT NULL 是必需的**：领取路径要拿它做
    `epoch = epoch + 1`，NULL 会让算术静默变 NULL、守卫全失效。

    `sign_tasks.epoch` 由 v18 建齐；此处一并 `_ensure_column` 兜底——v18 若被回退，
    本迁移仍能把队列侧的护栏补上。两条 ALTER 都幂等，失败后版本不提升、下次启动重跑。
    """
    _ensure_column(conn, "sign_claims", "epoch", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "sign_tasks", "epoch", "INTEGER NOT NULL DEFAULT 0")
    _commit_if_free(conn)


# ---------------------------------------------------------------------------
# v20 backfill 参数（**冻结语义**：迁移一发布即不可修改，见模块头）
# ---------------------------------------------------------------------------
#: 回看窗口（天）：与备份保留期、领取池的观察期同量级——够覆盖一次跨版本接管之后
#: 的对账，又不至于每次启动去翻整年状态文件。
_BACKFILL_DAYS = 14

#: 状态文件里的 `time` 形态（HH:MM:SS，含取值范围校验）。不匹配则回退当日零点——
#: `run_at` 是 NOT NULL 且是"批领到期时刻"的比较对象，必须给一个合法时间串。
_BACKFILL_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d:[0-5]\d$")

#: 单批提交行数：攒够就提交一次，避免一次长事务长时间占住库级写锁。
_BACKFILL_COMMIT_ROWS = 50

#: JSON 状态 → `sign_tasks.state` 的映射（**冻结语义**；只登记终态）。
#: 为什么按这三档落：
#: - `success`/`already`/`no_task` 是 `yiban.status.CLAIM_DONE_STATUSES`，当日不必
#:   再签 → `done`；
#: - `skipped_window`/`skipped_norange`/`no_position` 与 `failed` 同落 `failed`：
#:   前两者**在 `UNDONE_STATUSES` 内**、`no_position` 是可重试侧（点位为空不代表
#:   今天的窗口不会再开），都属"还该再试"，落 `skipped` 会把它们判成已了结、让
#:   补签轮不再重跑；
#: - `paused`/`user_cancelled`/`global_paused` 是管理侧决策（熔断/用户自停/全站
#:   暂停），今天不会再试 → `skipped`；
#: - `supplementing`（平台 State=5 补签中）与上面三格同组 → `skipped`：平台正在处理，
#:   我们再跑一轮既改不了结果、又多一次真实登录（它也不在 `UNDONE_STATUSES`，
#:   见 `yiban/status.STATUS_SUPPLEMENTING`）。落 `failed` 会把它算成"未了结"，
#:   补签轮据此重跑该账号。
#: 在途状态（`retrying`/`pending`）**不补**：那不是终态，补进台账会凭空多出待办。
#: **新增状态码必须同步本表**：本表被 v20 补账与 `scripts/ledger_check.py` 的对账判定
#: 共用，少一格时两头同时失效——该补的行不进台账，而用同一张表做的对账还报"对账平"，
#: 全程无信号。绑定由 tests/test_migrations_v20.py 的键集守卫钉住。
# 一致性由 tests/test_migrations_v20.py 与未来 ledger_states 的等价断言共同保证
# ——将来若把这份映射搬到 `ledger_states.from_status_json`，必须与本表逐格一致。
_JSON_TERMINAL_TO_TASK_STATE = {
    "success": "done", "already": "done", "no_task": "done",
    "failed": "failed",
    "skipped_window": "failed", "skipped_norange": "failed", "no_position": "failed",
    "paused": "skipped", "user_cancelled": "skipped", "global_paused": "skipped",
    "supplementing": "skipped",
}

#: 补账行的 owner 标记（对账据此区分"平移行/补账行"）。
_BACKFILL_OWNER = "backfill"


def _read_sign_state(state_dir, day):
    """读某日的按日状态文件；缺失/损坏/非 dict/空 → None（调用方按"跳过该日"处理）。

    口径与 `state_io._daily_statuses` 一致（`utf-8-sig` 容 BOM、坏文件按无记录），
    但不复用它的实现：那个函数只读"今天"且把"读失败"折叠成空 dict，而本处要区分
    "无记录"与"这一日跳过"，以便把扫描账目记准。**不得**读 `sign-daily-<day>.json`
    ——那是 `{phone: 符号}` 的符号表，没有 `status`/`time`。
    """
    path = os.path.join(state_dir, f"sign-state-{day}.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.warning("v20 backfill：状态文件缺失，跳过该日 %s", path)
        return None
    except (OSError, ValueError, TypeError) as e:
        logger.warning("v20 backfill：状态文件不可读，跳过该日 %s [%s: %s]",
                       path, type(e).__name__, _sanitize_text(e))
        return None
    if not isinstance(data, dict) or not data:
        logger.warning("v20 backfill：状态文件非 dict 或为空，跳过该日 %s", path)
        return None
    return data


def terminal_task_state(entry):
    """该条目对应的池状态；非终态与「无记录」→ None。

    空串与缺 `status` 键都是「无记录」（与 `status.is_concluded_status` 同口径，
    即 `""` 不算一条"状态为空的结论"），不是 `pending`。

    公开名字是**有意的**：`scripts/ledger_check.py` 的对账判定必须与本处同一份逻辑，
    各写一份（哪怕共用同一张常量表）也会在规范化细节上漂移，对账据此报出并不存在的
    差异或漏报真差异。
    """
    if not isinstance(entry, dict):
        return None
    return _JSON_TERMINAL_TO_TASK_STATE.get(str(entry.get("status") or "").strip())


def _entry_time(entry):
    """条目里的执行时刻（HH:MM:SS）；缺失或不可解析 → 当日零点。"""
    raw = str(entry.get("time") or "")
    return raw if _BACKFILL_TIME_RE.match(raw) else "00:00:00"


def _ensure_state_dir_readable(state_dir):
    """v20 前置：**目录存在但读不动** ⇒ 延后（MigrationDeferred），绝不"空跑成功"。

    旧实现把整目录不可读折叠成"每天都没有状态文件"，返回 0 行还照样提升版本——台账
    从此对这段窗口永久空白且不再重试（MF-40 现象⑤）。区分两种形态：目录不存在 =
    无状态可补（全新部署常态）→ 放行；目录在而列不动（权限/挂载故障）→ 延后 +
    告警，下次启动整段重试。
    """
    if not os.path.isdir(state_dir):
        return
    try:
        os.listdir(state_dir)
    except OSError as e:
        logger.warning(
            "v20 backfill：状态目录不可读，本次延后（user_version 不提升，下次启动重试）: "
            "%s [%s: %s]", state_dir, type(e).__name__, _sanitize_text(e))
        raise MigrationDeferred(f"v20 状态目录不可读，台账补账延后: {state_dir}") from e


def migrate_v20(conn):
    """v20：把 `sign-state-*.json` 里的终态补进 `sign_tasks`（可选迁移，失败只告警不阻断）。

    为什么需要：v18 已**不再平移** `sign_claims`（单池化后旧表冻结），而 JSON 里的跳过类
    终态（`paused`/`skipped_window`/…）从来只在状态文件里、不在任务队列——只做 v18 的话
    台账对"已跳过"的账号仍是空白，故由本迁移从状态文件补回。

    只读 `sign-state-<day>.json`（按日结构化状态文件）；最近 `_BACKFILL_DAYS` 天里
    文件缺失或损坏的日**跳过**（台账以两张表为准），不报错也不阻断。但**状态目录
    存在而不可读**时延后（`_ensure_state_dir_readable`）：不补任何行、不提升版本、
    下次启动整段重试——旧实现让它"空跑成功"，台账窗口从此永久空白。

    `owner='backfill'` + `vshard=-1` 是与 v18 平移行同形的**惰性历史行**：`-1` 与任何
    执行体的分片集合不相交，既不参与批领也不进入任何接管并入范围。`INSERT OR IGNORE` 让本
    迁移幂等——已有行（平移行、执行体真写的行）一概不覆盖，故失败/延后后下次启动
    整段重跑也能收敛。每 `_BACKFILL_COMMIT_ROWS` 行提交一次，避免长事务占住写锁。
    框架持事务时这个提交点走 `_commit_if_free` 不生效：写锁本来就被外层
    `BEGIN IMMEDIATE` 持有，攒批只是直调（旧调用面/测试）形态下的让锁。

    返回本次**实际补入**的行数（重跑为 0），供调用方与运维判断收敛。
    """
    state_dir = env_io.resolve_path("YIBAN_STATE_DIR", "/var/log/yiban")
    _ensure_state_dir_readable(state_dir)
    # owner 直接内联：`_BACKFILL_OWNER` 是模块常量，不进绑定参数
    sql = ("INSERT OR IGNORE INTO sign_tasks (phone, day, vshard, owner, run_at, "
           "priority, state, attempts, lease_until, result, created_at) "
           f"VALUES (?, ?, -1, '{_BACKFILL_OWNER}', ?, 5, ?, 0, '', '', ?)")
    anchor = clock.now()
    inserted = 0
    scanned_days = 0
    skipped_days = 0
    uncommitted = 0
    for offset in range(_BACKFILL_DAYS):
        day = (anchor - timedelta(days=offset)).strftime("%Y-%m-%d")
        entries = _read_sign_state(state_dir, day)
        if entries is None:
            skipped_days += 1
            continue
        scanned_days += 1
        for phone, entry in entries.items():
            state = terminal_task_state(entry)
            if state is None:
                continue
            stamp = f"{day} {_entry_time(entry)}"
            cur = conn.execute(sql, (phone, day, stamp, state, stamp))
            inserted += cur.rowcount
            uncommitted += 1
            if uncommitted >= _BACKFILL_COMMIT_ROWS:
                _commit_if_free(conn)
                uncommitted = 0
    _commit_if_free(conn)
    # 只报计数：补账涉及的是账号，日志里不得出现手机号
    logger.info("v20 backfill：补入 %d 行（扫描 %d 天，跳过 %d 天）",
                inserted, scanned_days, skipped_days)
    return inserted


def migrate_v21(conn):
    """v21：内核进度事件表 `run_events`（N2a 打点层）。可选迁移，失败只告警不阻断。

    **为什么可选**：本表是观测面——`yiban/engine/executor_v3.py` 与
    `yiban/engine/runner.py` 经 `yiban.store.run_events.report` 写入，写入失败被
    隔离（只告警），签到结论与退出码都不依赖它。故迁移执行失败不该阻断启动。

    **但产物必须在册**：登记进产物登记表后，版本声称过了本档而表不存在
    （半升级库、有人手工 DROP）一律 `MigrationIntegrityError` 拒启——
    观测表静默零写比拒启更难查（与 sign_events 的成形理由相同）。

    **按日保留期**：行由 `run_events.purge` 按业务日（`day` 列）删除，故
    `day` 上建索引；`id` 是自增主键，下游按 id 增量尾读（SSE）时无须第二把索引。
    列窄而固定：节点值域是有限集合（`run_events.NODES`），不放 JSON 详情列——
    "什么节点、谁、哪个账号、哪一天、什么时刻"就是这张表的全部契约。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS run_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts TEXT NOT NULL, "
        "day TEXT NOT NULL, "
        "node TEXT NOT NULL, "
        "executor TEXT NOT NULL DEFAULT '', "
        "phone TEXT NOT NULL DEFAULT '', "
        "message TEXT NOT NULL DEFAULT ''"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_events_day ON run_events(day)"
    )
    _commit_if_free(conn)


# 迁移项格式：(目标版本号, 名称, 函数, 是否核心)
# - 核心迁移：现有功能依赖，失败应阻断启动。
# - 可选迁移：未来/非关键能力，失败只告警或延后重试。
#
# v17/v18/v19 于 MF-40 修复改判为核心：登记成"可选"后任一失败只发 warning、
# user_version 永不提升，而 v3 领取路径（claims.try_claim / queue_store.claim_batch /
# executor_v3）把这三版的产物当**硬编表名/列名**引用——缺了不会崩，只会整日静默
# 零签到。档位是数据（第 4 元），只管**执行失败时**阻断不阻断。产物由
# `_verify_migration_integrity` 按 `_ARTIFACTS` 核对、完成记录按 `schema_migrations`
# 核对，两侧都点名报错；产物核对不分档（可选档的 DDL 被回滚吃掉同样是脱节）。
# v20 保持可选：它只补台账数据，延后重试无正确性代价（但"目录读不动"不再算成功）。
_MIGRATIONS = [
    (1, "v1_add_account_user_paused", migrate_v1, True),
    (2, "v2_unique_owner_live", migrate_v2, False),
    (3, "v3_audit_hash_chain", migrate_v3, True),
    (4, "v4_visual_tables", migrate_v4, False),
    (5, "v5_user_deregistration", migrate_v5, True),
    (6, "v6_webui_stats", migrate_v6, False),
    (7, "v7_delete_request_kind", migrate_v7, True),
    (8, "v8_session_cache", migrate_v8, False),
    (9, "v9_user_mail_notify", migrate_v9, True),
    (10, "v10_account_deleted_by", migrate_v10, True),
    (11, "v11_user_session_sid", migrate_v11, True),
    (12, "v12_app_meta_repair", migrate_v12, True),
    (13, "v13_fix_malformed_column_decls", migrate_v13, True),
    (14, "v14_drop_legacy_stats", migrate_v14, False),
    (15, "v15_verify_jobs", migrate_v15, False),
    (16, "v16_verify_job_prev_status", migrate_v16, False),
    (17, "v17_sign_claims", migrate_v17, True),
    (18, "v18_sign_tasks", migrate_v18, True),
    (19, "v19_fencing_epoch", migrate_v19, True),
    (20, "v20_backfill_json_terminals", migrate_v20, False),
    # v21 可选：产物是观测表（run_events），签到正确性不依赖它；但产物在册，
    # 缺表仍 fail-closed 拒启（见 migrate_v21 的 docstring）。
    (21, "v21_run_events", migrate_v21, False),
]


def _create_schema_migrations(conn):
    """建迁移完成记录表（幂等）。列刻意不带前缀下划线以外的语义：version 即目标版本号。"""
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_SCHEMA_MIGRATIONS_TABLE} ("
        "version INTEGER PRIMARY KEY, "
        "name TEXT NOT NULL, "
        "applied_at TEXT NOT NULL)"
    )


def _backfill_inherited_records(conn, version):
    """存量库（记录表上线前升的级）首见时按继承回填记录，红线要求的兼容。

    现网 `user_version=17` 的库没有任何记录——那些版本提升由旧链做出，"记录缺失"
    不是漂移而是史前事实；不回填的话新代码第一次启动就把全部存量库拒之门外。
    真·漂移（拨 PRAGMA / 抹记录行）由紧随其后的 `_verify_migration_integrity`
    抓：继承回填只覆盖"登记表里 ≤ 当前版本"的那些项，抹掉**已回填过**的记录
    （表非空时不再回填）照样报错。判据是"表为空且版本 > 0"：全新库（版本 0）
    没有史前提升，不回填。
    """
    if version <= 0:
        return
    if conn.execute(f"SELECT COUNT(*) FROM {_SCHEMA_MIGRATIONS_TABLE}").fetchone()[0]:
        return
    for target_version, name, _fn, _core in _MIGRATIONS:
        if target_version <= version:
            conn.execute(
                f"INSERT OR IGNORE INTO {_SCHEMA_MIGRATIONS_TABLE} "
                "(version, name, applied_at) VALUES (?, ?, 'inherited')",
                (target_version, name),
            )


def _verify_migration_integrity(conn):
    """链尾 fail-closed 校验（MF-40 验收不变量①）：版本过了谁，谁就必须真的完成过。

    对登记表里 `target <= user_version` 的每一项核两件事：
    1. `schema_migrations` 有完成记录——没有 ⇒ 版本推进与迁移执行脱钩（手工拨
       PRAGMA、绕过链改 schema 等）⇒ 拒启；
    2. 该档在 `_ARTIFACTS` 登记的产物（表/列）仍存在于当前 schema——缺了 ⇒ 该迁移
       声称完成而效果不在（迁移后被人删列也算）⇒ 拒启。产物核对不分核心/可选档：
       可选档的 DDL 被回滚吃掉，同样是"版本与 schema 脱节"（ba-p01-02）。
    错误文本必须点名迁移（版本 + 名称 + 缺的东西），运维不用读代码就能定位。
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if _SCHEMA_MIGRATIONS_TABLE not in tables:
        raise MigrationIntegrityError(
            f"迁移记录表 {_SCHEMA_MIGRATIONS_TABLE} 缺失（user_version={version}）——"
            "完整性校验的基准不存在，拒绝启动")
    recorded = {r[0] for r in conn.execute(
        f"SELECT version FROM {_SCHEMA_MIGRATIONS_TABLE}").fetchall()}
    for target_version, name, _fn, _is_core in _MIGRATIONS:
        if target_version > version:
            continue
        if target_version not in recorded:
            # 点名缺什么不能只到"迁移名"粒度：该档登记的产物（表.列）一并列出，
            # 运维拿报错就能直接对照 PRAGMA 检查，不必再翻登记表。
            hints = "、".join(
                f"{t}.{c}" if c else t
                for t, c in _ARTIFACTS.get(target_version, ()))
            raise MigrationIntegrityError(
                f"迁移完整性校验失败: user_version={version} 声称已过 v{target_version}"
                f"（{name}），但 {_SCHEMA_MIGRATIONS_TABLE} 无该迁移的完成记录"
                + (f"（该迁移的产物：{hints}）" if hints else "")
                + "——版本推进与迁移执行脱钩，拒绝启动")
        # 产物核对不看档位是核心还是可选：档位只决定**执行失败时**阻断不阻断，
        # 而"版本过了这档、产物却不在"对两档都是同一类脱节（ba-p01-02）。
        for table, column in _ARTIFACTS.get(target_version, ()):
            if table not in tables:
                raise MigrationIntegrityError(
                    f"迁移完整性校验失败: 迁移 v{target_version}（{name}）的产物表"
                    f" {table} 缺失（user_version={version}）——版本说这档做完了，"
                    "schema 里却没有它建的表，拒绝启动")
            if column is not None and column not in _table_columns(conn, table):
                raise MigrationIntegrityError(
                    f"迁移完整性校验失败: 迁移 v{target_version}（{name}）的产物列"
                    f" {table}.{column} 缺失（user_version={version}）——该列由这档迁移"
                    "补出、被引用它的读写路径当硬编列名用，缺了整条路径静默失效，"
                    "拒绝启动")


def _run_migrations(conn):
    """按 PRAGMA user_version 顺序执行未应用的迁移，链尾做完整性 fail-closed 校验。

    核心迁移失败会抛出异常（init_db 会关闭连接并阻断启动），包括核心迁移抛
    MigrationDeferred；可选迁移失败/延后时先回滚该迁移的部分写入，再置 blocked
    并 continue，后续迁移照常执行，但 blocked 期间任何迁移都不提升 user_version，
    下次启动重试。成功提升与写 `schema_migrations` 记录在**同一事务**——版本推进
    自此与迁移完成互相见证，链尾 `_verify_migration_integrity` 兜底点名。

    执行迁移函数期间 `_framework_txn` 置真：每一个提交点（`_ensure_column` /
    `_ensure_index` / 各 `migrate_*` 体内）都不再提前 commit，"BEGIN IMMEDIATE 包整段
    迁移"的原子性承诺自此成立——半途失败整体回滚，不留半列半表。迁移体内不得再出现
    裸 `conn.commit()`（守卫：tests/test_migrations_schema_drift.py::NoBareCommitGuardTest）。
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    _create_schema_migrations(conn)
    _backfill_inherited_records(conn, version)
    conn.commit()
    blocked = False
    global _framework_txn
    for target_version, name, fn, is_core in _MIGRATIONS:
        if version >= target_version:
            continue
        try:
            # 单个迁移全程持库级写锁：
            # migrate_v5 的表重建是 CREATE → INSERT → DROP → RENAME 四条 DDL，
            # 而 Python sqlite3 对 DDL 不开隐式事务，每条语句各自 autocommit——
            # 在 DROP TABLE users 与 RENAME 之间，其他连接执行
            # SELECT ... FROM users 会直接报 "no such table: users"。该窗口在
            # SSD 上是微秒级，但容器首启并发 / 网络盘 / 大表时完全可命中，
            # 且若迁移在窗口中失败，核心迁移会一并阻断进程启动。
            # 包进 BEGIN IMMEDIATE 后整段迁移原子，中间态对外不可见。
            _facade()._begin_immediate(conn)
            _framework_txn = True
            try:
                fn(conn)
                if blocked:
                    # 不提升 user_version：后续启动会重跑本迁移（实现必须幂等）。
                    # 显式提交：不提交则改动滞留在未决事务中，是否被后续某次 commit
                    # 带走取决于执行顺序；幂等迁移下显式提交可预期。
                    # blocked 路径**不写记录**——记录只见证"版本提升发生过"。
                    conn.commit()
                    logger.info("schema 迁移已执行（blocked，不提升版本）: %s", name)
                else:
                    # 迁移体的提交点全部走 `_commit_if_free`，框架的 BEGIN IMMEDIATE 到
                    # bump 时**仍然开着**：迁移 DDL、完成记录、user_version 三步同事务、
                    # 同一次 commit 原子生效，半途失败整体回滚——版本与 schema 不脱节。
                    # 记录仍先写、版本后拨：直调路径（`_framework_txn` 为假）里迁移体已经
                    # 自己提交过，而默认隔离级别下 PRAGMA 不自开事务——放前面就按
                    # autocommit 立刻落盘，与随后 INSERT+commit 之间崩溃即留下"版本已
                    # 提升、记录没写上"的库态，下次启动被完整性门拒启且回填救不回
                    # （= 人工干预，破红线）。INSERT 先执行则隐式开事务，PRAGMA 落在事务内
                    # 与记录**同 commit 原子生效、同 rollback 一起消失**（反例见
                    # tests/test_migrations_fail_closed.py::BumpRecordAtomicityTest）。
                    conn.execute(
                        f"INSERT OR REPLACE INTO {_SCHEMA_MIGRATIONS_TABLE} "
                        "(version, name, applied_at) VALUES (?, ?, ?)",
                        (target_version, name, clock.ts()),
                    )
                    conn.execute(f"PRAGMA user_version = {target_version}")
                    conn.commit()
                    version = target_version
                    logger.info("schema 迁移完成: %s (user_version=%d)", name, target_version)
            except Exception:
                with contextlib.suppress(Exception):
                    conn.rollback()
                raise
            finally:
                _framework_txn = False
        except MigrationDeferred as e:
            if is_core:
                logger.error("核心 schema 迁移延后: %s: %s", name, e)
                raise
            with contextlib.suppress(Exception):
                conn.rollback()
            logger.warning("可选 schema 迁移延后: %s: %s", name, e)
            blocked = True
        except Exception as e:
            if is_core:
                logger.error("核心 schema 迁移失败: %s: %s", name, e)
                raise
            with contextlib.suppress(Exception):
                conn.rollback()
            logger.warning("可选 schema 迁移失败: %s: %s，继续后续迁移", name, e)
            blocked = True
    # 到这里每一条分支都提交或回滚过 ⇒ 事务已关。补一次 v18 的耐久级 PRAGMA：
    # SQLite 不允许在事务内改 synchronous，框架路径下 migrate_v18 末尾那条被跳过
    # （见其条件）。这是连接级设置、幂等，语义与 v18 内联那条一致。
    conn.execute("PRAGMA synchronous = FULL")
    _verify_migration_integrity(conn)


# ---------------------------------------------------------------------------
# JSON → SQLite 自动导入（幂等）
# ---------------------------------------------------------------------------
def _in_chunks(items, size=500):
    """把序列切成不超过 `size` 的定长块（绑定变量上限远小于源文件可能的行数）。"""
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _dropped_source_keys(conn, table, column, keys):
    """哪些源键**至少丢了一行** —— 即被 `INSERT OR IGNORE` 静默丢弃的那些行的标识。

    为什么要在**事务内**反查而不是只比计数：`INSERT OR IGNORE` 不报错、不留任何痕迹，
    唯一能拿回"丢的是谁"的办法就是拿源键回库比对。分批 `IN` 是为了避开 SQLite 的
    绑定变量上限（老版本 999），源文件几百上千行是常态。

    两类丢行都要盖住——**只比"库里有没有"会漏掉最常见的那一半**：

    - **键在源里重复**（`accounts.phone` UNIQUE、`idx_users_email_live`）：第一条插进去
      了，同键的后续行被丢，于是该键**在库里查得到**，纯差集查出空列表、告警里只会写
      "无法定位"。必须靠源内重复计数识别。
    - **整行被别的唯一索引挡下**（`idx_accounts_owner_live` 的 owner 部分唯一）：行没
      插进去，该键在库里查不到。
    """
    keys = [str(k or "") for k in keys]
    seen, duplicated = set(), set()
    for k in keys:
        if k in seen:
            duplicated.add(k)
        seen.add(k)
    present = set()
    for chunk in _in_chunks(sorted(seen)):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT {column} FROM {table} WHERE {column} IN ({placeholders})",
            tuple(chunk)).fetchall()
        present.update(r[0] for r in rows)
    lost = duplicated | (seen - present)
    return [k for k in keys if k in lost]


def _audit_json_import_loss(conn, losses):
    """把"导入丢行"写进审计链（**同事务**）。失败只告警，绝不拖累迁移本身。

    与业务写同事务的理由同 `audit_chain.record_in_txn` 的模块说明：中间被杀不会留下
    "丢了行却无留痕"。`detail` 截断在 200 字符（审计表列宽），手机号按掩码形态写入。
    审计表尚未落地（极早期的库）时只记日志，不抛——留痕是增益，不能反过来把迁移搞挂。
    """
    detail = "；".join(
        f"{table} 丢 {len(keys)} 行（{_mask_key_sample(keys)}）" for table, keys in losses)
    try:
        _facade().record_in_txn(conn, "system", "json_import_row_loss",
                                target=_connection._env_file or "", detail=detail)
    except Exception as e:
        logger.error("写入「JSON 导入丢行」审计失败（迁移本身继续）: %s", e)


def _mask_key_sample(keys, limit=5):
    """冲突键的日志/审计用摘要：逐个掩码 + 截断（手机号/邮箱不整体外泄）。"""
    masked = [mask_phone(k) if k.isdigit() else _sanitize_text(k)[:24] for k in keys[:limit]]
    more = f" 等 {len(keys)} 项" if len(keys) > limit else ""
    return "、".join(masked) + more


def _maybe_migrate(conn, json_base):
    """json_base 形如 /path/accounts.json（users.json 同目录推断）。

    读取 accounts/users 两个 JSON 后在**一个事务**内导入，逐文件核对"实际插入数 vs
    源行数"：**少一行即 fail-closed —— 该源文件不改名**，并把冲突行写进审计链。

    **为什么不一致就改名 .bak**（M22）：`INSERT OR IGNORE` 撞唯一约束时不报错、不留痕
    （`accounts.phone` UNIQUE、`idx_accounts_owner_live` 的 owner 部分唯一、
    `idx_users_email_live` 的 email 部分唯一都会触发），行被静默丢掉；随后仍把源文件
    改名 `.bak` 就成了**丢数据不留痕**——管理员看到"迁移完成"，源文件已不在原路径，
    丢掉的账号再也找不回来。故丢行时保留源文件原样，让它继续是那条"数据还在"的证据。

    丢行时**已成功插入的行照常提交**（它们本身合法，唯一性冲突的是另一批行），审计行
    与之同事务落库。代价要说清：下一轮 `init_db` 的 `has_db_rows` 会为真而整段跳过自动
    导入，剩余行需管理员按审计里的键手工处理——这正是"保留源文件"要留给人的入口。

    某个 JSON 读取失败/不存在时跳过该文件，不阻断另一个成功导入（与既有行为一致）。
    """
    accounts_json = json_base if json_base.endswith("accounts.json") else os.path.join(
        os.path.dirname(json_base), "accounts.json"
    )
    users_json = os.path.join(os.path.dirname(accounts_json), "users.json")
    has_db_rows = (
        conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] > 0
        or conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0
    )
    if has_db_rows:
        return  # 已迁移过
    accounts = []
    users = []
    load_errors = []
    if os.path.exists(accounts_json):
        try:
            with open(accounts_json, encoding="utf-8") as f:
                accounts = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            accounts = []
            load_errors.append(accounts_json)
            logger.error(
                "账号数据文件存在但读取/解析失败，未迁移（文件保留原样，请手工检查）: %s [%s: %s]",
                accounts_json, type(e).__name__, e,
            )
        if not isinstance(accounts, list):
            accounts = []
    if os.path.exists(users_json):
        try:
            with open(users_json, encoding="utf-8") as f:
                users = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            users = []
            load_errors.append(users_json)
            logger.error(
                "用户数据文件存在但读取/解析失败，未迁移（文件保留原样，请手工检查）: %s [%s: %s]",
                users_json, type(e).__name__, e,
            )
        if not isinstance(users, list):
            users = []
    if not accounts and not users:
        if load_errors:
            logger.error("存在无法读取的数据文件，本次未完成迁移（请勿误判为无数据）: %s",
                         ", ".join(load_errors))
        else:
            logger.info("SQLite 初始化完成（无 JSON 数据可迁移）")
        return
    imported_accounts = 0   # 逐文件计数：合并成一个数就分不出"是哪个文件丢的行"
    imported_users = 0
    key = account_crypto.load_key(_connection._env_file) if accounts else None
    had_plaintext = False  # 迁移源含明文字段 → .bak 逃生门需重写为加密版
    with _facade()._conn_lock, conn:
        if accounts:
            # 加密字段统一为库内 JSON 串格式：
            #   明文 str → 加密（复用 account_crypto）；
            #   密文 dict（0.16 JSON 嵌套对象）→ json.dumps 序列化；
            #   密文 JSON 串 → 原样。
            for i, a in enumerate(accounts):
                password = a.get("password", "") or ""
                phone_code = a.get("phone_code", "") or ""
                if key is not None:
                    if password and not _accounts._is_encrypted_value(password):
                        had_plaintext = True
                        password = json.dumps(account_crypto.encrypt_password(password, key, a.get("phone", "")))
                    elif isinstance(password, dict):
                        password = json.dumps(password)  # 已是密文对象 → 序列化入库
                    if phone_code and not _accounts._is_encrypted_value(phone_code):
                        had_plaintext = True
                        phone_code = json.dumps(account_crypto.encrypt_password(phone_code, key, a.get("phone", "")))
                    elif isinstance(phone_code, dict):
                        phone_code = json.dumps(phone_code)
                cur = conn.execute(
                    "INSERT OR IGNORE INTO accounts "
                    "(sort_order, name, phone, password, phone_model, phone_code, owner, status, reject_reason, deleted, deleted_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        i + 1,
                        a.get("name", ""),
                        a.get("phone", ""),
                        password,
                        a.get("phone_model", ""),
                        phone_code,
                        a.get("owner", "admin"),
                        a.get("status", "active"),
                        a.get("reject_reason", ""),
                        1 if a.get("deleted") else 0,
                        a.get("deleted_at", ""),
                    ),
                )
                imported_accounts += cur.rowcount
        if users:
            for u in users:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO users (email, password_hash, role, created_at, pw_version) VALUES (?,?,?,?,?)",
                    (
                        u.get("email", ""),
                        u.get("password_hash", ""),
                        u.get("role", "user"),
                        u.get("created_at", ""),
                        u.get("pw_version", 1),
                    ),
                )
                imported_users += cur.rowcount
        # ---- 同一事务内逐文件核对：实际插入数 vs 源行数（M22 fail-closed）----
        # `INSERT OR IGNORE` 撞唯一约束时 rowcount 为 0 且不留任何痕迹。少一行就是
        # 静默丢数据，此时**不改名源文件**（它仍是"数据还在"的证据）并写审计告警。
        losses = []
        if accounts and imported_accounts < len(accounts):
            lost = _dropped_source_keys(conn, "accounts", "phone",
                                        [a.get("phone", "") for a in accounts])
            losses.append(("accounts", lost))
        if users and imported_users < len(users):
            lost = _dropped_source_keys(conn, "users", "email",
                                        [u.get("email", "") for u in users])
            losses.append(("users", lost))
        if losses:
            _audit_json_import_loss(conn, losses)
    # 事务已提交（含审计行）。只有"逐文件零丢行"才改名：`.bak` 是逃生门，源文件一旦
    # 离开原路径，丢掉的行就再也找不回来——所以丢行时必须把它留在原处。
    if losses:
        for table, keys in losses:
            logger.error(
                "SQLite 自动迁移在 %s 上丢行：源 %d 行 / 实际插入 %d 行，"
                "冲突键 %s。**该源 JSON 未改名**（保留原路径作为数据仍在的证据）；"
                "已成功插入的行与本次审计行已提交。请按上述键手工处理源文件后重新导入"
                "（注意：库已有行，后续启动不会再自动导入）",
                table,
                len(accounts) if table == "accounts" else len(users),
                imported_accounts if table == "accounts" else imported_users,
                _mask_key_sample(keys) or "（无法定位，疑似空键）",
            )
    # 改名**逐文件**判定：只有自己零丢行的那个文件才走 .bak 逃生门。合并成"任一文件
    # 丢行就都不改名"会让一个账号文件的重复手机号把用户文件也钉在原路径（凭空多出一份
    # 没人认领的副本），反过来同理。
    lost_tables = {table for table, _ in losses}
    if accounts and "accounts" not in lost_tables:
        _rename_backup(accounts_json, reencrypt=had_plaintext, key=key)
    if users and "users" not in lost_tables:
        _rename_backup(users_json)
    logger.info("SQLite 自动迁移完成：导入 %d 条记录（JSON 已改名 .bak 保留逃生门）",
                imported_accounts + imported_users)


def _rename_backup(path, reencrypt=False, key=None):
    """JSON 迁移成功后改名保留（逃生门），避免被旧代码误写回。

    .bak 一律落 0600；迁移源含明文字段时（更早格式/手工构造/第三方导出），重写 .bak
    为加密版，杜绝明文凭据以 .bak 形态驻留磁盘。
    同日已有同名 .bak 时追加递增序号，确保源文件总能离开原路径——此前目标已存在
    即跳过 os.rename，会让含明文的源 JSON 以原文件名无限期驻留。
    """
    if not os.path.exists(path):
        return
    bak = f"{path}.bak-{clock.now().strftime('%Y%m%d')}"
    if os.path.exists(bak):
        seq = 1
        while os.path.exists(f"{bak}-{seq}"):
            seq += 1
        new_bak = f"{bak}-{seq}"
        logger.warning("迁移备份目标 %s 已存在，源文件改存为 %s", bak, new_bak)
        bak = new_bak
    os.rename(path, bak)
    try:  # 尽力而为、不阻断迁移，但失败必须出声（MF-40：.bak 里可能是明文凭据）
        os.chmod(bak, 0o600)
    except OSError as e:  # 非 POSIX 平台或权限受限
        logger.error("迁移备份权限收紧失败（.bak 可能仍组/其他可读，请手工检查）: %s [%s]",
                     bak, e)
    if reencrypt and key is not None:
        try:
            with open(bak, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                changed = False
                for a in data:
                    pwd = a.get("password", "") or ""
                    if pwd and not _accounts._is_encrypted_value(pwd):
                        a["password"] = json.dumps(
                            account_crypto.encrypt_password(pwd, key, a.get("phone", ""))
                        )
                        changed = True
                    code = a.get("phone_code", "") or ""
                    if code and not _accounts._is_encrypted_value(code):
                        a["phone_code"] = json.dumps(
                            account_crypto.encrypt_password(code, key, a.get("phone", ""))
                        )
                        changed = True
                if changed:
                    tmp = bak + ".tmp" + str(os.getpid())
                    # 权限先于内容（MF-40 修法 5）：旧写法 open(tmp,"w") 先按 umask
                    # 建出 0644 的文件、写完才 chmod——两步之间崩溃/失败即留下
                    # **永久**的组/其他可读明文残留。O_CREAT 直接把建文件权限
                    # 定死 0600（umask 只能收紧、放不大创建位），失败路径清 tmp。
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(tmp)  # 陈旧同名 tmp 的权限不可信，先清
                    try:
                        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        with os.fdopen(fd, "w", encoding="utf-8") as f:
                            json.dump(data, f, ensure_ascii=False)
                        os.replace(tmp, bak)
                    except (OSError, ValueError, TypeError):
                        with contextlib.suppress(OSError):
                            os.unlink(tmp)
                        raise
                    logger.warning("迁移 .bak 含明文字段，已重写为加密版（%s）", bak)
        except (OSError, ValueError, TypeError):
            logger.warning("迁移备份重写加密失败（.bak 保持原样，请手工检查权限）: %s", bak)
