# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""SQLite 数据访问层（web / signin 双进程共用）的门面与尚未按域拆出的表访问。

- accounts/users 数据从 JSON 整文件读写迁移到 SQLite（yiban.db，WAL 模式）
  ——根治并发覆盖 / 索引漂移 / 进程外覆盖三个历史问题
- 稳定 ID（id PK AUTOINCREMENT）：业务层用 id 寻址，不再受列表顺序漂移影响
- 密码/识别码字段在库内仍为 AES-GCM 密文（复用 account_crypto，解密在 load 时）
- 自动迁移：accounts/users 表为空且对应 JSON 存在 → 导入（幂等）→ JSON 改名 .bak 保留逃生门
- 操作审计：audit() 记录关键管理操作（多管理员追溯）
- 排序：sort_order 升序为签到顺序（移动 = 事务内交换/重排）

已按域拆出的模块（定义点不在本模块，这里只再导出）：
- `connection`：连接单例与路径（`_conn`/`_conn_lock`/`_db_file`/`_env_file`/`get_conn`）。
  `init_db` 留在这里——它是启动序列的编排点，也须与冻结的历史迁移函数共存。
- `migrations`：建表/索引、`migrate_v1..v17`、版本编排 `_run_migrations`。
- `audit_chain`：`audit()` 写入链路、哈希链校验、库外锚点族、审计密钥来源与缓存。
- `events`：sign_events 的写入/查询/统计与保留期清理。
- `users`：users / user_delete_requests 表的状态机、注销与反悔、到期清除。
- `cleanup`：每日清理编排（审计与账号保留期清除，并调用各域清理）。
- `accounts`：accounts 表的 CRUD、行加解密与运行期有效性判定。
- `session_cache`：session_cache 表族的读写、有效期判定与凭据加密。

本模块自身仍持有：time_prefs、时钟守卫与 app_meta、追踪盐哈希等尚未按域
拆出的部分，以及跨域粘合助手（写事务入口、连带清理、清理留痕）。子模块反向经本门面按
属性取这些名字（见各模块的 `_facade()`）；`db._audit_hash = 替身`、`db._conn = None` 一类
打桩面由本模块的再导出与读写转发维持不变。
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
import sys
import threading
import types

# 包导入引导：本模块已入包（`yiban.store.db`），正常导入路径下仓库根必然在 sys.path
# 里；这里仍补一次仓库根，作为"被以任意 sys.path 形状导入"的兜底（如容器里从
# `/app/scripts` 起手的老入口）。**必须先于任何 yiban 导入**（含下面的 infra 导入，
# 以及函数内的延迟导入）。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 2026-08-16 审查轮：原 5 处函数内 import 上移（account_crypto 不依赖 db，无循环）
# 表级数据访问已按表拆入 yiban/store/*；本模块保留同名再导出，旧调用方（web/app.py、
# 测试）继续用 db.xxx。依赖方向单向：db → store（store 只在函数内延迟取连接）。
from yiban import clock  # noqa: E402
from yiban.infra import account_crypto, env_lock  # noqa: E402
from yiban.store import accounts as _accounts  # noqa: E402
from yiban.store import audit_chain as _audit_chain  # noqa: E402
from yiban.store import claims as _claims  # noqa: E402
from yiban.store import cleanup as _cleanup  # noqa: E402
from yiban.store import connection as _connection  # noqa: E402
from yiban.store import events as _events  # noqa: E402
from yiban.store import migrations as _migrations  # noqa: E402
from yiban.store import session_cache as _session_cache  # noqa: E402
from yiban.store import users as _users  # noqa: E402
from yiban.store import verify_jobs as _verify_jobs  # noqa: E402

account_is_signable = _accounts.is_signable
account_signs_in = _accounts.signs_in
purge_orphan_session_cache = _accounts.purge_orphan_session_cache
# accounts.phone 唯一约束冲突的可区分异常，定义点随 accounts 表在 yiban/store/accounts.py
DuplicatePhoneError = _accounts.DuplicatePhoneError

VERIFY_JOB_RETENTION_DAYS = _verify_jobs.VERIFY_JOB_RETENTION_DAYS
VERIFY_JOB_PENDING = _verify_jobs.VERIFY_JOB_PENDING
VERIFY_JOB_RUNNING = _verify_jobs.VERIFY_JOB_RUNNING
VERIFY_JOB_DONE = _verify_jobs.VERIFY_JOB_DONE
VERIFY_JOB_REJECTED = _verify_jobs.VERIFY_JOB_REJECTED
VERIFY_JOB_CANCELLED = _verify_jobs.VERIFY_JOB_CANCELLED
VERIFY_JOB_STALE_SECONDS = _verify_jobs.VERIFY_JOB_STALE_SECONDS
VERIFY_JOB_STALE_MSG = _verify_jobs.VERIFY_JOB_STALE_MSG

create_verify_job = _verify_jobs.create
get_verify_job = _verify_jobs.get
claim_verify_job = _verify_jobs.claim
finish_verify_job = _verify_jobs.finish
cancel_verify_job = _verify_jobs.cancel
count_active_verify_jobs = _verify_jobs.count_active
reclaim_stale_verify_jobs = _verify_jobs.reclaim_stale
purge_verify_jobs = _verify_jobs.purge

