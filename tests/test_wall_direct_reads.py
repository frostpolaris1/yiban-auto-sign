# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""拆墙门（批 3-a 起；批 3-d 收口）：路由模块不得经 `web.app` 转发墙读 A 类名字。

**判据**
① 扫 `web/routes/*.py` 的 `m.<名字>` 读取：A 类名字必须 0 处。
② A 名一律从真源取：每个 A 名的读取点必须落在真源模块上（属性访问的基底或裸名导入）。
③ B 类名字不受本门约束：B 名是**永驻墙名**（组合根自身状态），批 3-d 后只剩 5 名。
④ 名单冻结：A ∩ B = ∅；扫描面 < 11 文件即红；0 处读取即红。
⑤ 批 3-b 已迁名（`BATCH3B_NAMES`）在 `web/app.py` 里不得再有**真定义**
   （`def` / `class` / 赋值）。真定义与真源两处同时存在即为双源。
⑥ 已去壳名（`UNWRAPPED_IN_3C_S1` / `_S2` / `_S3`）在 `web/app.py` 不得再有模块级绑定。
⑦ 批 3-d 已迁名（`BATCH3D_NAMES`，39 名）在 `web/app.py` 不得再有**任何绑定**
   （含 import）：它们的目的地只有 `web/services/*` 与 `web/render.py`。
⑧ 墙上剩余名字必须**恰好等于**永驻名单（5 名）＋ `__file__`——不是"⊆"。
⑨ 双源债务集（`_WALL_RESIDUAL_DEBT`）只许缩：每项必须仍真的双源。
⑩ **面外载体**（`frontend/e2e` / `scripts` / `docker` / `deploy`）不得经 app 模块对象读
   3-d 已迁名——它们不经 `web/routes`，判据① 扫不到（Playwright 启动器即其一）。

**来源与日期**
批 3-a 名单取自 `origin/develop` 的 `5823c48e`（2026-10-19）：A 名＝`web/app.py` 以
import 形态转发的、且被路由经 `m.*` 读取的名字；B 名＝同批读取里 `web/app.py` 自有
定义的名字。
批 3-b 名单取自 `origin/develop` 的 `e5aceb07`（2026-10-19）：从 B 名里挑出**纯常量**
与**纯助手函数**两类，搬进 `web/services/*`，故并入 A 侧判据。
批 3-c S1（`origin/develop` 的 `46e5f39c`，2026-10-19）：把 10 个自建别名去壳。
批 3-c S2（`origin/develop` 的 `86d06f5c`，2026-10-19）：两个进程内缓存搬
`web/services/startup_cache.py`。
批 3-c S3（`refactor/3c-cred-state` 末）：凭据/审核/校验进程族 15 名去壳，接缝交给
`web.security.configure(...)` 与 `web.services.verify_queue.configure(...)`。

**边界（批 3-d 收口后）**
本门只管读取形态，不管 `_appmod()` 本身。批 3-d 把剩余的 39 个"app 状态注入包装"搬进
服务层，并把注入改为各服务模块的 `configure(...)` ＋ `_hooks`（登记 lambda，调用时刻
解析）。墙上因此只剩组合根自身状态：
  · `ENV_FILE` / `STATE_DIR` / `DB_FILE`：路径句柄，`--config` 与测试都改写它们，
    组合根是唯一定义处；
  · `WEB_VERSION`：启动时刻派生值，每次进程启动变化；
  · `logger`：进程级日志通道。
