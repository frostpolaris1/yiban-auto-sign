# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""SQLite 数据访问层（web / signin 双进程共用）的门面与跨域粘合助手。

- accounts/users 数据从 JSON 整文件读写迁移到 SQLite（yiban.db，WAL 模式）
  ——根治并发覆盖 / 索引漂移 / 进程外覆盖三个历史问题
- 稳定 ID（id PK AUTOINCREMENT）：业务层用 id 寻址，不再受列表顺序漂移影响
- 密码/识别码字段在库内仍为 AES-GCM 密文（复用 account_crypto，解密在 load 时）
- 自动迁移：accounts/users 表为空且对应 JSON 存在 → 导入（幂等）→ JSON 改名 .bak 保留逃生门
- 操作审计：audit() 记录关键管理操作（多管理员追溯）
- 排序：sort_order 升序为签到顺序（移动 = 事务内交换/重排）

本模块再导出的同包模块（各域唯一定义点不在本模块）：
- `connection`：连接单例与路径（`_conn`/`_conn_lock`/`_db_file`/`_env_file`/`get_conn`）。
- `migrations`：建表/索引、`migrate_v1..v21`、版本编排 `_run_migrations` 与完整性校验
  （`MigrationIntegrityError`：迁移记录/登记产物缺失 ⇒ 拒启），以及 JSON → SQLite
  自动导入 `_maybe_migrate` / `_rename_backup`。
- `audit_chain`：`audit()` 写入链路、哈希链校验、库外锚点族、审计密钥来源与缓存。
- `events`：sign_events 的写入/查询/统计与保留期清理，以及 audit_logs 上的暂停冷却查询。
- `run_events`：内核进度事件表（v21）的写入入口与保留期清理（N2a 打点层）。
- `verify_jobs`：在线校验任务表的创建/领取/结算/取消、超龄回收与保留期清理。
- `claims`：旧签到领取池（`sign_claims`，单池化后冻结）的领取/续租/结算/放弃与清理；
  生产读口径已收口到 `queue_store`（见下），本域仅供既有单测覆盖。
- `queue_store`：任务队列 `sign_tasks`（台账单池化后的唯一生产台账）的批量领取/收尾/
  重排/回收/重签/事件签名，以及出口桶状态 `egress_state` 的读写。
- `users`：users / user_delete_requests 表的状态机、注销与反悔、到期清除。
- `cleanup`：每日清理编排（审计与账号保留期清除，并调用各域清理）。
- `accounts`：accounts 表的 CRUD、行加解密与运行期有效性判定。
- `session_cache`：session_cache 表族的读写、有效期判定与凭据加密。
- `time_prefs`：time_prefs 表的读写、拥挤度统计与保存冷却查询。
- `clock_meta`：app_meta 通用单键读写（供日报去重与审计锚点留痕）。
- `tracking`：追踪盐（YIBAN_TRACK_SALT）的取用/落盘与 IP、手机号加盐哈希。

