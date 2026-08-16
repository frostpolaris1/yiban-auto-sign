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
import calendar
import contextlib
import json
import logging
import os
import random
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

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
DB_DEFAULT = os.environ.get("YIBAN_DB_FILE", "yiban.db")
# 模块级路径（gunicorn 走 create_app() 不执行 main()，需在此初始化；main() 用 --config 等参数覆盖）
ACCOUNTS_FILE = ACCOUNTS_DEFAULT  # 仅作 JSON→SQLite 自动迁移来源（迁移后改名 .bak；users.json 同目录推断，无需单独路径）
LOG_FILE = LOG_DEFAULT
ENV_FILE = ENV_DEFAULT
STATE_DIR = STATE_DIR_DEFAULT
DB_FILE = DB_DEFAULT

# 普通用户账号的审核状态（2026-08-16 审查轮：原 STATUS_PENDING/ACTIVE/REJECTED 与签到状态码
# STATUS_* 同名异义（历史遗留），改名为 ACCOUNT_STATUS_* 彻底分离命名空间）
ACCOUNT_STATUS_PENDING = "pending"  # 待审核（不参与定时签到）
ACCOUNT_STATUS_ACTIVE = "active"  # 已生效（参与定时签到）
ACCOUNT_STATUS_REJECTED = "rejected"  # 已拒绝（附理由，用户可编辑重新提交）

# 软删除保留期（天）：管理员删除的账号进入待删除状态，超期自动彻底清除。
# 唯一来源在 db.py（SOFT_DELETE_RETENTION_DAYS），此处仅引用防双源漂移（2026-08-15 审查）
DELETED_RETENTION_DAYS = db.SOFT_DELETE_RETENTION_DAYS

# 密码策略：至少 10 位且包含大写/小写/数字/符号中至少两类（只对新建/修改生效，存量密码不受影响）
PASSWORD_MIN_LEN = 10
# 口令哈希算法（werkzeug scrypt，OWASP 推荐参数；check_password_hash 对旧哈希自动兼容）
SCRYPT_METHOD = "scrypt:65536:8:1"

# 账号编辑时识别码清空哨兵值（收到该值 = 显式删除设备识别码字段）
CLEAR_SENTINEL = "__clear__"

# 状态图标（与 tui/app.py 一致；前端渲染使用，后端仅用于日志解析）
SIGN_START = (6, 30)
SIGN_END = (7, 50)


def _sign_window():
    """签到窗口（调度 v2：支持 .env 覆盖 YIBAN_SIGN_START/END，非法回退默认）。"""
    start = SIGN_START
    end = SIGN_END
    env = read_env(ENV_FILE)
    for key in ("YIBAN_SIGN_START", "YIBAN_SIGN_END"):
        raw = env.get(key, "").strip()
        try:
            h, m = raw.split(":")
            parsed = (int(h), int(m))
            if 0 <= parsed[0] <= 23 and 0 <= parsed[1] <= 59:
                if key.endswith("START"):
                    start = parsed
                else:
                    end = parsed
        except (ValueError, AttributeError):
            pass
    if start >= end:
        start, end = SIGN_START, SIGN_END
    return start, end

# 登录时延拉平占位哈希：用户名/账号不存在时也执行一次等价 scrypt 比对，
# 消除「响应耗时差异」造成的用户枚举时序侧信道（占位哈希无需真实有效，比对恒为 False）。
_dummy_pw_hash = None


def _constant_time_dummy(password):
    """对不存在的账号执行一次与真实校验等价的 scrypt 比对（耗时拉平）。"""
    global _dummy_pw_hash
    if _dummy_pw_hash is None:
        _dummy_pw_hash = generate_password_hash("dummy-placeholder", method=SCRYPT_METHOD)
    check_password_hash(_dummy_pw_hash, password)


# 可信第一跳代理（nginx 反代）：仅当请求来自这些地址时才信任转发头。
# 生产部署：yiban-web 监听 127.0.0.1，nginx 反代并以 `proxy_set_header X-Forwarded-For $remote_addr`
# 覆盖设置（客户端伪造的 XFF 会被丢弃），故此处读取的 XFF 即真实客户端 IP。
TRUSTED_PROXIES = ("127.0.0.1", "::1")


def _json_body():
    """安全解析 JSON 请求体：非 dict（数组/数字/字符串/null）一律视为空对象，
    防 `.get()` AttributeError 导致 500（模糊测试：body=[1,2,3] / 42 → 500）。"""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _client_ip():
    """真实客户端 IP（限速/锁定/审计按真实 IP 隔离，防反代后全站共享同一桶）。

    - 反代场景：remote_addr 为代理地址且第一跳可信 → 取 X-Forwarded-For 首个值（nginx 已覆盖，不可伪造）；
    - 直连场景（无转发头/首跳不可信）：回退 remote_addr。
    注意：本函数假设应用不直接暴露公网（17892 仅监听回环 + 防火墙放行 22/443）。
    """
    r = request.remote_addr or "?"
    if r in TRUSTED_PROXIES:
        xff = request.headers.get("X-Forwarded-For", "")
        first = xff.split(",")[0].strip() if xff else ""
        if first and first != r:
            return first
    return r

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

# 容量上限（2026-08-15 对抗性审查补：注册/使用人数超负载兜底）：
# 注册用户上限默认 200（一人一号 ≈ 200 账号，远超班级/社团规模）；账号总数上限默认 500
# （调度窗口 80min ÷ 单账号平均 8s ≈ 600 理论上限，留裕量防 web 解密/轮询劣化）。
# 0 = 不限。可用 .env 的 YIBAN_MAX_USERS / YIBAN_MAX_ACCOUNTS 调整。
DEFAULT_MAX_USERS = 200
DEFAULT_MAX_ACCOUNTS = 500

# 自选时间片切换冷却（2026-08-15 用户反馈 → 弹性冷却）：
# 60 秒窗口内前 TIME_PREF_COOLDOWN_FREE 次切换完全自由（浏览式"全点一遍再定"属正常行为）；
# 超出后冷却递增：基础 × 2^(超限次数)，封顶 TIME_PREF_COOLDOWN_MAX（持续高频才被压制）。
# 高频切换本质是自我惩罚（updated_at 变晚 → 先到先得排后），冷却只为防连点/防刷屏噪音。
# 0 = 关闭。可用 .env 的 YIBAN_TIME_PREF_COOLDOWN_SEC 调整基础值（默认 30）。
TIME_PREF_COOLDOWN_SEC = 30
TIME_PREF_COOLDOWN_FREE = 20        # 60 秒窗口内自由切换次数（覆盖"全点一遍"16 片+选定）
TIME_PREF_COOLDOWN_MAX = 300        # 弹性封顶（秒）
TIME_PREF_COOLDOWN_WINDOW = 60      # 计数窗口（秒）

# 暂停签到冷却（2026-08-15 用户裁决）：暂停 30s 固定间隔（恢复不受限——恢复是紧迫正向
# 操作且无危害）。正常用户低频操作无感；防脚本刷审计/状态显示抖动。0=关闭。
PAUSE_COOLDOWN_SEC = 30

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

# 签到状态码（signin.py 写 sign-state 文件，为状态显示的事实源）与图标/文案映射
STATUS_SUCCESS = "success"
STATUS_ALREADY = "already"
STATUS_NO_TASK = "no_task"
STATUS_FAILED = "failed"
STATUS_RETRYING = "retrying"
STATUS_SKIPPED_WINDOW = "skipped_window"
STATUS_SKIPPED_NORANGE = "skipped_norange"
STATUS_PAUSED = "paused"  # 账密异常暂停（signin 熔断器）
STATUS_USER_CANCELLED = "user_cancelled"  # 用户自暂停签到（调度 v2）
STATUS_PENDING = "pending"  # 待签（未执行/无记录）；账号审核态已改名为 ACCOUNT_STATUS_PENDING（2026-08-16），命名空间已分离

STATUS_ICON = {
    STATUS_SUCCESS: "✅", STATUS_ALREADY: "✅", STATUS_NO_TASK: "➖",
    STATUS_FAILED: "❌", STATUS_RETRYING: "🔄",
    STATUS_SKIPPED_WINDOW: "⛔", STATUS_SKIPPED_NORANGE: "⛔",
    STATUS_PAUSED: "⏸️", STATUS_USER_CANCELLED: "⏹️", STATUS_PENDING: "⏳",
}
STATUS_TEXT = {
    STATUS_SUCCESS: "签到成功", STATUS_ALREADY: "已签到", STATUS_NO_TASK: "无需签到",
    STATUS_FAILED: "签到失败", STATUS_RETRYING: "重试中",
    STATUS_SKIPPED_WINDOW: "时段外", STATUS_SKIPPED_NORANGE: "窗口缺失",
    STATUS_PAUSED: "暂停", STATUS_USER_CANCELLED: "已取消", STATUS_PENDING: "待签",
}


