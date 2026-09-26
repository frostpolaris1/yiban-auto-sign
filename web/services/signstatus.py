# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""签到窗口与运行时段判定、系统信息（状态文案与连通性检测）。

**功能**
`.env` 开关值解析 `_env_flag`；签到窗口解析 `_sign_window`；"现在是否在窗口内"的纯钟点
判定 `_in_sign_window` 与含周末门/暂停门的 `_in_run_period`（门原因 `_day_off_reason` 读
调用方现取的 `.env` 真值，配套日历页显示载荷 `day_off_payload` / `DAY_OFF_TEXT`）；
系统信息族的 `sign_status`（状态文案与配色，钟点取有效窗口端点，附分钟格式化 `_hm`）
与 `check_connectivity`（不登录的可达性探测）。

**归属**
原 `web/app.py` 的模块级窗口与系统信息辅助，唯一真源在本模块；`web/app.py` 只保留名字面与
转发，把它自己持有、而本模块需要的模块级名字——`.env` 路径 `ENV_FILE`、整数配置读取器
`load_env_int`、窗口解析器 `_sign_window`、有效窗口视图 `sign_window_bounds`、钟点判定
`_in_sign_window`——在调用时刻现取后注入。

**复用**
窗口解析委托 `yiban.window.parse_window`、有效窗口委托 `yiban.window.bounds`、运行时段委托
`yiban.engine.schedule.day_off`——与引擎（signin）同一份口径，避免"网页显示 07:50、引擎按
别的值判定"这类同概念两套实现；`_in_run_period` 复用 `_in_sign_window` 的钟点判定再叠门，
不另写一套窗口逻辑。

