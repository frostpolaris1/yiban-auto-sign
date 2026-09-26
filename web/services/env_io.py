# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`.env` 读写与设置项展示：写锁、注入校验、原子落盘与公告元数据解析。

**功能**
`.env` 的全部读写入口：宽松解析 `read_env`、整数配置 `load_env_int`、键值写入
`write_env_key` / `write_env_int` 与批量原子写 `write_env_batch`、首次启动的
`YIBAN_SECRET_KEY` 生成 `ensure_secret_key`、写互斥 `_env_write_lock`；外加设置项展示族
（`_settings_label` / `_settings_value_text` / `_settings_effective_values`）、代理地址形状
校验 `_is_http_proxy_url`、启动期的歧义键报告 `_report_env_key_collisions` 与公告元数据
解析 `_parse_announcement_meta`。

**归属**
原 `web/app.py` 的模块级 env 辅助，唯一真源在本模块；`web/app.py` 只保留名字面与转发，
把它自己持有、而本模块需要的东西——落盘用的 `_atomic_write`、设置缺省值、开关解析器
`_env_flag`、告警出口 `send_notification`——在调用时刻现取后注入。

**复用**
批量写是单键写与整数写的底层（`write_env_key` / `write_env_int` 都折成
`write_env_batch({key: value})`），行分隔符注入校验、旧行折叠与原子替换因此只有一份；
公告草稿与线上公告共用同一份元数据形态，故解析器与它的两个格式常量同在一处。

