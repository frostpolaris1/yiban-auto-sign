# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`.env` 读写与设置项展示：写锁、注入校验、原子落盘与公告元数据解析。

**功能**
`.env` 的全部读写入口：宽松解析 `read_env`、整数配置 `load_env_int`、键值写入
`write_env_key` / `write_env_int` 与批量原子写 `write_env_batch`、首次启动的
`YIBAN_SECRET_KEY` 生成 `ensure_secret_key`、写互斥 `_env_write_lock`、写拒绝的统一
409 响应 `env_write_refused_response`（附 `env_refused_problems` 定位载荷：问题行号/
键名 + 脱敏片段，值一律隐去）与歧义行清理 `cleanup_env_ambiguous_line`
（只吃确含行分隔符的物理行，不是 .env 编辑器）；外加设置项展示族
（`_settings_label` / `_settings_value_text` / `_settings_effective_values`）、代理地址形状
校验 `_is_http_proxy_url`、启动期的歧义键报告 `_report_env_key_collisions` 与公告元数据
解析 `_parse_announcement_meta`；正态 μ/σ 区间四键的映射表 `SCHEDULE_DIST_ENV_KEYS` /
`SCHEDULE_DIST_DEFAULTS` / `SCHEDULE_DIST_KEYS`（A 档键名单源，读写两侧与前端对拍共用）；
以及**键名清单**——设置项档位（`MASTER_ONLY_KEYS` / `GATED_KEYS` / `GLOBAL_PAUSE_KEY`）
与公告四键（`ANNOUNCEMENT_KEY` / `ANNOUNCEMENT_DRAFT_KEY` /
`ANNOUNCEMENT_DRAFT_META_KEY` / `ANNOUNCEMENT_PUBLISHED_META_KEY`）。
键名与键的读写同处一模块：改名或增键时只有一处要动。

**归属**
原 `web/app.py` 的模块级 env 辅助，唯一真源在本模块。批 3-d 删除 `web/app.py` 的
转发包装：路由与测试直取本模块。落盘用的 `_atomic_write`、写拒绝审计回调、开关解析器
`_env_flag` 与告警出口 `send_notification` 经 `configure(...)` 登记进 `_hooks`，
按调用时刻解析。设置项缺省值直接取自 `web/services/limits.py`。

**复用**
批量写是单键写与整数写的底层（`write_env_key` / `write_env_int` 都折成
`write_env_batch({key: value})`），行分隔符注入校验、旧行折叠与原子替换因此只有一份；
公告草稿与线上公告共用同一份元数据形态，故解析器与它的两个格式常量同在一处。
正态 μ/σ 四键的缺省值直接取 `yiban.engine.schedule` 的 `_DEFAULT_*`（引擎是唯一真源，
本模块不另抄数字——抄一份迟早分叉，缺省一变设置页回显就骗人）。

