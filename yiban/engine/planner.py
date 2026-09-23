# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
每日一次的计划生成器（Planner）：把账号按双粒度时间分片铺进有效窗口——对外仍是
5 分钟自选片，引擎侧是 1 分钟分片 × 1 秒微槽 × 槽内相位——再叠加 HRW 分工，得到
`(vshard, owner, run_at)`，并批量幂等落库 `sign_tasks`。

**归属**
`yiban.engine` 的调度 v3 计划层，落实"计划与执行分离"：本模块只产出计划（`build_plan`
是纯函数）与落库；执行体只消费队列，不再各自排期。

**复用**
`build_plan` 可重放（同输入逐字段同输出），影子期（dry_run）与审计重放直接调它；
`write_plan` / `has_plan` / `plan_stats` 分别是落库、降级判定与可观测入口；
`slot_width_ms` 是压缩模式的判定口径（落库元数据与执行体读同一处）。

**通信**
输入：账号序列（只读 `.phone`，可选 `.user_paused`）、业务日 `day`、执行体身份串列表；
配置经 `schedule.planner_config()` 取（窗口/裁剪/三模式/μσ/bucket_rate/executors）。
输出：`[PlanRow, ...]`（dict）或落库行数；`plan_stats` 回摘要 dict。
调用谁：`yiban.window.bounds`（窗口唯一口径）、`yiban.engine.hrw`（分工与哈希）、
`yiban.engine.schedule`（配置、`_sigma_eff`、自选片成员性判定）、
`yiban.store.queue_store`（队列库连接与 `sign_tasks` 的 state 词汇）、
`yiban.store.clock_meta`（计划元数据）。
谁调用：v3 执行体启动时用 `has_plan` 判"无计划则降级动态领取"；运维与影子期用
`plan_stats` 对比落点分布与容量。本模块当前尚无调用点。
"""
import datetime
import logging
import math
import statistics

from yiban import clock, window
from yiban.engine import hrw, schedule
from yiban.masking import mask_phone
from yiban.store import clock_meta, queue_store

logger = logging.getLogger("yiban")

#: 一级分片宽度（秒）：引擎侧最小的时间切片单位
SLICE_SEC = 60
#: 二级微槽宽度（秒）：每片 60 槽；超容量时压缩到 0.5s（每片 120 槽）
SLOT_SEC = 1.0
SLOT_SEC_COMPRESSED = 0.5
#: 1s 槽宽下每片的槽数（= 该片的设计容量：1 槽 1 账号）
SLOTS_PER_SLICE = 60
#: 计划元数据键：槽宽（毫秒）。执行体不必感知压缩，读它即可
SLOT_WIDTH_META_KEY = "scheduler_v3_slot_width_ms"
#: 计划行的 priority 缺省值（重试 +1 / 手动 0 由运行期改）
PRIORITY_DEFAULT = 5
STATE_PENDING = queue_store.STATE_PENDING


def _u(*parts):
    """`[0,1)` 均匀量：`hrw._h` 取高 53 位（同一 blake2b 口径，禁内置 `hash`）。

    相位、正态分位、随机排序键都从它派生——全部只吃 `(phone, day, 用途串)`，
    故同一 `(phone, day)` 的落点在任何进程、任何时刻都可重放。
    """
    return (hrw._h(*parts) >> 11) / float(1 << 53)


def _day_str(day):
    """业务日归一化为 `YYYY-MM-DD`（接受 str / date / datetime）。"""
    if isinstance(day, (datetime.datetime, datetime.date)):
        return day.strftime("%Y-%m-%d")
    return str(day).strip()


def _phones(accounts):
    """账号序列 → 手机号列表：剔用户自暂停账号（零占位），按首次出现去重并保持顺序。

    顺序即分层抖动的"账号序号 `i`"——它必须跨天稳定（`slice_i = i mod N_slices`），
    故用调用方给的顺序，**不**按哈希排名（那样跨天会把片序号一起打乱）。
    """
    out, seen = [], set()
    for acc in accounts or ():
        phone = getattr(acc, "phone", None)
        if phone is None:
            phone = acc.get("phone") if isinstance(acc, dict) else acc
        phone = str(phone)
        if not phone or phone in seen or getattr(acc, "user_paused", False):
            continue
        seen.add(phone)
        out.append(phone)
    return out


def _span(cfg):
    """有效窗口的分钟边界 `(eff_lo, eff_hi)`——唯一口径在 `yiban.window`。"""
    win = window.bounds(cfg)
    return win.lo_min, win.hi_min


def _slice_count(cfg):
    """有效窗口内的 1 分钟分片数（向下取整，至少 1 片）。"""
    lo, hi = _span(cfg)
    return max(1, int((hi - lo) * 60 // SLICE_SEC))


def slot_width_ms(n, cfg=None):
    """计划的槽宽（毫秒）：N 超过槽位总容量（分片数 × 每片槽数）时 1s → 0.5s。

    容量 = 分片数 × 每片槽数（1s 时 60）。超过就把槽宽减半、槽数翻倍，而不是落一条
    "压缩告警"了事；实际槽宽由 `write_plan` 写进 `app_meta`，执行体读它即可。
    """
    cfg = cfg or schedule.planner_config()
    if n > _slice_count(cfg) * SLOTS_PER_SLICE:
        return int(SLOT_SEC_COMPRESSED * 1000)
    return int(SLOT_SEC * 1000)


def _pref_slices(slot_min, cfg, eff_lo, eff_hi, n_slices, slot_to_bi):
    """自选 5 分钟片 → 它覆盖的 1 分钟候选分片（升序）；空列表 = 该片今日不可用。

    可用性判定走 `schedule._slot_to_bi`（与 web `_pref_slots` 同一口径），本函数只把
    "片"切成 1 分钟分片。两处若各写一份判定，末尾片会出现"网页可点选、计划侧静默丢弃"。
    """
    if slot_min not in slot_to_bi:
        return []
    start_min = cfg["sign_start"][0] * 60 + cfg["sign_start"][1]
    b = start_min + slot_min
    k_lo = math.floor(b - eff_lo)
    k_hi = math.ceil(b + 5 - eff_lo)
    return [k for k in range(max(0, k_lo), min(n_slices, k_hi))
            if eff_lo + k < b + 5 and eff_lo + k + 1 > b]


def _nearest_free(cands, k0, filled, cap):
    """候选分片内就近找未满（同距离优先更早的片）——v2 `_nearest_available` 的同语义。"""
    for d in range(len(cands)):
        for k in (k0 - d, k0 + d):
            if k in cands and filled[k] < cap:
                return k
    return None


def _spill_block(slot_min, cfg, eff_lo, eff_hi, n_slices, slot_to_bi, filled, cap, k0):
    """外层溢出：向邻近 5 分钟片整体顺延（±5min → ±10min → …），同距离优先更早的片。

    只有自选片内 5 个 1 分钟分片全满才会走到这里，故跨片距离与 v2 完全一致。
    """
    start_min = cfg["sign_start"][0] * 60 + cfg["sign_start"][1]
    end_min = cfg["sign_end"][0] * 60 + cfg["sign_end"][1]
    blocks = int((end_min - start_min) // 5) + 1
    for d in range(1, blocks + 1):
        for s2 in (slot_min - 5 * d, slot_min + 5 * d):
            cands = _pref_slices(s2, cfg, eff_lo, eff_hi, n_slices, slot_to_bi)
            if cands and any(filled[k] < cap for k in cands):
                return _nearest_free(cands, k0, filled, cap)
    return None


def _density(n, cfg, day, span_sec):
    """正态模式的密度整形 → `(mu_min, sigma_min, alpha, phi_max)`。

    φ 是**归一化**密度（∫φ = 1），峰值到达速率 = `N × φ_max`，受出口令牌桶 Λ 封顶：
    只封 σ 不封峰值速率时，中段会形成相对突发。违反约束时把正态与均匀按 α 混合
    **压平峰值**，μ/σ 一律不动——改 σ 会把"作息形状"一起改掉。连均匀密度都超过 Λ 时
    α 取 1（能做的只有压到最平），余下的缺口属于出口令牌桶。
    """
    span_min = span_sec / 60.0
    mu_pct = cfg["mu_min_pct"] + _u(day, "mu") * (cfg["mu_max_pct"] - cfg["mu_min_pct"])
    sg_pct = cfg["sigma_min_pct"] + _u(day, "sigma") * (cfg["sigma_max_pct"] - cfg["sigma_min_pct"])
    mu_min = span_min * mu_pct / 100.0
    sigma_min = schedule._sigma_eff(span_min * sg_pct / 100.0, n, span_min)
    phi_norm = 1.0 / (max(sigma_min, 1e-6) * 60.0 * math.sqrt(2 * math.pi))
    phi_flat = 1.0 / span_sec
    lam = max(float(cfg["bucket_rate"]), 1e-9)
    peak_norm, peak_flat = n * phi_norm, n * phi_flat
    if peak_norm <= lam:
        alpha = 0.0
    elif peak_flat >= lam:
        alpha = 1.0
    else:
        alpha = (peak_norm - lam) / (peak_norm - peak_flat)
    return mu_min, sigma_min, alpha, (1 - alpha) * phi_norm + alpha * phi_flat


def _normal_off(phone, day, mu_min, sigma_min, alpha, span_min):
    """正态模式的落点（相对有效窗口起点的分钟数）。

    反射兜底（v2 同款）：越界就按窗口镜像折回，而不是丢弃重抽——重抽会让同一账号
    在不同进程里取到不同落点，计划就不再是纯函数。
    """
    if _u(phone, day, "flat") < alpha:
        x = _u(phone, day, "uniform") * span_min
    else:
        x = mu_min + sigma_min * statistics.NormalDist().inv_cdf(_u(phone, day, "normal"))
    for _ in range(10):
        if x < 0.0:
            x = -x
        elif x > span_min:
            x = 2 * span_min - x
        else:
            break
    else:
        x = _u(phone, day, "reflect") * span_min
    # 上界收在窗口内侧：落点恰在 eff_hi 上会越出有效窗口（槽内相位必须 < 槽宽）
    return min(max(x, 0.0), span_min - 1e-6)


def _slot_of(phone, day, slots):
    """片内微槽：`H(phone ‖ day ‖ "slot") mod 槽数`（确定性哈希，不用内置 hash）。"""
    return hrw._h(phone, day, "slot") % slots


def _phase_of(phone, day, slot_sec):
    """槽内相位：`U(0, 槽宽)`（当日密钥就是 day，跨天自动重排）。"""
    return _u(phone, day, "phase") * slot_sec


def _split_off(off_min, n_slices, slots, slot_sec):
    """窗口内偏移（分钟）→ `(分片, 微槽, 槽内相位)`，与 `_stamp_at` 互逆。"""
    off = max(0.0, off_min) * 60.0
    k = min(n_slices - 1, int(off // SLICE_SEC))
    rem = off - k * SLICE_SEC
    j = min(slots - 1, int(rem / slot_sec))
    return k, j, rem - j * slot_sec


def _stamp_at(base, eff_lo, k, j, phase, slot_sec):
    """`(分片, 微槽, 相位)` → `YYYY-MM-DD HH:MM:SS.mmm`（毫秒精度，字符串可直接比较）。

    与 `queue_store._lease_until` 同格式：计划列 `run_at`、租约列与领取比较都是
    字符串序，格式不一致会让"到点"判定静默错位。
    """
    t = base + datetime.timedelta(minutes=eff_lo,
                                  seconds=k * SLICE_SEC + j * slot_sec + phase)
    return t.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _read_prefs():
    """读全量自选片（总开关开启时才读）；读失败按"无人自选"继续，不阻断计划。"""
    from yiban.store import db
    try:
        return db.get_time_prefs()
    except Exception as e:
        logger.warning("读取自选时间失败（忽略，走自动分配）: %s", e)
        return {}


def _place_prefs(phones, day, prefs, cfg, eff_lo, eff_hi, n_slices, slots, filled,
                 placed, slot_sec):
    """自选优先占位：先到先得（`updated_at` 升序）→ 片内就近顺延 → 邻近 5 分钟片溢出。

    返回被钉住的手机号集合（其余走自动分配）。片容量 = 槽数（1 槽 1 账号），且**只在
    自选账号之间判定**：自由账号按 `i mod N_slices` 均衡（分层采样），超出槽位数即
    多账号共槽（3 万账号 ≈7.1 账号/槽）。
    """
    if not prefs:
        return set()
    slot_to_bi = schedule._slot_to_bi(cfg)
    valid = set(phones)
    by_slot = {}
    for phone, p in prefs.items():
        if phone not in valid:
            continue                        # 换号/删号后的孤儿不占容量
        try:
            slot = int(p.get("slot_min", -1))
        except (TypeError, ValueError):
            continue
        by_slot.setdefault(slot, []).append((str(p.get("updated_at", "")), phone))
    pinned = set()
    for slot in sorted(by_slot):
        cands = _pref_slices(slot, cfg, eff_lo, eff_hi, n_slices, slot_to_bi)
        if not cands:
            logger.warning("自选时间片 %s 不在今日可选范围，回退自动分配", slot)
            continue
        for _, phone in sorted(by_slot[slot]):     # updated_at 升序 → 先到先得
            k0 = cands[hrw.vshard_of(phone, day, len(cands))]   # 按 H(phone‖day) 取其一
            k = _nearest_free(cands, k0, filled, slots)
            if k is None:
                k = _spill_block(slot, cfg, eff_lo, eff_hi, n_slices, slot_to_bi,
                                 filled, slots, k0)
            if k is None:
                logger.warning("[%s] 自选时段已满且邻近时段无空位，回退自动分配",
                               mask_phone(phone))
                continue
            filled[k] += 1
            placed[phone] = (k, _slot_of(phone, day, slots), _phase_of(phone, day, slot_sec))
            pinned.add(phone)
    return pinned


def _place_free(phones, pinned, day, order, dist, cfg, n_slices, slots, slot_sec,
                span_sec, placed):
    """自由账号落点：分层抖动为主，`dist=normal` 时改为受速率约束的密度采样。

    `order=sequence` 用输入顺序分层（跨天稳定、每片人数零方差）；`order=random` 先按
    `H(phone‖day)` 排序再分层——是"当天重排的均匀序列"，不是逐账号独立抽样，故
    t=0 不会齐发（NHPP 生成器的等价物）。
    """
    free = [p for p in phones if p not in pinned]
    if order == "random":
        free.sort(key=lambda p: (_u(p, day, "rank"), p))
    profile = _density(len(phones), cfg, day, span_sec) if dist == "normal" and free else None
    for i, phone in enumerate(free):
        if profile is None:
            placed[phone] = (i % n_slices, _slot_of(phone, day, slots),
                             _phase_of(phone, day, slot_sec))
        else:
            x = _normal_off(phone, day, profile[0], profile[1], profile[2], span_sec / 60.0)
            placed[phone] = _split_off(x, n_slices, slots, slot_sec)


def build_plan(accounts, day, executors, *, order=None, dist=None, now=None,
               prefs=None, v=None, cfg=None):
    """生成当日全量计划（纯函数，不落库）。返回 `[PlanRow, ...]`（按 run_at 排序）。

    PlanRow = {"phone","day","vshard","owner","run_at","priority","state","epoch"}
    - `run_at` 为 "YYYY-MM-DD HH:MM:SS.mmm"（秒级微槽 + 亚秒相位，字符串可直接比较）；
    - `state` 恒为 `pending`、`priority` 恒为 5（重试 +1 / 手动 0 由运行期改）；
    - **与 K 无关**：改 `executors` 只改 `owner`，不改任何 `run_at`。

    `accounts` 只需 `.phone`（可选 `.user_paused`）；`day` 缺省取 `now`（默认
    `clock.now()`）的日期；`prefs` 为 `None` 且自选总开关开启时读库；`v` 缺省按
    规模选虚分片数（`hrw.v_for`）。
    """
    cfg = cfg or schedule.planner_config()
    order = (order or cfg["order"]).strip().lower()
    dist = (dist or cfg["dist"]).strip().lower()
    if order not in ("sequence", "random"):
        order = "sequence"
    if dist not in ("uniform", "normal"):
        dist = "uniform"
    day = _day_str(day if day else (now or clock.now()))
    phones = _phones(accounts)
    if not phones:
        return []
    eff_lo, eff_hi = _span(cfg)
    span_sec = (eff_hi - eff_lo) * 60.0
    n_slices = _slice_count(cfg)
    slot_sec = slot_width_ms(len(phones), cfg) / 1000.0
    slots = max(1, round(SLICE_SEC / slot_sec))
    if prefs is None:
        prefs = _read_prefs() if cfg.get("allow_time_pref") else {}

    placed = {}
    filled = [0] * n_slices
    pinned = _place_prefs(phones, day, prefs, cfg, eff_lo, eff_hi, n_slices, slots,
                          filled, placed, slot_sec)
    _place_free(phones, pinned, day, order, dist, cfg, n_slices, slots, slot_sec,
                span_sec, placed)

    v = v or hrw.v_for(len(phones))
    base = datetime.datetime.strptime(day, "%Y-%m-%d")
    rows = []
    for phone in phones:
        k, j, phase = placed[phone]
        vshard = hrw.vshard_of(phone, day, v)
        rows.append({
            "phone": phone,
            "day": day,
            "vshard": vshard,
            "owner": hrw.owner_of(vshard, executors, day),
            "run_at": _stamp_at(base, eff_lo, k, j, phase, slot_sec),
            "priority": PRIORITY_DEFAULT,
            "state": STATE_PENDING,
            "epoch": 0,
        })
    rows.sort(key=lambda r: (r["run_at"], r["phone"]))
    logger.info("计划：%d 行 / %d 分片 / 槽宽 %dms / 执行体 %d / %s×%s",
                len(rows), n_slices, int(slot_sec * 1000), len(executors or ()), order, dist)
    return rows


def write_plan(rows, day=None):
    """幂等落库：单事务 `executemany` + `INSERT OR IGNORE`，按 `(phone, day)` 主键去重。

    返回**实际写入**行数（被 IGNORE 的重复行不计），故 Planner 崩溃后直接重跑即可。
    已存在的行一律不覆盖（`state`/`result` 里的当日结论比计划新）；同时把槽宽写进
    `app_meta`——槽宽由 `(行数, 当前配置)` 经 `slot_width_ms` 重新判定（与 `build_plan`
    同一判定函数，不另存一份口径），执行体读它即可知道是否处于压缩模式。库不可用时
    **抛异常**——失败语义必须显式，调用方据此降级（计划层故障不得影响签到）。
    """
    items = list(rows or ())
    if not items:
        return 0
    created = clock.ts()
    params = [
        (r["phone"], day or r["day"], r["vshard"], r["owner"], r["run_at"],
         r.get("priority", PRIORITY_DEFAULT), r.get("state", STATE_PENDING), created)
        for r in items
    ]
    sql = ("INSERT OR IGNORE INTO sign_tasks (phone, day, vshard, owner, run_at, "
           "priority, state, created_at) VALUES (?,?,?,?,?,?,?,?)")
    try:
        conn, lock = queue_store._queue_conn()
        with lock:
            cur = conn.executemany(sql, params)
            conn.commit()
            written = cur.rowcount
    except Exception as e:
        logger.error("写入当日计划失败（执行体应降级动态领取）: %s", e)
        raise
    clock_meta.set_meta(SLOT_WIDTH_META_KEY, slot_width_ms(len(items)))
    return written


def has_plan(day):
    """当日是否已有计划行——执行体启动时的降级判定。

    库不可用 / 表未落地一律回 `False`：调用方据此退回 v17 的动态领取路径，而不是空转。
    """
    try:
        conn, lock = queue_store._queue_conn()
        with lock:
            row = conn.execute("SELECT 1 FROM sign_tasks WHERE day=? LIMIT 1",
                               (_day_str(day),)).fetchone()
        return row is not None
    except Exception as e:
        logger.warning("读取当日计划失败（按无计划处理，走动态领取）: %s", e)
        return False


def _minute_of_day(stamp):
    """`YYYY-MM-DD HH:MM:SS.mmm` → 当天分钟数（浮点）。"""
    return int(stamp[11:13]) * 60 + int(stamp[14:16]) + float(stamp[17:]) / 60.0


def plan_stats(rows, cfg=None, day=None):
    """计划摘要：总行数、按 vshard 的 owner 分布、落点直方图（按 5 分钟片分桶）。

    直方图桶键 = 自选片号（相对窗口起点的 5 分钟格，与 `time_prefs.slot_min` 同号），
    故影子期（dry_run）能把"计划分布"与现网实际落点逐格对比；`peak_per_sec` 是同一
    秒内的实际落点数（"任意 1 秒内实际请求数不超过计划密度"这条不变量的观测量）。
    `cfg` 缺省取当前配置，`dist=normal` 时另附密度整形结果（φ_max / 压平系数 α /
    峰值速率），供容量与分布一起核对。
    """
    items = list(rows or ())
    cfg = cfg or schedule.planner_config()
    day = _day_str(day or (items[0]["day"] if items else clock.today()))
    eff_lo, eff_hi = _span(cfg)
    span_sec = (eff_hi - eff_lo) * 60.0
    start_min = cfg["sign_start"][0] * 60 + cfg["sign_start"][1]
    owners, shards, hist, per_sec = {}, {}, {}, {}
    for r in items:
        owners[r["owner"]] = owners.get(r["owner"], 0) + 1
        shards[r["vshard"]] = shards.get(r["vshard"], 0) + 1
        stamp = r["run_at"]
        per_sec[stamp[:19]] = per_sec.get(stamp[:19], 0) + 1
        key = int((_minute_of_day(stamp) - start_min) // 5) * 5
        hist[key] = hist.get(key, 0) + 1
    dist = (cfg["dist"] or "uniform").strip().lower()
    if dist == "normal" and items:
        mu_min, sigma_min, alpha, phi = _density(len(items), cfg, day, span_sec)
    else:
        mu_min, sigma_min, alpha, phi = None, None, 0.0, 1.0 / span_sec
    return {
        "day": day,
        "n": len(items),
        "owners": owners,
        "shards": shards,
        "hist": hist,
        "peak_per_sec": max(per_sec.values(), default=0),
        "dist": dist,
        "slot_width_ms": slot_width_ms(len(items), cfg),
        "n_slices": _slice_count(cfg),
        "mu_min": mu_min,
        "sigma_min": sigma_min,
        "flatten_alpha": alpha,
        "phi_max": phi,
        "rate_peak": len(items) * phi,
        "lam": max(float(cfg["bucket_rate"]), 1e-9),
    }