# 签到领取池（v17，多执行体协调）
CLAIM_LEASE_SECONDS = _claims.LEASE_SECONDS
CLAIM_RETENTION_DAYS = _claims.RETENTION_DAYS
CLAIM_STATE_CLAIMED = _claims.STATE_CLAIMED
CLAIM_STATE_DONE = _claims.STATE_DONE
CLAIM_STATE_FAILED = _claims.STATE_FAILED
CLAIM_SETTLED_STATES = _claims.SETTLED_STATES
CLAIM_OPEN_STATES = _claims.OPEN_STATES
claim_new_owner = _claims.new_owner
claim_sign_account = _claims.try_claim
claim_touch = _claims.touch
claim_settle = _claims.settle
claim_give_up = _claims.give_up
claim_states_for_day = _claims.states_for_day
claim_in_flight = _claims.in_flight_phones
claim_stats = _claims.stats
claim_activity = _claims.activity
claim_owners_for_day = _claims.owners_for_day
claim_owners_since = _claims.owners_since
purge_sign_claims = _claims.purge

# 审计链域（唯一定义点在 yiban/store/audit_chain.py）：函数与常量按原样再导出，既有
# `db.audit()` / `db.audit_health()` / `db._audit_hash(...)` 调用面与打桩面不变。
# 三个**可变状态**名（`_AUDIT_KEY_CACHE` / `_AUDIT_FAIL_UNFLUSHED` / `_AUDIT_FAIL_UNFLUSHED_DB`）
# 不走这里的快照式再导出，而走下方模块类的读写转发——否则 `db._AUDIT_KEY_CACHE = None`
# （tests/test_rekey_key_source.py 的清缓存）只会写在一份陈旧副本上、真缓存纹丝不动。
_parse_env_file = _audit_chain._parse_env_file
_decode_audit_key = _audit_chain._decode_audit_key
_resolve_key_env_file = _audit_chain._resolve_key_env_file
_write_audit_key_to_env_file = _audit_chain._write_audit_key_to_env_file
_assert_key_source_certain = _audit_chain._assert_key_source_certain
_audit_key = _audit_chain._audit_key
_audit_hash = _audit_chain._audit_hash
_rechain_audit_logs = _audit_chain._rechain_audit_logs
_record_rechain_event = _audit_chain._record_rechain_event
audit_rechain_events = _audit_chain.audit_rechain_events

_bump_audit_write_failure = _audit_chain._bump_audit_write_failure
_unflushed_audit_failures = _audit_chain._unflushed_audit_failures
_reset_audit_fail_memory = _audit_chain._reset_audit_fail_memory
audit_persisted_write_failures = _audit_chain.audit_persisted_write_failures
audit_write_failures = _audit_chain.audit_write_failures
audit = _audit_chain.audit
audit_head_hash = _audit_chain.audit_head_hash
audit_row_count = _audit_chain.audit_row_count
verify_audit_chain = _audit_chain.verify_audit_chain

audit_anchor_path = _audit_chain.audit_anchor_path
_anchor_line_sha = _audit_chain._anchor_line_sha
_parse_anchor_line = _audit_chain._parse_anchor_line
_read_anchor_lines = _audit_chain._read_anchor_lines
_get_anchor_meta = _audit_chain._get_anchor_meta
_audit_purge_total = _audit_chain._audit_purge_total
_audit_purge_events = _audit_chain._audit_purge_events
audit_purge_total = _audit_chain.audit_purge_total
audit_purge_events = _audit_chain.audit_purge_events
record_audit_anchor = _audit_chain.record_audit_anchor
_record_anchor_trace = _audit_chain._record_anchor_trace
_last_audit_anchor = _audit_chain._last_audit_anchor
_anchor_file_state = _audit_chain._anchor_file_state
verify_audit_anchor = _audit_chain.verify_audit_anchor
_purge_events_after_anchor = _audit_chain._purge_events_after_anchor
_purge_event_covers = _audit_chain._purge_event_covers
_purge_event_sets_min = _audit_chain._purge_event_sets_min
_rechain_events = _audit_chain._rechain_events
_rechain_hint = _audit_chain._rechain_hint
audit_health = _audit_chain.audit_health
_rechain_diagnostics = _audit_chain._rechain_diagnostics

_AUDIT_KEY_LOCK = _audit_chain._AUDIT_KEY_LOCK
_AUDIT_FAIL_KEY = _audit_chain._AUDIT_FAIL_KEY
_AUDIT_FAIL_LOCK = _audit_chain._AUDIT_FAIL_LOCK
_AUDIT_RETRIES = _audit_chain._AUDIT_RETRIES
_AUDIT_RETRY_BASE_DELAY = _audit_chain._AUDIT_RETRY_BASE_DELAY
_ANCHOR_V1_TOKENS = _audit_chain._ANCHOR_V1_TOKENS
_ANCHOR_V2_TOKENS = _audit_chain._ANCHOR_V2_TOKENS
_ANCHOR_META_KEY = _audit_chain._ANCHOR_META_KEY
_AUDIT_PURGE_TOTAL_KEY = _audit_chain._AUDIT_PURGE_TOTAL_KEY
_AUDIT_PURGE_EVENTS_KEY = _audit_chain._AUDIT_PURGE_EVENTS_KEY
_PURGE_EVENTS_KEEP = _audit_chain._PURGE_EVENTS_KEEP
_RECHAIN_EVENTS_KEY = _audit_chain._RECHAIN_EVENTS_KEY
_RECHAIN_EVENTS_KEEP = _audit_chain._RECHAIN_EVENTS_KEEP
_ANCHOR_GENESIS = _audit_chain._ANCHOR_GENESIS