**通信**
本模块不反向导入 `web.app`（本仓测试以 `spec_from_file_location` 别名加载 `app.py`，
普通 import 会再执行一份副本模块）。落盘动作、审计回调与开关判定经 `configure(...)`
登记，按调用时刻解析：测试打桩 `_atomic_write` / `write_env_batch` / `_env_flag` 仍然
生效。未登记即 `RuntimeError`（fail-closed）。跨进程写锁沿用 `yiban.infra.env_lock`
的真源，不自建第二套。
"""

import contextlib
import logging
import os
import re
import secrets
from datetime import datetime

from flask import jsonify

from web.services.limits import (
    DEFAULT_ACCOUNT_GAP_MAX,
    DEFAULT_MAX_ACCOUNTS,
    DEFAULT_MAX_USERS,
)
from yiban.adapters.config import env_io as _env_io
from yiban.adapters.notifier.mail import layout as mail_layout
from yiban.core import window as yb_window
from yiban.engine import schedule as _schedule
from yiban.infra import env_lock

# 与 web.app 同名的日志通道：.env 读写与启动检测的告警落回既有通道，便于运维沿用同一处过滤
logger = logging.getLogger("web")

# ---------------------------------------------------------------------------
# 宿主注入登记点（批 3-d）
# ---------------------------------------------------------------------------
# 本模块原由 `web/app.py` 的转发包装在调用时刻现取宿主名字后注入。批 3-d 删除那层
# 包装，改由宿主在 import 末尾登记 lambda；本模块按调用时刻从 `_hooks` 取。
# 登记 lambda 而不是值：测试打桩 `_atomic_write` / `write_env_batch` / `_env_flag`
# 必须在调用时刻生效。未登记即 fail-closed（抛 `RuntimeError`）。
_hooks = {}


def configure(atomic_write=None, env_write_refuse_audit=None, env_flag=None):
    """注入宿主侧依赖（登记 lambda，见上方说明）。可重复调用（测试用）。

    只覆盖显式传入的键：未传的保持上一次登记。
    """
    if atomic_write is not None:
        _hooks["atomic_write"] = atomic_write
    if env_write_refuse_audit is not None:
        _hooks["env_write_refuse_audit"] = env_write_refuse_audit
    if env_flag is not None:
        _hooks["env_flag"] = env_flag


def _host(key):
    """取宿主登记的依赖。未登记即 fail-closed。

    **`try` 只包住取键，不包住调用**：宿主 lambda 内部抛的 `KeyError` 是真实业务
    错误，必须原样传出。
    """
    try:
        fn = _hooks[key]
    except KeyError:
        raise RuntimeError(
            "web.services.env_io 尚未登记宿主依赖 %r：请确认 web/app.py 的 import 末尾"
            "调用了 web.services.env_io.configure(...)" % (key,)
        ) from None
    return fn()


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


def effective_env(env_path):
    """**生效配置层**：进程环境为底、`.env` 覆盖（M27 全仓口径，唯一实现）。

    为什么需要它：`.env` 与进程环境是两层真值域，而仓库口径只承认一套——
    `.env` 优先、进程环境只补缺（`docker-compose.yml` 的 M27 注释）。本包此前
    有 `.env`-only（`load_env_int` / `read_env`）与 overlay（`capacity` /
    `signstatus` 由调用方注入 overlay）两种读法：同一个"这次判定看哪份值"的概念
    两套实现，键只配在进程环境（systemd `Environment=`、`docker run -e`）时
    容量/口令门禁旋钮会静默读成缺省值。
    `.env` 里写了空值仍按"未配置"处理，让进程环境补缺（与 notify 侧 `_env_str` 同语义）。
    """
    env = {k: v for k, v in os.environ.items() if k.startswith("YIBAN_")}
    for key, value in read_env(env_path).items():
        if str(value or "").strip():
            env[key] = value
    return env


def load_env_int(env_path, key, default, overlay=True):
    """读取整数配置，缺失/非法回退默认值。

    `overlay=True`（缺省）按 `effective_env` 的生效配置层取值：`.env` 优先、
    进程环境补缺（M27）。容量上限、口令门禁 TTL/冷却、暂停位这类键在"只配进程
    环境"的部署里因此不再静默读成缺省值。需要严格只读 `.env` 的调用面显式传
    `overlay=False`。
    """
    try:
        source = effective_env(env_path) if overlay else read_env(env_path)
        return max(0, int(source.get(key, "")))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 设置项展示（键的中文名 / 值的展示形态 / A/B 档生效值）
# ---------------------------------------------------------------------------
# A/B 档键的中文标签：400 提示、口令门禁留痕与变更明细（审计正文）共用一份，
# 避免同一件事在多处各写一套字面量（本表**不进日志**——全仓没有 logger 消费它）
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
    "health_report_time": "健康报告发送时刻",
    "health_report_weekday": "健康报告发送星期",
    "sign_order": "签到排序",
    "sign_dist": "签到分布",
    "sign_mode": "签到模式",
    "allow_time_pref": "自选时间片",
    "mu_min_pct": "正态峰值下限",
    "mu_max_pct": "正态峰值上限",
    "sigma_min_pct": "正态宽度下限",
    "sigma_max_pct": "正态宽度上限",
}

# 正态 μ/σ 区间四键（A 档：仅主管理员可改）：API/前端字段名 → `.env` 键名。
# 字段名刻意取引擎 cfg 里的短名（`mu_min_pct` 等），读写两侧与 `_schedule_config`
# 的键一一对得上，不必另立一套命名。缺省值单源在 `yiban.engine.schedule` 的
# `_DEFAULT_*`（本模块不另抄一份数字：抄一份迟早与引擎分叉，缺省一变前端回显就骗人）。
SCHEDULE_DIST_ENV_KEYS = {
    "mu_min_pct": "YIBAN_SCHEDULE_MU_MIN_PCT",
    "mu_max_pct": "YIBAN_SCHEDULE_MU_MAX_PCT",
    "sigma_min_pct": "YIBAN_SCHEDULE_SIGMA_MIN_PCT",
    "sigma_max_pct": "YIBAN_SCHEDULE_SIGMA_MAX_PCT",
}
SCHEDULE_DIST_DEFAULTS = {
    "mu_min_pct": _schedule._DEFAULT_MU_MIN_PCT,
    "mu_max_pct": _schedule._DEFAULT_MU_MAX_PCT,
    "sigma_min_pct": _schedule._DEFAULT_SIGMA_MIN_PCT,
    "sigma_max_pct": _schedule._DEFAULT_SIGMA_MAX_PCT,
}
#: 四键的字段名集合（A 档清单；`settings_api` 的 403/口令门禁与前端对拍测试共用一份）。
SCHEDULE_DIST_KEYS = frozenset(SCHEDULE_DIST_ENV_KEYS)

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
#
# 逐键复核（2026-09-28 用户判据：**在不当时间——窗口前几秒或窗口内——改它，能不能
# 静悄悄把签到序列搞炸？能，就留口令门**；不能的仅剩"只增摩擦"的键）：
#   留门（能静默搞炸，或属安全网本身）：
#     sign_window / window_edge_sec / edge_front_sec / edge_back_sec
#       ——窗口改窄或首尾裁切吃光有效窗口 ⇒ 当天整批账号静默跳过；
#     sunday_sign / saturday_sign —— 当天整天空签且不报错（面板会显示休息，但没人盯着看）；
#     start_delay_max / gap_max —— 延迟或间隔 × 账号数超出窗口 ⇒ 后段账号排队到最后全落空；
#     account_verify / registration_pause —— 关验证/重开注册 ⇒ 未验证的批量账号涌入主链
#       对易班发起真实登录 ⇒ 平台风控封号（§5.2 明列的用户面合规事故）；
#     probe_enable / probe_time / probe_interval —— 探针是发现账号异常的眼睛，改坏即
#       静默失去监测（与 SMTP 条目同属"安全网本身"，故同判据留门）；
#     global_pause 0→1 —— 全站停签（见下）。
#   仅摩擦、按判据可以放开（本次刻意不动：这三个键半年改一次，放开要连前端面板的收控件
#   一起改，收益不抵新增面；若日后摩擦真的咬人再动）：
#     max_users / max_accounts —— 只影响**新提交**的配额判定（有明确报错），
#       不触碰已排定的签到序列，改小也不会让在册账号掉队。
MASTER_ONLY_KEYS = frozenset({
    "sign_window", "window_edge_sec", "edge_front_sec", "edge_back_sec",
    "sunday_sign", "saturday_sign", "registration_pause",
    "start_delay_max", "gap_max", "max_users", "max_accounts",
    "account_verify", "probe_enable", "probe_time", "probe_interval",
    # 健康报告发送时刻/星期：与探针同属"安全网本身"——把时刻改到没人看的钟点、
    # 或把星期改到别的一天，报告照发但看不见，静默失去周期性证据（同判据留门）。
    "health_report_time", "health_report_weekday",
})
GATED_KEYS = frozenset({"sign_order", "sign_dist", "sign_mode", "allow_time_pref"})
# `global_pause` 刻意不进 `MASTER_ONLY_KEYS`：它是 A 档的唯一例外，权限按**变更方向**
# 分流（0→1 急停人人可做、1→0 恢复仅主管理员），故单独用这个键名判方向。
GLOBAL_PAUSE_KEY = "global_pause"

# 这些键的生效值是 0/1 开关：写进审计与告警正文时翻成中文，免得运维盯着 "0"→"1" 心算
_BOOL_SETTINGS_KEYS = frozenset({
    "sunday_sign", "saturday_sign", "global_pause", "registration_pause",
    "account_verify", "probe_enable", "allow_time_pref",
})


#: 报告发送星期的展示名（下标 = `YIBAN_HEALTH_REPORT_WEEKDAY` 的 0=周一…6=周日）。
#: 给变更明细用：运维在审计正文里看 "0→2" 还要心算，看 "周一→周三" 不用。
_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _settings_label(key):
    """设置键的中文名（未列入标签表的按键名原样回，绝不编一个名字）。"""
    return _SETTINGS_KEY_LABELS.get(key, key)


#: 空串对这两个键是**有意义的值**（= 回到未配置），不是"没有值"：审计正文里要写成人话，
#: 否则变更明细会出现"健康报告发送时刻=09:00→"这种半句话。
_UNSET_IS_MEANINGFUL_KEYS = frozenset({"health_report_time", "health_report_weekday"})


def _settings_value_text(key, value):
    """设置值写进变更明细时的展示形态（值本身已在现读侧归一，不含敏感串）。

    两处消费点（`settings_api` 的变更明细与口令门禁留痕，都经 `_change_text`），两处都只进
    审计正文（设置变更不外发告警）。
    """
    if key in _BOOL_SETTINGS_KEYS:
        return "开" if str(value) == "1" else "关"
    if key in _UNSET_IS_MEANINGFUL_KEYS and not str(value).strip():
        return "未配置"
    if key == "health_report_weekday":
        return _weekday_text(value)
    return str(value)


def _weekday_text(value):
    """星期取值的中文名；越界/非法原样回（不编一个不存在的星期）。"""
    try:
        idx = int(str(value))
    except (TypeError, ValueError):
        return str(value)
    return _WEEKDAY_NAMES[idx] if 0 <= idx < len(_WEEKDAY_NAMES) else str(value)


def health_report_time_str(env):
    """报告发送时刻的**归一取值**（`"HH:MM"`）；未配置或写坏返回 `""`（= 未配置）。

    刻意归一而不是原样回显：生效判据走 `yiban.window.parse_hhmm`，它同时收 `9:00`
    这种不补零写法。原样回显时前端的时间形状校验（`^\\d{2}:\\d{2}$`）会把它判成非法值
    ⇒ 界面显示"未配置"（开关关）而报告其实已按 09:00 锚定——显示值/生效值两段不等，
    正是时间类设置要消灭的病。
    """
    raw = str(env.get("YIBAN_HEALTH_REPORT_TIME", "")).strip()
    hhmm = yb_window.parse_hhmm(raw, None)
    return "" if hhmm is None else "%02d:%02d" % hhmm


def health_report_weekday_setting(env):
    """报告发送星期的**配置值**（归一为 `"0"~"6"`，0=周一…6=周日）；未配置、越界或
    解不出数字时返回空串 = "本键没配"。

    解析口径与运行期**同一处**：都先按 `int()` 归一（`"00"`→`0`、`"+3"`→`3`），不做
    更严的形状校验——更严的判据会让 `.env` 里手写的 `YIBAN_HEALTH_REPORT_WEEKDAY=00`
    出现"运行期按周一发、界面却显示未配置"的分叉。越界值（`9`）两侧都落到周一这个缺省上，
    故等价于未配置。`int()` 同时挡掉上标数字一类 `isdigit()` 认、`int()` 不认的写法。

    刻意与"运行期生效值"分开：运行期有缺省（未配置 ⇒ 周一，见
    `web.app._health_report_cfg()` 的回落），而界面必须能分辨"用户配了周一"与"没配"——
    分辨不出时"关掉固定发送"这一步就删不掉残留的星期键，报告会静默改到那一天
    （面板显示"已关闭"而实际按残留星期发）。`_settings_effective_values` 也取本函数的
    **配置值**（不补缺省）：那是"这次请求改没改配置"的判据，必须与 GET 回显同一口径，
    补了缺省就会让"提交一个等于回显的值"被判成没改、键永不落盘（0=周一…6=周日 与
    `web/app.py` 的 `_health_report_cfg()` 同一口径）。
    """
    raw = str(env.get("YIBAN_HEALTH_REPORT_WEEKDAY", "")).strip()
    try:
        idx = int(raw)
    except (TypeError, ValueError):
        return ""
    return str(idx) if 0 <= idx <= 6 else ""


def _settings_effective_values(env_file):
    """A/B 档设置项的**当前生效值**（归一为字符串），取值口径与 `GET /api/settings` 一致。

    只用于"这次请求到底改没改配置"的判定：一律现读现算，绝不信请求自带的旧值——
    否则把当前值原样抄进请求就能自称"无变更"，口令复核与变更告警双双被绕开
    （系统开关门原本就是这个语义，这里把同一语义铺满全部 A/B 档键）。

    取值口径与 `GET /api/settings` 的回显**逐键**同源：回显怎么归一，这里就怎么归一——
    两边不一致时"提交一个等于回显的值"会被判成改过（多要一次口令）或没改过（设置永不落盘，
    而每次打开设置页都显示一条清不掉的脏标记）。

    开关解析器从宿主取（`_env_flag` 是既有打桩点）；三个容量缺省值直接取自
    `web/services/limits.py`（单一事实源）。
    """
    env_flag = _host("env_flag")
    env = read_env(env_file)
    mode = env.get("YIBAN_SIGN_MODE", "").strip().lower()
    w_start, w_end, _invalid = yb_window.parse_window(env)
    front, back = yb_window.parse_edges(env)

    def _flag(key):
        return "1" if env_flag(env.get(key, "")) else "0"

    vals = {
        "start_delay_max": str(load_env_int(env_file, "YIBAN_START_DELAY_MAX", 0)),
        "gap_max": str(load_env_int(env_file, "YIBAN_ACCOUNT_GAP_MAX",
                                    DEFAULT_ACCOUNT_GAP_MAX)),
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
        # 两个暂停位与周末位同口径（`_env_flag` = 引擎/run.sh 同一套真值判定）：原先用
        # `load_env_int(...) == 1`，`=true/on/yes` 会被读成 0，与 GET /api/settings 及引擎
        # 分叉（census P0-1）。整改后两处读者与引擎同源，`_flag` 认 1/true/on/yes。
        "global_pause": _flag("YIBAN_GLOBAL_PAUSE"),
        "registration_pause": _flag("YIBAN_REGISTRATION_PAUSE"),
        "allow_time_pref": str(load_env_int(env_file, "YIBAN_ALLOW_TIME_PREF", 0)),
        "sign_mode": mode,
        # 排序/分布的生效值由旧模式派生（与 GET 同一式子）：只存 YIBAN_SIGN_MODE 的
        # 存量配置，其真实排序就是派生值，拿空串比会把"没改"误判成"改了"
        "sign_order": env.get("YIBAN_SIGN_ORDER", "").strip().lower() or (
            "random" if mode == "random" else "sequence"),
        "sign_dist": env.get("YIBAN_SIGN_DIST", "").strip().lower() or (
            "normal" if mode == "normal" else "front"),
        "account_verify": _flag("YIBAN_ACCOUNT_VERIFY"),
        "probe_enable": _flag("YIBAN_PROBE_ENABLE"),
        "probe_time": env.get("YIBAN_PROBE_TIME", "20:00").strip() or "20:00",
        "probe_interval": env.get("YIBAN_PROBE_INTERVAL_DAYS", "1").strip() or "1",
        # 告警通道健康报告：时刻缺省是空串（未配置 = 沿用"唤醒即发"，不是某个钟点），
        # 故不能用 `or "09:00"` 那种"补一个默认钟点"的写法——那会把"没配"显示成"配了"。
        "health_report_time": health_report_time_str(env),
        # 本键的"现值"取**配置值**（未配置 = 空串），不补缺省周一：本函数的用途是
        # "这次请求到底改没改配置"，而回显走的是同一个取值口径（配置值）。补了缺省就不是
        # 同一口径——提交"周一"会被判成"没改"、键永不落盘，而回显仍是空串 ⇒ 每次打开
        # 设置页都会显示一条永远清不掉的"未保存的修改"。生效缺省（未配置 ⇒ 周一）由
        # 运行期判定 `web.app._health_report_cfg()` 承担。
        "health_report_weekday": health_report_weekday_setting(env),
        "max_users": str(load_env_int(env_file, "YIBAN_MAX_USERS", DEFAULT_MAX_USERS)),
        "max_accounts": str(load_env_int(env_file, "YIBAN_MAX_ACCOUNTS",
                                         DEFAULT_MAX_ACCOUNTS)),
    }
    # 正态 μ/σ 区间四键：取值口径（现读 + 引擎缺省）与 `GET /api/settings` 的回显
    # 必须逐字一致——否则提交一个等于回显的值会被判成"没改"，A 档口令门禁被绕过。
    for _field, _env_key in SCHEDULE_DIST_ENV_KEYS.items():
        vals[_field] = str(load_env_int(env_file, _env_key, SCHEDULE_DIST_DEFAULTS[_field]))
    return vals


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


def write_env_int(env_path, key, value):
    """把整数配置写入 .env：value<=0 删除该行，>0 写入；保留其他行。"""
    write_env_key(env_path, key, str(value) if value > 0 else "")


def write_env_key(env_path, key, value):
    """把任意键值写入 .env：value 为空删除该行，否则写入；保留注释与其他行。

    单键形态 = write_env_batch({key: value})：行分隔符注入校验、写锁、原子替换
    均单源在 write_env_batch（防两份安全校验实现漂移）。本函数按模块名调用它，
    故测试打桩 `web.services.env_io.write_env_batch` 仍是"每一次 .env 落盘"的观测点。
    """
    write_env_batch(env_path, {key: value})


def write_env_batch(env_path, updates):
    """批量写入多个键值（原子操作）：读取一次，修改多个键，写入一次。
    避免多次独立写入时进程崩溃导致配置不一致。
    updates: dict {key: value}，value 为空字符串则删除该键。

    行模型、键/值校验、写入前后"键集合 diff"、跨进程写锁**全部单源在**
    `yiban.infra.env_io.write_env_keys`（web 与引擎共用一个实现，两侧拒绝文案同源）：
    本函数只负责 web 侧特有的落盘注入（`atomic_write`）。外层锁已就此去重——写锁
    下沉进 `write_env_keys` 后，这里再包一层是对同一路径的纯冗余嵌套；只在本函数
    之外还有"判定读取"要与之同临界区的调用方（改密、保存设置、执行体读-改-写）才
    需要继续自持 `_env_write_lock`。审计回调从宿主取：
    写入被拒（潜伏分隔符/未请求的键变化）时必须留痕。

    为什么不再自己用宽行模型读-改-写：宽行模型会把注释里潜伏的
    U+0085/U+2028 等先拆成两行、再把后半截实体化成真配置行（一次无关保存即可注入
    `YIBAN_GLOBAL_PAUSE=1`）。单一行模型同时是"校验行模型 = 写入行模型"的前提。
    """
    atomic_write = _host("atomic_write")
    audit = _host("env_write_refuse_audit")
    _env_io.write_env_keys(
        env_path, updates,
        write_text=lambda path, text: atomic_write(path, text, chmod_priv=True),
        audit=audit, delete_empty=True)


# 写拒绝的统一响应文案（web 全部 .env 写点共用）。此前只有 `/api/settings` 把拒绝
# 映射成 409+清理指引；公告 / 告警通道 / 改密 / 执行体等写点抛裸 `EnvWriteRefused`
# 后被兜底成 500（无指引）。现由 `web.app.create_app` 注册的 Flask errorhandler 与
# 各路由的显式 catch 共回这一份——写点不再各自编文案。
ENV_WRITE_REFUSED_MESSAGE = (
    "配置写入被拒绝：.env 存在行模型歧义（潜伏行分隔符或未请求的键变化），"
    "请按启动告警提示人工清理该行后重试"
)


# ---- 409 可操作化（写拒绝定位）----
# 问题片段的展示上限；片段经脱敏原语链，绝不回显值原文。
_ENV_SNIPPET_MAX = 160


def _env_problem_snippet(env_path, line_no):
    """取问题行的**脱敏片段**（绝不回显值原文）。

    原语链与日志出口同一组（`escape_line_breaks` 让潜伏分隔符可见 → `sanitize_text`
    → `mask_phones_in_text`），再对任何 `KEY=值` 形态做值隐去——行号与键名已足够
    定位，值本身不需要也不应该出现在管理界面。读不到文件/行号越界返回 None
    （409 的其余部分照常可用，定位载荷尽力而为）。
    """
    from yiban.core.masking import mask_phones_in_text, sanitize_text
    try:
        raw = _env_io._read_env_text(env_path)
        lines = _env_io.split_env_lines(raw)
        if not 1 <= line_no <= len(lines):
            return None
        snippet = _env_io.escape_line_breaks(lines[line_no - 1])
        snippet = mask_phones_in_text(sanitize_text(snippet))
        # 值隐去：任何 KEY=值 形态只留键名与等号（键名是定位信息，值不是）
        snippet = re.sub(r"([A-Za-z_][A-Za-z0-9_]{1,}\s*=)\S*",
                         r"\1***", snippet)
        snippet = snippet[:_ENV_SNIPPET_MAX]
        return snippet or None
    except Exception:
        # 定位载荷尽力而为：读失败/解码失败不改变"写入已被拒绝"这一事实
        return None


def env_refused_problems(exc, env_path):
    """从 `EnvWriteRefused` 组装 409 的定位载荷（problems 列表）。

    `line`（潜伏分隔符行）→ {"kind": "line", "line": N, "snippet": 脱敏片段}；
    `keys`（未请求键变化）→ 每键一条 {"kind": "key", "key": 名}。两枚属性只在
    新写入路径上有值；旧消息串不含定位信息时返回空列表（响应退化为纯指引）。
    """
    problems = []
    line_no = getattr(exc, "line", None)
    if line_no:
        problems.append({"kind": "line", "line": line_no,
                         "snippet": _env_problem_snippet(env_path, line_no)})
    for key in (getattr(exc, "keys", None) or []):
        problems.append({"kind": "key", "key": key})
    return problems


def env_write_refused_response(exc=None, env_path=None):
    """写拒绝的 409 响应（含清理指引 + 定位载荷）——web 各 .env 写点的唯一出口。

    `exc`/`env_path` 提供时附带 `problems`（问题行号/键名 + 脱敏片段，绝不回显
    值原文）；缺省调用保持旧形态（纯指引），兼容不留痕的测试构造。
    """
    body = {"error": ENV_WRITE_REFUSED_MESSAGE, "reason": "env_write_refused"}
    if exc is not None and env_path:
        body["problems"] = env_refused_problems(exc, env_path)
    return jsonify(body), 409


def cleanup_env_ambiguous_line(env_path, line_no, audit=None):
    """移除 `.env` 中**含潜伏行分隔符**的那一行（一键清理）。

    安全面收窄：只允许移除经 `has_line_break` 判定确含行分隔符的物理行——
    那是行模型歧义的唯一现场；普通配置行/注释行一律拒绝（本端点不是 .env
    编辑器，不存在借道删配置的口子）。写路径与写入口同一套纪律：跨进程写锁、
    写前字节快照、失败按原字节回滚、审计留痕（片段走同一脱敏原语链）。

    **落盘必须走 `_atomic_write`**（本包声明的"每一次 .env 落盘"唯一实现）：
    `open(env_path, "w")` 先截断成 0 字节，并发读者（run.sh 逐行 export、
    引擎子进程、其他线程的 `read_env`）在这个窗口里会拿到空/半份 `.env`——
    读到空配置不会报错，只会按默认值行事。原子写让读者只见旧内容或新内容，
    失败时目标文件保持旧内容，故不再需要就地把原字节写回（那会二次制造窗口）。
    返回 (ok, message, remaining)：ok=False 时 message 说明为什么拒绝。
    """
    with env_lock.env_write_lock(env_path):
        lines = _env_io.split_env_lines(_env_io._read_env_text(env_path))
        if not 1 <= line_no <= len(lines):
            return False, f"行号越界（文件共 {len(lines)} 行）", None
        target = lines[line_no - 1]
        if not _env_io.has_line_break(target):
            return False, "该行不含行分隔符，无需清理（本端点只处理行模型歧义行）", None
        snippet = _env_problem_snippet(env_path, line_no) or "(片段不可得)"
        remaining = lines[:line_no - 1] + lines[line_no:]
        new_text = "\n".join(remaining) + ("\n" if remaining else "")
        still_bad = [i + 1 for i, ln in enumerate(remaining)
                     if _env_io.has_line_break(ln)]
        try:
            _host("atomic_write")(env_path, new_text, chmod_priv=True)
        except (OSError, ValueError) as e:
            # 原子写失败时目标文件仍是旧内容（临时文件由 _atomic_write 清理），
            # 不存在"半份 .env"需要回滚；如实报错让运维处置。
            return False, f"清理写入失败（原文件未改动）：{e}", None
        if audit is not None:
            with contextlib.suppress(Exception):
                audit("env_line_cleanup", f"移除第 {line_no} 行（含潜伏行分隔符），"
                                          f"片段：{snippet}；剩余歧义行 {len(still_bad)}")
        return True, f"已移除第 {line_no} 行；剩余含分隔符行 {len(still_bad)} 行", len(still_bad)


def ensure_secret_key(env_path):
    """确保 .env 中存在 YIBAN_SECRET_KEY（缺失时自动生成随机值）。

    .env 不可写、或既有行含潜伏行分隔符（写入被 fail-closed 拒绝）时降级为进程内随机
    密钥并告警（服务可用，重启后会话失效）——与口令哈希迁移的降级策略一致：宁可告警后
    带病运行，也不让启动直接失败。行模型/校验单源在 `yiban.infra.env_io.write_env_keys`。

    判定"已有密钥/全新部署"走**严格读**（`strict=True`，与 account_crypto 建钥支、
    audit_chain 审计密钥支同口径）：宽松读把"文件存在但读不到"（权限/占用/upsert 截断
    与编辑器 unlink-新建造成的空残缺窗口）吞成空 dict，误判未配置就生成新钥落盘——
    旧钥被同键折叠悄悄顶掉、`REGISTRATION_PAUSE=1` 把注册静默关闭，正是"读失败误判
    未配置会静默生成新钥覆盖旧钥，宁可启动失败"在本仓写侧的翻版。严格读到 OSError 时
    **一次都不写**：无法确认旧钥是否存在就不能生成新钥，降级为进程内随机密钥并告警。
    判定与取值**共用这一次读取**（不再 os.path.exists + 宽松读两次不同源——两次之间
    文件可以换内容，判"全新"与写"新钥"就会各看一版）。
    """
    with _env_write_lock(env_path):
        try:
            env = _env_io.parse_env_file(env_path, strict=True)
        except OSError as e:
            logger.warning(
                "无法读取 %s（%s）：无法确认既有 YIBAN_SECRET_KEY，拒绝生成新钥落盘"
                "（换钥会静默顶掉旧钥并可能翻转注册开关）；仅本次进程使用随机密钥，"
                "重启后会话将失效，请检查文件权限后重启",
                env_path, e,
            )
            return secrets.token_hex(32)
        # 全新部署判定与取值同源于上面这一次严格读——文件不存在/空/仅注释（无任何
        # 有效键）= 首次初始化，默认写入「暂停注册」；既有部署不写此键，注册行为
        # 保持不变（用户裁决：默认允许，新部署才默认暂停）。touch 空 .env / 复制
        # .env.example 后无有效键仍视为全新部署。
        new_deployment = not env
        key = env.get("YIBAN_SECRET_KEY", "").strip()
        if key:
            return key
        key = secrets.token_hex(32)
        # 常量字面量写入（暂停键），无注入面；管理员完成初始配置后在设置页开启注册。
        # 同键旧行折叠由 write_env_keys 的 key_line_pattern 承担（影子行判据：同名键
        # 占多行时解析器按后写覆盖先写，生效值由落盘顺序决定）——`YIBAN_SECRET_KEY = `
        # 带空白写法、以及万一残留的旧 `YIBAN_REGISTRATION_PAUSE=0` 行都会被折成唯一
        # 一行，不会与本次新行并存成 =1/=0 双行。
        updates = {"YIBAN_SECRET_KEY": key}
        if new_deployment:
            updates["YIBAN_REGISTRATION_PAUSE"] = "1"
        try:
            atomic_write = _host("atomic_write")
            audit = _host("env_write_refuse_audit")
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
# 公告：三个 .env 键的键名单源 ＋ 元数据形态（草稿与线上公告共用）
# ---------------------------------------------------------------------------
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
# 该形态存在 .env 的公告键里，故解析器与它的分隔符/时刻格式也归本模块。
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
        datetime.strptime(when, ANNOUNCEMENT_DRAFT_META_FMT)  # noqa: DTZ007 仅校验业务时间串格式，不对时刻做运算
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
