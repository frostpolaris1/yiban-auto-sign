# -*- coding: utf-8 -*-
"""签到窗口：**唯一事实源**（排计划 / 判关闭 / 算容量三处必须同源）。

窗口 = `YIBAN_SIGN_START`~`YIBAN_SIGN_END`（默认 06:30~07:50，北京时间）；
"有效窗口" = 两端各让出 `YIBAN_WINDOW_EDGE_FRONT_SEC` / `_BACK_SEC` 秒（默认各 60，
防掐着边界发起请求）。有效窗口被裁剪吃空（前后裁剪 >= 窗口宽度）时**回退默认窗口**
并在 `fell_back` 上报告——回退与判定必须在 `bounds` 里一起做，否则会出现：

- 计划按"回退后的默认窗口"排，而关闭判定按"原始配置"算 → 有完整计划却整轮判
  "时段已结束"、零请求；
- 容量预检/网页预估用**完整**有效窗口算、不扣已流逝时间 → 迟启动时按满容量放行，
  真实剩余只能容纳 `(eff_hi - now) / (avg + gap)` 个，超出者全部落 skipped_window。

时间一律取 `yiban.clock`（北京时间），与窗口语义同源。

调用方的取舍（有意区分，勿"顺手统一"）：
- **引擎**（signin 预检）用 `remaining_sec(now)`——它问的是"今天还能签几个"；
- **Web 设置页**用 `full_sec()`——它问的是"按这套配置能容纳几个"，是配置属性，
  若扣掉流逝时间，管理员在窗口末尾将永远无法保存设置（保存闸门会误拒）。
"""
import datetime
import os

DEFAULT_START = (6, 30)
DEFAULT_END = (7, 50)
DEFAULT_EDGE_SEC = 60
EDGE_MIN_SEC = 0
EDGE_MAX_SEC = 300
# 补签轮（当天最后一轮）触发点，默认 07:12——**必须早于进程内补签轮的等待目标**：
# signin 的告警抑制靠它判断"是否还有下一轮兜底"。宿主 run.sh 与
# docker/scheduler.py 读同一个键（后者把真实值注入子进程环境）。
DEFAULT_RETRY_HM = (7, 12)


def parse_hhmm(value, default):
    """解析 HH:MM → (h, m)；非法返回 default（与 signin._parse_hhmm 同口径）。"""
    try:
        h, m = str(value).strip().split(":")
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return default
        return (h, m)
    except (ValueError, AttributeError):
        return default


def parse_window(env):
    """→ (start, end, invalid)。start >= end（跨零点不支持）时回退默认并报告。"""
    start = parse_hhmm(env.get("YIBAN_SIGN_START", ""), DEFAULT_START)
    end = parse_hhmm(env.get("YIBAN_SIGN_END", ""), DEFAULT_END)
    if start >= end:
        return DEFAULT_START, DEFAULT_END, True
    return start, end, False


def parse_edges(env):
    """→ (front_sec, back_sec)。旧键 YIBAN_WINDOW_EDGE_SEC（前后对称）优先映射。"""
    front = _int_or_none(env, "YIBAN_WINDOW_EDGE_FRONT_SEC")
    back = _int_or_none(env, "YIBAN_WINDOW_EDGE_BACK_SEC")
    legacy = _int_or_none(env, "YIBAN_WINDOW_EDGE_SEC", 0, 600)
    if front is None:
        front = legacy if legacy is not None else DEFAULT_EDGE_SEC
    if back is None:
        back = legacy if legacy is not None else DEFAULT_EDGE_SEC
    return front, back