# 事件域（唯一定义点在 yiban/store/events.py）：写入/查询/统计与保留期清理按原样再导出，
# 既有 `db.add_sign_event()` / `db.sign_event_stats()` / `db._event_cleanup(...)` 调用面不变。
SIGN_EVENTS_RETENTION_DAYS = _events.SIGN_EVENTS_RETENTION_DAYS
_normalize_limit = _events._normalize_limit
add_sign_event = _events.add_sign_event
add_sign_events_batch = _events.add_sign_events_batch
sign_event_stats = _events.sign_event_stats
sign_events_by_phone = _events.sign_events_by_phone
sign_events_since = _events.sign_events_since
probe_events_on = _events.probe_events_on
sign_events_on = _events.sign_events_on
sign_events_recent_date = _events.sign_events_recent_date
_event_cleanup = _events._event_cleanup

# 会话缓存域（唯一定义点在 yiban/store/session_cache.py）：函数走下方读写转发（内部调用点
# 会被 `db._session_cache_now` 打桩），四个常量是不可变配置、全仓无重绑与打桩，按常量
# 再导出即等价。`db.SESSION_CACHE_TTL_HOURS_DEFAULT` 一类读取不变。
SESSION_CACHE_TTL_HOURS_DEFAULT = _session_cache.SESSION_CACHE_TTL_HOURS_DEFAULT
SESSION_CACHE_TTL_HOURS_MIN = _session_cache.SESSION_CACHE_TTL_HOURS_MIN
SESSION_CACHE_TTL_HOURS_MAX = _session_cache.SESSION_CACHE_TTL_HOURS_MAX
SESSION_CACHE_HKDF_INFO = _session_cache.SESSION_CACHE_HKDF_INFO

# 用户与注销域（唯一定义点在 yiban/store/users.py）：users / user_delete_requests 表的状态机、
# 注销与反悔、到期物理清除按原样再导出，既有 `db.load_users()` / `db.restore_user()` /
# `db.LastAdminError` 调用面与异常捕获不变。宽限期常量同时约束用户行与账号行的清除时机。
SOFT_DELETE_RETENTION_DAYS = _users.SOFT_DELETE_RETENTION_DAYS
SOFT_DELETE_RETENTION_SECONDS = _users.SOFT_DELETE_RETENTION_SECONDS
PURGE_SKIP_CANCELLED_OWNER = _users.PURGE_SKIP_CANCELLED_OWNER
DuplicateOwnerError = _users.DuplicateOwnerError
LastAdminError = _users.LastAdminError
set_user_sid = _users.set_user_sid
load_users = _users.load_users
find_user = _users.find_user
find_user_any = _users.find_user_any
filter_mail_notify = _users.filter_mail_notify
admin_mail_recipients = _users.admin_mail_recipients
create_user = _users.create_user
update_user = _users.update_user
_assert_not_last_admin = _users._assert_not_last_admin
delete_user_with_accounts = _users.delete_user_with_accounts
set_user_role = _users.set_user_role
soft_delete_user_with_accounts = _users.soft_delete_user_with_accounts
restore_user = _users.restore_user
purge_deleted_users = _users.purge_deleted_users
purge_deleted_users_hard = _users.purge_deleted_users_hard
purge_old_delete_requests = _users.purge_old_delete_requests
record_user_delete_request = _users.record_user_delete_request
count_user_delete_requests = _users.count_user_delete_requests
is_last_registered_admin = _users.is_last_registered_admin
batch_user_ops = _users.batch_user_ops
_delete_user_delete_requests = _users._delete_user_delete_requests

# 每日清理域（唯一定义点在 yiban/store/cleanup.py）：清理编排与审计/账号保留期清除按原样
# 再导出，web 每日线程的 `db.run_daily_cleanup()`、signin 启动的
# `db.purge_expired_deleted_accounts()` 与测试直接调用的 `db._audit_cleanup(...)` 调用面不变。
run_daily_cleanup = _cleanup.run_daily_cleanup
_audit_cleanup = _cleanup._audit_cleanup
_purge_expired_deleted = _cleanup._purge_expired_deleted
purge_expired_deleted_accounts = _cleanup.purge_expired_deleted_accounts

