# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""SQLite 数据访问层（web / signin / tui 三进程共用）。

- accounts/users 数据从 JSON 整文件读写迁移到 SQLite（yiban.db，WAL 模式）
  ——根治并发覆盖 / 索引漂移 / 进程外覆盖三个历史问题
- 稳定 ID（id PK AUTOINCREMENT）：业务层用 id 寻址，不再受列表顺序漂移影响
- 密码/识别码字段在库内仍为 AES-GCM 密文（复用 account_crypto，解密在 load 时）
- 自动迁移：accounts/users 表为空且对应 JSON 存在 → 导入（幂等）→ JSON 改名 .bak 保留逃生门
- 操作审计：audit() 记录关键管理操作（多管理员追溯）
- 排序：sort_order 升序为签到顺序（移动 = 事务内交换/重排）
"""
import contextlib
import datetime
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time

# 2026-08-16 审查轮：原 5 处函数内 import 上移（account_crypto 不依赖 db，无循环）
import account_crypto
import env_lock
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF

logger = logging.getLogger("yiban.db")

DB_DEFAULT = os.environ.get("YIBAN_DB_FILE", "yiban.db")

# 软删除保留期（2026-08-15 审查统一命名/单位）：天为唯一来源，秒数派生——
# 此前 web(app.py DELETED_RETENTION_DAYS) 与 db 各持一份同名不同单位常量，易改一处漏一处
SOFT_DELETE_RETENTION_DAYS = 7
SOFT_DELETE_RETENTION_SECONDS = SOFT_DELETE_RETENTION_DAYS * 86400


def _normalize_limit(limit, default):
    """把 limit 钳制到 1..1000；非法值回退到默认值。"""
    try:
        return max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        return default


class DuplicatePhoneError(Exception):
    """手机号已存在（accounts.phone 唯一约束冲突）。"""


class DuplicateOwnerError(Exception):
    """该用户已有一个未删除账号（accounts.owner 部分唯一索引冲突）。"""


class MigrationDeferred(Exception):
    """迁移暂缓：本次不应用，下次启动重试（用于可选迁移遇到需人工处理的数据）。"""


class LastAdminError(Exception):
    """注销被拒绝：该用户是最后一个注册管理员（事务内复核，跨进程安全）。"""

# 模块级共享（web 通过环境变量注入路径后调用 init_db）
_conn = None
# RLock：所有读写操作统一串行化（SQLite 连接非线程安全，多线程并发裸 execute
# 会触发 "cannot start a transaction" / InterfaceError misuse——2026-08-15 本地并发验证暴露）
_conn_lock = threading.RLock()
_db_file = DB_DEFAULT
_env_file = None  # .env 路径（密钥来源；None=account_crypto 默认 .env）

# 审计 HMAC 密钥缓存与互斥（Phase 3）
_AUDIT_KEY_CACHE = None
_AUDIT_KEY_LOCK = threading.Lock()

# 可视化表保留期（Phase 4）
SIGN_EVENTS_RETENTION_DAYS = 180
PAGE_VISITS_RETENTION_DAYS = 90
SERVER_METRICS_RETENTION_DAYS = 30

# IP 加盐哈希（Phase 4）
_TRACK_SALT_CACHE = None
_TRACK_SALT_LOCK = threading.Lock()


def init_db(db_file=None, migrate_from=None, env_file=None, cleanup=True):
    """初始化连接与表结构；可选自动迁移（migrate_from 提供 json 文件基路径，如 /path/accounts.json）。

    env_file：.env 路径（加密密钥来源），须与调用方一致（web 用 --env 参数时必传），
    None 时走 account_crypto 默认（.env 于当前工作目录）。
    cleanup：默认 True 执行启动清理（审计/事件旧数据、过期软删用户等）；
    校验类工具应传 False，避免只读校验改变数据。
    """
    global _conn, _db_file, _env_file
    _env_file = env_file
    _db_file = db_file or os.environ.get("YIBAN_DB_FILE", DB_DEFAULT)
    if _conn is not None:
        return _conn
    with _conn_lock:
        if _conn is not None:
            return _conn
        _conn = sqlite3.connect(_db_file, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.execute("PRAGMA foreign_keys=OFF")
        _create_tables(_conn)
        # 通用幂等迁移框架（Phase 0）：按 PRAGMA user_version 顺序执行；
        # 核心迁移失败会关闭连接并抛出，阻断启动；可选迁移失败继续后续迁移但不提升版本。
        try:
            _run_migrations(_conn)
            # 自动迁移（幂等：库存在但空表 + JSON 存在才导入）
            if migrate_from:
                _maybe_migrate(_conn, migrate_from)
        except Exception:
            with contextlib.suppress(Exception):
                _conn.close()
            _conn = None
            raise
        if cleanup:
            run_daily_cleanup()
        return _conn


def get_conn():
    if _conn is None:
        init_db()
    return _conn


def is_initialized():
    """db 层是否已显式初始化（不触发隐式 init_db）。

    供 signin 判断会话缓存可用性：环境变量账号模式（CI 等）未初始化 db，
    不启用缓存——避免 get_conn 隐式 init 在工作目录创建空库。
    """
    return _conn is not None


# ---------------------------------------------------------------------------
# 表结构
# ---------------------------------------------------------------------------
def _create_tables(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          sort_order INTEGER NOT NULL,
          name TEXT NOT NULL DEFAULT '',
          phone TEXT NOT NULL UNIQUE,
          password TEXT NOT NULL DEFAULT '',
          phone_model TEXT NOT NULL DEFAULT '',
          phone_code TEXT NOT NULL DEFAULT '',
          owner TEXT NOT NULL DEFAULT 'admin',
          status TEXT NOT NULL DEFAULT 'pending',
          reject_reason TEXT NOT NULL DEFAULT '',
          deleted INTEGER NOT NULL DEFAULT 0,
          deleted_at TEXT NOT NULL DEFAULT '',
          user_paused INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_accounts_owner ON accounts(owner);
        CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
        CREATE INDEX IF NOT EXISTS idx_accounts_sort ON accounts(sort_order);

        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          email TEXT NOT NULL,
          password_hash TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'user',
          created_at TEXT NOT NULL DEFAULT '',
          pw_version INTEGER NOT NULL DEFAULT 1,
          deleted INTEGER NOT NULL DEFAULT 0,
          deleted_at TEXT NOT NULL DEFAULT '',
          mail_notify INTEGER NOT NULL DEFAULT 1
        );
        -- 注意：idx_users_email_live（依赖 users.deleted）由 migrate_v5 创建，
        -- 不能放在基线建表里——旧库（0.19.8，users 无 deleted 列）升级时会在
        -- 迁移执行前崩溃（对抗审查 2026-08-16 演练发现）。

        CREATE TABLE IF NOT EXISTS audit_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          username TEXT NOT NULL,
          action TEXT NOT NULL,
          target TEXT NOT NULL DEFAULT '',
          detail TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(ts);
        CREATE INDEX IF NOT EXISTS idx_audit_action_target ON audit_logs(action, target, id);

        CREATE TABLE IF NOT EXISTS time_prefs (
          phone TEXT PRIMARY KEY,
          slot_min INTEGER NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_time_prefs_slot ON time_prefs(slot_min);

        CREATE TABLE IF NOT EXISTS user_delete_requests (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT NOT NULL,
          ip_hash TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_user_delete_requests_user ON user_delete_requests(username);
        CREATE INDEX IF NOT EXISTS idx_user_delete_requests_ip ON user_delete_requests(ip_hash);
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 通用幂等迁移框架（Phase 0）
# ---------------------------------------------------------------------------
# 允许操作的表名白名单（防止 f-string SQL 注入）
_ALLOWED_TABLES = {"accounts", "users", "audit_logs", "time_prefs", "user_delete_requests",
                   "sign_events", "page_visits", "server_metrics", "session_cache"}


def _table_columns(conn, table):
    """返回表的所有列名（PRAGMA table_info）。"""
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"非法表名: {table!r}")
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(conn, table, column, definition):
    """缺列才 ALTER TABLE ADD COLUMN（幂等）。"""
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"非法表名: {table!r}")
    # 列名白名单：仅允许字母数字下划线，防止注入
    if not column.isidentifier() or not column.replace("_", "").isalnum():
        raise ValueError(f"非法列名: {column!r}")
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()


def _ensure_index(conn, create_sql):
    """按给定 CREATE INDEX / CREATE UNIQUE INDEX 语句幂等创建（依赖 IF NOT EXISTS）。"""
    conn.execute(create_sql)
    conn.commit()


def migrate_v1(conn):
    """v1：补齐 accounts.user_paused 列（现状基线迁移）。"""
    _ensure_column(conn, "accounts", "user_paused", "user_paused INTEGER NOT NULL DEFAULT 0")


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


def _maybe_add_account_columns(conn):
    """兼容旧入口：v1 迁移的薄封装（行为由 migrate_v1 负责）。"""
    migrate_v1(conn)


# ---------------------------------------------------------------------------
# 审计哈希链（Phase 3）
# ---------------------------------------------------------------------------
def _parse_env_file(env_file):
    """读取 .env 全部键值，返回 dict（文件缺失返回空；非法行跳过）。"""
    result = {}
    try:
        with open(env_file, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    result[key.strip()] = value.strip()
    except FileNotFoundError:
        pass  # 文件确实不存在 → 空配置
    except OSError as e:
        # 文件存在但读取失败：绝不静默当作"未配置"，否则 audit key / track salt
        # 自动生成路径会误判无密钥而重新生成，致既有审计链/追踪盐失效。
        # 宁可启动失败也不生成替代密钥（2026-08-27 审查）。
        logger.error("环境变量文件存在但读取失败，按错误处理而非未配置（请检查权限）: %s [%s]", env_file, e)
        raise
    return result


def _decode_audit_key(raw):
    """把 hex 字符串审计密钥解码为 bytes；格式/长度非法抛 ValueError。"""
    try:
        key = bytes.fromhex(raw)
    except (TypeError, ValueError) as e:
        raise ValueError("YIBAN_AUDIT_KEY 格式非法：应为 64 位十六进制字符串") from e
    if len(key) != 32:
        raise ValueError("YIBAN_AUDIT_KEY 长度非法：应为 32 字节（64 位十六进制）")
    return key


def _write_audit_key_to_env_file(env_file, key):
    """把新生成的审计密钥写入 .env（保留其他行，原子替换，Unix 权限 0600）。

    读-写-替换整体包进共享 env_lock：与 web/tui 写 .env 互斥；锁内仍保留
    “写入前重读”的既有兜底，避免多进程首启竞态覆盖。
    """
    with env_lock.env_write_lock(env_file):
        existing = _parse_env_file(env_file).get("YIBAN_AUDIT_KEY", "").strip()
        if existing:
            return _decode_audit_key(existing)
        lines = []
        if os.path.exists(env_file):
            with open(env_file, encoding="utf-8-sig") as f:
                lines = f.read().splitlines()
        out = [ln for ln in lines if not ln.strip().startswith("YIBAN_AUDIT_KEY=")]
        out.append(f"YIBAN_AUDIT_KEY={key.hex()}")
        tmp = f"{env_file}.tmp{secrets.token_hex(4)}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, env_file)
        with contextlib.suppress(OSError):
            os.chmod(env_file, 0o600)
        return key


def _audit_key(create=True):
    """获取审计 HMAC 密钥：环境变量 YIBAN_AUDIT_KEY 优先，回退 .env。

    create=True（默认）时缺失会生成并写入 .env；create=False 供只读校验，
    密钥缺失返回 None，由调用方按 fail-closed 处理。
    """
    global _AUDIT_KEY_CACHE
    env_file = _env_file or ".env"
    env_key = os.environ.get("YIBAN_AUDIT_KEY", "").strip()
    if env_key:
        _AUDIT_KEY_CACHE = _decode_audit_key(env_key)
        return _AUDIT_KEY_CACHE
    if _AUDIT_KEY_CACHE is not None:
        return _AUDIT_KEY_CACHE
    with _AUDIT_KEY_LOCK:
        if _AUDIT_KEY_CACHE is not None:
            return _AUDIT_KEY_CACHE
        file_key = _parse_env_file(env_file).get("YIBAN_AUDIT_KEY", "").strip()
        if file_key:
            _AUDIT_KEY_CACHE = _decode_audit_key(file_key)
            return _AUDIT_KEY_CACHE
        if not create:
            return None
        logger.info("未找到 YIBAN_AUDIT_KEY，已生成新密钥并写入 %s（chmod 600）", env_file)
        _AUDIT_KEY_CACHE = _write_audit_key_to_env_file(env_file, secrets.token_bytes(32))
        return _AUDIT_KEY_CACHE


def _audit_hash(prev_hash, ts, username, action, target, detail):
    """计算单条审计日志的 HMAC-SHA256 哈希。"""
    payload = json.dumps(
        [prev_hash, ts, username, action, target, detail],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hmac.new(_audit_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _rechain_audit_logs(conn):
    """按 id 升序重建审计哈希链（从空 prev_hash 开始）。"""
    rows = conn.execute(
        "SELECT id, ts, username, action, target, detail FROM audit_logs ORDER BY id LIMIT 10000"
    ).fetchall()
    prev = ""
    for r in rows:
        h = _audit_hash(prev, r["ts"], r["username"], r["action"], r["target"], r["detail"])
        conn.execute(
            "UPDATE audit_logs SET prev_hash=?, hash=? WHERE id=?",
            (prev, h, r["id"]),
        )
        prev = h
    conn.commit()


def migrate_v3(conn):
    """v3：审计日志加 prev_hash/hash 列，并对存量数据回填哈希链。"""
    _ensure_column(conn, "audit_logs", "prev_hash", "prev_hash TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "audit_logs", "hash", "hash TEXT NOT NULL DEFAULT ''")
    rows = conn.execute(
        "SELECT id, hash FROM audit_logs ORDER BY id LIMIT 10000"
    ).fetchall()
    if rows and any(r["hash"] == "" for r in rows):
        _rechain_audit_logs(conn)


# ---------------------------------------------------------------------------
# 可视化表（Phase 4）
# ---------------------------------------------------------------------------
def _write_track_salt_to_env_file(env_file, salt):
    """把新生成的 YIBAN_TRACK_SALT 写入 .env（保留其他行，原子替换）。

    读-写-替换整体包进共享 env_lock：与 web/tui 写 .env 互斥；锁内仍保留
    “写入前重读”的既有兜底，避免多进程首启竞态覆盖。
    """
    with env_lock.env_write_lock(env_file):
        existing = _parse_env_file(env_file).get("YIBAN_TRACK_SALT", "").strip()
        if existing:
            return existing
        lines = []
        if os.path.exists(env_file):
            with open(env_file, encoding="utf-8-sig") as f:
                lines = f.read().splitlines()
        out = [ln for ln in lines if not ln.strip().startswith("YIBAN_TRACK_SALT=")]
        out.append(f"YIBAN_TRACK_SALT={salt}")
        tmp = f"{env_file}.tmp{secrets.token_hex(4)}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, env_file)
        with contextlib.suppress(OSError):
            os.chmod(env_file, 0o600)
        return salt


def _track_salt():
    """获取 IP 加盐哈希用的盐：环境变量优先，回退 .env，缺失时生成。"""
    global _TRACK_SALT_CACHE
    env_file = _env_file or ".env"
    env_salt = os.environ.get("YIBAN_TRACK_SALT", "").strip()
    if env_salt:
        _TRACK_SALT_CACHE = env_salt
        return env_salt
    if _TRACK_SALT_CACHE is not None:
        return _TRACK_SALT_CACHE
    with _TRACK_SALT_LOCK:
        if _TRACK_SALT_CACHE is not None:
            return _TRACK_SALT_CACHE
        file_salt = _parse_env_file(env_file).get("YIBAN_TRACK_SALT", "").strip()
        if file_salt:
            _TRACK_SALT_CACHE = file_salt
            return file_salt
        logger.info("未找到 YIBAN_TRACK_SALT，已生成新盐并写入 %s（chmod 600）", env_file)
        _TRACK_SALT_CACHE = _write_track_salt_to_env_file(env_file, secrets.token_hex(32))
        return _TRACK_SALT_CACHE


def hash_ip(ip):
    """对 IP 加盐哈希（YIBAN_TRACK_SALT），返回十六进制字符串。

    2026-08-28 审查 M9：原实现为 `sha256(salt + ":" + ip)` 字符串拼接——
    构造上接近 HMAC 但非标准；改用 HMAC-SHA256(salt, ip)（密钥前向填充，防
    长度扩展类问题）。注意：盐与库同盘时（.env + yiban.db 同时被拿），IPv4
    空间仍可离线枚举还原——本函数用于限速计数/统计，不承担凭据级保密。
    """
    salt = _track_salt()
    return hmac.new(salt.encode("utf-8"), str(ip).encode("utf-8"), hashlib.sha256).hexdigest()


def hash_phone(phone):
    """对手机号做稳定匿名哈希（YIBAN_TRACK_SALT），返回十六进制字符串。

    与审计脱敏不同：同一手机号总是得到相同哈希，可供 time_pref 冷却等
    需要按账号关联审计记录的逻辑使用，同时不把真实手机号写入审计 target。
    格式 sha256(salt:phone)，复用 _track_salt 确保部署内稳定。
    """
    salt = _track_salt()
    return hashlib.sha256(f"{salt}:{phone}".encode("utf-8")).hexdigest()


def migrate_v4(conn):
    """v4：创建可视化三表（可选迁移，失败只告警不阻断启动）。"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sign_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          phone TEXT NOT NULL,
          status TEXT NOT NULL,
          message TEXT NOT NULL DEFAULT '',
          stage TEXT NOT NULL DEFAULT '',
          attempt INTEGER NOT NULL DEFAULT 0,
          account_id INTEGER,
          dur_sec REAL,
          finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sign_events_ts ON sign_events(ts);
        CREATE INDEX IF NOT EXISTS idx_sign_events_phone ON sign_events(phone);
        CREATE INDEX IF NOT EXISTS idx_sign_events_phone_ts ON sign_events(phone, ts);
        CREATE INDEX IF NOT EXISTS idx_sign_events_account_ts ON sign_events(account_id, ts);

        CREATE TABLE IF NOT EXISTS page_visits (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT '',
          path TEXT NOT NULL,
          ip_hash TEXT NOT NULL DEFAULT '',
          ua TEXT NOT NULL DEFAULT '',
          dur_ms INTEGER NOT NULL DEFAULT 0,
          user_id INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_page_visits_ts ON page_visits(ts);
        CREATE INDEX IF NOT EXISTS idx_page_visits_role ON page_visits(role);
        CREATE INDEX IF NOT EXISTS idx_page_visits_path_ts ON page_visits(path, ts);
        CREATE INDEX IF NOT EXISTS idx_page_visits_role_ts ON page_visits(role, ts);

        CREATE TABLE IF NOT EXISTS server_metrics (
          ts TEXT NOT NULL,
          cpu REAL,
          mem_pct REAL,
          disk_pct REAL,
          net_in REAL,
          net_out REAL,
          load1 REAL,
          load5 REAL,
          load15 REAL,
          proc_count INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_server_metrics_ts ON server_metrics(ts);
        """
    )
    conn.commit()


def migrate_v5(conn):
    """v5：用户注销支持——users 增加 deleted/deleted_at，邮箱唯一改为活跃唯一，新增注销请求表。"""
    cols = _table_columns(conn, "users")
    if "deleted" not in cols:
        # 使用 ALTER TABLE ADD COLUMN 而非表重建，避免崩溃窗口数据丢失
        conn.execute("ALTER TABLE users ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE users ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
        conn.commit()
    # 旧库 users.email TEXT UNIQUE 会生成 sqlite_autoindex_users_N 全局唯一索引，
    # 与“同邮箱可有一个活跃 + 多个已注销”的部分唯一索引冲突，必须先移除。
    # SQLite 不允许直接 DROP 与 UNIQUE 列约束关联的自动索引，因此重建 users 表
    # 去掉 email UNIQUE 约束（保留数据）；idx_users_email_live 本身保留不动。
    old_email_indexes = []
    for idx in conn.execute("PRAGMA index_list('users')").fetchall():
        name = idx["name"]
        if name == "idx_users_email_live" or not idx["unique"]:
            continue
        info = conn.execute(f"PRAGMA index_info('{name}')").fetchall()
        if [r["name"] for r in info] == ["email"]:
            old_email_indexes.append(name)
    if old_email_indexes:
        col_list = "id, email, password_hash, role, created_at, pw_version"
        cols = _table_columns(conn, "users")
        if "deleted" in cols:
            col_list += ", deleted"
        if "deleted_at" in cols:
            col_list += ", deleted_at"
        conn.execute(
            "CREATE TABLE users_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "email TEXT NOT NULL, "
            "password_hash TEXT NOT NULL, "
            "role TEXT NOT NULL DEFAULT 'user', "
            "created_at TEXT NOT NULL DEFAULT '', "
            "pw_version INTEGER NOT NULL DEFAULT 1, "
            "deleted INTEGER NOT NULL DEFAULT 0, "
            "deleted_at TEXT NOT NULL DEFAULT ''"
            ")"
        )
        conn.execute(
            f"INSERT INTO users_new ({col_list}) SELECT {col_list} FROM users"
        )
        conn.execute("DROP TABLE users")
        conn.execute("ALTER TABLE users_new RENAME TO users")
        conn.commit()
    _ensure_index(
        conn,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_live "
        "ON users(email) WHERE deleted = 0",
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS user_delete_requests (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT NOT NULL,
          ip_hash TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_user_delete_requests_user ON user_delete_requests(username);
        CREATE INDEX IF NOT EXISTS idx_user_delete_requests_ip ON user_delete_requests(ip_hash);
        """
    )
    conn.commit()


def migrate_v6(conn):
    """v6：WebUI 统计/监控补齐——sign_events 增加 account_id/dur_sec/finished_at，
    page_visits 增加 user_id，并补索引。可选迁移，失败不阻断启动。"""
    _ensure_column(conn, "sign_events", "account_id", "account_id INTEGER")
    _ensure_column(conn, "sign_events", "dur_sec", "dur_sec REAL")
    _ensure_column(conn, "sign_events", "finished_at", "finished_at TEXT")
    _ensure_column(conn, "page_visits", "user_id", "user_id INTEGER")
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
    try:
        conn.execute(
            "UPDATE sign_events SET account_id = ("
            "SELECT id FROM accounts WHERE accounts.phone = sign_events.phone LIMIT 1"
            ") WHERE account_id IS NULL"
        )
        conn.commit()
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.warning("回填 sign_events.account_id 失败: %s", e)


def migrate_v7(conn):
    """v7：注销请求表加 kind 列（delete=注销 / restore=恢复），修复注销记录阻断 60s 内恢复的漏洞。

    2026-08-17 排查复现：注销与恢复共用计数，注销动作本身写入的记录会让
    随后 60 秒内的恢复请求全部 429（真实用户"注销后立即反悔"路径必现）。
    旧数据无 kind → 默认 'delete'（历史记录均为注销）。
    """
    _ensure_column(
        conn, "user_delete_requests", "kind", "kind TEXT NOT NULL DEFAULT 'delete'"
    )


def migrate_v8(conn):
    """v8：会话 Cookie 缓存表（OAuth 会话复用，降低登录频率 = 降低风控触发面）。

    缓存对象为序列化 cookie jar + csrf（login_killyiban 完成后的完整认证态）；
    cookies_ct 为 AES-GCM 密文 JSON 串（AAD=phone，复用 account_crypto），
    库内绝不落明文 cookie。表结构见 docs/research-lumjiel-core-sign-20260822.md §七。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS session_cache (
          phone        TEXT PRIMARY KEY,
          cookies_ct   TEXT NOT NULL,
          csrf         TEXT NOT NULL,
          created_at   TEXT NOT NULL,
          updated_at   TEXT NOT NULL
        );
        -- 应用元数据（2026-08-28 审查 M3）：purge 时钟跳变保护的单调参照等
        CREATE TABLE IF NOT EXISTS app_meta (
          key   TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
    )
    conn.commit()


def migrate_v9(conn):
    """v9：用户邮箱通知开关（users.mail_notify，默认开启接收签到结果邮件）。

    1=接收（默认）；0=关闭（不接收用户签到失败邮件 B 线）。
    管理员告警邮件（A 线）不受此开关影响。旧库补列时默认置 1。
    """
    _ensure_column(conn, "users", "mail_notify", "mail_notify INTEGER NOT NULL DEFAULT 1")


# 迁移项格式：(目标版本号, 名称, 函数, 是否核心)
# - 核心迁移：现有功能依赖，失败应阻断启动。
# - 可选迁移：未来/非关键能力，失败只告警或延后重试。
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
]


def _run_migrations(conn):
    """按 PRAGMA user_version 顺序执行未应用的迁移。

    核心迁移失败会抛出异常（init_db 会关闭连接并阻断启动），包括核心迁移抛
    MigrationDeferred；可选迁移失败/延后时先回滚该迁移的部分写入，再置 blocked
    并 continue，后续迁移照常执行，但 blocked 期间任何迁移都不提升 user_version，
    下次启动重试。
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    blocked = False
    for target_version, name, fn, is_core in _MIGRATIONS:
        if version >= target_version:
            continue
        try:
            # 单个迁移全程持库级写锁（2026-08-28 审查 B-4）：
            # migrate_v5 的表重建是 CREATE → INSERT → DROP → RENAME 四条 DDL，
            # 而 Python sqlite3 对 DDL 不开隐式事务，原实现下每条语句各自
            # autocommit——在 DROP TABLE users 与 RENAME 之间，其他连接执行
            # SELECT ... FROM users 会直接报 "no such table: users"。该窗口在
            # SSD 上是微秒级，但容器首启并发 / 网络盘 / 大表时完全可命中，
            # 且若迁移在窗口中失败，核心迁移会一并阻断进程启动。
            # 包进 BEGIN IMMEDIATE 后整段迁移原子，中间态对外不可见。
            conn.execute("BEGIN IMMEDIATE")
            try:
                fn(conn)
                if blocked:
                    # 不提升 user_version：后续启动会重跑本迁移（实现必须幂等）。
                    # 显式提交（原实现此处未提交，改动滞留在未决事务中，是否被
                    # 后续某次 commit 带走取决于执行顺序——幂等迁移下显式提交可预期）
                    conn.commit()
                    logger.info("schema 迁移已执行（blocked，不提升版本）: %s", name)
                else:
                    conn.execute(f"PRAGMA user_version = {target_version}")
                    conn.commit()
                    version = target_version
                    logger.info("schema 迁移完成: %s (user_version=%d)", name, target_version)
            except Exception:
                with contextlib.suppress(Exception):
                    conn.rollback()
                raise
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


# ---------------------------------------------------------------------------
# 自动迁移（JSON → SQLite，幂等）
# ---------------------------------------------------------------------------
def _maybe_migrate(conn, json_base):
    """json_base 形如 /path/accounts.json（users.json 同目录推断）。

    读取 accounts/users 两个 JSON 后在一个事务内导入，两个都成功后一起改名 .bak；
    某个 JSON 读取失败/不存在时跳过该文件，不阻断另一个成功导入。
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
    imported = 0
    key = account_crypto.load_key(_env_file) if accounts else None
    had_plaintext = False  # 迁移源含明文字段 → .bak 逃生门需重写为加密版（2026-08-27 审查缺口 2）
    with _conn_lock, conn:
        if accounts:
            # 加密字段统一为库内 JSON 串格式：
            #   明文 str → 加密（复用 account_crypto）；
            #   密文 dict（0.16 JSON 嵌套对象）→ json.dumps 序列化；
            #   密文 JSON 串 → 原样。
            for i, a in enumerate(accounts):
                password = a.get("password", "") or ""
                phone_code = a.get("phone_code", "") or ""
                if key is not None:
                    if password and not _is_encrypted_value(password):
                        had_plaintext = True
                        password = json.dumps(account_crypto.encrypt_password(password, key, a.get("phone", "")))
                    elif isinstance(password, dict):
                        password = json.dumps(password)  # 已是密文对象 → 序列化入库
                    if phone_code and not _is_encrypted_value(phone_code):
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
                imported += cur.rowcount
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
                imported += cur.rowcount
    # 事务提交成功后统一改名，避免单个 JSON 导入失败时已把另一个改名
    if accounts:
        _rename_backup(accounts_json, reencrypt=had_plaintext, key=key)
    if users:
        _rename_backup(users_json)
    logger.info("SQLite 自动迁移完成：导入 %d 条记录（JSON 已改名 .bak 保留逃生门）", imported)


def _rename_backup(path, reencrypt=False, key=None):
    """JSON 迁移成功后改名保留（逃生门），避免被旧代码误写回。

    2026-08-27 审查缺口 2：.bak 一律落 0600；迁移源含明文字段时（更早格式/手工构造/
    第三方导出），重写 .bak 为加密版，杜绝明文凭据以 .bak 形态驻留磁盘。
    同日已有同名 .bak 时追加递增序号，确保源文件总能离开原路径——此前目标已存在
    即跳过 os.rename，会让含明文的源 JSON 以原文件名无限期驻留。
    """
    if not os.path.exists(path):
        return
    bak = f"{path}.bak-{datetime.datetime.now().strftime('%Y%m%d')}"
    if os.path.exists(bak):
        seq = 1
        while os.path.exists(f"{bak}-{seq}"):
            seq += 1
        new_bak = f"{bak}-{seq}"
        logger.warning("迁移备份目标 %s 已存在，源文件改存为 %s", bak, new_bak)
        bak = new_bak
    os.rename(path, bak)
    try:
        os.chmod(bak, 0o600)
    except OSError:
        pass  # 非 POSIX 平台或权限受限：尽力而为，不阻断迁移
    if reencrypt and key is not None:
        try:
            with open(bak, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                changed = False
                for a in data:
                    pwd = a.get("password", "") or ""
                    if pwd and not _is_encrypted_value(pwd):
                        a["password"] = json.dumps(
                            account_crypto.encrypt_password(pwd, key, a.get("phone", ""))
                        )
                        changed = True
                    code = a.get("phone_code", "") or ""
                    if code and not _is_encrypted_value(code):
                        a["phone_code"] = json.dumps(
                            account_crypto.encrypt_password(code, key, a.get("phone", ""))
                        )
                        changed = True
                if changed:
                    tmp = bak + ".tmp" + str(os.getpid())
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False)
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, bak)
                    logger.warning("迁移 .bak 含明文字段，已重写为加密版（%s）", bak)
        except (OSError, ValueError, TypeError):
            logger.warning("迁移备份重写加密失败（.bak 保持原样，请手工检查权限）: %s", bak)


# ---------------------------------------------------------------------------
# accounts CRUD（单行操作，事务内）
# ---------------------------------------------------------------------------
def _row_to_account(row, conn=None):
    a = dict(row)
    a["deleted"] = bool(a["deleted"])
    a["user_paused"] = bool(a.get("user_paused", 0))  # 用户自暂停签到（调度 v2）
    # 密文解密（password/phone_code 存 JSON 串；解密失败抛明确错误，绝不静默降级）
    for k in ("password", "phone_code"):
        v = a.get(k)
        if v:
            try:
                obj = json.loads(v)
            except (TypeError, ValueError):
                obj = None
            if isinstance(obj, dict) and "ct" in obj:
                if not account_crypto.has_key(_env_file):
                    raise RuntimeError(
                        "账号已加密但未配置 YIBAN_ACCOUNTS_KEY（请在 .env 配置或恢复密钥备份）"
                    )
                key = account_crypto.load_key(_env_file)
                try:
                    a[k] = account_crypto.decrypt_password(obj, key, a.get("phone", ""))
                except ValueError as e:
                    # 统一收口：解密失败（密钥不匹配/密文损坏）→ RuntimeError，
                    # 与密钥缺失分支一致，由 web 层统一 JSON 错误处理（对抗性审查 L1）
                    raise RuntimeError(str(e)) from e
            else:
                # 明文驻留检测（2026-08-27 审查缺口 1）：非密文值照常使用（不阻断业务），
                # 但必须告警 + 持锁幂等加密回写——堵住"明文已进库"无人察觉；
                # 对照 session_cache 对旧明文行抛错清除（M14），accounts 此前无对应策略。
                phone = str(a.get("phone", ""))
                phone_masked = phone[:3] + "****" + phone[7:] if len(phone) == 11 else phone
                a[k] = v
                if conn is not None:
                    enc = _encrypt_field(v, a.get("phone", ""))
                    conn.execute(
                        f"UPDATE accounts SET {k}=? WHERE id=?", (enc, a["id"])
                    )
                    logger.warning(
                        "账号 %s 的 %s 为明文存储（迁移残留/手工改库/第三方写入），已自动加密回写",
                        phone_masked, k,
                    )
                else:
                    logger.warning(
                        "账号 %s 的 %s 为明文存储；本次读取未持连接上下文，未回写，"
                        "将在下次带连接的读取时自动加密（现有调用方均传连接，此为防御分支）",
                        phone_masked, k,
                    )
    return a


def _is_encrypted_value(v):
    """字段值是否为密文（dict 密文对象，或密文 JSON 串）——迁移/写路径判定用。"""
    if isinstance(v, dict):
        return account_crypto.is_encrypted(v)
    if isinstance(v, str):
        try:
            obj = json.loads(v)
        except (TypeError, ValueError):
            return False
        return account_crypto.is_encrypted(obj)
    return False


def _encrypt_field(value, phone):
    """写库前密文化：dict 密文对象 → JSON 串；其他非空值 → AES-GCM 加密（AAD=phone）→ JSON 串；空值原样。

    无密钥时 load_key 自动生成并持久化（与 web 现状一致）；密钥非法则抛错（绝不静默降级明文）。
    """
    if not value:
        return ""
    if isinstance(value, dict):
        return json.dumps(value)  # 已是密文对象
    key = account_crypto.load_key(_env_file)
    return json.dumps(account_crypto.encrypt_password(str(value), key, phone))


# 时钟跳变保护参数（2026-08-28 审查 M3）：
# 允许的"时间前进"上限。软删保留期 7 天——系统时间被拨快 8 天，刚软删 1 秒的
# 账号会在下次清理时被立即物理清除、7 天反悔窗口归零。取 72h：每日正常运行的
# 服务不会超过；停机 >3 天后的首轮清理会被跳过（代价：清理推迟一天，安全方向）；
# 拨快 >3 天被拦下（防数据丢失，危险方向）。
_CLOCK_ALLOW_FWD_HOURS = 72
# 允许的"时间回拨"上限（秒）：正常 NTP 校正是秒级，回拨超过 1h 视为异常
_CLOCK_ALLOW_BACK_SECONDS = 3600


def _clock_jump_guard(conn, key):
    """以 app_meta 记录的最近一次 seen-now 为参照，检测系统时钟异常跳变。

    返回 (ok, note)：ok=False 时调用方应跳过本次清理并告警（防"拨快后刚软删的
    数据被立即物理清除"）；note 为告警文本或空串。
    每次调用都会把当前时间 upsert 进 app_meta（ok 路径）——该 INSERT 同时充当
    库级写锁（WAL 下 INSERT 即持 RESERVED 锁），调用方无需另开 BEGIN IMMEDIATE。
    """
    now = datetime.datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    if row is None:
        conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)", (key, ts))
        return True, ""
    try:
        last = datetime.datetime.strptime(row["value"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)", (key, ts))
        return True, ""
    fwd = (now - last).total_seconds()
    back = (last - now).total_seconds()
    if fwd > _CLOCK_ALLOW_FWD_HOURS * 3600 or back > _CLOCK_ALLOW_BACK_SECONDS:
        return False, (
            f"系统时间异常跳变（上次记录 {row['value']}，当前 {ts}，"
            f"前进 {fwd / 3600:.1f}h / 回拨 {back / 3600:.1f}h），"
            "已跳过本次物理清理以防误删"
        )
    conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)", (key, ts))
    return True, ""


def _purge_expired_deleted(conn):
    """软删除超过保留期（>= 7 天）的行物理清除（web 原 load 惰性清理语义，库内必有 deleted_at）。

    deleted_at 为 %Y-%m-%d %H:%M:%S 格式（统一写入格式），字符串比较等价时间序。
    清理失败仅告警不阻断（规范审查 D6：原静默吞错无痕迹）。
    """
    try:
        # 2026-08-28 审查 M3：时钟异常跳变（拨快 >72h / 回拨 >1h）时跳过清理，
        # 防"系统时间被拨快后刚软删 1 秒的账号被立即物理清除、反悔窗口归零"。
        # 守卫的 INSERT upsert 在 WAL 下即持 RESERVED 写锁，兼作 M5 的事务边界。
        ok, note = _clock_jump_guard(conn, "purge_accounts_clock")
        if not ok:
            logger.error("%s", note)
            with contextlib.suppress(Exception):
                conn.rollback()
            return
        cutoff = (datetime.datetime.now() - datetime.timedelta(seconds=SOFT_DELETE_RETENTION_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
        # 2026-08-16 优化（性能审查遗留）：先查有无超期行再删——无行时不发 DELETE
        # 事务，只提交守卫的时钟参照一行（每天 1~2 次调用，开销可忽略）
        probe = conn.execute(
            "SELECT 1 FROM accounts WHERE deleted=1 AND deleted_at != '' AND deleted_at <= ? LIMIT 1",
            (cutoff,),
        ).fetchone()
        if not probe:
            conn.commit()
            return
        # 2026-08-28 审查 M5：读 phones → DELETE → 连带清理 整段原子。
        # 原实现 SELECT 与 DELETE 之间跨进程无互斥，期间被管理员恢复的账号
        # （deleted=0）其行会被 WHERE deleted=1 正确保留，但 time_prefs /
        # session_cache 会被陈旧 phones 列表连带误删——用户自选签到时段被静默
        # 重置为自动错峰。守卫 INSERT 已持写锁，事务内重读 phones（不复用事务
        # 前列表），读-删-清对外原子。
        phones = [
            r["phone"]
            for r in conn.execute(
                "SELECT phone FROM accounts WHERE deleted=1 AND deleted_at != '' AND deleted_at <= ?",
                (cutoff,),
            ).fetchall()
        ]
        conn.execute(
            "DELETE FROM accounts WHERE deleted=1 AND deleted_at != '' AND deleted_at <= ?",
            (cutoff,),
        )
        _delete_time_prefs_by_phones(conn, phones)
        _clear_session_cache_by_phones(conn, phones)
        _delete_sign_events_by_phones(conn, phones)  # M2：物理清除时事件连带清理
        conn.commit()
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.warning("清理超期软删除账号失败: %s", e)


def purge_expired_deleted_accounts():
    """物理清除超过保留期的软删除账号（显式调用：init_db 启动清理 / web 每日线程 / signin 启动）。

    2026-08-20 对抗性审查修复（P1）：原实现挂在 load_accounts() 读路径上，任何一次
    列表读取都可能物理删行并使其后所有下标前移——web 层按 idx 寻址的 mutation 在
    管理员持旧视图时会静默命中错误对象（"无人触发的索引漂移"）。移出读路径后，
    清理只发生在显式时机，两次读取之间的列表顺序保持稳定。
    """
    with _conn_lock:
        conn = get_conn()
        _purge_expired_deleted(conn)


def load_accounts():
    """全部账号（按 sort_order 升序），已解密。

    注意：不再在读路径顺带清除超期软删除行（2026-08-20 对抗性审查 P1 修复）——
    读中途物理删行会使 idx 寻址的 mutation 错位命中其他账号；清理改由
    purge_expired_deleted_accounts() 在启动/每日线程/signin 启动时显式执行。
    """
    with _conn_lock:
        conn = get_conn()
        rows = conn.execute("SELECT * FROM accounts ORDER BY sort_order").fetchall()
        try:
            accts = [_row_to_account(r, conn) for r in rows]
        except Exception:
            # 自愈/解密中途抛错（如某行密文损坏）时，前面已执行的成功自愈/解密
            # 会在共享连接上留下未提交的隐式事务；不回滚会让后续所有
            # `BEGIN IMMEDIATE` 写路径报 "cannot start a transaction within a
            # transaction"，夜间事件落库等连锁失效。先回滚清场再原样抛出（2026-08-27）。
            with contextlib.suppress(Exception):
                conn.rollback()
            raise
        # 明文自愈回写持久化（2026-08-27 审查缺口 1）：UPDATE 不改变行数/顺序，
        # 不会引发 idx 寻址漂移；未 commit 会在连接关闭时回滚导致自愈失效
        conn.commit()
        return accts


def load_accounts_raw():
    """全部账号原始行（password/phone_code 保持密文 JSON 串，不解密）。

    供 db_export 等导出场景使用：避免生成明文凭据文件。
    （超期软删行清理已移出读路径，见 load_accounts 注释。）
    """
    with _conn_lock:
        conn = get_conn()
        rows = conn.execute("SELECT * FROM accounts ORDER BY sort_order").fetchall()
        return [{**dict(r), "deleted": bool(r["deleted"])} for r in rows]


def _next_sort_order(conn):
    row = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM accounts").fetchone()
    return row["n"]


def _convert_integrity_error(e):
    """把 sqlite3.IntegrityError 转换为可区分异常；无法识别则原样抛出。"""
    msg = str(e)
    if "accounts.owner" in msg:
        raise DuplicateOwnerError("该用户已有一个未删除账号") from e
    if "accounts.phone" in msg:
        raise DuplicatePhoneError("手机号已存在") from e
    raise e


def add_account(fields):
    """新增账号（fields 为业务层明文 dict），返回新 id。

    敏感字段写库前加密（AAD=手机号）；手机号重复抛 sqlite3.IntegrityError（业务层捕获）。
    BEGIN IMMEDIATE：跨进程（多 worker）并发时提前获取写锁，
    保证 MAX(sort_order)+1 的读与 INSERT 原子（防并发重复排序号）。
    """
    conn = get_conn()
    with _conn_lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO accounts (sort_order, name, phone, password, phone_model, phone_code, owner, status, reject_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    _next_sort_order(conn),
                    fields.get("name", ""),
                    fields.get("phone", ""),
                    _encrypt_field(fields.get("password"), fields.get("phone", "")),
                    fields.get("phone_model", ""),
                    _encrypt_field(fields.get("phone_code"), fields.get("phone", "")),
                    fields.get("owner", "admin"),
                    fields.get("status", "pending"),
                    fields.get("reject_reason", ""),
                ),
            )
            new_id = cur.lastrowid
            conn.commit()
            return new_id
        except sqlite3.IntegrityError as e:
            conn.rollback()
            _convert_integrity_error(e)
        except Exception:
            conn.rollback()
            raise


def update_account(account_id, fields, expect_snapshot=None):
    """更新单行；expect_snapshot 为乐观锁指纹 dict（name/phone/phone_model/status/deleted），不匹配返回 False。

    手机号变更时自动用新手机号重加密 password/phone_code（旧密文 AAD 绑定旧手机号）；
    改 phone 撞 UNIQUE 抛 sqlite3.IntegrityError（业务层捕获）。

    2026-08-28 审查 B-5：整个读-改-写过程纳入 BEGIN IMMEDIATE 事务。
    原实现 SELECT（读行 + 解密）与 UPDATE 之间跨进程无互斥（_conn_lock 仅进程内），
    中间还夹着解密与重加密——两个进程或两个标签页并发编辑同一账号时，后提交者
    静默覆盖前者（实测：A 写 name=FROM_A、B 写 name=FROM_B，双方都收到成功，
    最终只剩 FROM_B，A 的编辑被丢弃且无任何提示）。
    危险的是 web 用户自编辑路径不传 expect_snapshot、且总把 old["password"] 回填，
    覆盖时可能把用户刚改的密码静默回滚。持锁后读-改-写原子，即使调用方
    不传乐观锁指纹，并发也不会丢更新。
    """
    conn = get_conn()

    def _body():
        """读-改-写事务体（在 BEGIN IMMEDIATE 写锁内执行）。

        拆出闭包是为了让事务边界清晰：外层统一 commit/rollback，内层只负责
        读-改-写。IntegrityError 分支需要"回滚主更新 → 重放密文自愈 → 独立提交"，
        故该分支自行控制事务（_convert_integrity_error 必定抛出，不会走到末尾）。
        """
        # 改绑手机号时体内会重新绑定 fields（补齐待重加密的敏感字段）：
        # 不加 nonlocal 会被当作 _body 的局部变量，首次读取即 UnboundLocalError。
        nonlocal fields
        cur = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,))
        row = cur.fetchone()
        if row is None:
            return None if expect_snapshot is not None else False
        cur_a = _row_to_account(row, conn)  # 解密（AAD=库内当前手机号）；明文驻留自动加密回写
        if expect_snapshot is not None:
            snap = {
                "name": cur_a.get("name", ""),
                "phone": cur_a.get("phone", ""),
                "phone_model": cur_a.get("phone_model", ""),
                "status": cur_a.get("status", ""),
                "deleted": bool(cur_a.get("deleted")),
            }
            if snap != expect_snapshot:
                return False  # 已被他人修改（409）
        # 手机号变更且敏感字段未随本次提供 → 用旧手机号解密的明文按新手机号重加密
        new_phone = fields.get("phone")
        if new_phone is not None and new_phone != cur_a.get("phone"):
            # 改绑手机号：旧手机号的会话缓存随之失效（主键/AAD 均按旧号，不复用）
            _clear_session_cache_by_phones(conn, [cur_a.get("phone", "")])
            for k in ("password", "phone_code"):
                if k not in fields:
                    fields = dict(fields)
                    fields[k] = cur_a.get(k, "")  # 已解密明文
        phone_for_aad = new_phone if new_phone is not None else cur_a.get("phone", "")
        sets = []
        vals = []
        for k in ("name", "phone", "phone_model", "owner", "status", "reject_reason", "deleted", "deleted_at"):
            if k in fields:
                sets.append(f"{k}=?")
                vals.append(fields[k])
        for k in ("password", "phone_code"):
            if k in fields:
                sets.append(f"{k}=?")
                vals.append(_encrypt_field(fields[k], phone_for_aad))
        if not sets:
            return True
        vals.append(account_id)
        try:
            conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", vals)
        except sqlite3.IntegrityError as e:
            conn.rollback()
            # 主更新失败回滚会连带撤销 _row_to_account 的明文自愈；凭据不留明文优先，
            # 用已解密值重放自愈并独立提交（幂等，仅原行确为明文时生效）。2026-08-27
            for k in ("password", "phone_code"):
                raw = row[k]
                if raw and not _is_encrypted_value(raw) and cur_a.get(k):
                    conn.execute(
                        f"UPDATE accounts SET {k}=? WHERE id=?",
                        (_encrypt_field(cur_a[k], cur_a.get("phone", "")), account_id),
                    )
            conn.commit()
            _convert_integrity_error(e)
        return True

    with _conn_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = _body()
            conn.commit()
            return result
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise


def set_account_deleted(account_id, deleted, deleted_at=""):
    conn = get_conn()
    with _conn_lock, conn:
        conn.execute(
            "UPDATE accounts SET deleted=?, deleted_at=? WHERE id=?",
            (1 if deleted else 0, deleted_at, account_id),
        )


def purge_account(account_id):
    conn = get_conn()
    with _conn_lock, conn:
        row = conn.execute("SELECT phone FROM accounts WHERE id=?", (account_id,)).fetchone()
        conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        if row is not None:
            _delete_time_prefs_by_phones(conn, [row["phone"]])  # 连带清理自选（调度 v2）
            _clear_session_cache_by_phones(conn, [row["phone"]])  # 连带清理会话缓存
            _delete_sign_events_by_phones(conn, [row["phone"]])  # 连带清理事件（M2）


def update_account_status(account_id, status, reject_reason=None):
    conn = get_conn()
    with _conn_lock, conn:
        if reject_reason is None:
            conn.execute("UPDATE accounts SET status=? WHERE id=?", (status, account_id))
        else:
            conn.execute("UPDATE accounts SET status=?, reject_reason=? WHERE id=?", (status, reject_reason, account_id))


def set_user_paused(account_id, paused):
    """用户自暂停/恢复签到（user_paused 0/1）。"""
    conn = get_conn()
    with _conn_lock, conn:
        conn.execute("UPDATE accounts SET user_paused=? WHERE id=?", (1 if paused else 0, account_id))


def move_account(account_id, direction):
    """direction: -1 上移 / 1 下移。事务内与相邻账号交换 sort_order。"""
    conn = get_conn()
    with _conn_lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id, sort_order FROM accounts WHERE deleted=0 ORDER BY sort_order"
            ).fetchall()
            pos = next((i for i, r in enumerate(rows) if r["id"] == account_id), None)
            if pos is None:
                conn.rollback()
                return False
            target = pos + direction
            if target < 0 or target >= len(rows):
                conn.rollback()
                return False
            a, b = rows[pos]["sort_order"], rows[target]["sort_order"]
            conn.execute("UPDATE accounts SET sort_order=? WHERE id=?", (b, rows[pos]["id"]))
            conn.execute("UPDATE accounts SET sort_order=? WHERE id=?", (a, rows[target]["id"]))
            conn.commit()
            return True
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise


def delete_accounts_by_owner(owner):
    """删除某用户提交的全部易班账号（用户删除/清空账号用，事务内）。返回删除行数。"""
    conn = get_conn()
    with _conn_lock, conn:
        rows = conn.execute("SELECT phone FROM accounts WHERE owner=?", (owner,)).fetchall()
        cur = conn.execute("DELETE FROM accounts WHERE owner=?", (owner,))
        phones = [r["phone"] for r in rows]
        _delete_time_prefs_by_phones(conn, phones)  # 连带清理自选（调度 v2）
        # 2026-08-28 审查 M1 补：原先漏清理会话缓存，已删账号的加密 cookie/csrf
        # 会以 phone 为主键永久驻留（密钥仍在 .env，等同凭据残留）
        _clear_session_cache_by_phones(conn, phones)
        _delete_sign_events_by_phones(conn, phones)  # M2：明文事件连带清理
        return cur.rowcount


def delete_user_with_accounts(email):
    """删除用户及其全部易班账号（单事务，防崩溃窗口数据不一致）。返回删除账号行数。"""
    conn = get_conn()
    with _conn_lock, conn:
        rows = conn.execute("SELECT phone FROM accounts WHERE owner=?", (email,)).fetchall()
        cur = conn.execute("DELETE FROM accounts WHERE owner=?", (email,))
        _delete_time_prefs_by_phones(conn, [r["phone"] for r in rows])  # 连带清理自选（H2 对抗性审查补）
        _clear_session_cache_by_phones(conn, [r["phone"] for r in rows])  # 连带清理会话缓存
        _delete_sign_events_by_phones(conn, [r["phone"] for r in rows])  # M2
        conn.execute("DELETE FROM users WHERE email=?", (email,))
        _delete_user_delete_requests(conn, email)  # M4：冷却计数连带清除
        return cur.rowcount


def replace_accounts(accounts):
    """整表替换（TUI 保存专用）：事务内清空并重插，sort_order=列表顺序 1..N。

    敏感字段密文化同 add_account（AAD=手机号）。
    ⚠️ 整表替换语义：与 web 并发使用时以最后一次保存为准（TUI 与 web 勿同时编辑）。
    不再存在的账号连带清理自选时间片（H2 对抗性审查补：防孤儿 pref 虚高拥挤度）。
    """
    conn = get_conn()
    with _conn_lock, conn:
        old = conn.execute("SELECT phone FROM accounts").fetchall()
        conn.execute("DELETE FROM accounts")
        keep = {a.get("phone", "") for a in accounts}
        # 2026-08-28 审查 M1 补：整表替换时移除的账号原先只清 time_prefs，
        # 漏清会话缓存（凭据残留）。保留的账号不动，避免无谓的重新登录。
        removed = [r["phone"] for r in old if r["phone"] not in keep]
        _delete_time_prefs_by_phones(conn, removed)
        _clear_session_cache_by_phones(conn, removed)
        for i, a in enumerate(accounts):
            try:
                conn.execute(
                    "INSERT INTO accounts (sort_order, name, phone, password, phone_model, phone_code, owner, status, reject_reason, deleted, deleted_at, user_paused) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        i + 1,
                        a.get("name", ""),
                        a.get("phone", ""),
                        _encrypt_field(a.get("password"), a.get("phone", "")),
                        a.get("phone_model", ""),
                        _encrypt_field(a.get("phone_code"), a.get("phone", "")),
                        a.get("owner", "admin"),
                        a.get("status", "active"),
                        a.get("reject_reason", ""),
                        1 if a.get("deleted") else 0,
                        a.get("deleted_at", ""),
                        1 if a.get("user_paused") else 0,
                    ),
                )
            except sqlite3.IntegrityError as e:
                conn.rollback()
                _convert_integrity_error(e)
    return len(accounts)


def batch_account_ops(ops):
    """在一个事务内批量执行账号操作（Phase 1：整体成功或整体回滚）。

    ops 为 (op, params) 列表，op 支持：
      ("update_status", account_id, status, reject_reason)
      ("set_deleted", account_id, deleted, deleted_at)
      ("purge", account_id)
    """
    conn = get_conn()
    with _conn_lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            for op in ops:
                kind = op[0]
                if kind == "update_status":
                    _, account_id, status, reject_reason = op
                    conn.execute(
                        "UPDATE accounts SET status=?, reject_reason=? WHERE id=?",
                        (status, reject_reason, account_id),
                    )
                elif kind == "set_deleted":
                    _, account_id, deleted, deleted_at = op
                    try:
                        conn.execute(
                            "UPDATE accounts SET deleted=?, deleted_at=? WHERE id=?",
                            (1 if deleted else 0, deleted_at, account_id),
                        )
                    except sqlite3.IntegrityError as e:
                        _convert_integrity_error(e)
                elif kind == "purge":
                    _, account_id = op
                    row = conn.execute(
                        "SELECT phone FROM accounts WHERE id=?", (account_id,)
                    ).fetchone()
                    conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
                    if row is not None:
                        _delete_time_prefs_by_phones(conn, [row["phone"]])
                        # 2026-08-28 审查 M1 补：批量彻底删除是 Web 唯一入口，
                        # 原先漏清理会话缓存（凭据残留，详见 delete_accounts_by_owner）
                        _clear_session_cache_by_phones(conn, [row["phone"]])
                        _delete_sign_events_by_phones(conn, [row["phone"]])  # M2
                else:
                    raise ValueError(f"未知批量账号操作: {kind}")
            conn.commit()
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise


# ---------------------------------------------------------------------------
# users CRUD
# ---------------------------------------------------------------------------
def load_users(include_deleted=False):
    """全部用户（默认排除已注销/软删除用户）。"""
    with _conn_lock:
        conn = get_conn()
        if include_deleted:
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM users WHERE deleted=0 ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]


def find_user(email):
    """查找有效（未注销）用户。"""
    with _conn_lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM users WHERE email=? AND deleted=0", (email,)
        ).fetchone()
        return dict(row) if row else None


def find_user_any(email):
    """查找任意用户（含已注销），供恢复/管理排查使用。优先返回活跃用户。"""
    with _conn_lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM users WHERE email=? ORDER BY deleted ASC, id DESC LIMIT 1", (email,)
        ).fetchone()
        return dict(row) if row else None


def filter_mail_notify(emails):
    """过滤出「接收邮件提醒」的邮箱列表（mail_notify=1 或非注册用户默认接收）。

    供 A 线（管理员告警邮件）按收件人个人开关过滤：普通用户/管理员关闭
    mail_notify 后，即使其邮箱位于 YIBAN_MAIL_ADMIN_TO，也不再接收告警邮件。
    内置主管理员（.env 账号，users 表无记录）不受影响，由全局开关
    YIBAN_MAIL_ENABLE 控制；查库异常时按「接收」处理（不误伤收件人）。
    """
    result = []
    for e in emails:
        u = None
        try:
            u = find_user(e)
        except Exception:
            u = None
        if u is None or str(u.get("mail_notify", 1)).strip().lower() in ("1", "true", "on", "yes"):
            result.append(e)
    return result


def admin_mail_recipients(extra_emails=()):
    """A 线告警邮件的完整收件人列表（去重）。

    组成 = ADMIN_TO（.env，经 filter_mail_notify 按个人开关过滤） + 所有
    「开启接收邮件」的管理员用户邮箱（users.role=admin 且 mail_notify=1）。

    这样普通管理员自动获得告警收件权，无需手动加入 YIBAN_MAIL_ADMIN_TO；
    普通管理员关闭 mail_notify 后即从收件人剔除。内置主管理员（.env 账号，
    不在 users 表）由 extra_emails（ADMIN_TO）覆盖。
    """
    recipients = set(filter_mail_notify(extra_emails))
    try:
        with _conn_lock:
            conn = get_conn()
            rows = conn.execute(
                "SELECT email, mail_notify FROM users WHERE role='admin' AND deleted=0"
            ).fetchall()
    except Exception as e:
        rows = []
        # 留痕（2026-08-27 审查）：否则库故障时普通管理员被无声剔出告警收件人，
        # 事后无从回答"为何没人收到告警"（降级仍保留 .env 配置的 ADMIN_TO）
        logger.warning("查询管理员告警收件人失败，仅保留 .env 配置的收件人: %s", e)
    for r in rows:
        if str(r["mail_notify"] if r["mail_notify"] is not None else 1).strip().lower() in ("1", "true", "on", "yes"):
            recipients.add(r["email"])
    return sorted(recipients)


def create_user(email, password_hash, role="user", created_at="", pw_version=1):
    conn = get_conn()
    with _conn_lock, conn:
        # 使用 INSERT ... ON CONFLICT DO NOTHING 并通过 rowcount 判断是否实际创建
        cur = conn.execute(
            "INSERT OR IGNORE INTO users (email, password_hash, role, created_at, pw_version, deleted, deleted_at) "
            "VALUES (?,?,?,?,?,0,'')",
            (email, password_hash, role, created_at, pw_version),
        )
        return cur.rowcount > 0


def update_user(email, fields):
    """更新用户字段；返回受影响行数。

    2026-08-28 审查 C-M1：原实现返回 None，调用方无法区分"更新成功"与
    "邮箱不存在/已注销"的静默 no-op——邮件通知开关等接口对内置管理员（不在
    users 表）会谎报成功，刷新后开关弹回开启，还污染审计链。返回 rowcount
    供调用方判 404。
    """
    conn = get_conn()
    with _conn_lock, conn:
        sets, vals = [], []
        for k in ("password_hash", "role", "pw_version", "mail_notify"):
            if k in fields:
                sets.append(f"{k}=?")
                vals.append(fields[k])
        if not sets:
            return 0
        vals.append(email)
        cur = conn.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE email=? AND deleted=0", vals
        )
        return cur.rowcount


def delete_user(email):
    conn = get_conn()
    with _conn_lock, conn:
        conn.execute("DELETE FROM users WHERE email=? AND deleted=0", (email,))


# ---------------------------------------------------------------------------
# 用户主动注销（软删除 + 宽限期，Phase 5）
# ---------------------------------------------------------------------------
def soft_delete_user_with_accounts(email):
    """软注销：标记用户 deleted=1，并软删除其易班账号与 time_prefs。

    2026-08-16 安全审查（用户提出错位问题）：账号由物理删除改为软删除，
    与管理员删除账号的 7 天保留语义对齐——宽限期内 restore_user 可完整恢复
    （用户 + 账号）；软删账号不参与签到（signin _load_accounts_from_file 过滤 deleted）。

    2026-08-28 审查 C-M3：'最后一个注册管理员不可注销'的复核从 web 进程内锁
    下沉到本事务内——原实现 web 层用进程内 _file_lock 检查后调用本函数，TUI /
    多容器共享同一库时两名管理员可同时通过检查双双注销，系统失去全部管理
    入口（内置管理员未配置时彻底无法进入）。现于 BEGIN IMMEDIATE 后 COUNT 复核，
    命中即抛 LastAdminError（web 捕获转 400）。

    返回是否找到并注销了有效用户。
    """
    conn = get_conn()
    with _conn_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT id, role FROM users WHERE email=? AND deleted=0", (email,)
            ).fetchone()
            if row is None:
                with contextlib.suppress(Exception):
                    conn.rollback()
                return False
            if row["role"] == "admin":
                total = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role='admin' AND deleted=0"
                ).fetchone()[0]
                if total <= 1:
                    with contextlib.suppress(Exception):
                        conn.rollback()
                    raise LastAdminError(
                        "该用户是最后一个注册管理员，不可注销（系统需保留至少一个管理入口）"
                    )
            rows = conn.execute(
                "SELECT phone FROM accounts WHERE owner=? AND deleted=0", (email,)
            ).fetchall()
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE accounts SET deleted=1, deleted_at=? WHERE owner=? AND deleted=0",
                (now, email),
            )
            _delete_time_prefs_by_phones(conn, [r["phone"] for r in rows])
            _clear_session_cache_by_phones(conn, [r["phone"] for r in rows])  # 注销后停用会话缓存
            conn.execute(
                "UPDATE users SET deleted=1, deleted_at=? WHERE id=?",
                (now, row["id"]),
            )
            conn.commit()
            return True
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise


def restore_user(email):
    """撤销注销：仅当没有同邮箱活跃用户时，把最近一个已注销用户及其账号恢复。

    2026-08-16 安全审查：联动恢复该用户的软删易班账号（deleted=0），
    保证反悔恢复 = 用户 + 账号完整回来（此前账号在注销时被物理删除，恢复残缺）。

    2026-08-28 审查 M7：检查-写入整体纳入 BEGIN IMMEDIATE 写锁。原实现
    `with _conn_lock, conn:` 下 SELECT 走 autocommit，检查（无活跃用户）与
    UPDATE 之间跨进程无互斥——另一进程并发 create_user（同邮箱）可在检查
    通过后抢注成功，本 UPDATE 撞 idx_users_email_live 唯一索引抛
    IntegrityError，web 侧未捕获 → 500。持锁后并发注册被串行化；若仍撞约束
    （防御纵深）由上层捕获转 409。
    """
    conn = get_conn()
    with _conn_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            active = conn.execute(
                "SELECT id FROM users WHERE email=? AND deleted=0", (email,)
            ).fetchone()
            if active is not None:
                with contextlib.suppress(Exception):
                    conn.rollback()
                return False
            deleted = conn.execute(
                "SELECT id, deleted_at FROM users WHERE email=? AND deleted=1 "
                "ORDER BY id DESC LIMIT 1",
                (email,),
            ).fetchone()
            if deleted is None:
                with contextlib.suppress(Exception):
                    conn.rollback()
                return False
            conn.execute(
                "UPDATE users SET deleted=0, deleted_at='' WHERE id=?",
                (deleted["id"],),
            )
            # 只恢复同一注销事件的账号（deleted_at 与用户行一致），
            # 避免把用户注销后单独软删的其他账号也一起恢复造成 owner 冲突。
            conn.execute(
                "UPDATE accounts SET deleted=0, deleted_at='' "
                "WHERE owner=? AND deleted=1 AND deleted_at=?",
                (email, deleted["deleted_at"]),
            )
            conn.commit()
            return True
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise


def purge_deleted_users(days=None):
    """物理清除超过宽限期的已注销用户（默认取 SOFT_DELETE_RETENTION_DAYS）；失败仅告警。

    2026-08-28 审查 C-1：原默认参数硬编码 `days=7`，与 SOFT_DELETE_RETENTION_DAYS
    （账号侧保留期的唯一事实源）以及 web 侧 DELETE_GRACE_DAYS 形成三份互不相干的
    "7"。运维按注释去调 SOFT_DELETE_RETENTION_DAYS 时，此处仍按 7 天清理，
    而 web 的恢复宽限期又是另一份——三者的错位会造成静默数据丢失（详见
    web/app.py DELETE_GRACE_DAYS 处的说明）。现改为取同一常量。

    2026-08-16 安全审查（用户提出错位问题）：宽限期 3 天 → 7 天，
    与账号软删除保留期（_purge_expired_deleted，7 天）对齐——第 7 天用户与
    账号同天清除，邮箱/手机号同时释放，消除"反悔窗口内资产被抢占"风险。
    """
    days = SOFT_DELETE_RETENTION_DAYS if days is None else days
    try:
        conn = get_conn()
        with _conn_lock, conn:
            # M3 时钟保护：跳变时跳过，防注销宽限期被拨快吞掉
            ok, note = _clock_jump_guard(conn, "purge_users_clock")
            if not ok:
                logger.error("%s", note)
                return
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            # M4a：先清这些已注销用户的冷却计数（明文邮箱随用户行一并释放，
            # 不再驻留 user_delete_requests 至 30 天保留期满）
            conn.execute(
                "DELETE FROM user_delete_requests WHERE username IN ("
                "SELECT email FROM users WHERE deleted=1 AND deleted_at != '' AND deleted_at <= ?)",
                (cutoff,),
            )
            conn.execute(
                "DELETE FROM users WHERE deleted=1 AND deleted_at != '' AND deleted_at <= ?",
                (cutoff,),
            )
            conn.commit()
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.warning("清理已注销用户失败: %s", e)


def purge_deleted_users_hard(emails):
    """管理员手动物理清除指定的已注销用户（2026-08-17 需求：不等 7 天自动清除）。

    安全边界：仅处理 deleted=1 的用户行——传入活跃用户邮箱时该用户被直接跳过
    （管理员误操作/并发注册新同邮箱用户均不可能误删活跃数据）。
    账号行只删除 deleted=1 的软删账号；活跃账号跳过，避免误删用户注销后
    重新添加的账号。单事务连带清理：这些已删账号对应 time_prefs + 用户行。
    返回实际清除的邮箱列表（供调用方审计与回显）。
    """
    if not emails:
        return []
    conn = get_conn()
    purged = []
    with _conn_lock, conn:
        for email in emails:
            row = conn.execute(
                "SELECT id FROM users WHERE email=? AND deleted=1", (email,)
            ).fetchone()
            if row is None:
                continue
            phones = [
                r["phone"]
                for r in conn.execute(
                    "SELECT phone FROM accounts WHERE owner=? AND deleted=1", (email,)
                ).fetchall()
            ]
            conn.execute("DELETE FROM accounts WHERE owner=? AND deleted=1", (email,))
            _delete_time_prefs_by_phones(conn, phones)
            _clear_session_cache_by_phones(conn, phones)  # 连带清理会话缓存
            _delete_sign_events_by_phones(conn, phones)  # M2：事件连带清理
            # 2026-08-20 对抗性审查修复：DELETE 复核 deleted=1——SELECT 与 DELETE 之间
            # 用户可能被并发 restore（跨进程/多 worker），无条件按 id 删会物理删除刚恢复的用户
            cur = conn.execute(
                "DELETE FROM users WHERE id=? AND deleted=1", (row["id"],)
            )
            if cur.rowcount > 0:
                purged.append(email)
                _delete_user_delete_requests(conn, email)  # M4：冷却计数连带清除
    return purged


def purge_old_delete_requests(days=30):
    """物理清除超过保留期的注销请求记录（默认 30 天）；失败仅告警。

    对抗审查 2026-08-16：user_delete_requests 只增不删会无限累积
    （长期使用后 count 查询变慢、库体积膨胀）——启动时随 purge_deleted_users 一并清理。
    """
    try:
        conn = get_conn()
        with _conn_lock, conn:
            # M3 时钟保护：跳变时跳过（该表是冷却计数，误删只影响限速；保守一致）
            ok, note = _clock_jump_guard(conn, "purge_requests_clock")
            if not ok:
                logger.error("%s", note)
                return
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            conn.execute(
                "DELETE FROM user_delete_requests WHERE created_at <= ?",
                (cutoff,),
            )
            conn.commit()
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.warning("清理注销请求记录失败: %s", e)


def run_daily_cleanup():
    """每日定期清理的集中入口（2026-08-28 审查 M6）。

    审计/事件旧数据 + 过期软删账号 + 过期注销用户 + 注销请求记录的清理，
    原先挂在 init_db(cleanup=True) 上——而 signin 子进程每天要跑 2~3 次
    （Docker 调度器首签/补签/探针，宿主 cron 同理），每次都执行一轮
    全表 DELETE + 多个 purge，与 web 的 8 个线程抢库级写锁，是审计写入
    失败（B-1）与陈旧列表误删（M5）的主要诱因。
    现改为：web 每日线程统一调用本函数；signin 侧 init_db(cleanup=False)
    （其 main() 保留对超期软删账号的显式清理，覆盖无 web 的纯 cron 场景）。
    """
    conn = get_conn()
    _audit_cleanup(conn)
    _event_cleanup(conn)
    purge_expired_deleted_accounts()
    purge_deleted_users()
    purge_old_delete_requests()


def record_user_delete_request(username, ip_hash="", kind="delete"):
    """记录一次注销/恢复请求（供冷却/防批量使用；kind: delete=注销 / restore=恢复，v7）。"""
    try:
        with _conn_lock:
            conn = get_conn()
            conn.execute(
                "INSERT INTO user_delete_requests (username, ip_hash, created_at, kind) "
                "VALUES (?,?,?,?)",
                (
                    username or "",
                    ip_hash or "",
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    kind if kind in ("delete", "restore") else "delete",
                ),
            )
            conn.commit()
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.warning("记录注销请求失败: %s", e)


def count_user_delete_requests(username=None, ip_hash=None, since_ts=None, kind=None):
    """统计窗口内注销/恢复请求次数（用户或 IP 维度；kind=None 统计全部，v7）。

    计数类查询 fail-closed：异常包装为 RuntimeError 由上层统一处理。
    """
    try:
        with _conn_lock:
            conn = get_conn()
            sql = "SELECT COUNT(*) FROM user_delete_requests WHERE 1=1"
            params = []
            if username:
                sql += " AND username=?"
                params.append(username)
            if ip_hash:
                sql += " AND ip_hash=?"
                params.append(ip_hash)
            if since_ts:
                sql += " AND created_at >= ?"
                params.append(since_ts)
            if kind:
                sql += " AND kind=?"
                params.append(kind)
            row = conn.execute(sql, params).fetchone()
            return row[0] if row else 0
    except Exception as e:
        raise RuntimeError(f"统计注销请求失败: {e}") from e


def is_last_registered_admin(email):
    """判断该邮箱是否是最后一个注册管理员（不含 .env 内置管理员）。"""
    with _conn_lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND deleted=0"
        ).fetchone()
        total = row[0] if row else 0
        target = conn.execute(
            "SELECT id FROM users WHERE email=? AND role='admin' AND deleted=0",
            (email,),
        ).fetchone()
        return target is not None and total <= 1


def batch_user_ops(ops):
    """在一个事务内批量执行用户操作（Phase 1：整体成功或整体回滚）。

    ops 为 (op, params) 列表，op 支持：
      ("update_user", email, fields_dict)          # role/password_hash/pw_version
      ("delete_user_with_accounts", email)
    """
    conn = get_conn()
    with _conn_lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            for op in ops:
                kind = op[0]
                if kind == "update_user":
                    _, email, fields = op
                    sets, vals = [], []
                    for k in ("password_hash", "role", "pw_version"):
                        if k in fields:
                            sets.append(f"{k}=?")
                            vals.append(fields[k])
                    if not sets:
                        continue
                    vals.append(email)
                    conn.execute(
                        f"UPDATE users SET {', '.join(sets)} WHERE email=? AND deleted=0",
                        vals,
                    )
                elif kind == "delete_user_with_accounts":
                    _, email = op
                    rows = conn.execute(
                        "SELECT phone FROM accounts WHERE owner=?", (email,)
                    ).fetchall()
                    conn.execute("DELETE FROM accounts WHERE owner=?", (email,))
                    phones = [r["phone"] for r in rows]
                    _delete_time_prefs_by_phones(conn, phones)
                    # 2026-08-28 审查 M1 补：与 delete_user_with_accounts 对齐，
                    # 批量路径同样漏了会话缓存清理（凭据残留）
                    _clear_session_cache_by_phones(conn, phones)
                    _delete_sign_events_by_phones(conn, phones)  # M2
                    conn.execute("DELETE FROM users WHERE email=?", (email,))
                    _delete_user_delete_requests(conn, email)  # M4
                else:
                    raise ValueError(f"未知批量用户操作: {kind}")
            conn.commit()
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise


# ---------------------------------------------------------------------------
# 操作审计
# ---------------------------------------------------------------------------
# 审计写入失败计数（进程内累计）。审计是唯一追溯凭据，写入失败若无人察觉，
# 就会出现"业务操作已生效、审计表里却没有这条记录"且哈希链依然自洽的静默丢失。
# 全仓 db.audit() 调用点众多且不检查返回值，故用此计数器兜底：由每日校验
# （web 每日线程 / audit_verify.py）读取并告警，无需逐调用点改造。
_AUDIT_FAIL_COUNT = 0
_AUDIT_FAIL_LOCK = threading.Lock()
# 审计写入重试（2026-08-28 审查 B-1）：锁竞争时的短暂失败值得重试
_AUDIT_RETRIES = 3
_AUDIT_RETRY_BASE_DELAY = 0.2


def audit_write_failures():
    """返回本进程累计的审计写入失败次数（供每日校验告警；0 = 无欠账）。"""
    with _AUDIT_FAIL_LOCK:
        return _AUDIT_FAIL_COUNT


def audit(username, action, target="", detail=""):
    """记录关键管理操作（多管理员追溯；detail 需已脱敏）。

    Phase 3：写入 HMAC 哈希链，prev_hash 取上一条 hash；签名保持不变。
    2026-08-20 对抗性审查修复：prev_hash 读取纳入 BEGIN IMMEDIATE 写事务——
    原实现"读上一条 hash"与 INSERT 之间无跨进程互斥（_conn_lock 仅进程内），
    web 与 TUI 并发写审计会读到同一 prev_hash 造成链分叉（verify 断链）。

    2026-08-28 审查 B-1（fail-loud）：原实现 `except Exception` 后只写一条
    WARNING 并返回 None——锁等待超过 busy_timeout 时（长事务如 replace_accounts
    整表重插、夜间批量签到与 web 争锁）业务接口照常返回 200，审计表里却没有
    这条记录。因为是"没写进去"而非"写完被删"，哈希链依然自洽，verify 永远
    验不出问题。现改为：失败重试 → 仍失败则计 ERROR + 累加失败计数（供每日
    校验告警），并返回 bool 供关键路径在需要时显式判定。

    返回 True 表示已落库；False 表示重试耗尽仍未写入（调用方据此决定是否
    阻断业务）。既有调用点不检查返回值也不会出错，失败会由每日校验兜住。
    """
    global _AUDIT_FAIL_COUNT
    conn = None
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    detail = detail[:200]
    last_err = None
    for attempt in range(_AUDIT_RETRIES):
        try:
            with _conn_lock:
                conn = get_conn()
                # 防御：正常路径所有写操作均已提交（with conn 模式），若前序调用遗留
                # 未提交事务，先提交之——否则 BEGIN IMMEDIATE 会报 "within a transaction"
                if conn.in_transaction:
                    logger.warning("audit 检测到遗留未提交事务，已先行提交")
                    conn.commit()
                # IMMEDIATE：取 prev_hash 前先拿库级写锁，跨进程串行化"读尾→追加"
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT hash FROM audit_logs ORDER BY id DESC LIMIT 1"
                ).fetchone()
                prev_hash = row["hash"] if row else ""
                h = _audit_hash(prev_hash, ts, username, action, target, detail)
                conn.execute(
                    "INSERT INTO audit_logs (ts, username, action, target, detail, prev_hash, hash) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (ts, username, action, target, detail, prev_hash, h),
                )
                conn.commit()
            return True
        except Exception as e:  # noqa: BLE001 —— 审计失败不得影响业务主流程
            last_err = e
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.rollback()
            if attempt < _AUDIT_RETRIES - 1:
                logger.warning(
                    "审计写入失败，%.1fs 后重试（第 %d/%d 次）: %s",
                    _AUDIT_RETRY_BASE_DELAY * (attempt + 1), attempt + 1, _AUDIT_RETRIES, e,
                )
                time.sleep(_AUDIT_RETRY_BASE_DELAY * (attempt + 1))
    with _AUDIT_FAIL_LOCK:
        _AUDIT_FAIL_COUNT += 1
    logger.error(
        "审计写入最终失败（业务操作已生效但无留痕，重试 %d 次）: %s | action=%s target=%s",
        _AUDIT_RETRIES, last_err, action, target,
    )
    return False


def audit_head_hash():
    """返回审计链当前头哈希（空链返回空串）；供外部锚点导出（append-only 日志）。

    2026-08-21 对抗性审查补充：HMAC 链密钥与数据同盘时"整体重算"零成本，
    把链头哈希定期追加到独立文件（web 每日线程写 STATE_DIR/audit-anchor.log），
    使重写库内审计链还需同步篡改锚点文件，外部锚定抬高伪造成本。
    """
    try:
        with _conn_lock:
            conn = get_conn()
            row = conn.execute(
                "SELECT hash FROM audit_logs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row["hash"] if row else ""
    except Exception as e:
        logger.warning("读取审计链头失败: %s", e)
        return ""


def verify_audit_chain():
    """校验审计哈希链。

    把当前表中 id 最小的一行视为链根：首行以自身 prev_hash 为锚校验 hash，
    不判首行 prev 断链（清理旧行后 prev_hash 指向已删除的前序行是合法状态）。
    从第二行开始要求 prev_hash 等于上一行 hash，且每行 hash 与内容匹配。

    返回 (ok, broken_count, first_broken_id)；broken_count=-1 表示校验过程异常
    或审计密钥缺失（fail-closed）。
    """
    try:
        key = _audit_key(create=False)
        if key is None:
            logger.warning("审计链校验失败: 未配置 YIBAN_AUDIT_KEY")
            return False, -1, None
        with _conn_lock:
            conn = get_conn()
            rows = conn.execute(
                "SELECT id, prev_hash, hash, ts, username, action, target, detail "
                "FROM audit_logs ORDER BY id"
            ).fetchall()
            if not rows:
                return True, 0, None
            prev = None
            broken = 0
            first_broken = None
            for r in rows:
                # 首行锚点取自身 prev_hash；后续行要求 prev_hash 与上一行 hash 一致
                anchor = r["prev_hash"] if prev is None else prev
                if prev is not None and r["prev_hash"] != prev:
                    broken += 1
                    if first_broken is None:
                        first_broken = r["id"]
                expected_hash = _audit_hash(
                    anchor, r["ts"], r["username"], r["action"], r["target"], r["detail"]
                )
                if r["hash"] != expected_hash:
                    broken += 1
                    if first_broken is None:
                        first_broken = r["id"]
                prev = r["hash"]
            return broken == 0, broken, first_broken
    except Exception as e:
        logger.warning("审计链校验失败: %s", e)
        return False, -1, None


# ---------------------------------------------------------------------------
# 审计链外部锚点（2026-08-28 审查 B-2）
# ---------------------------------------------------------------------------
# verify_audit_chain() 只能检出"删中间"：首行以自身 prev_hash 自锚、空表直接判
# 通过（见其实现）。于是「删前缀 / 删尾 / 清空整表」三类篡改全部验不出来。
#
# 锚点记录 (min_id, max_id, head_hash) 三元组即可覆盖：
#   - 当前 max_id < 锚点 max_id            → 尾部被删（最危险：抹掉最近的记录）
#   - 表空但锚点 max_id > 0                → 整表被清空
#   - max_id 相同但 head_hash 不符          → 链尾内容被篡改
#   - 当前 min_id > 锚点 min_id            → 前缀被删（**不判失败**）
#
# 前缀删除刻意降级为「信息」而非「失败」：_audit_cleanup（保留 180 天）本身就是
# 删前缀的合法路径，判失败会让每日校验天天误报，反而淹没真告警。
#
# 锚点存放在**库外**：若存进 yiban.db，有写库权限者可连同审计表一起删掉，锚点
# 形同虚设。库外文件使"整体重写审计链"还需同步篡改该文件。
def audit_anchor_path():
    """外部锚点文件路径（库外 append-only）。"""
    state_dir = os.environ.get("YIBAN_STATE_DIR") or "."
    return os.path.join(state_dir, "audit-anchor.log")


def record_audit_anchor(path=None):
    """把当前审计链的 (min_id, max_id, head_hash) 追加到外部锚点文件。

    返回写入的锚点行；无审计记录或写入失败返回 None（不阻断调用方）。
    建议在每日清理之后调用，使锚点反映清理后的合法状态。
    """
    path = path or audit_anchor_path()
    try:
        with _conn_lock:
            conn = get_conn()
            row = conn.execute(
                "SELECT MIN(id) AS min_id, MAX(id) AS max_id FROM audit_logs"
            ).fetchone()
        if not row or row["max_id"] is None:
            return None
        min_id, max_id = int(row["min_id"]), int(row["max_id"])
        head = audit_head_hash()
        line = (
            f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
            f"{min_id} {max_id} {head}"
        )
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return line
    except Exception as e:  # noqa: BLE001 —— 锚点写入失败不得阻断业务
        logger.warning("审计链锚点写入失败: %s", e)
        return None


def _last_audit_anchor(path):
    """读取最后一条有效锚点行；无锚点或格式不符返回 None。

    行格式为 `<ts> <min_id> <max_id> <head>`。注意时间戳本身含空格
    （"2026-08-28 18:50:00"），故**从行尾**取固定字段（head / max_id / min_id），
    不能按下标从头取——否则字段会整体错位一格。

    兼容旧格式（仅 `ts head`，2026-08-21 起 web 写入）：旧行尾部只有一个
    非数字段，int() 转换失败即跳过，不参与判定，避免升级后误报。
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except OSError:
        return None
    for ln in reversed(lines):
        parts = ln.split()
        if len(parts) < 4:
            continue
        try:
            return {
                "ts": " ".join(parts[:-3]),
                "min_id": int(parts[-3]),
                "max_id": int(parts[-2]),
                "head": parts[-1],
            }
        except ValueError:
            continue
    return None


def verify_audit_anchor(path=None):
    """与库外锚点比对，检出「删尾 / 清空整表 / 篡改链尾」。

    返回 (ok: bool, message: str)：
    - ok=False → 确证异常，调用方应告警；
    - ok=True 且 message 非空 → 提示性信息（如合法清理造成的前缀回收），记录即可。
    无可用锚点时返回 (True, "")——首次运行或从未记录过锚点不做判定。
    """
    anchor = _last_audit_anchor(path or audit_anchor_path())
    if anchor is None:
        return True, ""
    try:
        with _conn_lock:
            conn = get_conn()
            row = conn.execute(
                "SELECT MIN(id) AS min_id, MAX(id) AS max_id, COUNT(*) AS n FROM audit_logs"
            ).fetchone()
        n = int(row["n"] or 0)
        cur_min = int(row["min_id"]) if row["min_id"] is not None else 0
        cur_max = int(row["max_id"]) if row["max_id"] is not None else 0
        if n == 0:
            return False, (
                f"审计表为空，但锚点（{anchor['ts']}）记录曾有 {anchor['max_id']} 条"
                "——疑似整表被清空"
            )
        if cur_max < anchor["max_id"]:
            return False, (
                f"审计记录条数减少：锚点 max_id={anchor['max_id']}，当前 max_id={cur_max}"
                f"——疑似删除了最近 {anchor['max_id'] - cur_max} 条记录"
            )
        cur_head = audit_head_hash()
        if cur_max == anchor["max_id"] and cur_head != anchor["head"]:
            return False, "审计链头哈希与锚点不符（链尾内容被篡改）"
        if cur_min > anchor["min_id"]:
            return True, (
                f"审计链最早记录由 id={anchor['min_id']} 回收至 {cur_min}"
                "（保留期清理的正常现象，非告警）"
            )
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"锚点校验异常: {e}"


def audit_health(path=None):
    """审计可追溯性综合体检（供每日线程 / audit_verify.py 调用）。

    返回 dict：
      chain_ok      链内哈希自洽（False = 有记录被篡改/删除）
      broken        链内断链条数（-1 = 校验异常或密钥缺失）
      anchor_ok     与库外锚点一致（False = 删尾 / 清空 / 篡改链尾）
      anchor_msg    锚点判定的说明或提示信息
      write_failures 本进程累计的审计写入失败次数（>0 = 有操作未留痕）
      healthy       综合结论（上述全部正常且无写入欠账）
    """
    chain_ok, broken, _first = verify_audit_chain()
    anchor_ok, anchor_msg = verify_audit_anchor(path)
    write_failures = audit_write_failures()
    return {
        "chain_ok": chain_ok,
        "broken": broken,
        "anchor_ok": anchor_ok,
        "anchor_msg": anchor_msg,
        "write_failures": write_failures,
        "healthy": bool(chain_ok and anchor_ok and write_failures == 0),
    }


def last_time_pref_set_at(phone):
    """指定账号最近一次自选时间片保存时间（切换冷却判定用；无记录返回 None）。

    按被选账号（审计 target=hash_phone(phone)（匿名稳定键））而非操作用户计价（H3/H4 对抗性审查）：
    - 多管理员共享 admin 账号时冷却全局生效（管理员 A 保存后 B 立即改选也被拦截）；
    - 改手机号/删号重提交新号后，新 phone 无历史审计 → 不被旧账号冷却误伤。
    """
    try:
        with _conn_lock:
            conn = get_conn()
            target = hash_phone(phone) if phone else phone or ""
            row = conn.execute(
                "SELECT ts FROM audit_logs WHERE action='time_pref_set' AND target=? "
                "ORDER BY id DESC LIMIT 1",
                (target,),
            ).fetchone()
            return row["ts"] if row else None
    except Exception as e:
        raise RuntimeError(f"查询自选保存时间失败: {e}") from e


def time_pref_set_count_since(phone, since_ts):
    """指定账号在 since_ts 之后的保存次数（弹性冷却高频判定用；ts 定宽字符串可比较）。

    审计 target=hash_phone(phone)（匿名稳定键）。
    """
    try:
        with _conn_lock:
            conn = get_conn()
            target = hash_phone(phone) if phone else phone or ""
            row = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='time_pref_set' "
                "AND target=? AND ts >= ?",
                (target, since_ts),
            ).fetchone()
            return row[0] if row else 0
    except Exception as e:
        raise RuntimeError(f"统计自选保存次数失败: {e}") from e


