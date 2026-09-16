# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""容量与排期：容量预估口径、调度 v2（统一填充框架）与签到窗口判定。

调度 v2 是"排序维度（顺序/随机）× 分布维度（均匀/正态）"的 2×2 组合：块容量顺延、
自选时间片优先、正态钟形锚点、σ 自适应封顶、超容量压缩模式。窗口（起止 + 掐头去尾 +
是否已关闭）的唯一口径在 `yiban.window`，本模块只做调用与告警，不另存一份判断。

**告警去重**：窗口配置非法 / 有效窗口被裁剪吃空各只并入当日汇总邮件一次（`_invalid_
window_notified`、`_edge_empty_window_notified`）——这两个函数每天被多账号多轮调用，
不去重会把同一配置错误刷成几十条。

跨模块调用纪律见包说明：跨模块一律走模块属性访问。
"""
import logging
import math
import os
import random
from datetime import timedelta

from yiban import clock, window
from yiban.store import db

logger = logging.getLogger("yiban")

# ---------------------------------------------------------------------------
# 调度 v2（S1 demo）：统一填充框架配置
# 设计文档：docs/design/plan-scheduler-v2.md（2×2 组合 + 安全底座）
# ---------------------------------------------------------------------------
_DEFAULT_SIGN_START = (6, 30)
_DEFAULT_SIGN_END = (7, 50)
_DEFAULT_EDGE_SEC = 60          # 首尾缓冲：有效窗口 [SIGN_START+60s, SIGN_END-60s]
_DEFAULT_BLOCK_CAP = 15         # 块容量（每块最多人数，满则向后顺延）
_DEFAULT_MU_MIN_PCT = 40        # 正态高峰中心范围（有效窗口相对位置 %）
_DEFAULT_MU_MAX_PCT = 60
_DEFAULT_SIGMA_MIN_PCT = 15     # 正态分散程度范围（有效窗口宽度 %）
_DEFAULT_SIGMA_MAX_PCT = 25
_DEFAULT_MIN_EXEC_GAP = 5       # 请求最小间隔下限（秒，压缩模式防请求过密；F1 接线于 run_queue_retry）
# 容量预检与容量预估共用的单账号耗时估算（秒）。缺省按压测实测定档：
# 单账号（登录链 + 签到链共 6 次请求）实测 0.08s（零延迟）、1.87s（拟真 300ms）、
# 3.1s（含尾延迟）。旧缺省 8s 无实测依据，把可容纳账号数低估约 2.6 倍，
# 并使保存门误拒 261~360 个账号的站点；真实网络更慢时由 YIBAN_AVG_ATTEMPT_SEC 覆盖。
_DEFAULT_AVG_ATTEMPT_SEC = 3
_DEFAULT_RETRY_MIN_INTERVAL = 60
_DEFAULT_EXEC_GAP_MIN = 10      # 启动对齐：已过点账号相邻最小间隔（秒）
_DEFAULT_ALLOW_TIME_PREF = 0    # 用户自选时间片总开关（0=关默认，管理员开启后生效）

# 签到窗口配置异常的一次性告警标记（2026-08-28 审查 F3）：
# _schedule_config 每次调度都会调用，非法窗口回退默认窗口的告警只收集一次，
# 避免同一个配置错误在每日汇总邮件里重复出现 N 次
_invalid_window_notified = False

# 有效签到窗口为空的一次性告警标记：
# _schedule_blocks 每次调度都会调用（多账号/多轮），前后裁剪吃满窗口回退默认
# 窗口的邮件告警同样只收集一次（镜像上方 F3 去重模式），防汇总邮件刷屏
_edge_empty_window_notified = False


def _parse_hhmm(value, default):
    """解析 HH:MM → (h, m)；非法返回 default。"""
    try:
        h, m = value.strip().split(":")
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return default
        return (h, m)
    except (ValueError, AttributeError):
        return default


def _env_int(name, default, lo=None, hi=None):
    """读整数环境变量；缺失/非法回退默认（配置校验：回退 + 警告，不崩溃）。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        logger.warning("配置 %s=%r 非法，回退默认 %s", name, raw, default)
        return default
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        logger.warning("配置 %s=%s 超出范围 [%s, %s]，回退默认 %s", name, v, lo, hi, default)
        return default
    return v