`LOG_FILE` 已不属墙名（`web.app` 无定义，路由直取服务层 hook）。`__file__` 是 app.py
自身属性，无法搬走，故留在 `ALIAS_NOT_MIGRATED`；`STATUS_TEXT` 同理保留（无路由读者）。
本门对永驻 5 名的 `m.<名字>` 不做迁移断言，但 ⑧ 钉住"墙上不会多出别的名字"。
"""
import ast
import io
import os
import shutil
import tempfile
import unittest
from typing import ClassVar

#: 路由目录（本门只扫这里；`__init__.py` 是装配件，不算路由体）。
_ROUTES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "web", "routes")
#: 扫描面下限：带 `m.*` 读取的路由文件实测 14 个，低于 11 即扫描器失效。
_MIN_FILES = 11
#: `web/app.py` 路径（判据⑤：已迁名不得再有真定义）。
_APP_PY = os.path.join(os.path.dirname(_ROUTES_DIR), "app.py")
#: 服务层目录（判据⑨：派生债务集要真源侧的定义面）。
_WEB_DIR = os.path.dirname(_ROUTES_DIR)


#: 残留双源债务集（判据⑨）：既不在 B_NAMES 、也不在 ALIAS_NOT_MIGRATED ，
#: 但 `web/app.py` 与服务层**同时**有模块级定义的名字。
#:
#: 为何存在：批 3-c 只迁了凭据 / 审核 / 校验族。剩下的转发包装仍有真读者在
#: `web/app.py` 内部（启动路径、日志 handler、create_app 内的接线），
#: 不是零读者，不能一次性删。它们是车道 C（墙残余收敛）的活。
#:
#: **本集只可缩。** 新增一项就是新的双源。每一项必须真仍然双源，
#: 否则 `test_residual_debt_entries_are_really_duplicated` 会红。
_WALL_RESIDUAL_DEBT = frozenset({
    "_SCRIPTS_DIR",
    "_alert_channel_status",
    "_channel_status_lines",
    "_mail_alert_due",
    "_push_ever_configured",
    "_report_env_key_collisions",
    "_send_channel_health_report",
    "_sensitive_gate_params",
    "_today_has_logs",
    "check_admin_configured",
    "ensure_secret_key",
    "migrate_admin_password_to_hash",
    "reject_default_admin_password",
    "write_env_int",
    "write_env_key",
})
#: 债务集规模上限（阶梯式闸，只许降）。
_WALL_RESIDUAL_DEBT_MAX = 15


def _read_abs(path):
    """读绝对路径的文本件。"""
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _walk_module_scope(nodes):
    """遍历**模块作用域**的语句节点，穿过复合语句、不进入函数/类体。

    为什么必须穿：`try:` / `if TYPE_CHECKING:` / `with … as X:` / `for X in …:` 里的
    绑定在模块级同样让 `web.app.<名>` 重新可用；只扫 `tree.body` 的直接子节点会静默
    放行这些形态（2026-10-20 独立审查实测：五种形态全部漏判，判据⑦与⑨同时失明）。
    函数体与类体仍然不进入——判据⑨是跨文件求交集，局部名会把交集淹没。

    `ast.TryStar`（`try … except*`）**不是** `ast.Try` 的子类，必须单列——复审实测
    它原本漏判（追加 `except*` 块内的 import 仍绿），故与本函数同批补上。
    """
    stack = list(nodes)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue                      # 自身是绑定，但不进它的体
        if isinstance(node, ast.If):
            stack.extend(node.body)
            stack.extend(node.orelse)
        elif isinstance(node, (ast.Try, ast.TryStar)):
            stack.extend(node.body)
            stack.extend(node.orelse)
            stack.extend(node.finalbody)
            for h in node.handlers:
                stack.extend(h.body)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            stack.extend(node.body)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            stack.extend(node.body)
            stack.extend(node.orelse)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                stack.extend(case.body)


def _assign_target_names(target):
    """赋值目标里的名字：`X` / `a, b` / `*a, b` / `[a, b]` 全部展开。

    只认 `ast.Name` 会漏掉元组与星号目标（`send_notification, _x = f()` 在模块级
    同样让 `web.app.send_notification` 可用）。复审实测：旧实现对该形态放行。
    """
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        out = set()
        for elt in target.elts:
            out |= _assign_target_names(elt)
        return out
    if isinstance(target, ast.Starred):
        return _assign_target_names(target.value)
    return set()


def _toplevel_definitions(src):
    """一段源码里的**模块级**真定义名（`def` / `class` / 赋值目标）。

    与 `app_definition_names` 的差别：不收函数体内的局部名。
    为什么必须分开：判据⑨是跨文件求交集，若把局部变量算进来（实测多出 21 个假
    命中，如 `path` / `raw` / `result` / `ts`），交集立刻被噪声淹没，判据失去分辨力。
    复合语句里的模块级绑定算真定义（见 `_walk_module_scope`）。
    """
    out = set()
    for node in _walk_module_scope(ast.parse(src).body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                out |= _assign_target_names(t)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


#: 仓库根（`web/` 的上一级）。
_REPO_ROOT = os.path.dirname(_WEB_DIR)
#: 面外载体目录：持有 `web/app.py` 模块对象、却不经 `web/routes` 的代码。
#: 现量（2026-10-20 实测）：`frontend/e2e` 1 ＋ `scripts` 20 ＋ `docker` 1 ＝ 22 个 `.py`
#: （`deploy/` 下无 `.py`，仍保留在清单里——它是最可能新增长期读者的地方）。
_OUT_OF_FACE_DIRS = ("frontend/e2e", "scripts", "docker", "deploy")
#: 扫描面下限（实测 22；低于 15 即目录改名或过滤失效）。
_OUT_OF_FACE_MIN_FILES = 15


def _out_of_face_files():
    """面外载体 `.py` 文件绝对路径（只扫清单内目录，不递归进测试圈）。"""
    out = []
    for rel in _OUT_OF_FACE_DIRS:
        d = os.path.join(_REPO_ROOT, *rel.split("/"))
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".py"):
                out.append(os.path.join(d, fn))
    return out


def _out_of_face_hits(src):
    """一段源码里**经 app 模块对象**读 3-d 已迁名 → `行号: 基.名`。

    面外载体经 importlib 载入的是 `web/app.py` 的**模块对象**，基底名不固定
    （`webapp` / `app_mod` / `webapp_e2e` / `web_app`），也可能挂在属性上
    （`self.webapp.x` / `mod.webapp.x`）。故基底取"裸名"或"属性链的末段名"，
    并只收名字含 `app` 的那些：直取服务层真源（`_logs_svc.log_path_for`）是
    **正确**写法，不得误红——旧版按属性名一刀切，本门第一版就误红了这条。
    """
    hits = []
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Attribute) and node.attr in BATCH3D_NAMES):
            continue
        base = node.value
        name = (base.id if isinstance(base, ast.Name)
                else base.attr if isinstance(base, ast.Attribute) else "")
        if "app" in name.lower():
            hits.append("%d: %s.%s" % (node.lineno, name, node.attr))
    return hits


def _service_modules():
    """服务层模块路径列表：`web/security.py` ＋ `web/services/*.py`（排除 `__init__`）。"""
    out = [os.path.join(_WEB_DIR, "security.py")]
    svc_dir = os.path.join(_WEB_DIR, "services")
    for fn in sorted(os.listdir(svc_dir)):
        if fn.endswith(".py") and fn != "__init__.py":
            out.append(os.path.join(svc_dir, fn))
    return out


def _route_files_in(root):
    """在给定目录下**递归**取路由模块相对路径（排除 `__init__.py`）。

    抽成带 `root` 参数的函数，是为了让递归本身可被实测（造临时子目录验）。
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fn in sorted(filenames):
            if not fn.endswith(".py") or fn == "__init__.py":
                continue
            out.append(os.path.relpath(os.path.join(dirpath, fn), root))
    return sorted(out)

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

#: 批 3-d 已迁名：原"app 状态注入包装"39 名。它们现在住在服务层；注入改由各模块的
#: `configure(...)` ＋ `_hooks` 承担。判据⑦ 钉住 `web/app.py` 不得再有**任何绑定**。
BATCH3D_NAMES = frozenset({
    "_accounts_at_capacity", "_capacity_estimate", "_day_off_reason", "_doc_page",
    "_estimate_slot", "_executor_rows", "_executors_window", "_in_run_period",
    "_log_lines_for", "_log_manual_sign_exit", "_measure_state_path",
    "_most_recent_log_date", "_mutate_executor_rows", "_notify_capacity_once",
    "_read_doc_html", "_registration_paused", "_save_fallback_egress",
    "_save_row_egress", "_save_slot_egress", "_settings_effective_values",
    "_sign_window", "_slot_to_label", "_users_at_capacity", "_write_measure_state",
    "accounts_terminal_summary", "edge_config", "email_domain_error", "icp_info",
    "load_sign_state", "log_path_for", "parse_sign_log", "police_info", "police_link",
    "send_notification", "sign_status", "sign_window_bounds", "site_description",
    "site_image", "write_env_batch",
})

#: 本门约束的 A 名单＝批 3-a 的转发名 ∪ 批 3-b 已迁名 ∪ 批 3-d 已迁名。
A_NAMES = A_NAMES_BATCH3A | BATCH3B_NAMES | BATCH3D_NAMES

