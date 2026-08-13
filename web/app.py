#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""易班自动签到网页管理系统（服务器端）。

在浏览器中替代 TUI 面板：管理员登录后，可在任意设备（手机/平板/电脑）
查看和管理签到任务。功能与 TUI 对齐：

- 账号管理：列表 / 添加 / 编辑 / 删除 / 排序（决定顺序打卡顺序）
- 签到日志：解析 sign.log 展示最近记录与今日各账号状态图标
- 手动签到：单账号后台执行 scripts/signin.py --only
- 系统设置：随机延迟开关（写入 .env）、连通性检测、服务器时间/签到窗口状态

运行：
    python3 -m web                 # 默认 0.0.0.0:8000
    python3 -m web --port 9000     # 自定义端口

管理员账号：首次启动自动生成 SECRET_KEY 并写入 .env；
在 .env 配置 YIBAN_ADMIN_USER / YIBAN_ADMIN_PASSWORD 后即可登录。
"""

import argparse
import contextlib
import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime

import requests
from flask import Flask, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

# 共享模块（web/ 与 scripts/ 同级）：加密模块 + SQLite 数据访问层
_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import account_crypto  # noqa: E402
import db  # noqa: E402

# 默认路径（与 tui/app.py / run.sh 保持一致，可用参数覆盖）
ACCOUNTS_DEFAULT = os.environ.get("YIBAN_ACCOUNTS_FILE", "accounts.json")
# 按日状态文件目录（signin.py 写入 sign-daily-YYYY-MM-DD.json，网页日历读取）
STATE_DIR_DEFAULT = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
LOG_DEFAULT = os.environ.get("YIBAN_LOG_FILE", "/var/log/yiban/sign.log")
ENV_DEFAULT = os.environ.get("YIBAN_ENV_FILE", ".env")
USERS_DEFAULT = os.environ.get("YIBAN_USERS_FILE", "users.json")
DB_DEFAULT = os.environ.get("YIBAN_DB_FILE", "yiban.db")
# 模块级路径（gunicorn 走 create_app() 不执行 main()，需在此初始化；main() 用 --config 等参数覆盖）
ACCOUNTS_FILE = ACCOUNTS_DEFAULT  # 仅作 JSON→SQLite 自动迁移来源（迁移后改名 .bak）
LOG_FILE = LOG_DEFAULT
ENV_FILE = ENV_DEFAULT
USERS_FILE = USERS_DEFAULT
STATE_DIR = STATE_DIR_DEFAULT
DB_FILE = DB_DEFAULT

# 普通用户账号的审核状态
STATUS_PENDING = "pending"  # 待审核（不参与定时签到）
STATUS_ACTIVE = "active"  # 已生效（参与定时签到）
STATUS_REJECTED = "rejected"  # 已拒绝（附理由，用户可编辑重新提交）

# 软删除保留期：管理员删除的账号进入待删除状态，超期自动彻底清除
DELETED_RETENTION_DAYS = 7

# 密码策略：至少 10 位且包含大写/小写/数字/符号中至少两类（只对新建/修改生效，存量密码不受影响）
PASSWORD_MIN_LEN = 10
# 口令哈希算法（werkzeug scrypt，OWASP 推荐参数；check_password_hash 对旧哈希自动兼容）
SCRYPT_METHOD = "scrypt:65536:8:1"

# 账号编辑时识别码清空哨兵值（收到该值 = 显式删除设备识别码字段）
CLEAR_SENTINEL = "__clear__"

# 状态图标（与 tui/app.py 一致；前端渲染使用，后端仅用于日志解析）
SIGN_START = (6, 30)
SIGN_END = (7, 50)

# 随机延迟默认上限（与 signin.py 一致）
DEFAULT_START_DELAY_MAX = 60
DEFAULT_ACCOUNT_GAP_MAX = 10

# 登录失败限速：同一 IP 连续失败超过阈值后锁定
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 300
# 连续失败告警阈值：达到后通过 YIBAN_NOTIFY_URL 通知管理员（每轮锁定只告警一次）
LOGIN_FAIL_NOTIFY = 3

# IP 计数 dict（限速/登录失败/注册）的条目上限与最长保留：防公网扫描器多 IP 打爆内存
_IP_STORE_LIMIT = 10000
_IP_STORE_MAX_AGE = 3600

# 全局请求限速（防疯狂刷新/脚本轰炸）：每 IP 窗口内最多 RATE_MAX 次
RATE_WINDOW = 10  # 窗口（秒）
RATE_MAX = 60  # 窗口内最大请求数（正常用户远低于此）
# 注册限速（防邮箱批量注册）：每 IP 窗口内最多 REGISTER_MAX 次成功注册
REGISTER_WINDOW = 600  # 窗口（秒）= 10 分钟
REGISTER_MAX = 5  # 窗口内最大成功注册数

# 普通用户邮箱格式校验（用户名部分（@ 前）限 32 字符：防超长用户名破坏界面显示）
EMAIL_RE = re.compile(r"^[\w.+-]{1,32}@[\w-]+(\.[\w-]+)+$")
EMAIL_USER_MAX = 32  # 邮箱用户名部分（@ 前）最大长度
# 手机号格式（易班登录账号为中国 11 位手机号；恶意字符可注入前端事件与日志）
PHONE_RE = re.compile(r"^1\d{10}$")

# 手动签到防抖：同一账号两次触发的最小间隔（秒）
SIGN_MIN_INTERVAL = 30

# 日志格式（与 signin.py / tui/app.py 相同）
# 行格式: [2026-08-07 06:40:04] [INFO] yiban: [手机号] ✅ 签到成功
SIGN_LOG_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}) [\d:]+\] \[(\w+)\] (\w+): (.*)")
STATE_RE = re.compile(r"\[(\d+)\]\s*(✅|❌|🔄|➖)")

logger = logging.getLogger("web")


# ---------------------------------------------------------------------------
# 签到日志解析（与 tui/app.py parse_sign_log 保持一致）
# ---------------------------------------------------------------------------
# 签到日志解析（与 tui/app.py parse_sign_log 保持一致）
# ---------------------------------------------------------------------------
_LOG_TAIL_BYTES = 2 * 1024 * 1024  # 日志倒读上限 2MB（约 2 万行）


def _tail_lines(path, max_bytes=_LOG_TAIL_BYTES):
    """从文件尾部读取最多 max_bytes 的完整文本行：大日志避免整读入内存。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    try:
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # 丢弃首个不完整行
                raw = f.read()
            else:
                raw = f.read()
    except OSError:
        return []
    return raw.decode("utf-8", errors="replace").splitlines()


def parse_sign_log(path):
    """解析签到日志：返回 (今日各账号状态 dict, 最近日志行列表)。"""
    today = datetime.now().strftime("%Y-%m-%d")
    states = {}
    recent = []
    for line in _tail_lines(path):
        m = SIGN_LOG_RE.match(line.strip())
        if not m:
            continue
        date, level, logger_name, msg = m.groups()
        if logger_name != "yiban" or level == "DEBUG":
            continue
        recent.append(line.strip())
        if date == today:
            sm = STATE_RE.search(msg)
            if sm:
                states[sm.group(1)] = sm.group(2)
    return states, recent


# ---------------------------------------------------------------------------
# .env 读写（与 tui/app.py 保持一致）
# ---------------------------------------------------------------------------
def read_env(env_path):
    """读取 .env 全部键值，返回 dict。"""
    result = {}
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    result[key.strip()] = value.strip()
    except OSError:
        pass
    return result


def load_env_int(env_path, key, default):
    """读取 .env 中的整数配置，缺失/非法回退默认值。"""
    try:
        return max(0, int(read_env(env_path).get(key, "")))
    except (TypeError, ValueError):
        return default


def write_env_int(env_path, key, value):
    """把整数配置写入 .env：value<=0 删除该行，>0 写入；保留其他行。"""
    write_env_key(env_path, key, str(value) if value > 0 else "")


def write_env_key(env_path, key, value):
    """把任意键值写入 .env：value 为空删除该行，否则写入；保留注释与其他行。"""
    lines = []
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    out = [ln for ln in lines if not ln.strip().startswith(f"{key}=")]
    if value:
        out.append(f"{key}={value}")
    _atomic_write(env_path, "\n".join(out) + "\n")


def ensure_secret_key(env_path):
    """确保 .env 中存在 YIBAN_SECRET_KEY（缺失时自动生成随机值）。"""
    env = read_env(env_path)
    key = env.get("YIBAN_SECRET_KEY", "").strip()
    if key:
        return key
    key = secrets.token_hex(32)
    lines = []
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    if not any(ln.strip().startswith("YIBAN_SECRET_KEY=") for ln in lines):
        lines.append(f"YIBAN_SECRET_KEY={key}")
    _atomic_write(env_path, "\n".join(lines) + "\n")
    logger.info("已自动生成 YIBAN_SECRET_KEY 并写入 %s", env_path)
    return key


def _atomic_write(path, content):
    """原子写文件：先写临时文件再替换，避免半写状态（cron 并发读取安全）。"""
    tmp = f"{path}.tmp{secrets.token_hex(4)}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())  # 落盘再替换：极端掉电场景不丢数据
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 数据读写（SQLite：db 层单行事务 + WAL，天然原子，无需 TTL 缓存）
# RLock：进程内"读→检查→写"操作级序列互斥（防呆判定与写入之间不被同进程请求交错；
# 跨进程一致性由 SQLite 事务与 UNIQUE 约束保证）
# ---------------------------------------------------------------------------
_file_lock = threading.RLock()

# 启动缓存（与数据无关）：CHANGELOG 部署重启自然失效；公告保存时同步更新
_changelog_cache = [None]  # [文本]
_announcement_cache = [None]  # [公告文本]


def load_accounts():
    """全部账号（SQLite，password/phone_code 已解密为明文，按 sort_order 升序）。

    _file_lock：与写操作同锁，避免读到同一连接上未提交事务的部分结果
    （批量操作进行中，并发读可能看到半成品状态；RLock 可重入，写操作内调用无死锁）。
    """
    with _file_lock:
        return db.load_accounts()


def load_users():
    """全部用户（SQLite）。"""
    with _file_lock:
        return db.load_users()


def _mask_phone(p):
    """日志/列表脱敏：11 位手机号 → 138****8000；已脱敏（含 *）或非 11 位原样返回（幂等）。"""
    p = str(p)
    if "*" in p:
        return p
    return p[:3] + "****" + p[7:] if len(p) == 11 else p


def _mask_email(e):
    """日志/列表脱敏：邮箱 → abc***@example.com（保留域名）；已脱敏或非邮箱原样返回（幂等）。"""
    e = str(e)
    if "*" in e:
        return e
    i = e.find("@")
    if i <= 0:
        return e
    return e[: min(3, i)] + "***" + e[i:]


