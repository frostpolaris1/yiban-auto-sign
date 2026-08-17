# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""SQLite 数据访问层（web / signin / tui 三进程共用）。

- accounts/users 数据从 JSON 整文件读写迁移到 SQLite（yiban.db，WAL 模式）
  ——根治并发覆盖 / 索引漂移 / 进程外覆盖三个历史问题
- 稳定 ID（id PK AUTOINCREMENT）：业务层用 id 寻址，不再受列表顺序漂移影响
- 密码/识别码字段在库内仍为 AES-GCM 密文（复用 account_crypto，解密在 load 时）
- 自动迁移：yiban.db 不存在且 accounts.json/users.json 存在 → 导入（幂等）→ JSON 改名 .bak 保留逃生门
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

# 2026-08-16 审查轮：原 5 处函数内 import 上移（account_crypto 不依赖 db，无循环）
import account_crypto

logger = logging.getLogger("yiban.db")

DB_DEFAULT = os.environ.get("YIBAN_DB_FILE", "yiban.db")

# 软删除保留期（2026-08-15 审查统一命名/单位）：天为唯一来源，秒数派生——
# 此前 web(app.py DELETED_RETENTION_DAYS) 与 db 各持一份同名不同单位常量，易改一处漏一处
SOFT_DELETE_RETENTION_DAYS = 7
SOFT_DELETE_RETENTION_SECONDS = SOFT_DELETE_RETENTION_DAYS * 86400

# schema 版本号（PRAGMA user_version）：0 = 未迁移；>=1 = 已应用对应迁移
SCHEMA_VERSION = 7


class DuplicatePhoneError(Exception):
    """手机号已存在（accounts.phone 唯一约束冲突）。"""


class DuplicateOwnerError(Exception):
    """该用户已有一个未删除账号（accounts.owner 部分唯一索引冲突）。"""


class MigrationDeferred(Exception):
    """迁移暂缓：本次不应用，下次启动重试（用于可选迁移遇到需人工处理的数据）。"""

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


def init_db(db_file=None, migrate_from=None, env_file=None):
    """初始化连接与表结构；可选自动迁移（migrate_from 提供 json 文件基路径，如 /path/accounts.json）。

    env_file：.env 路径（加密密钥来源），须与调用方一致（web 用 --env 参数时必传），
    None 时走 account_crypto 默认（.env 于当前工作目录）。
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
        # 核心迁移失败会关闭连接并抛出，阻断启动；可选迁移失败仅告警。
        try:
            _run_migrations(_conn)
        except Exception:
            with contextlib.suppress(Exception):
                _conn.close()
            _conn = None
            raise
        # 自动迁移（幂等：库存在但空表 + JSON 存在才导入）
        if migrate_from:
            _maybe_migrate(_conn, migrate_from)
        _audit_cleanup(_conn)
        _event_cleanup(_conn)
        purge_deleted_users()
        purge_old_delete_requests()
        return _conn


def get_conn():
    if _conn is None:
        init_db()
    return _conn


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
          deleted_at TEXT NOT NULL DEFAULT ''
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
                   "sign_events", "page_visits", "server_metrics"}


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
    """读取 .env 全部键值，返回 dict（文件缺失/非法行静默跳过）。"""
    result = {}
    try:
        with open(env_file, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    result[key.strip()] = value.strip()
    except OSError:
        pass
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

    写入前再读一次 .env：若其他进程已写入密钥则复用，避免多进程首启竞态。
    """
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


def _audit_key():
    """获取审计 HMAC 密钥：环境变量 YIBAN_AUDIT_KEY 优先，回退 .env，缺失时生成。"""
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
        "SELECT id, ts, username, action, target, detail FROM audit_logs ORDER BY id"
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
        "SELECT id, hash FROM audit_logs ORDER BY id"
    ).fetchall()
    if rows and any(r["hash"] == "" for r in rows):
        _rechain_audit_logs(conn)


# ---------------------------------------------------------------------------
# 可视化表（Phase 4）
# ---------------------------------------------------------------------------
def _write_track_salt_to_env_file(env_file, salt):
    """把新生成的 YIBAN_TRACK_SALT 写入 .env（保留其他行，原子替换）。"""
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
    """对 IP 加盐哈希（YIBAN_TRACK_SALT），返回十六进制字符串。"""
    salt = _track_salt()
    return hashlib.sha256(f"{salt}:{ip}".encode("utf-8")).hexdigest()


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
]