def last_pause_at(username):
    """指定用户最近一次暂停签到时间（暂停冷却判定用；恢复不计，按用户计价）。

    审计 target 为脱敏手机号，故按 username 关联；多管理员共享账号各自独立计价
    （暂停/恢复冷却仅防噪音，绕过危害极小，可接受）。
    """
    try:
        with _conn_lock:
            conn = get_conn()
            row = conn.execute(
                "SELECT ts FROM audit_logs WHERE username=? AND action='my_account_pause' "
                "ORDER BY id DESC LIMIT 1",
                (username or "",),
            ).fetchone()
            return row["ts"] if row else None
    except Exception as e:
        raise RuntimeError(f"查询暂停时间失败: {e}") from e


def pause_count_since(username, since_ts):
    """指定用户在 since_ts 之后的暂停次数（弹性冷却高频判定用）。"""
    try:
        with _conn_lock:
            conn = get_conn()
            row = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE username=? "
                "AND action='my_account_pause' AND ts >= ?",
                (username or "", since_ts),
            ).fetchone()
            return row[0] if row else 0
    except Exception as e:
        raise RuntimeError(f"统计暂停次数失败: {e}") from e


def _audit_cleanup(conn):
    """清理超 180 天审计（启动时顺带，一条 DELETE）。清理失败仅告警（规范审查 D6）。

    Phase 3 修订：删除旧行后不重建哈希链——剩余首行仍保留指向已删前序行的
    prev_hash 作为锚，verify_audit_chain 以该锚校验首行 hash。
    """
    try:
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=180)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("DELETE FROM audit_logs WHERE ts < ?", (cutoff,))
        conn.commit()
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.warning("清理旧审计日志失败: %s", e)