def clear_fuse_pause(phone):
    """账号凭据变更（改密码/编辑）后清除熔断暂停记录，使其立即恢复签到。

    2026-08-15 命名审查：原名 clear_cred_state 误导（"cred"易被理解为清除凭据/密钥，
    实际只删 cred-state.json 里的熔断暂停条目）；现名体现真实行为。
    """
    try:
        path = os.path.join(STATE_DIR, "cred-state.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or phone not in data:
            return
        del data[phone]
        # 唯一临时名：防与 signin 收尾 _save_cred_state 跨进程并发碰撞（对抗性审查 F5）
        tmp = f"{path}.tmp{secrets.token_hex(4)}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except (OSError, ValueError):
        pass


def load_sign_state(date_str=None):
    """读取按日结构化状态文件：{phone: {status, message, time, task}}。

    缺失/损坏/目录不存在时回退读旧格式按日文件（sign-daily，符号 → 状态码）：
    覆盖部署过渡期（sign-state 尚未生成）与历史日期查看场景。
    两者都无 → 返回空 dict（前端回退显示待签 ⏳）。
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(STATE_DIR, f"sign-state-{date_str}.json")
    try:
        # utf-8-sig：兼容 Windows 记事本/手工编辑可能写入的 UTF-8 BOM（BOM 会让 json.load 抛错）
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict) and data:
            return data
    except (OSError, ValueError):
        pass
    # 回退：sign-daily（旧版符号 ✅/❌/➖）→ 状态码
    daily_path = os.path.join(STATE_DIR, f"sign-daily-{date_str}.json")
    try:
        with open(daily_path, encoding="utf-8-sig") as f:
            daily = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(daily, dict):
        return {}
    sym_map = {"✅": STATUS_SUCCESS, "❌": STATUS_FAILED, "➖": STATUS_NO_TASK}
    return {
        phone: {"status": sym_map.get(sym, STATUS_PENDING), "message": "", "task": "default"}
        for phone, sym in daily.items()
    }

logger = logging.getLogger("web")


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
    """解析签到日志：返回最近日志行列表（yiban 非 DEBUG 行）。

    2026-08-16 审查轮：原返回值 (states, recent) 的 states（日志符号 → 图标）从未被
    正确消费——账号状态的事实源是 sign-state 文件（load_sign_state，/api/accounts），
    日志符号与前端状态码语义不符，曾被 /api/logs 透传污染前端图标/统计卡（历史遗留）。
    现与 tui 同构：仅返回 recent 行。
    """
    recent = []
    for line in _tail_lines(path):
        m = SIGN_LOG_RE.match(line.strip())
        if not m:
            continue
        _date, level, logger_name, _msg = m.groups()
        if logger_name != "yiban" or level == "DEBUG":
            continue
        recent.append(line.strip())
    return recent


def _is_valid_date_str(s):
    """YYYY-MM-DD 格式且为真实日历日期（2026-13-99 这类非法值拒绝）。"""
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def log_path_for(date_str=None):
    """按天日志文件路径：{LOG_FILE 目录}/sign-YYYY-MM-DD.log（date_str 缺省=今天）。

    2026-08-16 日志按天分文件：每天一个文件，按日期查看 = 直接读对应文件；
    run.sh / signin.py / 手动签到子进程均写入当天文件（保留 LOG_FILE 配置的目录）。
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    return os.path.join(os.path.dirname(LOG_FILE), f"sign-{date_str}.log")


def _log_lines_for(date_str):
    """读取指定日期日志的行（行首日期过滤防跨天残留；仅 yiban 非 DEBUG 行）。

    文件缺失/不可读返回空列表（历史日期无日志是正常状态，不报错）。
    """
    prefix = f"[{date_str} "
    out = []
    for line in _tail_lines(log_path_for(date_str)):
        if not line.startswith(prefix):
            continue
        m = SIGN_LOG_RE.match(line.strip())
        if not m:
            continue
        _, level, logger_name, _msg = m.groups()
        if logger_name != "yiban" or level == "DEBUG":
            continue
        out.append(line.strip())
    return out


# ---------------------------------------------------------------------------
# .env 读写（与 tui/app.py 保持一致）
# ---------------------------------------------------------------------------
def read_env(env_path):
    """读取 .env 全部键值，返回 dict。

    utf-8-sig：兼容带 BOM 的 .env（Windows 记事本等工具保存时会带 BOM，
    否则首个键名会带上 \ufeff 前缀导致读不到，管理员登录/改密会静默失败）。
    """
    result = {}
    try:
        with open(env_path, encoding="utf-8-sig") as f:
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
    """把任意键值写入 .env：value 为空删除该行，否则写入；保留注释与其他行。

    写锁（_env_write_lock）：并发保存设置/公告时读-改-写互斥，防跨 worker 丢更新。
    """
    with _env_write_lock(env_path):
        lines = []
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8-sig") as f:  # utf-8-sig：兼容带 BOM 的 .env
                lines = f.read().splitlines()
        out = [ln for ln in lines if not ln.strip().startswith(f"{key}=")]
        if value:
            out.append(f"{key}={value}")
        _atomic_write(env_path, "\n".join(out) + "\n", chmod_priv=True)


def ensure_secret_key(env_path):
    """确保 .env 中存在 YIBAN_SECRET_KEY（缺失时自动生成随机值）。

    .env 不可写时降级为进程内随机密钥并告警（服务可用，重启后会话失效）——
    与 migrate_admin_password_to_hash 的降级策略一致（对抗性审查 F4）。
    """
    with _env_write_lock(env_path):
        env = read_env(env_path)
        key = env.get("YIBAN_SECRET_KEY", "").strip()
        if key:
            return key
        key = secrets.token_hex(32)
        lines = []
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8-sig") as f:  # utf-8-sig：兼容带 BOM 的 .env
                lines = f.read().splitlines()
        if not any(ln.strip().startswith("YIBAN_SECRET_KEY=") for ln in lines):
            lines.append(f"YIBAN_SECRET_KEY={key}")
        try:
            _atomic_write(env_path, "\n".join(lines) + "\n", chmod_priv=True)
        except OSError as e:
            logger.warning(
                "无法写入 %s（%s）：YIBAN_SECRET_KEY 仅本次进程生效（重启后会话将失效），"
                "请修复目录权限或手动配置密钥",
                env_path, e,
            )
            return key
        logger.info("已自动生成 YIBAN_SECRET_KEY 并写入 %s", env_path)
        return key


def migrate_admin_password_to_hash(env_path):
    """启动时安全迁移：检测到管理员口令以明文（YIBAN_ADMIN_PASSWORD）存储且无哈希时，
    自动生成 scrypt 哈希写入 YIBAN_ADMIN_PASSWORD_HASH 并清空明文。

    说明：仅改变口令的存储形态（明文 → 哈希），口令本身不变；已有哈希则跳过；
    明文回退比对路径（verify_admin）保留以兼容未迁移的存量部署。
    迁移失败（如 .env 对进程不可写）只告警不阻断启动——明文回退仍可登录。
    """
    try:
        env = read_env(env_path)
        if env.get("YIBAN_ADMIN_PASSWORD_HASH", "").strip():
            return
        plain = env.get("YIBAN_ADMIN_PASSWORD", "").strip()
        if not plain:
            return
        write_env_key(
            env_path,
            "YIBAN_ADMIN_PASSWORD_HASH",
            generate_password_hash(plain, method=SCRYPT_METHOD),
        )
        write_env_key(env_path, "YIBAN_ADMIN_PASSWORD", "")
    except OSError as e:
        logger.warning(
            "管理员口令明文迁移失败（%s 不可写？）：%s；将暂时回退明文比对，"
            "请修复权限后重启或手动改密",
            env_path,
            e,
        )
        return
    logger.warning(
        "检测到管理员口令明文存储（%s），已自动迁移为 scrypt 哈希并清空明文；"
        "口令本身未变更，请确认其强度足够（弱口令仍可被猜测）",
        env_path,
    )


def _atomic_write(path, content, chmod_priv=False):
    """原子写文件：先写临时文件再替换，避免半写状态（cron 并发读取安全）。

    chmod_priv=True 时写完后收紧为 0600（含密钥/口令的 .env 场景），
    防止默认 umask 下产生同主机其他用户可读的宽松权限。
    """
    tmp = f"{path}.tmp{secrets.token_hex(4)}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())  # 落盘再替换：极端掉电场景不丢数据
    os.replace(tmp, path)
    if chmod_priv:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)  # 仅属主可读写（Windows 无实际效果，忽略失败）


# ---------------------------------------------------------------------------
# 数据读写（SQLite：db 层单行事务 + WAL，天然原子，无需 TTL 缓存）
# RLock：进程内"读→检查→写"操作级序列互斥（防呆判定与写入之间不被同进程请求交错；
# 跨进程一致性由 SQLite 事务与 UNIQUE 约束保证）
# ---------------------------------------------------------------------------
_file_lock = threading.RLock()


@contextlib.contextmanager
def _env_write_lock(env_path):
    """.env 写互斥：进程内 RLock + 跨进程 flock（POSIX 可用时）。

    修复（对抗性审查 2026-08-15 实证）：并发保存设置/公告时 read-modify-write
    丢更新（60/60 轮必丢一个键）——gunicorn 多 worker 跨进程写 .env 需文件锁。
    Windows 无 fcntl 退化为进程内锁（本地开发单进程足够）。
    """
    with _file_lock:
        try:
            import fcntl
        except ImportError:
            yield
            return
        fd = open(env_path + ".lock", "a+")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()

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


def _mask_log_phones(line):
    """日志行内全部 [11 位手机号] 脱敏（/api/logs 与 /api/my-logs 共用，防展示层漏出 PII）。

    覆盖 signin.py 的行格式 `[13800138000] 结果`；其他格式（如 `账号: 138...`）
    不进日志（通知内容不落盘），单一格式正则足够——见日志审查 P3。
    """
    return re.sub(r"\[(\d{11})\]", lambda m: "[" + _mask_phone(m.group(1)) + "]", line)


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


def _owner_display_of(owner_email):
    """把账号归属邮箱映射为展示名（后台归属列用）：普通用户显示邮箱前缀（@ 前）。"""
    if owner_email in ("admin", ""):
        return "管理员"
    return owner_email.split("@")[0] if "@" in owner_email else owner_email