def _run_migrations(conn):
    """按 PRAGMA user_version 顺序执行未应用的迁移。

    核心迁移失败会抛出异常（init_db 会关闭连接并阻断启动）；
    可选迁移失败只告警并停止后续迁移，不阻断启动；
    可选迁移抛 MigrationDeferred 时视为“延后”，下次启动重试。
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for target_version, name, fn, is_core in _MIGRATIONS:
        if version >= target_version:
            continue
        try:
            fn(conn)
            conn.execute(f"PRAGMA user_version = {target_version}")
            conn.commit()
            version = target_version
            logger.info("schema 迁移完成: %s (user_version=%d)", name, target_version)
        except MigrationDeferred as e:
            logger.warning("可选 schema 迁移延后: %s: %s", name, e)
            break
        except Exception as e:
            if is_core:
                logger.error("核心 schema 迁移失败: %s: %s", name, e)
                raise
            logger.warning("可选 schema 迁移失败: %s: %s，已跳过后续迁移", name, e)
            break


# ---------------------------------------------------------------------------
# 自动迁移（JSON → SQLite，幂等）
# ---------------------------------------------------------------------------
def _maybe_migrate(conn, json_base):
    """json_base 形如 /path/accounts.json（users.json 同目录推断）。"""
    accounts_json = json_base if json_base.endswith("accounts.json") else os.path.join(
        os.path.dirname(json_base), "accounts.json"
    )
    users_json = os.path.join(os.path.dirname(accounts_json), "users.json")
    has_db_rows = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] > 0
    if has_db_rows:
        return  # 已迁移过
    imported = 0
    if os.path.exists(accounts_json):
        try:
            with open(accounts_json, encoding="utf-8") as f:
                accounts = json.load(f)
        except (json.JSONDecodeError, OSError):
            accounts = []
        if isinstance(accounts, list) and accounts:
            # 加密字段统一为库内 JSON 串格式：
            #   明文 str → 加密（复用 account_crypto）；
            #   密文 dict（0.16 JSON 嵌套对象）→ json.dumps 序列化；
            #   密文 JSON 串 → 原样。
            key = account_crypto.load_key(_env_file)
            with _conn_lock, conn:
                for i, a in enumerate(accounts):
                    password = a.get("password", "") or ""
                    phone_code = a.get("phone_code", "") or ""
                    if key is not None:
                        if password and not _is_encrypted_value(password):
                            password = json.dumps(account_crypto.encrypt_password(password, key, a.get("phone", "")))
                        elif isinstance(password, dict):
                            password = json.dumps(password)  # 已是密文对象 → 序列化入库
                        if phone_code and not _is_encrypted_value(phone_code):
                            phone_code = json.dumps(account_crypto.encrypt_password(phone_code, key, a.get("phone", "")))
                        elif isinstance(phone_code, dict):
                            phone_code = json.dumps(phone_code)
                    conn.execute(
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
            imported += len(accounts)
            _rename_backup(accounts_json)
    if os.path.exists(users_json):
        try:
            with open(users_json, encoding="utf-8") as f:
                users = json.load(f)
        except (json.JSONDecodeError, OSError):
            users = []
        if isinstance(users, list) and users:
            with _conn_lock, conn:
                for u in users:
                    conn.execute(
                        "INSERT OR IGNORE INTO users (email, password_hash, role, created_at, pw_version) VALUES (?,?,?,?,?)",
                        (
                            u.get("email", ""),
                            u.get("password_hash", ""),
                            u.get("role", "user"),
                            u.get("created_at", ""),
                            u.get("pw_version", 1),
                        ),
                    )
            imported += len(users)
            _rename_backup(users_json)
    if imported:
        logger.info("SQLite 自动迁移完成：导入 %d 条记录（JSON 已改名 .bak 保留逃生门）", imported)
    else:
        logger.info("SQLite 初始化完成（无 JSON 数据可迁移）")


def _rename_backup(path):
    """JSON 迁移成功后改名保留（逃生门），避免被旧代码误写回。"""
    if os.path.exists(path):
        bak = f"{path}.bak-{datetime.datetime.now().strftime('%Y%m%d')}"
        if not os.path.exists(bak):
            os.rename(path, bak)


# ---------------------------------------------------------------------------
# accounts CRUD（单行操作，事务内）
# ---------------------------------------------------------------------------
def _row_to_account(row):
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
                a[k] = v  # 明文（迁移前数据或未加密）
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


def _purge_expired_deleted(conn):
    """软删除超过保留期（>= 7 天）的行物理清除（web 原 load 惰性清理语义，库内必有 deleted_at）。

    deleted_at 为 %Y-%m-%d %H:%M:%S 格式（统一写入格式），字符串比较等价时间序。
    清理失败仅告警不阻断（规范审查 D6：原静默吞错无痕迹）。
    """
    try:
        cutoff = (datetime.datetime.now() - datetime.timedelta(seconds=SOFT_DELETE_RETENTION_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
        # 2026-08-16 优化（性能审查遗留）：先查有无超期行再删——无行时不发写事务，
        # 避免 1000 账号每 10s 轮询重复执行 DELETE+COMMIT
        row = conn.execute(
            "SELECT 1 FROM accounts WHERE deleted=1 AND deleted_at != '' AND deleted_at <= ? LIMIT 1",
            (cutoff,),
        ).fetchone()
        if row:
            conn.execute(
                "DELETE FROM accounts WHERE deleted=1 AND deleted_at != '' AND deleted_at <= ?",
                (cutoff,),
            )
            conn.commit()
    except Exception as e:
        logger.warning("清理超期软删除账号失败: %s", e)


def load_accounts():
    """全部账号（按 sort_order 升序），已解密；顺带清除超期软删除行。"""
    with _conn_lock:
        conn = get_conn()
        _purge_expired_deleted(conn)
        rows = conn.execute("SELECT * FROM accounts ORDER BY sort_order").fetchall()
        return [_row_to_account(r) for r in rows]


def load_accounts_raw():
    """全部账号原始行（password/phone_code 保持密文 JSON 串，不解密）。

    供 db_export 等导出场景使用：避免生成明文凭据文件；顺带清除超期软删除行。
    """
    with _conn_lock:
        conn = get_conn()
        _purge_expired_deleted(conn)
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
    """
    conn = get_conn()
    with _conn_lock, conn:
        cur = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,))
        row = cur.fetchone()
        if row is None:
            return None if expect_snapshot is not None else False
        cur_a = _row_to_account(row)  # 解密（AAD=库内当前手机号）
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
            _convert_integrity_error(e)
        return True


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
    with _conn_lock, conn:
        rows = conn.execute(
            "SELECT id, sort_order FROM accounts WHERE deleted=0 ORDER BY sort_order"
        ).fetchall()
        pos = next((i for i, r in enumerate(rows) if r["id"] == account_id), None)
        if pos is None:
            return False
        target = pos + direction
        if target < 0 or target >= len(rows):
            return False
        a, b = rows[pos]["sort_order"], rows[target]["sort_order"]
        conn.execute("UPDATE accounts SET sort_order=? WHERE id=?", (b, rows[pos]["id"]))
        conn.execute("UPDATE accounts SET sort_order=? WHERE id=?", (a, rows[target]["id"]))
        return True