#: 永驻墙名：`web/app.py` 是组合根，这 5 名是它自身状态，路由经 `m.*` 读取是既有接缝。
#: 批 3-d 后本集合即墙上**全部**名字（另加 `__file__`，见判据⑧）。
B_NAMES = frozenset({
    "DB_FILE", "ENV_FILE", "STATE_DIR", "WEB_VERSION", "logger",
})

#: 批 3-c S3 已去壳的凭据 / 审核 / 校验进程族：`web/app.py` 里不得再有模块级绑定。
#: 它们是本批从 `B_NAMES` 移出的 15 名；再出现即第二份可漂移的面（判据⑧钉住）。
#: 判据① 管读侧（路由不得经墙读），判据⑧ 管写侧（墙上不得再绑定），缺一条静默一半。
#:
#: **名单完整性由 `test_residual_debt_is_frozen` 从代码侧反查兜底。** 本名单是按
#: 实现时手工枚举的，漏一个就静默一格（2026-10-20 独立审查实测抓到 3 个漏登：
#: `_admin_session_facts` / `_effective_role` / `run_gated_call`，均已补入）。
#: 手工枚举只能保证“已想到的不回来”，保证不了“没想到的进不来”——故必须有派生判据。
UNWRAPPED_IN_3C_S3 = frozenset({
    "_account_verify_enabled", "_admin_session_facts", "_builtin_admin_email",
    "_builtin_admin_loginable", "_current_role", "_effective_role",
    "_is_builtin_admin_session", "_issue_admin_sid", "_pw_gate_tier",
    "run_gated_call", "run_verify_with_gate", "verify_admin", "verify_async_enabled",
    "VERIFY_BUSY_MSG", "VERIFY_FAIL_COOLDOWN_MSG",
})

#: 留在墙上、但不属 A 也不属 B 的名字（本批不迁，见模块 docstring 的边界段）。
ALIAS_NOT_MIGRATED = frozenset({
    "STATUS_TEXT", "__file__",
})

#: 批 3-c S1 已去壳的别名：`web/app.py` 里不得再有赋值 / `def` / `class`（判据⑥）。
#: 它们是 3-c S1 从 `ALIAS_NOT_MIGRATED` 移出的 10 名；再出现即第二份可漂移的面。
UNWRAPPED_IN_3C_S1 = frozenset({
    "DELETED_RETENTION_DAYS", "EXECUTOR_INDEX_MAX", "STATUS_ALREADY", "STATUS_ICON",
    "STATUS_NO_TASK", "STATUS_PENDING", "STATUS_SUCCESS", "STATUS_USER_CANCELLED",
    "VERIFY_JOB_TERMINAL", "_has_line_break",
})

