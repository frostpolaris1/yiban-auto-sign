# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""一轮队列：把账号列表跑成一轮签到（含分级重试、领取池分工、窗口收尾）。

两种形态共用同一份重试分级与领取池：调度 v2 的**时间驱动堆队列**（自动错峰，失败
账号经 `_next_retry_at` 重新采样落点后非阻塞重插）与**手动队列**（`--only`/兜底执行体
传入空 schedule，失败回队尾并等满最小间隔）。多执行体的分工靠 `yiban/store/claims.py`
的账号级租约：领不到即"别人正在做它"，本进程不碰（不写状态、不重试、不告警）。

跨模块调用纪律见包说明：跨模块一律走模块属性访问。
"""
import heapq
import logging
import os
import random
import time
from datetime import datetime, timedelta

from yiban import clock, egress, notify
from yiban import status as yiban_status
from yiban.engine import alerts, state_io
from yiban.engine import attempts as attempts_mod
from yiban.engine import schedule as schedule_mod
from yiban.masking import mask_phone as _mask_phone
from yiban.masking import sanitize_text as _sanitize_text
from yiban.store import db

logger = logging.getLogger("yiban")

# 签到模式：sequence（列表顺序，默认）/ random（列表随机打散）。
# 由网页系统设置页写入 .env（YIBAN_SIGN_MODE），run.sh 加载后经环境变量传入。
SIGN_MODE = os.environ.get("YIBAN_SIGN_MODE", "").strip().lower()

# 单次尝试耗时告警阈值（秒）：超此值 → warning + 管理员汇总邮件 + 即时通知
_DEFAULT_SLOW_SIGN_SEC = 30

# 状态码别名（与 yiban.status 同一对象）
STATUS_SUCCESS = yiban_status.STATUS_SUCCESS
STATUS_ALREADY = yiban_status.STATUS_ALREADY
STATUS_NO_TASK = yiban_status.STATUS_NO_TASK
STATUS_PENDING = yiban_status.STATUS_PENDING
STATUS_RETRYING = yiban_status.STATUS_RETRYING
STATUS_SKIPPED_WINDOW = yiban_status.STATUS_SKIPPED_WINDOW
STATUS_SKIPPED_NORANGE = yiban_status.STATUS_SKIPPED_NORANGE
STATUS_NO_POSITION = yiban_status.STATUS_NO_POSITION
STATUS_PAUSED = yiban_status.STATUS_PAUSED
STATUS_USER_CANCELLED = yiban_status.STATUS_USER_CANCELLED

# 状态码 → 日志/日历符号（同一对象，非副本）
STATUS_SYMBOL = yiban_status.SYMBOL

#: 领取池的"当日了结"口径（`state_io._second_run_drop_done` 的剔除集合与 `_settle_claims`
#: 的记 `done` 判据都用它，改一处必须同改另一处）：
#: 这三个状态意味着今天不必再签，其余状态（含窗口外跳过、无点位、失败）都仍开放，
#: 由补签轮或兜底执行体接手。
_CLAIM_DONE_STATUSES = (STATUS_SUCCESS, STATUS_ALREADY, STATUS_NO_TASK)


def _next_retry_at(now_dt, sch_cfg, rng=None):
    """重试落点：在剩余有效窗口的偏早段重新采样。

    - 下界 now + retry_min_interval（防连击）；
    - 上界 eff_hi = sign_end - edge_back（统一截止口径）；
    - 只在前 60% 的剩余窗口里均匀采样：不尾端扎堆、无固定尾序，也不回队尾立即执行；
    - 窗口放不下下一次尝试时返回 None，由调用方走放弃路径。
    """
    rng = rng or random.Random()
    base = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end_min = sch_cfg["sign_end"][0] * 60 + sch_cfg["sign_end"][1]
    eff_hi = base + timedelta(minutes=end_min - sch_cfg["edge_back_sec"] / 60.0)
    lo = now_dt + timedelta(seconds=sch_cfg["retry_min_interval"])
    if lo >= eff_hi:
        return None
    window = (eff_hi - lo).total_seconds()
    return lo + timedelta(seconds=rng.uniform(0, window * 0.6))


def run_queue_retry(accounts, notify_url, start_delay_max, gap_max, schedule=None, cred_state=None,
                    event_sink=None, reclaim=False, delegated=None):
    """轮询队列 + 分散重试执行全部账号签到。

    流程（schedule 为空=手动签到）：按签到模式（列表顺序 / 列表随机）
    确定执行顺序逐个尝试；失败的账号不立即重试，放入队尾等待下一轮；
    每账号总尝试次数受 `attempts._retry_budget` 分级控制（确定性认证失败 1 次、
    风控类最多 2 次，其他最多 `attempts.MAX_ATTEMPTS` 次）；同一账号两次尝试间隔
    不小于 `attempts.RETRY_MIN_INTERVAL` 秒，避免连击。相邻账号请求间隔对齐到不小于
    gap_max（与自动调度、容量预估同一「最小间隔」语义）。

    schedule 非空（自动错峰模式，时间驱动队列）：按 {phone: datetime} 时间点到点执行
    （已过点立即执行），不再叠加启动/账号间随机延迟；失败的账号经 _next_retry_at 重新
    采样到剩余有效窗口的偏早段后非阻塞重插（不再回队尾 + 阻塞等待），窗口不足时明确
    放弃；相邻请求间隔受 min_exec_gap / exec_gap_min 兜底；截止保护统一按
    eff_hi（sign_end - edge_back）。

    cred_state（账密熔断）：暂停中的账号零请求跳过（半开试探日除外）；
    执行后更新凭据失败计数（成功清除、凭据类失败累计、达阈值暂停）。

    event_sink（可选）：签到事件落库回调——每次尝试/状态迁移调用一次，
    传入 dict 行（sign_events 表字段）。None 时不收集；回调异常一律吞掉，
    事件留痕绝不影响签到主流程。

    reclaim（多执行体）：True 时允许重新领取"当日已了结"的账号——只有**手动指定账号**
    才这么传（用户主动点的签到应当照做）。补签轮与兜底 worker 不传：它们接手的是
    "未了结"账号，已了结的账号再登录一次纯属多余的风控暴露。

    **多执行体分工（动态领取 + 账号级租约，见 yiban/store/claims.py）**：每次尝试前
    领取该账号当日的领取记录，领不到即"别的执行体正在做它"→ 本进程不碰（不写状态、
    不重试、不告警）；本轮结束后统一收尾：已了结（成功/已签到/今日无任务）落 `done`，
    其余落 `failed` 并**放开租约**（补签轮/兜底执行体可立刻接手）。执行体进程崩溃时
    领取记录停在 `claimed`，租约到期后由其他执行体接管——不需要人工介入。

    `delegated`（可选出参）：把"不在本执行体范围内"的账号（领不到的那些）收集到
    这个 set 里。汇总与退出码必须据此把它们从"失败"里摘出去——否则每个执行体都会把
    别人的活报成自己的失败。

    返回结果字典 {手机号: (success, message, skip, status)}。
    """
    schedule = schedule or {}
    cred_state = cred_state or {}
    # .env 直配超大值不得把队列睡死——与网页设置侧 3600 上限同口径
    # （该保护原先内置于共享随机延迟 helper，间隔改为确定性对齐后收口到入参处）
    gap_max = min(gap_max, 3600)
    # start_delay_max 只为兼容旧调用签名保留，值不再使用：启动延迟已由
    # 调度 v2 的时间点分布 + 掐头去尾取代
    queue = list(accounts)
    if SIGN_MODE == "random" and not schedule:
        # 列表随机模式：每次运行打乱顺序（打破"固定顺序+固定时刻"的脚本指纹）；
        # 时间点模式下随机性已由 build_schedule 的槽位重排承担，此处不再重复打乱
        random.shuffle(queue)
        logger.debug(f"签到模式: 列表随机（顺序已打散，共 {len(queue)} 个账号）")
    else:
        logger.debug(f"签到模式: 列表顺序（共 {len(queue)} 个账号）")
    attempts = {acc.phone: 0 for acc in accounts}
    results = {}
    first_round = True

    # ---- 领取池（多执行体协调；单执行体形态下永远领得到，行为与旧版一致）----
    # 单执行体形态的身份是稳定槽位名 `single@{主机名}`：跨重启不变，故重启后立刻认领
    # 自己上一轮的在飞账号；代价是同一槽位名不得两台机器同时跑（跨主机靠 @主机名 区分，
    # 同机靠运行锁挡住——单执行体形态由调用方持锁，见 yiban/engine/cli_support.py）。
    executor_id = (os.environ.get("YIBAN_EXECUTOR_ID", "").strip()
                   or egress.single_owner())
    claimed_day = {}   # 本进程领到的账号 → 业务日（跨午夜时逐账号不同）

    def _claim(phone, day):
        """领取该账号当日的工作权；领不到返回 False（别人在做）。

        **库未初始化时直接放行且不碰库**：领取池只是"多执行体协调"的手段，
        而"不碰库"是有意的——否则纯状态文件部署（无 DB）会被这次调用顺手创建
        一个默认库，纯属副作用。
        """
        if not db.is_initialized():
            return True
        try:
            got = db.claim_sign_account(phone, day, executor_id, allow_settled=reclaim)
        except Exception as e:
            # 协调层故障不得让签到停摆（单执行体形态这个池可有可无）
            logger.debug(f"[{phone}] 领取失败（按可执行处理）: {e}")
            got = True
        if got:
            claimed_day[phone] = day
        return got

    def _settle_claims(res):
        """本轮结束后统一收尾本轮领到的账号（只认本轮领过的，避免误写他人在飞的记录）。

        了结口径与展示口径刻意一致：`success/already/no_task` 记为 `done`（当日无需再签），
        其余记为 `failed` 但**未了结**——补签轮与兜底执行体正是为接手它们而存在。
        """
        if not claimed_day:
            return   # 本轮没领过任何账号（库未初始化 / 全被他人领取）
        for ph, (_ok, _msg, _skip, st) in res.items():
            day = claimed_day.get(ph)
            if not day:
                continue
            try:
                if st in _CLAIM_DONE_STATUSES:
                    db.claim_settle(ph, day, executor_id, db.CLAIM_STATE_DONE, str(st))
                else:
                    db.claim_give_up(ph, day, executor_id, str(st))
            except Exception as e:
                logger.debug(f"[{ph}] 收尾领取记录失败（不影响签到结果）: {e}")

    def _emit_event(phone, status, message, dur=None, attempt_no=None):
        """签到事件留痕。

        每次尝试与状态迁移（含重试/跳过）落一行，stage="sign"；探针沿用既有
        stage="probe" 写入口径。异常吞掉——留痕失败不得影响签到主流程。
        """
        if event_sink is None:
            return
        try:
            ts = clock.now().strftime("%Y-%m-%d %H:%M:%S")
            event_sink({
                "ts": ts,
                "phone": phone,
                "status": status,
                "message": _sanitize_text(str(message or ""))[:200],
                "stage": "sign",
                "attempt": attempts.get(phone, 0) if attempt_no is None else attempt_no,
                "dur_sec": dur,
                "finished_at": ts,
            })
        except Exception:
            pass

    def _mark_window_skip(rest_accs):
        """窗口关闭收尾：只把**当日尚无结论**的账号标记为窗口外跳过。

        不覆盖已有结论：本轮（或上一轮补签）已经得出的 failed / no_position 等真实
        原因必须保留——原实现无条件改写，会把"重试没赶上窗口"记成"窗口外"，
        日历上丢掉失败原因，`has_real_failure` 也一起变 False（失败告警被吞掉）。
        补签轮起跑时窗口已关闭同理：整轮零请求却不该改写首轮结论。

        **`pending` 不是结论**：排计划阶段给每个账号都写了"计划 HH:MM"（同一份状态
        文件），若把它当成"已有记录"，窗口外起跑的全量轮会一个账号都进不了 `results`
        ——汇总把它们算成失败（❌ N 失败、退出码 1、发失败邮件），而真相是"一个请求都
        没发"（2026-09-17 测试机实测复现；该行在 `pending` 判定加入前对 base 提交同样）。

        **快照只用于预筛，落盘再 CAS 一次**：`_daily_statuses()` 是无锁快照，从快照
        判"无记录"到写入之间，另一执行体可能刚把真实结论落盘——写走
        `only_if_absent`（锁内再判），CAS 被拒的账号不写、不改 results、不发事件。
        """
        recorded = state_io._daily_statuses()
        for _ra in rest_accs:
            _p = _ra.phone
            if _p in results or recorded.get(_p, "") not in ("", STATUS_PENDING):
                continue
            if not state_io._write_sign_state(_p, STATUS_SKIPPED_WINDOW,
                                              "签到时段已结束", only_if_absent=True):
                # 锁内发现当日已有结论（他执行体刚写入）：不覆盖，保持真实失败可见
                logger.info(f"[{_p}] ⛔ 签到时段已结束，但当日已有结论，跳过写入")
                continue
            results[_p] = (False, "签到时段已结束", True, STATUS_SKIPPED_WINDOW)
            _emit_event(_p, STATUS_SKIPPED_WINDOW, "签到时段已结束")

    # 调度 v2 安全底座参数（schedule 模式）：本地截止保护 + 启动对齐
    sch_cfg = schedule_mod._schedule_config() if schedule else None
    last_done = None  # 上次尝试结束时刻（monotonic），启动对齐用
    # 耗时告警阈值可配（YIBAN_SLOW_SIGN_SEC），每账号每轮最多告警 1 次
    slow_sec = schedule_mod._env_int("YIBAN_SLOW_SIGN_SEC", _DEFAULT_SLOW_SIGN_SEC, 1, 600)
    slow_notified = set()

    # ---- 调度 v2 时间驱动队列：重试重新尊重计划 ----
    # pending: (next_at, seq, acc) 按下次尝试时刻排序；首 attempt 落点=计划时刻（已过点
    # 立即）；重试经 _next_retry_at 重新采样落点后非阻塞重插，不再"回队尾 + 阻塞 sleep"。
    if schedule:
        pending = []
        _seq = 0

        def _push(_acc, _at):
            nonlocal _seq
            heapq.heappush(pending, (_at, _seq, _acc))
            _seq += 1

        _now0 = clock.now()
        for _acc in accounts:
            _t = schedule.get(_acc.phone)
            _push(_acc, _t if _t and _t > _now0 else _now0)
        while pending:
            _at_dt, _seq_no, acc = heapq.heappop(pending)
            phone = acc.phone
            # 每次尝试（含重试）重算 today，跨午夜执行不沿用启动日
            today = clock.now().strftime("%Y-%m-%d")
            now_dt = clock.now()
            # 截止保护（统一 eff_hi 口径）：窗口关闭 → 剩余账号全部跳过
            if schedule_mod._window_closed(sch_cfg, now_dt):
                logger.info(f"[{phone}] ⛔ 签到时段已结束，跳过执行")
                _mark_window_skip([acc] + [r[2] for r in pending])
                break
            # 先判后睡：将跳过的账号（用户自取消/熔断暂停）不睡到时段槽位——
            # 死号排在后段时，此前会先睡满槽位间隔才发现可跳过，把活号挤出窗口
            cred = cred_state.get(phone, {})
            if getattr(acc, "user_paused", False):
                results[phone] = (False, "用户已取消签到", True, STATUS_USER_CANCELLED)
                state_io._write_sign_state(phone, STATUS_USER_CANCELLED, "用户已取消签到")
                _emit_event(phone, STATUS_USER_CANCELLED, "用户已取消签到")
                logger.info(f"[{phone}] ⏹️ 用户已取消签到，跳过执行")
                continue
            # 账密熔断：暂停中的账号零请求直接跳过（半开试探日除外——试探 1 次以验证恢复）
            if cred.get("paused_since") and not attempts_mod._probe_due(cred, today):
                results[phone] = (False, "账密异常已暂停，请修改密码", True, STATUS_PAUSED)
                state_io._write_sign_state(phone, STATUS_PAUSED, "账密异常已暂停（连续失败），请修改密码")
                _emit_event(phone, STATUS_PAUSED, "账密异常已暂停（连续失败），请修改密码")
                logger.info(f"[{phone}] ⏸️ 账密异常已暂停，跳过执行")
                continue
            # 到点执行（已过点立即）；重试落点已由 _next_retry_at 采样
            wait = (_at_dt - now_dt).total_seconds()
            if wait > 0:
                time.sleep(wait)
            # 请求最小间隔兜底：min_exec_gap 与 exec_gap_min（过点账号）取较大值；
            # 账号间隔设置（gap_max）对自动调度同样生效，作为相邻请求间隔下限
            if last_done is not None:
                min_gap = max(
                    sch_cfg["min_exec_gap"],
                    sch_cfg["exec_gap_min"] if wait <= 0 else 0,
                    gap_max,
                )
                gap = min_gap - (time.monotonic() - last_done)
                if gap > 0:
                    logger.debug(f"[{phone}] 间隔对齐: 补 {int(gap)}s（最小 {min_gap}s）")
                    time.sleep(gap)
            # 睡眠/间隔对齐之后**再判一次**窗口：等待期间可能已越过 eff_hi，此时
            # 仍发起请求就落到窗口外（学校侧会拒），且会挤占后面的账号
            if wait > 0 and schedule_mod._window_closed(sch_cfg, clock.now()):
                logger.info(f"[{phone}] ⛔ 等待期间已越过签到时段，跳过执行")
                _mark_window_skip([acc] + [r[2] for r in pending])
                break
            # 领取（放在"要发请求"的最后一步之前：睡到计划时刻的过程中不占租约）
            if not _claim(phone, today):
                logger.debug(f"[{phone}] 已被其他执行体领取，本进程跳过")
                if delegated is not None:
                    delegated.add(phone)
                continue
            attempts[phone] += 1
            logger.debug(f"[{phone}] 🔄 第 {attempts[phone]} 次尝试")
            t0 = time.monotonic()  # 单次尝试耗时起点（慢响应可判）
            success, message, skip, status = attempts_mod.attempt_signin(acc)
            last_done = time.monotonic()  # 启动对齐：记录本次尝试结束时刻
            dur = last_done - t0
            state_io._write_sign_state(phone, status, message, dur=dur)
            _emit_event(phone, status, message, dur=dur)
            # 单次尝试超阈值 → warning + 通知
            if dur > slow_sec and phone not in slow_notified:
                slow_notified.add(phone)
                alerts._alert_slow_sign(phone, dur, slow_sec, status, message, notify_url)
            # 熔断计数：成功清除；凭据类失败累计（含半开试探结果——成功即恢复）
            attempts_mod._update_cred_state(cred_state, phone, success, message, today)
            # 半开试探"凭据健康"判定：签到成功，或已成功登录但被签到时段规则跳过
            # （SKIPPED_WINDOW/NORANGE 发生在登录并拉取任务之后，凭据已被证实可用——
            # 只看 success 会把窗口跳过误判为试探失败，再冻一个试探周期）
            probe_healthy = success or (
                skip and status in (STATUS_SKIPPED_WINDOW, STATUS_SKIPPED_NORANGE)
            )
            if cred.get("paused_since") and probe_healthy:
                if not success:  # success 时 _update_cred_state 已清除；窗口跳过需显式清除
                    cred_state.pop(phone, None)
                logger.info(f"[{phone}] ✅ 半开试探确认账密可用，解除暂停")
            elif cred.get("paused_since") and attempts_mod._probe_due(cred, today):
                # 试探失败：仅凭据类失败顺延试探日——网络类瞬时失败可自愈，
                # 顺延会把状态放大成周级停签，故保持 probe_date 不变、次日再试
                if attempts_mod._is_credential_failure(message):
                    next_probe = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=attempts_mod.PROBE_INTERVAL_DAYS)).strftime("%Y-%m-%d")
                    cred_state[phone]["probe_date"] = next_probe
                    logger.warning(f"[{phone}] ⏸️ 半开试探失败，保持暂停（下次 {next_probe} 试探）")
                else:
                    logger.warning(f"[{phone}] ⏸️ 半开试探遇网络类失败，保持暂停（试探日不变，次日再试）")
            if success:
                results[phone] = (True, message, skip, status)
                logger.info(f"[{phone}] {STATUS_SYMBOL[status]} {message}")
                continue
            # 失败：跳过类不重试；其余按分级重试
            if skip:
                results[phone] = (False, message, True, status)
                logger.info(f"[{phone}] ⛔ {message}（不重试）")
                continue
            max_attempts, clear_cache = attempts_mod._retry_budget(message)
            # 会话缓存联动：风控类（e003/WAF）、"授权设备"、会话陈旧三类当前会话都不可信，
            # 清掉缓存后下一次尝试会走真实登录（attempt_signin 每次新建 client）
            if clear_cache:
                attempts_mod.clear_session_cache_quiet(phone)
            if attempts[phone] >= max_attempts:
                results[phone] = (False, message, False, status)
                if status == STATUS_NO_POSITION:
                    # 无点位=易班侧无数据（任务未配置/当日任务已关闭），非账号/凭据
                    # 问题：管理员无从修复，不按"签到失败"告警轰炸；仅留日志与状态
                    # （独立状态码供展示/统计），补签重试同样无意义。
                    logger.warning(
                        f"[{phone}] 🚫 易班未返回签到点位，当日不签到（重试无意义）: {message}"
                    )
                    continue
                logger.error(f"[{phone}] ❌ 已尝试 {attempts[phone]} 次，放弃: {message}")
                alerts._collect_admin_mail("易班签到失败", f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}")
                if notify.is_configured():
                    alerts.send_notification("易班签到失败", f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}", notify_url)
                alerts.send_user_fail_mail(acc.owner, phone, message)
                continue
            # 重试落点：窗口内重新采样后非阻塞重插；窗口不足 → 放弃
            nxt = _next_retry_at(clock.now(), sch_cfg)
            if nxt is None:
                results[phone] = (False, message, False, status)
                logger.error(f"[{phone}] ❌ 窗口剩余不足，不再重试: {message}")
                alerts._collect_admin_mail("易班签到失败", f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}")
                if notify.is_configured():
                    alerts.send_notification("易班签到失败", f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}", notify_url)
                alerts.send_user_fail_mail(acc.owner, phone, message)
                continue
            # 重试入队统一兜底失败原因：把本次失败 message 原样补进状态/事件/日志三处
            # 出口，原因经 _sanitize_text 防换行/回车注入（与 web 展示一致）。
            state_io._write_sign_state(phone, STATUS_RETRYING, f"待重试（已 {attempts[phone]} 次）: {_sanitize_text(message)}")
            _emit_event(phone, STATUS_RETRYING, f"待重试（已 {attempts[phone]} 次）: {_sanitize_text(message)}")
            _push(acc, nxt)
            logger.warning(f"[{phone}] ⏳ 待重试（已 {attempts[phone]} 次，上限 {max_attempts} 次，{nxt.strftime('%H:%M:%S')} 再试）: {_sanitize_text(message)}")
        _settle_claims(results)
        return results

    while queue:
        acc = queue.pop(0)
        phone = acc.phone
        # 每次尝试（含重试）重算 today，跨午夜执行不沿用启动日
        today = clock.now().strftime("%Y-%m-%d")
        is_first = first_round
        first_round = False
        # 先判后睡：将跳过的账号（用户自取消/熔断暂停）不占账号间隔——
        # 此前先睡满间隔再判跳过，死号排在前段时会白烧窗口
        if getattr(acc, "user_paused", False):
            results[phone] = (False, "用户已取消签到", True, STATUS_USER_CANCELLED)
            state_io._write_sign_state(phone, STATUS_USER_CANCELLED, "用户已取消签到")
            _emit_event(phone, STATUS_USER_CANCELLED, "用户已取消签到")
            logger.info(f"[{phone}] ⏹️ 用户已取消签到，跳过执行")
            continue
        # 账密熔断：暂停中的账号零请求直接跳过（半开试探日除外——试探 1 次以验证恢复）
        cred = cred_state.get(phone, {})
        if cred.get("paused_since") and not attempts_mod._probe_due(cred, today):
            results[phone] = (False, "账密异常已暂停，请修改密码", True, STATUS_PAUSED)
            state_io._write_sign_state(phone, STATUS_PAUSED, "账密异常已暂停（连续失败），请修改密码")
            _emit_event(phone, STATUS_PAUSED, "账密异常已暂停（连续失败），请修改密码")
            logger.info(f"[{phone}] ⏸️ 账密异常已暂停，跳过执行")
            continue
        # 首轮（第一个账号）不等待，后续每个账号（含重试回队）对齐相邻请求的
        # 最小间隔——与铺点路径、容量预估 (avg+gap) 同一「下限」语义。
        if not is_first and last_done is not None:
            gap = gap_max - (time.monotonic() - last_done)
            if gap > 0:
                logger.debug(f"[{phone}] 间隔对齐: 补 {int(gap)}s（最小 {gap_max}s）")
                time.sleep(gap)

        # 领取（与铺点路径同口径；手动指定账号时 reclaim=True，可重签当日已了结的账号）
        if not _claim(phone, today):
            logger.debug(f"[{phone}] 已被其他执行体领取，本进程跳过")
            if delegated is not None:
                delegated.add(phone)
            continue
        attempts[phone] += 1
        logger.debug(f"[{phone}] 🔄 第 {attempts[phone]} 次尝试")

        t0 = time.monotonic()  # 单次尝试耗时起点（慢响应可判）
        success, message, skip, status = attempts_mod.attempt_signin(acc)
        last_done = time.monotonic()  # 启动对齐：记录本次尝试结束时刻
        # 每次尝试结束即更新结构化状态文件（失败回队时显示 🔄 重试中；附耗时 dur）
        dur = last_done - t0
        state_io._write_sign_state(phone, status, message, dur=dur)
        _emit_event(phone, status, message, dur=dur)
        # 单次尝试超阈值 → warning + 通知。节流：每账号每轮最多 1 次（重试连击不刷屏；
        # 最终失败另有失败通知，此处主要覆盖"慢但成功"的接口劣化预警）；
        # 通知失败不影响签到（内部已捕获）。
        if dur > slow_sec and phone not in slow_notified:
            slow_notified.add(phone)
            alerts._alert_slow_sign(phone, dur, slow_sec, status, message, notify_url)
        # 熔断计数：成功清除；凭据类失败累计（含半开试探结果——成功即恢复）
        attempts_mod._update_cred_state(cred_state, phone, success, message, today)
        # 半开试探"凭据健康"判定：签到成功，或已成功登录但被签到时段规则跳过
        # （SKIPPED_WINDOW/NORANGE 发生在登录并拉取任务之后，凭据已被证实可用——
        # 只看 success 会把窗口跳过误判为试探失败，再冻一个试探周期）
        probe_healthy = success or (
            skip and status in (STATUS_SKIPPED_WINDOW, STATUS_SKIPPED_NORANGE)
        )
        if cred.get("paused_since") and probe_healthy:
            if not success:  # success 时 _update_cred_state 已清除；窗口跳过需显式清除
                cred_state.pop(phone, None)
            logger.info(f"[{phone}] ✅ 半开试探确认账密可用，解除暂停")
        elif cred.get("paused_since") and attempts_mod._probe_due(cred, today):
            # 试探失败：仅凭据类失败才顺延试探日（理由同 schedule 分支）
            if attempts_mod._is_credential_failure(message):
                next_probe = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=attempts_mod.PROBE_INTERVAL_DAYS)).strftime("%Y-%m-%d")
                cred_state[phone]["probe_date"] = next_probe
                logger.warning(f"[{phone}] ⏸️ 半开试探失败，保持暂停（下次 {next_probe} 试探）")
            else:
                logger.warning(f"[{phone}] ⏸️ 半开试探遇网络类失败，保持暂停（试探日不变，次日再试）")

        if success:
            results[phone] = (True, message, skip, status)
            # 符号按状态码输出：success/already→✅、no_task→➖（与界面显示一致）
            logger.info(f"[{phone}] {STATUS_SYMBOL[status]} {message}")
            continue

        # 失败：跳过类不重试；其余按分级放回队尾
        if skip:
            results[phone] = (False, message, True, status)
            logger.info(f"[{phone}] ⛔ {message}（不重试）")
            continue

        max_attempts, clear_cache = attempts_mod._retry_budget(message)
        # 会话缓存联动：与队列路径同口径——风控类/"授权设备"/会话陈旧都清缓存，
        # 避免下次尝试复用已被服务端作废的会话
        if clear_cache:
            attempts_mod.clear_session_cache_quiet(phone)
        if attempts[phone] >= max_attempts:
            results[phone] = (False, message, False, status)
            if status == STATUS_NO_POSITION:
                # 同 schedule 分支：无点位非账号/凭据问题，不按"签到失败"告警轰炸。
                logger.warning(
                    f"[{phone}] 🚫 易班未返回签到点位，当日不签到（重试无意义）: {message}"
                )
                continue
            logger.error(f"[{phone}] ❌ 已尝试 {attempts[phone]} 次，放弃: {message}")
            # A 线合并：失败并入任务结束汇总邮件（webhook 仍即时推送）
            alerts._collect_admin_mail(
                "易班签到失败",
                f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}",
            )
            if notify.is_configured():
                alerts.send_notification(
                    "易班签到失败", f"账号: {_mask_phone(phone)}\n原因: {_sanitize_text(message)}", notify_url
                )
            # B 线：向账号归属用户发失败提醒（未开启/未绑定用户则静默跳过）
            alerts.send_user_fail_mail(acc.owner, phone, message)
            continue

        # 放回队尾前的等待：单次 sleep 保证总间隔 ≥ retry_min_interval，
        # 随机部分只用于打散，不允许把最小间隔缩水。
        # 重试入队统一兜底失败原因：把本次失败 message 原样补进状态/事件/日志三处出口，
        # 原因经 _sanitize_text 防换行/回车注入（与 web 展示一致）。
        state_io._write_sign_state(phone, STATUS_RETRYING, f"待重试（已 {attempts[phone]} 次）: {_sanitize_text(message)}")
        _emit_event(phone, STATUS_RETRYING, f"待重试（已 {attempts[phone]} 次）: {_sanitize_text(message)}")
        retry_min_interval = attempts_mod.RETRY_MIN_INTERVAL
        wait = max(retry_min_interval, retry_min_interval - gap_max + random.uniform(0, attempts_mod.RETRY_GAP_MAX))
        logger.debug(f"[{phone}] 重试前等待 {wait:.1f}s（最小 {retry_min_interval}s）")
        time.sleep(wait)
        queue.append(acc)
        logger.warning(f"[{phone}] ⏳ 待重试（已 {attempts[phone]} 次，上限 {max_attempts} 次）: {_sanitize_text(message)}")

    _settle_claims(results)
    return results