def delete_accounts_by_owner(owner):
    """删除某用户提交的全部易班账号（用户删除/清空账号用，事务内）。返回删除行数。"""
    conn = get_conn()
    with _conn_lock, conn:
        rows = conn.execute("SELECT phone FROM accounts WHERE owner=?", (owner,)).fetchall()
        cur = conn.execute("DELETE FROM accounts WHERE owner=?", (owner,))
        _delete_time_prefs_by_phones(conn, [r["phone"] for r in rows])  # 连带清理自选（调度 v2）
        return cur.rowcount


def delete_user_with_accounts(email):
    """删除用户及其全部易班账号（单事务，防崩溃窗口数据不一致）。返回删除账号行数。"""
    conn = get_conn()
    with _conn_lock, conn:
        rows = conn.execute("SELECT phone FROM accounts WHERE owner=?", (email,)).fetchall()
        cur = conn.execute("DELETE FROM accounts WHERE owner=?", (email,))
        _delete_time_prefs_by_phones(conn, [r["phone"] for r in rows])  # 连带清理自选（H2 对抗性审查补）
        conn.execute("DELETE FROM users WHERE email=?", (email,))
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
        _delete_time_prefs_by_phones(conn, [r["phone"] for r in old if r["phone"] not in keep])
        for i, a in enumerate(accounts):
            try:
                conn.execute(
                    "INSERT INTO accounts (sort_order, name, phone, password, phone_model, phone_code, owner, status, reject_reason, deleted, deleted_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
                    ),
                )
            except sqlite3.IntegrityError as e:
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
    """查找任意用户（含已注销），供恢复/管理排查使用。"""
    with _conn_lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM users WHERE email=? ORDER BY id DESC LIMIT 1", (email,)
        ).fetchone()
        return dict(row) if row else None


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
    conn = get_conn()
    with _conn_lock, conn:
        sets, vals = [], []
        for k in ("password_hash", "role", "pw_version"):
            if k in fields:
                sets.append(f"{k}=?")
                vals.append(fields[k])
        if not sets:
            return
        vals.append(email)
        conn.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE email=? AND deleted=0", vals
        )


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

    返回是否找到并注销了有效用户。
    """
    conn = get_conn()
    with _conn_lock, conn:
        row = conn.execute(
            "SELECT id FROM users WHERE email=? AND deleted=0", (email,)
        ).fetchone()
        if row is None:
            return False
        rows = conn.execute(
            "SELECT phone FROM accounts WHERE owner=? AND deleted=0", (email,)
        ).fetchall()
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE accounts SET deleted=1, deleted_at=? WHERE owner=? AND deleted=0",
            (now, email),
        )
        _delete_time_prefs_by_phones(conn, [r["phone"] for r in rows])
        conn.execute(
            "UPDATE users SET deleted=1, deleted_at=? WHERE id=?",
            (now, row["id"]),
        )
        return True


def restore_user(email):
    """撤销注销：仅当没有同邮箱活跃用户时，把最近一个已注销用户及其账号恢复。

    2026-08-16 安全审查：联动恢复该用户的软删易班账号（deleted=0），
    保证反悔恢复 = 用户 + 账号完整回来（此前账号在注销时被物理删除，恢复残缺）。
    """
    conn = get_conn()
    with _conn_lock, conn:
        active = conn.execute(
            "SELECT id FROM users WHERE email=? AND deleted=0", (email,)
        ).fetchone()
        if active is not None:
            return False
        deleted = conn.execute(
            "SELECT id FROM users WHERE email=? AND deleted=1 "
            "ORDER BY id DESC LIMIT 1",
            (email,),
        ).fetchone()
        if deleted is None:
            return False
        conn.execute(
            "UPDATE users SET deleted=0, deleted_at='' WHERE id=?",
            (deleted["id"],),
        )
        conn.execute(
            "UPDATE accounts SET deleted=0, deleted_at='' WHERE owner=? AND deleted=1",
            (email,),
        )
        return True


def purge_deleted_users(days=7):
    """物理清除超过宽限期的已注销用户（默认 7 天）；失败仅告警。

    2026-08-16 安全审查（用户提出错位问题）：宽限期 3 天 → 7 天，
    与账号软删除保留期（_purge_expired_deleted，7 天）对齐——第 7 天用户与
    账号同天清除，邮箱/手机号同时释放，消除"反悔窗口内资产被抢占"风险。
    """
    try:
        conn = get_conn()
        with _conn_lock, conn:
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            conn.execute(
                "DELETE FROM users WHERE deleted=1 AND deleted_at != '' AND deleted_at <= ?",
                (cutoff,),
            )
            conn.commit()
    except Exception as e:
        logger.warning("清理已注销用户失败: %s", e)


def purge_deleted_users_hard(emails):
    """管理员手动物理清除指定的已注销用户（2026-08-17 需求：不等 7 天自动清除）。

    安全边界：仅处理 deleted=1 的用户行——传入活跃用户邮箱时该用户被直接跳过
    （管理员误操作/并发注册新同邮箱用户均不可能误删活跃数据）。
    单事务连带清理：该用户全部账号行（含软删）+ 账号对应 time_prefs + 用户行。
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
                    "SELECT phone FROM accounts WHERE owner=?", (email,)
                ).fetchall()
            ]
            conn.execute("DELETE FROM accounts WHERE owner=?", (email,))
            _delete_time_prefs_by_phones(conn, phones)
            conn.execute("DELETE FROM users WHERE id=?", (row["id"],))
            purged.append(email)
    return purged