**通信**
本模块不反向导入 `web.app`（本仓测试以 `spec_from_file_location` 别名加载 `app.py`，
普通 import 会再执行一份副本模块）。落盘动作与设置项缺省值/开关判定都作为显式参数接收：
它们在 `web.app` 上是会被测试改写、也会随运行方式变化的模块级名字（既有测试正是在
`web.app` 上打桩 `_atomic_write` / `write_env_batch` 来观测每一次落盘），本模块另持一份
绑定会让打桩静默失效。跨进程写锁沿用 `yiban.infra.env_lock` 的真源，不自建第二套。
"""

import contextlib
import logging
import os
import re
import secrets
from datetime import datetime

from yiban import window as yb_window
from yiban.infra import env_io as _env_io
from yiban.infra import env_lock
from yiban.mail import layout as mail_layout

# 与 web.app 同名的日志通道：.env 读写与启动检测的告警落回既有通道，便于运维沿用同一处过滤
logger = logging.getLogger("web")


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def read_env(env_path):
    """读取 .env 全部键值，返回 dict（宽松：文件缺失/读失败返回空 dict）。

    解析实现单一来源见 `yiban.infra.env_io.parse_env_file`（utf-8-sig 兼容 BOM——Windows
    记事本等工具保存时会带 BOM，否则首个键名会带上 \\ufeff 前缀导致读不到，
    管理员登录/改密会静默失败）。
    """
    return _env_io.parse_env_file(env_path)


def load_env_int(env_path, key, default):
    """读取 .env 中的整数配置，缺失/非法回退默认值。"""
    try:
        return max(0, int(read_env(env_path).get(key, "")))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 设置项展示（键的中文名 / 值的展示形态 / A/B 档生效值）
# ---------------------------------------------------------------------------
# A/B 档键的中文标签：变更告警正文与审计明细共用一份，避免同一件事在两处各写一套字面量
_SETTINGS_KEY_LABELS = {
    "sign_window": "签到窗口",
    "window_edge_sec": "首尾裁剪",
    "edge_front_sec": "前裁缓冲",
    "edge_back_sec": "后裁缓冲",
    "sunday_sign": "周日签到",
    "saturday_sign": "周六签到",
    "global_pause": "全局暂停签到",
    "registration_pause": "暂停注册",
    "start_delay_max": "启动随机延迟",
    "gap_max": "账号间隔",
    "max_users": "用户容量上限",
    "max_accounts": "账号容量上限",
    "account_verify": "注册账号验证",
    "probe_enable": "健康探针",
    "probe_time": "探针时刻",
    "probe_interval": "探针频率",
    "sign_order": "签到排序",
    "sign_dist": "签到分布",
    "sign_mode": "签到模式",
    "allow_time_pref": "自选时间片",
}

# 这些键的生效值是 0/1 开关：写进审计与告警正文时翻成中文，免得运维盯着 "0"→"1" 心算
_BOOL_SETTINGS_KEYS = frozenset({
    "sunday_sign", "saturday_sign", "global_pause", "registration_pause",
    "account_verify", "probe_enable", "allow_time_pref",
})


def _settings_label(key):
    """设置键的中文名（未列入标签表的按键名原样回，绝不编一个名字）。"""
    return _SETTINGS_KEY_LABELS.get(key, key)


def _settings_value_text(key, value):
    """设置值写进审计/告警正文时的展示形态（值本身已在现读侧归一，不含敏感串）。"""
    if key in _BOOL_SETTINGS_KEYS:
        return "开" if str(value) == "1" else "关"
    return str(value)


def _settings_effective_values(env_file, env_flag, *, gap_max_default, max_users_default,
                               max_accounts_default):
    """A/B 档设置项的**当前生效值**（归一为字符串），取值口径与 `GET /api/settings` 一致。

    只用于"这次请求到底改没改配置"的判定：一律现读现算，绝不信请求自带的旧值——
    否则把当前值原样抄进请求就能自称"无变更"，口令复核与变更告警双双被绕开
    （系统开关门原本就是这个语义，这里把同一语义铺满全部 A/B 档键）。

    开关解析器与三个容量缺省值由调用方传入：它们是 `web.app` 的模块级名字（`_env_flag`
    之后还会随签到状态域迁走），本模块另持绑定会让打桩与后续搬动静默失效。
    """
    env = read_env(env_file)
    mode = env.get("YIBAN_SIGN_MODE", "").strip().lower()
    w_start, w_end, _invalid = yb_window.parse_window(env)
    front, back = yb_window.parse_edges(env)

    def _flag(key):
        return "1" if env_flag(env.get(key, "")) else "0"

    return {
        "start_delay_max": str(load_env_int(env_file, "YIBAN_START_DELAY_MAX", 0)),
        "gap_max": str(load_env_int(env_file, "YIBAN_ACCOUNT_GAP_MAX", gap_max_default)),
        "sign_window": (f"{w_start[0]:02d}:{w_start[1]:02d}"
                        f"~{w_end[0]:02d}:{w_end[1]:02d}"),
        # 旧键（前后对称）**一次写两侧**：现值取「前/后」组合串。只按前裁比的话，
        # 前后不等的存量配置提交一个等于旧前裁的值会被判成"没改"，从而绕开口令复核，
        # 而写侧其实把后裁改了。
        "window_edge_sec": f"{front}/{back}",
        "edge_front_sec": str(front),
        "edge_back_sec": str(back),
        "sunday_sign": _flag("YIBAN_SUNDAY_SIGN"),
        "saturday_sign": _flag("YIBAN_SATURDAY_SIGN"),
        # 两个暂停位沿用 `load_env_int(...) == 1` 的既有判据（写侧只落 "1" 或删键），
        # 与 GET /api/settings 及系统开关门读的现值逐字一致
        "global_pause": "1" if load_env_int(env_file, "YIBAN_GLOBAL_PAUSE", 0) == 1 else "0",
        "registration_pause": "1" if load_env_int(env_file, "YIBAN_REGISTRATION_PAUSE", 0) == 1 else "0",
        "allow_time_pref": str(load_env_int(env_file, "YIBAN_ALLOW_TIME_PREF", 0)),
        "sign_mode": mode,
        # 排序/分布的生效值由旧模式派生（与 GET 同一式子）：只存 YIBAN_SIGN_MODE 的
        # 存量配置，其真实排序就是派生值，拿空串比会把"没改"误判成"改了"
        "sign_order": env.get("YIBAN_SIGN_ORDER", "").strip().lower() or (
            "random" if mode == "random" else "sequence"),
        "sign_dist": env.get("YIBAN_SIGN_DIST", "").strip().lower() or (
            "normal" if mode == "normal" else "uniform"),
        "account_verify": _flag("YIBAN_ACCOUNT_VERIFY"),
        "probe_enable": _flag("YIBAN_PROBE_ENABLE"),
        "probe_time": env.get("YIBAN_PROBE_TIME", "20:00").strip() or "20:00",
        "probe_interval": env.get("YIBAN_PROBE_INTERVAL_DAYS", "1").strip() or "1",
        "max_users": str(load_env_int(env_file, "YIBAN_MAX_USERS", max_users_default)),
        "max_accounts": str(load_env_int(env_file, "YIBAN_MAX_ACCOUNTS",
                                         max_accounts_default)),
    }


def _is_http_proxy_url(value):
    """代理地址格式校验：`http(s)://[user:pass@]host[:port]`（宽松但明确）。

    只做形状校验（scheme + 主机非空、无空白/换行）；**不解析、不连接**——
    代理是否可用由签到进程在使用时报错，网页侧只拦"明显填错"（例如把备注写进去）。
    """
    if not value:
        return True                       # 空串=直连，合法
    if re.search(r"\s", value):
        return False
    parts = re.match(r"^https?://([^/]*?)(/.*)?$", value)
    if not parts:
        return False
    host_part = parts.group(1)
    if "@" in host_part:                  # 去掉可能的 userinfo
        host_part = host_part.rsplit("@", 1)[1]
    return bool(host_part)


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _env_write_lock(env_path):
    """.env 写互斥：复用 `yiban.infra.env_lock` 的共享锁。

    并发保存设置/公告时 read-modify-write 会丢更新——gunicorn 多 worker 跨进程写 .env
    需文件锁；同一把锁也供密钥生成等场景使用，避免各写各的锁文件。
    """
    with env_lock.env_write_lock(env_path):
        yield


def write_env_int(env_path, key, value, write_batch):
    """把整数配置写入 .env：value<=0 删除该行，>0 写入；保留其他行。"""
    write_env_key(env_path, key, str(value) if value > 0 else "", write_batch)


def write_env_key(env_path, key, value, write_batch):
    """把任意键值写入 .env：value 为空删除该行，否则写入；保留注释与其他行。

    单键形态 = write_env_batch({key: value})：行分隔符注入校验、写锁、原子替换
    均单源在 write_env_batch（防两份安全校验实现漂移）。`write_batch` 由调用方传入
    `web.app` 的 `write_env_batch`——它是既有测试观测"每一次 .env 落盘"的打桩点。
    """
    write_batch(env_path, {key: value})


def write_env_batch(env_path, updates, atomic_write, audit=None):
    """批量写入多个键值（原子操作）：读取一次，修改多个键，写入一次。
    避免多次独立写入时进程崩溃导致配置不一致。
    updates: dict {key: value}，value 为空字符串则删除该键。

    行模型、键/值校验、写入前后"键集合 diff"**全部单源在**
    `yiban.infra.env_io.write_env_keys`（web 与引擎共用一个实现，两侧拒绝文案同源）：
    本函数只负责 web 侧特有的两件事——跨进程写锁（`_env_write_lock`）与落盘注入
    （`atomic_write`）。`audit` 由调用方（web.app 转发）注入 db 审计回调：写入被拒
    （潜伏分隔符/未请求的键变化）时必须留痕，调用方不传也不影响拒绝本身。

    为什么不再自己用宽行模型读-改-写：宽行模型会把注释里潜伏的
    U+0085/U+2028 等先拆成两行、再把后半截实体化成真配置行（一次无关保存即可注入
    `YIBAN_GLOBAL_PAUSE=1`）。单一行模型同时是"校验行模型 = 写入行模型"的前提。
    """
    with _env_write_lock(env_path):
        _env_io.write_env_keys(
            env_path, updates,
            write_text=lambda path, text: atomic_write(path, text, chmod_priv=True),
            audit=audit, delete_empty=True)


def ensure_secret_key(env_path, atomic_write, audit=None):
    """确保 .env 中存在 YIBAN_SECRET_KEY（缺失时自动生成随机值）。

    .env 不可写、或既有行含潜伏行分隔符（写入被 fail-closed 拒绝）时降级为进程内随机
    密钥并告警（服务可用，重启后会话失效）——与口令哈希迁移的降级策略一致：宁可告警后
    带病运行，也不让启动直接失败。行模型/校验单源在 `yiban.infra.env_io.write_env_keys`。
    """
    with _env_write_lock(env_path):
        # 全新部署判定必须在读取前——.env 不存在 = 首次初始化，默认写入「暂停注册」；
        # 既有部署（文件已存在，如升级安装）不写此键，注册行为保持不变
        # （用户裁决：默认允许，新部署才默认暂停）。
        # touch 空 .env / 复制 .env.example 后文件存在但无任何有效键
        # 仍视为全新部署（此前判定仅看文件存在性，会把空配置误判为既有部署
        # 而不写暂停键，新部署默认开放注册）。
        env = read_env(env_path)
        new_deployment = not os.path.exists(env_path) or not env
        key = env.get("YIBAN_SECRET_KEY", "").strip()
        if key:
            return key
        key = secrets.token_hex(32)
        # 常量字面量写入（暂停键），无注入面；管理员完成初始配置后在设置页开启注册。
        # 旧键折叠由 write_env_keys 的 key_line_pattern 承担（`YIBAN_SECRET_KEY = `
        # 这类带空白写法同样是该键的行，不会被漏判出重复影子行）。
        updates = {"YIBAN_SECRET_KEY": key}
        if new_deployment:
            updates["YIBAN_REGISTRATION_PAUSE"] = "1"
        try:
            _env_io.write_env_keys(
                env_path, updates,
                write_text=lambda path, text: atomic_write(path, text, chmod_priv=True),
                audit=audit, delete_empty=True)
        except (OSError, ValueError) as e:
            logger.warning(
                "无法写入 %s（%s）：YIBAN_SECRET_KEY 仅本次进程生效（重启后会话将失效），"
                "请修复目录权限或清理潜伏行分隔符后重试",
                env_path, e,
            )
            return key
        logger.info("已自动生成 YIBAN_SECRET_KEY 并写入 %s", env_path)
        if new_deployment:
            logger.info(
                "新部署默认暂停注册（YIBAN_REGISTRATION_PAUSE=1），"
                "完成初始配置后可在设置页开启"
            )
        return key


# ---------------------------------------------------------------------------
# 公告元数据（草稿与线上公告共用同一形态：`<小写邮箱>|YYYY-MM-DD HH:MM:SS`）
# ---------------------------------------------------------------------------
# 该形态存在 .env 的公告键里，故解析器与它的分隔符/时刻格式归本模块；键名本身
# （ANNOUNCEMENT_*_KEY）仍归公告域所在的 web.app。
ANNOUNCEMENT_DRAFT_META_SEP = "|"
ANNOUNCEMENT_DRAFT_META_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_announcement_meta(raw):
    """解析公告元数据 `<小写邮箱>|YYYY-MM-DD HH:MM:SS` → `(作者, 时刻)`。

    草稿与线上公告共用这一形态，故解析器只有一个。任何不符都返回 `("", "")`
    （"元数据不可用"）而不是抛错：该键可被手工编辑，也可能是半成品写入，而它只是
    公告 GET 的附带信息，不值当把这条**匿名也要读**的接口拖崩。
    """
    parts = str(raw or "").split(ANNOUNCEMENT_DRAFT_META_SEP)
    if len(parts) != 2:
        return "", ""
    author, when = parts[0].strip(), parts[1].strip()
    if not author or not when:
        return "", ""
    try:
        datetime.strptime(when, ANNOUNCEMENT_DRAFT_META_FMT)
    except ValueError:
        return "", ""
    return author, when


# ---------------------------------------------------------------------------
# 启动期：.env 行模型歧义键报告
# ---------------------------------------------------------------------------
def _report_env_key_collisions(env_path, send_notification):
    """报告 .env 的行模型歧义键（潜伏行分隔符 / 影子重复行）。只检测不改写。

    刻意先于启动流程里一切 .env 写入（口令迁移 / 落盐 / ensure_secret_key）：这些写入的
    读-改-写会把潜伏载荷实体化——升级后第一次重启本身就是一个实体化器，报告必须赶在它
    前面，运维才能据此判断是否在升级前被打。不做静默自动改写：归一化会连带改动其他键的
    存量值；清理动作 = ERROR 日志 + 一次 urgent 告警点名键与清理方法。

    告警出口由调用方传入 web.app 的 `send_notification`（它带着既有的节流与账本语义）；
    "每进程只报一次"的闩由调用方持有。
    """
    hits = _env_io.find_env_key_collisions(env_path)
    if not hits:
        return
    keys = ", ".join(sorted(hits))
    logger.error(
        "%s 检测到行模型歧义配置键（值内潜伏行分隔符或同名键多行，"
        "解析器按后写覆盖先写取值）：%s。请备份后手工把每个键清理为唯一一行；"
        "本次启动只检测不改写",
        env_path, keys,
    )
    send_notification(
        ".env 配置歧义告警",
        mail_layout.Mail(
            summary=f"{env_path} 检测到行模型歧义配置键。",
            fields=[("受影响键", keys),
                    ("成因", "值内藏行分隔符（U+2028 等，任何一次读-改-写都会实体化成新配置行）"
                             "或同名键多行（含带空格 `KEY = v` 写法），"
                             "解析器按后写覆盖先写取值，生效值不可信")],
            advice=["备份后手工编辑 .env，把列出的每个键清理为唯一一行",
                    "本检测不会自动改写文件"],
            level="urgent",
        ),
        urgent=True,
    )