# 迁移域（唯一定义点在 yiban/store/migrations.py）：建表/索引定义、migrate_v1..v17、版本编排
# `_run_migrations` 与迁移助手按原样再导出，既有 `db.migrate_v10(...)` / `db._ensure_column(...)`
# / `db._create_tables(...)` 调用面不变。`_MIGRATIONS` 是可变登记表，走下方模块类的读写转发
# （测试以 `db._MIGRATIONS = [...]` 缩窄或替换迁移集）。
MigrationDeferred = _migrations.MigrationDeferred
_ALLOWED_TABLES = _migrations._ALLOWED_TABLES
_table_columns = _migrations._table_columns
_ensure_column = _migrations._ensure_column
_ensure_index = _migrations._ensure_index
_create_tables = _migrations._create_tables
_chain_head = _migrations._chain_head
_MALFORMED_COL_RE = _migrations._MALFORMED_COL_RE
_malformed_schema_tables = _migrations._malformed_schema_tables
_create_verify_jobs_table = _migrations._create_verify_jobs_table
migrate_v1 = _migrations.migrate_v1
migrate_v2 = _migrations.migrate_v2
migrate_v3 = _migrations.migrate_v3
migrate_v4 = _migrations.migrate_v4
migrate_v5 = _migrations.migrate_v5
migrate_v6 = _migrations.migrate_v6
migrate_v7 = _migrations.migrate_v7
migrate_v8 = _migrations.migrate_v8
migrate_v9 = _migrations.migrate_v9
migrate_v10 = _migrations.migrate_v10
migrate_v11 = _migrations.migrate_v11
migrate_v12 = _migrations.migrate_v12
migrate_v13 = _migrations.migrate_v13
migrate_v14 = _migrations.migrate_v14
migrate_v15 = _migrations.migrate_v15
migrate_v16 = _migrations.migrate_v16
migrate_v17 = _migrations.migrate_v17
_run_migrations = _migrations._run_migrations

logger = logging.getLogger("yiban.db")

DB_DEFAULT = _connection.DB_DEFAULT

# 连接层再导出（唯一定义点在 yiban/store/connection.py）：`_conn`/`_db_file`/`_env_file`
# 读写都**转发**——全仓 190+ 处测试收尾 `db._conn = None` 与 `db._env_file = path` 若只写
# 一份快照就静默失效（connection 仍握真连接/旧路径）；`_conn_lock`（永不重绑）与
# `get_conn`/`is_initialized` 直接再导出即等价：本模块内部按裸名调用，既有
# `mock.patch.object(db, "get_conn"/"_conn_lock", …)` 打桩仍然生效。
get_conn = _connection.get_conn
is_initialized = _connection.is_initialized
_conn_lock = _connection._conn_lock

# 需要**读写转发**的模块级状态与账号域迁出名：模块级赋值/删除默认直写 `__dict__`、不触发
# 下方的魔术方法，故这些名字一律不进本模块的 `__dict__`——读取回落到唯一定义点，写入也
# 落到那里：
#   `_conn`/`_db_file`/`_env_file` → connection（全仓 190+ 处测试收尾 `db._conn = None`）；
#   `_AUDIT_KEY_CACHE` 等三个审计域进程内状态 → audit_chain（test_rekey_key_source 的
#   `db._AUDIT_KEY_CACHE = None` 必须真的清掉密钥缓存）；
#   `_MIGRATIONS` → migrations（测试以 `db._MIGRATIONS = [...]` 缩窄/替换迁移集，
#   `_run_migrations` 必须读到改写后的登记表）；
#   账号域全部迁出名 → accounts：`db._decrypt_row` / `db.decrypt_account_rows` 被
#   tests/test_a2_decrypt_out_of_lock.py 打桩后，**accounts 模块内部**的调用点必须看到替身；
#   会话缓存域全部迁出名 → session_cache：`db._session_cache_now` 被
#   tests/test_session_cache_db.py 打桩（读写同钟），**session_cache 模块内部**的
#   `get_session_cache` / `set_session_cache` 调用点必须看到替身；
#   快照式再导出只换掉门面那一份，内部照旧调真名——打桩静默失效。
# 其余名字（`_conn_lock` 永不重绑、审计/迁移/事件/用户/校验任务/领取池域函数与常量）按
# 快照式再导出即等价；会话缓存域的四个常量同样按常量再导出（无内部惰性读取之外的语义）。
_FORWARDED_STATE = {
    "_conn": _connection,
    "_db_file": _connection,
    "_env_file": _connection,
    "_AUDIT_KEY_CACHE": _audit_chain,
    "_AUDIT_FAIL_UNFLUSHED": _audit_chain,
    "_AUDIT_FAIL_UNFLUSHED_DB": _audit_chain,
    "_MIGRATIONS": _migrations,
    # 账号域迁出名（唯一定义点在 yiban/store/accounts.py）
    "_mask_phone_display": _accounts,
    "_decrypt_row": _accounts,
    "_apply_plaintext_heal": _accounts,
    "_row_to_account": _accounts,
    "_is_encrypted_value": _accounts,
    "_encrypt_field": _accounts,
    "accounts_snapshot": _accounts,
    "load_accounts_raw": _accounts,
    "decrypt_account_rows": _accounts,
    "read_accounts": _accounts,
    "load_accounts": _accounts,
    "_next_sort_order": _accounts,
    "_convert_integrity_error": _accounts,
    "add_account": _accounts,
    "update_account": _accounts,
    "set_account_deleted": _accounts,
    "purge_account": _accounts,
    "update_account_status": _accounts,
    "set_user_paused": _accounts,
    "move_account": _accounts,
    "delete_accounts_by_owner": _accounts,
    "replace_accounts": _accounts,
    "batch_account_ops": _accounts,
    "update_account_status_if": _accounts,
    # 会话缓存域迁出名（唯一定义点在 yiban/store/session_cache.py）
    "_session_cache_now": _session_cache,
    "_session_cache_key": _session_cache,
    "_session_cache_ttl_hours": _session_cache,
    "get_session_cache": _session_cache,
    "set_session_cache": _session_cache,
    "clear_session_cache": _session_cache,
}
# delattr 撤下的名字（见 _StateForwardingModule.__delattr__）：名字重新可读即移出
_FORWARDED_STATE_HIDDEN = set()


