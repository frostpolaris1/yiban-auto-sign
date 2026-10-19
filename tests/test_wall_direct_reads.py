# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""拆墙门（批 3-a ＋ 批 3-b）：路由模块不得经 `web.app` 转发墙读 A 类名字。

**判据**
① 扫 `web/routes/*.py` 的 `m.<名字>` 读取：A 类名字必须 0 处。
② A 名一律从真源取：每个 A 名的读取点必须落在真源模块上（属性访问的基底或裸名导入）。
③ B 类名字不受本门约束：B 名仍在墙上，属批 3-c。本门对 B 名的 `m.<名字>` 不做断言。
④ 名单冻结：A ∩ B = ∅；墙上剩余名字必须 ⊆ B 名单；扫描面 < 11 文件即红；0 处读取即红。
⑤ 批 3-b 已迁名（`BATCH3B_NAMES`）在 `web/app.py` 里不得再有**真定义**
   （`def` / `class` / 赋值）。真定义与真源两处同时存在即为双源。

**来源与日期**
批 3-a 名单取自 `origin/develop` 的 `5823c48e`（2026-10-19）：A 名＝`web/app.py` 以
import 形态转发的、且被路由经 `m.*` 读取的名字；B 名＝同批读取里 `web/app.py` 自有
定义的名字。`__file__` 是 app.py 自身属性，两名单都不收。
批 3-b 名单取自 `origin/develop` 的 `e5aceb07`（2026-10-19）：从 B 名里挑出**纯常量**
与**纯助手函数**两类，搬进 `web/services/*`，故并入 A 侧判据。

**边界（B 名单为何还剩 58 名）**
本门只管读取形态，不管 `_appmod()` 本身。批 3-b 只迁两类，其余按域缓迁，逐类理由如下：
  · (c) 进程内状态对象（`_announcement_cache` / `_changelog_cache`）：限速与启动缓存存
    进程内存是明文设计不变量（web 固定 `-w 1`），搬动必须连打桩面一起设计。
  · (d) env 句柄（`STATE_DIR` / `ENV_FILE` / `DB_FILE` / `LOG_FILE`）：`web/app.py` 是
    组合根，测试靠改 `web.app.STATE_DIR` 换环境，这是既有接缝。
  · (e) 凭据 / 审核 / 签到进程族（`verify_admin` / `_current_role` / `_pw_gate_tier` /
    `run_verify_with_gate` / `VERIFY_*_MSG` / `_account_verify_enabled` /
    `_issue_admin_sid` / `_builtin_admin_*` / `_is_builtin_admin_session` /
    `verify_async_enabled`）：凭据与执行核近邻，须独立审查。
  · (f) app 状态注入包装（其余 42 名）：每个都在调用时刻现取 `web/app.py` 的模块级名字
    （`.env` 路径与读取器、`_sign_window`、`_atomic_write`、`send_notification`、
    `_tail_lines` 等）后注入服务层。搬动它们要先重设那层注入接缝，属 3-c。
    `logger` 与 `WEB_VERSION` 归此：前者是进程级日志通道，后者是启动时刻派生值。