def purge_old_delete_requests(days=30):
    """物理清除超过保留期的注销请求记录（默认 30 天）；失败仅告警。

    对抗审查 2026-08-16：user_delete_requests 只增不删会无限累积
    （长期使用后 count 查询变慢、库体积膨胀）——启动时随 purge_deleted_users 一并清理。
    """
    try:
        conn = get_conn()
        with _conn_lock, conn:
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            conn.execute(
                "DELETE FROM user_delete_requests WHERE created_at <= ?",
                (cutoff,),
            )
            conn.commit()
    except Exception as e:
        logger.warning("清理注销请求记录失败: %s", e)


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
        logger.warning("记录注销请求失败: %s", e)


def count_user_delete_requests(username=None, ip_hash=None, since_ts=None, kind=None):
    """统计窗口内注销/恢复请求次数（用户或 IP 维度；kind=None 统计全部，v7）。"""
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
        logger.warning("统计注销请求失败: %s", e)
        return 0


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
                    _delete_time_prefs_by_phones(conn, [r["phone"] for r in rows])
                    conn.execute("DELETE FROM users WHERE email=?", (email,))
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
def audit(username, action, target="", detail=""):
    """记录关键管理操作（多管理员追溯；detail 需已脱敏）。

    Phase 3：写入 HMAC 哈希链，prev_hash 取上一条 hash；签名保持不变。
    """
    try:
        with _conn_lock:
            conn = get_conn()
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            detail = detail[:200]
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
    except Exception as e:
        logger.warning("审计写入失败: %s", e)