def _password_policy_error(password):
    """校验密码强度：至少 10 位且包含大写/小写/数字/符号中至少两类。返回错误信息 or None。"""
    if len(password) < PASSWORD_MIN_LEN:
        return f"密码至少 {PASSWORD_MIN_LEN} 位，且需包含两类以上字符"
    classes = sum(
        bool(re.search(pat, password)) for pat in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
    )
    if classes < 2:
        return "密码需包含大写字母/小写字母/数字/符号中的至少两类"
    return None


class AccountCryptoError(Exception):
    """账号密文处理失败（解密失败/密钥缺失）：数据不可用，拒绝继续，避免静默明文化。

    （db 层解密失败抛 RuntimeError，此异常兼容保留——不再有抛出点，仅防御旧调用。）"""


def _owner_display_of(owner_email):
    """把账号归属邮箱映射为展示名（后台归属列用）：普通用户显示邮箱前缀（@ 前）。"""
    if owner_email in ("admin", ""):
        return "管理员"
    return owner_email.split("@")[0] if "@" in owner_email else owner_email


def mask_account(acc, index, masked=True):
    """账号展示序列化（列表默认脱敏手机号/归属邮箱，网络层不泄露完整 PII）。

    masked=False 时返回完整信息（仅详情接口使用，按需取完整号用于编辑/签到等操作）。
    密码始终不下发（has_password 布尔）；设备识别码始终不下发（has_phone_code 布尔）。
    """
    phone = acc.get("phone", "")
    owner = acc.get("owner", "admin")
    return {
        "index": index,
        "name": acc.get("name", ""),
        "phone": _mask_phone(phone) if masked else phone,
        "phone_model": acc.get("phone_model", ""),
        "has_password": bool(acc.get("password")),
        "has_phone_code": bool(acc.get("phone_code")),
        "display_name": acc.get("name") or f"账号{index + 1}",
        # 普通用户体系：owner=提交者邮箱（'admin'=管理员添加），status=待审核/已生效
        "owner": _mask_email(owner) if masked else owner,
        "owner_display": _owner_display_of(owner),
        "status": acc.get("status", STATUS_ACTIVE),
        "reject_reason": acc.get("reject_reason", ""),
        # 软删除：管理员删除后进入待删除状态（保留期内可恢复）
        "deleted": bool(acc.get("deleted")),
        "deleted_at": acc.get("deleted_at", ""),
    }


def find_account_index(accounts, phone):
    """按手机号查账号下标（手动签到用）。"""
    for i, acc in enumerate(accounts):
        if acc.get("phone") == phone:
            return i
    return None


def _owner_has_other_live(accounts, acc):
    """归属用户（非 admin）名下是否已有其他未删除账号（每人限 1 个，恢复/添加时校验）。"""
    owner = acc.get("owner", "admin")
    if not owner or owner == "admin":
        return False
    return any(
        a.get("owner") == owner and not a.get("deleted") and a is not acc for a in accounts
    )


def validate_account(data, require_password):
    """校验账号字段。require_password=True 时密码必填；返回 (错误信息 or None, 清洗后的账号 dict)。"""
    name = str(data.get("name", "")).strip()
    if len(name) > 50:
        return "名称过长（最多 50 字）", None
    phone = str(data.get("phone", "")).strip()
    password = str(data.get("password", "")).strip()
    if not phone:
        return "手机号为必填项", None
    if not PHONE_RE.match(phone):
        return "手机号格式不正确（应为 1 开头的 11 位数字）", None
    if require_password and not password:
        return "密码为必填项", None
    phone_model = str(data.get("phone_model", "")).strip()
    if len(phone_model) > 50:
        return "设备型号过长（最多 50 字）", None
    phone_code = str(data.get("phone_code", "")).strip()
    if len(phone_code) > 128:
        return "设备识别码过长", None
    return None, {
        "name": name,
        "phone": phone,
        "password": password,
        "phone_model": phone_model,
        "phone_code": phone_code,
    }


# ---------------------------------------------------------------------------
# 管理员认证
# ---------------------------------------------------------------------------
def check_admin_configured():
    """管理员账号是否已在 .env 配置（口令哈希或旧明文任一即可）。"""
    env = read_env(ENV_FILE)
    return bool(
        env.get("YIBAN_ADMIN_USER", "").strip()
        and (
            env.get("YIBAN_ADMIN_PASSWORD_HASH", "").strip()
            or env.get("YIBAN_ADMIN_PASSWORD", "").strip()
        )
    )


def verify_admin(username, password):
    """校验管理员账号（每次登录实时读 .env，修改立即生效）。

    口令哈希（YIBAN_ADMIN_PASSWORD_HASH，scrypt）优先；未配置哈希时
    回退旧明文（YIBAN_ADMIN_PASSWORD）比对，兼容存量部署。
    注意：compare_digest 不支持非 ASCII 直接比较，先编码为 UTF-8 字节。
    """
    env = read_env(ENV_FILE)
    admin_user = env.get("YIBAN_ADMIN_USER", "").strip()
    if not secrets.compare_digest(
        username.strip().encode("utf-8"), admin_user.encode("utf-8")
    ):
        return False
    pw_hash = env.get("YIBAN_ADMIN_PASSWORD_HASH", "").strip()
    if pw_hash:
        return check_password_hash(pw_hash, password)
    admin_pass = env.get("YIBAN_ADMIN_PASSWORD", "").strip()
    return secrets.compare_digest(password.encode("utf-8"), admin_pass.encode("utf-8"))


# ---------------------------------------------------------------------------
# 系统信息
# ---------------------------------------------------------------------------
def sign_status(now=None):
    """基于服务器时间计算签到状态（与 tui/app.py _sign_status 保持一致）。

    返回 (显示文本, 颜色)。
    """
    now = now or datetime.now()
    if now.weekday() == 6:  # 周日
        return "🌙 今日无需打卡（周日）", "#565f89"
    start = now.replace(hour=SIGN_START[0], minute=SIGN_START[1], second=0, microsecond=0)
    end = now.replace(hour=SIGN_END[0], minute=SIGN_END[1], second=0, microsecond=0)
    if now < start:
        return f"⏳ 未到签到时间（{SIGN_START[0]:02d}:{SIGN_START[1]:02d} 开始）", "#7aa2f7"
    if now <= end:
        return f"🔔 签到窗口（~{SIGN_END[0]:02d}:{SIGN_END[1]:02d} 结束）", "#9ece6a"
    return "✅ 打卡时间已过", "#e0af68"


def check_connectivity():
    """连通性检测：不登录，仅检查易班 API 可达性。返回 (ok, detail)。"""
    try:
        resp = requests.get(
            "https://api.uyiban.com/base/c/auth/yiban",
            timeout=6,
            headers={
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
            },
        )
        ok = resp.status_code < 500
        detail = f"HTTP {resp.status_code}"
    except Exception as e:
        ok = False
        detail = str(e)[:60]
    return ok, detail


def send_notification(title, content):
    """通过 YIBAN_NOTIFY_URL webhook 发送告警通知（.env 优先，静默失败）。

    与 scripts/signin.py 的 send_notification 同款渠道：Server 酱 / Bark / 企业微信。
    """
    env = read_env(ENV_FILE)
    url = env.get("YIBAN_NOTIFY_URL", "").strip() or os.environ.get("YIBAN_NOTIFY_URL", "").strip()
    if not url:
        return
    try:
        requests.post(url, json={"title": title, "content": content}, timeout=10)
        logger.info("告警通知已发送: %s", title)
    except Exception as e:
        logger.warning("告警通知发送失败: %s", e)


# ---------------------------------------------------------------------------
# Flask 应用
# ---------------------------------------------------------------------------
# 应用版本号（页面底部显示；每次修改按语义递增：修复 +0.0.1 / 功能 +0.1.0 / 大版本 +1.0.0）
APP_VERSION = "0.17.1"
# 页面失效版本：每次启动变化，供前端"版本失效自动刷新"兜底（防止缓存旧页面）
WEB_VERSION = datetime.now().strftime("%Y%m%d%H%M%S")


