#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""易班自动签到网页管理系统（服务器端）。

网页管理后台：管理员登录后，可在任意设备（手机/平板/电脑）
查看和管理签到任务。功能：

- 账号管理：列表 / 添加 / 编辑 / 删除 / 排序（决定顺序打卡顺序）
- 签到日志：解析 sign.log 展示最近记录与今日各账号状态图标
- 手动签到：单账号后台执行 scripts/signin.py --only
- 系统设置：随机延迟开关（写入 .env）、连通性检测、服务器时间/签到窗口状态

运行：
    python3 -m web                 # 默认 127.0.0.1:17892（仅回环；生产用 systemd/gunicorn 模板）
    python3 -m web --port 9000     # 自定义端口

管理员账号：首次启动自动生成 SECRET_KEY 并写入 .env；
在 .env 配置 YIBAN_ADMIN_USER / YIBAN_ADMIN_PASSWORD 后即可登录。
"""

import argparse
import calendar  # noqa: F401  # 月历路由已入 web/routes/my.py，此处仅为保持 web.app 名字面不变
import contextlib
import html  # noqa: F401  # 文档页转义已入 web/render.py，此处仅为保持 web.app 名字面不变
import json  # noqa: F401  # 通道健康日报已入 web/services/channel_health.py，此处仅为保持 web.app 名字面不变
import logging
import os
import re
import secrets
import sqlite3  # noqa: F401  # 个人/数据域路由已迁出，此处仅为保持 web.app 名字面不变
import subprocess  # noqa: F401  # 手动签到子进程族已入 web/services/manual_sign.py，保留名字面（测试打桩 webapp.subprocess.Popen）
import sys
import threading
import time

# 时间构造已随账号数据 / 日志 / 实测族迁出；测试仍以 `webapp.datetime` 替换 web 侧时钟
from datetime import datetime, timedelta  # noqa: F401  # 名字面零损失

import requests  # noqa: F401  # 连通性检测已入 web/services/signstatus.py，此处仅为保持 web.app 名字面不变
from flask import (
    Flask,
    Response,  # noqa: F401  # 日志导出已入 web/routes/data.py，此处仅为保持 web.app 名字面不变
    abort,
    jsonify,
    redirect,  # noqa: F401  # 页面路由已入 web/routes/pages.py，此处仅为保持 web.app 的导入面不变
    render_template,
    request,
    send_file,  # noqa: F401  # 日志导出已入 web/routes/data.py，保留 web.app.<名字> 可 import（打桩面零损失）
    session,
    url_for,
)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: F401

# 共享模块（web/ 与 scripts/ 同级）：加密模块 + SQLite 数据访问层 + 子进程环境构造
# **必须排在下面的 yiban.* 导入之前**：yiban 包在仓库根（scripts/ 下的 locks 等又被它
# 依赖），两者都要先入 sys.path 才能导入。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
for _p in (_SCRIPTS_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# scripts/ 下的裸模块（同属"共享模块"段）：三者本模块均无自用点，保留供 web.app.<名字>
# 取用（routes 经 m.* 取用、测试按属性打桩）。位置随包导入引导之后、web./yiban. 之前。
import child_env  # noqa: E402,F401  # 签到子进程环境注入已入 web/routes/signin_api.py
import email_policy  # noqa: E402,F401  # 域名审查实现已入 web/render.py
import signin  # noqa: E402,F401  # 探针/注册验证与容量公式已随域迁出

# 渲染层实现（web/render.py）：合规文档与站点展示族的真源。本模块只转发，并在转发时
# 注入本模块持有的模块级状态（`_REPO_ROOT` / `_doc_cache` / `ENV_FILE` / `read_env`）
# ——它们会被测试改写、也会随运行方式变化，必须调用时刻现取。
from web import render as _render  # noqa: E402

# 服务层实现（web/services/）：env 读写与执行体清单的真源。本模块只保留名字面，
# 需要本模块模块级状态的入口（_atomic_write / write_env_batch / _env_flag / 设置缺省值 /
# send_notification / ENV_FILE / _sign_window）在下方转发时调用时刻现取后注入——它们会被
# 测试改写、也会随运行方式变化，服务层另持一份绑定会让打桩与 --config 静默失效。
# 安全域实现（web/security.py）同理：它在本模块持有的模块级名字（ENV_FILE / read_env /
# write_env_key / write_env_batch / load_env_int / _count_env_key_lines /
# send_notification / SESSION_ABS_TTL_SECONDS / _constant_time_dummy）在下方转发包装里
# 调用时刻现取后注入，另持一份绑定会让打桩与 --env-file 静默失效。
from web import security as _security  # noqa: E402
from web.render import (  # noqa: E402
    # 名字面零损失：web.app.<名字> 仍可 import（routes 经 m.* 取用）
    _DOC_FILES,  # noqa: F401
    _LINK_RE,  # noqa: F401
    _SAFE_LINK_SCHEMES,  # noqa: F401
    _inline_md,  # noqa: F401
    _render_md,  # noqa: F401
)
from web.routes import (  # noqa: E402
    register_all,  # 路由分域装配（各域在 web/routes/）
    # 账号验证冷却与配额：取用点已收在 web.routes，两条提交路径均已随域迁出，
    # 此处保留仅为 web.app 名字面零损失（打桩面）
    verify_fails,  # noqa: F401
    verify_limits,  # noqa: F401
)
from web.routes.pages import NO_STORE_PAGES  # noqa: E402  # 页面禁缓存清单（页面路径唯一登记点）

# 安全域（web/security.py）：纯再导出保持 web.app.<名字> 可达（routes 经 m.* 取用、
# 测试按属性读取或替换）。需要本模块模块级状态的入口（`.env` 路径与读取器、写路径、
# 整数读取器、键行计数、告警出口、会话绝对期）在下方转发包装里调用时刻现取后注入。
from web.security import (  # noqa: E402
    _DEFAULT_ADMIN_LITERALS,  # noqa: F401
    _IP_STORE_LIMIT,  # noqa: F401
    _IP_STORE_MAX_AGE,
    _REPLACE_RETRY_ATTEMPTS,  # noqa: F401
    _REPLACE_RETRY_BASE_SEC,  # noqa: F401
    ADMIN_SID_ENV_KEY,  # noqa: F401
    LOGIN_LOCK_SECONDS,  # noqa: F401
    PW_CONFIRM_COOLDOWN_DEFAULT,  # noqa: F401
    PW_CONFIRM_TTL_DEFAULT,  # noqa: F401
    PW_CONFIRM_TTL_MAX,  # noqa: F401
    SCRYPT_METHOD,  # noqa: F401
    TRUSTED_PROXIES,
    VERIFY_FAIL_AUTH_KEYWORDS,  # noqa: F401
    VERIFY_FAIL_COOLDOWN,  # noqa: F401
    VERIFY_FAIL_MAX,  # noqa: F401
    VERIFY_FAIL_WINDOW,  # noqa: F401
    VERIFY_MAX,  # noqa: F401
    VERIFY_WINDOW,  # noqa: F401
    _atomic_write,
    _bump_login_failure,  # noqa: F401
    _bump_window_count,
    _client_ip,
    _constant_time_dummy,
    _ip_store_trim,
    _new_admin_sid,  # noqa: F401
    _record_verify_failure,
    _replace_with_retry,  # noqa: F401
    _verify_attempt_allowed,
    _verify_fail_cooldown_remaining,  # noqa: F401
)
from web.services import accounts_data as _accounts_data  # noqa: E402
from web.services import capacity as _capacity  # noqa: E402
from web.services import channel_health as _channel_health  # noqa: E402
from web.services import env_io as _env_io_svc  # noqa: E402
from web.services import executor_env as _executor_env  # noqa: E402
from web.services import logs as _logs_svc  # noqa: E402
from web.services import manual_sign as _manual_sign  # noqa: E402
from web.services import measure as _measure  # noqa: E402
from web.services import notify_mail as _notify_mail  # noqa: E402
from web.services import signstatus as _signstatus  # noqa: E402
from web.services import verify_queue as _verify_queue  # noqa: E402

# 名字面零损失：以下再导出的 web.app.<名字> 仍可 import（routes 经 m.* 取用、
# 测试按属性读取或替换）。
from web.services.accounts_data import (  # noqa: E402
    # 账号审核态词表、手机号正则、注销宽限期与口令策略常量随账号数据族搬入
    # web/services/accounts_data.py，此处取回保 m.* 名字面
    _PASSWORD_CLASS_HINT,  # noqa: F401
    _PASSWORD_CLASS_LABELS,  # noqa: F401
    _PASSWORD_CLASS_PATTERNS,  # noqa: F401
    _PASSWORD_MIN_CLASSES,  # noqa: F401
    _PASSWORD_POLICY_HINT,  # noqa: F401
    ACCOUNT_STATUS_ACTIVE,  # noqa: F401
    ACCOUNT_STATUS_PENDING,  # noqa: F401
    ACCOUNT_STATUS_REJECTED,  # noqa: F401
    ADMIN_PASSWORD_MIN_CLASSES,  # noqa: F401
    ADMIN_PASSWORD_MIN_LEN,  # noqa: F401
    DELETE_GRACE_DAYS,  # noqa: F401
    PASSWORD_MIN_LEN,  # noqa: F401
    PHONE_RE,  # noqa: F401
    _admin_password_policy_error,  # noqa: F401
    _as_signin_account,  # noqa: F401
    _delete_grace_remaining,  # noqa: F401
    _duplicate_phone_error,  # noqa: F401
    _mask_email,  # noqa: F401
    _owner_display_of,  # noqa: F401
    _owner_has_other_live,  # noqa: F401
    _password_policy_error,  # noqa: F401
    _stale_idx_guard,  # noqa: F401
    _verify_account_clean,
    find_account_index,  # noqa: F401
    load_accounts,
    load_accounts_raw,  # noqa: F401
    load_users,  # noqa: F401
    mask_account,  # noqa: F401
    validate_account,  # noqa: F401
)
from web.services.capacity import (  # noqa: E402
    # 名字面零损失：容量族常量与节流状态随族搬入 web/services/capacity.py
    # （routes 经 m.* 取用、测试按属性读取 `_mail_alert_ts` 复位节流），此处再导出
    DEFAULT_MAIL_ALERT_COOLDOWN,  # noqa: F401
    _capacity_account_count,  # noqa: F401
    _capacity_alerts,  # noqa: F401
    _capacity_audit_count,  # noqa: F401
    _mail_alert_lock,  # noqa: F401
    _mail_alert_ts,  # noqa: F401
)
from web.services.channel_health import (  # noqa: E402
    # 名字面零损失：通道健康族的纯逻辑与常量（routes/测试按属性读取日报标记键）
    _HEALTH_REPORT_META_KEY,  # noqa: F401
    _audit_channel_health_degraded,  # noqa: F401
    _channel_health_degraded,  # noqa: F401
    _channel_health_facts,  # noqa: F401
    _daily_budget_desc,  # noqa: F401
    _health_report_sent_today,  # noqa: F401
)
from web.services.env_io import (  # noqa: E402
    # 名字面零损失：web.app.<名字> 仍可 import（routes 经 m.* 取用）
    _BOOL_SETTINGS_KEYS,  # noqa: F401
    _SETTINGS_KEY_LABELS,  # noqa: F401
    ANNOUNCEMENT_DRAFT_META_FMT,  # noqa: F401
    ANNOUNCEMENT_DRAFT_META_SEP,  # noqa: F401
    _env_write_lock,  # noqa: F401
    _is_http_proxy_url,  # noqa: F401
    _parse_announcement_meta,  # noqa: F401
    _settings_label,  # noqa: F401
    _settings_value_text,  # noqa: F401
    load_env_int,
    read_env,
)
from web.services.executor_env import (  # noqa: E402
    # 名字面零损失：执行体清单及其 .env 键已入 web/services/executor_env.py，
    # 本模块已无自用点，保留为 web.app.<名字> 的兼容面（routes 经 m.* 取用）
    _executor_activity,  # noqa: F401
    _executor_row_payload,  # noqa: F401
    _last_executors,  # noqa: F401
    _next_executor_slot,  # noqa: F401
    _validated_name,  # noqa: F401
    _validated_proxy_value,  # noqa: F401
)
from web.services.locks import (  # noqa: E402
    # 进程内锁真源：`_file_lock`（账号/用户读改写）与 `_rate_lock`（限速/失败计数表）
    # 都与 m.<名字> 是同一把，供路由取用
    _file_lock,  # noqa: F401
    _rate_lock,
)
from web.services.logs import (  # noqa: E402
    # 名字面零损失：web.app.<名字> 仍可 import（routes 经 m.* 取用）
    _LOG_TAIL_BYTES,  # noqa: F401
    SIGN_LOG_RE,  # noqa: F401
    _cred_paused_phones,  # noqa: F401
    _is_valid_date_str,  # noqa: F401
    _log_line_visible,  # noqa: F401
    _mask_log_phones,  # noqa: F401
    _most_recent_log_cache,  # noqa: F401
    _tail_lines,
    clear_fuse_on_cred_change,  # noqa: F401
    clear_fuse_pause,  # noqa: F401
)
from web.services.manual_sign import (  # noqa: E402
    # 名字面零损失：手动签到子进程族（等待回收、队列超时缩放、退出码词表）随族搬入
    # web/services/manual_sign.py；只有 `_log_manual_sign_exit` 需注入本模块的
    # `log_path_for`，故在下方转发
    _SIGNIN_EXIT_REASONS,  # noqa: F401
    _batch_wait_timeout,  # noqa: F401
    _manual_sign_failure_reason,  # noqa: F401
    _wait_signin_proc,  # noqa: F401
)
from web.services.measure import (  # noqa: E402
    # 名字面零损失：现场实测的状态文件与冷却判定已入 web/services/measure.py，
    # 保留 web.app.<名字> 的兼容面；
    # `_measure_state_path` / `_write_measure_state` 另被本模块的转发包装注入
    MEASURE_STATE_FILE,  # noqa: F401
    _measure_cooldown_remaining,  # noqa: F401
    _pick_measure_account,  # noqa: F401
    _read_measure_state,  # noqa: F401
)
from web.services.notify_mail import (  # noqa: E402
    # 名字面零损失：通知与告警邮件族随族搬入 web/services/notify_mail.py
    # （`_nl_safe` / `_audit_actor` / `_audit_alert_facts` 本模块自用，其余为 m.* 兼容面）；
    # `send_notification` 与 `_push_ever_configured` 需注入本模块的名字，故在下方转发
    _MAIL_FLAG_NAMES,  # noqa: F401
    _NOTIFY_LEDGER_LABELS,  # noqa: F401
    _PUSH_CONFIG_ENV_KEYS,  # noqa: F401
    _alert_mail_recipients,  # noqa: F401
    _audit_actor,
    _audit_alert_facts,
    _change_mail,  # noqa: F401
    _exhaustion_notice_mail,  # noqa: F401
    _last_cleanup_text,  # noqa: F401
    _mail_flags_desc,  # noqa: F401
    _nl_safe,
    _notify_change_desc,  # noqa: F401
    _review_reject_mail,  # noqa: F401
)
from web.services.signstatus import (  # noqa: E402
    # 名字面零损失：签到窗口/运行时段判定与系统信息已入 web/services/signstatus.py，
    # 保留 web.app.<名字> 的兼容面；
    # `_day_off_reason` / `_env_flag` / `_in_sign_window` 另被本模块的转发包装注入
    _TRUTHY_LITERALS,  # noqa: F401
    _day_off_reason,
    _env_flag,
    _in_sign_window,
    check_connectivity,  # noqa: F401
)
from web.services.verify_queue import (  # noqa: E402
    # 名字面零损失：在线校验的执行闸门、异步任务与失败落库已入
    # web/services/verify_queue.py，保留 web.app.<名字> 的兼容面；
    # `run_verify_with_gate` / `verify_async_enabled` / `_account_verify_enabled`
    # 另被本模块的转发包装注入（席位、配额判定、只读验证、ENV_FILE 与 read_env）
    VERIFY_JOBS_MAX_PENDING,  # noqa: F401
    VerifyGateBusy,  # noqa: F401
    VerifyQuotaExceeded,  # noqa: F401
    _reclaim_stale_verify_jobs,
    _reject_account,
    _start_verify_job,  # noqa: F401
    _verify_queue_full,
)
from yiban import __version__ as APP_VERSION  # noqa: E402  # 版本唯一来源：yiban/__init__.py

# cred_state 已无自用点，保留供 web.app.<名字> 取用（须在引导之后导入）
from yiban import clock, cred_state  # noqa: E402,F401

# 窗口唯一口径 `yiban.window`：`sign_window_bounds` 用它把起止与前后裁剪折成有效窗口
from yiban import window as yb_window  # noqa: E402
from yiban.attempt import jobs as verify_jobs  # noqa: E402
from yiban.logging_ext import DailyFlockFileHandler  # noqa: E402
from yiban.masking import mask_phone as _mask_phone  # noqa: E402
from yiban.masking import mask_url_userinfo as _mask_url_userinfo  # noqa: E402,F401  # 代理脱敏

# 名字面零损失：纯再导出见上方 import；需要注入本模块模块级状态的入口
# （_read_doc_html / _doc_page / 站点展示族）在下方转发。


# 合规文档渲染缓存：登录页为公开高频入口，每次请求读盘+全量正则渲染会放大 I/O 与 DoS 面。
# 按 (mtime_ns, size) 缓存，部署者更新文件后自动失效；转发的 `_read_doc_html` 在本模块
# 调用时刻现取本字典，故测试清空/读取 `web.app._doc_cache` 仍是同一份。
_doc_cache = {}  # filename -> ((mtime_ns, size), html)


def _read_doc_html(filename):
    """读取并渲染合规文档（实现见 web/render.py）；注入本模块现取的文档根与渲染缓存。"""
    return _render._read_doc_html(filename, _REPO_ROOT, _doc_cache)


def _doc_page(title, body_html, icp_text="", police_text="", base_path="", police_link="https://beian.mps.gov.cn/", description=""):
    """合规文档独立页（实现见 web/render.py）；摘要留空时取本模块现读的站点简介默认文案。"""
    return _render._doc_page(title, body_html, icp_text, police_text, base_path, police_link,
                             description or site_description())


# 告警两条通道的实现都在包内：A 线管理员邮件（SMTP，零依赖；不配置则不启用）
# 与 Webhook 推送（Server酱/自定义 URL，加密配置 + 节流 + 响应检查）。
from yiban import egress as yb_egress  # noqa: E402  # 出口（代理）分配：唯一口径

# 两条通道的读配置/取走标记已随通知族迁出（web/services/notify_mail.py），
# 保留 web.app.mailer / web.app.notify 名字面（两者都是测试的打桩点）
from yiban import mail as mailer  # noqa: E402,F401
from yiban import notify  # noqa: E402,F401
from yiban import status as yiban_status  # noqa: E402  # 状态词汇表唯一事实源

# 周末门/暂停门与易班端点：实现已入 web/services/signstatus.py，保留供 web.app.<名字> 取用
from yiban.engine import schedule as yb_schedule  # noqa: E402,F401
from yiban.fyiban.protocol import API_AUTH_URL  # noqa: E402,F401
from yiban.infra import (  # noqa: E402
    account_crypto,  # noqa: F401  # 本模块已无自用点，保留：web.app.<名字> 仍可 import（打桩面零损失）
    env_io,
    env_lock,  # noqa: F401  # 跨进程写锁真源（写路径已入 web/services/env_io.py），保留名字面
)
from yiban.mail import (  # noqa: E402
    config as mail_config,  # noqa: F401  # 无自用点，保留供 web.app.<名字> import
)
from yiban.mail import layout as mail_layout  # noqa: E402  # 正文排版层（三出口）
from yiban.store import db  # noqa: E402  # SQLite 数据访问层（实现已入包，此即唯一出处）

#: 并行执行体槽位的最大下标（`YIBAN_WORKERS` 旧口径 1~64 → 下标 0~63）。
#: 唯一口径在 `yiban.egress.SLOT_MAX`（清单模型的槽位上限），此处只是别名：
#: 单槽位出口写接口按"下标 + 当前执行体数"两重判定拒绝未被使用的槽位。
EXECUTOR_INDEX_MAX = yb_egress.SLOT_MAX

# 默认路径（与 run.sh 保持一致，可用参数覆盖）
ACCOUNTS_DEFAULT = os.environ.get("YIBAN_ACCOUNTS_FILE", "accounts.json")
# 按日状态文件目录（signin.py 写入 sign-daily-YYYY-MM-DD.json，网页日历读取）
# 路径必须读 .env（环境变量优先）——只认 os.environ 时"写进 .env"对 web 进程无效，
# 会静默落到 /var/log/yiban；同机第二份部署因此与第一份共用状态目录与磁盘外锚点。
STATE_DIR_DEFAULT = env_io.resolve_path("YIBAN_STATE_DIR", "/var/log/yiban")
LOG_DEFAULT = env_io.resolve_path("YIBAN_LOG_FILE", "/var/log/yiban/sign.log")
ENV_DEFAULT = os.environ.get("YIBAN_ENV_FILE", ".env")
DB_DEFAULT = os.environ.get("YIBAN_DB_FILE", "yiban.db")
# 模块级路径（gunicorn 走 create_app() 不执行 main()，需在此初始化；main() 用 --config 等参数覆盖）
ACCOUNTS_FILE = ACCOUNTS_DEFAULT  # 仅作 JSON→SQLite 自动迁移来源（迁移后改名 .bak；users.json 同目录推断，无需单独路径）
LOG_FILE = LOG_DEFAULT
ENV_FILE = ENV_DEFAULT
STATE_DIR = STATE_DIR_DEFAULT
DB_FILE = DB_DEFAULT

# 普通用户账号的审核状态（原 STATUS_PENDING/ACTIVE/REJECTED 与签到状态码 STATUS_* 同名
# 异义（历史遗留），改名为 ACCOUNT_STATUS_* 彻底分离命名空间）实现见
# web/services/accounts_data.py，此处以导入区再导出保持 m.ACCOUNT_STATUS_* 可达。

# 软删除保留期（天）：管理员删除的账号进入待删除状态，超期自动彻底清除。
# 唯一来源在 db.py（SOFT_DELETE_RETENTION_DAYS），此处仅引用防双源漂移
DELETED_RETENTION_DAYS = db.SOFT_DELETE_RETENTION_DAYS

# 密码策略（至少 10 位且包含大写/小写/数字/符号中至少两类，只对新建/修改生效）及其
# 主管理员单独提档的两档下限实现见 web/services/accounts_data.py，此处以导入区再导出
# 保持 m.PASSWORD_MIN_LEN / m._PASSWORD_* / m.ADMIN_PASSWORD_MIN_* 可达。
# 口令哈希算法（werkzeug scrypt，OWASP 推荐参数；check_password_hash 对旧哈希自动兼容）
# 已随安全域搬入 web/security.py，此处以导入区再导出保持 m.SCRYPT_METHOD 可达。

# 账号编辑时识别码清空哨兵值（收到该值 = 显式删除设备识别码字段）
CLEAR_SENTINEL = "__clear__"

# 单次批量操作上限（2026-08-29 由 100 收紧为 10）：批量通过/删除/设管理员/重置密码
# 与「清除已注销用户」共用同一上限——被盗管理员会话即使一个请求，一次最多影响 10 条，
# 降低误操作与滥用影响范围。三处接口共用本常量，防单处调整后其他路径遗漏。
BATCH_OP_LIMIT = 10


def _sign_window():
    """签到窗口（`.env` 覆盖 YIBAN_SIGN_START/END，非法回退默认，实现见 web/services/signstatus.py）。

    `.env` 路径与读取器按调用时刻现取本模块的（测试与 `--config` 都改写 `ENV_FILE`）。
    """
    return _signstatus._sign_window(ENV_FILE, read_env)


def _in_run_period(bounds, now=None):
    """当前是否落在**本应运行**的时段内＝有效窗口内 且 今天没被门挡下。

    门与钟点判定的口径见 web/services/signstatus.py；两个判定都按调用时刻现取本模块的
    （`_in_sign_window` 与 `_day_off_reason` 在既有测试中被直接打桩）。
    """
    return _signstatus._in_run_period(bounds, _in_sign_window, _day_off_reason, now)


def _executors_window():
    """执行体接口口径的**有效签到窗口**（实现见 web/services/executor_env.py）。

    `.env` 路径与窗口解析器按调用时刻现取本模块的（测试与 `--config` 都改写 `ENV_FILE`）。
    """
    return _executor_env._executors_window(ENV_FILE, _sign_window)


# ---------------------------------------------------------------------------
# 现场实测单账号耗时（仅主管理员；**会真实访问易班一次**）
# ---------------------------------------------------------------------------
#: 两次实测之间的全局冷却（秒）；可被 `.env` 的 YIBAN_MEASURE_COOLDOWN 覆盖。
MEASURE_COOLDOWN_SEC = 600
# 实测状态文件的路径/读写/冷却判定与可实测账号的挑选实现见 web/services/measure.py；
# 本模块的 `_measure_state_path` 与 `_write_measure_state` 转发时现取本模块的
# STATE_DIR 与 _atomic_write（测试会赋值 `web.app.STATE_DIR`、在 `web.app` 上打桩
# `_atomic_write`），其余三个名字以导入区再导出保持 m.* 可达。
def _measure_state_path():
    """实测冷却状态文件路径（实现见 web/services/measure.py）。"""
    return _measure._measure_state_path(STATE_DIR)


def _write_measure_state(path, payload):
    """原子写实测状态（实现见 web/services/measure.py）；落盘经本模块的 `_atomic_write`。"""
    return _measure._write_measure_state(path, payload, _atomic_write)

# 登录时延拉平（`_constant_time_dummy` 与其占位哈希缓存 `_dummy_pw_hash`）实现见
# web/security.py，此处以导入区再导出保持 m._constant_time_dummy 可达（登录、恢复、
# 注册等路径经 m.* 取用；本模块的 verify_admin 转发包装在服务层内部按同一函数取用）。

# 可信第一跳代理清单（`TRUSTED_PROXIES`）与真实客户端出口 `_client_ip` 实现见
# web/security.py，此处以导入区再导出保持 m.* 可达。


# 防错位校验（`_stale_idx_guard`）实现见 web/services/accounts_data.py，此处以导入区
# 再导出保持 m.* 可达。


def _json_body():
    """安全解析 JSON 请求体：
    - 空 body → {}
    - 非法 JSON / 非对象 JSON → 400（API 语义清晰，不静默按空请求处理）
    """
    if not request.data:
        return {}
    data = request.get_json(silent=True)
    if data is None:
        abort(400, description="请求体不是合法 JSON")
    if not isinstance(data, dict):
        abort(400, description="请求体应为 JSON 对象")
    return data


# 真实客户端出口（`_client_ip`）见 web/security.py，此处以导入区再导出保持 m.* 可达。


# 注销冷却剩余（`_delete_grace_remaining`）与注销宽限期常量（DELETE_GRACE_DAYS）见
# web/services/accounts_data.py，此处以导入区再导出保持 m.* 可达。

# 随机延迟默认上限（与 signin.py 一致）
DEFAULT_START_DELAY_MAX = 60
DEFAULT_ACCOUNT_GAP_MAX = 10

# 登录失败限速：同一 IP 连续失败超过阈值后锁定（锁定秒数 LOGIN_LOCK_SECONDS 随安全域
# 搬入 web/security.py，此处以导入区再导出保持 m.LOGIN_LOCK_SECONDS 可达）
LOGIN_MAX_FAILS = 5
# 账号恢复的每 IP 聚合失败窗口（跨邮箱喷洒防护——单邮箱 5 次锁定
# 只约束单账号，攻击者可换邮箱继续；命中恢复即接管该账号与其易班凭据）
RESTORE_FAIL_MAX = 30
RESTORE_FAIL_WINDOW = 600
# 连续失败告警阈值：达到后通过 YIBAN_NOTIFY_URL 通知管理员（每轮锁定只告警一次）
LOGIN_FAIL_NOTIFY = 3
# 敏感操作口令复核失败的独立计数窗口（秒，M5）：与登录计数分离，
# 只用于告警与冷却判定，不锁管理员（P18）。
SENSITIVE_PW_FAIL_WINDOW = 900
# 敏感口令门禁的两个默认窗口（.env 可覆盖，唯一解析处见 web/security.py 的
# _sensitive_gate_params）：PW_CONFIRM_TTL_DEFAULT 是豁免窗口（本会话在 TTL 秒内
# 复核过口令、且出口 IP 未变 → 配置类动作免再输口令），PW_CONFIRM_TTL_MAX 是硬钳
# （豁免窗口比会话本身还长就等于取消门禁）；PW_CONFIRM_COOLDOWN_DEFAULT 是独立计数
# 达阈值后**一切需要复核的写操作**（含正确口令）一律拒绝的时长。三个常量本体随安全
# 域搬入 web/security.py，此处以导入区再导出保持 m.* 可达。
# 门禁拒绝文案（按状态码取）：403 是"设置未生效"（系统开关/执行体写沿用），
# 400 是"操作已取消"（高危二次鉴权沿用）。两处历史契约都不动。
PW_DENY_TEXT = {400: "当前密码不正确，操作已取消", 403: "口令校验未通过，设置未生效"}
# 「没提交口令」与「口令输错」必须分开：前者是调用方还没问用户要口令（前端应弹口令框
# 后重试），后者是用户真的输错了（应显示"不正确"）。共用一句"密码不正确"会让前端无法
# 区分这两种处置，也让运维误以为自己的口令被改了。状态码两档与上面完全一致，
# 只有 error 文案与 reason 不同。
PW_MISSING_TEXT = {400: "此操作需要输入当前密码，操作已取消",
                   403: "需要输入当前口令，设置未生效"}
# 给前端的机器可读口径（前端不要靠比对中文文案分支）：
# password_required → 收口令后重试；password_incorrect → 提示输错并计数。
PW_DENY_REASON = {"missing": "password_required", "wrong": "password_incorrect"}
# 口令喷洒判定：同一 IP 在本窗口内失败过的不同用户名数达到该值 → 告警升级为紧急
# （低于此值多半是本人忘密码，不该占用每天只有 3 条的紧急账）
LOGIN_SPRAY_USERS = 3

# IP 计数 dict（限速/登录失败/注册）的条目上限与最长保留实现见 web/security.py
# （`_ip_store_trim` 按它们判定），此处以导入区再导出保持 m.* 可达。

# API 请求限速（防脚本轰炸）：每 IP 窗口内最多 RATE_MAX 次 /api/* 请求
RATE_WINDOW = 10  # 窗口（秒）
RATE_MAX = 60  # 窗口内最大 API 请求数（正常用户远低于此）
# 已登录会话的 GET 放宽阈值（用户实拍快速切页 429）：新前端每个页面首屏 5–9 个
# /api/*（外壳 me/clock/announcement/nav badges + 页面数据），快速切 7 页/10s 最坏
# ≈63 次就撞严格阈值。外壳数据客户端缓存后实测降到 ≤15 次/10s，故取 240 = 严格阈
# 值的 4 倍，覆盖未缓存最坏 3.8 倍余量；仍远低于脚本化滥用可接受上限（24 req/s）。
# 仅放宽「已登录 + GET」：写路径（POST/PUT/DELETE）与匿名请求维持 RATE_MAX，脚本
# 轰炸主防线不变。
RATE_MAX_AUTH_GET = 240
# 注册限速（防邮箱批量注册）：每 IP 窗口内最多 REGISTER_MAX 次成功注册
REGISTER_WINDOW = 600  # 窗口（秒）= 10 分钟
REGISTER_MAX = 5  # 窗口内最大成功注册数
# 日志导出限速：每 IP 窗口内最多 EXPORT_MAX 次（导出按日期可枚举且整份返回
# 日志文本，限速防脚本化批量拉取历史日期）
EXPORT_WINDOW = 60  # 窗口（秒）
EXPORT_MAX = 6  # 窗口内最大导出次数

# 账号详情读取限速：/api/accounts/<idx>/detail 的 idx 从 0 递增即可整库枚举，
# 且它是列表接口之外唯一回传明文口令/完整手机号的面。列表侧已脱敏、编辑框每次
# 只取一行，故取"一个会话一分钟 60 次"——等于每秒点开一次编辑框且整整一分钟
# 不停，人类运维到不了；对照 EXPORT_WINDOW/EXPORT_MAX 同量级的"整份数据出口"口径。
DETAIL_WINDOW = 60  # 窗口（秒）
DETAIL_MAX = 60  # 窗口内最大详情读取次数（超限 429）

# 只读面聚合审计：同一管理员对同一资源类在一个窗口内只按档位落几行，detail 带
# 累计次数与脱敏目标摘要。逐请求一行会把审计表变成"被盗会话的免费打字机"——
# 拒绝面已经实测过这个洞（拿 429 当产出），读取面若做成逐条就是换个口子重开。
# 窗口取 1 小时：日志页 10s 轮询是页面心跳而非取数，短窗口会让心跳自己刷满
# 审计表；档位（首次必落 + 10/50 两个批量档 + 此后每 200 次）保证"读一次也留痕、
# 读五十行得见累计"，最坏情形（日志页常开一整天）约百行，远小于逐条口径。
READ_AUDIT_WINDOW = 3600  # 聚合窗口（秒）= 1 小时
READ_AUDIT_LADDER = (1, 10, 50)  # 窗口内累计次数落在这些档位时各写一行
READ_AUDIT_EVERY = 200  # 越过最高档后每多少次追加一行
READ_AUDIT_TARGET_CAP = 8  # 单行里最多列几个脱敏目标（db.audit 的 detail 本身截 200 字）

# 告警通道 GET 里仅主管理员可读的字段：两本账的**当日余量**。
# 上限（daily_max / urgent_daily_max）与节流（cooldown）刻意不在此列——它们是规则
# 配置，注册管理员看不到就无法判断"为什么没收到告警"，而余量才是拆报警器前的勘察面。
_NOTIFY_QUOTA_HIDDEN_KEYS = ("daily_remaining", "urgent_daily_remaining")

# ---- 推送/邮件配置的输入上限（写侧唯一拦点）----
# 为什么要有上限：本项在裸机直连形态下没有 nginx 的请求体/头长度兜底，超长值会被
# 原样加密进 .env，并在每次推送/发信时带出；条数不设限则一次请求就能把 .env 撑大。
# 取值依据（不是拍脑袋）：Server酱 SendKey 是"定长前缀 SCT + 固定宽度主体"，
# 实测 35 字符 → 上限取 64（约一倍余量），下限 8 只为挡明显占位串；自定义通知地址
# 按 URL 的实际长度上限取 2048；告警收件人与 SMTP 备选条目都是"管理员级"的小列表
# （当前部署各 1~3 条），上限取 10 足够真实使用且不会被误触。
NOTIFY_SENDKEY_MIN_LEN = 8
NOTIFY_SENDKEY_MAX_LEN = 64
NOTIFY_URL_MAX_LEN = 2048
MAIL_ADMIN_TO_MAX = 10
MAIL_SMTPS_MAX = 10

# 账号验证尝试限频（2026-08-27 P1-2）：每用户窗口内网络验证次数上限。
# 预验证 = 服务器代发真实易班登录，必须在资格预筛之外再加用户维度节流。
# 常量本体（VERIFY_MAX / VERIFY_WINDOW）随安全域搬入 web/security.py（判定在
# `_verify_attempt_allowed`），此处以导入区再导出保持 m.* 可达。

# 账号验证认证失败冷却（2026-09-04 生产复盘）：同一手机号窗口内认证失败达到
# 阈值后临时拒绝再验证。密码错误属确定性失败，重复验证每次都是一次真实易班
# 登录，连续少量错误易班侧即返回「错误尝试过多」锁定账号（生产实测 6 次即锁），
# 故按「被锁定对象 = 易班账号 = 手机号」设冷却；仅限 web 验证路径，探针与
# cron 签到不受影响。
# 三个阈值与失败特征词本体（VERIFY_FAIL_MAX / VERIFY_FAIL_WINDOW /
# VERIFY_FAIL_COOLDOWN / VERIFY_FAIL_AUTH_KEYWORDS）随安全域搬入 web/security.py
# （判定在 `_record_verify_failure`），此处以导入区再导出保持 m.* 可达。
VERIFY_FAIL_COOLDOWN_MSG = (
    "该手机号验证失败次数过多，已被临时限制验证，请约 10 分钟后再试；"
    "连续密码错误会导致易班账号被锁定，如密码有误请先在易班 APP 重置。"
)

# 外呼校验的**全局**并发上限（A4，2026-09-15）：上面两条配额都是「按会话用户」与
# 「按手机号」，覆盖不到"多个账号同时校验"这个维度。实测 8 个并发校验即占满
# gunicorn 的 8 个线程 → 整站约 15 秒完全无响应（/api/clock 探针在饱和期无响应）。
# 这里限制同时在跑的外呼条数，**超出立即失败而非排队**——排队会把线程继续钉住，
# 正是要避免的情形。
VERIFY_CONCURRENCY_MAX = 2
VERIFY_BUSY_MSG = "校验繁忙，请稍后重试"

# 异步校验任务（A4 第二段）：实现已收进 yiban/attempt/jobs.py（调度核心行为）。
# 待办（pending + running）上限说明：每个任务占一个后台线程，而外呼席位只有
# VERIFY_CONCURRENCY_MAX 个——不设上界时并发提交会堆出大量等席位的线程。
# 这是 503「校验繁忙」在异步模式下的**可达来源**（席位满由后台排队消化，
# 不拒绝用户；待办队列满才拒绝）。待办上限常量本体已随校验队列搬入
# web/services/verify_queue.py，此处以导入区再导出保持 m.* 可达。
# 任务终态（查询/取消端点判定用）
VERIFY_JOB_TERMINAL = verify_jobs.TERMINAL_STATUSES

# 注销账号冷却（防批量注销，user_delete_requests 表计数，v5）：
# 每用户 60 秒内最多 1 次、每 IP 60 秒内最多 DELETE_MAX_REQUESTS_PER_IP 次；
# 超限返回 429 且不暴露冷却秒数（信息分层，防恶意用户据此规划批量节奏）
DELETE_COOLDOWN_SEC = 60

# 会话绝对过期上限默认天数（2026-08-27 P2-5）：实际值在 create_app 内按
# YIBAN_SESSION_ABS_DAYS 解析并钳制到 [1,30]；此处为 create_app 前引用兜底。
SESSION_ABS_DAYS_DEFAULT = 7
SESSION_ABS_TTL_SECONDS = SESSION_ABS_DAYS_DEFAULT * 86400
DELETE_MAX_REQUESTS_PER_IP = 5
# 高危删除操作冷却（2026-08-29 被盗号滥用面加固）：同一管理员在窗口内最多执行
# ADMIN_DELETE_MAX 次删除类高危操作（批量删除/彻底清除/完全删除），防被盗会话
# 快速反复删除用户并刷告警邮件。与注销冷却同语义，超限 429 且不暴露冷却参数。
# .env 可调（YIBAN_ADMIN_DELETE_COOLDOWN_SEC / YIBAN_ADMIN_DELETE_MAX，0=关闭）。
ADMIN_DELETE_COOLDOWN_SEC = 60
ADMIN_DELETE_MAX = 5
# 注销宽限期（天）：软删除冷却期，与账号软删除保留期对齐，与 db.purge_deleted_users
# 默认一致；已注销用户视图按此计算剩余天数。常量本体（取 db.SOFT_DELETE_RETENTION_DAYS
# ——账号保留期的**唯一事实源**，不要再写字面量）已随账号数据族搬入
# web/services/accounts_data.py，此处以导入区再导出保持 m.DELETE_GRACE_DAYS 可达。

# 容量上限（2026-08-15 对抗性审查补：注册/使用人数超负载兜底；2026-08-31 口径修订）：
# 用户 = 全部未删除注册用户（含尚未添加账号的），上限默认 500——注册表防膨胀，口径宽松；
# 账号 = 至少持有 1 个非删除账号的活跃注册用户，上限默认 200（一人一号 ≈ 200 活跃使用者，
#   调度窗口 80min ÷ 单账号平均 8s ≈ 600 理论上限，留裕量防 web 解密/轮询劣化）。
# 0 = 不限。可用 .env 的 YIBAN_MAX_USERS / YIBAN_MAX_ACCOUNTS 调整。
DEFAULT_MAX_USERS = 500
DEFAULT_MAX_ACCOUNTS = 200

# ---- 设置项档位（`POST /api/settings` 权限判定的**唯一事实源**）----
# 分档判据是「影响半径 × 能否造成静默漏签」，不是"看起来危不危险"：
#   A 档（`MASTER_ONLY_KEYS`）一次改动就波及全站签到或直接拆掉安全闸门——周末开关、
#     签到窗口与首尾裁切决定"今天到底签不签得到"，随机延迟与容量上限决定"多少账号被
#     挤出窗口"，account_verify / probe_* 会让服务器对**全站账号**发起真实易班登录。
#     故仅主管理员可写，且值真变化时必须当次输口令（短时豁免不适用）+ 变更告警。
#   B 档（`GATED_KEYS`）只改排序风格与自选权，出错有 A 档参数兜底，故任意管理员可写，
#     值真变化时过同一个口令门禁但允许豁免。
# 唯一的例外是 `global_pause`：0→1「急停」任意管理员都能做（当次口令 + 占用高危额度 +
# 紧急告警），1→0 恢复仍仅主管理员——把"先止损"的权力留在在场每个人手里，把"放开"的
# 权力收在主管理员手里。
# 新增设置键时必须改这里而不是在路由里再列一遍键名：此前 403 清单只写在 handler 内，
# 与前端各页自己的收控件清单两处各写一遍、必然漂移（测试里的元测试负责比对这两份）。
MASTER_ONLY_KEYS = frozenset({
    "sign_window", "window_edge_sec", "edge_front_sec", "edge_back_sec",
    "sunday_sign", "saturday_sign", "registration_pause",
    "start_delay_max", "gap_max", "max_users", "max_accounts",
    "account_verify", "probe_enable", "probe_time", "probe_interval",
})
GATED_KEYS = frozenset({"sign_order", "sign_dist", "sign_mode", "allow_time_pref"})
# `global_pause` 刻意不进 `MASTER_ONLY_KEYS`：它是 A 档的唯一例外，权限按**变更方向**
# 分流（0→1 急停人人可做、1→0 恢复仅主管理员），故单独用这个键名判方向。
GLOBAL_PAUSE_KEY = "global_pause"

# 自选时间片切换冷却（2026-08-15 用户反馈 → 弹性冷却）：
# 60 秒窗口内前 TIME_PREF_COOLDOWN_FREE 次切换完全自由（浏览式"全点一遍再定"属正常行为）；
# 超出后冷却递增：基础 × 2^(超限次数)，封顶 TIME_PREF_COOLDOWN_MAX（持续高频才被压制）。
# 高频切换本质是自我惩罚（updated_at 变晚 → 先到先得排后），冷却只为防连点/防刷屏噪音。
# 0 = 关闭。可用 .env 的 YIBAN_TIME_PREF_COOLDOWN_SEC 调整基础值（默认 30）。
TIME_PREF_COOLDOWN_SEC = 30
TIME_PREF_COOLDOWN_FREE = 20        # 60 秒窗口内自由切换次数（覆盖"全点一遍"16 片+选定）
TIME_PREF_COOLDOWN_MAX = 300        # 弹性封顶（秒）
TIME_PREF_COOLDOWN_WINDOW = 60      # 计数窗口（秒）

# 暂停签到冷却（2026-08-16 调整）：恢复不受限；暂停采用弹性冷却——
# 60 秒窗口内前 PAUSE_COOLDOWN_FREE 次完全自由（好奇地暂停/恢复/再暂停不会被误杀），
# 超出后冷却递增（基础 × 2^(超限次数)，封顶 PAUSE_COOLDOWN_MAX）。
# 防脚本刷审计/状态显示抖动，但不惩罚正常手快用户。0=关闭。
PAUSE_COOLDOWN_SEC = 30
PAUSE_COOLDOWN_FREE = 3         # 60 秒窗口内自由暂停次数（覆盖"试一下"）
PAUSE_COOLDOWN_MAX = 120        # 弹性封顶（秒）
PAUSE_COOLDOWN_WINDOW = 60      # 计数窗口（秒）

# 普通用户邮箱格式校验（用户名部分（@ 前）限 32 字符：防超长用户名破坏界面显示）
# re.ASCII——str 模式的 \w 匹配 Unicode 字母，同形字/IDN 域名可绕过
# 一次性域名黑名单的字面匹配；限 ASCII 后此类注册直接被格式校验拦截
EMAIL_RE = re.compile(r"^[\w.+-]{1,32}@[\w-]+(\.[\w-]+)+$", re.ASCII)
EMAIL_USER_MAX = 32  # 邮箱用户名部分（@ 前）最大长度
# 手机号格式（易班登录账号为中国 11 位手机号；恶意字符可注入前端事件与日志）常量已随
# 账号数据族搬入 web/services/accounts_data.py，此处以导入区再导出保持 m.PHONE_RE 可达。

# 手动签到防抖：同一账号两次触发的最小间隔（秒）
SIGN_MIN_INTERVAL = 30  # 手动签到防抖窗口（秒）；注释口径见 web/routes/signin_api.py 的 _spawn_signin docstring

# 日志行可见性与行正则（`SIGN_LOG_RE`）的实现见 web/services/logs.py：两者在导入区
# 再导出，`web.app.<名字>` 的取用面不变。
# 日志格式（与 signin.py 相同）：
# 行格式: [2026-08-07 06:40:04] [INFO] yiban: [手机号] ✅ 签到成功
# logger 名允许点分（`yiban.client` / `yiban.fyiban.protocol` …）：只认 `(\w+)` 的正则匹配不到
# 带点的名字，签到链路的**细节行**（登录成功 / 生成定位 / 签到成功）会整行被丢弃，日志页只剩
# 汇总与结果。

# 签到状态码与图标/文案映射：**定义在 yiban.status（唯一事实源）**，此处为别名。
# 历史上本文件另定义了一份同名常量与 STATUS_ICON/STATUS_TEXT，与 signin 侧各自漂移
# （实测本侧缺 no_position/global_paused、signin 侧缺 pending）。收口后状态码只有一处
# 定义；两张映射的差异是**有意的**（不同消费方），合并需前后端协同，见 yiban/status.py。
STATUS_SUCCESS = yiban_status.STATUS_SUCCESS
STATUS_ALREADY = yiban_status.STATUS_ALREADY
STATUS_NO_TASK = yiban_status.STATUS_NO_TASK
STATUS_FAILED = yiban_status.STATUS_FAILED
STATUS_RETRYING = yiban_status.STATUS_RETRYING
STATUS_SKIPPED_WINDOW = yiban_status.STATUS_SKIPPED_WINDOW
STATUS_SKIPPED_NORANGE = yiban_status.STATUS_SKIPPED_NORANGE
STATUS_PAUSED = yiban_status.STATUS_PAUSED
STATUS_USER_CANCELLED = yiban_status.STATUS_USER_CANCELLED
STATUS_PENDING = yiban_status.STATUS_PENDING

# 状态图标（全站统一口径；经 /api/my-accounts 的 state_icon 下发，前端按码渲染）
STATUS_ICON = yiban_status.ICON
STATUS_TEXT = yiban_status.TEXT


# 账号状态族（`load_sign_state` / `_cred_paused_phones` / `clear_fuse_pause` /
# `clear_fuse_on_cred_change`）的实现见 web/services/logs.py：前三个与凭据熔断清理族在
# 导入区再导出，`load_sign_state` 在下方转发（需注入本模块持有的 STATE_DIR）。


def load_sign_state(date_str=None):
    """读取按日结构化状态文件（实现见 web/services/logs.py）。

    状态目录按调用时刻现取本模块的 `STATE_DIR`（可被 `--config` 等参数覆盖）。
    """
    return _logs_svc.load_sign_state(STATE_DIR, date_str)

logger = logging.getLogger("web")


# ---------------------------------------------------------------------------
# 签到日志解析
# ---------------------------------------------------------------------------
# 实现见 web/services/logs.py：`_LOG_TAIL_BYTES` / `_tail_lines` / `_most_recent_log_cache`
# 与 `_is_valid_date_str` 在导入区再导出；需要本模块模块级状态的入口（日志目录 LOG_FILE、
# 倒读实现 `_tail_lines`）在下方转发时调用时刻现取后注入——它们会被测试改写（既有测试直接
# 赋值 `web.app.LOG_FILE`、并在 `web.app` 上打桩 `_tail_lines`），服务层另持一份绑定会让
# 打桩静默失效。


def parse_sign_log(path):
    """解析签到日志（实现见 web/services/logs.py）；倒读实现现取本模块的 `_tail_lines`。"""
    return _logs_svc.parse_sign_log(path, _tail_lines)


def log_path_for(date_str=None):
    """按天日志文件路径（实现见 web/services/logs.py）；日志目录现取本模块的 `LOG_FILE`。"""
    return _logs_svc.log_path_for(LOG_FILE, date_str)


def _log_lines_for(date_str):
    """读取指定日期日志的行（实现见 web/services/logs.py）。

    路径与倒读实现都按调用时刻现取本模块的（`LOG_FILE` 可被测试直接赋值改写）。
    """
    return _logs_svc._log_lines_for(date_str, log_path_for, _tail_lines)


def _today_has_logs():
    """今天是否有 yiban 签到日志行（实现见 web/services/logs.py）。

    注入同 `_log_lines_for`：日志路径与倒读实现都现取本模块的 `LOG_FILE` / `_tail_lines`。
    """
    return _logs_svc._today_has_logs(log_path_for, _tail_lines)


def _most_recent_log_date(max_days=30):
    """查找最近有日志的日期（实现见 web/services/logs.py）。

    注入同 `_log_lines_for`：日志路径与倒读实现都现取本模块的 `LOG_FILE` / `_tail_lines`。
    """
    return _logs_svc._most_recent_log_date(max_days, log_path_for, _tail_lines)


# ---------------------------------------------------------------------------
# .env 读写
# ---------------------------------------------------------------------------
# 读（read_env / load_env_int）在导入区再导出；写与设置项展示族的转发在下方
# （它们需要注入本模块持有的 _atomic_write / write_env_batch / _env_flag / 设置缺省值）。


# 站点展示族（备案信息 / 分享摘要配图 / 窗口裁剪）：实现见 web/render.py，本模块只转发。
# `.env` 一律经本模块的 `read_env(ENV_FILE)` 调用时刻现读，保持既有打桩面（测试与
# `--env` 都会改写 ENV_FILE，由转发处取用才不会被 render 层的副本绑定架空）。
def email_domain_error(email):
    """邮箱域名可用性审查（实现见 web/render.py）。"""
    return _render.email_domain_error(email, read_env(ENV_FILE))


def icp_info():
    """网站 ICP 备案信息（可选，实现见 web/render.py）。"""
    return _render.icp_info(read_env(ENV_FILE))


def police_info():
    """公安备案信息（可选，实现见 web/render.py）。"""
    return _render.police_info(read_env(ENV_FILE))


def police_link():
    """公安备案查询链接（实现见 web/render.py；scheme 白名单与回落同在其中）。"""
    return _render.police_link(read_env(ENV_FILE))


# 站点简介默认文案（分享预览用；措辞取自 README 项目介绍，勿写成营销语）。
# 部署方可经 .env 的 YIBAN_SITE_DESCRIPTION 覆盖。
SITE_DESCRIPTION_DEFAULT = (
    "易班自动签到辅助工具：配置一次后每天定时自动完成易班早操签到，"
    "无需手动操作；提供网页管理后台，支持多账号管理、失败重试与告警通知。"
)


def site_description():
    """站点简介（实现见 web/render.py）；留空时用本模块的默认文案。"""
    return _render.site_description(read_env(ENV_FILE), SITE_DESCRIPTION_DEFAULT)


def site_image():
    """分享预览配图绝对地址（可选，实现见 web/render.py）。"""
    return _render.site_image(read_env(ENV_FILE))


# 掐头去尾（前后独立，秒级，0.5 分钟=30s 粒度）：
# 新键 YIBAN_WINDOW_EDGE_FRONT_SEC / _BACK_SEC 优先；旧键 YIBAN_WINDOW_EDGE_SEC（前后对称）
# 存在时映射为 front=back=旧值，保证升级前配置行为不变。范围 0~300 秒。
def edge_config():
    """返回 (front_sec, back_sec)：签到窗口前后裁剪秒数（实现见 web/render.py）。"""
    return _render.edge_config(read_env(ENV_FILE))


def edge_front_sec():
    """前裁秒数（兼容旧调用的便捷入口，实现见 web/render.py）。"""
    return _render.edge_front_sec(read_env(ENV_FILE))


def sign_window_bounds():
    """有效签到窗口（起止 + 前后裁剪，含裁剪吃空时的回退）→ `yiban.window.Window`。

    唯一口径在 `yiban.window.bounds`：自选片展示与引擎排计划必须同源——网页侧重算一遍
    几何会在"有效窗口被裁剪吃空"时与引擎分叉（引擎按回退默认窗口切块，网页却按原始
    配置把片全置灰），用户所选片随之被静默放弃。窗口起止与前后裁剪两个取值点按调用
    时刻现取本模块的（测试会打桩 `web.app._sign_window` / `web.app.edge_config`）。
    """
    start, end = _sign_window()
    front_sec, back_sec = edge_config()
    return yb_window.bounds({
        "sign_start": start,
        "sign_end": end,
        "edge_front_sec": front_sec,
        "edge_back_sec": back_sec,
    })


# 设置项展示族（键的中文标签 / 值的展示形态 / A/B 档生效值）实现见 web/services/env_io.py；
# 三个容量缺省值与开关解析器 `_env_flag` 由本模块现取注入（它们是本模块的名字，会被测试改写）。
def _settings_effective_values(env_file):
    """A/B 档设置项的当前生效值（实现见 web/services/env_io.py）。"""
    return _env_io_svc._settings_effective_values(
        env_file, _env_flag,
        gap_max_default=DEFAULT_ACCOUNT_GAP_MAX,
        max_users_default=DEFAULT_MAX_USERS,
        max_accounts_default=DEFAULT_MAX_ACCOUNTS,
    )


#: 并行执行体槽位的最大下标（`YIBAN_WORKERS` 允许 1~64 → 下标 0~63）；
#: 单槽位出口写接口按"下标 + 当前执行体数"两重判定拒绝未被使用的槽位。
#: 常量本体在导入区（= `yiban.egress.SLOT_MAX`，清单模型的唯一口径）定义。


# 执行体清单的写入与校验（`YIBAN_EXECUTORS`）实现见 web/services/executor_env.py。
# 写回一律经本模块的 `write_env_batch` 现取注入：它既是"每一次 .env 落盘"的观测点
# （测试在此打桩），也负责把落盘交给本模块的 `_atomic_write`。
def _save_slot_egress(env_path, key, index, value):
    """读-改-写 `.env` 里一段出口（实现见 web/services/executor_env.py）。"""
    return _executor_env._save_slot_egress(env_path, key, index, value, write_env_batch)


def _executor_rows(env_path=ENV_FILE):
    """读执行体清单，必要时按旧三键迁移写回（实现见 web/services/executor_env.py）。"""
    return _executor_env._executor_rows(env_path, write_env_batch)


def _mutate_executor_rows(mutator, env_path=ENV_FILE):
    """`.env` 写锁内读清单 → 应用 mutator → 写回（实现见 web/services/executor_env.py）。"""
    return _executor_env._mutate_executor_rows(mutator, env_path, write_env_batch)


def _save_row_egress(env_path, slot, value):
    """清单模式下只改该行的出口（实现见 web/services/executor_env.py）。"""
    return _executor_env._save_row_egress(env_path, slot, value, write_env_batch)


def _save_fallback_egress(env_path, value):
    """清单模式下改兜底行的出口（实现见 web/services/executor_env.py）。"""
    return _executor_env._save_fallback_egress(env_path, value, write_env_batch)


def write_env_int(env_path, key, value):
    """把整数配置写入 .env：value<=0 删除该行，>0 写入（实现见 web/services/env_io.py）。"""
    return _env_io_svc.write_env_int(env_path, key, value, write_env_batch)


# 行分隔符判定 / 键行折叠 / 行计数 / 歧义检测的单一实现已迁至 yiban/infra/env_io.py
# （ENV_LINE_BREAK_CHARS / has_line_break / key_line_pattern / count_key_lines /
# find_env_key_collisions）：读的一半（parse_env_file）本就在那里，写的一半随之
# 落位，scripts/ 各 .env 写入方无需反向依赖 web 即可共用同一套判定。
# 不变量（读写两侧同源）：新注入在写入口被 has_line_break 拦下；升级前已埋下的
# 潜伏载荷不会被回溯改写——由 create_app 启动时的
# _report_env_key_collisions（yiban.infra.env_io.find_env_key_collisions）报告、运维手工清理。
# 此处保留模块级别名（本模块已无自用点）：既有调用点与测试的探测口径
# （webapp._has_line_break / webapp._ENV_LINE_BREAK_CHARS）保持不变。
_ENV_LINE_BREAK_CHARS = env_io.ENV_LINE_BREAK_CHARS
_has_line_break = env_io.has_line_break
_env_key_line_re = env_io.key_line_pattern
_count_env_key_lines = env_io.count_key_lines


def write_env_key(env_path, key, value):
    """把任意键值写入 .env：value 为空删除该行，否则写入；保留注释与其他行。

    实现见 web/services/env_io.py；写回落在本模块的 `write_env_batch` 上（同一观测点）。
    """
    return _env_io_svc.write_env_key(env_path, key, value, write_env_batch)


def write_env_batch(env_path, updates):
    """批量写入多个键值（原子操作，实现见 web/services/env_io.py）。

    落盘交给本模块现取的 `_atomic_write`：它是"每一次 .env 落盘"的观测点（测试在此
    打桩快照全文），且 Windows 上的替换重试策略在那里；服务层另持绑定会让打桩静默失效。
    """
    return _env_io_svc.write_env_batch(env_path, updates, _atomic_write)


def ensure_secret_key(env_path):
    """确保 .env 中存在 YIBAN_SECRET_KEY（缺失时自动生成随机值，实现见 web/services/env_io.py）。

    落盘同样交本模块现取的 `_atomic_write`（不可写时由服务层降级为进程内随机密钥并告警）。
    """
    return _env_io_svc.ensure_secret_key(env_path, _atomic_write)


# 内置主管理员（.env 账号）的会话凭据键名与会话凭据族（_new_admin_sid /
# _issue_admin_sid / _admin_session_facts）实现见 web/security.py；键名以导入区再导出
# 保持 m.ADMIN_SID_ENV_KEY 可达（改密路径经 m.* 取用它写 .env）。
def _issue_admin_sid(env_path):
    """换发内置主管理员会话凭据（实现见 web/security.py）。

    `.env` 写路径与读取器按调用时刻现取本模块的（测试会打桩 `web.app.read_env`，
    `--env-file` 也会改写 `ENV_FILE`）——服务层另持一份绑定会让改写静默失效。
    """
    return _security._issue_admin_sid(env_path, write_env_key, read_env)


def _admin_session_facts(env_path):
    """内置主管理员的两项会话吊销凭据 (口令版本, sid)（实现见 web/security.py）。

    `.env` 读取器按调用时刻现取本模块的（测试会打桩 `web.app.read_env`）。
    """
    return _security._admin_session_facts(env_path, read_env)


def _builtin_admin_email():
    """内置管理员（.env）标识（小写）（实现见 web/security.py）。

    `.env` 路径与读取器按调用时刻现取本模块的（测试与 `--env-file` 都会改写）。
    """
    return _security._builtin_admin_email(ENV_FILE, read_env)


def _is_builtin_admin_session():
    """当前会话是否确实是内置管理员（.env）登录（实现见 web/security.py）。

    `.env` 路径与读取器按调用时刻现取本模块的；本函数与 `_effective_role` 都在
    请求期调用（盘点确认无调度线程/启动路径调用）。
    """
    return _security._is_builtin_admin_session(ENV_FILE, read_env)


def _effective_role(username, pw_version=None):
    """实时角色判定（实现见 web/security.py）。

    `.env` 路径与读取器按调用时刻现取本模块的。注册用户侧按 `yiban.store.db` 的
    真源查表（本模块的 `db` 即那一份）。
    """
    return _security._effective_role(username, pw_version, ENV_FILE, read_env)


def _current_role():
    """当前登录会话的实时角色（实现见 web/security.py）。

    会话绝对期按调用时刻现取本模块的 `SESSION_ABS_TTL_SECONDS`：它在 create_app 里
    按 `YIBAN_SESSION_ABS_DAYS` 重新解析后回写模块全局，转发必须取到新值。
    """
    return _security._current_role(SESSION_ABS_TTL_SECONDS, ENV_FILE, read_env)


def migrate_admin_password_to_hash(env_path):
    """启动时安全迁移管理员口令明文 → scrypt 哈希（实现见 web/security.py）。

    `.env` 读取器、整数配置读取器与批量写路径按调用时刻现取本模块的：三者都会被
    测试打桩或赋值改写，服务层另持一份绑定会让改写静默失效。
    本函数由 create_app 启动路径调用（多 worker 各自执行），自带 `env_path`
    参数、不读请求上下文。
    """
    return _security.migrate_admin_password_to_hash(
        env_path, read_env, load_env_int, write_env_batch)


# `os.replace` 的瞬态失败重试预算与 `_replace_with_retry` / `_atomic_write` 实现见
# web/security.py，此处以导入区再导出保持 m.* 可达（`_atomic_write` 必须是**同一个
# 函数对象**：env_io / measure 的转发包装按调用时刻现取本模块的这个名字注入，
# patch.object(web.app, "_atomic_write") 才继续被落盘路径看见）。


# ---------------------------------------------------------------------------
# 数据读写（SQLite：db 层单行事务 + WAL，天然原子，无需 TTL 缓存）
# RLock：进程内"读→检查→写"操作级序列互斥（防呆判定与写入之间不被同进程请求交错；
# 跨进程一致性由 SQLite 事务与 UNIQUE 约束保证）
# ---------------------------------------------------------------------------
# 进程内两把锁的唯一真源都在 web/services/locks.py，本模块在导入区再导出
# （`web.app._file_lock` / `web.app._rate_lock` 与 m.* 是同一把）：账号读改写序列经
# `_file_lock` 互斥，限速/失败计数 dict 的读改写（H7：单 worker + 锁内原子更新；
# scrypt 校验不持锁，避免长时间阻塞其他请求）经 `_rate_lock` 互斥；各模块自建一把
# 会让"同进程内互斥"静默失效。
# 每日清理线程只允许同一进程启动一次（测试多次 create_app 时避免并发访问共享 SQLite 单例）
_purge_loop_started = False
_purge_loop_lock = threading.Lock()


# IP 计数表的回收与窗口/失败计数（`_ip_store_trim` / `_bump_window_count` /
# `_bump_login_failure`）、敏感口令门禁旋钮（`_sensitive_gate_params`）、账号校验配额
# 与冷却（`_verify_attempt_allowed` / `_verify_fail_cooldown_remaining` /
# `_record_verify_failure`）实现见 web/security.py，此处以导入区再导出保持 m.* 可达
# （路由在 m._rate_lock 下调用这些计数助手，同一把锁真源在 web/services/locks.py）。
# `_sensitive_gate_params` 是唯一例外：它要注入本模块的 `load_env_int`（转发包装见下方）。


def _read_audit_row_due(cnt):
    """只读面聚合审计：窗口内第 cnt 次读取是否该落一行（True=落行）。

    单独成函数的理由是把"聚合而非逐条"这条判据变成可被用例直接钉住的纯逻辑：
    首行必落（读一次也得留痕，不能等窗口关闭才 flush——进程重启或"就读这一次"
    会让痕迹永久消失），此后只在批量档位补行。行数上界 = 档位数 + 越档后每
    READ_AUDIT_EVERY 次一行，详情面这一路还被 DETAIL_MAX 的 429 再卡一道。
    """
    if cnt in READ_AUDIT_LADDER:
        return True
    return cnt > READ_AUDIT_LADDER[-1] and cnt % READ_AUDIT_EVERY == 0


def _sensitive_gate_params(env_path):
    """敏感口令门禁两个旋钮的唯一解析处（实现见 web/security.py）。

    整数配置读取器按调用时刻现取本模块的（测试会打桩 `web.app.load_env_int`）。
    """
    return _security._sensitive_gate_params(env_path, load_env_int)


# 手动签到子进程族（等待回收 `_wait_signin_proc` / 队列超时缩放 `_batch_wait_timeout` /
# 退出码原因 `_manual_sign_failure_reason` 与退出码表 `_SIGNIN_EXIT_REASONS`）实现见
# web/services/manual_sign.py，此处以导入区再导出保持 m.* 可达；只有 `_log_manual_sign_exit`
# 需要注入本模块持有的入口，故在下方转发。
def _log_manual_sign_exit(phone_label, returncode):
    """手动签到退出码留痕（实现见 web/services/manual_sign.py）。

    日志路径按调用时刻现取本模块的 `log_path_for`（它再现取 `LOG_FILE`）：测试会赋值
    `webapp.LOG_FILE` 换日志目录，转发处不现取就会被服务层的副本绑定架空。
    """
    return _manual_sign._log_manual_sign_exit(phone_label, returncode, log_path_for)


# `.env` 写互斥与公告元数据解析的实现见 web/services/env_io.py：
# `_env_write_lock` 是纯再导出（跨进程写锁沿用 yiban.infra.env_lock 真源）；
# `_parse_announcement_meta` 与其分隔符/时刻格式常量同在导入区再导出。

# 启动缓存（与数据无关）：CHANGELOG 部署重启自然失效；公告**发布**时同步更新
_changelog_cache = [None]  # [文本]
_announcement_cache = [None]  # [已发布公告文本]（草稿刻意不进缓存：它要按会话现读）

# ---- 全站公告双人发布：三个 .env 键的键名单源在此 ----
# 公告会出现在**全体学生**的页面顶横幅与登录页上，是内部人/被盗会话最好用的社工面，
# 故拆成两半：普通管理员只能写草稿，主管理员点"发布"才落成对外可见的那一键。
# 作者与时刻单独成键（而非塞进草稿正文），为的是发布人批准前就能看见"这是谁、
# 什么时候写的"——审计表只能事后追，双人发布要的是事前那一眼。
ANNOUNCEMENT_KEY = "YIBAN_ANNOUNCEMENT"
ANNOUNCEMENT_DRAFT_KEY = "YIBAN_ANNOUNCEMENT_DRAFT"
ANNOUNCEMENT_DRAFT_META_KEY = "YIBAN_ANNOUNCEMENT_DRAFT_META"
# 线上公告的发布人/时刻：前端要同屏显示"草稿是谁写的"与"线上是谁在何时发的"，
# 否则管理员只能看到一串文本，分不清自己看到的到底是待发布内容还是已生效内容。
ANNOUNCEMENT_PUBLISHED_META_KEY = "YIBAN_ANNOUNCEMENT_PUBLISHED_META"
# 元数据值的分隔符与时刻格式随解析器（_parse_announcement_meta）留在
# web/services/env_io.py，此处以导入区再导出保持 m.ANNOUNCEMENT_DRAFT_META_SEP/_FMT 可达。


# 账号 / 用户读取（load_accounts / load_accounts_raw / load_users）、展示序列化
# （mask_account / _mask_email / _owner_display_of）、定位与字段校验
# （find_account_index / _duplicate_phone_error / _owner_has_other_live / validate_account）、
# 口令策略（_password_policy_error / _admin_password_policy_error）与只读验证
# （_as_signin_account / _verify_account_clean）实现见 web/services/accounts_data.py；
# 审核态词表、手机号正则、注销宽限期与口令策略常量也随该族搬入，此处一律以导入区再导出
# 保持 m.* 可达。
def _slot_to_label(slot_min):
    """自选片窗口内分钟数 → "HH:MM"（实现见 web/services/accounts_data.py）。

    有效窗口视图按调用时刻现取本模块的（测试会打桩 `web.app._sign_window` /
    `web.app.edge_config`，`sign_window_bounds` 现取后穿透到服务层）。
    """
    return _accounts_data._slot_to_label(slot_min, sign_window_bounds)


def _estimate_slot(phone):
    """预计签到时段（实现见 web/services/accounts_data.py）。

    账号读入口、`.env` 路径与读取器、整数配置读取器、有效窗口视图都按调用
    时刻现取本模块的（测试会打桩 `read_env` / `_sign_window` / `edge_config` /
    `load_accounts`，也会赋值 `ENV_FILE`）。
    """
    return _accounts_data._estimate_slot(
        phone, load_accounts, read_env, ENV_FILE, load_env_int, sign_window_bounds)


# 账号展示序列化（mask_account）、定位与字段校验（find_account_index /
# _duplicate_phone_error / _owner_has_other_live / validate_account）、即时验证开关
# （_account_verify_enabled，转发见下方）与只读验证（_as_signin_account /
# _verify_account_clean）实现见 web/services/accounts_data.py，此处以导入区再导出保持
# m.* 可达；`_verify_account_clean` 另被本模块的转发包装与 verify_jobs 回调现取注入。


# 外呼校验的闸门与异步任务族（`VerifyGateBusy` / `VerifyQuotaExceeded` /
# `run_verify_with_gate` / `_reject_account` / `_reclaim_stale_verify_jobs` /
# `_verify_queue_full` / `_start_verify_job` / `verify_async_enabled` /
# `_account_verify_enabled`）实现见 web/services/verify_queue.py。
# 两个异常类型、失败落库、超龄收口、待办上限与建任务以导入区再导出保持 m.* 可达；
# 带闸校验与两个开关在下方转发时现取本模块的席位 / 配额判定 / 只读验证 / .env
# 路径与读取器（它们会被测试打桩或赋值改写）。
# A4 全局并发闸：进程内信号量（web 固定 -w 1，进程内即全局）。席位留在本模块：
# 静/异步两条路径共用同一个（异步侧经 verify_jobs.configure 登记）。
_verify_sem = threading.BoundedSemaphore(VERIFY_CONCURRENCY_MAX)


def run_verify_with_gate(clean, username, limits):
    """执行一次外呼校验（实现见 web/services/verify_queue.py），注入本模块现取的席位。"""
    return _verify_queue.run_verify_with_gate(
        clean, username, limits, _verify_sem, _verify_attempt_allowed, _verify_account_clean)


# 校验任务实现收在 yiban/attempt/jobs.py；注入宿主侧依赖（lambda 延迟解析，故测试可替换）。
verify_jobs.configure(
    seat=_verify_sem,
    verify_one=lambda clean: _verify_account_clean(clean),
    record_failure=lambda st, ph, err, now: _record_verify_failure(st, ph, err, now),
    mask_phone=lambda phone: _mask_phone(phone),
    reject_account=lambda phone, reason, aid, expect: _reject_account(phone, reason, aid, expect),
    queue_full=_verify_queue_full,
)


def verify_async_enabled():
    """在线校验异步开关（实现见 web/services/verify_queue.py）。

    `.env` 路径与读取器按调用时刻现取本模块的（测试会打桩 `web.app.read_env`，
    也会赋值 `ENV_FILE`）——本函数须用**本进程的** ENV_FILE，它可被 --env-file 改写。
    """
    return _verify_queue.verify_async_enabled(read_env, ENV_FILE)


def _account_verify_enabled():
    """注册/添加账号时是否做即时验证（实现见 web/services/verify_queue.py）。

    `.env` 路径与读取器按调用时刻现取本模块的（测试会打桩 `web.app.read_env`）。
    """
    return _verify_queue._account_verify_enabled(read_env, ENV_FILE)


# ---------------------------------------------------------------------------
# 管理员认证
# ---------------------------------------------------------------------------
def check_admin_configured():
    """管理员账号是否已在 .env 配置（实现见 web/security.py）。

    `.env` 路径与读取器按调用时刻现取本模块的（测试会打桩 `web.app.read_env`，
    `--env-file` 也会改写 `ENV_FILE`）。
    """
    return _security.check_admin_configured(ENV_FILE, read_env)


def _builtin_admin_loginable():
    """内置（.env）主管理员此刻是否真的进得来（实现见 web/security.py）。

    判据与 `verify_admin` 的三道 fail-closed 逐条对齐，一致性由用例对拍（防两侧漂移）。
    `.env` 路径、读取器与键行计数按调用时刻现取本模块的。
    """
    return _security._builtin_admin_loginable(ENV_FILE, read_env, _count_env_key_lines)


def verify_admin(username, password):
    """校验管理员账号（实现见 web/security.py；每次登录实时读 .env，修改立即生效）。

    `.env` 路径与读取器、键行计数、告警出口、时延拉平都按调用时刻现取本模块的
    （这些名字会被测试打桩或赋值改写，服务层另持绑定会让打桩静默失效）。
    """
    return _security.verify_admin(
        username, password, ENV_FILE, read_env, _count_env_key_lines,
        send_notification, _constant_time_dummy)


# ---------------------------------------------------------------------------
# 系统信息
# ---------------------------------------------------------------------------
def sign_status(now=None):
    """基于服务器时间计算签到状态（实现见 web/services/signstatus.py）。

    `.env` 路径、整数配置读取器与窗口解析器都按调用时刻现取本模块的。
    """
    return _signstatus.sign_status(ENV_FILE, load_env_int, _sign_window, now)


# 通知与告警邮件族（正文净化 `_nl_safe`、审计 actor 与事实 `_audit_actor` /
# `_audit_alert_facts` / `_last_cleanup_text`、变更与审核邮件 `_change_mail` /
# `_review_reject_mail`、收件人算法 `_alert_mail_recipients`、耗尽告知
# `_exhaustion_notice_mail`、开关与推送变更描述 `_mail_flags_desc` /
# `_notify_change_desc` 及随族常量）实现见 web/services/notify_mail.py，此处以导入区
# 再导出保持 m.* 可达；`send_notification` 与 `_push_ever_configured` 需要注入本模块
# 持有的名字，故在下方转发。


def send_notification(title, content, urgent=False, force=False, ledger=None):
    """发送告警通知（A 线邮件 + Webhook 双通道，实现见 web/services/notify_mail.py）。

    同类型告警邮件节流 `_mail_alert_due` 按调用时刻现取本模块的（测试会打桩
    `webapp._mail_alert_due`，服务层另持绑定会让这个桩静默失效）。
    """
    return _notify_mail.send_notification(
        title, content, urgent, force, ledger, mail_alert_due=_mail_alert_due)


# 判定"推送这路是否曾配置过"的键表与其唯一实现见 web/services/notify_mail.py，
# 此处以导入区再导出保持 m.* 可达。


def _push_ever_configured(envs=None):
    """手机推送通道在本部署历史上是否配置过（实现见 web/services/notify_mail.py）。

    `.env` 路径与读取器按调用时刻现取本模块的（测试会打桩 `web.app.read_env`、
    也会赋值 `ENV_FILE`），故转发必须在调用时刻现取后传入。
    """
    return _notify_mail._push_ever_configured(ENV_FILE, read_env, envs)


def _alert_channel_status():
    """两条告警通道的结构化可用性判据（实现见 web/services/channel_health.py）。

    "推送是否曾配置过"的判据按调用时刻现取本模块的 `_push_ever_configured`（它再现取
    本进程的 `ENV_FILE` 与 `read_env`），服务层另持绑定会让改写 `web.app.ENV_FILE`
    的测试静默失效。
    """
    return _channel_health._alert_channel_status(_push_ever_configured)


# 告警通道健康族（降级判定 `_channel_health_degraded`、状态行 `_channel_status_lines`、
# 额度描述 `_daily_budget_desc`、日报已播标记读取 `_health_report_sent_today`、事实摘要
# `_channel_health_facts`、降级痕迹 `_audit_channel_health_degraded` 与日报标记键
# `_HEALTH_REPORT_META_KEY`）的纯逻辑实现见 web/services/channel_health.py，此处以导入区
# 再导出保持 m.* 可达；需要现取本模块名字（状态生产者 / 状态行 / 告警出口）的入口在
# 下方转发。


def _channel_status_lines(status=None):
    """两条告警通道的当前状态文本行（实现见 web/services/channel_health.py）。

    默认的状态生产者按调用时刻现取本模块的 `_alert_channel_status`（它再现取
    `_push_ever_configured` 与 `.env` 状态）：服务层另持绑定会让改写 `ENV_FILE`
    的测试静默失效。
    """
    return _channel_health._channel_status_lines(_alert_channel_status, status)


def _send_channel_health_report(force=False):
    """告警通道健康日报（每日线程调用，实现见 web/services/channel_health.py）。

    状态生产者 / 状态行 / 告警出口三个入口都按调用时刻现取本模块的（既有测试在
    `web.app` 上打桩 `_channel_status_lines` 做"纯文案改版"对拍，又打桩
    `send_notification` 模拟发信失败），服务层另持绑定会让这些桩静默失效。
    """
    return _channel_health._send_channel_health_report(
        force, alert_channel_status=_alert_channel_status,
        status_lines=_channel_status_lines, send_notification=send_notification)


# 容量核计与触顶告警族（账号/用户配额判定 `_capacity_account_count` /
# `_capacity_audit_count` / `_accounts_at_capacity` / `_users_at_capacity`、容量预估
# `_capacity_estimate`、注册暂停 `_registration_paused`、同类型告警邮件节流
# `_mail_alert_due` 与触顶通知 `_notify_capacity_once`，含去重表 `_capacity_alerts`、
# 节流表 `_mail_alert_ts` / `_mail_alert_lock` 与缺省窗口常量）实现见
# web/services/capacity.py，此处以导入区再导出保持 m.* 可达；需要现取本模块名字
# （`.env` 路径与读取器、缺省上限、窗口/裁剪口径、告警出口）的入口在下方转发。
def _capacity_estimate(gap=0):
    """按当前窗口与账号间隔预估可容纳账号数（实现见 web/services/capacity.py）。

    窗口解析器与掐头去尾口径按调用时刻现取本模块的（测试会打桩 `web.app._sign_window`
    与 `web.app.edge_config`），故转发必须现取后传入。
    """
    return _capacity._capacity_estimate(gap, sign_window=_sign_window, edge_config=edge_config)


def _accounts_at_capacity(extra_accounts=0):
    """账号配额判定（实现见 web/services/capacity.py）。

    `.env` 路径、整数读取器与缺省上限按调用时刻现取本模块的（测试会赋值 `ENV_FILE`、
    打桩 `read_env` / `load_env_int`），故转发必须现取后传入。
    """
    return _capacity._accounts_at_capacity(
        extra_accounts, env_file=ENV_FILE, load_env_int=load_env_int,
        max_accounts_default=DEFAULT_MAX_ACCOUNTS)


def _registration_paused():
    """注册是否处于暂停状态（实现见 web/services/capacity.py）。

    `.env` 路径与读取器按调用时刻现取本模块的（测试会赋值 `ENV_FILE` / 打桩
    `read_env`），故转发必须现取后传入。
    """
    return _capacity._registration_paused(ENV_FILE, load_env_int)


def _users_at_capacity():
    """用户配额判定（实现见 web/services/capacity.py）。

    `.env` 路径、整数读取器与缺省上限按调用时刻现取本模块的（测试会赋值 `ENV_FILE`、
    打桩 `read_env` / `load_env_int`），故转发必须现取后传入。
    """
    return _capacity._users_at_capacity(
        env_file=ENV_FILE, load_env_int=load_env_int, max_users_default=DEFAULT_MAX_USERS)


def _mail_alert_due(title):
    """同类型告警邮件节流判断（实现见 web/services/capacity.py）。

    `.env` 路径与读取器按调用时刻现取本模块的（测试会赋值 `ENV_FILE` / 打桩
    `read_env`），故转发必须现取后传入。
    """
    return _capacity._mail_alert_due(title, ENV_FILE, load_env_int)


def _notify_capacity_once(kind, limit, label):
    """容量触顶通知（实现见 web/services/capacity.py）。

    告警出口按调用时刻现取本模块的 `send_notification`（测试会打桩
    `webapp.send_notification`），服务层另持绑定会让那些桩静默失效。
    """
    return _capacity._notify_capacity_once(
        kind, limit, label, send_notification=send_notification)


# ---------------------------------------------------------------------------
# Flask 应用
# ---------------------------------------------------------------------------
# 应用版本号（页面底部显示；0.x 阶段递增规则：里程碑级功能波次 +0.1.0 / 修复与微调 +0.0.1 / 大版本暂不升 1）
# 2026-08-16 运维体系收尾：备份含日志/状态清理/设置审计/耗时记录/缓存优化（0.19.7）
# 2026-08-17 清理任务权限事故修复：cleanup 独立日志 + cron 改 yiban 用户（0.20.8）
# 2026-08-17 全量审查第一批修复：事务锁+时间戳+宽限期+密码泄露（0.20.9）
# 2026-08-17 全量审查第二批修复：AES弱密钥检测+CSP nonce+systemd加固+flock路径+migrate_v5+备份加密+测试补齐（0.20.10）
# 2026-08-17 全量审查第三+四批修复：中/低严重度问题全面清理（0.20.11）
# 2026-08-17 全量审查 0.21.0 修复：版本号更新
# 2026-08-17 全局暂停签到 + 备案信息预留区（0.21.1）
# 2026-08-17 合规文档接入网页 + 部署者模板化 + 渲染修复（0.21.2）
# 2026-08-20 审查修复（XSS/同意校验/缓存/状态语义）+ 掐头去尾前后独立可配（0.21.3）
# 2026-08-21 对抗性审查修复：空凭据管理员登录 + idx 防错位 + 读路径清理外移 + 审计链
#           BEGIN IMMEDIATE + 注册时延拉平 + my-* 单快照/日志脱敏 + HSTS/Permissions-Policy
#           + --host 默认回环（0.21.4）
# 2026-08-23 新增 Docker 部署能力（0.22.0）
# 2026-08-23 系统设置页容量统计口径修正（0.22.1）
# 2026-08-24 邮箱通知（SMTP）：管理员告警邮件 A 线 + 用户签到失败邮件 B 线 + 用户端开关（0.23.0）
# 2026-08-26 界面动效审查修复：过渡属性收敛、抽屉遮罩淡入与曲线、登录页切换统一、Toast 动效、reduced-motion 支持（v0.2.7 内并入）
# 2026-08-29 通知推送与账户安全加固：消息推送组件（Server酱/自定义 URL，加密配置）+ 高危告警邮件节流 + 高危删除冷却 + 删除二次鉴权（v0.3.0）
# 2026-08-31 安全修复 + 公测反馈：告警通道二次鉴权、推送额度分账、账号清除门禁、登录留痕、口令策略口径、失效会话自动重登、新申请提醒（v0.3.0 内并入）
# 2026-09-01：注册暂停开关、web 日志落盘、notify 账本单锁化、重置密码二次鉴权、
# 批量签到冷却、无点位独立状态、节流跨进程化、迁移原子性、e2e 契约刷新（v0.3.1）
# 2026-09-06：文档页脚本注入转义、告警通道参数收口+先告警后落盘、改绑回审、
# 历史数据隔离、清空账号门禁、注册文案统一、cookie path、签到冷却单源、批量上限与超时、
# 告警兜底与额度分账、日志单写、超时钳位（v0.3.2）
# 2026-09-06：审核拒绝邮件触达提交者、待处理列待审核置顶/已拒绝沉底、账号弹窗改「内容区滚动+按钮常驻」修小视口按钮截断（v0.3.2 内并入）
# 2026-09-07 移除 TUI 终端面板（账号配置统一走网页后台）+ 内部冗余收敛：.env 解析单一实现、
# write_env 安全校验单源化、删除未引用字体切片（v0.3.3）
# 2026-09-08 安全加固：.env 行边界判定单源化+提权链封堵、告警通道实际可用性判定、
# 日志导出脱敏副本+审计限速、审计链锚点进健康日报、容量口径单档化（v0.3.4）
# 2026-09-09 告警收件人网页可编辑（admin_to 写路径+旧收件人变更通知）、
# 站点分享摘要 meta/og、表单占位字号统一（v0.3.4 内并入）
# 2026-09-10 补签改为进程内第二轮（与容器同语义，失败账号当天即得第二次尝试）、
# 管理端软删账号纳入高危限速并即时通知、设置页账号容量三分类明细、
# 签到重试日志补记失败原因、注销恢复保留自选时间片（v0.3.4）
# 2026-09-13 前端整体重写（导航分组/路由统一/列表标签页化/自研日期时间控件/暗色与可达性/
# 加载错误态与重试/系统开关口令真校验）；历史版本号已压缩重编号（0.1.0–0.3.4）
# 2026-09-14 运营面收口（错误页/爬虫协议/站标族）+ 容量口径统一（容量与保存门同源）
# + 密钥轮换强制参数生效 + 总览成功率数字着色与空态字号修复（v0.4.1）
# 2026-09-16 容量口径与数据恢复修正（v0.4.3）：未通过审核不占账号容量 + 审核通过过闸门
# + 注销恢复带回账号（时间戳错位）+ 按日状态文件清理收口（宿主/容器同一套规则）
# + 容器时段标记原子化 + 告警末轮时刻与补签时刻对齐
# 2026-09-15 后端修复批次（v0.4.2）：时区口径（UTC 主机不再整日漏签）+ 运行期账号复核
# + 在线校验三缺陷 + 窗口单一口径与容量预检 + 熔断状态读改写原子化 + 镜像补拷共享包
# 2026-09-16 结构与通知拆分（v0.4.4）：登录/签到协议层独立（yiban/fyiban/protocol.py，
# 安全校验以策略注入）+ 客户端外观（yiban/client.py）+ 安全策略层（yiban/security.py）
# + 通知与邮件拆为 yiban/notify 与 yiban/mail
# 版本号：由 yiban/__init__.py 的 __version__ 唯一提供（上文已导入为 APP_VERSION）。
# 改版本时的连带项：根目录 CHANGELOG.md + web/__init__.py（转出）+ 版本门禁用例。
# 页面失效版本：每次启动变化，供前端"版本失效自动刷新"兜底（防止缓存旧页面）
WEB_VERSION = clock.now().strftime("%Y%m%d%H%M%S")


def _is_loopback_host(host):
    """判断监听地址是否为回环（用于 H6 非回环 Secure Cookie 强警告）。"""
    h = str(host or "").strip().lower()
    return h in ("127.0.0.1", "::1", "localhost") or h.startswith("127.")


# .env 歧义键启动检测：每进程只报一次（同 _notify_capacity_once 的节流思路）。
# 闩留在本模块（测试按 `webapp._env_collision_reported = False` 复位它来逐例复现启动）；
# 检测、ERROR 日志与告警正文的实现见 web/services/env_io.py，告警出口现取本模块的
# `send_notification`（它带着既有的节流与账本语义）。
_env_collision_reported = False


def _report_env_key_collisions(env_path):
    """启动时报告 .env 的行模型歧义键（实现见 web/services/env_io.py）。只检测不改写。"""
    global _env_collision_reported
    if _env_collision_reported:
        return
    _env_collision_reported = True
    return _env_io_svc._report_env_key_collisions(env_path, send_notification)


# ---------------------------------------------------------------------------
# 子路径 / 独立子域 前缀自适应中间件（2026-08-23）
# ---------------------------------------------------------------------------
# 背景：本应用可部署在域名根、独立子域、或主站子路径（如 /tools/yiban-auto-sign/demo/）下。
# 部署契约：反向代理只需把完整 URI【原样透传】（proxy_pass 后面不要加 "/" 去剥前缀），
# 本中间件即可自动感知挂载前缀并重写 SCRIPT_NAME / PATH_INFO，使：
#   · url_for() 自动带上前缀（服务端 redirect 改用 url_for 即可，见页面路由）；
#   · 应用内部 request.path 仍是干净路径（现有 /static/、/api/ 判断无需改动）；
#   · Flask 的 strict_slashes 补斜杠跳转自动带前缀。
# 前缀判定优先级：代理已传 SCRIPT_NAME（WSGI 契约，直接放行）> 环境变量 YIBAN_BASE_PATH > 自动探测。
# 自动探测采用【最短（首个）命中】前缀：优先把 /api/、/static/、页面路由等应用自身路由留在
# 剩余路径里（如 /tools/yiban-auto-sign/demo/api/login 应切成前缀 + /api/login，而非 .../api + /login）。
# 若挂载前缀本身恰好含 /api、/static 或页面名等会与应用路由撞车的段，自动探测可能切错，
# 此时请用 YIBAN_BASE_PATH 显式指定前缀（见 deploy 文档）。
# 约定：子路径首页请带尾斜杠访问（.../demo/，url_for 生成的首页地址即带斜杠）；
# 不带尾斜杠的裸路径无法可靠区分“子路径首页”与“根路径 404”，按 404 处理（防误伤根部署）。
class BasePathMiddleware:
    # 应用的扁平路由标记（新增顶层页面 / 接口前缀需同步追加）。
    # _ROOT_PREFIXES 按"路径段前缀"比对，覆盖 `/组/页面` 两级路由（data/work/my 为分组）。
    # _ROOT_MARKERS 是**根级端点**的完整路径：探测据此判断"这段之后已是应用自身路由"。
    # 新增任何根级路由（不带分组前缀）都必须登记，否则子路径部署下会被当成挂载前缀的一部分
    # 而 404（图标、旧路径重定向均属此类）——test_subpath_deploy 的 url_map 元测试兜底。
    _ROOT_MARKERS = (
        "/login", "/user", "/terms", "/privacy",
        "/favicon.png", "/gongan-beian.png", "/robots.txt",
        # 改版前的旧路径（历史书签兼容，302 到 /组/页面）
        "/logs", "/accounts", "/users", "/settings", "/mine", "/mine/calendar",
    )
    _ROOT_PREFIXES = ("/api/", "/static/", "/data/", "/work/", "/my/", "/user/")

    def __init__(self, wsgi_app, base_path=None):
        self.wsgi_app = wsgi_app
        self.base_path = base_path  # 显式前缀（优先于自动探测）；None 则自动

    def __call__(self, environ, start_response):
        # WSGI 契约：代理已设 SCRIPT_NAME 时，PATH_INFO 已相对该脚本路径，直接放行
        if (environ.get("SCRIPT_NAME") or "").strip("/"):
            return self.wsgi_app(environ, start_response)
        path = environ.get("PATH_INFO", "/")
        prefix = self._resolve_prefix(environ, path)
        if prefix:
            rest = path[len(prefix):]
            if not rest:
                rest = "/"
            environ["SCRIPT_NAME"] = prefix
            environ["PATH_INFO"] = rest
        return self.wsgi_app(environ, start_response)

    def _resolve_prefix(self, environ, path):
        # 显式配置（构造参数 > 环境变量 YIBAN_BASE_PATH）；仅当路径确实以该前缀开头才生效
        configured = (self.base_path or os.environ.get("YIBAN_BASE_PATH", "") or "").strip().strip("/")
        if configured:
            configured = "/" + configured
            if path == configured or path.startswith(configured + "/"):
                return configured
        # 自动探测（零配置默认路径）
        return self._detect_prefix(path)

    @classmethod
    def _detect_prefix(cls, path):
        # 根路径部署：本身就是首页或已知路由 → 无前缀
        if path == "/" or path in cls._ROOT_MARKERS:
            return ""
        # 以 "//" 开头的路径不是合法挂载前缀：切出来的前缀会进 SCRIPT_NAME，
        # 而 url_for() 会把它拼成协议相对地址（`//evil.com/login`）——开放重定向。
        # 反代通常合并重复斜杠（nginx merge_slashes on）故难触发，但直连时成立，直接拒绝。
        if path.startswith("//"):
            return ""
        if path.startswith(cls._ROOT_PREFIXES):
            return ""
        # 按 "/" 边界切分，取【首个】命中：剩余部分为 "/"（子路径首页带尾斜杠）、
        # 已知路由、或应用自身的路由前缀时，切掉的部分即前缀（最短=最先命中）
        pos = path.find("/", 1)
        while pos != -1:
            rest = path[pos:]
            if (rest == "/" or rest in cls._ROOT_MARKERS
                    or rest.startswith(cls._ROOT_PREFIXES)):
                prefix = path[:pos]
                # 前缀本身必须是规范的绝对路径（无空段、无 ".."、"。"、反斜杠），
                # 否则一律当根部署处理，不回填 SCRIPT_NAME
                if "//" in prefix or any(seg in (".", "..") for seg in prefix.split("/")) or "\\" in prefix:
                    return ""
                return prefix
            pos = path.find("/", pos + 1)
        return ""


# 公开模板默认字面量口令表（_DEFAULT_ADMIN_LITERALS）与启动检测
# reject_default_admin_password 的实现见 web/security.py，此处以导入区再导出 /
# 转发包装保持 m.* 可达与打桩面。
# 主管理员（内置 .env 管理员）口令单独提档：12 位三类——最高权限账号的口令强度要求
# 高于普通用户原口径，启动 fail-closed 与自助改密（_admin_password_policy_error）共用
# 同一组常量；常量本体随口令策略搬入 web/services/accounts_data.py，此处以导入区再导出
# 保持两处口径同源（不要再写字面量）。


def reject_default_admin_password(env_path):
    """启动检测内置管理员弱口令（实现见 web/security.py）。

    `.env` 读取器按调用时刻现取本模块的（测试会打桩 `web.app.read_env`）。
    本函数由 create_app 启动路径调用（多 worker 各自执行），自带 `env_path`
    参数、不读请求上下文。
    """
    return _security.reject_default_admin_password(env_path, read_env)


def create_app(host=None):
    global _purge_loop_started
    # v0.26.3：web 进程日志并入按天日志文件（与 signin 子进程同口径）。
    # gunicorn 走 create_app 不执行 main()，此前 root logger 无文件 handler：
    # INFO 被丢弃、WARNING 只进 journald，邮件/推送/DB 的告警在后台日志页
    # 与 sign-*.log 里都看不到。挂载幂等：已挂且目录一致则复用；目录变化
    # （测试环境各用例独立 tmp）则替换，防句柄指向已删除目录反复写失败。
    _log_dir = os.path.dirname(LOG_FILE)
    with contextlib.suppress(OSError):
        os.makedirs(_log_dir, exist_ok=True)  # 目录建不出来时交由 handler 的降级路径（emit 失败不阻断 web）
    _root_logger = logging.getLogger()
    _existing_fh = [
        _h for _h in _root_logger.handlers
        if isinstance(_h, DailyFlockFileHandler)
    ]
    if _existing_fh and _existing_fh[0]._log_dir != _log_dir:
        _root_logger.removeHandler(_existing_fh[0])
        with contextlib.suppress(Exception):
            _existing_fh[0].close()
        _existing_fh = []
    if not _existing_fh:
        try:
            _daily_fh = DailyFlockFileHandler(_log_dir)
        except OSError:
            # 日志目录不可写时构造失败不得阻断 web 启动——仅告警降级
            # （与上文注释"降级路径"口径一致：emit 失败不阻断业务，这里连挂载都失败）
            logger.warning(
                "无法创建按天日志 handler（目录 %s 不可写或不存在）——web 日志不落盘，"
                "仅输出到 stderr/标准日志通道", _log_dir)
            _daily_fh = None
        if _daily_fh is not None:
            _daily_fh.setFormatter(logging.Formatter(
                "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            _root_logger.addHandler(_daily_fh)
    # root 保持 WARNING，避免 requests/urllib3/werkzeug 等第三方库 INFO
    # 全量落盘且无轮转上限；仅对本项目自有组件单独放开 INFO（每次 create_app 幂等设置）
    _root_logger.setLevel(logging.WARNING)
    for _name in ("yiban", "web", "notify", "mailer", "db", "scheduler",
                  "account_crypto"):
        logging.getLogger(_name).setLevel(logging.INFO)
    for _name in ("requests", "urllib3", "werkzeug", "gunicorn"):
        _third = logging.getLogger(_name)
        _third.setLevel(logging.WARNING)
        _third.addHandler(logging.NullHandler())
    # .env 歧义键检测必须先于一切 .env 写入（口令迁移 / init_db 落盐 /
    # ensure_secret_key）——升级后第一次重启本身就是实体化器（见
    # _report_env_key_collisions：只报告不改写，每进程一次）
    _report_env_key_collisions(ENV_FILE)
    # 默认/弱口令启动检测（必须在口令明文→哈希迁移之前，
    # 迁移会把明文清空导致无从检查）
    reject_default_admin_password(ENV_FILE)
    # 启动安全迁移：管理员口令明文 → scrypt 哈希（幂等，多 worker 并发写同口令哈希无害）
    migrate_admin_password_to_hash(ENV_FILE)
    # SQLite 数据层初始化：首次启动自动迁移 accounts.json/users.json → yiban.db（幂等，
    # JSON 改名 .bak 保留逃生门）；多 worker 各自调用幂等（模块级连接缓存）
    db.init_db(DB_FILE, migrate_from=ACCOUNTS_FILE, env_file=ENV_FILE)
    # 启动期收口超龄校验任务：上一次进程若在任务执行中消失（重启/重部署/OOM），
    # 任务会永久停在 running 并占用待办名额（累计到上限后在线校验永久 503、
    # 不自愈）。收口本身失败只告警，不阻断启动。
    _reclaim_stale_verify_jobs()
    app = Flask(__name__)
    # Lucide 图标精灵图：启动时一次性读入并注册为 Jinja 全局 `lucide_sprite`，
    # 模板经 macros/ui.html 的 sprite() 宏原样输出（每次渲染零文件 I/O，见 ui.html 顶部说明）。
    # 读取失败只降级为空串（图标不显示、页面不报错），不阻断启动。
    try:
        with open(
            os.path.join(app.static_folder, "vendor", "lucide", "_sprite.svg"),
            "r",
            encoding="utf-8",
        ) as _sprite_f:
            app.jinja_env.globals["lucide_sprite"] = Markup(_sprite_f.read())
    except OSError as _sprite_err:
        app.jinja_env.globals["lucide_sprite"] = Markup("")
        logger.warning("Lucide 图标精灵图读取失败，图标将不可见：%s", _sprite_err)
    app.config["SECRET_KEY"] = ensure_secret_key(ENV_FILE)
    app.config["SESSION_COOKIE_NAME"] = "yiban_admin"
    app.config["SESSION_COOKIE_HTTPONLY"] = True  # JS 不可读 session cookie（防 XSS 窃取）
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # 跨站请求不携带 cookie（防 CSRF）
    # H6：Secure 标志可由 YIBAN_COOKIE_SECURE 显式开启（env 文件或环境变量，1/true/yes 开，
    # 默认关）。默认关保持本机 HTTP 直连演示可用；生产 systemd 模板置 1。
    cookie_secure_raw = os.environ.get("YIBAN_COOKIE_SECURE")
    if cookie_secure_raw is None:
        cookie_secure_raw = read_env(ENV_FILE).get("YIBAN_COOKIE_SECURE", "")
    cookie_secure = str(cookie_secure_raw).strip().lower() in ("1", "true", "yes", "on")
    app.config["SESSION_COOKIE_SECURE"] = cookie_secure
    # 子路径部署收窄会话 Cookie 作用域——读取 .env 的
    # YIBAN_BASE_PATH（显式配置形态），值非空且非 "/" 时把 SESSION_COOKIE_PATH
    # 设为该前缀（统一补尾斜杠），登录 Cookie 不再下发到同域其他路径下的应用。
    # 说明：BasePathMiddleware 的"自动探测"形态（未设 YIBAN_BASE_PATH）在请求期
    # 才能感知前缀，应用启动时无法可靠得知，故此处不强行处理——需要收窄 Cookie
    # 的子路径部署请显式设置 YIBAN_BASE_PATH（同时可避免自动探测与应用路由段撞车）。
    _base_path_env = read_env(ENV_FILE).get("YIBAN_BASE_PATH", "").strip()
    if _base_path_env and _base_path_env != "/":
        app.config["SESSION_COOKIE_PATH"] = "/" + _base_path_env.strip("/") + "/"
    # HTTPS 反代自动升级 Secure——请求经 https 到达而 Secure 未显式开启时，给本次响应
    # 的会话 Cookie 带上 Secure 标志（逐请求判定，不需要重启也不粘住进程）。
    # **显式**配置 YIBAN_COOKIE_SECURE（含显式 0）的部署完全不参与自动判定，保持原行为。
    #
    # 两条判据都不能省：
    # 1) 只有**键在且非空**才算"显式配置"——把"未配置"与"显式关"混成一个 False 会让
    #    默认部署永不自动升级（HTTPS 反代下 Cookie 一直不带 Secure）；
    # 2) 转发头只在**第一跳可信**（remote_addr 落在 TRUSTED_PROXIES）时才采信。判据与
    #    `_client_ip` 同源：直连形态下客户端能自己发 `X-Forwarded-Proto: https`，粘性
    #    采信会把这个进程的会话 Cookie 永久粘成 Secure，站点退回 HTTP 后浏览器不再回传
    #    Cookie，表现为"登录不上"。
    #
    # 逐请求而非粘性：反代头消失（拓扑变更、代理降级为纯 HTTP）时必须能跟着回落，否则
    # 进程会继续发已不可用的 Secure Cookie。同进程内不同请求可给出不同判定——每个响应
    # 只按"自己这一跳"是否 https 决定，这正是要的语义。
    _cookie_secure_explicit = bool(str(cookie_secure_raw or "").strip())
    _secure_auto_notice = {"logged": False}

    def _forwarded_proto_is_https():
        """`X-Forwarded-Proto` 是否声称本次请求走 https（仅可信第一跳采信）。

        取首段（逗号分隔链里最靠近客户端的那一跳），与 `_client_ip` 读 XFF 的口径一致。
        """
        if (request.remote_addr or "") not in TRUSTED_PROXIES:
            return False
        raw = request.headers.get("X-Forwarded-Proto", "")
        return bool(raw) and raw.split(",")[0].strip().lower() == "https"

    @app.before_request
    def _auto_secure_on_https():
        if _cookie_secure_explicit:
            return
        want = bool(request.is_secure or _forwarded_proto_is_https())
        if app.config.get("SESSION_COOKIE_SECURE") != want:
            app.config["SESSION_COOKIE_SECURE"] = want
        if want and not _secure_auto_notice["logged"]:
            _secure_auto_notice["logged"] = True
            logger.info("检测到 HTTPS（或可信反代的转发头），会话 Cookie 自动启用 Secure")
    if host is not None and not _is_loopback_host(host) and not cookie_secure:
        logger.warning(
            "YIBAN_COOKIE_SECURE 未开启：当前监听地址 %s 非回环，生产环境请设置 "
            "YIBAN_COOKIE_SECURE=1（.env 或环境变量），否则登录 Cookie 可能在 HTTPS 下被浏览器拒绝",
            host,
        )
    app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 14  # 14 天（折中：安全与管理员便利平衡）

    # ---- 会话绝对过期上限（2026-08-27 对抗性审查 P2-5）----
    # 滑动续期防不了「被盗 Cookie 永久续命」：任何会话自登录起最多存活 N 天，
    # 到期硬失效需重新登录。YIBAN_SESSION_ABS_DAYS 可配，越界回退默认并告警
    # （风格对齐 M13 会话缓存 TTL 钳制）。判定逻辑见 _current_role。
    global SESSION_ABS_TTL_SECONDS
    _abs_days = load_env_int(ENV_FILE, "YIBAN_SESSION_ABS_DAYS", SESSION_ABS_DAYS_DEFAULT)
    if _abs_days < 1 or _abs_days > 30:
        logger.warning(
            "YIBAN_SESSION_ABS_DAYS=%s 越界（允许 1~30 天），回退默认 %d 天",
            _abs_days, SESSION_ABS_DAYS_DEFAULT,
        )
        _abs_days = SESSION_ABS_DAYS_DEFAULT
    SESSION_ABS_TTL_SECONDS = _abs_days * 86400

    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # 请求体上限 64KB

    # 登录失败记录 {ip: [fail_count, lock_until]}
    # 可变状态挂在 app.extensions（而非模块级）：测试进程反复 create_app，进程级
    # 共享会把上一个实例的失败计数与 lock 带进下一个。登录、改密、注销、恢复四条
    # 路由共用**同一份**字典（安全语义依赖同一份账），取用点收在 web/routes 包。
    app.extensions["yiban_login_fails"] = {}
    # 敏感操作口令复核失败的**独立**计数 {fail_key: [count, window_start]}（M5）：
    # 与登录失败表分开——P18 教训是"持 Cookie 者若写共享计数可把管理员锁出登录"，
    # 故高危二次鉴权/开关门/执行体门的失败只走本计数 + 首达阈值告警，绝不碰登录计数。
    _sensitive_pw_fails = {}
    # 门禁级冷却 {(ip, 用户名): (失败次数, 解锁时刻)}：独立计数达阈值后，解锁时刻之前
    # 一律拒绝需要复核的写操作（见 _sensitive_password_gate）。刻意与登录失败表的
    # lock_until 分开两份账——冷却封的是"高危写"，登录与只读必须照常，反之亦然。
    # 值末位统一是时间戳，故写入路径的 _ip_store_trim 能按同口径回收（防无界增长）。
    _sensitive_pw_cooldown = {}
    # 全局限速记录 {ip: [count, window_start]}
    _rate_limits = {}
    # 已登录 GET 的独立计数桶（与严格桶分开，互不挤占；见 rate_limit 说明）
    _rate_limits_get = {}
    # 注册限速记录 {ip: [count, window_start]}（仅注册接口使用，状态挂 extensions 保每实例语义）
    app.extensions["yiban_register_limits"] = {}
    # 登录频率限制 {ip: [count, window_start]}：比全局限速更严，防换用户名密码喷洒
    app.extensions["yiban_login_rate"] = {}
    # 账号恢复的每 IP 聚合失败窗口 {ip: [count, window_start]}（仅恢复接口使用，
    # 状态挂 extensions 保每实例语义）
    app.extensions["yiban_restore_fail_rate"] = {}
    # 账号验证尝试配额 {username.lower(): (count, window_start)}（2026-08-27 P1-2）
    # 管理员添加与用户自助提交共用同一份账；状态挂 extensions 保每 app 实例一份，
    # 取用点收在 web.routes.verify_limits()
    app.extensions["yiban_verify_limits"] = {}
    # 账号验证认证失败冷却 {phone: (fails, window_start, cooldown_until)}（2026-09-04 生产复盘）
    # 同上：两条提交路径共用，取用点 web.routes.verify_fails()
    app.extensions["yiban_verify_fails"] = {}
    # 高危删除操作冷却 {username.lower(): (count, window_start)}（2026-08-29）
    _admin_delete_limits = {}
    # 日志导出限速 {ip: (count, window_start)}
    # 状态挂 extensions 保每 app 实例一份，取用点 web.routes.export_limits()
    app.extensions["yiban_export_limits"] = {}
    # 账号详情读取限速 {actor: (count, window_start)}（按会话而非 IP：校园网出口
    # 高度共享，按 IP 会把两个管理员的运维互相挡死，与"新 IP 即告警"同一理由）
    # 状态挂 extensions 保每 app 实例一份，取用点 web.routes.detail_limits()
    app.extensions["yiban_detail_limits"] = {}
    # 只读面聚合审计计数 {(actor, 资源类): (count, window_start)}
    _read_audit_counts = {}
    # 只读面聚合审计的目标摘要 {(actor, 资源类): [脱敏目标样本, 目标总数, window_start]}
    _read_audit_targets = {}
    # 只读面"超限被拒"留痕 {(actor, 资源类): (count, window_start)}：limit=1 即
    # "每个窗口至多一行"——被拒的那一侧绝不能逐条写，否则又被当成免费打字机
    _read_audit_denied = {}

    def _read_audit_trace(action, target=""):
        """只读面留痕：按 (actor, 资源类) 在窗口内聚合成一行，不逐请求写。

        target 必须是**已脱敏**的短标识（_mask_phone/_mask_email 或"N 条"这类
        聚合量），因为它进审计表、随日志页与备份包外流。

        拒绝面（超限 429）走 _read_audit_denied 的另一条 limit=1 计数，同样
        复用 _bump_window_count——全项目只有一份窗口计数实现。
        """
        actor = (session.get("username") or "?")[:64]
        key = (actor, action)
        now = time.time()
        with _rate_lock:
            _ip_store_trim(_read_audit_counts, READ_AUDIT_WINDOW + _IP_STORE_MAX_AGE)
            _ip_store_trim(_read_audit_targets, READ_AUDIT_WINDOW + _IP_STORE_MAX_AGE)
        cnt, start, _ = _bump_window_count(
            _read_audit_counts, key, now, READ_AUDIT_WINDOW)
        with _rate_lock:
            # 摘要与计数同窗口：_bump_window_count 翻窗时给出的 start 是新窗口起点，
            # 与存量摘要的 start 不一致即丢弃旧摘要（旧窗口的行已在首行时落库）
            entry = _read_audit_targets.get(key)
            if not entry or entry[2] != start:
                entry = _read_audit_targets[key] = [[], 0, start]
            if target and target not in entry[0]:
                entry[1] += 1
                if len(entry[0]) < READ_AUDIT_TARGET_CAP:
                    entry[0].append(target)
        if not _read_audit_row_due(cnt):
            return
        shown = "、".join(entry[0]) or "-"
        if entry[1] > READ_AUDIT_TARGET_CAP:
            shown += f" 等 {entry[1]} 个"
        db.audit(actor, action, db.hash_ip(_client_ip()),
                 f"窗口内累计第 {cnt} 次读取；目标 {shown}")

    def _read_audit_denied_trace(action):
        """超限被拒只留一行/窗口：既不能无痕，更不能被拿去刷表。"""
        actor = (session.get("username") or "?")[:64]
        key = (actor, action)
        with _rate_lock:
            _ip_store_trim(_read_audit_denied, READ_AUDIT_WINDOW + _IP_STORE_MAX_AGE)
        _cnt, _start, first = _bump_window_count(
            _read_audit_denied, key, now=time.time(), window=READ_AUDIT_WINDOW, limit=1)
        if not first:
            return
        db.audit(actor, action, db.hash_ip(_client_ip()),
                 "读取超限被拒（本窗口仅留此一行）")

    # _ip_store_trim（上提为模块级，见 _bump_window_count 上方）：
    # 各限速表写入路径统一调用，防公网扫描器用海量键打爆内存。

    # ---- 全局限速：防脚本轰炸 API（2026-08-16 用户决策：只对 /api/* 限速，
    # 页面/静态放宽，避免 302+200 双请求导致正常页面浏览被误伤）----
    @app.before_request
    def rate_limit():
        if not request.path.startswith("/api/"):
            return
        ip = _client_ip()
        now = time.time()
        # 分级：已登录 GET 走放宽的独立桶（页面首屏并发 + 快速切页），写路径与匿名
        # 请求走严格桶（脚本轰炸主防线）。两桶独立计数，正常浏览不会互相挤占。
        relaxed = request.method == "GET" and _current_role() is not None
        if relaxed:
            with _rate_lock:
                _ip_store_trim(_rate_limits_get, _IP_STORE_MAX_AGE)
            cnt, _start, _allowed = _bump_window_count(_rate_limits_get, ip, now, RATE_WINDOW)
            if cnt > RATE_MAX_AUTH_GET:
                return jsonify({"error": "请求过于频繁，请稍后再试"}), 429
            return
        with _rate_lock:
            _ip_store_trim(_rate_limits, _IP_STORE_MAX_AGE)
        cnt, _start, _allowed = _bump_window_count(_rate_limits, ip, now, RATE_WINDOW)
        if cnt > RATE_MAX:
            return jsonify({"error": "请求过于频繁，请稍后再试"}), 429

    # ---- 认证守卫：/api/* 需登录；普通用户仅限 my-* 与 clock ----
    @app.before_request
    def require_login():
        if not request.path.startswith("/api/"):
            return
        if request.path in ("/api/login", "/api/register", "/api/me/restore"):
            return
        # 公告/更新日志读取对所有用户开放（含未登录，登录页也显示）；
        # 注册暂停状态（v0.26.3）供登录页决定是否禁用注册入口，同口径公开
        if request.path in ("/api/announcement", "/api/changelog") and request.method == "GET":
            return
        if request.path == "/api/registration_paused" and request.method == "GET":
            return
        role = _current_role()
        if role is None:
            return jsonify({"error": "未登录"}), 401
        if role == "admin":
            return
        # 普通用户：只能操作自己的账号（/api/my-*）、读取时钟、查询身份与登出
        if request.path.startswith("/api/my-") or request.path.startswith(
            "/api/verify-jobs"
        ) or request.path in (
            "/api/clock",
            "/api/me",
            "/api/logout",
            "/api/me/password",
            "/api/me/delete",
        ):
            return
        # 越权尝试留痕——已登录普通用户命中管理面路径是盗号/滥用
        # 的最高信号之一，此前 403 零留痕。IP 经 hash_ip 匿名化；频次天然受
        # /api/* 全局限速约束，且普通用户正常操作不会触达本分支。
        db.audit(
            _audit_actor(),
            "forbidden_path",
            db.hash_ip(_client_ip()),
            _nl_safe(request.path)[:120],
        )
        return jsonify({"error": "无权限"}), 403

    # ---- CSRF 防护：登录后所有写请求（POST/PUT/DELETE）必须携带与 session 匹配的 token ----
    # 登录/注册无需 token（未登录态，跨站表单攻击由 SameSite=Lax 已基本阻断）；
    # 已登录用户的写操作由 token 双重校验（借鉴 flask-wtf 的 Session 方案，自实现零依赖）。
    # token 的生成随个人域路由迁往 web/routes/me.py（唯一使用点），校验仍在下面。
    def _is_same_origin():
        """登录/注册等未登录写接口的同源校验：跨站表单提交的 POST 必然携带 Origin 头。

        浏览器同源 fetch POST 也携带 Origin；无 Origin 的请求（同站导航、curl）放行。
        """
        origin = request.headers.get("Origin")
        if not origin:
            return True
        from urllib.parse import urlparse

        try:
            o = urlparse(origin)
        except ValueError:
            return False
        return (o.scheme, o.netloc) == (request.scheme, request.host)

    # NEW-M1（反代转发头误诊断）：Origin 为 https 而应用侧 request.scheme 为 http
    # 时，说明反向代理的 X-Forwarded-Proto 未生效（nginx 缺 proxy_set_header，或
    # gunicorn 未信任代理头），同源校验会把全部正常登录/注册误判为跨站拒绝。
    # 首次命中输出一次性 ERROR 指引排查（模块级标记，重启后重置）。
    _forwarded_proto_mismatch_logged = False

    def _log_forwarded_proto_mismatch_once():
        """同源校验拒绝时附带 NEW-M1 一次性诊断（Origin=https / scheme=http）。"""
        nonlocal _forwarded_proto_mismatch_logged
        if _forwarded_proto_mismatch_logged:
            return
        origin = request.headers.get("Origin", "")
        from urllib.parse import urlparse

        try:
            o = urlparse(origin)
        except ValueError:
            return
        if o.scheme == "https" and request.scheme == "http":
            _forwarded_proto_mismatch_logged = True
            logger.error(
                "NEW-M1：请求 Origin 为 https 但应用侧 scheme 为 http——反向代理转发头"
                "未生效，同源校验将拒绝所有正常登录/注册。请检查：nginx 配置需含 "
                "proxy_set_header X-Forwarded-Proto $scheme；gunicorn 需信任代理头"
                "（forwarded_allow_ips 包含代理地址，如 --forwarded-allow-ips=127.0.0.1）"
            )

    @app.before_request
    def check_csrf():
        if request.method not in ("POST", "PUT", "DELETE"):
            return
        if not request.path.startswith("/api/"):
            return
        if request.path in ("/api/login", "/api/register", "/api/me/restore"):
            # 未登录态无 session token：用同源校验阻断跨站 CSRF（与登录/注册同等级）
            if not _is_same_origin():
                _log_forwarded_proto_mismatch_once()  # NEW-M1 反代头未生效诊断
                # 告警行落按天 sign-*.log 并经 /api/logs 与导出可见：IP 与审计
                # 同口径 hash_ip 匿名化（哈希形态足以关联同一来源的连续告警）；
                # 限速等内存计数仍用裸 IP
                logger.warning(
                    "跨站登录/注册被拒绝: ip=%s path=%s origin=%s",
                    db.hash_ip(_client_ip()),
                    _nl_safe(request.path),
                    _nl_safe(request.headers.get("Origin")),
                )
                return jsonify({"error": "请求来源异常，请刷新页面后重试"}), 403
            return
        token = request.headers.get("X-CSRF-Token", "")
        sess_token = session.get("csrf_token", "")
        # 非 ASCII token 会让 compare_digest 抛 TypeError → 500；
        # fail-closed 语义不变，但改为显式 403 且不刷异常日志
        if not token or not token.isascii() or not secrets.compare_digest(token, sess_token):
            logger.warning(
                "CSRF 校验失败: ip=%s path=%s token_len=%d session_token_len=%d",
                db.hash_ip(_client_ip()),
                _nl_safe(request.path),
                len(token),
                len(sess_token),
            )
            return jsonify({"error": "请求校验失败，请刷新页面后重试"}), 403

    # ---- 错误页（404/500）----
    # 静态资源与 API 的 404 不渲染 HTML 页面：前者只需空响应（浏览器/爬虫不当页面看），
    # 后者按 JSON 契约返回，避免前端 fetch 拿到 HTML 再 res.json() 报解析错。
    _404_OPAQUE_EXT = (
        ".png", ".ico", ".svg", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
        ".css", ".js", ".mjs", ".map", ".woff", ".woff2", ".ttf", ".otf",
        ".txt", ".xml", ".json", ".webmanifest",
    )

    def _home_endpoint_for(role):
        """按当前角色给出「返回首页」端点名与按钮文案：未登录 → 登录页。"""
        if role == "admin":
            return "dashboard_page", "返回总览"
        if role == "user":
            return "user_calendar_page", "返回签到日历"
        return "login_page", "去登录"

    def _render_error_page(code, title, message):
        """错误页：匿名用认证外壳，**登录态用管理端外壳**（保留侧栏与导航）。

        用户 2026-09-17：登录态管理员点到过期链接时不该"丢侧栏"，错误页恰恰最需要导航。
        管理端外壳要读更多上下文（导航/公告等），**错误路径本身可能是坏的**，故渲染失败
        （任何异常）一律退回认证外壳，绝不让 404/500 再抛一次。
        """
        role = _current_role()
        endpoint, label = _home_endpoint_for(role)
        base = dict(
            web_version=WEB_VERSION,
            app_version=APP_VERSION,
            icp_info=icp_info(),
            police_info=police_info(),
            police_link=police_link(),
            site_description=site_description(),
            err_code=code,
            err_title=title,
            err_message=message,
            home_url=url_for(endpoint),
            home_label=label,
            logged_in=role is not None,
        )
        if role is not None:
            try:
                return (
                    render_template("error.html", layout_name="layout_admin.html",
                                    use_admin_shell=True, **base),
                    code,
                )
            except Exception as e:      # 兜底就是"什么错都退回安全外壳"，见 docstring
                logger.warning("错误页渲染管理端外壳失败，退回认证外壳: %s", e)
        return (
            render_template("error.html", layout_name="layout_auth.html",
                            use_admin_shell=False, **base),
            code,
        )

    @app.errorhandler(404)
    def _handle_404(e):
        path = request.path
        if path.startswith((request.script_root or "") + "/api/"):
            return jsonify({"error": "接口不存在"}), 404
        if path.lower().endswith(_404_OPAQUE_EXT):
            return app.response_class("", status=404, mimetype="text/plain")
        return _render_error_page(
            404, "页面不存在", "你访问的地址不存在或已被移动，请检查链接是否输入正确。"
        )

    @app.errorhandler(500)
    def _handle_500(e):
        if request.path.startswith((request.script_root or "") + "/api/"):
            return jsonify({"error": "服务器内部错误，请稍后重试"}), 500
        return _render_error_page(
            500, "服务器内部错误", "请求处理失败，请稍后重试；若持续出现，请联系管理员。"
        )

    @app.after_request
    def no_cache(resp):
        # 全站安全头（所有响应，含 API）：防 MIME 嗅探 / 点击劫持 / 泄露来源 / XSS 与注入面
        # 注意：不使用 CSP nonce——模板含大量内联 onclick 处理器（无法加 nonce），
        # nonce 存在时 'unsafe-inline' 会被浏览器忽略导致全部处理器失效（2026-08-17 线上事故）。
        # 后续可将内联事件迁移到 addEventListener 后再启用 nonce 防护。
        resp.headers["X-Content-Type-Options"] = "nosniff"
        # 与边缘 nginx 保持一致（SAMEORIGIN）：防止子路径(经 nginx 反代)下出现
        # "应用 DENY / nginx SAMEORIGIN" 双头取值不一致。SAMEORIGIN 仍防点击劫持，
        # 且对同源内嵌场景更兼容。
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        # 补 COOP，收敛跨窗口攻面（CSP/XFO 之外的最后一块）
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        # 与 nginx 对齐：strict-origin-when-cross-origin（同源保留 referer，跨源最小化）
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # HSTS 移至边缘 nginx 统一下发（http_transport 一致性，疑自签过渡期阶段）。
        # 本应用不再重复下发，避免与 nginx 的 max-age 取值不一致造成双头歧义。
        # 注：若部署不经 nginx（如本地直连远程调试），可在此按需补回
        #   if request.is_secure: resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # 关闭无关能力面（2026-08-20 对抗性审查 P3 补；payment 与 nginx 对齐）
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self'; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'; object-src 'none'"
        )
        # 页面一律禁缓存（含旧的被重定向路径），防止浏览器缓存旧版 HTML/JS 造成登录循环
        if request.path in NO_STORE_PAGES:
            resp.headers["Cache-Control"] = "no-store"
        elif request.path.startswith("/static/") and resp.status_code < 400:
            # 静态资源长缓存 30 天（版本变化由 ?v= 兜底）；404 等错误响应不缓存（防浏览器缓存 404）
            resp.headers["Cache-Control"] = "public, max-age=2592000"
        return resp

    # ---- 数据层错误保护：SQLite 读写/密文解密失败 → 明确 500（防静默降级或返回错误数据）----
    @app.errorhandler(RuntimeError)
    def _handle_data_error(e):
        logger.error("数据层错误: %s", e)  # 详细信息只入日志，不回显客户端（防内部路径/字段泄露）
        return jsonify({"error": "服务器内部错误，请稍后重试或联系管理员"}), 500

    # ---- 敏感操作口令门禁与高危限速（设置 / 执行体 / 公告 / 用户管理各域共用）----
    def _admin_delete_limited():
        """高危操作限速（2026-08-29）：同一管理员窗口内超限返回 True（应拒绝 429）。

        键 = 会话用户名（统一小写）；窗口/上限由 .env 调整，0 = 关闭。
        与登录频率同语义（先判后增）：窗口内允许前 ADMIN_DELETE_MAX 次，之后拒绝。

        调用点从"三处高危删除"扩到"两处告警通道的高危配置变更"
        （关闭邮件通道 / 关闭推送 / 清空或更换推送密钥）。刻意共用同一套计数、
        不另建第二套——在攻击者手里"删数据"与"拆报警器"是同一条链，合并计数才
        真的限制得住一个被盗会话能造成多大静默。

        本函数**判定即占用**，故必须在二次鉴权通过
        之后调用（五个高危调用点统一走 _high_risk_gate，不再各自手搓顺序）。
        原先放在口令校验之前，不知口令的被盗会话可以用错口令尝试把主管理员的
        "删除 + 通道变更"预算（默认 5 次 / 60 秒）刷满，反过来让合法运维全程 429。
        """
        window = load_env_int(ENV_FILE, "YIBAN_ADMIN_DELETE_COOLDOWN_SEC", ADMIN_DELETE_COOLDOWN_SEC)
        limit = load_env_int(ENV_FILE, "YIBAN_ADMIN_DELETE_MAX", ADMIN_DELETE_MAX)
        if window <= 0 or limit <= 0:
            return False
        # 写入前顺带 trim（与其余限速表同口径防无界增长）
        with _rate_lock:
            _ip_store_trim(_admin_delete_limits, window + _IP_STORE_MAX_AGE)
        _cnt, _start, allowed = _bump_window_count(
            _admin_delete_limits,
            (session.get("username") or "?").strip().lower(),
            time.time(),
            window,
            limit=limit,
        )
        return not allowed

    def _verify_session_password(password):
        """当前会话管理员口令纯比对（不读写失败计数、不判定锁定）。

        口令核对语义的单一来源：内置管理员（.env）走 verify_admin（哈希优先，
        fail-closed）；注册管理员（users 表）走 password_hash 比对。
        失败处置**只有一处**——_sensitive_password_gate 在其上叠加独立计数、告警与
        冷却；本函数自己不动任何计数表。
        """
        username = session.get("username", "")
        if _is_builtin_admin_session():
            return verify_admin(username, password)
        u = db.find_user(username.strip().lower())
        return bool(u) and check_password_hash(u.get("password_hash", ""), password)

    def _pw_confirm_exempt(ttl, now):
        """本会话是否处在"刚复核过口令"的豁免窗口内（仅配置类动作可用）。

        两个条件缺一不可：
        - TTL 内复核成功过（`ttl <= 0` 直接关闭豁免，回到"每次都要口令"）；
        - **当前出口 IP 与授权时一致**——被窃 Cookie 换个出口就免检是不可接受的，
          而管理员从手机热点/VPN 换个出口后重新输一次口令是可接受的摩擦。
        """
        if ttl <= 0:
            return False
        ts = session.get("pw_ok_ts")
        return bool(
            isinstance(ts, (int, float))
            and now - ts <= ttl
            and session.get("pw_ok_ip") == _client_ip()
        )

    def _sensitive_pw_denied(key, action, deny_status, cooldown, now):
        """门禁口令不符的处置：独立计数 → 首达阈值告警 → 达阈值起进入/续期冷却。

        刻意**绝不写登录失败表**（P18）：能持 Cookie 撞门禁的人若可写登录侧的共享
        计数，就能用错口令把管理员同时锁在"登录"和"所有高危运维"之外，把风控变成攻击面。

        告警按 `== LOGIN_FAIL_NOTIFY` 只发一条（同一窗口不刷屏，运维口径），但冷却按
        `>= 阈值` **每次失败都续期**：只在"恰好等于阈值"那一次布防的话，冷却到期后的
        第 4、5… 次失败既不再告警也不再被挡，等于把同一个洞留回原处。续期之后，
        攻击者每 `cooldown` 秒最多只能做 `LOGIN_FAIL_NOTIFY` 次口令散列（实测单次
        scrypt 约 157ms），而不是此前的约 6 次/秒。
        """
        with _rate_lock:
            _ip_store_trim(_sensitive_pw_fails,
                           SENSITIVE_PW_FAIL_WINDOW + _IP_STORE_MAX_AGE)
        cnt, _start, _allowed = _bump_window_count(
            _sensitive_pw_fails, key, now, SENSITIVE_PW_FAIL_WINDOW)
        if cnt >= LOGIN_FAIL_NOTIFY and cooldown > 0:
            with _rate_lock:
                _sensitive_pw_cooldown[key] = (cnt, now + cooldown)
        if cnt == LOGIN_FAIL_NOTIFY:
            send_notification(
                "高危操作二次鉴权失败告警",
                mail_layout.Mail(
                    summary=f"IP {_nl_safe(key[0])} 对「{action}」连续 {cnt} 次口令验证失败。",
                    fields=([("会话用户", _nl_safe(key[1]))]
                            + ([("敏感操作已暂停",
                                 f"{cooldown} 秒（登录、只读页面与普通设置不受影响）")]
                               if cooldown > 0 else [])),
                    advice=["如非本人操作，可能是账号或会话被他人使用，请立即检查"],
                    level="urgent",
                ),
                urgent=True,
            )
            # 只在布防那一刻留一条审计：429 本身不逐条写，否则被盗会话又能拿
            # "拒绝"当免费打字机刷审计表。
            if cooldown > 0:
                db.audit(
                    key[1], "sensitive_pw_cooldown", db.hash_ip(key[0]),
                    f"「{action}」口令复核连续失败 {cnt} 次，"
                    f"敏感操作暂停 {cooldown} 秒",
                )
        return jsonify({"error": PW_DENY_TEXT[deny_status],
                        "reason": PW_DENY_REASON["wrong"]}), deny_status

    def _sensitive_password_gate(data, action, *, always_required=False,
                                 deny_status=403):
        """敏感操作口令复核的**唯一入口**。返回 None = 放行，否则是要直接 `return` 的响应。

        为什么必须收成一个入口：三处落点（系统开关、执行体写、高危二次鉴权）此前各写各的
        判定，共同点是**没有冷却**——实测以主管理员会话对 `POST /api/settings` 连投错误
        `confirm_password`，200 次只被通用 API 限速（60 次/10 秒）挡住，约 6 次/秒 ×
        单次 scrypt 157ms 就能打满一核；比项目自己的登录口（10 次/60 秒 + 第 5 次锁
        300 秒）快约 38 倍且**永不锁**，等于给绕过登录限速留了个算力口子。

        三段判定按序：
        1. **冷却优先于口令**：本 (出口 IP, 会话账号) 已进冷却 → 429，正确口令也不放行
           （否则"改用对口令"就是冷却自带的绕过口子）。冷却只封这条门禁：登录、只读
           GET、以及不需要复核的写操作一律照常。
        2. **豁免**（仅 `always_required=False` 的配置类动作）：见 _pw_confirm_exempt。
        3. **口令比对**：通过则把授权时刻与出口 IP 记进会话，供第 2 段用。

        `always_required=True` 用于"必须当次输口令"的动作——不可逆清除、关闭/改道告警
        通道、角色变更、重置他人口令、改主管理员口令。调用点逐个标注，见各站点注释。
        """
        if not session.get("auth"):
            return jsonify({"error": "未登录"}), 401
        ttl, cooldown = _sensitive_gate_params(ENV_FILE)
        key = (_client_ip(), (session.get("username") or "?").strip().lower()[:64])
        now = time.time()
        with _rate_lock:
            _ip_store_trim(_sensitive_pw_cooldown, cooldown + _IP_STORE_MAX_AGE)
            until = (_sensitive_pw_cooldown.get(key) or (0, 0))[1]
        if now < until:
            return jsonify(
                {"error": "口令校验失败次数过多，敏感操作已暂停，请稍后再试"}), 429
        if not always_required and _pw_confirm_exempt(ttl, now):
            return None
        submitted = str(data.get("confirm_password", ""))
        if not submitted:
            # 没提交口令 ≠ 猜错口令：照旧拒绝（状态码不变），但**不计数、不告警、
            # 不进冷却**。冷却要限的是口令散列次数（实测单次 scrypt 约 157ms），而空口令
            # 在入口就被挡掉、一次散列都不做；把它计入阈值等于让攻击者用"空请求"就能把
            # 合法管理员的敏感操作预算刷光，也正是 _admin_delete_limited 修掉的那类运维 DoS
            # （前端"点了保存又取消口令框"的正常操作同样不该被罚）。
            # 文案走 PW_MISSING_TEXT：不能对用户说"密码不正确"，他根本没输。
            return jsonify({"error": PW_MISSING_TEXT[deny_status],
                            "reason": PW_DENY_REASON["missing"]}), deny_status
        if _verify_session_password(submitted):
            session["pw_ok_ts"] = now
            session["pw_ok_ip"] = key[0]
            return None
        return _sensitive_pw_denied(key, action, deny_status, cooldown, now)

    def _reconfirm_admin_password(password, action_label, always_required=True):
        """高危操作二次鉴权（2026-08-29）：要求当前会话管理员重新输入口令。

        签名与返回约定保持不变（None = 通过，否则 `(响应, 状态码)` 元组），以免改动
        20+ 调用点；实现整体交给 _sensitive_password_gate（含失败计数、告警与冷却）。
        与原实现的两处语义差别：
        - **不再读写登录失败表**：原实现把失败记进与登录共用的桶并置 lock_until，
          于是与管理员同出口 IP 的被窃会话可以用错口令把主管理员同时锁在"登录"和
          "所有高危运维"之外（P18 残留，本次摘掉）；
        - 也不再借用登录侧的锁定状态：登录侧锁着，不影响本会话用**正确口令**做运维。

        `always_required` 默认 True——本函数的调用点本来就是一串"不可逆清除 / 角色变更 /
        重置他人口令 / 关闭告警通道"，这些必须当次输口令；只有设置页里两个纯配置项
        （签到随机延迟、容量上限）显式传 False 走豁免。
        """
        return _sensitive_password_gate(
            {"confirm_password": password}, action_label,
            always_required=always_required, deny_status=400)

    def _high_risk_gate(data, action_label, limit_msg="操作过于频繁，请稍后再试"):
        """高危动作统一门禁：先二次鉴权，**通过之后**才占用高危限速额度。

        顺序即本次修复：原五处调用都是"先判后增再鉴权"，于是
        一个只拿到 Cookie、不知道口令的被盗会话，用错口令反复尝试就能把主管理员
        的"删除 + 告警通道变更"预算（默认 5 次 / 60 秒）全部吃掉，反过来让合法
        运维的每一次高危操作都撞 429（运维 DoS）。口令暴力的防护本就由
        _sensitive_password_gate 里的独立计数与门禁级冷却承担（第 3 次告警并暂停
        敏感操作），不需要再借用高危额度；额度只该被**真实执行过**的高危动作消耗。

        仍复用同一套计数（不新建第二套 store，评审口径），不改变"超限即 429"的语义。
        返回 None 表示放行；否则返回应直接 `return` 给客户端的 4xx 响应。

        本函数走的全部是"必须当次输口令"的动作（`always_required=True`，豁免不适用），
        逐个落点：账号/用户的不可逆清除与删除（/api/accounts/batch 的 purge、
        /api/accounts/<idx>/delete、/api/users/batch 的 delete、
        /api/users/<email>/delete 的 full 与 accounts_only、
        /api/users/deleted/purge）、关闭告警通道或改其密钥/额度（/api/mail-config 的
        开关、/api/notify-config）、角色变更（/api/users/<email>/role）、
        重置他人口令（/api/users/<email>/password）。
        同为 always_required 但不占高危额度的还有 /api/mail-config 的 SMTP/收件人变更
        （直连 _reconfirm_admin_password）。
        可被 TTL 豁免的配置类动作只有三处：/api/settings 的系统开关、
        /api/settings 的签到随机延迟与容量上限、/api/scheduler/executors* 的写操作。
        """
        pw_err = _reconfirm_admin_password(
            str(data.get("confirm_password", "")), action_label, always_required=True)
        if pw_err:
            return pw_err
        if _admin_delete_limited():
            return jsonify({"error": limit_msg}), 429
        return None

    # 高危门禁与只读留痕闭包登记为 app 属性：它们闭包依赖工厂局部的限速计数表，做成模块级
    # 会跨 app 实例串额度（测试进程反复 create_app）；搬进 web/routes 的通知/账号域经
    # web.routes.high_risk_gate() / reconfirm_admin_password() / admin_delete_limited() /
    # read_audit_trace() / read_audit_denied_trace() 按实例取回同一闭包。
    app.extensions["yiban_high_risk_gate"] = _high_risk_gate
    app.extensions["yiban_reconfirm_admin_password"] = _reconfirm_admin_password
    app.extensions["yiban_admin_delete_limited"] = _admin_delete_limited
    app.extensions["yiban_read_audit_trace"] = _read_audit_trace
    app.extensions["yiban_read_audit_denied_trace"] = _read_audit_denied_trace
    app.extensions["yiban_sensitive_password_gate"] = _sensitive_password_gate
    # ---- 每日自动清除超期注销用户（2026-08-17：修复"仅启动时清除一次"的隐患）----
    # 此前 purge_deleted_users 只在 db 连接初始化（服务启动）时执行，长期不重启的
    # 服务会让超期注销用户数据（邮箱、软删易班账密）无限留存，与页面"系统定期
    # 物理清除"的承诺不符。后台 daemon 线程：启动 60s 后先跑一次，此后每 24h 一次。
    def _daily_purge_loop():
        # 首轮延迟：避开启动高峰（迁移/预热），且此时 init_db 已跑过一次 purge，
        # 延迟不会造成额外的清除延迟（下一轮 24h 内必然覆盖）
        time.sleep(60)
        while True:
            try:
                # 每日集中清理（2026-08-28 审查 M6 收口）：审计/事件旧数据 +
                # 过期软删账号 + 过期注销用户 + 注销请求记录，统一走 db.run_daily_cleanup()。
                # 此前 _audit_cleanup/_event_cleanup（全表 DELETE）只挂在 init_db 上，
                # 而 signin 子进程每天 2~3 次 init_db 也各跑一轮，与 web 8 线程争锁；
                # 现清理只在 web 每日线程执行，signin 侧 init_db(cleanup=False)。
                db.run_daily_cleanup()
            except Exception as e:
                logger.warning("每日自动清除注销用户失败: %s", e)
            # 审计可追溯性每日校验（2026-08-28 审查 B-3）：
            # 此前 verify_audit_chain 生产环境从不调用、外部锚点只写不读——审计写入
            # 可静默丢（B-1）、删前缀/删尾/清空验不出（B-2）也无人知晓。
            # 现每日流程：先校验（链自洽 + 库外锚点比对 + 写入失败计数），任一异常
            # 即告警；校验通过且清理已发生后，再追加新锚点（使锚点反映清理后的
            # 合法状态，且记录 min_id/max_id 以覆盖删尾/清空检测，原两段格式不具备）。
            try:
                # 显式传入锚点路径——原调用走 db.audit_anchor_path()
                # 默认解析（env 或 cwd），裸机部署下与本进程写锚点的 STATE_DIR 分裂，
                # 造成"每日误报锚点被删 + 真实锚点从未参与校验"的双重失效
                _health = db.audit_health(path=os.path.join(STATE_DIR, "audit-anchor.log"))
                if not _health["healthy"]:
                    _facts = _audit_alert_facts(_health)
                    # 日志保持单行可 grep；邮件/推送读下面那份结构化正文
                    logger.error("审计链异常告警: %s",
                                 "；".join(f"{k} {v}" for k, v in _facts))
                    send_notification(
                        "审计链异常告警",
                        mail_layout.Mail(
                            summary="审计可追溯性校验失败：审计记录可能被篡改/删除，"
                                    "或存在未留痕的管理操作。",
                            fields=_facts,
                            advice=["立即核查审计链与库外锚点",
                                    "确认之前不要依赖审计记录做处置结论"],
                            level="urgent",
                        ),
                        urgent=True,
                    )
                elif _health["anchor_msg"]:
                    # 非异常的提示性信息（如保留期清理回收了最早记录），记录即可
                    logger.info("审计链提示: %s", _health["anchor_msg"])
                # 时钟守卫拦截后的持续告警——守卫拦截会把清理永久
                # 冻结（人工重置前不恢复），每日线程在此读 app_meta 留痕并发邮件，
                # 直到管理员运行 scripts/clock_guard_reset.py 重置为止（每日重发
                # 是刻意的：冻结状态必须保持可见，防止静默腐烂）
                _cg = db.clock_guard_alert()
                if _cg:
                    _cg_mail = mail_layout.Mail(
                        summary="系统时间异常跳变已被拦截，全部物理清理处于冻结状态。",
                        fields=[("告警时间", _cg.get("ts", "?")),
                                ("守卫备注", _cg.get("note") or "（无）")],
                        advice=["先核实系统时间与 NTP 同步状态",
                                "确认时间正确后运行 "
                                "python3 scripts/clock_guard_reset.py --confirm 重置"],
                        level="urgent",
                    )
                    logger.error("时钟守卫告警: %s", _cg.get("note", ""))
                    send_notification("时钟跳变守卫告警", _cg_mail, urgent=True)
                db.record_audit_anchor(os.path.join(STATE_DIR, "audit-anchor.log"))
            except Exception as e:
                logger.warning("审计链每日校验/锚点写入失败: %s", e)
            # 告警通道健康日报——本系统所有安全告警只有邮件 +
            # 手机推送两条出口，两条同时失效时管理员将彻底失明（活体复现的
            # 攻击链正是"拿到大管理员 cookie 后两步关通道、零外发"）。除门禁外再加
            # 一层兜底：每日固定报告两条通道当前状态与今日额度，通道被关也照样
            # 发一封"已关闭"，让"报警器被拆"这件事本身有个可观测的周期性痕迹。
            # 修复轮 1 ④：本线程在启动 60 秒后即跑第一轮，故"每日至多一封"的去重
            # 标记与"通道降级"痕迹都在函数内落库（app_meta + db.audit），重启不重发、
            # 两通道全断时也仍留得住证据。
            # 修复轮 2：标记改在发信成功后才落——本处 except 吞掉的正是"今天没发出去"，
            # 不落标记才能让下一次进程启动（同一日）再试一封，而不是静默到明天。
            try:
                _send_channel_health_report()
            except Exception as e:
                logger.warning("告警通道健康日报发送失败: %s", e)
            time.sleep(24 * 3600)

    # 测试环境通过 YIBAN_DISABLE_PURGE_LOOP=1 禁止启动该线程（全量 pytest 会反复 create_app，
    # 大量 60s 后唤醒的线程并发访问共享 SQLite 单例有 access violation 风险）；
    # 生产默认不设置该变量，仍按原逻辑启动，且同一进程最多一个 daily-purge。
    if os.environ.get("YIBAN_DISABLE_PURGE_LOOP") != "1":
        with _purge_loop_lock:
            if not _purge_loop_started:
                _purge_loop_started = True
                threading.Thread(
                    target=_daily_purge_loop, daemon=True, name="daily-purge"
                ).start()

    # 路由装配：页面/API 各域在 web/routes/*，此处一次接入（注册顺序不参与路由判定）
    register_all(app)

    # 前缀自适应：把 WSGI 层包一层（app 本身仍是 Flask 对象，.run()/gunicorn 调用不受影响）。
    # 支持子路径 / 独立子域 / 根路径三种部署；详见 BasePathMiddleware 类注释。
    app.wsgi_app = BasePathMiddleware(app.wsgi_app)

    return app


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    global ACCOUNTS_FILE, LOG_FILE, ENV_FILE, STATE_DIR, DB_FILE
    parser = argparse.ArgumentParser(description="易班自动签到网页管理系统")
    # 2026-08-20 对抗性审查 P2：默认改回环——werkzeug 开发服务器不应默认暴露
    # 全网卡（明文 HTTP + 无反代防护）。生产走 systemd/gunicorn 模板不受影响；
    # 确需直连局域网时显式传 --host 0.0.0.0（自担风险）。
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，仅回环）")
    # 非常见端口（默认 17892）：避开 8000/5000/3000 等常见端口，防止与其他部署冲突
    parser.add_argument("--port", type=int, default=17892, help="监听端口（默认 17892）")
    parser.add_argument(
        "--config", default=ACCOUNTS_DEFAULT, help=f"JSON 数据文件路径（迁移来源，默认: {ACCOUNTS_DEFAULT}）"
    )
    parser.add_argument("--log", default=LOG_DEFAULT, help=f"签到日志路径（默认: {LOG_DEFAULT}）")
    parser.add_argument("--env", default=ENV_DEFAULT, help=f".env 路径（默认: {ENV_DEFAULT}）")
    parser.add_argument(
        "--db", default=DB_DEFAULT, help=f"SQLite 数据库路径（默认: {DB_DEFAULT}）"
    )
    parser.add_argument("--debug", action="store_true", help="Flask 调试模式")
    args = parser.parse_args()
    ACCOUNTS_FILE = args.config
    LOG_FILE = args.log
    ENV_FILE = args.env
    DB_FILE = args.db
    STATE_DIR = STATE_DIR_DEFAULT

    # 日志 handler 统一由 create_app 配置（DailyFlockFileHandler → 按天文件）；
    # 此处不再 basicConfig(stderr)——双重 handler 会把每条日志写两遍。
    # 原实现先 create_app 一次、查完管理员配置后再 create_app
    # 一次——第二次调用重复执行口令迁移 / init_db / 日志 handler 幂等装配等全部
    # 启动逻辑（纯浪费，多 worker 下还加倍迁移竞态窗口）。现只调用一次；
    # check_admin_configured 仅读 .env，置于其前行为等价。
    logger.info(
        "启动网页管理系统: http://%s:%d（数据库: %s / 日志: %s / .env: %s）",
        args.host,
        args.port,
        DB_FILE,
        LOG_FILE,
        ENV_FILE,
    )
    if not check_admin_configured():
        logger.warning(
            "未配置管理员账号：请在 %s 中设置 YIBAN_ADMIN_USER / YIBAN_ADMIN_PASSWORD", ENV_FILE
        )

    app = create_app(host=args.host)
    # 生产模式：debug 关闭（Werkzeug 单进程即可；如部署用 systemd 更稳）
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