# ---------------------------------------------------------------------------
# 可视化表（Phase 4）
# ---------------------------------------------------------------------------
def add_sign_event(ts, phone, status, message="", stage="", attempt=0,
                   account_id=None, dur_sec=None, finished_at=None):
    """写入签到事件；失败仅告警，不影响调用方。"""
    try:
        with _conn_lock:
            conn = get_conn()
            conn.execute(
                "INSERT INTO sign_events (ts, phone, status, message, stage, attempt, "
                "account_id, dur_sec, finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (ts, phone, status, message, stage, attempt,
                 account_id, dur_sec, finished_at),
            )
            conn.commit()
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.warning("写入 sign_events 失败: %s", e)


def add_page_visit(ts, role, path, ip_hash="", ua="", dur_ms=0, user_id=None):
    """写入页面访问；失败仅告警，不影响调用方。"""
    try:
        with _conn_lock:
            conn = get_conn()
            conn.execute(
                "INSERT INTO page_visits (ts, role, path, ip_hash, ua, dur_ms, user_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (ts, role, path, ip_hash, ua, dur_ms, user_id),
            )
            conn.commit()
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.warning("写入 page_visits 失败: %s", e)


def add_server_metric(ts, cpu=None, mem_pct=None, disk_pct=None,
                      net_in=None, net_out=None, load1=None, load5=None,
                      load15=None, proc_count=None):
    """写入服务器采样；失败仅告警，不影响调用方。"""
    try:
        with _conn_lock:
            conn = get_conn()
            conn.execute(
                "INSERT INTO server_metrics (ts, cpu, mem_pct, disk_pct, net_in, net_out, "
                "load1, load5, load15, proc_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, cpu, mem_pct, disk_pct, net_in, net_out, load1, load5, load15, proc_count),
            )
            conn.commit()
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.warning("写入 server_metrics 失败: %s", e)