def _slot_to_label(slot_min):
    """自选片窗口内分钟数 → "HH:MM"（调度 v2，与 signin 的 slot 口径一致：06:30 → 390）。"""
    if slot_min is None:
        return None
    sw = _sign_window()
    base = sw[0][0] * 60 + sw[0][1]
    m = base + int(slot_min)
    return f"{m // 60:02d}:{m % 60:02d}"


def _estimate_slot(phone):
    """预计签到时段（调度 v2 2.1，docs/design/plan-scheduler-v2.md）：
    顺序排序 = 可预期（线性填块区间 / 锚点中心 / 小人数确定性等分）；
    随机排序 = 每天重排，返回 None + 提示文案。
    返回 (estimated_str|None, note_str)。
    """
    env = read_env(ENV_FILE)
    mode = env.get("YIBAN_SIGN_MODE", "").strip().lower()
    order = env.get("YIBAN_SIGN_ORDER", "").strip().lower() or (
        "random" if mode == "random" else "sequence")
    dist = env.get("YIBAN_SIGN_DIST", "").strip().lower() or (
        "normal" if mode == "normal" else "uniform")
    if order != "sequence":
        return None, "随机模式每日重排，签到时间当天 06:31 后可见"
    accounts = load_accounts()
    # 与 build_schedule 一致：user_paused 账号不参与调度（零占位），预计时段按实际参与人计算
    live = [a for a in accounts if not a.get("user_paused")]
    idx = next((i for i, a in enumerate(live) if a.get("phone") == phone), None)
    if idx is None or not live:
        return None, ""
    sw = _sign_window()
    edge = load_env_int(ENV_FILE, "YIBAN_WINDOW_EDGE_SEC", 60) // 60
    start_min = sw[0][0] * 60 + sw[0][1]
    end_min = sw[1][0] * 60 + sw[1][1]
    eff_lo = start_min + edge
    eff_hi = end_min - edge
    span = eff_hi - eff_lo

    def fmt(m):
        m = int(m)
        return f"{m // 60:02d}:{m % 60:02d}"

    if dist == "uniform":
        # 线性填块（与 signin._schedule_blocks 同口径：块从窗口起点步进 5、裁到有效窗口、
        # 被缓冲吃掉的无效块跳过；压缩模式等极端场景按末块估算）
        k = load_env_int(ENV_FILE, "YIBAN_BLOCK_CAP", 15)
        valid = []
        b = start_min
        while b < end_min:
            lo = max(b, eff_lo)
            hi = min(b + 5, eff_hi)
            if hi > lo:
                valid.append((lo, hi))
            b += 5
        if not valid:
            return None, ""
        bi = min(idx // k, len(valid) - 1)
        lo, hi = valid[bi]
        return f"{fmt(lo)}~{fmt(hi)}", "（每日固定时段，块内时刻每天略有抖动）"
    # 顺序 × 正态：锚点 z 固定 → 预期中心（μ 中值 50%、σ 中值 20%）
    z = random.Random(str(phone)).gauss(0, 1)
    center = max(eff_lo, min(eff_hi, eff_lo + span * 0.5 + span * 0.20 * z))
    return f"约 {fmt(center)}", "（每日波动约 ±10 分钟）"


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
        "status": acc.get("status", ACCOUNT_STATUS_ACTIVE),
        "reject_reason": acc.get("reject_reason", ""),
        "user_paused": bool(acc.get("user_paused", False)),  # 用户自暂停（调度 v2）
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
        _constant_time_dummy(password)  # 时延拉平：防用户名枚举（与真实比对等开销）
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
    if now.weekday() == 6 and not load_env_int(ENV_FILE, "YIBAN_SUNDAY_SIGN", 0):
        # 周日：仅当「周日签到」开启时走正常窗口逻辑，否则提示无需打卡
        return "🌙 今日无需打卡（周日）", "#565f89"
    sw = _sign_window()  # 单次读取（每次调用都会重读 .env，避免重复解析）
    start_h, start_m = sw[0]
    end_h, end_m = sw[1]
    start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if now < start:
        return f"⏳ 未到签到时间（{start_h:02d}:{start_m:02d} 开始）", "#7aa2f7"
    if now <= end:
        return f"🔔 签到窗口（~{end_h:02d}:{end_m:02d} 结束）", "#9ece6a"
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


# 容量告警去重（进程内）：首次触顶通知一次，之后静默拒绝（防通知风暴；重启后重置）
_capacity_alerts = {"users": False, "accounts": False}


def _notify_capacity_once(kind, limit, label):
    """容量触顶通知（每进程每种资源只发一次）：管理员知情且不刷屏。"""
    if _capacity_alerts.get(kind):
        return
    _capacity_alerts[kind] = True
    logger.warning("%s已达上限 %d，已拒绝新注册/添加", label, limit)
    send_notification(
        f"{label}已达上限",
        f"{label}已达上限（{limit}），新的注册/添加已被拒绝。\n"
        f"如需扩容请在 .env 调整 YIBAN_MAX_USERS / YIBAN_MAX_ACCOUNTS。",
    )


# ---------------------------------------------------------------------------
# Flask 应用
# ---------------------------------------------------------------------------
# 应用版本号（页面底部显示；每次修改按语义递增：修复 +0.0.1 / 功能 +0.1.0 / 大版本 +1.0.0）
# 2026-08-16 运维体系收尾：备份含日志/状态清理/设置审计/耗时记录/缓存优化（0.19.7）
APP_VERSION = "0.19.8"
# 页面失效版本：每次启动变化，供前端"版本失效自动刷新"兜底（防止缓存旧页面）
WEB_VERSION = datetime.now().strftime("%Y%m%d%H%M%S")


def create_app():
    # 启动安全迁移：管理员口令明文 → scrypt 哈希（幂等，多 worker 并发写同口令哈希无害）
    migrate_admin_password_to_hash(ENV_FILE)
    # SQLite 数据层初始化：首次启动自动迁移 accounts.json/users.json → yiban.db（幂等，
    # JSON 改名 .bak 保留逃生门）；多 worker 各自调用幂等（模块级连接缓存）
    db.init_db(DB_FILE, migrate_from=ACCOUNTS_FILE, env_file=ENV_FILE)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = ensure_secret_key(ENV_FILE)
    app.config["SESSION_COOKIE_NAME"] = "yiban_admin"
    app.config["SESSION_COOKIE_HTTPONLY"] = True  # JS 不可读 session cookie（防 XSS 窃取）
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # 跨站请求不携带 cookie（防 CSRF）
    # Secure 标志由部署层保证：nginx 反代配置 `proxy_cookie_flags yiban_admin secure`（生产 HTTPS）；
    # 不在应用层强制，避免本机 HTTP 直连演示（localhost）登录失效
    app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 14  # 14 天（折中：安全与管理员便利平衡）
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
        ip = _client_ip()
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
                    _client_ip(),
                    request.path,
                    request.headers.get("Origin"),
                )
                return jsonify({"error": "请求来源异常，请刷新页面后重试"}), 403
            return
        token = request.headers.get("X-CSRF-Token", "")
        sess_token = session.get("csrf_token", "")
        if not token or not secrets.compare_digest(token, sess_token):
            logger.warning(
                "CSRF 校验失败: ip=%s path=%s token_len=%d session_token_len=%d",
                _client_ip(),
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
            ip = _client_ip()
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
        elif request.path.startswith("/static/") and resp.status_code < 400:
            # 静态资源长缓存 30 天（版本变化由 ?v= 兜底）；404 等错误响应不缓存（防浏览器缓存 404）
            resp.headers["Cache-Control"] = "public, max-age=2592000"
        return resp

    # ---- 数据层错误保护：SQLite 读写/密文解密失败 → 明确 500（防静默降级或返回错误数据）----
    @app.errorhandler(RuntimeError)
    def _handle_data_error(e):
        logger.error("数据层错误: %s", e)  # 详细信息只入日志，不回显客户端（防内部路径/字段泄露）
        return jsonify({"error": "服务器内部错误，请稍后重试或联系管理员"}), 500

    # ---- 认证 API ----
    @app.route("/api/login", methods=["POST"])
    def api_login():
        """登录：管理员（.env 配置）或普通用户（users.json 注册）。返回 role。"""
        ip = _client_ip()
        now = time.time()
        data = _json_body()
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
            # 不显示剩余秒数：避免向用户暴露锁定窗口参数（信息分层，2026-08-15）
            return jsonify({"error": "密码错误次数过多，请稍后再试"}), 429
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
            if u is None:
                _constant_time_dummy(password)  # 时延拉平：防邮箱枚举（与真实比对等开销）
            elif check_password_hash(u.get("password_hash", ""), password):
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
        data = _json_body()
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
        ip = _client_ip()
        now = time.time()
        _ip_store_trim(_register_limits, REGISTER_WINDOW)
        rcnt, rstart = _register_limits.get(ip, (0, now))
        if now - rstart > REGISTER_WINDOW:
            rcnt, rstart = 0, now
        if rcnt >= REGISTER_MAX:
            # 不暴露限速窗口分钟数（防恶意用户据此规划批量注册节奏，信息分层 2026-08-15）
            return jsonify({"error": "注册过于频繁，请稍后再试"}), 429
        # 操作级锁：邮箱唯一性检查与写入原子（UNIQUE 约束兜底并发注册）
        with _file_lock:
            # 容量兜底：注册总人数上限（防分布式注册无限膨胀 users 表，对抗性审查补）
            max_users = load_env_int(ENV_FILE, "YIBAN_MAX_USERS", DEFAULT_MAX_USERS)
            if max_users > 0 and len(db.load_users()) >= max_users:
                _notify_capacity_once("users", max_users, "注册人数")
                return jsonify({"error": "注册人数已达上限，请联系管理员"}), 403
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
        旧会话失效由 pw_version 递增实现（_effective_role 实时校验）。
        """
        data = _json_body()
        old_password = str(data.get("old_password", ""))
        new_password = str(data.get("new_password", ""))
        if new_password != str(data.get("confirm_password", "")):
            return jsonify({"error": "两次输入的新密码不一致"}), 400
        pw_err = _password_policy_error(new_password)
        if pw_err:
            return jsonify({"error": f"新密码不符合要求：{pw_err}"}), 400
        username = session.get("username", "")
        ip = _client_ip()
        now = time.time()
        # 失败计数键与登录一致：按 (IP, 用户名) 组合
        fail_key = (ip, username.strip().lower())
        fails, lock_until, _ = _login_fails.get(fail_key, (0, 0, 0))
        if now < lock_until:
            # 不显示剩余秒数（信息分层，2026-08-15）
            return jsonify({"error": "尝试次数过多，请稍后再试"}), 429

        def _handle_failed_login():
            """当前密码校验失败：递增失败计数，达阈值锁定（与 api_login 一致）。

            2026-08-15 命名审查：原名 _pw_failed 读作"记录失败"，实际返回 429/400 响应。
            """
            nfails = fails + 1
            if nfails >= LOGIN_MAX_FAILS:
                _login_fails[fail_key] = (0, now + LOGIN_LOCK_SECONDS, now)
                logger.warning("改密失败次数过多，IP %s 锁定 %s 秒", ip, LOGIN_LOCK_SECONDS)
                # 不暴露锁定时长分钟数（信息分层，2026-08-15）
                return jsonify({"error": "密码错误次数过多，请稍后再试"}), 429
            if nfails == LOGIN_FAIL_NOTIFY:
                send_notification(
                    "改密失败告警",
                    f"IP {ip} 连续 {nfails} 次修改密码失败（用户名: {username}）\n"
                    f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"如非本人操作，请检查是否有人尝试暴力破解",
                )
            _login_fails[fail_key] = (nfails, 0, now)
            return jsonify({"error": "当前密码不正确"}), 400

        # 内置管理员：验证 .env 当前口令后更新
        if username.strip().lower() == _builtin_admin_email():
            if not verify_admin(username, old_password):
                return _handle_failed_login()
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
            logger.info("内置管理员密码已更新")
            return jsonify({"ok": True, "msg": "密码已更新，下次登录使用新密码"})
        # 注册用户（含提升的管理员）：db 单行更新（事务内，防并发覆盖）
        with _file_lock:
            u = db.find_user(username.strip().lower())
            if u is not None:
                if not check_password_hash(u.get("password_hash", ""), old_password):
                    return _handle_failed_login()
                db.update_user(
                    u["email"],
                    {
                        "password_hash": generate_password_hash(new_password, method=SCRYPT_METHOD),
                        "pw_version": u.get("pw_version", 1) + 1,  # 旧会话随之失效
                    },
                )
                db.audit(username, "user_password", username, "自助改密")
                _login_fails.pop(fail_key, None)
                logger.info("用户 %s 已修改自己的密码", _mask_email(username))
                return jsonify({"ok": True, "msg": "密码已更新，下次登录使用新密码"})
        return jsonify({"error": "用户不存在"}), 404

    @app.route("/api/me")
    def api_me():
        # admin 字段为旧版前端兼容（早期前端检查 me.admin；新版用 role）——
        # 防止浏览器缓存旧页面时误判未登录导致刷新循环
        role = _current_role()
        username = session.get("username") or ""
        # 调度 v2：排序×分布模式与自选开关同步给用户（只读展示）
        env = read_env(ENV_FILE)
        mode = env.get("YIBAN_SIGN_MODE", "").strip().lower()
        sign_order = env.get("YIBAN_SIGN_ORDER", "").strip().lower() or (
            "random" if mode == "random" else "sequence"
        )
        sign_dist = env.get("YIBAN_SIGN_DIST", "").strip().lower() or (
            "normal" if mode == "normal" else "uniform"
        )
        sw = _sign_window()
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
                # 调度 v2（docs/design/plan-scheduler-v2.md 2.1/2.2）
                "sign_order": sign_order,
                "sign_dist": sign_dist,
                "time_pref_allowed": load_env_int(ENV_FILE, "YIBAN_ALLOW_TIME_PREF", 0) == 1,
                "sign_window": f"{sw[0][0]:02d}:{sw[0][1]:02d} ~ {sw[1][0]:02d}:{sw[1][1]:02d}",
            }
        )

    # ---- 账号管理 ----
    @app.route("/api/accounts")
    def api_accounts():
        accounts = load_accounts()
        # 附带今日签到状态（键脱敏与 /api/logs 一致）：前端账号表格状态图标不再依赖
        # 单独的日志轮询（logs/accounts tab 各自可见时才请求对应接口，减少无效轮询）
        # 状态来源：signin.py 写的结构化状态文件（status 码），前端做图标映射
        states = load_sign_state()
        # 用户自暂停账号：状态直接呈现"已取消"（⏹️）——无需等下次签到执行写状态文件，
        # 管理员面板立即反映（2026-08-15 修复：此前仅 sign-state 有该状态时才会显示）
        for acc in accounts:
            if acc.get("user_paused"):
                states[acc.get("phone", "")] = {
                    "status": STATUS_USER_CANCELLED,
                    "message": "用户已取消签到",
                }
        # 调度 v2：自选时间（管理员查看每个用户选的片；slot_min → "HH:MM" + 首尾标记）
        prefs = {p: v["slot_min"] for p, v in db.get_time_prefs().items()}
        sw = _sign_window()
        _span_min = (sw[1][0] * 60 + sw[1][1]) - (sw[0][0] * 60 + sw[0][1])

        def _edge_mark(slot):
            if slot is None:
                return None
            if slot == 0:
                return "first"
            if slot >= _span_min - 5:
                return "last"
            return None

        return jsonify(
            {
                "ok": True,
                "accounts": [
                    {
                        **mask_account(a, i),
                        "time_pref": _slot_to_label(prefs.get(a["phone"])),
                        "time_pref_edge": _edge_mark(prefs.get(a["phone"])),
                    }
                    for i, a in enumerate(accounts)
                ],
                # states 值压成状态码字符串（前端图标映射用）
                "states": {
                    _mask_phone(k): (v.get("status", STATUS_PENDING) if isinstance(v, dict) else STATUS_PENDING)
                    for k, v in states.items()
                },
                # 状态原因/计划（如"计划 06:42"），前端表格 title 展示
                "state_msgs": {
                    _mask_phone(k): (v.get("message", "") if isinstance(v, dict) else "")
                    for k, v in states.items()
                },
                # 单次签到耗时秒数（P6）：表格状态 title 展示"耗时 xx s"；无记录为 None
                "state_durs": {
                    _mask_phone(k): (v.get("dur") if isinstance(v, dict) else None)
                    for k, v in states.items()
                },
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
        data = _json_body()
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
            # 容量兜底：账号总数上限（防账号无限增长拖垮解密/轮询性能，对抗性审查补）
            max_accounts = load_env_int(ENV_FILE, "YIBAN_MAX_ACCOUNTS", DEFAULT_MAX_ACCOUNTS)
            if max_accounts > 0 and len(accounts) >= max_accounts:
                _notify_capacity_once("accounts", max_accounts, "账号数量")
                return jsonify({"error": f"账号数量已达上限（{max_accounts}），请联系管理员扩容"}), 403
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
                clean["status"] = ACCOUNT_STATUS_PENDING
            else:
                clean["owner"] = "admin"
                clean["status"] = ACCOUNT_STATUS_ACTIVE
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
            data = _json_body()
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
            # 手机号变更 → 旧号自选时间片失效（防孤儿 pref 占容量，对抗性审查补）
            if clean["phone"] != old.get("phone"):
                db.clear_time_pref(old.get("phone", ""))
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
            clean["status"] = old.get("status", ACCOUNT_STATUS_ACTIVE)
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
            # 凭据变更（改密码/识别码）后清除熔断暂停，立即恢复签到
            clear_fuse_pause(clean["phone"])
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
            data = _json_body()
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
                            ACCOUNT_STATUS_PENDING,
                            ACCOUNT_STATUS_REJECTED,
                        ):
                            db.update_account_status(acc["id"], ACCOUNT_STATUS_ACTIVE, reject_reason="")
                            done += 1
                    elif action == "reject":
                        if acc.get("status") in (ACCOUNT_STATUS_PENDING, ACCOUNT_STATUS_REJECTED):
                            db.update_account_status(acc["id"], ACCOUNT_STATUS_REJECTED, reason)
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
                    {"ok": True, "msg": f"批量操作中断，已处理 {done} 个（详情见服务器日志）"}
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
            action = (_json_body()).get("action")
            acc = accounts[idx]
            if action == "approve":
                # 软删除账号不可被审核通过（deleted 账号不参与审核流转）
                if acc.get("deleted") or acc.get("status") not in (ACCOUNT_STATUS_PENDING, ACCOUNT_STATUS_REJECTED):
                    return jsonify({"error": "该账号无需审核"}), 400
                db.update_account_status(acc["id"], ACCOUNT_STATUS_ACTIVE, reject_reason="")
                db.audit(
                    session.get("username") or "?",
                    "account_review",
                    _mask_phone(acc.get("phone", "")),
                    "approve",
                )
                logger.info("审核通过账号 %s（提交者 %s）", _mask_phone(acc.get("phone", "")), _mask_email(acc.get("owner", "")))
                # 回显脱敏（与列表口径一致，防响应混入完整 PII；管理员详情页可取完整号）
                return jsonify({"ok": True, "msg": f"已通过 {_mask_phone(acc.get('phone', ''))}，将参与定时签到"})
            if action == "reject":
                if acc.get("status") not in (ACCOUNT_STATUS_PENDING, ACCOUNT_STATUS_REJECTED):
                    return jsonify({"error": "该账号无需拒绝"}), 400
                # 理由清洗：换行/控制字符 → 空格（防日志注入伪造日志行）
                reason = (
                    str((_json_body()).get("reason", ""))
                    .strip()[:100]
                    .replace("\r", " ")
                    .replace("\n", " ")
                )
                db.update_account_status(acc["id"], ACCOUNT_STATUS_REJECTED, reason)
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
                direction = int((_json_body()).get("dir", 0))
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
        """用户视图：账号脱敏 + 今日状态（结构化状态文件）+ 审核状态 + 最近相关日志 + 排队信息。

        排队说明：签到按 accounts.json 顺序执行（队列重试模式）；
        queue_ahead = 自己账号之前、今日尚未了结（未 success/already/no_task）的已生效账号数。
        """
        recent = parse_sign_log(log_path_for())  # 最近日志仅用于「最近签到记录」展示（按天文件 = 今天）
        states = load_sign_state()  # 今日状态事实源（signin.py 写入）
        # 参与排队队列的账号：已生效（active，pending 不参与签到）且未软删除、未自暂停
        active = [
            a for a in accounts
            if a.get("status") == ACCOUNT_STATUS_ACTIVE and not a.get("deleted")
            and not a.get("user_paused", False)
        ]
        # 执行顺序（调度 v2，2026-08-15 改进）：优先按今日计划时间（sign-state scheduled 字段，
        # cron 生成后即真实执行顺序——覆盖自选/正态/随机模式）；计划未生成（06:31 前）回退列表顺序。
        # scheduled 为 "HH:MM:SS" 字符串，字典序即时间序；无计划者排在有计划者之后（列表序兜底）。
        def _exec_order_key(a):
            st = states.get(a.get("phone", ""), {})
            sched = st.get("scheduled", "") if isinstance(st, dict) else ""
            return (0 if sched else 1, sched, a.get("sort_order", 0))

        active_sorted = sorted(active, key=_exec_order_key)
        # 排队位置预计算（单次遍历累计，替代每个账号 O(pos) 切片求和）
        queue_before = {}
        running = 0
        for a in active_sorted:
            queue_before[a.get("phone", "")] = running
            st_status = states.get(a.get("phone", ""), {}).get("status", STATUS_PENDING)
            if st_status not in (STATUS_SUCCESS, STATUS_ALREADY, STATUS_NO_TASK):
                running += 1
        # 今日前缀：账号卡片「最近签到记录」只显示今天的日志（日志文件跨多天时避免混入历史）
        today_prefix = f"[{datetime.now().strftime('%Y-%m-%d')} "
        result = []
        for i, real_idx in enumerate(indices):
            acc = accounts[real_idx]
            phone = acc.get("phone", "")
            my_logs = [
                line for line in recent
                if line.startswith(today_prefix) and f"[{phone}]" in line
            ]
            # 排队：按今日计划时间排序的队列中，自己之前未了结的账号数（含自暂停排除）
            queue_ahead = 0
            if acc.get("status") == ACCOUNT_STATUS_ACTIVE and not acc.get("user_paused", False):
                queue_ahead = queue_before.get(phone, 0)
            st = states.get(phone, {})
            st_status = st.get("status", STATUS_PENDING) if isinstance(st, dict) else STATUS_PENDING
            result.append(
                {
                    "index": i,
                    "name": acc.get("name", ""),
                    "display_name": acc.get("name") or f"账号{i + 1}",
                    "phone": phone,
                    "phone_model": acc.get("phone_model", ""),
                    "status": acc.get("status", ACCOUNT_STATUS_ACTIVE),
                    "reject_reason": acc.get("reject_reason", ""),
                    "state_icon": STATUS_ICON.get(st_status, "⏳"),
                    "state_status": st_status,  # 状态码（前端按码映射文案）
                    "state_message": st.get("message", "") if isinstance(st, dict) else "",
                    "queue_ahead": queue_ahead,
                    "logs": my_logs[-5:],
                    "deleted": bool(acc.get("deleted")),
                    "deleted_at": acc.get("deleted_at", ""),
                    "user_paused": bool(acc.get("user_paused", False)),  # 用户自暂停（调度 v2）
                    # 2026-08-15 用户确认：管理员账号（owner=admin）不支持自暂停——
                    # 暂停是普通用户管理自己账号的能力；该字段仅管理员视图可见（前端据此隐藏按钮）
                    "pause_forbidden": acc.get("owner", "admin") == "admin" and not acc.get("deleted"),
                }
            )
        return result

    @app.route("/api/my-accounts")
    def api_my_accounts():
        accounts = load_accounts()
        indices = _my_account_indices()
        return jsonify({"ok": True, "accounts": _my_account_view(accounts, indices)})

    # ---- 用户自选时间片（调度 v2，docs/design/plan-scheduler-v2.md 2.2）----
    def _my_phone():
        """当前用户的自选绑定账号（2026-08-15 修复：与「我的账号」视图同口径）。

        普通用户=本人账号；内置管理员=归属 admin/本人邮箱的账号；注册管理员=归属本人邮箱的账号。
        ——此前 admin 分支硬编码 owner='admin'，导致注册管理员也绑定到内置管理员的账号，
        选片显示/保存互相覆盖（用户实测报告）。
        仅 status=active（正式进入签到列表）才算——pending/rejected 的"注册但未生效"
        用户不可查看/选择时间片（GET 返回 has_account=False → 前端整卡隐藏；PUT 400 兜底）。
        """
        accounts = load_accounts()
        for idx in _my_account_indices_of(accounts):
            acc = accounts[idx]
            if not acc.get("deleted") and acc.get("status") == ACCOUNT_STATUS_ACTIVE:
                return acc.get("phone", "")
        return None

    def _pref_slots(sw):
        """窗口内 16 个 5 分钟片（时钟对齐）：[{slot_min, label}]。"""
        start_min = sw[0][0] * 60 + sw[0][1]
        end_min = sw[1][0] * 60 + sw[1][1]
        edge = load_env_int(ENV_FILE, "YIBAN_WINDOW_EDGE_SEC", 60) // 60
        slots = []
        for b in range(start_min, end_min, 5):
            lo = max(b, start_min + edge)
            hi = min(b + 5, end_min - edge)
            if hi > lo:
                m = b - start_min
                slots.append({"slot_min": m, "label": f"{b // 60:02d}:{b % 60:02d}"})
        return slots

    @app.route("/api/my-time-pref")
    def api_my_time_pref():
        """我的自选 + 拥挤度 + 预计签到时段（选片卡片数据；总开关关时仍可预配置，调度侧不激活）。

        拥挤度防调研（2026-08-15 用户决策）：普通用户端只下发「已选百分比」（整数，四舍五入），
        不下发真实人数/块容量——不知道 K 无法反推人数；管理端 stats 接口保留精确计数。
        """
        sw = _sign_window()
        phone = _my_phone()
        pref = db.get_time_pref(phone) if phone else None
        stats = {s["slot_min"]: s["count"] for s in db.time_pref_stats()}
        cap = load_env_int(ENV_FILE, "YIBAN_BLOCK_CAP", 15)
        slots = []
        for s in _pref_slots(sw):
            count = stats.get(s["slot_min"], 0)
            # 粗粒度 10% 档（对抗性审查补）：精确百分比 + 已知默认 K 可反推人数；
            # 未满封顶 90、满员恰好 100——前端 pct>=100 判满精确（19/20=95% 不会再被
            # 四舍五入成 100 误报"已选满"，与后端 count>=cap 口径一致）
            if cap > 0:
                pct = 100 if count >= cap else min(90, round(count * 100 / cap / 10) * 10)
            else:
                pct = 0
            slots.append({"slot_min": s["slot_min"], "label": s["label"], "pct": pct})
        estimated, estimate_note = _estimate_slot(phone) if phone else (None, "")
        return jsonify({
            "ok": True,
            "pref": _slot_to_label(pref["slot_min"]) if pref else None,
            "pref_slot": pref["slot_min"] if pref else None,
            "slots": slots,
            "allowed": load_env_int(ENV_FILE, "YIBAN_ALLOW_TIME_PREF", 0) == 1,
            "window": f"{sw[0][0]:02d}:{sw[0][1]:02d} ~ {sw[1][0]:02d}:{sw[1][1]:02d}",
            "edge_sec": load_env_int(ENV_FILE, "YIBAN_WINDOW_EDGE_SEC", 60),
            "has_account": bool(phone),
            "estimated": estimated,        # 预计签到时段（顺序排序可预期；随机为 None）
            "estimate_note": estimate_note,
        })

    @app.route("/api/my-time-pref", methods=["PUT"])
    def api_my_time_pref_save():
        """保存/清除自选：{slot_min: int|null}。校验 5 对齐、窗口内；生效按分界时刻提示。"""
        # F3 对抗性审查（TOCTOU）：read-check-write 整段持 _file_lock 原子化——
        # 并发 purge 删号时不再重新插入孤儿 pref（已删 phone 残留→重占号被新账号继承泄漏）；
        # 冷却检查与写入原子化（防并发双请求绕过冷却）；满员统计与写入原子化（防超容写入）
        with _file_lock:
            phone = _my_phone()
            if not phone:
                # 2026-08-15 用户反馈：非正式用户不可选时间片——区分"未提交"与"已提交未生效"，
                # 提示不给待审核用户误导（信息分层，不暴露审核细节）
                has_submitted = any(
                    not a.get("deleted")
                    for a in (load_accounts()[i] for i in _my_account_indices())
                ) if _current_role() != "admin" else any(
                    a.get("owner", "admin") == "admin" and not a.get("deleted")
                    for a in load_accounts()
                )
                if has_submitted:
                    return jsonify({"error": "账号审核通过后即可选择签到时间"}), 400
                return jsonify({"error": "请先提交易班账号"}), 400
            data = _json_body()
            slot = data.get("slot_min")
            if slot is None:
                db.clear_time_pref(phone)
                db.audit(session.get("username", "?"), "time_pref_clear", phone, "")
                return jsonify({"ok": True, "msg": "已清除自选，恢复自动分配"})
            # M1 对抗性审查：严格类型校验——bool（False→0）与小数（5.9→5）截断不得误入合法槽位
            if isinstance(slot, bool) or (isinstance(slot, float) and not slot.is_integer()):
                return jsonify({"error": "时间片取值无效"}), 400
            try:
                slot = int(slot)
            except (TypeError, ValueError):
                return jsonify({"error": "时间片取值无效"}), 400
            sw = _sign_window()
            span = (sw[1][0] * 60 + sw[1][1]) - (sw[0][0] * 60 + sw[0][1])
            edge = load_env_int(ENV_FILE, "YIBAN_WINDOW_EDGE_SEC", 60) // 60
            if slot % 5 != 0 or not (0 <= slot < span - 2 * edge):
                # 不暴露"5 分钟对齐"等调度机制细节（信息分层，2026-08-15）
                return jsonify({"error": "所选时间片不在可选范围内，请重新选择"}), 400
            # 弹性切换冷却（2026-08-15 用户反馈）：60s 窗口内自由次数内完全放行（浏览式
            # "全点一遍再定"正常）；超出后冷却随超限次数递增（30s→60s→120s→…封顶 300s），
            # 持续高频才被压制。按被选账号计价（H3 多管理员共享全局生效；H4 新号豁免）。
            # 时长可配（YIBAN_TIME_PREF_COOLDOWN_SEC 基础值，默认 30；0=关闭）
            base_cd = load_env_int(ENV_FILE, "YIBAN_TIME_PREF_COOLDOWN_SEC", TIME_PREF_COOLDOWN_SEC)
            if base_cd > 0:
                now_ts = datetime.now()
                since = (now_ts - timedelta(seconds=TIME_PREF_COOLDOWN_WINDOW)
                         ).strftime("%Y-%m-%d %H:%M:%S")
                count = db.time_pref_set_count_since(phone, since)
                if count >= TIME_PREF_COOLDOWN_FREE:
                    # 弹性冷却 = 基础 × 2^(超限次数)，封顶
                    cooldown = min(base_cd * (2 ** (count - TIME_PREF_COOLDOWN_FREE + 1)),
                                   TIME_PREF_COOLDOWN_MAX)
                    last_ts = db.last_time_pref_set_at(phone)
                    if last_ts:
                        try:
                            last_dt = datetime.strptime(str(last_ts), "%Y-%m-%d %H:%M:%S")
                            elapsed = (now_ts - last_dt).total_seconds()
                            # 负间隔（时钟回拨）视为已过冷却，不误伤（对抗性审查第三轮）
                            if 0 <= elapsed < cooldown:
                                # 不暴露冷却时长（信息分层）
                                return jsonify({"error": "切换过于频繁，请稍后再试"}), 429
                        except ValueError:
                            # ts 格式异常（写坏）：保守按冷却生效拦截（M3：防 fail-open 绕过）
                            return jsonify({"error": "切换过于频繁，请稍后再试"}), 429
            # 满员提示（对抗性审查补，2026-08-15 用户决策：可继续选+提示会顺延）：
            # 该片已选人数 ≥ 块容量时仍允许保存（先到先得+溢出顺延语义），但明确告知；
            # 提示不暴露真实人数/容量（防调研，与用户端 pct 口径一致）
            cap = load_env_int(ENV_FILE, "YIBAN_BLOCK_CAP", 15)
            stats = {s["slot_min"]: s["count"] for s in db.time_pref_stats()}
            count = stats.get(slot, 0)
            cur = db.get_time_pref(phone)
            if cur and cur.get("slot_min") == slot:
                count = max(0, count - 1)  # 排除自己已占的位（换片/保留不误报）
            full_notice = "，该时段已选满，将就近安排到附近时段" if count >= cap else ""
            # updated_at 带微秒（M2 对抗性审查）：同秒保存的"先到先得"可区分先后，
            # 不再退化为按 phone 顺序的不可预期平局（字典序定宽，旧秒级数据兼容为更早）
            db.set_time_pref(phone, slot, datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"))
            db.audit(session.get("username", "?"), "time_pref_set", phone, _slot_to_label(slot))
            # 生效分界（2026-08-15 用户反馈：卡点缓冲）：
            # 优先用当日调度快照标记（signin 构建调度后写入 sched-snapshot-YYYY-MM-DD.json，
            # 精确等于 cron 实际读取自选表的时刻）——改选在快照后必为"明日生效"，提示与实际 100% 一致；
            # 标记不存在（当日 cron 未运行/自选未激活）回退"窗口起点 + 1 分钟"兜底
            now = datetime.now()
            boundary = None
            try:
                snap_path = os.path.join(STATE_DIR, f"sched-snapshot-{now.strftime('%Y-%m-%d')}.json")
                with open(snap_path, encoding="utf-8") as f:
                    snap = json.load(f)
                boundary = datetime.strptime(
                    f"{now.strftime('%Y-%m-%d')} {snap['snapshot_at']}", "%Y-%m-%d %H:%M:%S"
                )
                # H1 对抗性审查：快照标记晚于当前时刻（时钟偏移/写坏）→ 视为无效回退兜底，
                # 避免"提示今日生效但实际不可能"（改选时 cron 早已建表）
                if boundary > now:
                    boundary = None
            except (OSError, ValueError, KeyError, TypeError):
                boundary = None
            if boundary is None:
                try:
                    boundary = now.replace(hour=sw[0][0], minute=sw[0][1], second=0, microsecond=0)
                except ValueError:
                    boundary = now
                boundary += timedelta(minutes=1)
            when = "今日生效" if now < boundary else "明日生效"
            return jsonify({"ok": True, "msg": f"已保存自选 {_slot_to_label(slot)}，{when}{full_notice}"})

    @app.route("/api/time-prefs/stats")
    def api_time_prefs_stats():
        """每片已选人数（拥挤度，管理员；用户端由 my-time-pref 附带，不单独暴露）。"""
        sw = _sign_window()
        stats = {s["slot_min"]: s["count"] for s in db.time_pref_stats()}
        cap = load_env_int(ENV_FILE, "YIBAN_BLOCK_CAP", 15)
        return jsonify({
            "ok": True,
            "slots": [{**s, "count": stats.get(s["slot_min"], 0), "cap": cap}
                      for s in _pref_slots(sw)],
        })

    @app.route("/api/my-accounts", methods=["POST"])
    def api_my_account_add():
        """提交自己的易班账号：每个用户仅限 1 套，写入 accounts.json 状态 pending（待审核）。

        操作级锁：单账号限制与手机号唯一检查 + 写入原子（防并发双提交互相覆盖）。
        """
        with _file_lock:
            accounts = load_accounts()
            # 容量兜底：账号总数上限（用户提交同样受限，对抗性审查补）
            max_accounts = load_env_int(ENV_FILE, "YIBAN_MAX_ACCOUNTS", DEFAULT_MAX_ACCOUNTS)
            if max_accounts > 0 and len(accounts) >= max_accounts:
                _notify_capacity_once("accounts", max_accounts, "账号数量")
                # 不向普通用户暴露容量数字（信息分层，2026-08-15）
                return jsonify({"error": "账号数量已达上限，请联系管理员"}), 403
            # 单账号限制：已有未删除提交（含待审核/已生效）则拒绝；待删除（管理员已删）不占名额
            email = session.get("username", "").lower()
            has_live = any(a.get("owner") == email and not a.get("deleted") for a in accounts)
            if has_live:
                return jsonify({"error": "每个用户只能提交一个账号，可编辑或删除后重新提交"}), 400
            data = _json_body()
            err, clean = validate_account(data, require_password=True)
            if err:
                return jsonify({"error": err}), 400
            if find_account_index(accounts, clean["phone"]) is not None:
                return jsonify({"error": f"手机号 {clean['phone']} 已被使用"}), 400
            # 管理员提交的账号归属 'admin'（后台添加账号同理），直接生效免审核
            clean["owner"] = (
                "admin" if _current_role() == "admin" else session.get("username", "").lower()
            )
            clean["status"] = ACCOUNT_STATUS_PENDING if _current_role() != "admin" else ACCOUNT_STATUS_ACTIVE
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
        days_in_month = calendar.monthrange(year, mon)[1]
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
        return jsonify({
            "ok": True,
            "month": month,
            "days": result,
            "sunday_sign": load_env_int(ENV_FILE, "YIBAN_SUNDAY_SIGN", 0),  # 前端据此决定周日是否置灰/可查
        })

    @app.route("/api/my-logs")
    def api_my_logs():
        """我的账号指定日期（YYYY-MM-DD）的日志（按手机号过滤，最多 50 条）。

        2026-08-16 起读按天文件（sign-YYYY-MM-DD.log）：历史日期同样可查
        （此前只读当前 sign.log，轮转后历史日期恒为空）。
        """
        date = str(request.args.get("date", "")).strip()
        if not _is_valid_date_str(date):
            return jsonify({"error": "日期格式不正确，应为 YYYY-MM-DD"}), 400
        accounts = load_accounts()
        indices = _my_account_indices()
        phones = [str(accounts[i].get("phone", "")) for i in indices]
        out = []
        for line in _log_lines_for(date):
            if any(f"[{p}]" in line for p in phones):
                out.append(line.strip())
        # 脱敏后再截断：与 /api/logs 同口径（日志行内 [手机号] 不落完整号）
        return jsonify({"ok": True, "date": date, "logs": [_mask_log_phones(ln) for ln in out[-50:]]})

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
            data = _json_body()
            err, clean = validate_account(data, require_password=False)
            if err:
                return jsonify({"error": err}), 400
            if (
                clean["phone"] != old.get("phone")
                and find_account_index(accounts, clean["phone"]) is not None
            ):
                return jsonify({"error": f"手机号 {clean['phone']} 已被使用"}), 400
            # 手机号变更 → 旧号自选时间片失效（防孤儿 pref 占容量，对抗性审查补）
            if clean["phone"] != old.get("phone"):
                db.clear_time_pref(old.get("phone", ""))
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
                ACCOUNT_STATUS_PENDING
                if old.get("status") == ACCOUNT_STATUS_REJECTED
                else old.get("status", ACCOUNT_STATUS_PENDING)
            )
            if clean["status"] == ACCOUNT_STATUS_PENDING:
                # 2026-08-16 审查轮修复：原 clean.pop("reject_reason") 对不存在的键是空操作，
                # 导致重新提交后旧拒绝理由残留（注释意图与实际不符）；显式置空随 update 落库
                clean["reject_reason"] = ""
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
            # 用户改密码/识别码后清除熔断暂停，立即恢复签到
            clear_fuse_pause(clean["phone"])
            logger.info("用户 %s 编辑账号 %s", _mask_email(clean["owner"]), _mask_phone(clean["phone"]))
            if old.get("status") == ACCOUNT_STATUS_REJECTED:
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

    @app.route("/api/my-accounts/<int:idx>/pause", methods=["PUT"])
    def api_my_account_pause(idx):
        """用户自暂停/恢复签到（调度 v2）：暂停后主程序自动跳过，状态显示红底"已取消"。"""
        with _file_lock:
            accounts = load_accounts()
            indices = _my_account_indices_of(accounts)
            if not 0 <= idx < len(indices):
                return jsonify({"error": "账号不存在"}), 404
            acc = accounts[indices[idx]]
            data = _json_body()
            paused = 1 if str(data.get("paused", "")).strip().lower() in ("1", "true", "on", "yes") else 0
            # 2026-08-15 用户确认：管理员不能暂停自己账号（owner=admin 为系统/管理员账号；
            # 暂停是普通用户管理自己账号的能力，管理端界面本无此入口，防 /user 页绕过）。
            # 恢复放行（幂等无害；该状态本不可达，仅保持接口一致性）
            if paused and acc.get("owner", "admin") == "admin":
                return jsonify({"error": "管理员账号不支持自暂停"}), 403
            # 暂停冷却（2026-08-15 用户裁决）：仅"暂停"计冷却（固定间隔，默认 30s），
            # "恢复"不受限——恢复是紧迫正向操作，绝不该被挡。冷却防连点/防审计噪音，
            # 非安全边界。按用户计价（多管理员共享账号各自独立，可接受）。时长可配
            # （YIBAN_PAUSE_COOLDOWN_SEC，默认 30；0=关闭）。不暴露时长（信息分层）。
            if paused:
                base_cd = load_env_int(ENV_FILE, "YIBAN_PAUSE_COOLDOWN_SEC", PAUSE_COOLDOWN_SEC)
                if base_cd > 0:
                    last_ts = db.last_pause_at(session.get("username", "") or "")
                    if last_ts:
                        try:
                            last_dt = datetime.strptime(str(last_ts), "%Y-%m-%d %H:%M:%S")
                            # 负间隔（时钟回拨）视为已过冷却，不误伤（与弹性冷却同口径）
                            if 0 <= (datetime.now() - last_dt).total_seconds() < base_cd:
                                return jsonify({"error": "操作过于频繁，请稍后再试"}), 429
                        except ValueError:
                            # ts 格式异常（写坏）：保守按冷却生效拦截（M3 口径：防 fail-open 绕过）
                            return jsonify({"error": "操作过于频繁，请稍后再试"}), 429
            db.set_user_paused(acc["id"], paused)
            db.audit(
                session.get("username", "") or "?",
                "my_account_pause" if paused else "my_account_resume",
                _mask_phone(acc.get("phone", "")),
                "用户自暂停" if paused else "用户恢复",
            )
            logger.info(
                "用户 %s %s 账号 %s",
                session.get("username", ""), "暂停" if paused else "恢复",
                _mask_phone(acc.get("phone", "")),
            )
            return jsonify({
                "ok": True,
                "msg": "已暂停签到，主程序将自动跳过" if paused else "已恢复签到",
                "paused": bool(paused),
            })

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
                    and a.get("status") == ACCOUNT_STATUS_PENDING
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
            data = _json_body()
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
                target = next((u for u in users if u.get("email") == email), None)
                if not target or email == builtin:  # 内置管理员不可批量操作
                    continue
                if action == "set_admin":
                    # 只能将正式用户（有生效账号且无待审核）设为管理员；
                    # 正式用户判定仅 status==active 算（rejected 不算），且软删除不算
                    has_pending = any(
                        a.get("owner") == email
                        and a.get("status") == ACCOUNT_STATUS_PENDING
                        and not a.get("deleted")
                        for a in accounts
                    )
                    has_active = any(
                        a.get("owner") == email
                        and a.get("status") == ACCOUNT_STATUS_ACTIVE
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
        data = _json_body()
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
                    and a.get("status") == ACCOUNT_STATUS_PENDING
                    and not a.get("deleted")
                    for a in accounts
                )
                has_active = any(
                    a.get("owner") == email
                    and a.get("status") == ACCOUNT_STATUS_ACTIVE
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
        data = _json_body()
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
        data = _json_body()
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
    _batch_signin_running = False  # 批量签到队列互斥：同时只允许一个在跑
    _batch_signin_lock = threading.Lock()

    # ---- 手动签到（单个 / 批量）----
    def _spawn_signin(phone, accounts=None):
        """触发单账号手动签到子进程（signin.py --only）。

        防抖：60 秒内同账号不重复触发（SIGN_MIN_INTERVAL）；仍在运行的旧进程先终止。
        返回 (ok: bool, msg: str)。
        """
        accounts = accounts if accounts is not None else load_accounts()
        idx = find_account_index(accounts, phone)
        if idx is None:
            return False, f"账号 {phone} 不在配置中"
        acc = accounts[idx]
        if acc.get("deleted") or acc.get("status") != ACCOUNT_STATUS_ACTIVE:
            return False, f"账号 {phone} 不可手动签到（未生效或已删除）"
        with _signin_lock:  # 原子检查+占位：并发请求不能同时通过防抖
            now = time.time()
            if phone in _last_trigger and now - _last_trigger[phone] < SIGN_MIN_INTERVAL:
                remain = int(SIGN_MIN_INTERVAL - (now - _last_trigger[phone]))
                return False, f"账号 {phone} 正在签到，请 {remain} 秒后再试"
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
        with contextlib.suppress(OSError):
            log_fh = open(
                log_path_for(), "a", encoding="utf-8", buffering=1
            )  # 日志不可写时丢弃，不影响签到执行（按天文件：sign-当天.log）
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
            return False, f"账号 {phone} 手动签到启动失败，请稍后重试"
        finally:
            if log_fh is not None:
                log_fh.close()  # 父进程关闭句柄（子进程已继承自身副本），防句柄累积
        logger.info("触发手动签到: %s", _mask_phone(phone))
        return True, f"已触发 {phone} 手动签到（后台执行，日志约 30 秒内刷新）"

    @app.route("/api/signin", methods=["POST"])
    def api_signin():
        """手动签到指定账号：子进程执行 signin.py --only（与 TUI M 键一致）。"""
        data = _json_body()
        phone = str(data.get("phone", "")).strip()
        ok, msg = _spawn_signin(phone)
        if not ok:
            if "不在配置中" in msg:
                return jsonify({"error": msg}), 404
            if "不可手动签到" in msg:
                return jsonify({"error": msg}), 400
            if "正在签到" in msg:
                return jsonify({"error": msg}), 429
            return jsonify({"error": msg}), 500
        return jsonify({"ok": True, "msg": msg})

    @app.route("/api/signin/batch", methods=["POST"])
    def api_signin_batch():
        """批量手动签到：顺序逐个触发（与自动签到同语义，防风控）。

        全局互斥（同时只允许一个批量队列在跑）；防抖冲突的账号自动跳过。
        接口立即返回，实际执行在后台线程。
        """
        data = _json_body()
        ids = data.get("ids")
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "请先勾选要签到的账号"}), 400
        accounts = load_accounts()
        phones = []
        for i in ids:
            if not isinstance(i, int) or not 0 <= i < len(accounts):
                continue
            acc = accounts[i]
            if acc.get("deleted") or acc.get("status") != ACCOUNT_STATUS_ACTIVE:
                continue
            phone = str(acc.get("phone", "")).strip()
            if phone:
                phones.append(phone)
        if not phones:
            return jsonify({"error": "选中的账号均不可手动签到（未生效或已删除）"}), 400
        nonlocal _batch_signin_running
        with _batch_signin_lock:
            if _batch_signin_running:
                return jsonify({"error": "已有批量签到正在执行，请稍后再试"}), 429
            _batch_signin_running = True

        def _run_batch():
            try:
                ok_n = skip_n = 0
                for phone in phones:
                    ok, _ = _spawn_signin(phone, accounts=accounts)
                    if ok:
                        ok_n += 1
                        proc = _signin_procs.get(phone)
                        if proc:
                            with contextlib.suppress(Exception):
                                proc.wait(timeout=300)  # 等该账号完成再触发下一个（顺序执行）
                    else:
                        skip_n += 1
                logger.info("批量手动签到完成: 触发 %s 个，跳过 %s 个", ok_n, skip_n)
            finally:
                nonlocal _batch_signin_running
                with _batch_signin_lock:
                    _batch_signin_running = False

        threading.Thread(target=_run_batch, daemon=True).start()
        return jsonify({
            "ok": True,
            "msg": f"已加入批量签到队列（{len(phones)} 个账号，将按顺序逐个执行，日志约几分钟内刷新）",
        })

    # ---- 日志与状态 ----
    @app.route("/api/logs")
    def api_logs():
        """签到日志与今日状态。

        ?date=YYYY-MM-DD（可选，仅管理员）：缺省=今天（原行为不变）；
        指定日期时 logs 为该日日志、states 仍为今日状态（账号表格图标语义不随历史日期变化）。
        """
        date = str(request.args.get("date", "")).strip()
        if date and not _is_valid_date_str(date):
            return jsonify({"error": "日期格式不正确，应为 YYYY-MM-DD"}), 400
        date = date or datetime.now().strftime("%Y-%m-%d")
        logs = _log_lines_for(date)
        # 响应层脱敏：日志行内 [手机号] 不落完整号（前端 maskPhone 幂等兼容）。
        # 注意：不返回 states——账号表格图标的事实源是 /api/accounts（sign-state 文件），
        # 日志符号（✅/❌）与状态码（success/failed）语义不同，曾造成前端图标/统计卡被
        # 符号污染（2026-08-16 审查轮修复）。
        return jsonify(
            {
                "ok": True,
                "logs": [_mask_log_phones(ln) for ln in logs[-80:]],
                "log_file": f"sign-{date}.log",  # 只暴露文件名，不暴露服务器路径
                "date": date,
            }
        )

    # ---- 设置（随机延迟，写入 .env）----
    @app.route("/api/settings")
    def api_settings():
        env = read_env(ENV_FILE)
        mode = env.get("YIBAN_SIGN_MODE", "").strip().lower()
        sw = _sign_window()
        return jsonify(
            {
                "ok": True,
                "start_delay_max": load_env_int(ENV_FILE, "YIBAN_START_DELAY_MAX", 0),
                "gap_max": load_env_int(ENV_FILE, "YIBAN_ACCOUNT_GAP_MAX", 0),
                "default_start_delay_max": DEFAULT_START_DELAY_MAX,
                "default_gap_max": DEFAULT_ACCOUNT_GAP_MAX,
                # 签到模式：sequence（列表顺序，默认）/ random（列表随机打散）
                "sign_mode": mode or "sequence",
                # 调度 v2：排序×分布二级开关 + 首尾缓冲 + 自选总开关 + 窗口
                "sign_order": env.get("YIBAN_SIGN_ORDER", "").strip().lower() or (
                    "random" if mode == "random" else "sequence"),
                "sign_dist": env.get("YIBAN_SIGN_DIST", "").strip().lower() or (
                    "normal" if mode == "normal" else "uniform"),
                "window_edge_sec": load_env_int(ENV_FILE, "YIBAN_WINDOW_EDGE_SEC", 60),
                "allow_time_pref": load_env_int(ENV_FILE, "YIBAN_ALLOW_TIME_PREF", 0),
                "sign_window": f"{sw[0][0]:02d}:{sw[0][1]:02d} ~ {sw[1][0]:02d}:{sw[1][1]:02d}",
                # 容量状态（对抗性审查补）：注册/账号上限与当前使用量（管理员知情）
                "capacity": {
                    "users": len(db.load_users()),
                    "users_max": load_env_int(ENV_FILE, "YIBAN_MAX_USERS", DEFAULT_MAX_USERS),
                    "accounts": len(load_accounts()),
                    "accounts_max": load_env_int(ENV_FILE, "YIBAN_MAX_ACCOUNTS", DEFAULT_MAX_ACCOUNTS),
                },
                # 周日签到：1=开启（周日也尝试签到），0=关闭（默认）
                "sunday_sign": load_env_int(ENV_FILE, "YIBAN_SUNDAY_SIGN", 0),
                # 批量多选：前端会话级开关（不持久化，每次进入页面默认关闭）
                "batch_mode": False,
            }
        )

    @app.route("/api/settings", methods=["POST"])
    def api_settings_save():
        data = _json_body()
        # 调度权限（2026-08-15 确认）：仅主管理员可改调度字段（排序/分布/缓冲/自选/窗口）；
        # 普通管理员可改随机延迟/周日/公告等低风险项
        username = session.get("username") or ""
        is_master = username.strip().lower() == _builtin_admin_email()
        if not is_master and any(
            k in data for k in ("sign_order", "sign_dist", "window_edge_sec",
                                "allow_time_pref", "sign_window")
        ):
            return jsonify({"error": "仅主管理员可修改调度设置"}), 403
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
        # 调度 v2：排序×分布二级开关（替代旧三选一，旧值自动映射兼容）
        sign_order = str(data.get("sign_order", "")).strip().lower()
        sign_dist = str(data.get("sign_dist", "")).strip().lower()
        if sign_order and sign_order not in ("sequence", "random"):
            return jsonify({"error": "排序方式取值应为 sequence 或 random"}), 400
        if sign_dist and sign_dist not in ("uniform", "normal"):
            return jsonify({"error": "分布方式取值应为 uniform 或 normal"}), 400
        if sign_order:
            write_env_key(ENV_FILE, "YIBAN_SIGN_ORDER", sign_order)
        if sign_dist:
            write_env_key(ENV_FILE, "YIBAN_SIGN_DIST", sign_dist)
        # 首尾缓冲（0=关闭，设置页需警示尾部风险）
        # 注意：0 是合法配置值（关闭缓冲），不能用 write_env_int（其语义为 <=0 删除行）
        edge_raw = data.get("window_edge_sec")
        if edge_raw is not None:
            try:
                edge = int(edge_raw)
            except (TypeError, ValueError):
                return jsonify({"error": "首尾缓冲必须是整数秒"}), 400
            if not (0 <= edge <= 600):
                return jsonify({"error": "首尾缓冲应在 0~600 秒之间"}), 400
            write_env_key(ENV_FILE, "YIBAN_WINDOW_EDGE_SEC", str(edge))
        # 用户自选总开关（0/1；0 同样需显式写入）
        pref_raw = data.get("allow_time_pref")
        if pref_raw is not None:
            pref = 1 if str(pref_raw).strip().lower() in ("1", "true", "on", "yes") else 0
            write_env_key(ENV_FILE, "YIBAN_ALLOW_TIME_PREF", str(pref))
        # 签到窗口（HH:MM，管理员可调；校验非法拒绝）
        win = str(data.get("sign_window", "")).strip()
        if win:
            try:
                w_start, w_end = win.split("~")
                sh, sm = (int(x) for x in w_start.strip().split(":"))
                eh, em = (int(x) for x in w_end.strip().split(":"))
            except (ValueError, AttributeError):
                return jsonify({"error": "签到窗口格式应为 HH:MM ~ HH:MM"}), 400
            if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59 and (sh, sm) < (eh, em)):
                return jsonify({"error": "签到窗口非法（需 HH:MM 且开始早于结束）"}), 400
            write_env_key(ENV_FILE, "YIBAN_SIGN_START", f"{sh:02d}:{sm:02d}")
            write_env_key(ENV_FILE, "YIBAN_SIGN_END", f"{eh:02d}:{em:02d}")
        # 周日签到开关（1=开启/0=关闭）：写入 .env，cron 的 run.sh 加载后 signin.py 生效
        sunday_sign = 1 if str(data.get("sunday_sign", "")).strip().lower() in ("1", "true", "on", "yes") else 0
        write_env_int(ENV_FILE, "YIBAN_SUNDAY_SIGN", sunday_sign)
        # 批量多选为前端会话级开关，不写入配置
        logger.info(
            "更新设置: 启动=%s 间隔=%s 签到模式=%s 排序=%s 分布=%s 缓冲=%s 自选=%s 窗口=%s 周日=%s",
            start, gap, sign_mode or "不变", sign_order or "不变", sign_dist or "不变",
            edge_raw if edge_raw is not None else "不变", pref_raw if pref_raw is not None else "不变",
            win or "不变", sunday_sign,
        )
        # 设置变更审计（2026-08-16 补 P8：此前调度/系统设置保存无留痕，与其他管理操作不一致）
        db.audit(
            session.get("username") or "?",
            "settings_save",
            "settings",
            f"启动延迟={start} 间隔={gap} 模式={sign_mode or '-'} 排序={sign_order or '-'} "
            f"分布={sign_dist or '-'} 缓冲={edge_raw if edge_raw is not None else '-'} "
            f"自选={pref_raw if pref_raw is not None else '-'} 窗口={win or '-'} 周日={sunday_sign}",
        )
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
        data = _json_body()
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
    global ACCOUNTS_FILE, LOG_FILE, ENV_FILE, STATE_DIR, DB_FILE
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
        "--db", default=DB_DEFAULT, help=f"SQLite 数据库路径（默认: {DB_DEFAULT}）"
    )
    parser.add_argument("--debug", action="store_true", help="Flask 调试模式")
    args = parser.parse_args()
    ACCOUNTS_FILE = args.config
    LOG_FILE = args.log
    ENV_FILE = args.env
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