def avg_attempt_sec():
    """单账号签到耗时估算（秒）：YIBAN_AVG_ATTEMPT_SEC 显式配置优先，缺省 3s。"""
    return _env_int("YIBAN_AVG_ATTEMPT_SEC", _DEFAULT_AVG_ATTEMPT_SEC, 1, 300)


def capacity_accounts(window_sec, gap=0, avg=None):
    """有效窗口内可容纳的账号数（容量口径唯一源：引擎预检与 web 容量预估共用）。

    模型：首个账号立刻占用 avg 秒，此后每个账号按「上一次完成 + 间隔下限」推进，
    相邻账号墙钟间隔 = avg + gap（实测印证：gap=10、t=1.87s → 单账号周期 11.875s）。
    故 容量 = floor((窗口 − avg) ÷ (avg + gap)) + 1；窗口容不下单账号耗时为 0。

    window_sec：有效窗口秒数（已扣掐头去尾）
    gap：账号间隔下限秒（YIBAN_ACCOUNT_GAP_MAX）
    avg：单账号耗时秒；缺省取 avg_attempt_sec()
    """
    if avg is None:
        avg = avg_attempt_sec()
    avg = max(1, int(avg))
    gap = max(0, int(gap or 0))
    slack = int(window_sec) - avg
    if slack < 0:
        return 0
    return slack // (avg + gap) + 1


def _schedule_config():
    """读取调度 v2 配置（每次调用读取，便于测试与热改）。

    兼容旧 YIBAN_SIGN_MODE：sequence→顺序×均匀、random→随机×均匀、normal→顺序×正态；
    新参数 YIBAN_SIGN_ORDER / YIBAN_SIGN_DIST 优先。
    返回 dict：order/dist/edge_front_sec/edge_back_sec/block_cap/mu/sigma 百分比/
    min_exec_gap/avg_attempt_sec/retry_min_interval/exec_gap_min/sign_start/sign_end。
    """
    # 局部导入：alerts 反向依赖本模块的窗口判定（告警要判"窗口是否还开着"），
    # 模块级互引会成环；本函数每天只调用几次，局部导入的开销可忽略。
    from yiban.engine import alerts

    mode = os.environ.get("YIBAN_SIGN_MODE", "").strip().lower()
    order = os.environ.get("YIBAN_SIGN_ORDER", "").strip().lower()
    dist = os.environ.get("YIBAN_SIGN_DIST", "").strip().lower()
    if order not in ("sequence", "random"):
        order = "random" if mode == "random" else "sequence"
        if dist not in ("uniform", "normal"):
            dist = "normal" if mode == "normal" else "uniform"
    elif dist not in ("uniform", "normal"):
        dist = "uniform"
    # 窗口与前后裁剪的解析委托 yiban.window（排计划/判关闭/算容量同源）；告警仍在此处发
    start, end, _win_invalid = window.parse_window(os.environ)
    if _win_invalid:
        global _invalid_window_notified
        if not _invalid_window_notified:
            _invalid_window_notified = True
            _msg = (
                f"签到窗口 {start[0]:02d}:{start[1]:02d} ~ {end[0]:02d}:{end[1]:02d} 非法"
                "（start>=end，跨零点窗口不受支持），已回退默认 06:30~07:50，"
                "实际签到时间将与配置不符！请修改 YIBAN_SIGN_START / YIBAN_SIGN_END"
            )
            logger.error("%s", _msg)
            # 2026-08-28 审查 F3：原实现只写 WARNING 日志，管理员在 Web 界面看到的
            # 窗口设置"看起来生效"、实际签到时刻完全不同且无人知情。现并入当日
            # 汇总邮件（A 线），确保配置错误可被管理员发现。
            alerts._collect_admin_mail("签到窗口配置异常", _msg)
        start, end = _DEFAULT_SIGN_START, _DEFAULT_SIGN_END
    mu_lo = _env_int("YIBAN_SCHEDULE_MU_MIN_PCT", _DEFAULT_MU_MIN_PCT, 0, 100)
    mu_hi = _env_int("YIBAN_SCHEDULE_MU_MAX_PCT", _DEFAULT_MU_MAX_PCT, 0, 100)
    if mu_lo >= mu_hi:
        logger.warning("μ 范围 %s~%s 非法，回退默认 40~60", mu_lo, mu_hi)
        mu_lo, mu_hi = _DEFAULT_MU_MIN_PCT, _DEFAULT_MU_MAX_PCT
    sigma_lo = _env_int("YIBAN_SCHEDULE_SIGMA_MIN_PCT", _DEFAULT_SIGMA_MIN_PCT, 0, 100)
    sigma_hi = _env_int("YIBAN_SCHEDULE_SIGMA_MAX_PCT", _DEFAULT_SIGMA_MAX_PCT, 0, 100)
    if sigma_lo >= sigma_hi:
        logger.warning("σ 范围 %s~%s 非法，回退默认 15~25", sigma_lo, sigma_hi)
        sigma_lo, sigma_hi = _DEFAULT_SIGMA_MIN_PCT, _DEFAULT_SIGMA_MAX_PCT
    # 掐头去尾（0.22.0 起前后独立，秒级，0.5 分钟=30s 粒度；UI 按 0.5 分钟步进）：
    # 新键 YIBAN_WINDOW_EDGE_FRONT_SEC / _BACK_SEC 优先；旧键 YIBAN_WINDOW_EDGE_SEC
    # 存在时映射为前后对称（保证旧配置行为不变）——解析在 yiban.window.parse_edges。
    edge_front, edge_back = window.parse_edges(os.environ)
    return {
        "order": order,
        "dist": dist,
        "edge_front_sec": edge_front,
        "edge_back_sec": edge_back,
        "block_cap": _env_int("YIBAN_BLOCK_CAP", _DEFAULT_BLOCK_CAP, 1, 200),
        "mu_min_pct": mu_lo,
        "mu_max_pct": mu_hi,
        "sigma_min_pct": sigma_lo,
        "sigma_max_pct": sigma_hi,
        "min_exec_gap": _env_int("YIBAN_MIN_EXEC_GAP", _DEFAULT_MIN_EXEC_GAP, 1, 60),
        "avg_attempt_sec": avg_attempt_sec(),
        "retry_min_interval": _env_int("YIBAN_RETRY_MIN_INTERVAL", _DEFAULT_RETRY_MIN_INTERVAL, 1, 600),
        "exec_gap_min": _env_int("YIBAN_EXEC_GAP_MIN", _DEFAULT_EXEC_GAP_MIN, 0, 300),
        "allow_time_pref": _env_int("YIBAN_ALLOW_TIME_PREF", _DEFAULT_ALLOW_TIME_PREF, 0, 1),
        "sign_start": start,
        "sign_end": end,
    }