def create_app():
    # SQLite 数据层初始化：首次启动自动迁移 accounts.json/users.json → yiban.db（幂等，
    # JSON 改名 .bak 保留逃生门）；多 worker 各自调用幂等（模块级连接缓存）
    db.init_db(DB_FILE, migrate_from=ACCOUNTS_FILE, env_file=ENV_FILE)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = ensure_secret_key(ENV_FILE)
    app.config["SESSION_COOKIE_NAME"] = "yiban_admin"
    app.config["SESSION_COOKIE_HTTPONLY"] = True  # JS 不可读 session cookie（防 XSS 窃取）
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # 跨站请求不携带 cookie（防 CSRF）
    app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30  # 30 天
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # 请求体上限 64KB

    # 登录失败记录 {ip: [fail_count, lock_until]}
    _login_fails = {}
    # 全局限速记录 {ip: [count, window_start]}
    _rate_limits = {}
    # 注册限速记录 {ip: [count, window_start]}
    _register_limits = {}
    # 登录频率限制 {ip: [count, window_start]}：比全局限速更严，防脚本化密码喷洒
    _login_rate = {}

    def _ip_store_trim(store, max_age):
        """IP 计数 dict 超限时清理过期条目：仅当长度超上限才遍历，避免每请求开销。

        各 store 的值为二元/三元组，末位统一是时间戳；防止公网扫描器用海量
        不同 IP 打爆内存（无界增长 DoS）。
        """
        if len(store) <= _IP_STORE_LIMIT:
            return
        now = time.time()
        stale = [k for k, v in store.items() if now - v[-1] > max_age]
        for k in stale:
            store.pop(k, None)

    # ---- 全局限速：防疯狂刷新/脚本轰炸（所有请求，含页面与 API）----
    @app.before_request
    def rate_limit():
        ip = request.remote_addr or "?"
        now = time.time()
        _ip_store_trim(_rate_limits, _IP_STORE_MAX_AGE)
        cnt, start = _rate_limits.get(ip, (0, now))
        if now - start > RATE_WINDOW:
            cnt, start = 0, now
        cnt += 1
        _rate_limits[ip] = (cnt, start)
        if cnt > RATE_MAX:
            return jsonify({"error": "请求过于频繁，请稍后再试"}), 429

    # ---- 认证守卫：/api/* 需登录；普通用户仅限 my-* 与 clock ----
    @app.before_request
    def require_login():
        if not request.path.startswith("/api/"):
            return
        if request.path in ("/api/login", "/api/register"):
            return
        # 公告/更新日志读取对所有用户开放（含未登录，登录页也显示）
        if request.path in ("/api/announcement", "/api/changelog") and request.method == "GET":
            return
        role = _current_role()
        if role is None:
            return jsonify({"error": "未登录"}), 401
        if role == "admin":
            return
        # 普通用户：只能操作自己的账号（/api/my-*）、读取时钟、查询身份与登出
        if request.path.startswith("/api/my-") or request.path in (
            "/api/clock",
            "/api/me",
            "/api/logout",
            "/api/me/password",
        ):
            return
        return jsonify({"error": "无权限"}), 403

    # ---- CSRF 防护：登录后所有写请求（POST/PUT/DELETE）必须携带与 session 匹配的 token ----
    # 登录/注册无需 token（未登录态，跨站表单攻击由 SameSite=Lax 已基本阻断）；
    # 已登录用户的写操作由 token 双重校验（借鉴 flask-wtf 的 Session 方案，自实现零依赖）。
    def get_csrf_token():
        """惰性生成并返回当前会话的 CSRF token。"""
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(32)
        return session["csrf_token"]

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

    @app.before_request
    def check_csrf():
        if request.method not in ("POST", "PUT", "DELETE"):
            return
        if not request.path.startswith("/api/"):
            return
        if request.path in ("/api/login", "/api/register"):
            # 未登录态无 session token：用同源校验阻断跨站登录/注册 CSRF
            if not _is_same_origin():
                logger.warning(
                    "跨站登录/注册被拒绝: ip=%s path=%s origin=%s",
                    request.remote_addr,
                    request.path,
                    request.headers.get("Origin"),
                )
                return jsonify({"error": "请求来源校验失败"}), 403
            return
        token = request.headers.get("X-CSRF-Token", "")
        sess_token = session.get("csrf_token", "")
        if not token or not secrets.compare_digest(token, sess_token):
            logger.warning(
                "CSRF 校验失败: ip=%s path=%s token_len=%d session_token_len=%d",
                request.remote_addr,
                request.path,
                len(token),
                len(sess_token),
            )
            return jsonify({"error": "请求校验失败，请刷新页面后重试"}), 403

    # ---- 页面（服务端按登录态重定向，避免未登录时先渲染后台造成闪烁）----
    @app.route("/")
    def index_page():
        role = _current_role()
        if role is None:
            return redirect("/login")
        if role != "admin":
            return redirect("/user")
        return render_template("index.html", web_version=WEB_VERSION, app_version=APP_VERSION)

    @app.route("/user")
    def user_page():
        role = _current_role()
        if role is None:
            return redirect("/login")
        if role != "user":
            return redirect("/")
        return render_template("user.html", web_version=WEB_VERSION, app_version=APP_VERSION)

    # 登录页循环检测 {ip: [count, first_ts]}：浏览器缓存旧 JS 时可能无限 302 循环，
    # 同 IP 短时间频繁访问 /login 超过阈值 → 直接渲染登录页打断循环
    _login_loop = {}

    @app.route("/login")
    def login_page():
        if session.get("auth"):
            ip = request.remote_addr or "?"
            now = time.time()
            _ip_store_trim(_login_loop, 60)
            cnt, first = _login_loop.get(ip, (0, now))
            if now - first > 10:
                cnt, first = 0, now
            cnt += 1
            _login_loop[ip] = (cnt, first)
            if cnt < 4:
                return redirect("/" if _current_role() == "admin" else "/user")
            logger.warning("检测到登录页访问循环（IP %s），已打断并渲染登录页", ip)
        return render_template("login.html", web_version=WEB_VERSION, app_version=APP_VERSION)

    # ---- 页面缓存策略：管理页面禁止缓存（防浏览器缓存旧版 JS 导致登录循环）----
    @app.after_request
    def no_cache(resp):
        # 全站安全头（所有响应，含 API）：防 MIME 嗅探 / 点击劫持 / 泄露来源 / XSS 与注入面
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'"
        )
        if request.path in ("/", "/login", "/user"):
            resp.headers["Cache-Control"] = "no-store"
        elif request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "public, max-age=86400"  # 静态资源长缓存（版本变化由 ?v= 兜底）
        return resp

    # ---- 数据层错误保护：SQLite 读写/密文解密失败 → 明确 500（防静默降级或返回错误数据）----
    @app.errorhandler(RuntimeError)
    def _handle_data_error(e):
        logger.error("数据层错误: %s", e)
        return jsonify({"error": str(e)}), 500

    # ---- 认证 API ----
    @app.route("/api/login", methods=["POST"])
    def api_login():
        """登录：管理员（.env 配置）或普通用户（users.json 注册）。返回 role。"""
        ip = request.remote_addr or "?"
        now = time.time()
        data = request.get_json(silent=True) or {}
        username = str(
            data.get("username", "")
        ).strip()  # 管理员用户名保持原样；邮箱仅用户登录时小写
        password = str(data.get("password", ""))
        # 失败计数按 (IP, 用户名) 组合：同一出口 IP 的用户不因他人爆破尝试被连带锁定
        # 值三元组 (count, lock_until, last_ts)：last_ts 供超限清理
        fail_key = (ip, username.lower())
        _ip_store_trim(_login_fails, LOGIN_LOCK_SECONDS + _IP_STORE_MAX_AGE)
        fails, lock_until, _ = _login_fails.get(fail_key, (0, 0, 0))
        if now < lock_until:
            remain = int(lock_until - now)
            return jsonify({"error": f"失败次数过多，请 {remain} 秒后重试"}), 429
        # 登录频率限制（60 秒窗口 10 次/IP，比全局限速更严）：防换用户名密码喷洒
        _ip_store_trim(_login_rate, 60 + _IP_STORE_MAX_AGE)
        lcnt, lstart = _login_rate.get(ip, (0, now))
        if now - lstart > 60:
            lcnt, lstart = 0, now
        if lcnt >= 10:
            return jsonify({"error": "登录尝试过于频繁，请稍后再试"}), 429
        _login_rate[ip] = (lcnt + 1, lstart)

        role = None
        pw_version = None
        # 1) 内置管理员（.env，兜底超级管理员）
        if verify_admin(username, password):
            role = "admin"
            pw_version = load_env_int(ENV_FILE, "YIBAN_ADMIN_PW_VERSION", 1)  # 改密后旧会话失效
        else:
            # 2) 普通用户（users，邮箱登录，不区分大小写；role 支持多管理员）
            email = username.lower()
            u = db.find_user(email)
            if u and check_password_hash(u.get("password_hash", ""), password):
                role = "admin" if u.get("role") == "admin" else "user"
                pw_version = u.get("pw_version", 1)
        if role:
            _login_fails.pop(fail_key, None)
            # 防 session 固定：登录成功先清空再重建会话
            session.clear()
            session.permanent = True
            session["auth"] = True
            session["role"] = role
            session["username"] = username
            session["pw_version"] = pw_version  # 密码版本（注册用户改密/被重置后旧会话失效）
            return jsonify({"ok": True, "role": role})
        fails += 1
        if fails >= LOGIN_MAX_FAILS:
            _login_fails[fail_key] = (0, now + LOGIN_LOCK_SECONDS, now)
            logger.warning("登录失败次数过多，IP %s 锁定 %s 秒", ip, LOGIN_LOCK_SECONDS)
            return jsonify(
                {"error": f"密码错误次数过多，已锁定 {LOGIN_LOCK_SECONDS // 60} 分钟"}
            ), 429
        # 连续失败达到阈值时告警（每轮锁定只发一次），提示可能为暴力破解
        if fails == LOGIN_FAIL_NOTIFY:
            send_notification(
                "登录失败告警",
                f"IP {ip} 连续 {fails} 次登录失败（尝试用户名: {username}）\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"如非本人操作，请检查是否有人尝试暴力破解",
            )
        _login_fails[fail_key] = (fails, 0, now)
        return jsonify({"error": "用户名或密码错误"}), 401

    @app.route("/api/register", methods=["POST"])
    def api_register():
        """开放注册普通用户：邮箱 + 密码（哈希存储）。

        邮箱格式校验；邮箱全局唯一；不做验证码服务。无昵称体系（一人一号，账号备注名在账号表单中填写）。
        """
        data = request.get_json(silent=True) or {}
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        if len(email.split("@")[0]) > EMAIL_USER_MAX:
            return jsonify({"error": f"邮箱用户名部分过长（最多 {EMAIL_USER_MAX} 字符）"}), 400
        if not EMAIL_RE.match(email) or len(email) > 64:
            return jsonify({"error": "请输入有效的邮箱地址"}), 400
        pw_err = _password_policy_error(password)
        if pw_err:
            return jsonify({"error": pw_err}), 400
        # 注册限速：同 IP 窗口内成功注册次数超限则拒绝（防邮箱批量注册）
        ip = request.remote_addr or "?"
        now = time.time()
        _ip_store_trim(_register_limits, REGISTER_WINDOW)
        rcnt, rstart = _register_limits.get(ip, (0, now))
        if now - rstart > REGISTER_WINDOW:
            rcnt, rstart = 0, now
        if rcnt >= REGISTER_MAX:
            return jsonify({"error": f"注册过于频繁，请 {REGISTER_WINDOW // 60} 分钟后再试"}), 429
        # 操作级锁：邮箱唯一性检查与写入原子（UNIQUE 约束兜底并发注册）
        with _file_lock:
            if db.find_user(email) is not None:
                return jsonify({"error": "该邮箱已注册"}), 400
            try:
                db.create_user(
                    email,
                    generate_password_hash(password, method=SCRYPT_METHOD),
                    role="user",
                    created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    pw_version=1,  # 密码版本：改密时递增，旧会话随之失效
                )
            except sqlite3.IntegrityError:
                return jsonify({"error": "该邮箱已注册"}), 400  # 并发注册兜底
            db.audit(email, "user_register", email, "开放注册")
        _register_limits[ip] = (rcnt + 1, rstart)
        logger.info("新用户注册: %s", _mask_email(email))
        return jsonify({"ok": True})

    @app.route("/api/logout", methods=["POST"])
    def api_logout():
        session.clear()
        return jsonify({"ok": True})

    @app.route("/api/me/password", methods=["POST"])
    def api_me_password():
        """所有用户自助修改自己的密码（账号不可修改）。

        内置管理员（.env）验证当前口令后写入新哈希（YIBAN_ADMIN_PASSWORD_HASH，scrypt），
        并清理旧明文；注册用户（含提升的管理员）验证当前密码后更新 users.json 哈希。
        失败计数与登录共用限速：达阈值（LOGIN_MAX_FAILS）锁定，超阈值返回 429；
        改密成功后轮换 SECRET_KEY（旧会话全部失效，防已泄露密钥继续可用）。
        """
        data = request.get_json(silent=True) or {}
        old_password = str(data.get("old_password", ""))
        new_password = str(data.get("new_password", ""))
        pw_err = _password_policy_error(new_password)
        if pw_err:
            return jsonify({"error": f"新密码不符合要求：{pw_err}"}), 400
        username = session.get("username", "")
        ip = request.remote_addr or "?"
        now = time.time()
        # 失败计数键与登录一致：按 (IP, 用户名) 组合
        fail_key = (ip, username.strip().lower())
        fails, lock_until, _ = _login_fails.get(fail_key, (0, 0, 0))
        if now < lock_until:
            return jsonify({"error": f"尝试次数过多，请 {int(lock_until - now)} 秒后重试"}), 429

        def _pw_failed():
            """当前密码校验失败：递增失败计数，达阈值锁定（与 api_login 一致）。"""
            nfails = fails + 1
            if nfails >= LOGIN_MAX_FAILS:
                _login_fails[fail_key] = (0, now + LOGIN_LOCK_SECONDS, now)
                logger.warning("改密失败次数过多，IP %s 锁定 %s 秒", ip, LOGIN_LOCK_SECONDS)
                return jsonify(
                    {"error": f"密码错误次数过多，已锁定 {LOGIN_LOCK_SECONDS // 60} 分钟"}
                ), 429
            if nfails == LOGIN_FAIL_NOTIFY:
                send_notification(
                    "改密失败告警",
                    f"IP {ip} 连续 {nfails} 次修改密码失败（用户名: {username}）\n"
                    f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"如非本人操作，请检查是否有人尝试暴力破解",
                )
            _login_fails[fail_key] = (nfails, 0, now)
            return jsonify({"error": "当前密码不正确"}), 400

        def _rotate_secret_key():
            """改密成功后轮换 SECRET_KEY：已登录旧会话全部失效，防已泄露密钥继续可用。"""
            new_key = secrets.token_hex(32)
            write_env_key(ENV_FILE, "YIBAN_SECRET_KEY", new_key)
            app.config["SECRET_KEY"] = new_key  # 当前进程立即生效（会话签名按请求实时读取）
            logger.info("SECRET_KEY 已轮换（改密触发）")

        # 内置管理员：验证 .env 当前口令后更新
        if username.strip().lower() == _builtin_admin_email():
            if not verify_admin(username, old_password):
                return _pw_failed()
            write_env_key(
                ENV_FILE,
                "YIBAN_ADMIN_PASSWORD_HASH",
                generate_password_hash(new_password, method=SCRYPT_METHOD),
            )
            write_env_key(ENV_FILE, "YIBAN_ADMIN_PASSWORD", "")  # 清理旧明文口令，改由哈希校验
            write_env_int(  # 密码版本递增：已登录的旧会话随之失效
                ENV_FILE,
                "YIBAN_ADMIN_PW_VERSION",
                load_env_int(ENV_FILE, "YIBAN_ADMIN_PW_VERSION", 1) + 1,
            )
            _login_fails.pop(fail_key, None)
            _rotate_secret_key()
            logger.info("内置管理员密码已更新")
            return jsonify({"ok": True, "msg": "密码已更新，下次登录使用新密码"})
        # 注册用户（含提升的管理员）：db 单行更新（事务内，防并发覆盖）
        with _file_lock:
            u = db.find_user(username.strip().lower())
            if u is not None:
                if not check_password_hash(u.get("password_hash", ""), old_password):
                    return _pw_failed()
                db.update_user(
                    u["email"],
                    {
                        "password_hash": generate_password_hash(new_password, method=SCRYPT_METHOD),
                        "pw_version": u.get("pw_version", 1) + 1,  # 旧会话随之失效
                    },
                )
                db.audit(username, "user_password", username, "自助改密")
                _login_fails.pop(fail_key, None)
                _rotate_secret_key()
                logger.info("用户 %s 已修改自己的密码", _mask_email(username))
                return jsonify({"ok": True, "msg": "密码已更新，下次登录使用新密码"})
        return jsonify({"error": "用户不存在"}), 404

    @app.route("/api/me")
    def api_me():
        # admin 字段为旧版前端兼容（早期前端检查 me.admin；新版用 role）——
        # 防止浏览器缓存旧页面时误判未登录导致刷新循环
        role = _current_role()
        username = session.get("username") or ""
        return jsonify(
            {
                "ok": True,
                "auth": bool(session.get("auth")),
                "role": role,
                "username": username,
                "email": username,  # 普通用户顶部显示邮箱前缀（管理员为用户名）
                "admin": role == "admin",
                "is_builtin_admin": role == "admin" and username.strip().lower()
                == _builtin_admin_email(),  # 主管理员（.env）：仅主管理员可改管理员权限
                "csrf_token": get_csrf_token(),
            }
        )

    # ---- 账号管理 ----
    @app.route("/api/accounts")
    def api_accounts():
        accounts = load_accounts()
        return jsonify(
            {
                "ok": True,
                "accounts": [mask_account(a, i) for i, a in enumerate(accounts)],
                "config_file": os.path.basename(DB_FILE),
            }
        )

    @app.route("/api/accounts/<int:idx>/detail")
    def api_account_detail(idx):
        """账号完整信息（仅管理员；列表接口已脱敏，编辑/签到等操作按需取完整号）。"""
        accounts = load_accounts()
        if not 0 <= idx < len(accounts):
            return jsonify({"error": "账号不存在"}), 404
        return jsonify({"ok": True, "account": mask_account(accounts[idx], idx, masked=False)})

    @app.route("/api/accounts", methods=["POST"])
    def api_account_add():
        """添加账号。

        - 不填邮箱：管理员自有账号（owner=admin，直接生效）
        - 填用户邮箱：账号归属该用户并进入待审核（仍需管理员点"通过"）；
          邮箱未注册时自动创建网站用户（生成临时密码，需告知用户）。
        """
        # 操作级锁：手机号唯一/每人限 1/自动注册检查与写入原子（防并发重复添加与覆盖丢失）
        data = request.get_json(silent=True) or {}
        err, clean = validate_account(data, require_password=True)
        if err:
            return jsonify({"error": err}), 400
        email = str(data.get("email", "")).strip().lower()
        initial_hash = None  # 锁外预计算（scrypt ~100ms 不阻塞其他请求）
        if email:
            initial = str(data.get("initial_password", ""))
            if initial:
                pw_err = _password_policy_error(initial)
                if pw_err:
                    return jsonify({"error": f"初始密码不符合要求：{pw_err}"}), 400
                initial_hash = generate_password_hash(initial, method=SCRYPT_METHOD)
        with _file_lock:
            accounts = load_accounts()
            if find_account_index(accounts, clean["phone"]) is not None:
                return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400

            if email:
                if len(email.split("@")[0]) > EMAIL_USER_MAX:
                    return jsonify({"error": f"邮箱用户名部分过长（最多 {EMAIL_USER_MAX} 字符）"}), 400
                if not EMAIL_RE.match(email) or len(email) > 64:
                    return jsonify({"error": "用户邮箱格式不正确"}), 400
                # 该用户已有账号（每人限 1 个）则拒绝（软删除的不占名额，与用户端一致）
                if any(a.get("owner") == email and not a.get("deleted") for a in accounts):
                    return jsonify({"error": f"{email} 已有一个账号，无需重复添加"}), 400
                # 自动注册：邮箱未注册则创建网站用户（初始密码由管理员在表单中设置，不生成明文临时密码）
                if db.find_user(email) is None:
                    if initial_hash is None:
                        return jsonify({"error": f"{email} 尚未注册，请填写「初始密码」为其创建首登密码"}), 400
                    with contextlib.suppress(sqlite3.IntegrityError):
                        # 并发已注册：继续归属流程（下方不重复注册）
                        db.create_user(
                            email, initial_hash, "user",
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 1,
                        )
                    logger.info("为邮箱 %s 自动注册用户（管理员设置初始密码）", _mask_email(email))
                clean["owner"] = email
                clean["status"] = STATUS_PENDING
            else:
                clean["owner"] = "admin"
                clean["status"] = STATUS_ACTIVE
            try:
                db.add_account(clean)
            except sqlite3.IntegrityError:
                return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400  # 并发重复兜底
            db.audit(
                session.get("username") or "?",
                "account_add",
                _mask_phone(clean["phone"]),
                f"归属 {_mask_email(clean['owner'])} 状态 {clean['status']}",
            )
            accounts = load_accounts()  # 重读（含新行，返回前端列表）
        logger.info(
            "添加账号 %s（归属 %s，状态 %s）",
            _mask_phone(clean["phone"]),
            _mask_email(clean["owner"]),
            clean["status"],
        )
        return jsonify(
            {
                "ok": True,
                "msg": "已添加，等待审核通过后参与签到",
                "accounts": [mask_account(a, i) for i, a in enumerate(accounts)],
            }
        )

    @app.route("/api/accounts/<int:idx>", methods=["PUT"])
    def api_account_update(idx):
        with _file_lock:
            accounts = load_accounts()
            if not 0 <= idx < len(accounts):
                return jsonify({"error": "账号不存在"}), 404
            old = accounts[idx]
            # 软删除账号禁止编辑（防编辑流程绕过软删除，恢复需走 restore 接口）
            if old.get("deleted"):
                return jsonify({"error": "账号已删除，请先恢复"}), 400
            data = request.get_json(silent=True) or {}
            # 乐观锁：请求携带编辑打开时的账号快照（JSON 字符串），与库内当前值
            # 不一致 → db 返回 False → 409，防多管理员/多标签页并发编辑互相覆盖
            snapshot = None
            snapshot_raw = data.get("_snapshot") or ""
            if snapshot_raw:
                try:
                    snapshot = (
                        json.loads(snapshot_raw) if isinstance(snapshot_raw, str) else snapshot_raw
                    )
                except json.JSONDecodeError:
                    snapshot = None
            err, clean = validate_account(data, require_password=False)
            if err:
                return jsonify({"error": err}), 400
            # 手机号变更时检查冲突（排除自己）
            if (
                clean["phone"] != old.get("phone")
                and find_account_index(accounts, clean["phone"]) is not None
            ):
                return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400
            # 密码留空 = 保持不变（密码明文永不下发前端）
            if not clean["password"]:
                clean["password"] = old.get("password", "")
            # 设备识别码：__clear__ = 显式清空该字段；留空 = 保持不变（表单不预填防误清空）
            if clean["phone_code"] == CLEAR_SENTINEL:
                clean.pop("phone_code", None)
            elif not clean["phone_code"]:
                clean["phone_code"] = old.get("phone_code", "")
            # 归属与审核状态保持不变（管理员编辑不改变提交者与生效状态）
            clean["owner"] = old.get("owner", "admin")
            clean["status"] = old.get("status", STATUS_ACTIVE)
            try:
                result = db.update_account(
                    old["id"],
                    clean,
                    expect_snapshot=snapshot if isinstance(snapshot, dict) else None,
                )
            except sqlite3.IntegrityError:
                return jsonify({"error": f"手机号 {clean['phone']} 已存在"}), 400  # 并发改号兜底
            if result is False:
                return jsonify({"error": "账号已被其他管理员修改，请刷新后重试"}), 409
            if result is None:
                return jsonify({"error": "账号不存在"}), 404
            db.audit(
                session.get("username") or "?",
                "account_update",
                _mask_phone(clean["phone"]),
                "编辑账号",
            )
            accounts = load_accounts()
            logger.info("编辑账号 %s", _mask_phone(clean["phone"]))
            return jsonify(
                {"ok": True, "accounts": [mask_account(a, i) for i, a in enumerate(accounts)]}
            )

    @app.route("/api/accounts/batch", methods=["POST"])
    def api_accounts_batch():
        """批量操作账号（批量多选功能）：approve/reject 审核，purge 彻底删除。

        body: {"action": ..., "ids": [...], "reason": "批量拒绝理由"}
        """
        with _file_lock:
            accounts = load_accounts()
            data = request.get_json(silent=True) or {}
            action = data.get("action")
            ids = data.get("ids") or []
            if action not in ("approve", "reject", "purge", "restore", "delete"):
                return jsonify({"error": "未知操作"}), 400
            if not isinstance(ids, list) or not ids:
                return jsonify({"error": "请选择要操作的账号"}), 400
            reason = str(data.get("reason", "")).strip()[:100]
            if action == "reject" and not reason:
                return jsonify({"error": "批量拒绝需要填写理由"}), 400
            valid = sorted(
                {i for i in ids if isinstance(i, int) and 0 <= i < len(accounts)}, reverse=True
            )
            if not valid:
                return jsonify({"error": "所选账号不存在"}), 404
            if action == "restore":
                # 恢复防呆：归属用户名下已有其他未删除账号时整批拒绝（每人限 1 个，
                # 防恢复后同一用户出现多套账号；校验在变更前完成，避免部分生效）
                for i in valid:
                    acc = accounts[i]
                    if acc.get("deleted") and _owner_has_other_live(accounts, acc):
                        return jsonify(
                            {
                                "error": f"账号「{acc.get('name', '')}」的归属用户已有生效账号，无法恢复（每人限 1 个）"
                            }
                        ), 400
            done = 0
            try:
                for i in valid:
                    acc = accounts[i]
                    if action == "approve":
                        # 软删除账号不可被审核通过（deleted 账号不参与审核流转）
                        if not acc.get("deleted") and acc.get("status") in (
                            STATUS_PENDING,
                            STATUS_REJECTED,
                        ):
                            db.update_account_status(acc["id"], STATUS_ACTIVE, reject_reason="")
                            done += 1
                    elif action == "reject":
                        if acc.get("status") in (STATUS_PENDING, STATUS_REJECTED):
                            db.update_account_status(acc["id"], STATUS_REJECTED, reason)
                            done += 1
                    elif action == "purge":
                        # 仅允许彻底删除「已软删除」账号（与单个彻底删除一致，防误删正常账号）
                        if not acc.get("deleted"):
                            continue
                        db.purge_account(acc["id"])
                        done += 1
                    elif action == "restore" and acc.get("deleted"):
                        db.set_account_deleted(acc["id"], 0)
                        done += 1
                    elif action == "delete" and not acc.get("deleted"):
                        # 软删除：进入待删除列表（保留期内可恢复），与单个删除一致
                        db.set_account_deleted(
                            acc["id"], 1, datetime.now().isoformat(timespec="seconds")
                        )
                        done += 1
            except RuntimeError as e:
                # 循环中断（如数据库异常）：已处理部分已生效，明确告知避免用户困惑
                logger.error("批量%s中断: %s（已处理 %d 个）", action, e, done)
                db.audit(
                    session.get("username") or "?",
                    "account_batch",
                    action,
                    f"中断，已处理 {done} 个",
                )
                return jsonify(
                    {"ok": True, "msg": f"批量操作中断，已处理 {done} 个（{e}）"}
                )
            db.audit(
                session.get("username") or "?",
                "account_batch",
                action,
                f"处理 {done} 个",
            )
            accounts = load_accounts()
            logger.info("批量%s账号 %d 个", action, done)
            msg = {
                "approve": f"已通过 {done} 个账号",
                "reject": f"已拒绝 {done} 个账号",
                "purge": f"已彻底删除 {done} 个账号",
                "restore": f"已恢复 {done} 个账号",
                "delete": f"已删除 {done} 个账号（可恢复）",
            }[action]
            return jsonify(
                {
                    "ok": True,
                    "msg": msg,
                    "accounts": [mask_account(a, i) for i, a in enumerate(accounts)],
                }
            )

    @app.route("/api/accounts/<int:idx>", methods=["DELETE"])
    def api_account_delete(idx):
        """删除账号（软删除）：进入待删除状态，保留期内可恢复，超期自动彻底清除。"""
        with _file_lock:
            accounts = load_accounts()
            if not 0 <= idx < len(accounts):
                return jsonify({"error": "账号不存在"}), 404
            acc = accounts[idx]
            db.set_account_deleted(
                acc["id"], 1, datetime.now().isoformat(timespec="seconds")
            )
            db.audit(
                session.get("username") or "?",
                "account_delete",
                _mask_phone(acc.get("phone", "")),
                "软删除",
            )
            accounts = load_accounts()
            logger.info(
                "软删除账号 %s（%s 天内可恢复）", _mask_phone(acc.get("phone", "")), DELETED_RETENTION_DAYS
            )
            return jsonify(
                {
                    "ok": True,
                    "msg": f"已删除「{acc.get('name', '')}」，{DELETED_RETENTION_DAYS} 天内可在待删除列表恢复",
                    "accounts": [mask_account(a, i) for i, a in enumerate(accounts)],
                }
            )

    @app.route("/api/accounts/<int:idx>/restore", methods=["POST"])
    def api_account_restore(idx):
        """恢复待删除账号：撤销软删除，回到删除前状态。"""
        with _file_lock:
            accounts = load_accounts()
            if not 0 <= idx < len(accounts):
                return jsonify({"error": "账号不存在"}), 404
            acc = accounts[idx]
            if not acc.get("deleted"):
                return jsonify({"error": "该账号不在待删除状态"}), 400
            # 防呆：归属用户名下已有其他未删除账号则拒绝恢复（每人限 1 个，防恢复后重复）
            if _owner_has_other_live(accounts, acc):
                return jsonify(
                    {"error": "该用户已有生效账号，无法恢复（每人限 1 个）"}
                ), 400
            db.set_account_deleted(acc["id"], 0)
            db.audit(
                session.get("username") or "?",
                "account_restore",
                _mask_phone(acc.get("phone", "")),
                "撤销软删除",
            )
            accounts = load_accounts()
            logger.info("恢复账号 %s", _mask_phone(acc.get("phone", "")))
            return jsonify(
                {
                    "ok": True,
                    "msg": f"已恢复「{acc.get('name', '')}」",
                    "accounts": [mask_account(a, i) for i, a in enumerate(accounts)],
                }
            )

    @app.route("/api/accounts/<int:idx>/purge", methods=["POST"])
    def api_account_purge(idx):
        """彻底删除待删除账号：立即物理清除，不可恢复。"""
        with _file_lock:
            accounts = load_accounts()
            if not 0 <= idx < len(accounts):
                return jsonify({"error": "账号不存在"}), 404
            acc = accounts[idx]
            if not acc.get("deleted"):
                return jsonify({"error": "该账号不在待删除状态"}), 400
            db.purge_account(acc["id"])
            db.audit(
                session.get("username") or "?",
                "account_purge",
                _mask_phone(acc.get("phone", "")),
                "彻底删除",
            )
            accounts = load_accounts()
            logger.info("彻底删除账号 %s", _mask_phone(acc.get("phone", "")))
            return jsonify(
                {
                    "ok": True,
                    "msg": f"已彻底删除「{acc.get('name', '')}」",
                    "accounts": [mask_account(a, i) for i, a in enumerate(accounts)],
                }
            )

    @app.route("/api/accounts/<int:idx>/review", methods=["POST"])
    def api_account_review(idx):
        """审核普通用户提交的账号：
        approve=生效参与定时签到；reject=标记拒绝并附理由（用户可编辑后重新提交）。
        """
        with _file_lock:
            accounts = load_accounts()
            if not 0 <= idx < len(accounts):
                return jsonify({"error": "账号不存在"}), 404
            action = (request.get_json(silent=True) or {}).get("action")
            acc = accounts[idx]
            if action == "approve":
                # 软删除账号不可被审核通过（deleted 账号不参与审核流转）
                if acc.get("deleted") or acc.get("status") not in (STATUS_PENDING, STATUS_REJECTED):
                    return jsonify({"error": "该账号无需审核"}), 400
                db.update_account_status(acc["id"], STATUS_ACTIVE, reject_reason="")
                db.audit(
                    session.get("username") or "?",
                    "account_review",
                    _mask_phone(acc.get("phone", "")),
                    "approve",
                )
                logger.info("审核通过账号 %s（提交者 %s）", _mask_phone(acc.get("phone", "")), _mask_email(acc.get("owner", "")))
                return jsonify({"ok": True, "msg": f"已通过 {acc.get('phone')}，将参与定时签到"})
            if action == "reject":
                if acc.get("status") not in (STATUS_PENDING, STATUS_REJECTED):
                    return jsonify({"error": "该账号无需拒绝"}), 400
                # 理由清洗：换行/控制字符 → 空格（防日志注入伪造日志行）
                reason = (
                    str((request.get_json(silent=True) or {}).get("reason", ""))
                    .strip()[:100]
                    .replace("\r", " ")
                    .replace("\n", " ")
                )
                db.update_account_status(acc["id"], STATUS_REJECTED, reason)
                db.audit(
                    session.get("username") or "?",
                    "account_review",
                    _mask_phone(acc.get("phone", "")),
                    "reject" + (f" {reason[:60]}" if reason else ""),
                )
                logger.info(
                    "拒绝账号 %s（提交者 %s，理由: %s）",
                    _mask_phone(acc.get("phone", "")),
                    _mask_email(acc.get("owner", "")),
                    reason or "无",
                )
                return jsonify({"ok": True, "msg": "已拒绝，用户可查看理由并重新提交"})
        return jsonify({"error": "未知操作"}), 400

    @app.route("/api/accounts/<int:idx>/move", methods=["POST"])
    def api_account_move(idx):
        """上移/下移账号：调整顺序模式下的打卡顺序。body: {"dir": -1|1}"""
        with _file_lock:
            accounts = load_accounts()
            if not 0 <= idx < len(accounts):
                return jsonify({"error": "账号不存在"}), 404
            try:
                direction = int((request.get_json(silent=True) or {}).get("dir", 0))
            except (TypeError, ValueError):
                return jsonify({"error": "无法移动"}), 400
            if direction not in (-1, 1):
                return jsonify({"error": "无法移动"}), 400
            if not db.move_account(accounts[idx]["id"], direction):
                return jsonify({"error": "无法移动"}), 400
            db.audit(
                session.get("username") or "?",
                "account_move",
                _mask_phone(accounts[idx].get("phone", "")),
                f"dir {direction}",
            )
            accounts = load_accounts()
            return jsonify(
                {"ok": True, "accounts": [mask_account(a, i) for i, a in enumerate(accounts)]}
            )

    # ---- 普通用户：我的账号（提交 / 查看 / 编辑 / 删除，仅限本人）----
    def _my_account_indices_of(accounts):
        """按账号列表快照计算当前用户的账号下标（锁内调用，避免重复读文件）。

        管理员：内置管理员（.env）显示 owner 'admin' + 本人邮箱；注册管理员仅本人邮箱
        （不显示他人/内置管理员添加的账号）；均不含待删除。
        普通用户：本人邮箱（含待删除，用于展示「已删除」状态；单账号限制在提交处另行排除）。
        """
        email = session.get("username", "").lower()
        if _current_role() == "admin":
            if email == _builtin_admin_email():
                return [
                    i
                    for i, a in enumerate(accounts)
                    if a.get("owner") in ("admin", email) and not a.get("deleted")
                ]
            return [
                i for i, a in enumerate(accounts) if a.get("owner") == email and not a.get("deleted")
            ]
        return [i for i, a in enumerate(accounts) if a.get("owner") == email]

    def _my_account_indices():
        return _my_account_indices_of(load_accounts())

    def _my_account_view(accounts, indices):
        """用户视图：账号脱敏 + 今日状态图标 + 审核状态 + 最近相关日志 + 排队信息。

        排队说明：签到按 accounts.json 顺序执行（队列重试模式）；
        queue_ahead = 自己账号之前、今日尚未签到成功（非 ✅）的已生效账号数。
        """
        states, recent = parse_sign_log(LOG_FILE)  # 单次解析复用（原双调用读盘+正则×2）
        # 参与排队队列的账号：已生效（active，pending 不参与签到）且未软删除
        active = [
            a for a in accounts if a.get("status") == STATUS_ACTIVE and not a.get("deleted")
        ]
        # 排队位置预计算（单次遍历累计，替代每个账号 O(pos) 切片求和）
        queue_before = []
        running = 0
        for a in active:
            queue_before.append(running)
            if states.get(a.get("phone", ""), "⏳") not in ("✅",):
                running += 1
        result = []
        for i, real_idx in enumerate(indices):
            acc = accounts[real_idx]
            phone = acc.get("phone", "")
            my_logs = [line for line in recent if f"[{phone}]" in line]
            # 排队：自己账号在 active 队列中的位置之前、今日状态非 ✅ 的账号数
            queue_ahead = 0
            if acc.get("status") == STATUS_ACTIVE:
                pos = next((j for j, a in enumerate(active) if a.get("phone") == phone), None)
                if pos is not None:
                    queue_ahead = queue_before[pos]
            result.append(
                {
                    "index": i,
                    "name": acc.get("name", ""),
                    "display_name": acc.get("name") or f"账号{i + 1}",
                    "phone": phone,
                    "phone_model": acc.get("phone_model", ""),
                    "status": acc.get("status", STATUS_ACTIVE),
                    "reject_reason": acc.get("reject_reason", ""),
                    "state_icon": states.get(phone, "⏳"),
                    "queue_ahead": queue_ahead,
                    "logs": my_logs[-5:],
                    "deleted": bool(acc.get("deleted")),
                    "deleted_at": acc.get("deleted_at", ""),
                }
            )
        return result

    @app.route("/api/my-accounts")
    def api_my_accounts():
        accounts = load_accounts()
        indices = _my_account_indices()
        return jsonify({"ok": True, "accounts": _my_account_view(accounts, indices)})

    @app.route("/api/my-accounts", methods=["POST"])
    def api_my_account_add():
        """提交自己的易班账号：每个用户仅限 1 套，写入 accounts.json 状态 pending（待审核）。

        操作级锁：单账号限制与手机号唯一检查 + 写入原子（防并发双提交互相覆盖）。
        """
        with _file_lock:
            accounts = load_accounts()
            # 单账号限制：已有未删除提交（含待审核/已生效）则拒绝；待删除（管理员已删）不占名额
            email = session.get("username", "").lower()
            has_live = any(a.get("owner") == email and not a.get("deleted") for a in accounts)
            if has_live:
                return jsonify({"error": "每个用户只能提交一个账号，可编辑或删除后重新提交"}), 400
            data = request.get_json(silent=True) or {}
            err, clean = validate_account(data, require_password=True)
            if err:
                return jsonify({"error": err}), 400
            if find_account_index(accounts, clean["phone"]) is not None:
                return jsonify({"error": f"手机号 {clean['phone']} 已被使用"}), 400
            # 管理员提交的账号归属 'admin'（后台添加账号同理），直接生效免审核
            clean["owner"] = (
                "admin" if _current_role() == "admin" else session.get("username", "").lower()
            )
            clean["status"] = STATUS_PENDING if _current_role() != "admin" else STATUS_ACTIVE
            try:
                db.add_account(clean)
            except sqlite3.IntegrityError:
                return jsonify({"error": f"手机号 {clean['phone']} 已被使用"}), 400  # 并发提交兜底
            db.audit(
                clean["owner"],
                "my_account_add",
                _mask_phone(clean["phone"]),
                f"用户提交 状态 {clean['status']}",
            )
            logger.info("用户 %s 提交账号 %s（待审核）", _mask_email(clean["owner"]), _mask_phone(clean["phone"]))
            return jsonify({"ok": True, "msg": "已提交，等待管理员审核后参与签到"})

    @app.route("/api/my-calendar")
    def api_my_calendar():
        """我的账号月历：返回指定月份（YYYY-MM）每天每账号的签到状态（✅/❌/空字符串）。"""
        month = str(request.args.get("month", "")).strip()
        try:
            year, mon = map(int, month.split("-"))
            if not (2000 <= year <= 2100 and 1 <= mon <= 12):
                raise ValueError
        except Exception:
            return jsonify({"error": "月份格式不正确，应为 YYYY-MM"}), 400
        accounts = load_accounts()
        indices = _my_account_indices()
        phones = [str(accounts[i].get("phone", "")) for i in indices]
        import calendar as _cal

        days_in_month = _cal.monthrange(year, mon)[1]
        result = {f"{year:04d}-{mon:02d}-{d:02d}": {} for d in range(1, days_in_month + 1)}
        # 聚合读取：单次目录遍历取本月全部日文件（替代每天一次 exists+open 共 30 次 IO）
        prefix = f"sign-daily-{year:04d}-{mon:02d}-"
        try:
            for entry in os.scandir(STATE_DIR):
                if entry.name.startswith(prefix):
                    date = entry.name[len("sign-daily-") : -len(".json")]
                    try:
                        with open(entry.path, encoding="utf-8") as f:
                            daily = json.load(f)
                    except Exception:
                        daily = {}
                    # setdefault：异常文件名（非 YYYY-MM-DD）不落入本月键时自动补空，防 KeyError 500
                    result.setdefault(date, {}).update({p: daily.get(p, "") for p in phones})
        except OSError:
            pass  # STATE_DIR 不存在等：按无记录返回
        return jsonify({"ok": True, "month": month, "days": result})

    @app.route("/api/my-logs")
    def api_my_logs():
        """我的账号指定日期（YYYY-MM-DD）的日志（按手机号过滤，最多 50 条）。"""
        date = str(request.args.get("date", "")).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return jsonify({"error": "日期格式不正确，应为 YYYY-MM-DD"}), 400
        accounts = load_accounts()
        indices = _my_account_indices()
        phones = [str(accounts[i].get("phone", "")) for i in indices]
        prefix = f"[{date} "
        out = []
        # 倒序扫描：日志按时间追加，从尾部向前（_tail_lines 限制读取量，不整读大文件）；
        # 遇到更早日期的行即停止
        for line in reversed(_tail_lines(LOG_FILE)):
            if line.startswith(prefix):
                if any(f"[{p}]" in line for p in phones):
                    out.append(line.strip())
            elif line.startswith("[") and line < prefix:
                break
        out.reverse()
        return jsonify({"ok": True, "date": date, "logs": out[-50:]})

    @app.route("/api/my-accounts/<int:idx>", methods=["PUT"])
    def api_my_account_update(idx):
        """编辑自己提交的账号：密码/识别码留空=保留；不影响已生效状态。"""
        with _file_lock:
            accounts = load_accounts()
            indices = _my_account_indices_of(accounts)
            if not 0 <= idx < len(indices):
                return jsonify({"error": "账号不存在"}), 404
            real_idx = indices[idx]
            old = accounts[real_idx]
            # 软删除账号禁止编辑（防编辑流程绕过软删除；恢复由管理员操作）
            if old.get("deleted"):
                return jsonify({"error": "账号已删除，请先恢复"}), 400
            data = request.get_json(silent=True) or {}
            err, clean = validate_account(data, require_password=False)
            if err:
                return jsonify({"error": err}), 400
            if (
                clean["phone"] != old.get("phone")
                and find_account_index(accounts, clean["phone"]) is not None
            ):
                return jsonify({"error": f"手机号 {clean['phone']} 已被使用"}), 400
            if not clean["password"]:
                clean["password"] = old.get("password", "")
            # 设备识别码：__clear__ = 显式清空该字段；留空 = 保持不变
            if clean["phone_code"] == CLEAR_SENTINEL:
                clean.pop("phone_code", None)
            elif not clean["phone_code"]:
                clean["phone_code"] = old.get("phone_code", "")
            clean["owner"] = old.get("owner", "")
            # 被拒绝的账号编辑后 = 重新提交审核（回 pending，清除拒绝理由）
            clean["status"] = (
                STATUS_PENDING
                if old.get("status") == STATUS_REJECTED
                else old.get("status", STATUS_PENDING)
            )
            if clean["status"] == STATUS_PENDING:
                clean.pop("reject_reason", None)
            try:
                db.update_account(old["id"], clean)
            except sqlite3.IntegrityError:
                return jsonify({"error": f"手机号 {clean['phone']} 已被使用"}), 400  # 并发改号兜底
            db.audit(
                clean["owner"],
                "my_account_update",
                _mask_phone(clean["phone"]),
                "用户编辑",
            )
            logger.info("用户 %s 编辑账号 %s", _mask_email(clean["owner"]), _mask_phone(clean["phone"]))
            if old.get("status") == STATUS_REJECTED:
                return jsonify({"ok": True, "msg": "已重新提交，等待管理员审核"})
            return jsonify({"ok": True, "msg": "已保存"})

    @app.route("/api/my-accounts/<int:idx>", methods=["DELETE"])
    def api_my_account_delete(idx):
        with _file_lock:
            accounts = load_accounts()
            indices = _my_account_indices_of(accounts)
            if not 0 <= idx < len(indices):
                return jsonify({"error": "账号不存在"}), 404
            removed = accounts[indices[idx]]
            db.purge_account(removed["id"])
            db.audit(
                session.get("username", "") or "?",
                "my_account_delete",
                _mask_phone(removed.get("phone", "")),
                "用户删除",
            )
            logger.info(
                "用户 %s 删除账号 %s", session.get("username", ""), _mask_phone(removed.get("phone", ""))
            )
            return jsonify({"ok": True, "msg": "已删除"})

    # ---- 用户管理（仅管理员；路径不在普通用户白名单，自动 403）----
    def _builtin_admin_email():
        """内置管理员（.env）标识（小写），用于防呆比较：不可改角色/删除。"""
        env = read_env(ENV_FILE)
        return env.get("YIBAN_ADMIN_USER", "").strip().lower()

    def _builtin_admin_display():
        """内置管理员显示名（保留 .env 原始大小写，仅用于界面展示）。"""
        env = read_env(ENV_FILE)
        return env.get("YIBAN_ADMIN_USER", "").strip() or "admin"

    def _effective_role(username, pw_version=None):
        """实时角色判定（每次请求读取，不依赖登录时固化的 session）：
        内置管理员 → admin；注册用户 → users.json 的 role；查无此人 → None。
        管理员变更角色后，已登录用户的下一次请求立即生效，无需重新登录；
        被删除/取消权限的用户旧会话随之失效（None 视为未登录）；
        注册用户密码被重置/修改后（pw_version 递增）旧会话随之失效；
        内置管理员改密后（.env 版本递增）旧会话同样失效。
        """
        if not username:
            return None
        if username.strip().lower() == _builtin_admin_email():
            # 内置管理员：session 版本与当前 .env 版本不一致 → 视为未登录（改密后旧会话失效）
            cur = load_env_int(ENV_FILE, "YIBAN_ADMIN_PW_VERSION", 1)
            return "admin" if pw_version == cur else None
        email = username.strip().lower()
        u = db.find_user(email)
        if u is not None:
            # 旧数据（无 pw_version 字段）不做会话吊销校验，兼容存量会话
            if "pw_version" in u and pw_version != u.get("pw_version", 1):
                return None
            return "admin" if u.get("role") == "admin" else "user"
        return None

    def _current_role():
        """当前登录会话的实时角色；未登录 → None。"""
        if not session.get("auth"):
            return None
        return _effective_role(session.get("username"), session.get("pw_version"))

    def _find_user(users, email):
        return next((u for u in users if u.get("email") == email), None)

    @app.route("/api/users")
    def api_users():
        """用户列表（完整邮箱/角色/注册时间/账号数/待审核账号数）+ 内置管理员信息。"""
        users = load_users()
        accounts = load_accounts()
        result = [
            {
                "email": u.get("email", ""),
                "role": u.get("role", "user"),
                "created_at": u.get("created_at", ""),
                # 计数排除软删除账号（删除后不占账号数/待审核数）
                "account_count": sum(
                    1
                    for a in accounts
                    if a.get("owner") == u.get("email") and not a.get("deleted")
                ),
                "pending_count": sum(
                    1
                    for a in accounts
                    if a.get("owner") == u.get("email")
                    and a.get("status") == STATUS_PENDING
                    and not a.get("deleted")
                ),
            }
            for u in users
        ]
        return jsonify(
            {
                "ok": True,
                "users": result,
                "builtin_admin": _builtin_admin_display(),
            }
        )

    @app.route("/api/users/batch", methods=["POST"])
    def api_users_batch():
        """批量操作注册用户：set_admin/unset_admin/reset_password/delete。

        body: {"action": ..., "emails": [...], "password": "批量重置的新密码"}
        set_admin/unset_admin 仅主管理员（.env 内置管理员）可用；set_admin 仅限正式用户。
        """
        with _file_lock:
            users = load_users()
            data = request.get_json(silent=True) or {}
            action = data.get("action")
            emails = data.get("emails") or []
            if action not in ("set_admin", "unset_admin", "reset_password", "delete"):
                return jsonify({"error": "未知操作"}), 400
            if not isinstance(emails, list) or not emails:
                return jsonify({"error": "请选择要操作的用户"}), 400
            if any(not isinstance(e, str) or len(e) > 64 for e in emails):
                return jsonify({"error": "邮箱格式不正确"}), 400
            password = str(data.get("password", ""))
            if action == "reset_password":
                pw_err = _password_policy_error(password)
                if pw_err:
                    return jsonify({"error": f"新密码不符合要求：{pw_err}"}), 400
            # 权限：管理员权限变更仅主管理员（普通管理员可重置密码/删除，不可改权限）
            if action in ("set_admin", "unset_admin"):
                username = (session.get("username") or "").strip().lower()
                if username != _builtin_admin_email():
                    return jsonify({"error": "仅主管理员可修改管理员权限"}), 403
            builtin = _builtin_admin_email()
            accounts = load_accounts() if action == "set_admin" else None
            done = 0
            for email in emails:
                target = _find_user(users, email)
                if not target or email == builtin:  # 内置管理员不可批量操作
                    continue
                if action == "set_admin":
                    # 只能将正式用户（有生效账号且无待审核）设为管理员；
                    # 正式用户判定仅 status==active 算（rejected 不算），且软删除不算
                    has_pending = any(
                        a.get("owner") == email
                        and a.get("status") == STATUS_PENDING
                        and not a.get("deleted")
                        for a in accounts
                    )
                    has_active = any(
                        a.get("owner") == email
                        and a.get("status") == STATUS_ACTIVE
                        and not a.get("deleted")
                        for a in accounts
                    )
                    if not has_active or has_pending:
                        continue
                    db.update_user(email, {"role": "admin"})
                    done += 1
                elif action == "unset_admin":
                    # 每次循环内动态重算 admins：前一个被取消后，后续目标以最新列表判定
                    admins = [u for u in load_users() if u.get("role") == "admin"]
                    # 防呆：内置管理员不存在且这是最后一个注册管理员时跳过
                    if target.get("role") == "admin" and len(admins) <= 1 and not builtin:
                        continue
                    db.update_user(email, {"role": "user"})
                    done += 1
                elif action == "reset_password":
                    db.update_user(
                        email,
                        {
                            "password_hash": generate_password_hash(password, method=SCRYPT_METHOD),
                            "pw_version": target.get("pw_version", 1) + 1,
                        },
                    )
                    done += 1
                elif action == "delete":
                    # 防呆：目标为管理员时校验至少保留 1 个管理员
                    # （内置管理员存在时允许删除最后一个注册管理员，与单条路径一致）
                    if target.get("role") == "admin":
                        admins = [u for u in load_users() if u.get("role") == "admin"]
                        if len(admins) <= 1 and not builtin:
                            continue
                    db.delete_user_with_accounts(email)
                    done += 1
            db.audit(
                session.get("username") or "?",
                "users_batch",
                action,
                f"处理 {done} 个",
            )
            logger.info("批量%s用户 %d 个", action, done)
            msg = {
                "set_admin": f"已设为管理员 {done} 个用户",
                "unset_admin": f"已取消管理员 {done} 个用户",
                "reset_password": f"已重置密码 {done} 个用户",
                "delete": f"已删除 {done} 个用户",
            }[action]
            return jsonify({"ok": True, "msg": msg})

    @app.route("/api/users/<email>/role", methods=["POST"])
    def api_user_role(email):
        """设为管理员 / 取消管理员。仅主管理员（.env 内置管理员）可操作；
        只能将「正式用户」（有生效账号且无待审核）设为管理员；
        防呆：内置管理员不可改；至少保留 1 个管理员。"""
        # 权限：仅主管理员（普通管理员无管理员权限变更权）
        username = (session.get("username") or "").strip().lower()
        if username != _builtin_admin_email():
            return jsonify({"error": "仅主管理员可修改管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        new_role = data.get("role")
        if new_role not in ("admin", "user"):
            return jsonify({"error": "未知角色"}), 400
        # 内置管理员（.env）不可修改角色
        if email == _builtin_admin_email():
            return jsonify({"error": "内置管理员不可修改角色"}), 400
        with _file_lock:
            target = db.find_user(email)
            if not target:
                return jsonify({"error": "用户不存在"}), 404
            if new_role == "admin":
                # 只能将正式用户（有生效账号且无待审核）设为管理员；
                # 正式用户判定仅 status==active 算（rejected 不算），且软删除不算
                accounts = load_accounts()
                has_pending = any(
                    a.get("owner") == email
                    and a.get("status") == STATUS_PENDING
                    and not a.get("deleted")
                    for a in accounts
                )
                has_active = any(
                    a.get("owner") == email
                    and a.get("status") == STATUS_ACTIVE
                    and not a.get("deleted")
                    for a in accounts
                )
                if not has_active or has_pending:
                    return jsonify({"error": "仅正式用户可设为管理员（需有已生效账号且无待审核）"}), 400
            if new_role == "user" and target.get("role") == "admin":
                admins = [u for u in load_users() if u.get("role") == "admin"]
                # 内置管理员（.env）也是管理员且不可被移除——存在时允许取消 users.json 中的最后一个管理员
                if len(admins) <= 1 and not _builtin_admin_email():
                    return jsonify({"error": "至少保留 1 个管理员"}), 400
            db.update_user(email, {"role": new_role})
            db.audit(
                username,
                "user_role",
                _mask_email(email),
                f"角色 → {new_role}",
            )
            logger.info("主管理员 %s 将用户 %s 角色 → %s", _mask_email(username), _mask_email(email), new_role)
            return jsonify(
                {
                    "ok": True,
                    "msg": f"{email} 已{'设为管理员' if new_role == 'admin' else '取消管理员'}",
                }
            )

    @app.route("/api/users/<email>/password", methods=["POST"])
    def api_user_password(email):
        """重置用户密码（管理员无法查看原密码，只能设置新密码）。"""
        data = request.get_json(silent=True) or {}
        password = str(data.get("password", ""))
        pw_err = _password_policy_error(password)
        if pw_err:
            return jsonify({"error": f"新密码不符合要求：{pw_err}"}), 400
        with _file_lock:
            target = db.find_user(email)
            if not target:
                return jsonify({"error": "用户不存在"}), 404
            db.update_user(
                email,
                {
                    "password_hash": generate_password_hash(password, method=SCRYPT_METHOD),
                    "pw_version": target.get("pw_version", 1) + 1,  # 被重置用户的旧会话随之失效
                },
            )
            db.audit(
                session.get("username") or "?",
                "user_password_reset",
                _mask_email(email),
                "管理员重置密码",
            )
            logger.info("已重置用户 %s 密码", _mask_email(email))
            return jsonify({"ok": True, "msg": f"{email} 密码已重置"})

    @app.route("/api/users/<email>/delete", methods=["POST"])
    def api_user_delete(email):
        """删除用户：mode=accounts_only 仅清空其易班账号（保留用户可重新提交）；
        mode=full 完全删除用户及其账号。"""
        data = request.get_json(silent=True) or {}
        mode = data.get("mode", "full")
        if mode not in ("accounts_only", "full"):
            return jsonify({"error": "未知操作"}), 400
        if email == _builtin_admin_email():
            return jsonify({"error": "内置管理员不可删除"}), 400
        with _file_lock:
            target = db.find_user(email)
            if not target:
                return jsonify({"error": "用户不存在"}), 404
            if mode == "full" and target.get("role") == "admin":
                admins = [u for u in load_users() if u.get("role") == "admin"]
                # 内置管理员（.env）兜底存在时可删除 users.json 中的最后一个管理员
                if len(admins) <= 1 and not _builtin_admin_email():
                    return jsonify({"error": "至少保留 1 个管理员"}), 400
            # 删除其提交的易班账号（full 模式用单事务组合函数，防崩溃窗口不一致）
            if mode == "full":
                db.delete_user_with_accounts(email)
            else:
                db.delete_accounts_by_owner(email)
            db.audit(
                session.get("username") or "?",
                "user_delete",
                _mask_email(email),
                f"mode={mode}",
            )
            if mode == "full":
                logger.info("完全删除用户 %s（含易班账号）", _mask_email(email))
                return jsonify({"ok": True, "msg": f"{email} 已完全删除"})
            logger.info("清空用户 %s 的易班账号（保留用户）", _mask_email(email))
            return jsonify({"ok": True, "msg": f"{email} 的易班账号已清空（用户保留，可重新提交）"})

    # ---- 手动签到 ----
    _last_trigger = {}  # phone -> 上次触发时间戳
    _signin_procs = {}  # phone -> Popen（新触发时终止仍在运行的旧进程，防重复签到触发风控）
    _signin_lock = threading.Lock()  # 防抖检查+赋值原子化（TOCTOU 竞态防护）

    @app.route("/api/signin", methods=["POST"])
    def api_signin():
        """手动签到指定账号：子进程执行 signin.py --only（与 TUI M 键一致）。"""
        data = request.get_json(silent=True) or {}
        phone = str(data.get("phone", "")).strip()
        accounts = load_accounts()
        idx = find_account_index(accounts, phone)
        if idx is None:
            return jsonify({"error": f"账号 {phone} 不在配置中"}), 404
        acc = accounts[idx]
        # 仅可签到账号可手动触发：已生效（active）且未软删除
        if acc.get("deleted") or acc.get("status") != STATUS_ACTIVE:
            return jsonify({"error": "该账号不可手动签到（未生效或已删除）"}), 400
        with _signin_lock:  # 原子检查+占位：并发请求不能同时通过防抖
            now = time.time()
            if phone in _last_trigger and now - _last_trigger[phone] < SIGN_MIN_INTERVAL:
                remain = int(SIGN_MIN_INTERVAL - (now - _last_trigger[phone]))
                return jsonify({"error": f"该账号正在签到，请 {remain} 秒后再试"}), 429
            old = _signin_procs.get(phone)
            if old and old.poll() is None:
                old.terminate()  # 仍在运行 → 终止旧进程，防止同账号并发签到
            _last_trigger[phone] = now

        # 项目根目录（web 的上一级），与 TUI action_manual_sign 一致
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = os.path.join(base, "scripts", "signin.py")
        env = dict(os.environ)
        # 单账号手动签到：关闭随机延迟，避免等待
        env["YIBAN_START_DELAY_MAX"] = "0"
        env["YIBAN_ACCOUNT_GAP_MAX"] = "0"
        # 子进程读取与主进程相同的数据库（--db 自定义路径时保持一致）
        env["YIBAN_DB_FILE"] = DB_FILE
        # 解密 yiban.db 需要同一密钥：显式注入（--env 自定义路径时保证一致）
        if account_crypto.has_key(ENV_FILE) and not env.get("YIBAN_ACCOUNTS_KEY"):
            env["YIBAN_ACCOUNTS_KEY"] = account_crypto.load_key(ENV_FILE).hex()
        log_fh = None
        from contextlib import suppress

        with suppress(OSError):
            log_fh = open(
                LOG_FILE, "a", encoding="utf-8", buffering=1
            )  # 日志不可写时丢弃，不影响签到执行
        try:
            proc = subprocess.Popen(
                [sys.executable, script, "--only", phone],
                cwd=base,
                env=env,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )
            _signin_procs[phone] = proc  # 记录子进程，供下次触发时终止旧进程
        except FileNotFoundError:
            with _signin_lock:
                _last_trigger.pop(phone, None)
            return jsonify({"error": "手动签到启动失败，请稍后重试"}), 500
        finally:
            if log_fh is not None:
                log_fh.close()  # 父进程关闭句柄（子进程已继承自身副本），防句柄累积
        logger.info("触发手动签到: %s", _mask_phone(phone))
        return jsonify(
            {"ok": True, "msg": f"已触发 {phone} 手动签到（后台执行，日志约 30 秒内刷新）"}
        )

    # ---- 日志与状态 ----
    @app.route("/api/logs")
    def api_logs():
        states, recent = parse_sign_log(LOG_FILE)
        # 响应层脱敏：states 键与日志行内 [手机号] 不落完整号（前端 maskPhone 幂等兼容）
        return jsonify(
            {
                "ok": True,
                "states": {_mask_phone(k): v for k, v in states.items()},
                "logs": [re.sub(r"\[(\d{11})\]", lambda m: "[" + _mask_phone(m.group(1)) + "]", ln) for ln in recent[-80:]],
                "log_file": os.path.basename(LOG_FILE),  # 只暴露文件名，不暴露服务器路径
            }
        )

    # ---- 设置（随机延迟，写入 .env）----
    @app.route("/api/settings")
    def api_settings():
        return jsonify(
            {
                "ok": True,
                "start_delay_max": load_env_int(ENV_FILE, "YIBAN_START_DELAY_MAX", 0),
                "gap_max": load_env_int(ENV_FILE, "YIBAN_ACCOUNT_GAP_MAX", 0),
                "default_start_delay_max": DEFAULT_START_DELAY_MAX,
                "default_gap_max": DEFAULT_ACCOUNT_GAP_MAX,
                # 签到模式：sequence（列表顺序，默认）/ random（列表随机打散）
                "sign_mode": read_env(ENV_FILE).get("YIBAN_SIGN_MODE", "").strip() or "sequence",
                # 批量多选：前端会话级开关（不持久化，每次进入页面默认关闭）
                "batch_mode": False,
            }
        )

    @app.route("/api/settings", methods=["POST"])
    def api_settings_save():
        data = request.get_json(silent=True) or {}
        try:
            start = int(data.get("start_delay_max", 0))
            gap = int(data.get("gap_max", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "延迟秒数必须是整数"}), 400
        # 上限 1 小时：防止误填超大值破坏签到随机延迟
        start = min(max(start, 0), 3600)
        gap = min(max(gap, 0), 3600)
        write_env_int(ENV_FILE, "YIBAN_START_DELAY_MAX", start)
        write_env_int(ENV_FILE, "YIBAN_ACCOUNT_GAP_MAX", gap)
        # 签到模式（sequence/random）：写入 .env，cron 的 run.sh 加载后 signin.py 生效
        sign_mode = str(data.get("sign_mode", "")).strip().lower()
        if sign_mode and sign_mode not in ("sequence", "random"):
            return jsonify({"error": "签到模式取值应为 sequence 或 random"}), 400
        if sign_mode:
            write_env_key(ENV_FILE, "YIBAN_SIGN_MODE", sign_mode)
        # 批量多选为前端会话级开关，不写入配置
        logger.info("更新设置: 启动=%s 间隔=%s 签到模式=%s", start, gap, sign_mode or "不变")
        return jsonify({"ok": True, "msg": "设置已保存（cron 下次触发自动生效）"})

    # ---- 全局公告（所有页面顶部显示；GET 公开，PUT 仅管理员）----
    @app.route("/api/changelog")
    def api_changelog():
        """更新日志：读取项目根 CHANGELOG.md（公开，无需登录；启动后缓存，部署重启自然失效）。"""
        if _changelog_cache[0] is None:
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.join(base, "CHANGELOG.md")
            try:
                with open(path, encoding="utf-8") as f:
                    _changelog_cache[0] = f.read()
            except OSError:
                _changelog_cache[0] = "暂无更新日志"
        return jsonify({"ok": True, "text": _changelog_cache[0]})

    @app.route("/api/announcement", methods=["GET"])
    def api_announcement():
        # 公告缓存：首次读取 .env，保存公告时更新（write 接口同步 _announcement_cache）
        if _announcement_cache[0] is None:
            _announcement_cache[0] = read_env(ENV_FILE).get("YIBAN_ANNOUNCEMENT", "").strip()
        return jsonify({"ok": True, "text": _announcement_cache[0]})

    @app.route("/api/announcement", methods=["PUT"])
    def api_announcement_save():
        data = request.get_json(silent=True) or {}
        text = str(data.get("text", "")).strip()
        if len(text) > 500:  # 后端长度限制（前端 maxlength=200 可绕过，防 .env 膨胀 DoS）
            return jsonify({"error": "公告内容过长（最多 500 字）"}), 400
        write_env_key(ENV_FILE, "YIBAN_ANNOUNCEMENT", text)
        _announcement_cache[0] = text  # 同步内存缓存
        logger.info("公告已更新: %s", text[:50] or "（已清除）")
        return jsonify({"ok": True, "msg": "公告已更新" if text else "公告已清除"})

    # ---- 连通性检测 ----
    @app.route("/api/ping", methods=["POST"])
    def api_ping():
        ok, detail = check_connectivity()
        return jsonify({"ok": True, "reachable": ok, "detail": detail})

    # ---- 时钟与签到状态 ----
    @app.route("/api/clock")
    def api_clock():
        text, color = sign_status()
        return jsonify(
            {
                "ok": True,
                "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "server_ts": int(time.time()),  # 服务器 epoch 秒，供前端平滑走秒与校准
                "sign_status": text,
                "color": color,
            }
        )

    return app


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    global ACCOUNTS_FILE, LOG_FILE, ENV_FILE, USERS_FILE, STATE_DIR, DB_FILE
    parser = argparse.ArgumentParser(description="易班自动签到网页管理系统")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    # 非常见端口（默认 17892）：避开 8000/5000/3000 等常见端口，防止与其他部署冲突
    parser.add_argument("--port", type=int, default=17892, help="监听端口（默认 17892）")
    parser.add_argument(
        "--config", default=ACCOUNTS_DEFAULT, help=f"JSON 数据文件路径（迁移来源，默认: {ACCOUNTS_DEFAULT}）"
    )
    parser.add_argument("--log", default=LOG_DEFAULT, help=f"签到日志路径（默认: {LOG_DEFAULT}）")
    parser.add_argument("--env", default=ENV_DEFAULT, help=f".env 路径（默认: {ENV_DEFAULT}）")
    parser.add_argument(
        "--users", default=USERS_DEFAULT, help=f"用户表 JSON 路径（迁移来源，默认: {USERS_DEFAULT}）"
    )
    parser.add_argument(
        "--db", default=DB_DEFAULT, help=f"SQLite 数据库路径（默认: {DB_DEFAULT}）"
    )
    parser.add_argument("--debug", action="store_true", help="Flask 调试模式")
    args = parser.parse_args()
    ACCOUNTS_FILE = args.config
    LOG_FILE = args.log
    ENV_FILE = args.env
    USERS_FILE = args.users
    DB_FILE = args.db
    STATE_DIR = STATE_DIR_DEFAULT

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
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

    app = create_app()
    # 生产模式：debug 关闭（Werkzeug 单进程即可；如部署用 systemd 更稳）
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