def __getattr__(name):
    """PEP 562：转发名（连接三态、审计/迁移域可变状态、账号域迁出名）读取回落到定义点。"""
    mod = _FORWARDED_STATE.get(name)
    if mod is not None:
        if name in _FORWARDED_STATE_HIDDEN:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _StateForwardingModule(types.ModuleType):
    """转发名**写入/撤销**（`db._conn = None`、`db._AUDIT_KEY_CACHE = None`、
    `db.add_account = 替身`、`del db._conn`）。

    模块级赋值/删除默认直写 `__dict__`、不触发魔术方法，故本类只影响外部写入与
    mock/pytest 的撤销路径；本文件自身的名字绑定不受影响。
    """

    def __setattr__(self, name, value):
        mod = _FORWARDED_STATE.get(name)
        if mod is not None:
            _FORWARDED_STATE_HIDDEN.discard(name)
            setattr(mod, name, value)
            return
        types.ModuleType.__setattr__(self, name, value)

    def __delattr__(self, name):
        """撤销外部赋值：把名字从门面上摘下来（读取随即回落到真状态）。

        **为什么必须"摘下来"而不是去删真状态**：`mock.patch.object` /
        `mock.patch("yiban.store.db._conn", …)` 对不在 `__dict__` 里的名字走
        `delattr` 撤销路径，随后按"删完名字还在不在"决定要不要 `setattr` 回原值
        （`unittest.mock._patch.__exit__`）——若删不掉（`__getattr__` 照旧转发），
        原值永不被恢复，打桩静默残留；pytest `monkeypatch.delattr` 的 undo 同理
        （它靠"delattr 过"来记账、undo 时 setattr 原值）。故这里只在本模块层面
        隐藏该名字（`_FORWARDED_STATE_HIDDEN`），真状态与各定义点的内部使用
        一概不动；紧跟其后的 `setattr` 原值会经 `__setattr__` 写回并解除隐藏。
        """
        if name in _FORWARDED_STATE:
            _FORWARDED_STATE_HIDDEN.add(name)
            return
        types.ModuleType.__delattr__(self, name)


sys.modules[__name__].__class__ = _StateForwardingModule


# IP 加盐哈希（Phase 4）
_TRACK_SALT_CACHE = None
_TRACK_SALT_LOCK = threading.Lock()


def init_db(db_file=None, migrate_from=None, env_file=None, cleanup=True, migrate=True):
    """初始化连接与表结构；可选自动迁移（migrate_from 提供 json 文件基路径，如 /path/accounts.json）。

    env_file：.env 路径（加密密钥来源），须与调用方一致（web 用 --env 参数时必传），
    None 时按 YIBAN_ENV_FILE → 当前工作目录 ".env" 回落（CLI/取证类
    调用方应显式传入，勿让密钥来源依赖 cwd）。
    cleanup：默认 True 执行启动清理（审计/事件旧数据、过期软删用户等）；
    校验类工具应传 False，避免只读校验改变数据。
    migrate：默认 True 执行迁移；只读校验类工具应传 False——迁移会重写审计链
    （v3 rechain）等，使"被校验对象在校验过程中被改动"。
    """
    # 库路径 / .env 路径**无条件刷新**（即使连接已存在——它们是"最近一次 init_db 的
    # 来源"），经 connection 的显式 API 写入
    _connection.set_env_file(env_file)
    db_path = db_file or os.environ.get("YIBAN_DB_FILE", DB_DEFAULT)
    _connection.set_db_file(db_path)
    conn = _connection.current()
    if conn is not None:
        return conn
    with _conn_lock:
        conn = _connection.current()
        if conn is not None:
            return conn
        conn = sqlite3.connect(db_path, check_same_thread=False)
        # 先登记再配置：建表/迁移函数内部会经 get_conn() 取"当前连接"，故连接必须先
        # 入册，其后才逐条 PRAGMA/DDL
        _connection.set_conn(conn)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # 5000ms 在夜间批量签到/整表重建等长事务窗口内不够，业务写
        # 路径无重试，超限即 500——提到 15s 并保留 audit() 自身的 3 次重试
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=OFF")
        _create_tables(conn)
        # 通用幂等迁移框架（Phase 0）：按 PRAGMA user_version 顺序执行；
        # 核心迁移失败会关闭连接并抛出，阻断启动；可选迁移失败继续后续迁移但不提升版本。
        try:
            if migrate:
                _run_migrations(conn)
                # 自动迁移（幂等：库存在但空表 + JSON 存在才导入）
                if migrate_from:
                    _maybe_migrate(conn, migrate_from)
        except Exception:
            with contextlib.suppress(Exception):
                conn.close()
            _connection.reset_conn()
            raise
        if cleanup:
            run_daily_cleanup()
        return conn