def _anchor_z(phone):
    """账号锚点分位（顺序×正态）：hash(phone) 派生标准正态值，零持久化、每天稳定。"""
    return random.Random(str(phone)).gauss(0, 1)


def _sigma_eff(sigma, n, span_minutes):
    """人数自适应 + 封顶：σ×(1+log2(n/20))，上限 有效窗口/3（防端点堆积）。"""
    if n > 20:
        sigma = sigma * (1 + math.log2(n / 20))
    return min(sigma, span_minutes / 3)


def _schedule_blocks(cfg):
    """按时钟 5 分钟对齐切块（首尾块各 4 分钟），返回 (blocks, eff_lo, eff_hi)。

    blocks: [(lo_min, hi_min), ...]（浮点分钟，支持 0.5 分钟=30s 的裁剪粒度）；
    eff_lo/eff_hi：有效窗口分钟边界（相对当天 0:00），由前后裁剪分别决定。
    有效窗口为空（前裁+后裁 >= 窗口宽度）时回退默认窗口，保证调用方永不拿到空块列表。
    """
    from yiban.engine import alerts  # 局部导入的理由同 _schedule_config

    win = window.bounds(cfg)
    start_min, end_min = win.start_min, win.end_min
    eff_lo, eff_hi = win.lo_min, win.hi_min
    if win.fell_back:
        logger.warning(
            "有效签到窗口为空（窗口 %s~%s、前裁 %ss 后裁 %ss），回退默认窗口 06:30~07:50",
            cfg["sign_start"], cfg["sign_end"], cfg["edge_front_sec"], cfg["edge_back_sec"],
        )
        # 镜像 _schedule_config 的 F3 模式（一次性去重）——原实现
        # 只写 WARNING 日志，管理员在 Web 界面看到的裁剪设置"看起来生效"、实际
        # 签到时刻完全不同且无人知情。现并入当日汇总邮件（A 线）一次，确保
        # 前后裁剪配置错误可被管理员发现；去重防多账号/多轮调用刷屏。
        global _edge_empty_window_notified
        if not _edge_empty_window_notified:
            _edge_empty_window_notified = True
            alerts._collect_admin_mail(
                "签到窗口配置异常",
                (
                    f"有效签到窗口为空：窗口 "
                    f"{cfg['sign_start'][0]:02d}:{cfg['sign_start'][1]:02d}"
                    f"~{cfg['sign_end'][0]:02d}:{cfg['sign_end'][1]:02d}"
                    f" 被前后裁剪吃满（前 {cfg['edge_front_sec']}s / 后 "
                    f"{cfg['edge_back_sec']}s），已回退默认窗口 06:30~07:50，"
                    "实际签到时间将与配置不符！请调小 "
                    "YIBAN_WINDOW_EDGE_FRONT_SEC / YIBAN_WINDOW_EDGE_BACK_SEC"
                    "（或放宽 YIBAN_SIGN_START / YIBAN_SIGN_END）"
                ),
            )
    blocks = []
    b = start_min
    while b < end_min:
        lo = max(b, eff_lo)
        hi = min(b + 5, eff_hi)
        if hi > lo:
            blocks.append((lo, hi))
        b += 5
    return blocks, eff_lo, eff_hi


