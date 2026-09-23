# -*- coding: utf-8 -*-
"""签到窗口：**唯一事实源**（排计划 / 判关闭 / 算容量三处必须同源）。

窗口 = `YIBAN_SIGN_START`~`YIBAN_SIGN_END`（默认 06:30~07:50，北京时间）；
"有效窗口" = 两端各让出 `YIBAN_WINDOW_EDGE_FRONT_SEC` / `_BACK_SEC` 秒（默认各 60，
防掐着边界发起请求）。

**退化处置：窗口是管理员意图、缓冲只是精修，故牺牲精修、绝不牺牲意图。**
前后缓冲之和 >= 窗口宽度时，**保留管理员设的窗口**，把缓冲**等比收缩**到只占窗口
宽度的 20%（有效窗口 = 窗口宽度的 80%），并在 `edges_clamped` 上报告。为什么不能用
内置默认窗口顶替：那个默认只是项目作者自己的时段，真实签到时段在别处的部署者会被
静默改到错误时段签到（例：设 21:00~21:05 + 各 300s 缓冲 ⇒ 改按 06:30~07:50 跑）。
只有窗口**本身不可用**（宽度 <= 0，上游 `parse_window` 对"起 >= 止/无法解析"已替换为
默认窗口，故这里是防御分支）才回退默认窗口，并在 `fell_back` 上报告。

收缩与判定必须在 `bounds` 里一起做，否则会出现：

- 计划按"收缩/回退后的窗口"排，而关闭判定按"原始配置"算 → 有完整计划却整轮判
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
#: 缓冲合计占窗口宽度的比例上限：收缩后有效窗口 = 窗口宽度的 (1 - 此值)。
#: 取 0.2 是"精修仍有意义"与"窗口被吃空"之间的余量，也远小于前端/保存侧
#: 单边 20%（合计 40%）的预防上限，故正常路径下根本不会走到收缩。
EDGE_BUDGET_RATIO = 0.2
#: 单边缓冲的预防上限（保存侧夹取与前端滑块量程共用）：窗口宽度的 20%。
#: 两边合计 40%，仍远小于 `bounds` 的收缩阈值（合计 >= 100%），故保存过的配置
#: 在运行期不会再触发收缩——预防在前，收缩只是手改 .env 时的兜底。
EDGE_SIDE_CAP_RATIO = 0.2
# 补签轮（当天最后一轮）触发点，默认 07:12——**必须早于进程内补签轮的等待目标**：
# signin 的告警抑制靠它判断"是否还有下一轮兜底"。宿主 run.sh 与
# docker/scheduler.py 读同一个键（后者把真实值注入子进程环境）。
DEFAULT_RETRY_HM = (7, 12)


def parse_hhmm(value, default):
    """解析 HH:MM → (h, m)；非法返回 default（唯一实现：容器调度器与 run.sh 都按此口径校验）。"""
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


def edge_cap_sec(window_sec):
    """单边缓冲的预防上限（秒）：窗口宽度的 20%，按 30s 粒度向下取整，封顶 `EDGE_MAX_SEC`。

    保存侧夹取与前端滑块量程**共用本函数的口径**（前端为同一条式子的 JS 版）：
    两边合计最多 40%，永远够不到 `bounds` 的收缩阈值（合计 >= 100%）。取整到 30s
    与提交校验同粒度（0.5 分钟），否则夹出来的值前端滑块表达不出来。
    """
    cap = int(window_sec * EDGE_SIDE_CAP_RATIO)
    cap -= cap % 30
    return max(EDGE_MIN_SEC, min(cap, EDGE_MAX_SEC))


def bounds(cfg, invalid=False):
    """有效窗口边界（分钟，相对当天 0:00）→ `Window`。

    `cfg` 是**已解析的配置映射**，含 4 个键：`sign_start`/`sign_end`（(h, m)）、
    `edge_front_sec`/`edge_back_sec`（秒）——即 `signin._schedule_config()` 的返回形态。
    两个入口刻意分开：env 解析只发生在 `_schedule_config` / `from_env` 一处，
    引擎内部（排计划、判关闭、算容量）一律拿 cfg 调本函数，口径不可能各算各的。

    退化处置见模块文档：缓冲过大 → 等比收缩缓冲（`edges_clamped`）；窗口宽度 <= 0 →
    回退默认窗口（`fell_back`）。正常窗口逐值不变（不碰 start/end/edges）。
    """
    start = tuple(cfg["sign_start"])
    end = tuple(cfg["sign_end"])
    front = int(cfg["edge_front_sec"])
    back = int(cfg["edge_back_sec"])
    start_min, end_min = start[0] * 60 + start[1], end[0] * 60 + end[1]
    lo = start_min + front / 60.0
    hi = end_min - back / 60.0
    fell_back = edges_clamped = False
    if hi <= lo:
        width_min = end_min - start_min
        if width_min > 0:
            # 窗口可用：只等比收缩缓冲。收缩后有效窗口恰为窗口宽度的 80%，
            # 且窗口宽度 >= 1 分钟（分钟粒度）时必有 lo < hi。
            budget_sec = width_min * 60.0 * EDGE_BUDGET_RATIO
            total = front + back
            scale = budget_sec / total if total > 0 else 0.0
            front = round(front * scale)
            back = round(back * scale)
            lo = start_min + front / 60.0
            hi = end_min - back / 60.0
            edges_clamped = True
        else:
            fell_back = True
            start_min = DEFAULT_START[0] * 60 + DEFAULT_START[1]
            end_min = DEFAULT_END[0] * 60 + DEFAULT_END[1]
            front = back = DEFAULT_EDGE_SEC
            lo = start_min + front / 60.0
            hi = end_min - back / 60.0
    return Window(start_min, end_min, lo, hi, front, back, invalid, fell_back,
                  edges_clamped)


class Window:
    """有效窗口的不可变视图（见模块文档的调用方取舍）。

    三个退化标记语义**不同，勿混用**：

    - `invalid`：上游 `parse_window` 判"起止无法解析或起 >= 止"的事实，构造参数透传。
      它描述的是**原始配置**，不代表本视图已被替换（`bounds` 只在宽度 <= 0 时才替换）。
    - `fell_back`：窗口宽度 <= 0（防御分支）→ start/end/front/back 全部是内置默认值，
      管理员设的窗口**已不被采用**。这是唯一需要提示"配置异常、已按 X~Y 运行"的情形。
    - `edges_clamped`：窗口可用但缓冲之和 >= 窗口宽度 → start/end 原样保留，只有
      front/back 被等比收缩（有效窗口 = 窗口宽度的 80%）。管理员意图未被改动。

    `invalid` 当前全仓无消费者（外部可能打桩读取），故保留而不并入另外两者。
    """

    __slots__ = (
        "back_sec",
        "edges_clamped",
        "end_min",
        "fell_back",
        "front_sec",
        "hi_min",
        "invalid",
        "lo_min",
        "start_min",
    )

    def __init__(self, start_min, end_min, lo_min, hi_min,
                 front_sec, back_sec, invalid=False, fell_back=False,
                 edges_clamped=False):
        self.start_min = start_min
        self.end_min = end_min
        self.lo_min = lo_min
        self.hi_min = hi_min
        self.front_sec = front_sec
        self.back_sec = back_sec
        self.invalid = invalid
        self.fell_back = fell_back
        self.edges_clamped = edges_clamped

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

    def is_open(self, now_dt):
        """有效窗口是否**已经开始**（与 `is_closed` 配对成三段：未开 / 进行中 / 已关）。

        为什么必须与 `is_closed` 分开：只判"关没关"含不住"还没开"——提前拉起的常驻
        执行体（见 `run_fallback_worker`）会把窗口外当窗口内照发请求。
        """
        return _minute_of_day(now_dt) >= self.lo_min

    def opens_in_sec(self, now_dt):
        """距窗口开始还有多少秒（已开始则为 0 或负；调用方自行 `max(0, …)` 收敛）。"""
        return (self.lo_min - _minute_of_day(now_dt)) * 60.0


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