另有 11 个名字留在墙上但不属 A：10 个是 `web/app.py` 自建别名（`X = mod.attr`，
不是 import 转发）＋ `__file__`（app.py 自身属性）。它们登记在 `ALIAS_NOT_MIGRATED`。
"""
import ast
import io
import os
import unittest

#: 路由目录（本门只扫这里；`__init__.py` 是装配件，不算路由体）。
_ROUTES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "web", "routes")
#: 扫描面下限：带 `m.*` 读取的路由文件实测 14 个，低于 11 即扫描器失效。
_MIN_FILES = 11
#: `web/app.py` 路径（判据⑤：已迁名不得再有真定义）。
_APP_PY = os.path.join(os.path.dirname(_ROUTES_DIR), "app.py")

#: 批 3-a 的 A 类名字：`web/app.py` 以 import 形态转发的转发名（该批迁走）。
A_NAMES_BATCH3A = frozenset({
"ACCOUNT_STATUS_ACTIVE", "ACCOUNT_STATUS_PENDING", "ACCOUNT_STATUS_REJECTED",
    "ADMIN_SID_ENV_KEY", "ANNOUNCEMENT_DRAFT_META_FMT", "ANNOUNCEMENT_DRAFT_META_SEP",
    "APP_VERSION", "LOGIN_LOCK_SECONDS", "LOG_LEVELS", "LOG_LEVEL_DEFAULT", "PW_GATE_FULL",
    "SCRYPT_METHOD", "VerifyGateBusy", "VerifyQuotaExceeded", "_IP_STORE_MAX_AGE",
    "_admin_password_policy_error", "_as_signin_account", "_audit_actor",
    "_batch_wait_timeout", "_bump_login_failure", "_bump_window_count",
    "_capacity_account_count", "_change_mail", "_client_ip", "_constant_time_dummy",
    "_cred_paused_phones", "_delete_grace_remaining", "_duplicate_phone_error", "_env_flag",
    "_env_write_lock", "_executor_activity", "_executor_health_cards", "_executor_row_payload",
    "_file_lock", "_filter_log_level", "_in_sign_window", "_ip_store_trim",
    "_is_http_proxy_url", "_is_valid_date_str", "_last_executors", "_mask_email",
    "_mask_log_phones", "_mask_phone", "_mask_url_userinfo", "_measure_cooldown_remaining",
    "_new_admin_sid", "_next_executor_slot", "_nl_safe", "_owner_display_of",
    "_owner_has_other_live", "_parse_announcement_meta", "_password_policy_error",
    "_pick_measure_account", "_rate_lock", "_read_measure_state", "_reclaim_stale_verify_jobs",
    "_record_verify_failure", "_review_reject_mail", "_settings_label", "_settings_value_text",
    "_stale_idx_guard", "_start_verify_job", "_terminate_signin_proc", "_validated_name",
    "_validated_proxy_value", "_verify_attempt_allowed", "_verify_fail_cooldown_remaining",
    "_verify_queue_full", "_wait_signin_proc", "account_crypto", "check_connectivity",
    "child_env", "clear_fuse_on_cred_change", "clock", "db", "env_io", "find_account_index",
    "fold_phone_code", "load_accounts", "load_accounts_raw", "load_env_int", "load_users",
    "mail_config", "mail_layout", "mailer", "mask_account", "notify", "read_env",
    "run_after_file_lock", "signin", "validate_account", "yb_egress", "yiban_status",
})

#: 批 3-b 已迁名：B 类里**纯常量**与**纯助手函数**两类，搬进 `web/services/*`。
#: 判据⑤钉住它们在 `web/app.py` 不得再有真定义；① ② 钉住路由从新家直读。
BATCH3B_NAMES = frozenset({
    # ---- (a) 纯常量 → web/services/limits.py（窗口 / 上限 / 冷却 / 输入长度）----
    "BATCH_OP_LIMIT", "DEFAULT_ACCOUNT_GAP_MAX", "DEFAULT_MAX_ACCOUNTS",
    "DEFAULT_MAX_USERS", "DEFAULT_START_DELAY_MAX", "DELETE_COOLDOWN_SEC",
    "DELETE_MAX_REQUESTS_PER_IP", "DETAIL_MAX", "DETAIL_WINDOW", "DUPCHECK_MAX",
    "DUPCHECK_WINDOW", "EMAIL_RE", "EMAIL_USER_MAX", "EXPORT_MAX", "EXPORT_WINDOW",
    "LOGIN_FAIL_NOTIFY", "LOGIN_MAX_FAILS", "LOGIN_SPRAY_USERS", "MAIL_ADMIN_TO_MAX",
    "MAIL_SMTPS_MAX", "MEASURE_COOLDOWN_SEC", "NOTIFY_SENDKEY_MAX_LEN",
    "NOTIFY_SENDKEY_MIN_LEN", "NOTIFY_URL_MAX_LEN", "PAUSE_COOLDOWN_FREE",
    "PAUSE_COOLDOWN_MAX", "PAUSE_COOLDOWN_SEC", "PAUSE_COOLDOWN_WINDOW", "REGISTER_MAX",
    "REGISTER_WINDOW", "RESTORE_FAIL_MAX", "RESTORE_FAIL_WINDOW", "SIGN_MIN_INTERVAL",
    "TIME_PREF_COOLDOWN_FREE", "TIME_PREF_COOLDOWN_MAX", "TIME_PREF_COOLDOWN_SEC",
    "TIME_PREF_COOLDOWN_WINDOW", "_NOTIFY_QUOTA_HIDDEN_KEYS",
    # ---- (a) 键名常量 → web/services/env_io.py（设置档位 / 公告键）----
    "GATED_KEYS", "GLOBAL_PAUSE_KEY", "MASTER_ONLY_KEYS", "ANNOUNCEMENT_KEY",
    "ANNOUNCEMENT_DRAFT_KEY", "ANNOUNCEMENT_DRAFT_META_KEY",
    "ANNOUNCEMENT_PUBLISHED_META_KEY",
    # ---- (b) 纯助手函数 → web/services/http_request.py ----
    "_json_body", "_request_is_same_origin",
})

#: 本门约束的 A 名单＝批 3-a 的转发名 ∪ 批 3-b 已迁名。
A_NAMES = A_NAMES_BATCH3A | BATCH3B_NAMES

#: B 类名字：`web/app.py` 自有定义、且被路由经 `m.*` 读取的名字（批 3-b 缓迁，见模块 docstring 边界段）。
B_NAMES = frozenset({
    "DB_FILE", "ENV_FILE", "STATE_DIR", "VERIFY_BUSY_MSG", "VERIFY_FAIL_COOLDOWN_MSG",
    "WEB_VERSION", "_account_verify_enabled", "_accounts_at_capacity", "_announcement_cache",
    "_builtin_admin_email", "_builtin_admin_loginable", "_capacity_estimate",
    "_changelog_cache", "_current_role", "_day_off_reason", "_doc_page", "_estimate_slot",
    "_executor_rows", "_executors_window", "_in_run_period", "_is_builtin_admin_session",
    "_issue_admin_sid", "_log_lines_for", "_log_manual_sign_exit",
    "_measure_state_path", "_most_recent_log_date", "_mutate_executor_rows",
    "_notify_capacity_once", "_pw_gate_tier", "_read_doc_html", "_registration_paused",
    "_save_fallback_egress", "_save_row_egress",
    "_save_slot_egress", "_settings_effective_values", "_sign_window", "_slot_to_label",
    "_users_at_capacity", "_write_measure_state", "accounts_terminal_summary", "edge_config",
    "email_domain_error", "icp_info", "load_sign_state", "log_path_for", "logger",
    "parse_sign_log", "police_info", "police_link", "run_verify_with_gate",
    "send_notification", "sign_status", "sign_window_bounds", "site_description", "site_image",
    "verify_admin", "verify_async_enabled", "write_env_batch",
})

#: 留在墙上、但不属 A 也不属 B 的名字（本批不迁，见模块 docstring 的边界段）。
ALIAS_NOT_MIGRATED = frozenset({
    "DELETED_RETENTION_DAYS", "EXECUTOR_INDEX_MAX", "STATUS_ALREADY", "STATUS_ICON",
    "STATUS_NO_TASK", "STATUS_PENDING", "STATUS_SUCCESS", "STATUS_USER_CANCELLED",
    "VERIFY_JOB_TERMINAL", "_has_line_break", "__file__",
})

#: A 名 -> (真源模块, 真源原名)；原名 != 名字时说明该名是改名再导出。
TRUE_SOURCE = {
    "ACCOUNT_STATUS_ACTIVE": ("web.services.accounts_data", "ACCOUNT_STATUS_ACTIVE"),
    "ACCOUNT_STATUS_PENDING": ("web.services.accounts_data", "ACCOUNT_STATUS_PENDING"),
    "ACCOUNT_STATUS_REJECTED": ("web.services.accounts_data", "ACCOUNT_STATUS_REJECTED"),
    "ADMIN_SID_ENV_KEY": ("web.security", "ADMIN_SID_ENV_KEY"),
    "ANNOUNCEMENT_DRAFT_META_FMT": ("web.services.env_io", "ANNOUNCEMENT_DRAFT_META_FMT"),
    "ANNOUNCEMENT_DRAFT_META_SEP": ("web.services.env_io", "ANNOUNCEMENT_DRAFT_META_SEP"),
    "APP_VERSION": ("yiban", "__version__"),
    "LOGIN_LOCK_SECONDS": ("web.security", "LOGIN_LOCK_SECONDS"),
    "LOG_LEVELS": ("web.services.logs", "LOG_LEVELS"),
    "LOG_LEVEL_DEFAULT": ("web.services.logs", "LOG_LEVEL_DEFAULT"),
    "PW_GATE_FULL": ("web.security", "PW_GATE_FULL"),
    "SCRYPT_METHOD": ("web.security", "SCRYPT_METHOD"),
    "VerifyGateBusy": ("web.services.verify_queue", "VerifyGateBusy"),
    "VerifyQuotaExceeded": ("web.services.verify_queue", "VerifyQuotaExceeded"),
    "_IP_STORE_MAX_AGE": ("web.security", "_IP_STORE_MAX_AGE"),
    "_admin_password_policy_error": ("web.services.accounts_data", "_admin_password_policy_error"),
    "_as_signin_account": ("web.services.accounts_data", "_as_signin_account"),
    "_audit_actor": ("web.services.notify_mail", "_audit_actor"),
    "_batch_wait_timeout": ("web.services.manual_sign", "_batch_wait_timeout"),
    "_bump_login_failure": ("web.security", "_bump_login_failure"),
    "_bump_window_count": ("web.security", "_bump_window_count"),
    "_capacity_account_count": ("web.services.capacity", "_capacity_account_count"),
    "_change_mail": ("web.services.notify_mail", "_change_mail"),
    "_client_ip": ("web.security", "_client_ip"),
    "_constant_time_dummy": ("web.security", "_constant_time_dummy"),
    "_cred_paused_phones": ("web.services.logs", "_cred_paused_phones"),
    "_delete_grace_remaining": ("web.services.accounts_data", "_delete_grace_remaining"),
    "_duplicate_phone_error": ("web.services.accounts_data", "_duplicate_phone_error"),
    "_env_flag": ("web.services.signstatus", "_env_flag"),
    "_env_write_lock": ("web.services.env_io", "_env_write_lock"),
    "_executor_activity": ("web.services.executor_env", "_executor_activity"),
    "_executor_health_cards": ("web.services.executor_env", "_executor_health_cards"),
    "_executor_row_payload": ("web.services.executor_env", "_executor_row_payload"),
    "_file_lock": ("web.services.locks", "_file_lock"),
    "_filter_log_level": ("web.services.logs", "_filter_log_level"),
    "_in_sign_window": ("web.services.signstatus", "_in_sign_window"),
    "_ip_store_trim": ("web.security", "_ip_store_trim"),
    "_is_http_proxy_url": ("web.services.env_io", "_is_http_proxy_url"),
    "_is_valid_date_str": ("web.services.logs", "_is_valid_date_str"),
    "_last_executors": ("web.services.executor_env", "_last_executors"),
    "_mask_email": ("web.services.accounts_data", "_mask_email"),
    "_mask_log_phones": ("web.services.logs", "_mask_log_phones"),
    "_mask_phone": ("yiban.core.masking", "mask_phone"),
    "_mask_url_userinfo": ("yiban.core.masking", "mask_url_userinfo"),
    "_measure_cooldown_remaining": ("web.services.measure", "_measure_cooldown_remaining"),
    "_new_admin_sid": ("web.security", "_new_admin_sid"),
    "_next_executor_slot": ("web.services.executor_env", "_next_executor_slot"),
    "_nl_safe": ("web.services.notify_mail", "_nl_safe"),
    "_owner_display_of": ("web.services.accounts_data", "_owner_display_of"),
    "_owner_has_other_live": ("web.services.accounts_data", "_owner_has_other_live"),
    "_parse_announcement_meta": ("web.services.env_io", "_parse_announcement_meta"),
    "_password_policy_error": ("web.services.accounts_data", "_password_policy_error"),
    "_pick_measure_account": ("web.services.measure", "_pick_measure_account"),
    "_rate_lock": ("web.services.locks", "_rate_lock"),
    "_read_measure_state": ("web.services.measure", "_read_measure_state"),
    "_reclaim_stale_verify_jobs": ("web.services.verify_queue", "_reclaim_stale_verify_jobs"),
    "_record_verify_failure": ("web.security", "_record_verify_failure"),
    "_review_reject_mail": ("web.services.notify_mail", "_review_reject_mail"),
    "_settings_label": ("web.services.env_io", "_settings_label"),
    "_settings_value_text": ("web.services.env_io", "_settings_value_text"),
    "_stale_idx_guard": ("web.services.accounts_data", "_stale_idx_guard"),
    "_start_verify_job": ("web.services.verify_queue", "_start_verify_job"),
    "_terminate_signin_proc": ("web.services.manual_sign", "_terminate_signin_proc"),
    "_validated_name": ("web.services.executor_env", "_validated_name"),
    "_validated_proxy_value": ("web.services.executor_env", "_validated_proxy_value"),
    "_verify_attempt_allowed": ("web.security", "_verify_attempt_allowed"),
    "_verify_fail_cooldown_remaining": ("web.security", "_verify_fail_cooldown_remaining"),
    "_verify_queue_full": ("web.services.verify_queue", "_verify_queue_full"),
    "_wait_signin_proc": ("web.services.manual_sign", "_wait_signin_proc"),
    "account_crypto": ("yiban.adapters.config", "account_crypto"),
    "check_connectivity": ("web.services.signstatus", "check_connectivity"),
    "child_env": ("child_env", None),
    "clear_fuse_on_cred_change": ("web.services.logs", "clear_fuse_on_cred_change"),
    "clock": ("yiban.core", "clock"),
    "db": ("yiban.adapters.persistence", "db"),
    "env_io": ("yiban.adapters.config", "env_io"),
    "find_account_index": ("web.services.accounts_data", "find_account_index"),
    "fold_phone_code": ("web.services.accounts_data", "fold_phone_code"),
    "load_accounts": ("web.services.accounts_data", "load_accounts"),
    "load_accounts_raw": ("web.services.accounts_data", "load_accounts_raw"),
    "load_env_int": ("web.services.env_io", "load_env_int"),
    "load_users": ("web.services.accounts_data", "load_users"),
    "mail_config": ("yiban.adapters.notifier.mail", "config"),
    "mail_layout": ("yiban.adapters.notifier.mail", "layout"),
    "mailer": ("yiban.adapters.notifier", "mail"),
    "mask_account": ("web.services.accounts_data", "mask_account"),
    "notify": ("yiban.adapters.notifier", "notify"),
    "read_env": ("web.services.env_io", "read_env"),
    "run_after_file_lock": ("web.services.locks", "run_after_file_lock"),
    "signin": ("signin", None),
    "validate_account": ("web.services.accounts_data", "validate_account"),
    "yb_egress": ("yiban", "egress"),
    "yiban_status": ("yiban.core", "status"),
}

#: 批 3-b 已迁名的真源：新家模块 → 原名（不改名，故逐条同形）。
BATCH3B_TRUE_SOURCE = {
    # (a) 常量新家：web/services/limits.py
    "BATCH_OP_LIMIT": ("web.services.limits", "BATCH_OP_LIMIT"),
    "DEFAULT_ACCOUNT_GAP_MAX": ("web.services.limits", "DEFAULT_ACCOUNT_GAP_MAX"),
    "DEFAULT_MAX_ACCOUNTS": ("web.services.limits", "DEFAULT_MAX_ACCOUNTS"),
    "DEFAULT_MAX_USERS": ("web.services.limits", "DEFAULT_MAX_USERS"),
    "DEFAULT_START_DELAY_MAX": ("web.services.limits", "DEFAULT_START_DELAY_MAX"),
    "DELETE_COOLDOWN_SEC": ("web.services.limits", "DELETE_COOLDOWN_SEC"),
    "DELETE_MAX_REQUESTS_PER_IP": ("web.services.limits", "DELETE_MAX_REQUESTS_PER_IP"),
    "DETAIL_MAX": ("web.services.limits", "DETAIL_MAX"),
    "DETAIL_WINDOW": ("web.services.limits", "DETAIL_WINDOW"),
    "DUPCHECK_MAX": ("web.services.limits", "DUPCHECK_MAX"),
    "DUPCHECK_WINDOW": ("web.services.limits", "DUPCHECK_WINDOW"),
    "EMAIL_RE": ("web.services.limits", "EMAIL_RE"),
    "EMAIL_USER_MAX": ("web.services.limits", "EMAIL_USER_MAX"),
    "EXPORT_MAX": ("web.services.limits", "EXPORT_MAX"),
    "EXPORT_WINDOW": ("web.services.limits", "EXPORT_WINDOW"),
    "LOGIN_FAIL_NOTIFY": ("web.services.limits", "LOGIN_FAIL_NOTIFY"),
    "LOGIN_MAX_FAILS": ("web.services.limits", "LOGIN_MAX_FAILS"),
    "LOGIN_SPRAY_USERS": ("web.services.limits", "LOGIN_SPRAY_USERS"),
    "MAIL_ADMIN_TO_MAX": ("web.services.limits", "MAIL_ADMIN_TO_MAX"),
    "MAIL_SMTPS_MAX": ("web.services.limits", "MAIL_SMTPS_MAX"),
    "MEASURE_COOLDOWN_SEC": ("web.services.limits", "MEASURE_COOLDOWN_SEC"),
    "NOTIFY_SENDKEY_MAX_LEN": ("web.services.limits", "NOTIFY_SENDKEY_MAX_LEN"),
    "NOTIFY_SENDKEY_MIN_LEN": ("web.services.limits", "NOTIFY_SENDKEY_MIN_LEN"),
    "NOTIFY_URL_MAX_LEN": ("web.services.limits", "NOTIFY_URL_MAX_LEN"),
    "PAUSE_COOLDOWN_FREE": ("web.services.limits", "PAUSE_COOLDOWN_FREE"),
    "PAUSE_COOLDOWN_MAX": ("web.services.limits", "PAUSE_COOLDOWN_MAX"),
    "PAUSE_COOLDOWN_SEC": ("web.services.limits", "PAUSE_COOLDOWN_SEC"),
    "PAUSE_COOLDOWN_WINDOW": ("web.services.limits", "PAUSE_COOLDOWN_WINDOW"),
    "REGISTER_MAX": ("web.services.limits", "REGISTER_MAX"),
    "REGISTER_WINDOW": ("web.services.limits", "REGISTER_WINDOW"),
    "RESTORE_FAIL_MAX": ("web.services.limits", "RESTORE_FAIL_MAX"),
    "RESTORE_FAIL_WINDOW": ("web.services.limits", "RESTORE_FAIL_WINDOW"),
    "SIGN_MIN_INTERVAL": ("web.services.limits", "SIGN_MIN_INTERVAL"),
    "TIME_PREF_COOLDOWN_FREE": ("web.services.limits", "TIME_PREF_COOLDOWN_FREE"),
    "TIME_PREF_COOLDOWN_MAX": ("web.services.limits", "TIME_PREF_COOLDOWN_MAX"),
    "TIME_PREF_COOLDOWN_SEC": ("web.services.limits", "TIME_PREF_COOLDOWN_SEC"),
    "TIME_PREF_COOLDOWN_WINDOW": ("web.services.limits", "TIME_PREF_COOLDOWN_WINDOW"),
    "_NOTIFY_QUOTA_HIDDEN_KEYS": ("web.services.limits", "_NOTIFY_QUOTA_HIDDEN_KEYS"),
    # (a) 键名常量新家：web/services/env_io.py
    "GATED_KEYS": ("web.services.env_io", "GATED_KEYS"),
    "GLOBAL_PAUSE_KEY": ("web.services.env_io", "GLOBAL_PAUSE_KEY"),
    "MASTER_ONLY_KEYS": ("web.services.env_io", "MASTER_ONLY_KEYS"),
    "ANNOUNCEMENT_KEY": ("web.services.env_io", "ANNOUNCEMENT_KEY"),
    "ANNOUNCEMENT_DRAFT_KEY": ("web.services.env_io", "ANNOUNCEMENT_DRAFT_KEY"),
    "ANNOUNCEMENT_DRAFT_META_KEY": ("web.services.env_io", "ANNOUNCEMENT_DRAFT_META_KEY"),
    "ANNOUNCEMENT_PUBLISHED_META_KEY": ("web.services.env_io",
                                        "ANNOUNCEMENT_PUBLISHED_META_KEY"),
    # (b) 纯助手新家：web/services/http_request.py
    "_json_body": ("web.services.http_request", "_json_body"),
    "_request_is_same_origin": ("web.services.http_request", "_request_is_same_origin"),
}
TRUE_SOURCE.update(BATCH3B_TRUE_SOURCE)

#: 名字本身是模块对象（按名字导入，不做属性访问）。
MODULE_TYPED = frozenset({
    "child_env", "signin", "db", "clock", "yb_egress", "yiban_status",
    "account_crypto", "env_io", "mailer", "notify", "mail_config", "mail_layout",
})

#: 从包根按名再导出（`from yiban import __version__ as APP_VERSION`）：读法＝裸名。
DIRECT_NAME = frozenset({"APP_VERSION"})


def _route_files():
    """路由目录下的模块文件名（排除装配件 `__init__.py`）。"""
    return sorted(fn for fn in os.listdir(_ROUTES_DIR)
                  if fn.endswith(".py") and fn != "__init__.py")


def _top_level_imports(tree):
    """模块级导入表：别名 -> ("module", 模块路径) 或 ("name", 模块, 原名)。

    `import X as A` / `from P import M as A`（M 是子模块）记为 module 形态；
    `from P import f as A` 记为 name 形态。
    """
    out = {}
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Import):
            for a in node.names:
                out[a.asname or a.name] = ("module", a.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                alias = a.asname or a.name
                if a.asname and a.name[:1].islower():
                    out[alias] = ("module", node.module + "." + a.name)
                else:
                    out[alias] = ("name", node.module, a.name)
        elif isinstance(node, ast.Try):
            stack.extend(node.body)
            stack.extend(node.orelse)
            stack.extend(node.finalbody)
    return out


def _reads_true_source(imports, alias, mod, orig):
    """别名 alias 读出的对象是否就是真源的 `mod.orig`。"""
    got = imports.get(alias)
    if got is None:
        return False
    if got[0] == "module":
        return got[1] == mod
    return got[1] == mod and got[2] == orig


def scan(src):
    """扫一段源码：返回 (墙读取, 属性读取, 模块级导入, 裸名读, 被赋值名)。"""
    tree = ast.parse(src)
    wall, attrs, names, assigned = [], [], set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            base = node.value.id
            attrs.append((base, node.attr, node.lineno))
            if base == "m":
                wall.append((node.attr, node.lineno))
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                names.add(node.id)
            else:
                assigned.add(node.id)
    return wall, attrs, _top_level_imports(tree), names, assigned


def a_wall_reads(src):
    """源码里"经转发墙读 A 名"的 (名字, 行号) 列表——本门的核心判据。"""
    wall = scan(src)[0]
    return [(name, line) for name, line in wall if name in A_NAMES]


def a_reads_off_true_source(src):
    """源码里"A 名读法不合真源"的 (名字, 基底, 行号) 列表。

    名字型（模块对象与包根再导出）：该名必须从真源导入，且未被本文件赋值遮蔽。
    其余 A 名：读法不得落在**另一个真源模块**的别名上（`m.` 形态由墙判据管）。
    基数是 `db` 一类非真源模块的读点不在本判据面内（那些是既有合法读点）。
    """
    _wall, attrs, imports, names, assigned = scan(src)
    true_mods = {mod for mod, _orig in TRUE_SOURCE.values()}
    src_alias = {alias: got[1] for alias, got in imports.items()
                 if got[0] == "module" and got[1] in true_mods}
    problems = []
    for name in sorted(A_NAMES):
        mod, orig = TRUE_SOURCE[name]
        if name in MODULE_TYPED or name in DIRECT_NAME:
            if name in names and name not in assigned:
                got = imports.get(name)
                want_mod = mod if orig is None else mod + "." + orig
                if got not in (("module", want_mod), ("name", mod, orig)):
                    problems.append((name, name, 0))
            continue
        for base, attr, line in attrs:
            if attr != orig or base == "m":
                continue
            if base in src_alias and src_alias[base] != mod:
                problems.append((name, base, line))
    return problems


def _read(fn):
    with io.open(os.path.join(_ROUTES_DIR, fn), encoding="utf-8") as f:
        return f.read()


def app_definition_names(src):
    """一段源码里的**真定义**名：`def` / `class` / 赋值目标。

    import 绑定与转发别名（`X = mod.attr` 之外的 `from P import X`）不算真定义——
    本判据要钉的是"真源只许一处"，转发面本身不构成第二份定义。
    """
    tree = ast.parse(src)
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            out.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


class WallDirectReadsGateTest(unittest.TestCase):
    """批 3-a：A 类名字不得再经转发墙读；读法必须落在真源上。"""

    def test_scan_face_is_not_empty(self):
        files = _route_files()
        self.assertGreaterEqual(len(files), _MIN_FILES,
                                "路由扫描面过小（%d 个文件）：路径或解析器失效" % len(files))
        total = sum(len(scan(_read(fn))[0]) for fn in files)
        self.assertGreater(total, 0,
                           "扫到 0 处 `m.*` 读取：扫描器失效（扫空不得当绿）")

    def test_lists_are_disjoint_and_frozen(self):
        self.assertEqual(sorted(A_NAMES & B_NAMES), [],
                         "A 名单与 B 名单相交：两份名单已漂移")
        self.assertEqual(sorted(A_NAMES & ALIAS_NOT_MIGRATED), [],
                         "A 名单与未迁别名名单相交：两份名单已漂移")
        self.assertEqual(sorted(B_NAMES & ALIAS_NOT_MIGRATED), [],
                         "B 名单与未迁别名名单相交：两份名单已漂移")
        self.assertGreaterEqual(len(A_NAMES), 140, "A 名单过小：名单被截断")
        self.assertGreaterEqual(len(B_NAMES), 58, "B 名单过小：名单被截断")
        self.assertGreaterEqual(len(BATCH3B_NAMES), 47, "批 3-b 已迁名单过小：名单被截断")

    def test_a_names_are_not_read_through_the_wall(self):
        bad = []
        for fn in _route_files():
            for name, line in a_wall_reads(_read(fn)):
                bad.append("%s:%d: m.%s" % (fn, line, name))
        self.assertEqual(
            bad, [],
            "以下路由读取仍走转发墙（A 名须直连真源）：\n" + "\n".join(bad))

    def test_a_names_are_read_from_true_source(self):
        bad = []
        for fn in _route_files():
            for name, base, line in a_reads_off_true_source(_read(fn)):
                mod, orig = TRUE_SOURCE[name]
                want = mod if orig is None else mod + "." + orig
                bad.append("%s:%d: %s 经 %s 读，真源是 %s" % (fn, line, name, base, want))
        self.assertEqual(bad, [], "以下 A 名的读法不在真源上：\n" + "\n".join(bad))

    def test_batch3b_names_have_no_true_definition_in_app(self):
        """判据⑤：批 3-b 已迁名在 `web/app.py` 里不得再有真定义（真源只许一处）。"""
        with io.open(_APP_PY, encoding="utf-8") as f:
            defined = app_definition_names(f.read())
        self.assertGreater(len(defined), 50,
                           "web/app.py 真定义面过小——路径或解析器失效（扫空不得当绿）")
        dup = sorted(BATCH3B_NAMES & defined)
        self.assertEqual(
            dup, [],
            "以下批 3-b 已迁名在 web/app.py 仍有真定义（真源与真定义两处并存）：%s"
            % ", ".join(dup))

    def test_app_definition_scanner_has_teeth(self):
        """突变验证：塞回一处真定义 ⇒ 判据⑤必须命中；转发别名不算。"""
        self.assertIn("BATCH_OP_LIMIT", app_definition_names("BATCH_OP_LIMIT = 10\n"))
        self.assertIn("_json_body", app_definition_names("def _json_body():\n    pass\n"))
        self.assertIn("MASTER_ONLY_KEYS",
                      app_definition_names("MASTER_ONLY_KEYS: frozenset = frozenset()\n"))
        self.assertNotIn(
            "BATCH_OP_LIMIT",
            app_definition_names("from web.services.limits import BATCH_OP_LIMIT\n"),
            "import 转发别名被误判成真定义：判据⑤会误红")

    def test_wall_names_are_classified(self):
        unknown = set()
        for fn in _route_files():
            for name, _line in scan(_read(fn))[0]:
                if (name not in B_NAMES and name not in A_NAMES
                        and name not in ALIAS_NOT_MIGRATED):
                    unknown.add(name)
        self.assertEqual(
            sorted(unknown), [],
            "转发墙上出现未分类的名字：%s；请归入 A（迁走）、B（登记进 B_NAMES）"
            "或 ALIAS_NOT_MIGRATED（登记理由）" % ", ".join(sorted(unknown)))

    def test_scanner_has_teeth(self):
        """突变验证：塞一处 `m.<A名>` ⇒ 核心判据必须命中。"""
        src = ("def view():\n"
               "    m = _appmod()\n"
               "    return m.load_accounts()\n")
        self.assertEqual([n for n, _l in a_wall_reads(src)], ["load_accounts"],
                         "`m.<A名>` 形态未命中：本门没有牙")
        self.assertEqual(a_reads_off_true_source(src), [],
                         "墙读不该同时报成真源读法错（两条判据必须正交）")
        clean = ("from web.services import accounts_data as _accounts_data\n"
                 "def view():\n"
                 "    return _accounts_data.load_accounts()\n")
        self.assertEqual(a_wall_reads(clean), [], "真源读法被误判成墙读")
        self.assertEqual(a_reads_off_true_source(clean), [], "真源读法被误判成不合真源")
        wrong = ("from web.services import logs as _logs_svc\n"
                 "def view():\n"
                 "    return _logs_svc.load_accounts()\n")
        self.assertEqual([n for n, _b, _l in a_reads_off_true_source(wrong)],
                         ["load_accounts"], "错真源模块读法未判红：本门没有牙")
        wrong_mod = ("from yiban.core import clock as db\n"
                     "def view():\n"
                     "    return db.load_users()\n")
        self.assertEqual([n for n, _b, _l in a_reads_off_true_source(wrong_mod)],
                         ["db"], "模块型名字从错源导入未判红：本门没有牙")


if __name__ == "__main__":
    unittest.main()