def _minute_to_dt(base_date, minute):
    """当天分钟数 → datetime（base_date 提供日期）。"""
    return base_date + timedelta(minutes=minute)


def _nearest_available(bi, filled, blocks, cap):
    """双向就近找未满块（自选溢出顺延用；同距离优先更早的块）。无可用返回 None。"""
    n = len(blocks)
    for d in range(n):
        for idx in (bi - d, bi + d):
            if 0 <= idx < n and filled[idx] < cap:
                return idx
    return None


def _next_available(bi, filled, blocks, cap):
    """从 bi 向后（环回）找第一个未满块。"""
    n = len(blocks)
    for step in range(n):
        idx = (bi + step) % n
        if filled[idx] < cap:
            return idx
    return bi  # 全满（理论不会发生：cap 已按 n 放大）


def _slot_to_bi(cfg):
    """自选片分钟偏移（相对窗口起点）→ 块索引。

    口径与 web/app.py `_pref_slots` 完全一致：块起点 = 窗口起点 + 5k 对齐，
    key = 块起点 - 窗口起点（窗口起点非 5 分钟倍数时同样成立）。
    """
    start_min = cfg["sign_start"][0] * 60 + cfg["sign_start"][1]
    end_min = cfg["sign_end"][0] * 60 + cfg["sign_end"][1]
    front = cfg["edge_front_sec"] / 60.0
    back = cfg["edge_back_sec"] / 60.0
    m = {}
    bi = 0
    for b in range(start_min, end_min, 5):
        lo = max(b, start_min + front)
        hi = min(b + 5, end_min - back)
        if hi > lo:
            m[b - start_min] = bi
            bi += 1
    return m


