# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""签到窗口与运行时段判定、系统信息（状态文案与连通性检测）。

**功能**
`.env` 开关值解析 `_env_flag`；签到窗口解析 `_sign_window`；"现在是否在窗口内"的纯钟点
判定 `_in_sign_window` 与含周末门/暂停门的 `_in_run_period`（附门原因 `_day_off_reason`）；
系统信息族的 `sign_status`（状态文案与配色）与 `check_connectivity`（不登录的可达性探测）。

**归属**
原 `web/app.py` 的模块级窗口与系统信息辅助，唯一真源在本模块；`web/app.py` 只保留名字面与
转发，把它自己持有、而本模块需要的模块级名字——`.env` 路径 `ENV_FILE`、整数配置读取器
`load_env_int`、窗口解析器 `_sign_window`、钟点判定 `_in_sign_window`——在调用时刻现取后注入。

**复用**
窗口解析委托 `yiban.window.parse_window`、运行时段委托 `yiban.engine.schedule.day_off`——
与引擎（signin）同一份口径，避免"网页显示 07:50、引擎按别的值判定"这类同概念两套实现；
`_in_run_period` 复用 `_in_sign_window` 的钟点判定再叠门，不另写一套窗口逻辑。

**通信**
本模块不反向导入 `web.app`（本仓测试以别名加载 `app.py`，普通 import 会再执行一份副本
模块）。`.env` 路径、读取器与窗口/钟点判定都作为显式参数接收：它们在 `web.app` 上是会被
测试改写、也会随 `--config` 与运行方式变化的模块级名字（既有测试在 `web.app` 上打桩
`_sign_window` 与 `_in_sign_window` 来固定窗口与时段），本模块另持一份绑定会让这些改写
静默失效。
"""

import logging

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
    """把 `.env` 里的开关值解析成布尔（认 1/true/on/yes，大小写不敏感）。

    只认这四个真值字面量，其余（`0`/`false`/空/未设/写错的词）一律按**关**处理：
    兜底常驻是要占一份出口与一个常驻进程的动作，"开"必须是明确表达的意图。
    """
    return str(value if value is not None else "").strip().lower() in _TRUTHY_LITERALS


def _sign_window(env_file, read_env):
    """签到窗口（`.env` 覆盖 YIBAN_SIGN_START/END，非法回退默认）。

    解析委托 `yiban.window.parse_window`——与引擎（signin）同一份口径，
    避免"网页显示 07:50、引擎按别的值判定"这类同概念两套实现。

    `.env` 路径与读取器由调用方传入（`web.app` 的 `ENV_FILE` 与 `read_env`）：
    两者都是会被测试改写、也会随 `--config` 变化的模块级名字。
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


def _day_off_reason(now=None):
    """今天此刻是否被周末门/一键暂停挡下 → 原因串；空串=照常（与引擎同一实现）。

    组合口径只有一处（`yiban.engine.schedule.day_off`）：页面提示与引擎实际行为
    必须看同一个判据，否则又会出现"页面说会跑、进程其实不跑"。
    """
    try:
        return yb_schedule.day_off(now)
    except Exception:   # 配置读不到时按"照常"处理：宁可多显示一次窗口内，也别谎报跳过
        return ""


def _in_run_period(bounds, in_sign_window, day_off_reason, now=None):
    """当前是否落在**本应运行**的时段内＝有效窗口内 且 今天没被门挡下。

    执行体接口的 `in_window` 用这个（`in_window` 的含义是"兜底现在该不该在跑"，
    不新增字段）。为什么必须含门：兜底常驻在"周末签到关闭 / 一键暂停"时会直接退出，
    而这两天的钟点明明落在窗口内——只按钟点算，页面会在每个周末与每次暂停期间报
    "兜底开了却没跑起来"。含门后前端不需要改判断：`in_window=false` 就是"现在本不该
    有兜底在跑"的完整答案。

    钟点判定与门判定都由调用方传入（`web.app` 的 `_in_sign_window` / `_day_off_reason`）：
    两者在 `web.app` 上都可被打桩，本模块另持绑定会让"固定时段/固定门"的桩静默失效。
    """
    now = now or clock.now()
    return in_sign_window(bounds, now) and not day_off_reason(now)


# ---------------------------------------------------------------------------
# 系统信息
# ---------------------------------------------------------------------------
def sign_status(env_file, load_env_int, sign_window, now=None):
    """基于服务器时间计算签到状态。

    返回 (显示文本, 颜色)。颜色为原版配色（东京夜蓝系，深浅页面背景均可读）；
    文案不含 emoji（UI 图标统一走前端 SVG 图标系统）。

    `.env` 路径、整数配置读取器与窗口解析器由调用方传入（`web.app` 的
    `ENV_FILE` / `load_env_int` / `_sign_window`）：三者都会被测试改写或在调用点打桩。
    """
    now = now or clock.now()
    if now.weekday() == 6 and not load_env_int(env_file, "YIBAN_SUNDAY_SIGN", 0):
        # 周日：仅当「周日签到」开启时走正常窗口逻辑，否则提示无需打卡
        return "今日无需打卡（周日）", "#a1a1aa"
    if now.weekday() == 5 and not load_env_int(env_file, "YIBAN_SATURDAY_SIGN", 0):
        # 周六：默认关闭；开启后走正常窗口逻辑
        return "今日无需打卡（周六）", "#a1a1aa"
    sw = sign_window()  # 单次读取（每次调用都会重读 .env，避免重复解析）
    start_h, start_m = sw[0]
    end_h, end_m = sw[1]
    start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if now < start:
        return f"未到签到时间（{start_h:02d}:{start_m:02d} 开始）", "#7aa2f7"
    if now <= end:
        return f"签到窗口进行中（~{end_h:02d}:{end_m:02d} 结束）", "#9ece6a"
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
        ok = resp.status_code < 500
        detail = f"HTTP {resp.status_code}"
    except Exception as e:
        ok = False
        detail = str(e)[:60]
    return ok, detail