def verify_audit_chain():
    """校验审计哈希链。

    把当前表中 id 最小的一行视为链根（不校验其 prev_hash），
    从第二行开始要求 prev_hash 等于上一行 hash，且每行 hash 与内容匹配。

    返回 (ok, broken_count, first_broken_id)；broken_count=-1 表示校验过程异常。
    """
    try:
        with _conn_lock:
            conn = get_conn()
            rows = conn.execute(
                "SELECT id, prev_hash, hash, ts, username, action, target, detail "
                "FROM audit_logs ORDER BY id"
            ).fetchall()
            if not rows:
                return True, 0, None
            prev = ""
            broken = 0
            first_broken = None
            for r in rows:
                if r["prev_hash"] != prev:
                    broken += 1
                    if first_broken is None:
                        first_broken = r["id"]
                expected_hash = _audit_hash(
                    prev, r["ts"], r["username"], r["action"], r["target"], r["detail"]
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


def last_time_pref_set_at(phone):
    """指定账号最近一次自选时间片保存时间（切换冷却判定用；无记录返回 None）。

    按被选账号（审计 target=phone）而非操作用户计价（H3/H4 对抗性审查）：
    - 多管理员共享 admin 账号时冷却全局生效（管理员 A 保存后 B 立即改选也被拦截）；
    - 改手机号/删号重提交新号后，新 phone 无历史审计 → 不被旧账号冷却误伤。
    """
    try:
        with _conn_lock:
            conn = get_conn()
            row = conn.execute(
                "SELECT ts FROM audit_logs WHERE action='time_pref_set' AND target=? "
                "ORDER BY id DESC LIMIT 1",
                (phone or "",),
            ).fetchone()
            return row["ts"] if row else None
    except Exception as e:
        logger.warning("查询自选保存时间失败: %s", e)
        return None


def time_pref_set_count_since(phone, since_ts):
    """指定账号在 since_ts 之后的保存次数（弹性冷却高频判定用；ts 定宽字符串可比较）。"""
    try:
        with _conn_lock:
            conn = get_conn()
            row = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='time_pref_set' "
                "AND target=? AND ts >= ?",
                (phone or "", since_ts),
            ).fetchone()
            return row[0] if row else 0
    except Exception as e:
        logger.warning("统计自选保存次数失败: %s", e)
        return 0


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
        logger.warning("查询暂停时间失败: %s", e)
        return None


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
        logger.warning("统计暂停次数失败: %s", e)
        return 0


def _audit_cleanup(conn):
    """清理超 180 天审计（启动时顺带，一条 DELETE）。清理失败仅告警（规范审查 D6）。

    Phase 3：删除旧行后重建哈希链，使剩余最旧一行成为新根。
    """
    try:
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=180)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("DELETE FROM audit_logs WHERE ts < ?", (cutoff,))
        conn.commit()
        remaining = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        if remaining:
            _rechain_audit_logs(conn)
    except Exception as e:
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
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("sign_events_since 失败: %s", e)
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
                (cutoff, limit),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("page_visit_top_paths 失败: %s", e)
        return []


def page_visit_active_users(days=30):
    """活跃用户数：优先 user_id，缺失时回退 ip_hash。"""
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
                (limit,),
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
    """每片已选人数（拥挤度）：[{slot_min, count}]，按 slot_min 升序。"""
    try:
        with _conn_lock:
            conn = get_conn()
            rows = conn.execute(
                "SELECT slot_min, COUNT(*) AS count FROM time_prefs GROUP BY slot_min ORDER BY slot_min"
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