def resolve_env_file(cli_value=None):
    """解析显式密钥来源路径：命令行 --env → YIBAN_ENV_FILE → None。

    给 CLI/取证脚本传给 init_db(env_file=…) 用：这样密钥来源与进程 cwd 解耦。
    刻意不回落成字面量 ".env"——那等于把"来源不确定"伪装成"来源已指定"，
    会绕过 _assert_key_source_certain 的防游离落盘检查；返回 None 时由
    _resolve_key_env_file 的有序回落链决定实际路径并在来源不确定时拒绝生成新钥。
    """
    v = (cli_value or "").strip()
    if v:
        return v
    return (os.environ.get("YIBAN_ENV_FILE") or "").strip() or None


def require_existing_env_file(cli_value=None):
    """resolve_env_file + 显式来源存在性校验；不存在则抛 ValueError。

    为什么必须校验：--env 一旦给出，db 层就认为"密钥来源已确定"，防游离落盘的
    _assert_key_source_certain 对它不再生效。路径打错（少写一层目录、部署迁移后
    旧路径）时，工具会在该位置新建 .env 并生成一把**新**审计密钥，把这次留痕用
    第三把钥匙签名——真实哈希链从这条起判破，正是本任务要治的病症的新入口。
    未显式给出（cli_value 为空）时不校验，交由回落链判定，保持既有行为。
    """
    path = resolve_env_file(cli_value)
    if (cli_value or "").strip() and not os.path.exists(path):
        raise ValueError(
            f"--env 指定的 .env 不存在: {path}（拒绝在该路径新建 .env 并生成新审计密钥；"
            "请核对路径，或去掉 --env 改用 YIBAN_ENV_FILE）"
        )
    return path


def _begin_immediate(conn):
    """统一的写事务入口：遗留未提交事务先安全回滚再 BEGIN。

    原各写路径直接 BEGIN IMMEDIATE，一旦存在遗留事务（任何写路径漏 commit/rollback
    的 bug）即抛 "within a transaction" 并连锁锁死全部写路径；audit() 的旧 M8 防御
    只覆盖自己。统一走本函数：遗留半事务按安全默认丢弃并 ERROR 留痕定位根因。
    """
    if conn.in_transaction:
        logger.error(
            "检测到遗留未提交事务，已回滚解除写锁（未知半事务按安全默认丢弃）——"
            "请检查此前调用路径是否有写操作未 commit/rollback"
        )
        conn.rollback()
    conn.execute('BEGIN IMMEDIATE')


# ---------------------------------------------------------------------------
# 可视化表（Phase 4）
# ---------------------------------------------------------------------------
def _write_track_salt_to_env_file(env_file, salt):
    """把新生成的 YIBAN_TRACK_SALT 写入 .env（保留其他行，原子替换）。

    读-写-替换整体包进共享 env_lock：与 web 写 .env 互斥；锁内仍保留
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
        # 创建即 0600（盐泄漏 = IP/手机号哈希可离线枚举反查）
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, env_file)
        with contextlib.suppress(OSError):
            os.chmod(env_file, 0o600)
        return salt


def _track_salt():
    """获取 IP 加盐哈希用的盐：环境变量优先，回退 .env，缺失时生成。

    .env 路径回落顺序与审计密钥一致：init_db(env_file=…) →
    YIBAN_ENV_FILE → 当前目录 ".env"；来源只能靠 cwd 兜底且文件不存在时拒绝生成。
    """
    global _TRACK_SALT_CACHE
    env_file, from_cwd = _resolve_key_env_file()
    env_salt = os.environ.get("YIBAN_TRACK_SALT", "").strip()
    if env_salt:
        if len(env_salt) < 16:
            # 弱盐告警（不拒绝——存量部署换盐会使既有哈希关联失效）；
            # 盐被猜测即可离线反查 IP/手机号哈希
            logger.warning("YIBAN_TRACK_SALT 长度过短（<16），易被枚举，建议更换为 32 位以上随机串")
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
        _assert_key_source_certain("追踪盐", env_file, from_cwd)
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

    有意与 hash_ip 的 HMAC 口径不同：本函数的输出会作为**库内关联键**存储
    （time_pref 冷却等），更换算法将使全部存量关联失效；等值查询用途下
    sha256(salt:input) 无现实攻击面（长度扩展需要构造可验证的 MAC，此处
    哈希仅用于存储比对）。评审结论：保持口径并记录理由。
    """
    salt = _track_salt()
    return hashlib.sha256(f"{salt}:{phone}".encode("utf-8")).hexdigest()


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
    key = account_crypto.load_key(_connection._env_file) if accounts else None
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
    bak = f"{path}.bak-{clock.now().strftime('%Y%m%d')}"
    if os.path.exists(bak):
        seq = 1
        while os.path.exists(f"{bak}-{seq}"):
            seq += 1
        new_bak = f"{bak}-{seq}"
        logger.warning("迁移备份目标 %s 已存在，源文件改存为 %s", bak, new_bak)
        bak = new_bak
    os.rename(path, bak)
    with contextlib.suppress(OSError):  # 非 POSIX 平台或权限受限：尽力而为，不阻断迁移
        os.chmod(bak, 0o600)
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
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False)
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, bak)
                    logger.warning("迁移 .bak 含明文字段，已重写为加密版（%s）", bak)
        except (OSError, ValueError, TypeError):
            logger.warning("迁移备份重写加密失败（.bak 保持原样，请手工检查权限）: %s", bak)