def add_sign_events_batch(rows):
    """批量写入签到事件（单事务）；失败仅告警，不影响调用方。

    rows 为 dict 列表，支持 add_sign_event 的全部字段。
    """
    with _conn_lock:
        try:
            conn = get_conn()
            conn.execute("BEGIN IMMEDIATE")
            for r in rows:
                conn.execute(
                    "INSERT INTO sign_events (ts, phone, status, message, stage, attempt, "
                    "account_id, dur_sec, finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        r.get("ts", ""),
                        r.get("phone", ""),
                        r.get("status", ""),
                        r.get("message", ""),
                        r.get("stage", ""),
                        r.get("attempt", 0),
                        r.get("account_id"),
                        r.get("dur_sec"),
                        r.get("finished_at"),
                    ),
                )
            conn.commit()
        except Exception as e:
            with contextlib.suppress(Exception):
                conn.rollback()
            logger.warning("批量写入 sign_events 失败: %s", e)


def add_page_visits_batch(rows):
    """批量写入页面访问（单事务）；失败仅告警，不影响调用方。

    rows 为 dict 列表，支持 add_page_visit 的全部字段。
    """
    with _conn_lock:
        try:
            conn = get_conn()
            conn.execute("BEGIN IMMEDIATE")
            for r in rows:
                conn.execute(
                    "INSERT INTO page_visits (ts, role, path, ip_hash, ua, dur_ms, user_id) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        r.get("ts", ""),
                        r.get("role", ""),
                        r.get("path", ""),
                        r.get("ip_hash", ""),
                        r.get("ua", ""),
                        r.get("dur_ms", 0),
                        r.get("user_id"),
                    ),
                )
            conn.commit()
        except Exception as e:
            with contextlib.suppress(Exception):
                conn.rollback()
            logger.warning("批量写入 page_visits 失败: %s", e)