def retry_hm(env=None):
    """补签轮（当天最后一轮）触发点 (h, m)：取 `YIBAN_SECOND_RUN_TIME`，非法回默认。

    **调用方按需取当前值，不要在导入期缓存**：宿主 run.sh 的补签 cron 时刻由同一键
    配置，管理员改了键而进程内常量不跟着变，告警抑制就会按错的时刻判断"是否还有下
    一轮兜底"——阈值早于真实末轮 → 提前告警（噪音）；晚于真实末轮 → 真异常当天不再
    有任何提示（静默漏报，危害更大）。
    """
    if env is None:
        env = os.environ
    return parse_hhmm(env.get("YIBAN_SECOND_RUN_TIME", ""), DEFAULT_RETRY_HM)


def bounds(cfg, invalid=False):
    """有效窗口边界（分钟，相对当天 0:00）→ `Window`。

    `cfg` 是**已解析的配置映射**，含 4 个键：`sign_start`/`sign_end`（(h, m)）、
    `edge_front_sec`/`edge_back_sec`（秒）——即 `signin._schedule_config()` 的返回形态。
    两个入口刻意分开：env 解析只发生在 `_schedule_config` / `from_env` 一处，
    引擎内部（排计划、判关闭、算容量）一律拿 cfg 调本函数，口径不可能各算各的。
    """
    start = tuple(cfg["sign_start"])
    end = tuple(cfg["sign_end"])
    front = int(cfg["edge_front_sec"])
    back = int(cfg["edge_back_sec"])
    start_min, end_min = start[0] * 60 + start[1], end[0] * 60 + end[1]
    lo = start_min + front / 60.0
    hi = end_min - back / 60.0
    fell_back = hi <= lo
    if fell_back:
        start_min = DEFAULT_START[0] * 60 + DEFAULT_START[1]
        end_min = DEFAULT_END[0] * 60 + DEFAULT_END[1]
        front = back = DEFAULT_EDGE_SEC
        lo = start_min + front / 60.0
        hi = end_min - back / 60.0
    return Window(start_min, end_min, lo, hi, front, back, invalid, fell_back)


class Window:
    """有效窗口的不可变视图（见模块文档的调用方取舍）。"""

    __slots__ = (
        "back_sec",
        "end_min",
        "fell_back",
        "front_sec",
        "hi_min",
        "invalid",
        "lo_min",
        "start_min",
    )

    def __init__(self, start_min, end_min, lo_min, hi_min,
                 front_sec, back_sec, invalid=False, fell_back=False):
        self.start_min = start_min
        self.end_min = end_min
        self.lo_min = lo_min
        self.hi_min = hi_min
        self.front_sec = front_sec
        self.back_sec = back_sec
        self.invalid = invalid
        self.fell_back = fell_back

    # ---- 容量口径 ----
    def full_sec(self):
        """完整有效窗口秒数（配置属性；Web 设置页/保存闸门用）。"""
        return max(0.0, (self.hi_min - self.lo_min) * 60.0)

    def remaining_sec(self, now_dt):
        """从 now_dt 到有效窗口结束的剩余秒数（可为负；引擎预检用）。"""
        now_min = _minute_of_day(now_dt)
        return (self.hi_min - now_min) * 60.0

    def is_closed(self, now_dt):
        """有效窗口是否已关闭（排计划与判关闭同源的关键）。"""
        return _minute_of_day(now_dt) > self.hi_min


def from_env(env):
    """从环境/`.env` 映射直接构造 `Window`（Web 侧与独立工具用）。"""
    start, end, invalid = parse_window(env)
    front, back = parse_edges(env)
    return bounds({"sign_start": start, "sign_end": end,
                   "edge_front_sec": front, "edge_back_sec": back}, invalid=invalid)


def to_dt(base_date, minute):
    """当天分钟数 → datetime（base_date 提供日期）。"""
    return base_date + datetime.timedelta(minutes=minute)


# ---- 内部实现 ----
def _int_or_none(env, key, lo=EDGE_MIN_SEC, hi=EDGE_MAX_SEC):
    raw = str(env.get(key, "") or "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
    except ValueError:
        return None
    return v if lo <= v <= hi else None


def _minute_of_day(dt):
    return dt.hour * 60 + dt.minute + dt.second / 60.0