# 时钟跳变保护参数（2026-08-28 审查 M3）：
# 允许的"时间前进"上限。软删保留期 7 天——系统时间被拨快 8 天，刚软删 1 秒的
# 账号会在下次清理时被立即物理清除、7 天反悔窗口归零。取 72h：每日正常运行的
# 服务不会超过；停机 >3 天后的首轮清理会被跳过并触发告警，需人工核实时钟后用
# scripts/clock_guard_reset.py 显式重置（用户裁决 2026-08-29：
# 刻意不自动恢复——自动把参照点拨到当前时间等于给"拨快一次、下轮洗白"开通道）。
_CLOCK_ALLOW_FWD_HOURS = 72
# 允许的"时间回拨"上限（秒）：正常 NTP 校正是秒级，回拨超过 1h 视为异常
_CLOCK_ALLOW_BACK_SECONDS = 3600
# 守卫失败告警在 app_meta 的留痕键（web 每日线程读取并发邮件；人工重置后清除）
_CLOCK_GUARD_ALERT_KEY = "clock_guard_alert"


def _record_clock_guard_alert(note):
    """守卫拦截时把告警落到 app_meta。

    原实现 ok=False 仅 logger.error：无任何告警出口，且因不更新参照点，5 处清理
    **永久**冻结（软删数据永不物理清除、审计/事件表无限膨胀）——与注释承诺的
    「清理推迟一天」相悖，日志无人看时静默腐烂。此处用**独立短连接**写入：
    守卫运行在调用方事务内，随后调用方会 rollback，同连接写入会被一起回滚。
    写失败不影响主流程（只告警）。JSON 结构 {ts, note}。
    """
    try:
        target_db = _connection._db_file or os.environ.get("YIBAN_DB_FILE") or DB_DEFAULT
        conn2 = sqlite3.connect(target_db, timeout=5)
        try:
            conn2.execute(
                "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                (
                    _CLOCK_GUARD_ALERT_KEY,
                    json.dumps(
                        {
                            "ts": clock.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "note": note,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            conn2.commit()
        finally:
            conn2.close()
    except Exception as e:
        logger.warning("时钟守卫告警留痕失败: %s", e)


def clock_guard_alert():
    """读取未清除的时钟守卫告警（供 web 每日线程/体检调用）。无告警返回 None。"""
    try:
        with _conn_lock:
            conn = get_conn()
            r = conn.execute(
                "SELECT value FROM app_meta WHERE key=?", (_CLOCK_GUARD_ALERT_KEY,)
            ).fetchone()
        if r is None or not r["value"]:
            return None
        try:
            data = json.loads(r["value"])
            if isinstance(data, dict) and data.get("note"):
                return data
        except ValueError:
            pass
        return {"ts": "", "note": str(r["value"])}
    except Exception as e:
        logger.warning("读取时钟守卫告警失败: %s", e)
        return None


# ---------------------------------------------------------------------------
# app_meta 通用单键读写
# ---------------------------------------------------------------------------
# app_meta 此前只有内联 SQL（见 _record_clock_guard_alert / record_audit_anchor）。
# 告警通道健康日报需要一把"当日串键"做跨进程重启的每日去重——进程内 dict（如
# _mail_alert_ts）重启即失效，兜不住"每次重启各发一封"。故在此收口一对最小读写：
# 不新建表、不加迁移，值统一按 TEXT 存（调用方自行放日期串或 JSON）。
def get_meta(key, default=""):
    """读取 app_meta 单键值（str）。键不存在 / 表缺失（旧库未跑 v12）/ 读失败 → default。

    刻意不抛：本函数的调用方是"尽力而为"的元数据留痕（如日报去重），读失败时按
    "无记录"继续即可，不能把兜底路径变成新故障点。
    """
    try:
        with _conn_lock:
            conn = get_conn()
            row = conn.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
        if row is None or row["value"] is None:
            return default
        return str(row["value"])
    except Exception as e:
        logger.warning("读取 app_meta[%s] 失败（按无记录处理）: %s", key, e)
        return default


def set_meta(key, value):
    """写入/更新 app_meta 单键值（INSERT OR REPLACE）。返回 True 表示已落库。

    与其余写路径同口径走 _begin_immediate（WAL 下该 INSERT 即持 RESERVED 写锁）；
    失败只告警并返回 False——元数据留痕不得放大故障，也不得留下未决事务。
    """
    try:
        with _conn_lock:
            conn = get_conn()
            _begin_immediate(conn)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                    (key, str(value)),
                )
                conn.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    conn.rollback()
                raise
        return True
    except Exception as e:
        logger.warning("写入 app_meta[%s] 失败: %s", key, e)
        return False


def _clock_jump_guard(conn, key):
    """以 app_meta 记录的最近一次 seen-now 为参照，检测系统时钟异常跳变。

    返回 (ok, note)：ok=False 时调用方应跳过本次清理（防"拨快后刚软删的
    数据被立即物理清除"）；note 为告警文本或空串。ok=False 时告警已由
    _record_clock_guard_alert 落入 app_meta（web 每日线程发邮件），恢复清理
    需人工确认时钟正确后运行 scripts/clock_guard_reset.py 显式重置参照点。
    每次调用都会把当前时间 upsert 进 app_meta（ok 路径）——该 INSERT 同时充当
    库级写锁（WAL 下 INSERT 即持 RESERVED 锁），调用方无需另开 BEGIN IMMEDIATE。
    """
    now = clock.now()
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
        note = (
            f"系统时间异常跳变（上次记录 {row['value']}，当前 {ts}，"
            f"前进 {fwd / 3600:.1f}h / 回拨 {back / 3600:.1f}h），"
            "已跳过本次物理清理以防误删；请核实系统时间，确认正确后运行 "
            "scripts/clock_guard_reset.py 重置（清理将保持冻结直至重置）"
        )
        logger.error("%s", note)
        _record_clock_guard_alert(note)
        return False, note
    conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)", (key, ts))
    return True, ""


def _record_purge_event(conn, table, kind, cutoff, deleted, before, after, audit_seq=None):
    """把一次物理删除写入 app_meta 留痕（与删除同事务，由调用方 commit）。

    before/after 是删除前后的 (min_id, max_id)；表被删空时元素为 None。
    audit_seq 仅 audit_logs 的删除使用，值为累计数**自增后**的数：判据要精确知道
    "哪些留痕发生在某条锚点之后"，而这无法靠 ts 字符串比较得出——每日流程是"先
    清理、后写锚点"，同一天的两条记录 ts 先后与判据要的"锚点之后"根本不对应。

    写失败刻意上抛（不静默吞）：由调用方回滚，宁可本轮不删，也不留下
    "删了却无留痕"的删除——那种删除会让稠密性判据把合法清理误判成篡改。
    """
    if deleted <= 0:
        return
    event = {
        "kind": kind,
        "table": table,
        "cutoff": cutoff or "",
        "deleted": int(deleted),
        "before_min": before[0] if before else None,
        "before_max": before[1] if before else None,
        "after_min": after[0] if after else None,
        "after_max": after[1] if after else None,
        "ts": clock.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if audit_seq is not None:
        event["audit_seq"] = int(audit_seq)
    events = _audit_purge_events(conn)
    events.append(event)
    if len(events) > _PURGE_EVENTS_KEEP:
        events = events[-_PURGE_EVENTS_KEEP:]
    conn.execute(
        "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
        (_AUDIT_PURGE_EVENTS_KEY, json.dumps(events, ensure_ascii=False)),
    )
    if table == "audit_logs":
        conn.execute(
            "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
            (_AUDIT_PURGE_TOTAL_KEY, str(_audit_purge_total(conn) + int(deleted))),
        )


def _table_min_max(conn, table):
    """指定表的 (min_id, max_id)；空表或表不存在 → (None, None)。

    table 只接受 _ALLOWED_TABLES 里的字面量——留痕是取证数据，不能成为注入面。
    """
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"留痕不支持的表名: {table}")
    try:
        row = conn.execute(
            "SELECT MIN(id) AS mn, MAX(id) AS mx FROM " + table
        ).fetchone()
    except sqlite3.Error:
        return None, None
    if row is None:
        return None, None
    return row["mn"], row["mx"]


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


def _cascade_phone_owned(conn, phones):
    """账号物理删除时按手机号连带清理全部以 phone 为键的业务数据（须在调用方事务内）。

    单点收口：此前是 time_prefs / session_cache / sign_events 三处手写散点，
    每新增一张 phone 键表就要在全部删除路径上人肉补一遍——漏项是必然的
    （verify_jobs 就是这样漏掉的：删号后其明文手机号与错误文本驻留至保留期满，
    account_id 还悬空；sign_claims 上线时同样被这条枚举测试当场拦下）。现在所有
    删除路径只调本函数，schema 里凡以 phone 为键的表都必须在这里列出；
    `tests/test_verify_jobs_lifecycle.py` 有枚举测试兜底，漏加会直接红。

    注意：不能用 clear_session_cache()（其自带 BEGIN IMMEDIATE 事务，嵌套会撞
    "within a transaction"），故在调用方事务内直接 DELETE。
    软删除路径不调用（宽限期内恢复后事件历史仍需保留）。
    """
    if not phones:
        return
    rows = [(p,) for p in phones]
    conn.executemany("DELETE FROM time_prefs WHERE phone=?", rows)
    conn.executemany("DELETE FROM session_cache WHERE phone=?", rows)
    conn.executemany("DELETE FROM sign_events WHERE phone=?", rows)
    conn.executemany("DELETE FROM verify_jobs WHERE phone=?", rows)
    conn.executemany("DELETE FROM sign_claims WHERE phone=?", rows)


def _clear_session_cache_by_phones(conn, phones):
    """按手机号批量清除会话缓存（**部分清理**路径专用，须在调用方事务内）。

    与 `_cascade_phone_owned` 的区别是刻意的：改绑手机号（旧号的 cookie/csrf
    主键与 AAD 均按旧号，不复用）与用户注销（账号软删但不立即物理清除，
    time_prefs 保留至物理清除、sign_events 保留供恢复后查看历史）这两条路径
    只停用凭据缓存，不动其余历史数据。新的"账号物理删除"路径请用前者。
    """
    if not phones:
        return
    conn.executemany("DELETE FROM session_cache WHERE phone=?", [(p,) for p in phones])