#: 批 3-c S2 已去壳的进程内缓存名：`web/app.py` 里不得再有赋值 / `def` / `class`（判据⑥）。
#: 它们从 `B_NAMES` 移出（本片口径：`ref=origin/develop` 的 `86d06f5c` 实测
#: `len(B_NAMES)==58`，移出这 2 名即 56；同批 S3 片另移出 12 名，故当前下限见
#: `test_lists_are_disjoint_and_frozen`）。真源在 `web/services/startup_cache.py`；
#: 读者（路由 9 处 ＋ 测试 7 处）已改直取新家。
UNWRAPPED_IN_3C_S2 = frozenset({
    "_announcement_cache", "_changelog_cache",
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

#: 批 3-d 已迁名的真源：新家模块 → 原名（不改名，故逐条同形）。
BATCH3D_TRUE_SOURCE = {
    # 签到窗口/状态族 → web/services/signstatus.py
    "_sign_window": ("web.services.signstatus", "_sign_window"),
    "_in_run_period": ("web.services.signstatus", "_in_run_period"),
    "_day_off_reason": ("web.services.signstatus", "_day_off_reason"),
    "sign_window_bounds": ("web.services.signstatus", "sign_window_bounds"),
    "sign_status": ("web.services.signstatus", "sign_status"),
    # 渲染/站点展示族 → web/render.py
    "edge_config": ("web.render", "edge_config"),
    "_read_doc_html": ("web.render", "_read_doc_html"),
    "_doc_page": ("web.render", "_doc_page"),
    "email_domain_error": ("web.render", "email_domain_error"),
    "icp_info": ("web.render", "icp_info"),
    "police_info": ("web.render", "police_info"),
    "police_link": ("web.render", "police_link"),
    "site_description": ("web.render", "site_description"),
    "site_image": ("web.render", "site_image"),
    # 日志/状态文件族 → web/services/logs.py
    "load_sign_state": ("web.services.logs", "load_sign_state"),
    "accounts_terminal_summary": ("web.services.logs", "accounts_terminal_summary"),
    "parse_sign_log": ("web.services.logs", "parse_sign_log"),
    "log_path_for": ("web.services.logs", "log_path_for"),
    "_log_lines_for": ("web.services.logs", "_log_lines_for"),
    "_most_recent_log_date": ("web.services.logs", "_most_recent_log_date"),
    # 手动签到留痕 → web/services/manual_sign.py
    "_log_manual_sign_exit": ("web.services.manual_sign", "_log_manual_sign_exit"),
    # 自选片展示 → web/services/accounts_data.py
    "_slot_to_label": ("web.services.accounts_data", "_slot_to_label"),
    "_estimate_slot": ("web.services.accounts_data", "_estimate_slot"),
    # 执行体族 → web/services/executor_env.py
    "_executors_window": ("web.services.executor_env", "_executors_window"),
    "_executor_rows": ("web.services.executor_env", "_executor_rows"),
    "_mutate_executor_rows": ("web.services.executor_env", "_mutate_executor_rows"),
    "_save_slot_egress": ("web.services.executor_env", "_save_slot_egress"),
    "_save_row_egress": ("web.services.executor_env", "_save_row_egress"),
    "_save_fallback_egress": ("web.services.executor_env", "_save_fallback_egress"),
    # 实测族 → web/services/measure.py
    "_measure_state_path": ("web.services.measure", "_measure_state_path"),
    "_write_measure_state": ("web.services.measure", "_write_measure_state"),
    # 容量族 → web/services/capacity.py
    "_capacity_estimate": ("web.services.capacity", "_capacity_estimate"),
    "_accounts_at_capacity": ("web.services.capacity", "_accounts_at_capacity"),
    "_registration_paused": ("web.services.capacity", "_registration_paused"),
    "_users_at_capacity": ("web.services.capacity", "_users_at_capacity"),
    "_notify_capacity_once": ("web.services.capacity", "_notify_capacity_once"),
    # 告警出口 → web/services/notify_mail.py
    "send_notification": ("web.services.notify_mail", "send_notification"),
    # .env 写入与设置项展示 → web/services/env_io.py
    "write_env_batch": ("web.services.env_io", "write_env_batch"),
    "_settings_effective_values": ("web.services.env_io", "_settings_effective_values"),
}
TRUE_SOURCE.update(BATCH3D_TRUE_SOURCE)

#: 名字本身是模块对象（按名字导入，不做属性访问）。
MODULE_TYPED = frozenset({
    "child_env", "signin", "db", "clock", "yb_egress", "yiban_status",
    "account_crypto", "env_io", "mailer", "notify", "mail_config", "mail_layout",
})

#: 从包根按名再导出（`from yiban import __version__ as APP_VERSION`）：读法＝裸名。
DIRECT_NAME = frozenset({"APP_VERSION"})


def _route_files():
    """路由目录下（**递归**）的模块文件相对路径，排除装配件 `__init__.py`。

    为什么递归：`os.listdir` 只看一层，子目录里的路由完全不被扫。本仓同级门
    `test_web_boundary.py` 用的是 `os.walk`（见其 `_WALL_ROUTES_DIR` 扫描），两门
    扫描面口径必须一致，否则同一处泄漏在一门里红、在另一门里静默绿。
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(_ROUTES_DIR):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fn in sorted(filenames):
            if not fn.endswith(".py") or fn == "__init__.py":
                continue
            out.append(os.path.relpath(os.path.join(dirpath, fn), _ROUTES_DIR))
    return sorted(out)


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


def app_binding_names(src):
    """一段源码里的**模块级绑定**名：`def` / `class` / 赋值目标 / `import` 绑定。

    与 `app_definition_names` 的差别只有一条：包含 `import` 绑定。
    为什么判据⑥/⑧ 必须用这一版：去壳名要防的是"经 `m.<名>` 还能取到东西"。
    `from yiban.core.status import ICON as STATUS_ICON` 不是真定义，却让
    `web.app.STATUS_ICON` 重新可用——真定义口径看不见它（实测漏 5 种形态，
    `import-as` 是其中可用且真实可达的一种）。

    仍不覆盖的动态形态（`__getattr__` / `globals()[k]=` / `setattr`）由
    `test_wall_no_dynamic_forwarding` 单独钉住。复合语句（`try` / `if` / `with … as` /
    `for … in`）里的模块级绑定同样算——只扫 `tree.body` 会让这些形态静默放行，
    2026-10-20 独立审查已实测五种全部漏判（见 `_walk_module_scope`）。
    """
    out = set()
    for node in _walk_module_scope(ast.parse(src).body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                out |= _assign_target_names(t)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    out.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if isinstance(item.optional_vars, ast.Name):
                    out.add(item.optional_vars.id)
        elif (isinstance(node, (ast.For, ast.AsyncFor))
              and isinstance(node.target, ast.Name)):
            out.add(node.target.id)
    return out


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
        self.assertEqual(sorted(A_NAMES & UNWRAPPED_IN_3C_S1), [],
                         "已去壳名与 A 名单相交：A 名须直连真源，不得回墙上")
        self.assertEqual(sorted(B_NAMES & UNWRAPPED_IN_3C_S1), [],
                         "已去壳名与 B 名单相交：B 名仍在墙上，去壳即名单漂移")
        self.assertEqual(sorted(ALIAS_NOT_MIGRATED & UNWRAPPED_IN_3C_S1), [],
                         "已去壳名仍留在未迁名单：两份名单已漂移")
        # 批 3-c S2 的新集合与其余三个名单必须互斥（含 A 侧：S2 名已直取真源）。
        for other_name, other in (("A", A_NAMES), ("B", B_NAMES),
                                  ("未迁别名", ALIAS_NOT_MIGRATED),
                                  ("3-c S1 已去壳", UNWRAPPED_IN_3C_S1)):
            self.assertEqual(sorted(UNWRAPPED_IN_3C_S2 & other), [],
                             "3-c S2 已去壳名与 %s 名单相交：两份名单已漂移" % other_name)
        # 批 3-c S3：12 名凭据 / 审核 / 校验进程族与其余五个名单必须互斥。
        for other_name, other in (("A", A_NAMES), ("B", B_NAMES),
                                  ("未迁别名", ALIAS_NOT_MIGRATED),
                                  ("3-c S1 已去壳", UNWRAPPED_IN_3C_S1),
                                  ("3-c S2 已去壳", UNWRAPPED_IN_3C_S2)):
            self.assertEqual(sorted(UNWRAPPED_IN_3C_S3 & other), [],
                             "3-c S3 已去壳名与 %s 名单相交：两份名单已漂移" % other_name)
        # 批 3-d：39 名并入 A 侧（故不与 A 判相交），与其余五个名单必须互斥。
        for other_name, other in (("B", B_NAMES),
                                  ("未迁别名", ALIAS_NOT_MIGRATED),
                                  ("3-c S1 已去壳", UNWRAPPED_IN_3C_S1),
                                  ("3-c S2 已去壳", UNWRAPPED_IN_3C_S2),
                                  ("3-c S3 已去壳", UNWRAPPED_IN_3C_S3)):
            self.assertEqual(sorted(BATCH3D_NAMES & other), [],
                             "批 3-d 已迁名与 %s 名单相交：两份名单已漂移" % other_name)
        self.assertGreaterEqual(len(A_NAMES), 140, "A 名单过小：名单被截断")
        # 批 3-d 收口：B 名单只剩永驻 5 名。用等号而不是下限——多一名就是新的墙名，
        # 少一名就是有人把组合根状态删了；两种都必须有人看见并改这里的名单。
        self.assertEqual(len(B_NAMES), 5, "永驻墙名必须恰好 5 名（批 3-d 收口口径）")
        self.assertGreaterEqual(len(BATCH3B_NAMES), 47, "批 3-b 已迁名单过小：名单被截断")
        self.assertEqual(len(BATCH3D_NAMES), 39, "批 3-d 已迁名单过小：名单被截断")
        # 名单与真源表必须逐一对应：只钉"39 名"挡不住"换掉一名仍 39 名"——
        # 那样门会盯着一个没搬的名字，而真正搬走的那名失去保护（独立审查实测可绕）。
        self.assertEqual(sorted(BATCH3D_NAMES), sorted(BATCH3D_TRUE_SOURCE),
                         "3-d 已迁名单与 BATCH3D_TRUE_SOURCE 键集不一致：两份名单已漂移")
        self.assertGreaterEqual(len(UNWRAPPED_IN_3C_S2), 2,
                                "批 3-c S2 已去壳名单过小：名单被截断")

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
        """判据⑤：批 3-b 已迁名在 `web/app.py` 里不得再有真定义（真源只许一处）。

        口径：本判据用 `app_definition_names`（不含 `import` 绑定）。批 3-b 迁的是
        常量与纯助手，`from P import X` 的再导出不算第二份定义——本判据不把它判红。
        """
        with io.open(_APP_PY, encoding="utf-8") as f:
            defined = app_definition_names(f.read())
        self.assertGreater(len(defined), 50,
                           "web/app.py 真定义面过小——路径或解析器失效（扫空不得当绿）")
        dup = sorted(BATCH3B_NAMES & defined)
        self.assertEqual(
            dup, [],
            "以下批 3-b 已迁名在 web/app.py 仍有真定义（真源与真定义两处并存）：%s"
            % ", ".join(dup))

    def test_3c_s1_names_have_no_binding_in_app(self):
        """判据⑥：批 3-c S1 已去壳的 10 名在 `web/app.py` 里不得再有**模块级绑定**。

        它们此前是自建别名（`X = mod.attr`），读者经 `m.*` 取用；本批把读者改直连真源
        并删掉别名。留一个别名＝真源之外还有第二份可漂移的面，故此判据与①（路由不得墙读）
        成对：① 管读侧、⑥ 管写侧，缺一条就有一半静默。

        **口径为何是绑定而非真定义**：去壳名要防的是"经 `m.<名>` 还能取到东西"。
        `from yiban.core.status import ICON as STATUS_ICON` 不是真定义，却让
        `web.app.STATUS_ICON` 重新可用（实测：真定义口径漏此形态，且它真实可达）。
        绑定口径覆盖赋值 / `def` / `class` / `import` / `import ... as`。
        """
        with io.open(_APP_PY, encoding="utf-8") as f:
            bound = app_binding_names(f.read())
        self.assertGreater(len(bound), 100,
                           "web/app.py 模块级绑定面过小——路径或解析器失效（扫空不得当绿）")
        dup = sorted(UNWRAPPED_IN_3C_S1 & bound)
        self.assertEqual(
            dup, [],
            "以下批 3-c S1 已去壳名在 web/app.py 仍有模块级绑定（读者已直取真源，"
            "该名是第二份面）：%s" % ", ".join(dup))

    def test_3c_s2_names_have_no_binding_in_app(self):
        """判据⑥（S2 片）：批 3-c S2 已去壳的两个缓存名在 `web/app.py` 不得再有绑定。

        它们此前是 `web/app.py` 的模块级单格列表（`[None]`），被
        `web/routes/settings_api.py` 经 `m.*` 读写。本批把读者改为直取
        `web/services/startup_cache`，并删掉 app.py 的定义行。留一处赋值＝
        真源之外还有第二份可漂移的面，且路由不再读它。口径同 S1 判据（绑定，含 import）。
        """
        with io.open(_APP_PY, encoding="utf-8") as f:
            bound = app_binding_names(f.read())
        self.assertGreater(len(bound), 100,
                           "web/app.py 模块级绑定面过小——路径或解析器失效（扫空不得当绿）")
        dup = sorted(UNWRAPPED_IN_3C_S2 & bound)
        self.assertEqual(
            dup, [],
            "以下批 3-c S2 已去壳名在 web/app.py 仍有模块级绑定（读者已直取真源，"
            "旧名是第二份面）：%s" % ", ".join(dup))

    def test_3c_s3_names_have_no_binding_in_app(self):
        """判据⑧：批 3-c S3 已去壳的 15 名在 `web/app.py` 里不得再有模块级绑定。

        它们此前是转发包装（`def verify_admin(...): return _security.verify_admin(...)`），
        存在的唯一理由是按调用时刻现取 app 侧模块级名字后注入。本批把该接缝交给
        `web.security.configure(...)` / `web.services.verify_queue.configure(...)` 登记
        lambda，读者直取真源，故墙上不得再留任何形态的绑定。

        与判据①成对：① 管读侧（路由不得经墙读），⑧ 管写侧（墙上不得再绑定）。
        缺一条就有一半静默——只删读者而留别名，别名仍可被新代码读走。

        **名册必须是"本批实际删掉的每一个名字"**：本判据与判据①按名单做判断，
        名单漏一个就静默一格。由 `test_residual_debt_is_frozen` 从代码侧反查兜底。
        """
        with io.open(_APP_PY, encoding="utf-8") as f:
            bound = app_binding_names(f.read())
        self.assertGreater(len(bound), 100,
                           "web/app.py 模块级绑定面过小——路径或解析器失效（扫空不得当绿）")
        dup = sorted(UNWRAPPED_IN_3C_S3 & bound)
        self.assertEqual(
            dup, [],
            "以下批 3-c S3 已去壳名在 web/app.py 仍有模块级绑定（读者已直取真源，"
            "该名是第二份可漂移的面）：%s" % ", ".join(dup))

    def test_3d_names_have_no_binding_in_app(self):
        """判据⑦：批 3-d 已迁的 39 名在 `web/app.py` 不得再有**任何绑定**。

        口径比判据⑤更严：含 `import` 绑定。这 39 名的目的地只有服务层，
        连"再导出一份同名 import"都不允许——那会让 `web.app.<名>` 重新可用。
        """
        with io.open(_APP_PY, encoding="utf-8") as f:
            bound = app_binding_names(f.read())
        self.assertGreater(len(bound), 100,
                           "web/app.py 模块级绑定面过小——路径或解析器失效（扫空不得当绿）")
        dup = sorted(BATCH3D_NAMES & bound)
        self.assertEqual(
            dup, [],
            "以下批 3-d 已迁名在 web/app.py 仍有模块级绑定：%s" % ", ".join(dup))

    def test_wall_names_are_exactly_permanent(self):
        """判据⑧：墙上剩余名字必须恰好＝永驻 5 名 ＋ `__file__`（不是 ⊆）。

        "⊆" 会让"少了一个永驻名"静默通过；等号把删名与新增名都变成可见事件。
        `__file__` 是 app.py 自身属性，搬不走，故显式并入本断言而不是塞进 B_NAMES。
        """
        remaining = set()
        for fn in _route_files():
            remaining.update(name for name, _line in scan(_read(fn))[0])
        self.assertGreater(len(remaining), 0, "扫到 0 个墙名：扫描器失效")
        self.assertEqual(
            sorted(remaining), sorted(set(B_NAMES) | {"__file__"}),
            "墙上名字与永驻名单不一致：多出的名字要么迁走、要么登记进 B_NAMES；"
            "少掉的名字要有人看见")

    def test_migrated_names_are_not_read_out_of_face(self):
        """判据⑩：**面外载体**不得读 3-d 已迁名。

        为什么需要这一条：`web/routes/*` 不是唯一读者面。`frontend/e2e/server.py`
        用 importlib 载入 `web/app.py` 后按属性取名字（Playwright 的 webServer）。
        批 3-d 删掉 `log_path_for` 后该文件启动即 `AttributeError`，而 pytest 全绿、
        CI 不跑 Playwright —— 属**静默断点**（2026-10-20 独立审查实测）。
        面外载体清单是显式的：清单外新增一个读者不会自动纳入本判据，须登记。
        """
        files = _out_of_face_files()
        self.assertGreaterEqual(
            len(files), _OUT_OF_FACE_MIN_FILES,
            "面外载体扫描面过小（实测 22）：目录改名或过滤失效（扫空不得当绿）")
        hits = []
        for path in files:
            rel = os.path.relpath(path, _REPO_ROOT)
            hits.extend("%s → %s" % (rel, h) for h in _out_of_face_hits(_read_abs(path)))
        self.assertEqual(
            hits, [],
            "面外载体仍在读 3-d 已迁名（启动即 AttributeError）：\n" + "\n".join(hits))
        # 牙口自证：同一扫描器对一段旧形状源码必须命中（否则"扫空即绿"）。
        self.assertEqual(
            _out_of_face_hits("x = webapp.log_path_for('2026-01-01')\n"),
            ["1: webapp.log_path_for"],
            "面外扫描器对已知旧形状不命中：本判据没有牙")
        # 反向牙口：直取服务层真源是正确写法，不得误红。
        self.assertEqual(
            _out_of_face_hits("x = _logs_svc.log_path_for('2026-01-01')\n"), [],
            "面外扫描器把直取真源误判成墙读：本判据会逼人改写对的代码")
        # 属性链基底（`self.webapp.x`）同样要命中——复审实测旧实现只认裸名，漏这种。
        self.assertEqual(
            _out_of_face_hits("x = self.webapp.log_path_for('2026-01-01')\n"),
            ["1: webapp.log_path_for"],
            "面外扫描器对 `self.webapp.<名>` 不命中：属性链形态漏判")

    def test_3d_gate_has_teeth(self):
        """突变验证：把桩**追加到真实 `web/app.py` 源**上，判据⑦ 必须命中。

        只喂一段孤立源码是字符串级自证：名单被换成别的名字时，孤立源码照样命中，
        真实文件却已不受保护（独立审查实测：换一名 ＋ 塞回 import ⇒ 26 passed）。
        故这里拿真源拼接；并覆盖块内绑定（`try` / `except*` / `if` / `with` / `for`）
        与元组赋值目标——只扫 `tree.body` 的旧实现对这几种全部失明。
        """
        real = _read_abs(_APP_PY)
        cases = (
            ("def send_notification(t, c):\n    pass\n", "send_notification"),
            ('write_env_batch = _env_io_svc.write_env_batch\n', "write_env_batch"),
            ("from web.services.logs import log_path_for\n", "log_path_for"),
            ("import web.render as site_image\n", "site_image"),
            ("try:\n    def log_path_for(d=None):\n        pass\n"
             "except Exception:\n    pass\n", "log_path_for"),
            ("try:\n    pass\nexcept* ValueError:\n"
             "    from web.services.logs import parse_sign_log\n", "parse_sign_log"),
            ("if True:\n    from web.services.logs import parse_sign_log\n", "parse_sign_log"),
            ("with open(__file__) as parse_sign_log:\n    pass\n", "parse_sign_log"),
            ("for load_sign_state in ():\n    pass\n", "load_sign_state"),
            ("send_notification, _unused = (None, None)\n", "send_notification"),
            ("*write_env_batch, _rest = ()\n", "write_env_batch"),
        )
        for stub, name in cases:
            with self.subTest(stub=stub.strip().splitlines()[0]):
                self.assertIn(
                    name, app_binding_names(real + "\n" + stub),
                    "把 %r 追加到真实 app.py 后判据⑦ 没命中：本门没有牙" % name)
        src = ("def view():\n"
               "    m = _appmod()\n"
               "    return m.sign_window_bounds()\n")
        self.assertEqual([n for n, _l in a_wall_reads(src)], ["sign_window_bounds"],
                         "`m.<3-d 已迁名>` 未被判据①命中：本门没有牙")

    def test_3c_s3_gate_has_teeth(self):
        """突变验证：塞回一处 S3 已去壳名的定义 ⇒ 判据⑧必须命中。"""
        self.assertIn("verify_admin",
                      app_definition_names("def verify_admin(u, p):\n    pass\n"))
        self.assertIn("VERIFY_BUSY_MSG",
                      app_definition_names('VERIFY_BUSY_MSG = "x"\n'))
        self.assertIn("_is_builtin_admin_session",
                      app_definition_names("def _is_builtin_admin_session():\n    pass\n"))
        self.assertGreaterEqual(len(UNWRAPPED_IN_3C_S3), 15,
                                "S3 已去壳名单过小：名单被截断")
        self.assertEqual(
            app_binding_names("from web import security as _security\n"),
            {"_security"}, "import 绑定被误判：判据⑧口径应与 m.<名> 可达面一致")

    def test_3c_s3_gate_catches_import_as(self):
        """突变验证：`import ... as <已去壳名>` 必须被判据⑧ 命中。

        为什么单独验这个形态：它不是真定义，但让 `web.app.<名>` 重新可用
        （实测 `hasattr(web.app, 'STATUS_ICON')` 从 False 变 True）。真定义口径看不见它。
        """
        for src in ("from yiban.core import status as STATUS_ICON\n",
                    "from yiban.core.status import ICON as STATUS_ICON\n"):
            with self.subTest(src=src.strip()):
                self.assertIn("STATUS_ICON", app_binding_names(src),
                              "import-as 形态未被判据⑧命中：去壳名可经它重新上岗")

    def test_3c_s2_gate_has_teeth(self):
        """突变验证：塞回一处缓存赋值 ⇒ 判据⑥（S2 片）必须命中。"""
        for src in ("_changelog_cache = [None]\n",
                    "_announcement_cache = [None]  # [已发布公告文本]\n",
                    "_announcement_cache: list = [None]\n"):
            with self.subTest(src=src.strip()):
                self.assertIn(src.split(" =")[0].split(":")[0],
                              app_definition_names(src),
                              "塞回旧名赋值未被判据命中：本门没有牙")

    def test_3c_s1_gate_has_teeth(self):
        """突变验证：塞回一处 `STATUS_ICON = yiban_status.ICON` ⇒ 判据⑥必须命中。"""
        self.assertIn("STATUS_ICON",
                      app_definition_names("STATUS_ICON = yiban_status.ICON\n"))
        self.assertIn("_has_line_break",
                      app_definition_names("_has_line_break = env_io.has_line_break\n"))
        self.assertIn("DELETED_RETENTION_DAYS",
                      app_definition_names("DELETED_RETENTION_DAYS = db.NOPE\n"))
        self.assertGreaterEqual(len(UNWRAPPED_IN_3C_S1), 10,
                                "已去壳名单过小：名单被截断")

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


class WallResidualDebtTest(unittest.TestCase):
    """判据⑨：派生债务集——不依赖任何手工名册的兵底。

    **为什么需要这一条**：判据①/⑤/⑥/⑧ 全靠手工名册。名册只能保证"已想到的不回来"，
    保证不了"没想到的进不来"。2026-10-20 独立审查实测抓到 3 个漏登
    （`_admin_session_facts` / `_effective_role` / `run_gated_call`）：把它们的 `def`
    塞回 `web/app.py`，两门仍全绿——而 `verify_admin` 等 14 名会红。同类形状还会再发。

    本判据不枚举名字，而是**从两边各自算出**：
        app 侧模块级真定义  ∩  服务层（web/security.py ＋ web/services/*.py）真定义
    同一名字在两边都有 `def`/`class`/赋值 ⇔ 真源之外还有一份可漂移的定义。
    墙上有定义而服务层也有同名定义，就是双源。

    **允许集**＝仍有意的墙上包装（B_NAMES ∪ ALIAS_NOT_MIGRATED）＋ 冻结的残留债务
    `_WALL_RESIDUAL_DEBT`。债务集**只许缩**：新增一律红。
    """

    def test_residual_debt_is_frozen(self):
        """派生判据：跨层重复定义必须 ⊆ 允许集，且债务集不得变大。"""
        app = _toplevel_definitions(_read_abs(_APP_PY))
        svc = set()
        for path in _service_modules():
            svc |= _toplevel_definitions(_read_abs(path))
        # 批 3-d 删 39 个转发包装后，app.py 模块级真定义实测 82 个
        # （ref=`0423b225`＋批 3-d 收口树，2026-10-20 主会话现量；含复合语句里的模块级绑定）。
        # 下限只保"解析器没失效"，故按实测值留余量；判据本身不是"定义越多越好"。
        self.assertGreater(len(app), 60, "app.py 定义面过小：解析器失效")
        self.assertGreater(len(svc), 50, "服务层定义面过小：扫描面失效")
        allowed = set(B_NAMES) | set(ALIAS_NOT_MIGRATED) | set(_WALL_RESIDUAL_DEBT)
        unexpected = sorted((app & svc) - allowed)
        self.assertEqual(
            unexpected, [],
            "以下名字在 web/app.py 与服务层**同时**有模块级定义（双源），且未登记：%s\n"
            "处置：要么把 web/app.py 里那份删掉（读者直取真源），要么确定它确实必须留在墙上"
            "并加进 B_NAMES；确实迁移不完的，加进 _WALL_RESIDUAL_DEBT 并写明理由。"
            % ", ".join(unexpected))

    def test_residual_debt_shrinks_monotonically(self):
        """债务集长度不得回升：删掉的条目不得加回来。"""
        self.assertLessEqual(
            len(_WALL_RESIDUAL_DEBT), _WALL_RESIDUAL_DEBT_MAX,
            "残留债务集变大了（%d > %d）：本集只许缩。新增的双源定义应直接删掉，"
            "而不是登记成债务。" % (len(_WALL_RESIDUAL_DEBT), _WALL_RESIDUAL_DEBT_MAX))

    def test_residual_debt_entries_are_really_duplicated(self):
        """债务集的每一项必须仍真是双源。已不存在的项要删——只可缩。"""
        app = _toplevel_definitions(_read_abs(_APP_PY))
        svc = set()
        for path in _service_modules():
            svc |= _toplevel_definitions(_read_abs(path))
        stale = sorted(n for n in _WALL_RESIDUAL_DEBT if n not in (app & svc))
        self.assertEqual(
            stale, [],
            "以下残留债务项已经不构成双源（%s）：删掉它们，本集只可缩。"
            % ", ".join(stale))

    def test_derived_gate_has_teeth(self):
        """突变验证：把本批漏登的 3 名 def 塞回 app.py ⇒ 派生判据必须红。"""
        base = _read_abs(_APP_PY)
        stubs = (
            "\n\ndef _admin_session_facts():\n    return (1, \"\")\n"
            "\n\ndef _effective_role(username, pw_version=None):\n    return None\n"
            "\n\ndef run_gated_call(action, username, limits):\n    return None\n"
        )
        app = _toplevel_definitions(base + stubs)
        svc = set()
        for path in _service_modules():
            svc |= _toplevel_definitions(_read_abs(path))
        allowed = set(B_NAMES) | set(ALIAS_NOT_MIGRATED) | set(_WALL_RESIDUAL_DEBT)
        unexpected = (app & svc) - allowed
        for name in ("_admin_session_facts", "_effective_role", "run_gated_call"):
            self.assertIn(
                name, unexpected,
                "漏登名 %s 塞回后派生判据未命中：这条兜底没有牙" % name)


class RouteScanRecursionTest(unittest.TestCase):
    """判据⑩：路由扫描必须递归，与 `test_web_boundary` 同口径。

    为什么单独钉：本门原用 `os.listdir` 只看一层，而同批 `test_web_boundary.py`
    用 `os.walk`。子目录里的 `m.*` 读取完全不被拆墙门看见（实测：放
    `web/routes/sub/extra.py` 里一句 `m.load_accounts()`，本门仍绿）。
    两门扫描面口径不一致＝同一处泄漏在一门里红、在另一门里静默绿（§29）。
    """

    def test_route_scan_is_recursive(self):
        """扫描面必须递归：构造子目录读取器，验证命中。"""
        self.assertTrue(hasattr(os, "walk"))
        files = _route_files()
        self.assertGreaterEqual(len(files), _MIN_FILES,
                                "路由扫描面过小（%d）：路径或解析器失效" % len(files))
        # 扫描面必须能看见子目录——用临时子目录实测，不靠读代码断言。
        tmp = tempfile.mkdtemp(prefix="routescan-")
        try:
            sub = os.path.join(tmp, "sub")
            os.makedirs(sub)
            with io.open(os.path.join(sub, "extra.py"), "w", encoding="utf-8") as f:
                f.write("def view():\n    m = _appmod()\n    return m.load_accounts()\n")
            found = _route_files_in(tmp)
            self.assertEqual(found, [os.path.join("sub", "extra.py")],
                             "路由扫描不递归：子目录里的读取完全不被看见")
            self.assertTrue(
                a_wall_reads(_read_abs(os.path.join(sub, "extra.py"))),
                "子目录读取器未被扫描器命中")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_scan_face_matches_test_web_boundary_face(self):
        """两门的路由扫描面口径必须一致（同一 leak 不得在一门里绿）。"""
        self.assertTrue(os.path.isdir(_ROUTES_DIR))
        recursive_count = len(_route_files())
        flat_count = len([fn for fn in os.listdir(_ROUTES_DIR)
                          if fn.endswith(".py") and fn != "__init__.py"])
        self.assertEqual(
            recursive_count, flat_count,
            "当前路由目录没有子包（%d 递归 vs %d 平铺）——但扫描器必须递归，"
            "否则新建子包即静默失效" % (recursive_count, flat_count))


class HookRegistrationContractTest(unittest.TestCase):
    """契约：宿主注入登记点（`_hooks`）的已知行为，写成断言而不是只活在注释里。

    **为什么需要这个类**：批 3-c S3 引入了模块级 `_hooks`（`web/security.py` /
    `web/services/verify_queue.py`），它是一块**原来不存在的进程级状态**。它的语义：

      · 单份 `web/app.py`：打得上的桩经 `_hooks` 在调用时刻生效（本批要保的性质）；
      · **同进程第二份 `app.py` 副本载入后**：`_hooks` 改归后一份，先建副本上的桩
        **静默失效**（读到后建副本的真 `.env`）。

    第二句是本节要钉住的已知行为。它为何可达：仓内多个测试文件用
    `spec_from_file_location` 加载 `web/app.py` 成独立模块。当前编排下每个
    `setUpClass` 建完副本立刻用完、类间不交错，所以实测失效数为 0
    （口径：全量 `-n 4 --dist loadfile` ＋ 逐例探针，2026-10-20）。
    但"现在没撞上"不等于"结构上不可能"——下一个人写多副本用例时会以
    “桩生效”为前提。**本类把该前提写成可执行的约束**。

    **多副本用例的约束**：若你的用例需要在持有副本上打桩，必须在自己的例内
    重新调用 `configure(...)`，不能假定 `setUpClass` 那次登记仍有效。
    """

    _FEATURES: ClassVar = [
        ("web.security", ("env_file", "read_env", "write_env_key",
                          "count_env_key_lines", "send_notification")),
        ("web.services.verify_queue", ("env_file", "read_env", "seat",
                                       "attempt_allowed", "verify_clean")),
    ]

    def test_every_hook_is_registered_after_app_import(self):
        """导入 `web.app` 后，两个登记点的每一个键都必须已登记。

        漏登一个＝该依赖调用时抛 `RuntimeError`（fail-closed），
        属“忘登记”而不是“静谧降级”。本断言把它变成启动即错。
        """
        import importlib

        importlib.import_module("web.app")
        for mod_name, keys in self._FEATURES:
            mod = importlib.import_module(mod_name)
            missing = sorted(k for k in keys if k not in mod._hooks)
            self.assertEqual(
                missing, [],
                "%s 未登记以下宿主依赖：%s（读者调用时会抛 RuntimeError）"
                % (mod_name, ", ".join(missing)))

    def test_hooks_bind_to_the_last_loaded_app_copy(self):
        """已登记契约：同进程载入第二份 `app.py` 副本后，`_hooks` 归后一份。

        这不是期望行为，而是**已知行为**；写成断言是为了让改动它的人看见代价。
        若日后改成 per-instance 登记（挂到 `app.extensions`），本用例会红——
        那时应连同本类 docstring 一起更新，而不是删掉断言。
        """
        import sys

        app_py = _APP_PY
        first = _load_app_copy("wallhook_first", app_py)
        import web.security as _sec

        self.assertIs(_sec._host("env_file"), first.ENV_FILE,
                      "单副本时 _hooks 应归它")
        second = _load_app_copy("wallhook_second", app_py)
        try:
            self.assertIs(_sec._host("env_file"), second.ENV_FILE,
                          "已登记契约变了：_hooks 不再归最后载入的副本。"
                          "若这是有意的（改成 per-instance 登记），请同批更新"
                          "HookRegistrationContractTest 的 docstring。")
        finally:
            # 复原：必须指回**真模块**，不能指回任一副本。
            # 副本只在本用例内存在；指向副本会让同 worker 的后续用例读到副本的模块级
            # 状态（它们改的是 `web.app.ENV_FILE` 一类），而登记在副本上 ⇒ 改不动。
            # 独立审查实测：整改前残留 2 个登记点、整改后若指回副本会放大到 11 个。
            import web.app as _real_app
            _reconfigure_to(_real_app)
            sys.modules.pop("wallhook_first", None)
            sys.modules.pop("wallhook_second", None)


def _load_app_copy(tag, app_py):
    """以独立别名加载一份 `web/app.py`（与既有 Web 测试同形）。"""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(tag, app_py)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[tag] = mod
    spec.loader.exec_module(mod)
    return mod


def _reconfigure_to(app_mod):
    """把**全部**登记点重新指回给定副本（测试收尾用）。

    批 3-d 起登记点由 2 个扩到 11 个模块，故本处不再逐模块抄一份参数表——
    那会是登记契约的第二份定义、且必然漂移。副本自带 `_register_host_hooks()`，
    调用它即按该副本的模块级名字重登记（登记的是 lambda，解析仍在调用时刻）。
    """
    app_mod._register_host_hooks()


if __name__ == "__main__":
    unittest.main()