def sign_event_stats(days=30):
    """按天统计签到事件数量/状态分布；失败返回空列表。"""
    try:
        with _conn_lock:
            conn = get_conn()
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            rows = conn.execute(
                "SELECT substr(ts, 1, 10) AS day, status, COUNT(*) AS cnt "
                "FROM sign_events WHERE ts >= ? GROUP BY day, status ORDER BY day",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("sign_events 统计失败: %s", e)
        return []


def page_visit_stats(days=30):
    """按天统计 PV/UV 等基础指标；失败返回空列表。"""
    try:
        with _conn_lock:
            conn = get_conn()
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            rows = conn.execute(
                "SELECT substr(ts, 1, 10) AS day, COUNT(*) AS pv, "
                "COUNT(DISTINCT ip_hash) AS uv "
                "FROM page_visits WHERE ts >= ? GROUP BY day ORDER BY day",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("page_visits 统计失败: %s", e)
        return []


def server_metric_history(hours=24):
    """返回最近 N 小时服务器采样点；失败返回空列表。"""
    try:
        with _conn_lock:
            conn = get_conn()
            cutoff = (datetime.datetime.now() - datetime.timedelta(hours=hours)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            rows = conn.execute(
                "SELECT ts, cpu, mem_pct, disk_pct, net_in, net_out, "
                "load1, load5, load15, proc_count "
                "FROM server_metrics WHERE ts >= ? ORDER BY ts",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("server_metrics 查询失败: %s", e)
        return []


def sign_events_by_phone(phone, days=30):
    """单账号历史表现：按手机号返回时间线事件列表。"""
    try:
        with _conn_lock:
            conn = get_conn()
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            rows = conn.execute(
                "SELECT id, ts, phone, status, message, stage, attempt, "
                "account_id, dur_sec, finished_at "
                "FROM sign_events WHERE phone=? AND ts >= ? ORDER BY ts",
                (phone, cutoff),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("sign_events_by_phone 失败: %s", e)
        return []


def sign_events_since(since_ts, phone=None, limit=100):
    """实时事件流：返回 since_ts 之后的事件，可选按手机号过滤。"""
    try:
        with _conn_lock:
            conn = get_conn()
            sql = (
                "SELECT id, ts, phone, status, message, stage, attempt, "
                "account_id, dur_sec, finished_at FROM sign_events WHERE ts >= ?"
            )
            params = [since_ts]
            if phone:
                sql += " AND phone=?"
                params.append(phone)
            sql += " ORDER BY ts LIMIT ?"
            params.append(_normalize_limit(limit, 100))
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("sign_events_since 失败: %s", e)
        return []


def probe_events_on(date_str, limit=100):
    """指定日期（YYYY-MM-DD）的健康探测事件（stage="probe"，按时间正序）。

    供 Web 日志页展示探针结构化记录（v0.24.4 前 stage 仅落库无消费方）。
    查询失败返回空列表，不影响调用方。
    """
    try:
        with _conn_lock:
            conn = get_conn()
            start = f"{date_str} 00:00:00"
            end = f"{date_str} 23:59:59"
            sql = (
                "SELECT id, ts, phone, status, message, attempt "
                "FROM sign_events WHERE stage='probe' AND ts BETWEEN ? AND ? "
                "ORDER BY ts LIMIT ?"
            )
            params = [start, end, _normalize_limit(limit, 100)]
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("probe_events_on 失败: %s", e)
        return []


def sign_event_peak(days=7, bucket_minutes=5):
    """按时间桶统计签到事件数（第一版：近似并发/请求量）。"""
    try:
        with _conn_lock:
            conn = get_conn()
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            rows = conn.execute(
                "SELECT (strftime('%s', ts) - strftime('%s', ?)) / (? * 60) AS bucket, "
                "COUNT(*) AS cnt FROM sign_events WHERE ts >= ? "
                "GROUP BY bucket ORDER BY bucket",
                (cutoff, bucket_minutes, cutoff),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("sign_event_peak 失败: %s", e)
        return []


def sign_event_summary_today():
    """今日签到状态汇总。"""
    try:
        with _conn_lock:
            conn = get_conn()
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            rows = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM sign_events "
                "WHERE ts LIKE ? GROUP BY status",
                (today + "%",),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("sign_event_summary_today 失败: %s", e)
        return []


def page_visit_hourly(days=30):
    """按小时统计 PV/UV，支撑 24h 柱状图。"""
    try:
        with _conn_lock:
            conn = get_conn()
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            rows = conn.execute(
                "SELECT substr(ts, 12, 2) AS hour, COUNT(*) AS pv, "
                "COUNT(DISTINCT ip_hash) AS uv "
                "FROM page_visits WHERE ts >= ? GROUP BY hour ORDER BY hour",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("page_visit_hourly 失败: %s", e)
        return []


def page_visit_top_paths(days=30, limit=10):
    """页面访问排行。"""
    try:
        with _conn_lock:
            conn = get_conn()
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            rows = conn.execute(
                "SELECT path, COUNT(*) AS cnt FROM page_visits "
                "WHERE ts >= ? GROUP BY path ORDER BY cnt DESC LIMIT ?",
                (cutoff, _normalize_limit(limit, 10)),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("page_visit_top_paths 失败: %s", e)
        return []


def page_visit_active_users(days=30):
    """活跃用户数：仅统计登录用户（user_id 非空）。"""
    try:
        with _conn_lock:
            conn = get_conn()
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            cols = _table_columns(conn, "page_visits")
            if "user_id" in cols:
                row = conn.execute(
                    "SELECT COUNT(DISTINCT user_id) AS cnt FROM page_visits "
                    "WHERE ts >= ? AND user_id IS NOT NULL",
                    (cutoff,),
                ).fetchone()
                return row["cnt"] if row else 0
            # 旧库无 user_id 列时的迁移前兼容兜底
            row = conn.execute(
                "SELECT COUNT(DISTINCT ip_hash) AS cnt FROM page_visits WHERE ts >= ?",
                (cutoff,),
            ).fetchone()
            return row["cnt"] if row else 0
    except Exception as e:
        logger.warning("page_visit_active_users 失败: %s", e)
        return 0


def server_metric_latest(limit=60):
    """返回最近 N 条服务器采样点。"""
    try:
        with _conn_lock:
            conn = get_conn()
            rows = conn.execute(
                "SELECT ts, cpu, mem_pct, disk_pct, net_in, net_out, "
                "load1, load5, load15, proc_count "
                "FROM server_metrics ORDER BY ts DESC LIMIT ?",
                (_normalize_limit(limit, 60),),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("server_metric_latest 失败: %s", e)
        return []


def _event_cleanup(conn):
    """清理可视化表超期数据；失败仅告警。"""
    try:
        now = datetime.datetime.now()
        sign_cutoff = (now - datetime.timedelta(days=SIGN_EVENTS_RETENTION_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        page_cutoff = (now - datetime.timedelta(days=PAGE_VISITS_RETENTION_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        server_cutoff = (now - datetime.timedelta(days=SERVER_METRICS_RETENTION_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn.execute("DELETE FROM sign_events WHERE ts < ?", (sign_cutoff,))
        conn.execute("DELETE FROM page_visits WHERE ts < ?", (page_cutoff,))
        conn.execute("DELETE FROM server_metrics WHERE ts < ?", (server_cutoff,))
        conn.commit()
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.warning("清理可视化表失败: %s", e)


# ---------------------------------------------------------------------------
# 用户自选时间片（调度 v2，docs/design/plan-scheduler-v2.md 2.2）
# ---------------------------------------------------------------------------
def get_time_prefs():
    """全量自选 {phone: {"slot_min": int, "updated_at": str}}（build_schedule 每次启动读一次）。"""
    try:
        with _conn_lock:
            conn = get_conn()
            rows = conn.execute("SELECT phone, slot_min, updated_at FROM time_prefs").fetchall()
            return {r["phone"]: {"slot_min": r["slot_min"], "updated_at": r["updated_at"]} for r in rows}
    except Exception as e:
        logger.warning("读取 time_prefs 失败: %s", e)
        return {}


def get_time_pref(phone):
    """单个账号自选；无则 None。"""
    try:
        with _conn_lock:
            conn = get_conn()
            row = conn.execute(
                "SELECT phone, slot_min, updated_at FROM time_prefs WHERE phone=?", (phone,)
            ).fetchone()
            return None if row is None else {"slot_min": row["slot_min"], "updated_at": row["updated_at"]}
    except Exception as e:
        logger.warning("读取 time_pref %s 失败: %s", phone, e)
        return None


def set_time_pref(phone, slot_min, updated_at):
    """保存/更新自选（UPSERT）。slot_min 为窗口内分钟数（06:30 → 390，5 对齐）。"""
    conn = get_conn()
    with _conn_lock, conn:
        conn.execute(
            "INSERT INTO time_prefs (phone, slot_min, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(phone) DO UPDATE SET slot_min=excluded.slot_min, updated_at=excluded.updated_at",
            (phone, slot_min, updated_at),
        )


def clear_time_pref(phone):
    """清除自选（回退自动错峰）。"""
    conn = get_conn()
    with _conn_lock, conn:
        conn.execute("DELETE FROM time_prefs WHERE phone=?", (phone,))


def time_pref_stats():
    """每片已选人数（拥挤度）：[{slot_min, count}]，按 slot_min 升序。

    只统计未删除账号的自选，避免已注销/已软删账号的残留 pref 虚高拥挤度。
    """
    try:
        with _conn_lock:
            conn = get_conn()
            rows = conn.execute(
                "SELECT t.slot_min, COUNT(*) AS count "
                "FROM time_prefs t "
                "JOIN accounts a ON a.phone = t.phone AND a.deleted = 0 "
                "GROUP BY t.slot_min ORDER BY t.slot_min"
            ).fetchall()
            return [{"slot_min": r["slot_min"], "count": r["count"]} for r in rows]
    except Exception as e:
        logger.warning("time_prefs 统计失败: %s", e)
        return []


def _delete_time_prefs_by_phones(conn, phones):
    """按手机号批量删除自选（账号删除/清空时连带，须在调用方事务内）。"""
    if not phones:
        return
    conn.executemany("DELETE FROM time_prefs WHERE phone=?", [(p,) for p in phones])


def _clear_session_cache_by_phones(conn, phones):
    """按手机号批量清除会话缓存（账号删除/改绑时连带，须在调用方事务内）。

    2026-08-27 审查残留：删除/改绑路径此前不清 session_cache，旧手机号的加密
    cookie/csrf 行以手机号为主键永久驻留。与 _delete_time_prefs_by_phones 同款连带。
    注意：不能用 clear_session_cache()（其自带 BEGIN IMMEDIATE 事务，嵌套会撞
    "within a transaction"），故在调用方事务内直接 DELETE。
    """
    if not phones:
        return
    conn.executemany("DELETE FROM session_cache WHERE phone=?", [(p,) for p in phones])


def _delete_sign_events_by_phones(conn, phones):
    """按手机号批量清除签到事件（账号物理删除时连带，须在调用方事务内）。

    2026-08-28 审查 M2：sign_events 明文落 phone 且删号不清理——已删账号的
    明文手机号会驻留至 180 天保留期满。与 time_prefs/session_cache 同款连带
    （软删除不调用：宽限期内恢复后事件历史仍需保留）。
    """
    if not phones:
        return
    conn.executemany("DELETE FROM sign_events WHERE phone=?", [(p,) for p in phones])


def _delete_user_delete_requests(conn, email):
    """连带清除某邮箱的注销/恢复冷却计数（用户被物理删除时调用）。

    2026-08-28 审查 M4：user_delete_requests 只按保留期（30 天）清理，用户被
    物理删除（注销 purge / 管理员手动清除）后其明文邮箱仍驻留最多 30 天。
    在用户行物理删除的同一事务内连带清除，不留隐私残留。
    """
    if email:
        conn.execute("DELETE FROM user_delete_requests WHERE username=?", (email,))


# ---------------------------------------------------------------------------
# 会话 Cookie 缓存（v8，docs/research-lumjiel-core-sign-20260822.md §七）
# ---------------------------------------------------------------------------
# 会话 TTL：服务端会话真实有效期未知，保守取值——宁多登一次，不可拿过期
# 凭据撞风控。调度为每日一次签到，跨天复用需在 .env 调大本值（如 24~36）。
SESSION_CACHE_TTL_HOURS_DEFAULT = 12
# TTL 钳制边界（M13）：上限 72h（再大会把远过期凭据反复送去撞风控/已改密账号），
# 下限 1h（低于 1h 缓存命中形同虚设）。越界不静默接受，回默认并告警。
SESSION_CACHE_TTL_HOURS_MIN = 1.0
SESSION_CACHE_TTL_HOURS_MAX = 72.0

# L-03 密钥分离：session_cache 加密密钥不再直接复用账号凭据主密钥，
# 改由 YIBAN_ACCOUNTS_KEY 经 HKDF-SHA256 派生（info/salt 固定常量）。
# 主密钥泄露面的缩小之外，更防一类跨用途事故：cookie 缓存密文与账号密码密文
# 同钥同体系，任一用途的密文/明文对（如已知自己密码的账号）可为攻击者提供
# 验证主密钥的样本；派生隔离后互不可推。HKDF 每次现算（约 3 次 HMAC，开销
# 微秒级），不缓存派生结果——主密钥轮换后立即生效。
# 派生密钥变更后旧缓存解密必然失败 → 读侧按未命中清行，用户重登即可重建
# （会话缓存本就是可再生优化数据，此失效路径可接受）。
SESSION_CACHE_HKDF_INFO = b"yiban-session-cache-v1"


def _session_cache_key():
    """session_cache 专用加密密钥（HKDF-SHA256 派生，与账号凭据加密密钥隔离）。"""
    return HKDF(
        account_crypto.load_key(_env_file),
        32,
        salt=SESSION_CACHE_HKDF_INFO,
        hashmod=SHA256,
        context=SESSION_CACHE_HKDF_INFO,
    )


def _session_cache_ttl_hours():
    """读 TTL 小时数（YIBAN_SESSION_TTL_HOURS，默认 12）：缺失/非法/非正回退默认。

    M13：配置越界（<1h 或 >72h）同样回退默认并告警——此前可配 8760h 之类
    超大值，把早已失效的会话凭据跨季反复复用，等于放大风控与撞库面。
    """
    raw = os.environ.get("YIBAN_SESSION_TTL_HOURS", "").strip()
    if not raw:
        return SESSION_CACHE_TTL_HOURS_DEFAULT
    try:
        v = float(raw)
    except ValueError:
        logger.warning("配置 YIBAN_SESSION_TTL_HOURS=%r 非法，回退默认 %s 小时", raw, SESSION_CACHE_TTL_HOURS_DEFAULT)
        return SESSION_CACHE_TTL_HOURS_DEFAULT
    if v <= 0:
        logger.warning("配置 YIBAN_SESSION_TTL_HOURS=%s 非正，回退默认 %s 小时", raw, SESSION_CACHE_TTL_HOURS_DEFAULT)
        return SESSION_CACHE_TTL_HOURS_DEFAULT
    if not (SESSION_CACHE_TTL_HOURS_MIN <= v <= SESSION_CACHE_TTL_HOURS_MAX):
        logger.warning(
            "配置 YIBAN_SESSION_TTL_HOURS=%s 超出钳制范围 [%s, %s]，回退默认 %s 小时",
            raw, SESSION_CACHE_TTL_HOURS_MIN, SESSION_CACHE_TTL_HOURS_MAX,
            SESSION_CACHE_TTL_HOURS_DEFAULT,
        )
        return SESSION_CACHE_TTL_HOURS_DEFAULT
    return v


def get_session_cache(phone):
    """读取会话缓存：返回 {"cookies": <明文 JSON 串>, "csrf": str, 时间戳}，未命中返回 None。

    超过 TTL 的行读时顺手清除（updated_at 为定宽 %Y-%m-%d %H:%M:%S，字符串比较
    等价时间序，与库内其他 ts 比较口径一致）。缓存是可再生的优化数据，解密失败
    （换密钥/密文损坏）按未命中处理并清行，不阻断签到重新登录。
    M14：csrf 列同为准密文（AES-GCM 对象）。读侧遇到旧版明文行（非密文对象）
    一律视为失效——csrf 是免登录复用的关键凭据，明文残留行不可信，整行清除重登。
    """
    with _conn_lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT cookies_ct, csrf, created_at, updated_at FROM session_cache WHERE phone=?",
            (phone,),
        ).fetchone()
        if row is None:
            return None
        cutoff = (
            datetime.datetime.now()
            - datetime.timedelta(hours=_session_cache_ttl_hours())
        ).strftime("%Y-%m-%d %H:%M:%S")
        if row["updated_at"] <= cutoff:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM session_cache WHERE phone=?", (phone,))
                conn.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    conn.rollback()
                logger.warning("清理过期会话缓存失败: %s", phone)
            return None
        try:
            obj = json.loads(row["cookies_ct"])
            key = _session_cache_key()  # L-03：HKDF 派生密钥，与账号凭据密钥隔离
            cookies = account_crypto.decrypt_password(obj, key, phone)
            # M14：csrf 解密（旧明文行在此抛错 → 整行按未命中清除）
            csrf_obj = json.loads(row["csrf"])
            if not account_crypto.is_encrypted(csrf_obj):
                raise ValueError("csrf 字段不是密文对象（旧版明文行）")
            csrf = account_crypto.decrypt_password(csrf_obj, key, phone)
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            logger.warning("会话缓存解密失败（按未命中清除重登）: %s: %s", phone, e)
            with contextlib.suppress(Exception):
                conn.execute("DELETE FROM session_cache WHERE phone=?", (phone,))
                conn.commit()
            return None
        return {
            "cookies": cookies,
            "csrf": csrf,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


def set_session_cache(phone, cookie_json, csrf):
    """写入/更新会话缓存（UPSERT）。cookie_json 为明文 cookie jar JSON 串，
    落库前 AES-GCM 加密（AAD=phone，复用 account_crypto，与密码字段同体系）；
    M14：csrf 与 cookies 同等加密保护（csrf 也是免登录复用的认证凭据，
    明文落库使库文件泄露即可直接伪造请求）；更新时保留首次 created_at，
    只刷新 updated_at。

    BEGIN IMMEDIATE：跨进程写锁（signin 与 web 并存时串行化写路径）。
    """
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    key = _session_cache_key()  # L-03：HKDF 派生密钥，与账号凭据密钥隔离
    cookies_ct = json.dumps(
        account_crypto.encrypt_password(str(cookie_json), key, phone)
    )
    csrf_ct = json.dumps(account_crypto.encrypt_password(str(csrf), key, phone))
    conn = get_conn()
    with _conn_lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO session_cache (phone, cookies_ct, csrf, created_at, updated_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(phone) DO UPDATE SET cookies_ct=excluded.cookies_ct, "
                "csrf=excluded.csrf, updated_at=excluded.updated_at",
                (phone, cookies_ct, csrf_ct, now, now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def clear_session_cache(phone):
    """清除单账号会话缓存（会话失效/风控类失败联动；行不存在时无操作）。"""
    conn = get_conn()
    with _conn_lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM session_cache WHERE phone=?", (phone,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