**通信**
本模块不反向导入 `web.app`（本仓测试以别名加载 `app.py`，普通 import 会再执行一份副本
模块）。`.env` 路径、读取器与窗口/钟点判定都作为显式参数接收：它们在 `web.app` 上是会被
测试改写、也会随 `--config` 与运行方式变化的模块级名字（既有测试在 `web.app` 上打桩
`_sign_window` / `edge_config` / `_in_sign_window` 来固定窗口与时段，窗口打桩经
`sign_window_bounds` 现取后穿透到本模块），本模块另持一份绑定会让这些改写静默失效。
"""

import logging
import os

import requests

from yiban import clock
from yiban import window as yb_window
from yiban.engine import schedule as yb_schedule
from yiban.fyiban.protocol import API_AUTH_URL

# 与 web.app 同名的日志通道：本族的告警落回既有通道，便于运维沿用同一处过滤
logger = logging.getLogger("web")

#: 开关类 .env 值的真值字面量（与 run.sh 的 `_is_truthy` 同一套写法）
_TRUTHY_LITERALS = ("1", "true", "on", "yes")


def _env_flag(value):
    """把 `.env` 里的开关值解析成布尔（认 1/true/on/yes，大小写不敏感）。"""
    # 只认 _TRUTHY_LITERALS 那四个字面量，其余（0/false/空/未设/写错的词）一律按**关**：
    # 兜底常驻要占一份出口和一个常驻进程，"开"必须是明确表达出来的意图
    return str(value if value is not None else "").strip().lower() in _TRUTHY_LITERALS


def _sign_window(env_file, read_env):
    """签到窗口（`.env` 覆盖 YIBAN_SIGN_START/END，非法回退默认）。

    与引擎同一份口径（委托 `yiban.window.parse_window`）的原因见模块头「复用」。
    参数注入口径见模块头「通信」。
    """
    start, end, _invalid = yb_window.parse_window(read_env(env_file))
    return start, end


def _in_sign_window(bounds, now=None):
    """当前是否落在**有效**签到窗口内（已扣掐头去尾）——纯钟点口径。

    判定用引擎同一份 `yiban.window.bounds` 给出的边界，这里只把"现在"换算成
    当天分钟数再比区间，不另写一套窗口逻辑。**"窗口内不做实测"的 409 拦截用它**
    （只关心"会不会跟签到抢资源"）；执行体接口的 `in_window` 用下面的
    `_in_run_period`（还含周末门/暂停门，见其文档）。
    """
    now = now or clock.now()
    now_min = now.hour * 60 + now.minute + now.second / 60.0
    return bounds.lo_min <= now_min <= bounds.hi_min


def _day_off_reason(file_env, now=None):
    """今天此刻是否被周末门/一键暂停挡下 → 原因串；空串=照常（与引擎同一实现）。

    真值来源必须是**引擎读的同一份 `.env`**：引擎进程由 `run.sh` 把 `.env` 灌进进程
    环境，而 web 进程未必有这些键——原先直接调 `day_off(now)`（落回 `os.environ`），
    于是 `.env` 里的急停/周末开关在 web 侧**恒不生效**：引擎真的暂停、界面却说
    "待签到 · 前方排队 N 人"。这里把 `.env` 的键值叠在进程环境之上（`.env` 有键即以
    `.env` 为准）后交给**同一个** `schedule.day_off`——判定实现、播出文案与门语义一行
    未改，只把"读到的是哪份值"对齐（显示与真值同源 ≠ 改门）。

    `file_env` 由调用方在调用时刻现读（`web.app.read_env(ENV_FILE)`）：`.env` 路径会被
    测试与 `--config` 改写，本模块另持一份绑定会让改写静默失效。
    """
    # 只此一处组合口径（yiban.engine.schedule.day_off）：页面提示与引擎行为必须看同一个
    # 判据，否则就是"页面说会跑、进程其实不跑"
    try:
        src = dict(os.environ)
        src.update(file_env or {})
        return yb_schedule.day_off(now, env=src)
    except Exception:   # 读不到配置就按"照常"：宁可多显示一次窗口内，也不谎报今天跳过
        return ""


#: 门原因（`yiban.engine.schedule.day_off` 的返回值）→ 日历页文案与语气档。
#: 与 `day_off` 的原因常量一一对应；未列出的原因返回空串（绝不给一个编出来的说法）。
DAY_OFF_TEXT = {
    yb_schedule.DAY_OFF_SUNDAY: "今日无需签到（周日）",
    yb_schedule.DAY_OFF_SATURDAY: "今日无需签到（周六）",
    yb_schedule.DAY_OFF_PAUSED: "全局暂停（急停）：今日自动签到已停止",
}


def day_off_payload(reason):
    """门原因 → 日历页显示载荷 {reason,text,tone}；无门（空串）返回 None。

    语气档：急停是"管理员刚做的动作、必须被看到"，用 warn；周末是例行无需签到，用 muted。
    文案与判据都不在这里新写——原因串来自 `day_off`，文案取自 `DAY_OFF_TEXT`。
    """
    if not reason:
        return None
    text = DAY_OFF_TEXT.get(reason, "")
    if not text:
        return None
    return {"reason": reason, "text": text,
            "tone": "warn" if reason == yb_schedule.DAY_OFF_PAUSED else "muted"}


def _in_run_period(bounds, in_sign_window, day_off_reason, now=None):
    """当前是否落在**本应运行**的时段内＝有效窗口内 且 今天没被门挡下。

    执行体接口的 `in_window` 用这个（`in_window` 的含义是"兜底现在该不该在跑"，不新增字段）。
    参数注入口径见模块头「通信」（`_in_sign_window` / `_day_off_reason` 都可被打桩）。
    """
    now = now or clock.now()
    # 必须含门：兜底常驻在"周末签到关闭/一键暂停"时会直接退出，而这两天的钟点明明落在
    # 窗口内——只按钟点算，页面会在每个周末与每次暂停期间报"兜底开了却没跑起来"。
    # 含门后前端不必改判断：in_window=false 就是"现在本不该有兜底在跑"的完整答案
    return in_sign_window(bounds, now) and not day_off_reason(now)


# ---------------------------------------------------------------------------
# 系统信息
# ---------------------------------------------------------------------------
def _hm(minute_of_day):
    """当天分钟数 → "HH:MM"（向下取整到整分钟，与设置页窗口展示同一取整方向）。

    """
    m = int(minute_of_day)  # 端点可为小数分钟（391.5 = 06:31:30），只能取整到分钟展示
    return f"{m // 60:02d}:{m % 60:02d}"


def window_fallback_text(bounds):
    """窗口不可用（`bounds` 已回退默认窗口）时的可见提示；正常窗口返回空串。

    唯一文案源：`sign_status` 的展示与设置页的提示必须逐字一致——同一异常在两处说成
    两句话，管理员会以为是两件事。
    """
    if not getattr(bounds, "fell_back", False):
        # 只有回退需要这句话：缓冲过大只是收缩缓冲、窗口照旧（见 yiban.window 的退化处置）
        return ""
    return (f"配置异常：签到窗口不可用，已按 {_hm(bounds.lo_min)}~{_hm(bounds.hi_min)} 运行")


def sign_status(env_file, load_env_int, sign_window_bounds, now=None):
    """基于服务器时间计算签到状态。

    返回 (显示文本, 颜色)。颜色为原版配色（东京夜蓝系，深浅页面背景均可读）；
    文案不含 emoji（UI 图标统一走前端 SVG 图标系统）。

    三段判定与文案里的钟点都取**有效**窗口端点（`window.bounds`，已扣前后裁剪、
    缓冲过大时缓冲被收缩）：本函数只服务展示，但展示的正是引擎实际开关点——按原始
    配置出字会在日常配置下就与引擎分叉 1 分钟（页面说"~07:50 结束"，引擎 07:49 已停手）。
    窗口本身不可用时（`fell_back`，防御分支）改出"配置异常、已按 X~Y 运行"整句：
    此时管理员设的窗口根本没被采用，继续报三段状态等于谎报。

    参数注入口径见模块头「通信」（`ENV_FILE` / `load_env_int` / `sign_window_bounds`）。
    """
    now = now or clock.now()
    if now.weekday() == 6 and not load_env_int(env_file, "YIBAN_SUNDAY_SIGN", 0):
        # 周日：仅当「周日签到」开启时走正常窗口逻辑，否则提示无需打卡
        return "今日无需打卡（周日）", "#a1a1aa"
    if now.weekday() == 5 and not load_env_int(env_file, "YIBAN_SATURDAY_SIGN", 0):
        # 周六：默认关闭；开启后走正常窗口逻辑
        return "今日无需打卡（周六）", "#a1a1aa"
    win = sign_window_bounds()  # 单次读取（每次调用都会重读 .env，避免重复解析）
    fallback = window_fallback_text(win)
    if fallback:
        return fallback, "#e0af68"
    lo_min, hi_min = win.lo_min, win.hi_min
    now_min = now.hour * 60 + now.minute + now.second / 60.0
    if now_min < lo_min:
        return f"未到签到时间（{_hm(lo_min)} 开始）", "#7aa2f7"
    if now_min <= hi_min:
        return f"签到窗口进行中（~{_hm(hi_min)} 结束）", "#9ece6a"
    return "今日签到已结束", "#e0af68"


def check_connectivity():
    """连通性检测：不登录，仅检查易班 API 可达性。返回 (ok, detail)。"""
    try:
        resp = requests.get(
            API_AUTH_URL,
            timeout=6,
            headers={
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
            },
        )
        ok = resp.status_code < 500  # 4xx 也算"可达"：只回答通不通，不代表凭据有效
        detail = f"HTTP {resp.status_code}"
    except Exception as e:
        ok = False
        detail = str(e)[:60]  # 截 60：异常文本可能带长 URL/链路，接口只给一行
    return ok, detail