本模块自身仍持有：启动编排 `init_db`（与冻结的历史迁移函数共存）、密钥来源解析
`resolve_env_file` / `require_existing_env_file`、写事务入口 `_begin_immediate`、时钟跳变
守卫本体 `_clock_jump_guard`，以及跨域粘合助手 `_record_purge_event` / `_table_min_max` /
`_cascade_phone_owned` / `_clear_session_cache_by_phones`。子模块反向经本门面按属性取这些
名字（见各模块的 `_facade()`）；`db._audit_hash = 替身`、`db._conn = None` 一类打桩面由本模块
的再导出与读写转发维持不变。
"""
import contextlib
import datetime
import json
import logging
import os
import sqlite3
import sys
import types

# 包导入引导：本模块已入包（`yiban.adapters.persistence.db`），正常导入路径下仓库根必然在 sys.path
# 里；这里仍补一次仓库根，作为"被以任意 sys.path 形状导入"的兜底（如容器里从
# `/app/scripts` 起手的老入口）。**必须先于任何 yiban 导入**（含下面的 infra 导入，
# 以及函数内的延迟导入）。
# 仓库根：本模块批 B 从 `yiban/store/db.py` 搬来 `yiban/adapters/persistence/db.py`，深一层。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 表级数据访问按表拆在 yiban/adapters/persistence/*，本模块只做门面：多数域按原名再导出，`claims` 与
# `verify_jobs` 两域是重命名别名（逐条见各自绑定处的行尾注释）。依赖方向单向 db → store
# （store 只在函数内延迟取连接），旧调用方（web/app.py、测试）继续用 db.xxx。
# account_crypto 的唯一自用点（JSON 导入）已随迁移域迁入 migrations.py；保留绑定是因为
# `db.account_crypto` 仍被测试直接取用（test_db_residue / test_account_plaintext_patch
# 取 load_key / encrypt_password），删除即取用面损失。
from yiban.adapters.config import account_crypto, env_io  # noqa: E402, F401
from yiban.adapters.persistence import accounts as _accounts  # noqa: E402
from yiban.adapters.persistence import audit_chain as _audit_chain  # noqa: E402
from yiban.adapters.persistence import claims as _claims  # noqa: E402
from yiban.adapters.persistence import cleanup as _cleanup  # noqa: E402
from yiban.adapters.persistence import clock_meta as _clock_meta  # noqa: E402
from yiban.adapters.persistence import connection as _connection  # noqa: E402
from yiban.adapters.persistence import emit_ledger as _emit_ledger  # noqa: E402
from yiban.adapters.persistence import events as _events  # noqa: E402
from yiban.adapters.persistence import migrations as _migrations  # noqa: E402
from yiban.adapters.persistence import queue_store as _queue_store  # noqa: E402
from yiban.adapters.persistence import run_events as _run_events  # noqa: E402
from yiban.adapters.persistence import session_cache as _session_cache  # noqa: E402
from yiban.adapters.persistence import time_prefs as _time_prefs  # noqa: E402
from yiban.adapters.persistence import tracking as _tracking  # noqa: E402
from yiban.adapters.persistence import users as _users  # noqa: E402
from yiban.adapters.persistence import verify_jobs as _verify_jobs  # noqa: E402
from yiban.core import clock  # noqa: E402

account_is_signable = _accounts.is_signable
account_signs_in = _accounts.signs_in
purge_orphan_session_cache = _accounts.purge_orphan_session_cache
# accounts.phone 唯一约束冲突的可区分异常，定义点随 accounts 表在 yiban/adapters/persistence/accounts.py
DuplicatePhoneError = _accounts.DuplicatePhoneError

VERIFY_JOB_RETENTION_DAYS = _verify_jobs.VERIFY_JOB_RETENTION_DAYS
VERIFY_JOB_PENDING = _verify_jobs.VERIFY_JOB_PENDING
VERIFY_JOB_RUNNING = _verify_jobs.VERIFY_JOB_RUNNING
VERIFY_JOB_DONE = _verify_jobs.VERIFY_JOB_DONE
VERIFY_JOB_REJECTED = _verify_jobs.VERIFY_JOB_REJECTED
VERIFY_JOB_CANCELLED = _verify_jobs.VERIFY_JOB_CANCELLED
VERIFY_JOB_STALE_SECONDS = _verify_jobs.VERIFY_JOB_STALE_SECONDS
VERIFY_JOB_STALE_MSG = _verify_jobs.VERIFY_JOB_STALE_MSG

create_verify_job = _verify_jobs.create  # 域内是短动词名（create/get/claim/finish/cancel），门面上统一补 `_verify_job` 后缀
get_verify_job = _verify_jobs.get
claim_verify_job = _verify_jobs.claim
finish_verify_job = _verify_jobs.finish
cancel_verify_job = _verify_jobs.cancel
count_active_verify_jobs = _verify_jobs.count_active
reclaim_stale_verify_jobs = _verify_jobs.reclaim_stale
purge_verify_jobs = _verify_jobs.purge

# 签到领取池（v17，多执行体协调）：唯一定义点在 `yiban/adapters/persistence/claims.py`
CLAIM_LEASE_SECONDS = _claims.LEASE_SECONDS  # 账号级租约（900s，旧领取池冻结件）；queue_store 的任务级租约已随工单 l1rx 删除
CLAIM_RETENTION_DAYS = _claims.RETENTION_DAYS
CLAIM_STATE_CLAIMED = _claims.STATE_CLAIMED
CLAIM_STATE_DONE = _claims.STATE_DONE  # 了结词表成员见 yiban.core.status.CLAIM_DONE_STATUSES
CLAIM_STATE_FAILED = _claims.STATE_FAILED
CLAIM_SETTLED_STATES = _claims.SETTLED_STATES
CLAIM_OPEN_STATES = _claims.OPEN_STATES
claim_new_owner = _claims.new_owner
claim_sign_account = _claims.try_claim  # 门面名与域内名不同：`db.try_claim` 不存在
claim_touch = _claims.touch  # 续租只续自己持有的，返回 False = 已被接管
claim_reap_unreported = _claims.reap_unreported  # 轮末收尸：本轮领到却无结论的行显式弃权
claim_reap_abandoned = _claims.reap_abandoned  # 监督进程对已确认死亡的执行体名下在领行收尸
claim_settle = _claims.settle  # 必须带 try_claim 返回的 epoch，否则迟到的写会覆盖接管者的结论
claim_give_up = _claims.give_up  # 弃单不等于收工：置 failed（当日仍未了结），租约即刻放开
claim_states_for_day = _claims.states_for_day
claim_in_flight = _claims.in_flight_phones
claim_stats = _claims.stats
claim_activity = _claims.activity
claim_owners_for_day = _claims.owners_for_day
claim_latest_day = _claims.latest_claims_day
claim_owners_since = _claims.owners_since
purge_sign_claims = _claims.purge  # 只按 RETENTION_DAYS 清追溯用存量，展示口径不读它

# 任务队列（sign_tasks）展示读口径：台账单池化后展示/补签闸门读的就是这张表（唯一台账）。
# 上方 `claim_*` 是旧领取池（sign_claims，冻结）的读法，仅供既有单测覆盖，不再是生产读口。
task_stats = _queue_store.day_counts  # 当日各 state 计数与派生（settled/open/total）；读不通回 None 哨兵，别名原样透传
task_open_count = _queue_store.open_count  # 当日未了结且可领取（vshard>=0）的行数——无分片上下文的了结闸门；读不通回 None 哨兵，别名原样透传
task_owners_for_day = _queue_store.owners_for_day  # 当日 phone -> owner（一次取全）
task_run_at_by_phone = _queue_store.run_at_by_phone  # 当日 phone -> run_at（计划时刻唯一事实源）；读不通回 None 哨兵，别名原样透传
task_activity = _queue_store.activity  # 当日按执行体归属的 KPI 计数（已折 KPI 三键）
task_latest_day = _queue_store.latest_day  # 最近一次有记录的业务日
task_owners_since = _queue_store.owners_since  # 保留期内出现过的执行体身份串
purge_sign_tasks = _queue_store.purge  # 唯一台账（sign_tasks）的保留期清理（带时钟跳变守卫）

# 审计链域（唯一定义点在 yiban/adapters/persistence/audit_chain.py）：函数与常量按原样再导出，既有
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
_backfill_audit_hashes = _audit_chain._backfill_audit_hashes

_bump_audit_write_failure = _audit_chain._bump_audit_write_failure
_unflushed_audit_failures = _audit_chain._unflushed_audit_failures
_reset_audit_fail_memory = _audit_chain._reset_audit_fail_memory
audit_persisted_write_failures = _audit_chain.audit_persisted_write_failures
audit_write_failures = _audit_chain.audit_write_failures
audit = _audit_chain.audit
# 请求作用域（审计行携带"哪个请求做的"）与"业务+审计同事务"原语：
# `audit_unit` 上下文管理器、`record_in_txn`（业务事务内插审计行）、
# `audit_or_refuse`（跨存储调用点的 fail-closed 审计，失败抛 AuditWriteRefused）。
set_request_scope = _audit_chain.set_request_scope
current_request_scope = _audit_chain.current_request_scope
new_request_scope_id = _audit_chain.new_request_scope_id
record_in_txn = _audit_chain.record_in_txn
audit_unit = _audit_chain.audit_unit
audit_or_refuse = _audit_chain.audit_or_refuse
AuditWriteRefused = _audit_chain.AuditWriteRefused
audit_head_hash = _audit_chain.audit_head_hash
audit_head_hash_ex = _audit_chain.audit_head_hash_ex
audit_row_count = _audit_chain.audit_row_count
verify_audit_chain = _audit_chain.verify_audit_chain
audit_write_failures_unnotified = _audit_chain.audit_write_failures_unnotified
mark_audit_write_failures_notified = _audit_chain.mark_audit_write_failures_notified
audit_alert_signature = _audit_chain.audit_alert_signature
audit_alert_needs_attention = _audit_chain.audit_alert_needs_attention
mark_audit_alert_sent = _audit_chain.mark_audit_alert_sent

audit_anchor_path = _audit_chain.audit_anchor_path
_anchor_line_sha = _audit_chain._anchor_line_sha
_parse_anchor_line = _audit_chain._parse_anchor_line
_read_anchor_lines = _audit_chain._read_anchor_lines
_read_anchor_lines_ex = _audit_chain._read_anchor_lines_ex
_parse_anchor_lines = _audit_chain._parse_anchor_lines
_get_anchor_meta = _audit_chain._get_anchor_meta
_anchor_meta_line_count = _audit_chain._anchor_meta_line_count
_audit_purge_total = _audit_chain._audit_purge_total
_audit_purge_events = _audit_chain._audit_purge_events
audit_purge_total = _audit_chain.audit_purge_total
audit_purge_events = _audit_chain.audit_purge_events
record_audit_anchor = _audit_chain.record_audit_anchor
_record_anchor_trace = _audit_chain._record_anchor_trace
_last_audit_anchor = _audit_chain._last_audit_anchor
_last_anchor_of = _audit_chain._last_anchor_of
_max_anchor_of = _audit_chain._max_anchor_of
_anchor_file_state = _audit_chain._anchor_file_state
_anchor_file_state_ex = _audit_chain._anchor_file_state_ex
_anchor_status = _audit_chain._anchor_status
verify_audit_anchor = _audit_chain.verify_audit_anchor
_purge_events_after_anchor = _audit_chain._purge_events_after_anchor
_purge_event_sets_min = _audit_chain._purge_event_sets_min
audit_health = _audit_chain.audit_health
# 巡检面「审计链自洽」块（第二刀 ⑤，工单 vjy9）：三态裁决的唯一映射、锚点摘要、
# 已知欠账，以及体检快照的写/读（写方＝每日线程，读方＝巡检端点；端点不重算）。
audit_verdict = _audit_chain.audit_verdict
VERDICT_EXIT_CODES = _audit_chain.VERDICT_EXIT_CODES
VERDICT_INDETERMINATE = _audit_chain.VERDICT_INDETERMINATE
audit_anchor_summary = _audit_chain.audit_anchor_summary
audit_known_debt = _audit_chain.audit_known_debt
write_audit_health_snapshot = _audit_chain.write_audit_health_snapshot
read_audit_health_snapshot = _audit_chain.read_audit_health_snapshot

_AUDIT_KEY_LOCK = _audit_chain._AUDIT_KEY_LOCK
_AUDIT_FAIL_KEY = _audit_chain._AUDIT_FAIL_KEY
_AUDIT_FAIL_NOTIFIED_KEY = _audit_chain._AUDIT_FAIL_NOTIFIED_KEY
_AUDIT_ALERT_STATE_KEY = _audit_chain._AUDIT_ALERT_STATE_KEY
_AUDIT_FAIL_LOCK = _audit_chain._AUDIT_FAIL_LOCK
_AUDIT_RETRIES = _audit_chain._AUDIT_RETRIES
_AUDIT_RETRY_BASE_DELAY = _audit_chain._AUDIT_RETRY_BASE_DELAY
_ANCHOR_V0_TOKENS = _audit_chain._ANCHOR_V0_TOKENS
_ANCHOR_V1_TOKENS = _audit_chain._ANCHOR_V1_TOKENS
_ANCHOR_V2_TOKENS = _audit_chain._ANCHOR_V2_TOKENS
# 老格式（三字段）历史前缀的识别：截止日常量与判据函数。`_legacy_prefix_end` 同时是
# `audit_anchor_summary.old_format_lines` 与 `_anchor_file_state_ex` 的输入——两处各写
# 一份判据就会让"已知欠账计数"与"无法定论判据"对不上。
LEGACY_ANCHOR_LAST_DAY = _audit_chain.LEGACY_ANCHOR_LAST_DAY
_legacy_prefix_end = _audit_chain._legacy_prefix_end
_is_legacy_ts = _audit_chain._is_legacy_ts
_ANCHOR_META_KEY = _audit_chain._ANCHOR_META_KEY
_AUDIT_PURGE_TOTAL_KEY = _audit_chain._AUDIT_PURGE_TOTAL_KEY
_AUDIT_PURGE_EVENTS_KEY = _audit_chain._AUDIT_PURGE_EVENTS_KEY
_PURGE_EVENTS_KEEP = _audit_chain._PURGE_EVENTS_KEEP
_ANCHOR_GENESIS = _audit_chain._ANCHOR_GENESIS

# 事件域（唯一定义点在 yiban/adapters/persistence/events.py）：写入/查询/统计与保留期清理按原样再导出，
# 既有 `db.add_sign_event()` / `db.sign_event_stats()` / `db._event_cleanup(...)` 调用面不变。
# 按表归属并入事件域的暂停冷却两名（`last_pause_at` / `pause_count_since`，查 audit_logs）
# 走下方读写转发，不在这里再导出。
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
attempt_dur_quantile = _events.attempt_dur_quantile  # 容量告警的实测输入（MF-56③）
_event_cleanup = _events._event_cleanup

# 内核进度事件域（唯一定义点在 yiban/adapters/persistence/run_events.py，N2a 打点层）：两个写入入口
# 按门面惯例补域后缀再导出；`yiban/adapters/persistence/cleanup.py` 的每日编排经
# `_facade().purge_run_events()` 调保留期清理（与 purge_sign_tasks 同形）。
# 节点值域（`NODES`）是下游消费者的契约面，按原名留在域模块，门面不另立别名
# ——两处名字会让"改哪一处"变成判断题。
report_run_event = _run_events.report
report_run_events = _run_events.report_many
purge_run_events = _run_events.purge

# 发射账本（唯一定义点在 yiban/adapters/persistence/emit_ledger.py，单 3zr5）：
# 「是否已经说过」的唯一入口，两者都返回"我是不是说的那一个"。全仓只有引擎侧的
# 去重成因调用；门面导出是为了让 `db.emit_once` 一类调用与既有导出同形。
emit_once = _emit_ledger.emit_once
emit_on_change = _emit_ledger.emit_on_change

# 会话缓存域（唯一定义点在 yiban/adapters/persistence/session_cache.py）：函数走下方读写转发（内部调用点
# 会被 `db._session_cache_now` 打桩），四个常量是不可变配置、全仓无重绑与打桩，按常量
# 再导出即等价。`db.SESSION_CACHE_TTL_HOURS_DEFAULT` 一类读取不变。
SESSION_CACHE_TTL_HOURS_DEFAULT = _session_cache.SESSION_CACHE_TTL_HOURS_DEFAULT
SESSION_CACHE_TTL_HOURS_MIN = _session_cache.SESSION_CACHE_TTL_HOURS_MIN
SESSION_CACHE_TTL_HOURS_MAX = _session_cache.SESSION_CACHE_TTL_HOURS_MAX
SESSION_CACHE_HKDF_INFO = _session_cache.SESSION_CACHE_HKDF_INFO

# 用户与注销域（唯一定义点在 yiban/adapters/persistence/users.py）：users / user_delete_requests 表的状态机、
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
find_user_by_id = _users.find_user_by_id
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

# 每日清理域（唯一定义点在 yiban/adapters/persistence/cleanup.py）：清理编排与审计/账号保留期清除按原样
# 再导出，web 每日线程的 `db.run_daily_cleanup()`、signin 启动的
# `db.purge_expired_deleted_accounts()` 与测试直接调用的 `db._audit_cleanup(...)` 调用面不变。
run_daily_cleanup = _cleanup.run_daily_cleanup
_audit_cleanup = _cleanup._audit_cleanup
_purge_expired_deleted = _cleanup._purge_expired_deleted
purge_expired_deleted_accounts = _cleanup.purge_expired_deleted_accounts

# 迁移域（唯一定义点在 yiban/adapters/persistence/migrations.py）：建表/索引定义、migrate_v1..v20、版本编排
# `_run_migrations` 与迁移助手按原样再导出，既有 `db.migrate_v10(...)` / `db._ensure_column(...)`
# / `db._create_tables(...)` 调用面不变。`_MIGRATIONS` 是可变登记表，走下方模块类的读写转发
# （测试以 `db._MIGRATIONS = [...]` 缩窄或替换迁移集）。JSON → SQLite 自动导入两名
# （`_maybe_migrate` / `_rename_backup`）同样走读写转发：门面内的 `init_db` 按属性晚解析
# 调用它们，快照式再导出会让 `db._maybe_migrate = 替身` 的打桩看不到。
MigrationDeferred = _migrations.MigrationDeferred
MigrationIntegrityError = _migrations.MigrationIntegrityError
_ALLOWED_TABLES = _migrations._ALLOWED_TABLES
_table_columns = _migrations._table_columns
_ensure_column = _migrations._ensure_column
_ensure_index = _migrations._ensure_index
_create_tables = _migrations._create_tables
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
migrate_v18 = _migrations.migrate_v18
migrate_v19 = _migrations.migrate_v19
migrate_v20 = _migrations.migrate_v20
migrate_v21 = _migrations.migrate_v21
_run_migrations = _migrations._run_migrations

logger = logging.getLogger("yiban.db")

DB_DEFAULT = _connection.DB_DEFAULT

# 连接层再导出（唯一定义点在 yiban/adapters/persistence/connection.py）：`_conn`/`_db_file`/`_env_file`
# 读写都**转发**——全仓 190+ 处测试收尾 `db._conn = None` 与 `db._env_file = path` 若只写
# 一份快照就静默失效（connection 仍握真连接/旧路径）；`_conn_lock`（永不重绑）与
# `get_conn`/`is_initialized` 直接再导出即等价：本模块内部按裸名调用，既有
# `mock.patch.object(db, "get_conn"/"_conn_lock", …)` 打桩仍然生效。
get_conn = _connection.get_conn
is_initialized = _connection.is_initialized
# "部署声明了领取池库路径"的只读判据：执行侧据此区分"未配库（放行）"与"配了库但
# 当前不可用（拒跑）"——两种形态都从 `is_initialized()=False` 出发、结论相反。
pool_db_declared = _connection.pool_db_declared
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
#   事件域按表归属并入的暂停冷却两名 → events、用户自选时间片域七名 → time_prefs：
#   调用方只经门面属性访问，读写转发让 `db.<名字> = 替身` / `del db.<名字>` 落到真定义点
#   （`last_time_pref_set_at` 体内按属性取的 `db.hash_phone` 读到的正是 tracking 域真身）；
#   app_meta 通用单键读写两名 → clock_meta：`db.get_meta = 替身` 要被各域（日报去重、
#   审计锚点）看见，转发面是这些名字的唯一宿主；
#   迁移域的 JSON 导入两名 → migrations：门面内 `init_db` 按属性晚解析调用 `_maybe_migrate`；
#   追踪盐域四名与盐缓存的**可变状态** `_TRACK_SALT_CACHE` → tracking：
#   `db._TRACK_SALT_CACHE = None`（tests/test_rekey_key_source.py 清盐缓存）必须真的清掉
#   定义点那份缓存，且 `db.hash_phone = 替身` 要被 time_prefs 的冷却查询看见。
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
    # 账号域迁出名（唯一定义点在 yiban/adapters/persistence/accounts.py）
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
    # 单行窄取（dnox-B）：详情端点只解密目标行，不整表解密
    "account_row_at": _accounts,
    "read_account_at": _accounts,
    # 只读装载（MF-60）：不经 init_db，不建库/不建表/不迁移，供"只读"维护路径用
    "load_accounts_readonly": _accounts,
    # 只读打开（MF-60）：与 init_db 分道，不切 WAL、不建 -shm/-wal（连接层定义点）
    "open_readonly": _connection,
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
    # 会话缓存域迁出名（唯一定义点在 yiban/adapters/persistence/session_cache.py）
    "_session_cache_now": _session_cache,
    "_session_cache_key": _session_cache,
    "_session_cache_ttl_hours": _session_cache,
    "get_session_cache": _session_cache,
    "set_session_cache": _session_cache,
    "clear_session_cache": _session_cache,
    # 事件域按表归属并入的暂停冷却（查 audit_logs 表，唯一定义点在 yiban/adapters/persistence/events.py）
    "last_pause_at": _events,
    "pause_count_since": _events,
    # 用户自选时间片域迁出名（唯一定义点在 yiban/adapters/persistence/time_prefs.py）
    "last_time_pref_set_at": _time_prefs,
    "time_pref_set_count_since": _time_prefs,
    "get_time_prefs": _time_prefs,
    "get_time_pref": _time_prefs,
    "set_time_pref": _time_prefs,
    "clear_time_pref": _time_prefs,
    "time_pref_stats": _time_prefs,
    # 时钟守卫告警与 app_meta 单键读写（唯一定义点在 yiban/adapters/persistence/clock_meta.py）
    "get_meta": _clock_meta,
    "set_meta": _clock_meta,
    # JSON → SQLite 自动导入（唯一定义点在 yiban/adapters/persistence/migrations.py；门面内 init_db 晚解析调用）
    "_maybe_migrate": _migrations,
    "_rename_backup": _migrations,
    # 追踪盐与加盐哈希域迁出名（唯一定义点在 yiban/adapters/persistence/tracking.py）
    "_write_track_salt_to_env_file": _tracking,
    "_track_salt": _tracking,
    "hash_ip": _tracking,
    "hash_phone": _tracking,
    # 追踪盐的进程内缓存是可变状态：`db._TRACK_SALT_CACHE = None` 必须清到定义点那份
    "_TRACK_SALT_CACHE": _tracking,
}
# delattr 撤下的名字（见 _StateForwardingModule.__delattr__）：名字重新可读即移出
_FORWARDED_STATE_HIDDEN = set()


def __getattr__(name):
    """PEP 562：转发名（连接三态、审计/迁移域可变状态、各域迁出名）读取回落到定义点。"""
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
        `mock.patch("yiban.adapters.persistence.db._conn", …)` 对不在 `__dict__` 里的名字走
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


def _same_store_path(a, b):
    """两个库路径是否指向同一个文件（realpath + normcase，Windows 大小写不敏感）。"""
    return (os.path.normcase(os.path.realpath(os.path.abspath(str(a))))
            == os.path.normcase(os.path.realpath(os.path.abspath(str(b)))))


def init_db(db_file=None, migrate_from=None, env_file=None, cleanup=True, migrate=True,
            create=True):
    """初始化连接与表结构；可选自动迁移（migrate_from 提供 json 文件基路径，如 /path/accounts.json）。

    env_file：.env 路径（加密密钥来源），须与调用方一致（web 用 --env 参数时必传），
    None 时按 YIBAN_ENV_FILE → `env_io.DEFAULT_ENV_FILE`（`data/.env`，相对检出根）
    回落（CLI/取证类调用方应显式传入，勿让密钥来源靠默认值兜底）。
    cleanup：默认 True 执行启动清理（审计/事件旧数据、过期软删用户等）；
    校验类工具应传 False，避免只读校验改变数据。
    migrate：默认 True 执行迁移；只读校验类工具应传 False——迁移会回填审计链
    （v3）等，使"被校验对象在校验过程中被改动"。
    create=False：**只读初始化**——只打开既有库（`connection.open_readonly(...,
    immutable=False)`，`mode=ro` 见得到并发写者与锁），**不建库、不建表、不迁移、不切
    WAL、不跑清理**，供取证类只读调用方（如 `scripts/audit_verify.py`）复用
    `db.get_conn()` 体系而不改动目标库。库文件不存在时抛 FileNotFoundError（绝不
    `sqlite3.connect` 出空库——空库会让只读校验在空集上误报"通过"）。默认 True =
    既有行为逐字不变。
    """
    # 库路径 / .env 路径**无条件刷新**（即使连接已存在——它们是"最近一次 init_db 的
    # 来源"），经 connection 的显式 API 写入
    _connection.set_env_file(env_file)
    # 库路径解析统一走 env_io.resolve_path（进程环境 → .env → 默认值），与
    # YIBAN_STATE_DIR / YIBAN_LOG_FILE / 审计锚点等路径键同口径（E1/E7 收口）。
    # 过去这里只认 os.environ：`YIBAN_DB_FILE` 只写进 .env 而未 export 时（agent/CI
    # 直调 CLI 的常见形态），引擎静默回落到默认 ./yiban.db——0 账号 → "未配置任何
    # 账号"、exit 1，而 CLI 路径显示与 `db --status` 认的却是 .env 里的库，两侧
    # 看到的根本不是同一个库（2026-09-21 测试机 47 E2E 实测）。
    db_path = db_file or env_io.resolve_path("YIBAN_DB_FILE", DB_DEFAULT)
    _connection.set_db_file(db_path)
    # 已有连接时**不许静默换库**（工单 mgq5）：旧行为只刷新 `_db_file` 元数据、返回旧
    # 连接，于是 `init_db(别的库)` 之后的写入会落到旧库（测试夹具因此静默失去隔离，
    # 表现为"单跑绿、全量红"的假红）。切库必须由调用方显式收尾（关连接 →
    # `connection.reset_conn()`），这里 fail-closed 并点名两侧路径。
    # `:memory:` 连接按连接独立，跳过比较（PRAGMA 报的路径不可比）。
    _existing = _connection.current()
    if _existing is not None:
        _actual = _connection.current_db_file()
        if (_actual and ":memory:" not in _actual
                and ":memory:" not in str(db_path)
                and not _same_store_path(_actual, db_path)):
            raise RuntimeError(
                "init_db 拒绝在已有连接上切换库：当前连接指向 %r，本次声明 %r。"
                "切库前请关闭旧连接并调用 connection.reset_conn()——"
                "否则写入会落到旧库（工单 mgq5）" % (_actual, db_path))
    if not create:
        # 只读初始化：已初始化过就直接复用（只读入口只在进程首次打开库时有意义，
        # 不能把既有连接静默换成另一条只读连接）。
        existing = _connection.current()
        if existing is not None:
            return existing
        conn = _connection.open_readonly(db_path, immutable=False)
        if conn is None:
            raise FileNotFoundError(f"数据库文件不存在: {db_path}（只读初始化不建库）")
        _connection.set_conn(conn)
        # 与读写分支同一条自愈边界（工单 ba-p02-03 的同形状调用点）：登记之后的
        # PRAGMA 失败也必须关连接、清单例，否则重试会复用一条半初始化连接。
        try:
            conn.execute("PRAGMA busy_timeout=15000")
        except Exception:
            with contextlib.suppress(Exception):
                conn.close()
            _connection.reset_conn()
            raise
        return conn
    conn = _connection.current()
    if conn is not None:
        return conn
    with _conn_lock:
        conn = _connection.current()
        if conn is not None:
            return conn
        conn = sqlite3.connect(db_path, check_same_thread=False)
        # 先登记再配置：建表/迁移函数内部会经 get_conn() 取"当前连接"，故连接必须先
        # 入册，其后才逐条 PRAGMA/DDL。
        _connection.set_conn(conn)
        # 登记之后的**全部**初始化动作必须在同一个自愈边界内（工单 ba-p02-03）：
        # row_factory / PRAGMA / 建表任一失败（磁盘满、并发启动锁超时、schema 损坏）
        # 都必须关连接并重置单例。否则这条半初始化连接会留在原处，此后任何重试都在
        # 上面的 `conn is not None: return conn` 直接复用它——没有表，整个进程永久
        # "no such table"（web 全站 500、signin 整轮静默不签）。
        # 通用幂等迁移框架（Phase 0）：按 PRAGMA user_version 顺序执行；
        # 核心迁移失败会关闭连接并抛出，阻断启动；可选迁移失败继续后续迁移但不提升版本。
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            # 5000ms 在夜间批量签到/整表重建等长事务窗口内不够，业务写
            # 路径无重试，超限即 500——提到 15s 并保留 audit() 自身的 3 次重试
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA foreign_keys=OFF")
            _create_tables(conn)
            if migrate:
                _run_migrations(conn)
                # 自动迁移（幂等：库存在但空表 + JSON 存在才导入）
                # 定义点在 yiban/adapters/persistence/migrations.py：按属性取，`db._maybe_migrate = 替身`
                # 一类打桩必须被本调用点看见（晚解析）
                if migrate_from:
                    _migrations._maybe_migrate(conn, migrate_from)
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
    return env_io.env_path(default=None)


def require_existing_env_file(cli_value=None):
    """resolve_env_file + 显式来源存在性校验；不存在则抛 ValueError。

    为什么必须校验：--env 一旦给出，db 层就认为"密钥来源已确定"，防游离落盘的
    _assert_key_source_certain 对它不再生效。路径打错（少写一层目录、部署迁移后
    旧路径）时，工具会在该位置新建 .env 并生成一把**新**审计密钥，把这次留痕用
    第三把钥匙签名——真实哈希链从这条起判破，正是这道校验要治的病症的新入口。
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
    的 bug）即抛 "within a transaction" 并连锁锁死全部写路径；audit() 原有的自保防御
    只覆盖自己。统一走本函数：遗留半事务按安全默认丢弃并 ERROR 留痕定位根因。
    """
    if conn.in_transaction:
        logger.error(
            "检测到遗留未提交事务，已回滚解除写锁（未知半事务按安全默认丢弃）——"
            "请检查此前调用路径是否有写操作未 commit/rollback"
        )
        conn.rollback()
    conn.execute('BEGIN IMMEDIATE')


# 时钟跳变保护参数：
# 允许的"时间前进"上限。软删保留期 7 天——系统时间被拨快 8 天，刚软删 1 秒的
# 账号会在下次清理时被立即物理清除、7 天反悔窗口归零。取 72h：每日正常运行的
# 服务不会超过；停机 >3 天后的首轮清理会被跳过一轮并告警，参照点随即推进到当前
# 时间，下一次正常调用即恢复清理（不需要人工重置）。
_CLOCK_ALLOW_FWD_HOURS = 72
# 允许的"时间回拨"上限（秒）：正常 NTP 校正是秒级，回拨超过 1h 视为异常
_CLOCK_ALLOW_BACK_SECONDS = 3600


def _clock_jump_guard(conn, key):
    """以 app_meta 记录的最近一次 seen-now 为参照，检测系统时钟异常跳变。

    返回 (ok, note)：ok=False 时调用方应跳过本次清理（防"拨快后刚软删的数据被立即
    物理清除"）；note 为告警文本或空串。告警出口只有 logger.error——守卫不承担
    "冻结状态必须保持可见"的职责，故不留告警状态、不发邮件。

    **两条路径都把参照点推进到当前时间**：跳变只跳一轮，靠下一次正常调用恢复。
    留一个越界就不推进的参照点等于永久冻结（五处清理会每轮都再触发，直到有人手工
    拨回），而守卫存在的理由是"防误删"不是"停摆"——参照点一旦推进，后续 cutoff
    按真实当前时间算，误删窗口并未因此打开。

    越界路径**在守卫内提交**：把推进留给调用方就等于让它被一起回滚，冻结会重新变成
    永久。故调用方在 ok=False 分支上是否 rollback 都不影响这条推进——`users` 的两处
    钩子就是直接 `return`（不 rollback），`cleanup` / `events` 才 rollback 解除写锁。
    放行路径刻意不提交：那条 INSERT upsert 在 WAL 下即持 RESERVED 写锁，兼作调用方
    读-删-连带清理的事务边界，由调用方 commit。
    """
    now = clock.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    if row is None:
        conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)", (key, ts))
        return True, ""
    try:
        last = datetime.datetime.strptime(row["value"], "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007 解析 app_meta 库内业务本地 naive 时间串
    except ValueError:
        conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)", (key, ts))
        return True, ""
    fwd = (now - last).total_seconds()
    back = (last - now).total_seconds()
    if fwd > _CLOCK_ALLOW_FWD_HOURS * 3600 or back > _CLOCK_ALLOW_BACK_SECONDS:
        note = (
            f"系统时间异常跳变（上次记录 {row['value']}，当前 {ts}，"
            f"前进 {fwd / 3600:.1f}h / 回拨 {back / 3600:.1f}h），"
            "已跳过本次物理清理以防误删；请核实系统时间与 NTP 同步状态"
        )
        logger.error("%s", note)
        conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)", (key, ts))
        conn.commit()
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
    # session_cache（v8）与 verify_jobs（v15）是**可选迁移**产物：迁移失败时
    # user_version 不提升，链尾产物核对只看 `target <= user_version` 的档，表可以
    # 合法地不存在而进程照常服务。本函数在强制路径上（删号/整表保存），无条件 DELETE
    # 会抛 OperationalError 并回滚整个删号事务。故只对可选表做存在性守卫。
    # 核心表（time_prefs / sign_events / sign_claims / sign_tasks）不守卫：
    # 缺表是 schema 损坏，必须响亮失败，不能被守卫掩盖。
    if _table_columns(conn, "session_cache"):
        conn.executemany("DELETE FROM session_cache WHERE phone=?", rows)
    conn.executemany("DELETE FROM sign_events WHERE phone=?", rows)
    if _table_columns(conn, "verify_jobs"):
        conn.executemany("DELETE FROM verify_jobs WHERE phone=?", rows)
    conn.executemany("DELETE FROM sign_claims WHERE phone=?", rows)
    conn.executemany("DELETE FROM sign_tasks WHERE phone=?", rows)
    # run_events 只随**可选**迁移 v21 建出：v21 失败时版本不提升、链尾产物核对
    # 只看 `target <= user_version` 的档（v21 不被核对），表可以合法地不存在而进程
    # 照常服务（`run_events.report` / `purge` 也都容忍缺表）。本函数却落在强制路径上
    # （`purge_account` / `delete_accounts_by_owner` / `replace_accounts`），无条件
    # DELETE 会让缺表在删号/整表保存时抛 OperationalError、把整个事务连同删号一起
    # 回滚。故只在表在时连带清理，与另两条路径同口径。
    if _table_columns(conn, "run_events"):
        conn.executemany("DELETE FROM run_events WHERE phone=?", rows)


def _clear_session_cache_by_phones(conn, phones):
    """按手机号批量清除会话缓存（**部分清理**路径专用，须在调用方事务内）。

    与 `_cascade_phone_owned` 的区别是刻意的：改绑手机号（旧号的 cookie/csrf
    主键与 AAD 均按旧号，不复用）与用户注销（账号软删但不立即物理清除，
    time_prefs 保留至物理清除、sign_events 保留供恢复后查看历史）这两条路径
    只停用凭据缓存，不动其余历史数据。新的"账号物理删除"路径请用前者。
    """
    if not phones:
        return
    if not _table_columns(conn, "session_cache"):
        return  # v8 可选迁移未落地：无缓存可清，不得把改绑/注销打成 500
    conn.executemany("DELETE FROM session_cache WHERE phone=?", [(p,) for p in phones])


def _purge_file_facts(phones):
    """物理删除账号后清理文件面的手机号事实（R3-006）。

    覆盖所有物理删除路径：单条 purge、批量 purge、按 owner 清空、整表替换、
    管理员硬清除用户、软删到期自动清除。必须在 DB 事务**提交之后**调用；
    文件清理失败只告警，不回滚已完成的删除。

    延迟导入 `state_io`：它在顶层 import `yiban.engine`，persistence 顶层导入会成环。
    """
    if not phones:
        return
    try:
        from yiban.adapters.statefiles import cred_state, state_io
    except Exception as e:  # 导入失败不得影响已提交的删除
        logger.warning("清理已删账号的文件面失败（状态模块导入异常）: %s", e)
        return
    for phone in dict.fromkeys(str(p or "").strip() for p in phones):
        if not phone:
            continue
        try:
            state_io.purge_phone_from_sign_states(phone)
            cred_state.clear(phone)
        except Exception as e:  # 单个文件失败不阻断其余账号
            logger.warning("清理已删账号的文件面手机号失败: %s", type(e).__name__)