def build_schedule(accounts, order=None, dist=None, now=None, rng=None, prefs=None):
    """调度 v2（S1 demo）：统一填充框架。

    排序维度 × 分布维度（2×2）：
    - 顺序×均匀：线性填块（第 i 账号 → 第 i/K 块，先到先签）
    - 随机×均匀：打乱后循环填块（每块人数均衡、铺满窗口）
    - 顺序×正态：z_i 锚点（hash(phone)）稳定作息 + 钟形
    - 随机×正态：每天重抽分位（重排 + 钟形，防风控最强）
    自选优先（S2）：prefs 传入 {phone: {slot_min, updated_at}} 时，自选账号固定所选片
    （片内等分），片满先到先得（updated_at 早者留），溢出双向就近顺延；未选走四组合。
    安全底座：首尾缓冲有效窗口、块容量顺延、块内等分 + 抖动、
    σ_eff 封顶、反射兜底、n≤小人数免分块、超容量压缩模式。

    参数（None → 读环境变量，见 _schedule_config）：
    order: "sequence"|"random"；dist: "uniform"|"normal"
    now: 注入当天日期（默认 clock.now()）；rng: 注入随机源（测试固定 seed）
    prefs: 自选 {phone: {"slot_min": int, "updated_at": str}}；None → 总开关开时读 db
    返回 {phone: datetime}。
    """
    cfg = _schedule_config()
    order = (order or cfg["order"]).strip().lower()
    dist = (dist or cfg["dist"]).strip().lower()
    if order not in ("sequence", "random"):
        order = "sequence"
    if dist not in ("uniform", "normal"):
        dist = "uniform"
    rng = rng or random.Random()
    now = now or clock.now()

    # 用户自暂停账号不参与调度（零占位；执行侧 run_queue_retry 也会跳过）
    accounts = [a for a in accounts if not getattr(a, "user_paused", False)]
    n = len(accounts)
    if n == 0:
        return {}
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    blocks, eff_lo, eff_hi = _schedule_blocks(cfg)
    span = eff_hi - eff_lo
    if not blocks:  # 纵深防御：任何原因导致无块（理论上已被 _schedule_blocks 回退兜底）
        logger.error("有效签到窗口为空，无法生成调度计划（请检查签到窗口与缓冲配置）")
        return {}

    # 小人数复用同一分块机制（统一逻辑，后续加人行为连续，无需特判）：
    # 顺序×均匀 = 线性填块（n=2 → 两人同块等分）；随机×均匀 = 循环填块；正态 = 采样落块
    # 容量：块数 × K；超出 → 压缩模式（K 放大到能容纳所有人，间隔下限告警）
    cap = len(blocks) * cfg["block_cap"]
    k = cfg["block_cap"] if n <= cap else math.ceil(n / len(blocks))
    if n > cap:
        logger.warning(
            "压缩模式: %d 个账号超出块容量 %d，块容量放大至 %d（间隔 ≈ %.1fs）",
            n, cap, k, (span * 60) / n,
        )

    # 自选优先占块（S2）：先到先得 + 溢出双向就近顺延
    chosen = {}  # phone -> bi
    if prefs is None:
        prefs = {}
        if cfg["allow_time_pref"]:
            try:
                prefs = db.get_time_prefs()
            except Exception as e:
                logger.warning("读取自选时间失败（忽略，走自动分配）: %s", e)
    if prefs:
        # 对抗性审查补：只保留当前账号集合内的 pref（换号/删号后的孤儿不占容量）
        valid_phones = {a.phone for a in accounts}
        slot_to_bi = _slot_to_bi(cfg)
        by_slot = {}
        for phone, p in prefs.items():
            if phone not in valid_phones:
                continue
            try:
                slot = int(p.get("slot_min", -1))
            except (TypeError, ValueError):
                continue
            # 2026-08-28 审查 F4：原校验 `0 <= slot < span`（span = 有效窗口宽度），
            # 而 _slot_to_bi 的键范围是完整窗口——窗口长度非 5 分钟整数倍时
            # （如 06:30~07:52），末尾片在 Web 端可点选、此处却被判"落窗外"
            # 而静默回退自动分配。改以 _slot_to_bi 的成员性为准（与 Web 端
            # _pref_slots 同一套可用性判定）。
            if slot not in slot_to_bi:  # 片无效/落窗外 → 回退自动分配
                # 留痕——退化窗口下自选片被丢弃此前完全静默
                logger.warning(f"[{phone}] 自选时间片 {slot} 不在今日可选范围，回退自动分配")
                continue
            by_slot.setdefault(slot, []).append((str(p.get("updated_at", "")), phone))
        filled = [0] * len(blocks)
        overflow = []
        for slot, items in sorted(by_slot.items()):
            items.sort()  # updated_at 升序 → 先到先得
            bi = slot_to_bi.get(slot)
            if bi is None:
                for _u, phone in items:
                    overflow.append((slot, phone))
                continue
            for _u, phone in items:
                if filled[bi] < k:
                    chosen[phone] = bi
                    filled[bi] += 1
                else:
                    overflow.append((slot, phone))
        for slot, phone in overflow:  # 溢出：双向就近顺延（±1 块 → ±2 块 → …）
            base_bi = slot_to_bi.get(slot)
            if base_bi is None:
                continue
            bi = _nearest_available(base_bi, filled, blocks, k)
            if bi is not None:
                chosen[phone] = bi
                filled[bi] += 1
        if overflow:
            logger.info("自选顺延: %d 个账号所选时段已满，已就近调整到附近时段", len(overflow))

    # 排序维度：决定账号→位置的映射是否每天重排（自选账号不参与）
    ordered = [a for a in accounts if a.phone not in chosen]
    if order == "random":
        rng.shuffle(ordered)

    # 分布维度：uniform = 确定性位置；normal = 钟形采样位置
    mu = sigma = None
    if dist == "normal" and ordered:
        if order == "random":
            zs = [rng.gauss(0, 1) for _ in ordered]
            rng.shuffle(zs)
            zmap = {acc.phone: zs[i] for i, acc in enumerate(ordered)}
        else:
            zmap = {acc.phone: _anchor_z(acc.phone) for acc in ordered}
        # μ/σ 每天采样一次、全体共享（对抗性审查 2026-08-15：原实现在循环内每账号
        # 重采样，偏离设计"高峰中心每日一次全体共享"，导致分布趋平/作息漂移放大）
        mu = eff_lo + span * rng.uniform(cfg["mu_min_pct"], cfg["mu_max_pct"]) / 100.0
        sigma = _sigma_eff(
            span * rng.uniform(cfg["sigma_min_pct"], cfg["sigma_max_pct"]) / 100.0,
            n, span,
        )

    # 阶段 1：分配块归属（容量满向后顺延；自选已占位）
    assign = dict(chosen)
    filled = [0] * len(blocks)
    for phone in assign:
        filled[assign[phone]] += 1
    for rank, acc in enumerate(ordered):
        if dist == "uniform":
            bi = (rank // k) if order == "sequence" else (rank % len(blocks))
        else:
            x = mu + sigma * zmap[acc.phone] + rng.gauss(0, 2)  # 个人小抖动 N(0, 2min)
            # 反射回有效窗口（while 兜底，失败回退均匀随机）
            for _ in range(10):
                if x < eff_lo:
                    x = 2 * eff_lo - x
                elif x > eff_hi:
                    x = 2 * eff_hi - x
                else:
                    break
            else:
                x = rng.uniform(eff_lo, eff_hi)
            # 落块：按块边界定位（与 _schedule_blocks 的块一致；edge 掐掉首块时
            # (x - start_min)//5 会错位，改用边界匹配，杜绝越界/串块）
            bi = next((i for i, (lo, hi) in enumerate(blocks) if lo <= x < hi), None)
            if bi is None:
                bi = 0 if x < blocks[0][0] else len(blocks) - 1
        bi = _next_available(bi, filled, blocks, k)
        assign[acc.phone] = bi
        filled[bi] += 1

    # 阶段 2：块内等分 + 抖动（基于块内实际人数 m，避免人数少时挤前段）
    schedule = {}
    for bi, (lo, hi) in enumerate(blocks):
        phones = [p for p in assign if assign[p] == bi]
        m = len(phones)
        if m == 0:
            continue
        dur = hi - lo
        for j, p in enumerate(phones):
            t = lo + dur * (j + 0.5) / m + rng.uniform(0, min(0.8, dur / m / 2))
            schedule[p] = _minute_to_dt(base, min(t, hi - 0.001))
    return schedule

# ---------------------------------------------------------------------------
# 签到窗口是否已关闭（口径唯一源：yiban.window，与 _schedule_blocks 的 horizon 同源）
# ---------------------------------------------------------------------------
def _window_closed(sch_cfg, now_dt):
    """签到窗口是否已关闭（口径唯一源：yiban.window，与 _schedule_blocks 的 horizon 同源）。

    此前本函数只按 sign_end - edge_back 算，而 _schedule_blocks 在"有效窗口被裁剪
    吃空"时会回退到默认窗口——于是出现"有 80 分钟的完整计划、却整轮判时段已结束、
    零请求"。现两处都走 window.bounds（含同一套回退）。
    """
    return window.bounds(sch_cfg).is_closed(now_dt)
