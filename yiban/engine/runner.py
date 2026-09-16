# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""入口与轮次编排：`main(argv=None) -> int`（装载配置 → 分支 → 一轮队列 → 汇总退出码）。

**退出码由本函数返回、不再自己 `sys.exit`**（退出码语义逐字不变，见
`docs/dev/cli.md` §3）：这样 `yiban/cli.py` 与 `scripts/signin.py` 兼容壳可以统一
`sys.exit(main(argv))`，同时保留"返回码"这一可被直接断言的形式。

分支顺序是有意为之，改动前先读注释：兜底常驻 → 多执行体监督 → 补签轮判定（只读本地
状态）→ SIGTERM 兜底 → 加载账号 → 探针 → 零账号守卫 → `--only` 过滤 → `--check-config`
→ 周末门/全局暂停 → 补签轮定向重跑 → 进程级单实例锁 → 一轮队列 → 汇总与退出码。

跨模块调用纪律见包说明：跨模块一律走模块属性访问。
"""
import argparse
import json
import logging
import os
import signal
import sys
from datetime import datetime, timedelta

from yiban import __version__ as RELEASE_VERSION
from yiban import clock, window
from yiban import status as yiban_status
from yiban.engine import accounts as accounts_mod

# 三个模块以带后缀的别名导入：本模块内部有同名局部量（`accounts` 本轮账号列表、
# `schedule` 本轮时间表、以及内置函数名 `round`），同名会互相遮蔽。
from yiban.engine import alerts, attempts, cli_support, config_check, probe, state_io, workers
from yiban.engine import round as round_mod
from yiban.engine import schedule as schedule_mod
from yiban.masking import mask_phone as _mask_phone
from yiban.store import db

logger = logging.getLogger("yiban")

# 周日签到开关：部分学校周日也有签到任务（默认关闭，与历史行为一致）
# 由网页系统设置页写入 .env（YIBAN_SUNDAY_SIGN=1），run.sh 加载后经环境变量传入
SUNDAY_SIGN = os.environ.get("YIBAN_SUNDAY_SIGN", "").strip().lower() in ("1", "true", "on", "yes")
# 周六签到开关：2026-09-07（v0.29.0）起默认关闭，与周日同语义——
# 缺省/空/非法一律视为关闭，仅显式 1/true/on/yes 开启（此前缺省=1 的 fail-open
# 解析随默认反转一并废止，_parse_saturday_sign 已删）。需要在周六签到的部署
# 在网页「系统设置 → 周末签到」开启，或 .env 显式写 YIBAN_SATURDAY_SIGN=1。
SATURDAY_SIGN = os.environ.get("YIBAN_SATURDAY_SIGN", "").strip().lower() in ("1", "true", "on", "yes")

# 签到状态码与日志/日历符号：**定义在 yiban.status（唯一事实源）**，此处为别名。
# 历史上 web/app.py 另定义了一份同名常量与映射表，两份会各自漂移（实测 web 侧缺
# no_position/global_paused、signin 侧缺 pending）；收口后状态码只有一处定义。
STATUS_SUCCESS = yiban_status.STATUS_SUCCESS
STATUS_ALREADY = yiban_status.STATUS_ALREADY
STATUS_NO_TASK = yiban_status.STATUS_NO_TASK
STATUS_FAILED = yiban_status.STATUS_FAILED
STATUS_RETRYING = yiban_status.STATUS_RETRYING
STATUS_SKIPPED_WINDOW = yiban_status.STATUS_SKIPPED_WINDOW
STATUS_SKIPPED_NORANGE = yiban_status.STATUS_SKIPPED_NORANGE
STATUS_NO_POSITION = yiban_status.STATUS_NO_POSITION
STATUS_PAUSED = yiban_status.STATUS_PAUSED
STATUS_USER_CANCELLED = yiban_status.STATUS_USER_CANCELLED
STATUS_PENDING = yiban_status.STATUS_PENDING
STATUS_GLOBAL_PAUSED = yiban_status.STATUS_GLOBAL_PAUSED

# 状态码 → 日志/日历符号（同一对象，非副本）
STATUS_SYMBOL = yiban_status.SYMBOL

# `--second-run-check` 的退出码契约（run.sh 据此分支，勿随意改动）
SECOND_RUN_CHECK_NEED = 10   # 需要补跑第二轮
SECOND_RUN_CHECK_SKIP = 0    # 无需补跑


def main(argv=None):
    """主函数：加载账号配置并执行签到，**返回**退出码（不抛 SystemExit）。

    支持：
    - 数据库 yiban.db（SQLite，web 后台写入）与 YIBAN_ACCOUNTS_JSON
    - 旧格式 YIBAN_ACCOUNTS 或 YIBAN_PHONE/YIBAN_PASSWORD（向后兼容）
    - 队列重试：失败账号分散重试——开启签到调度时重新安排到窗口内合适时间，否则放回队尾（分级上限）
    - 随机延迟：YIBAN_START_DELAY_MAX（启动）/ YIBAN_ACCOUNT_GAP_MAX（账号间隔）
    - --only 指定手机号（逗号分隔），仅供手动签到单个账号
    - --check-config 仅检查配置，不发任何网络请求

    argv 为命令行参数（不含程序名）：默认取 `sys.argv[1:]`，与旧签名 `main()` 等价。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    # 进程 umask 077——状态/凭据/邮件配额文件（含完整手机号键）
    # 创建即 0600。宿主 run.sh 已有 umask 077；本处覆盖 web 子进程、容器
    # scheduler 与无宿主脚本的裸调路径（Windows 无实际效果，忽略）。
    os.umask(0o077)
    # 日志装配从模块导入期延迟到 CLI 入口（幂等；覆盖
    # --check-config / --probe / --only 全部路径），模块导入零副作用。
    cli_support._setup_cli_logging()
    parser = argparse.ArgumentParser(description="易班自动签到")
    parser.add_argument(
        "--check-config", action="store_true", help="仅检查账号配置（脱敏打印），不发起任何网络请求"
    )
    parser.add_argument(
        "--only", default="", help="仅签到指定手机号（逗号分隔，用于手动签到）"
    )
    parser.add_argument(
        "--probe", action="store_true",
        help="探针模式：非签到时段对全部账号做只读健康检查（需 .env 开启且到触发时间/频率）",
    )
    parser.add_argument(
        "--second-run-check", action="store_true",
        help=(
            "补签轮判定（供宿主 run.sh 调用）：当日全量未收尾或存在未了结账号时"
            f"退出码 {SECOND_RUN_CHECK_NEED}（需要补跑），否则 0。"
            "只读本地状态（领取池/状态文件），不加载账号、不发起任何网络请求。"
        ),
    )
    parser.add_argument(
        "--workers", type=int, default=1, metavar="N",
        help=(
            "多执行体：拉起 N 个并行执行体共同完成本轮（默认 1 = 单执行体，行为不变）。"
            "分工靠数据库里的领取池（动态领取 + 账号级租约），账号不会被两个执行体同时签；"
            "每个执行体可用 YIBAN_PROXY_LIST 配一个独立出口代理"
        ),
    )
    parser.add_argument(
        "--fallback", action="store_true",
        help=(
            "兜底常驻模式：在签到时段内反复扫描'尚未了结'的账号并随手接手（晚放号、"
            "窗口内新审核通过的账号、慢账号、待重试账号），时段结束自动退出。"
            "自己持独立锁、可用 YIBAN_PROXY_FALLBACK 配独立出口，与定时全量并存"
        ),
    )
    args = parser.parse_args(argv)

    # 兜底常驻执行体：先于其他分支（它自带循环与退出条件）
    if args.fallback:
        return workers.run_fallback_worker(argv)

    # 多执行体：本进程只做监督（持全局锁 + 汇总退出码），活儿由子进程干。
    # 放在补签轮判定之前不必要——补签轮判定只读文件，先走它更快。
    if args.workers and args.workers > 1:
        return workers.run_worker_supervisor(args.workers, argv)

    # 补签轮判定必须最先处理：只读状态文件，不加载账号、不建连接、不发请求。
    # 宿主 run.sh 在首轮结束仍持锁时调用本开关，据退出码决定是否补跑第二轮
    # （与容器 docker/scheduler.py 的 SECOND 闸门同语义，判定实现在 need_second_run）。
    if args.second_run_check:
        if state_io.need_second_run():
            logger.info("补签轮判定：需要补跑（当日全量未收尾或存在未了结账号）")
            return SECOND_RUN_CHECK_NEED
        logger.info("补签轮判定：无需补跑（当日已收尾且无未了结账号）")
        return SECOND_RUN_CHECK_SKIP

    # 超时击杀前的告警兜底：宿主 run.sh timeout / 容器 / 手动 terminate 均以
    # SIGTERM 结束子进程；注册在探针分支之前，签到与探针子进程同享。
    signal.signal(signal.SIGTERM, alerts._flush_mail_on_sigterm)

    notify_url = os.environ.get("YIBAN_NOTIFY_URL", "")

    # 加载账号配置（文件 > JSON 环境变量 > 旧格式，详见 load_accounts）
    try:
        accounts = accounts_mod.load_accounts()
    except RuntimeError as e:
        logger.error(f"配置加载失败: {e}")
        return 1

    # 探针模式必须先于「零账号守卫」处理（2026-08-27 审查修复）：空账号部署
    # 误开探针时此前会夜夜走「未配置任何账号」ERROR 分支且 once 永不关闭；
    # 探针语义下零账号=无事可做，静默成功退出。
    if args.probe:
        # 探针对全部账号做完整登录（等同一次真实签到，风控敏感）：一键暂停 /
        # 周末签到关闭期间照跑会把暂停语义打穿。门在探针分支内部判定——
        # 不上移全局门，保住「探针先于零账号守卫」的既有语义与 --check-config 路径。
        _paused = str(os.environ.get("YIBAN_GLOBAL_PAUSE", "")).strip().lower() in ("1", "true", "on", "yes")
        _weekday = clock.now().weekday()
        if _paused or (_weekday == 6 and not SUNDAY_SIGN) or (_weekday == 5 and not SATURDAY_SIGN):
            logger.info("==== 签到已暂停/周末签到关闭，本轮探针跳过（避免暂停期完整登录） ====")
            return 0
        # 探针会对全部账号做完整登录，必须与真实签到互斥——
        # 原实现绕过运行锁，23:55 探针与手动签到并发时同一账号被两进程并发登录。
        try:
            _probe_lock_fh = cli_support._acquire_run_lock(only_mode=True)
        except cli_support._RunLockHeld:
            logger.warning("已有签到进程在运行，本轮探针跳过（防同账号并发）")
            return 0
        if accounts:
            probe.run_probe(accounts)
        return 0

    # 超期软删账号物理清理（2026-08-20 随读路径清理外移而显式化）：cron/Actions
    # 部署可能没有常驻 web 进程，每日签到进程是清理的唯一时机，失败不阻断签到
    try:
        db.purge_expired_deleted_accounts()
    except Exception as e:
        logger.debug("清理超期软删除账号失败（不影响签到）: %s", e)

    if not accounts:
        logger.error("未配置任何账号，请通过以下任一方式配置：")
        logger.error("  1. yiban.db 数据库（推荐，用网页后台添加）")
        logger.error("  2. YIBAN_ACCOUNTS_JSON 环境变量（JSON 数组）")
        logger.error("  3. YIBAN_ACCOUNTS 环境变量（旧格式 phone:password#phone2:password2）")
        logger.error("  4. YIBAN_PHONE / YIBAN_PASSWORD 环境变量（单账号）")
        return 1

    # --only 过滤：只保留指定手机号（手动签到单个账号）
    # 未命中号码逐号 warning；全不命中时报错退出（既有行为）。
    if args.only:
        accounts, _missing = config_check._apply_only_filter(accounts, args.only)
        if not accounts:
            # 与 _apply_only_filter 同口径脱敏（args.only 是完整裸号）
            logger.error("--only 指定账号不在配置中: %s",
                         ", ".join(_mask_phone(p) for p in _missing))
            return 1

    # 仅检查配置模式：不发任何网络请求，用于部署验证
    if args.check_config:
        config_check.print_config_summary(accounts)
        return 0

    # 启动延迟已废弃（v0.29.0）：仅保持旧签名兼容，值不再使用（read 后仅透传给
    # run_queue_retry 的兼容参数位）；账号间隔 gap_max 仍生效
    start_delay_max = config_check.parse_env_int("YIBAN_START_DELAY_MAX", 0)
    # 缺省 10 与 web 设置页「默认开启 10 秒」口径一致（web 端 DEFAULT_ACCOUNT_GAP_MAX）：
    # 纯 signin 部署（.env 未配置该键）升级后自动获得 10s 账号间隔
    gap_max = config_check.parse_env_int("YIBAN_ACCOUNT_GAP_MAX", 10)

    # 周日签到开关：关闭时周日跳过（cron 已改为每天执行，靠此开关维持周日不签）；
    # 手动签到（--only）不受限——用户主动触发应当放行
    if not args.only and clock.now().weekday() == 6 and not SUNDAY_SIGN:
        logger.info("==== 周日签到未开启（系统设置中开启后周日也会尝试签到），跳过执行 ====")
        return 2  # SKIPPED 语义：run.sh 写 SKIPPED 状态，次日正常执行

    # 周六签到开关：默认开启（周六照常签到）；管理员关闭后周六跳过。
    # 手动签到（--only）不受限——用户主动触发应当放行（与周日开关语义一致）。
    if not args.only and clock.now().weekday() == 5 and not SATURDAY_SIGN:
        logger.info("==== 周六签到已关闭（系统设置中开启后周六也会尝试签到），跳过执行 ====")
        return 2  # SKIPPED 语义：run.sh 写 SKIPPED 状态，次日正常执行

    # 全局暂停（管理员 Web UI 一键暂停）：下一轮生效，当前进程照常跑完。
    # 手动签到（--only）不受限——用户主动触发应当放行（与周日开关语义一致）。
    # YIBAN_GLOBAL_PAUSE 由 .env 写入，run.sh 加载后经环境变量传入。
    if not args.only and str(os.environ.get("YIBAN_GLOBAL_PAUSE", "")).strip().lower() in ("1", "true", "on", "yes"):
        logger.info("==== 签到已暂停（管理员通过 Web UI 一键暂停），跳过执行 ====")
        return 2  # SKIPPED 语义：run.sh 写 SKIPPED 状态，恢复后次日正常执行

    # 补签轮定向重跑：存在未了结账号时补签闸门整站重跑，会把当日已 success 的
    # 账号再次完整登录（风控暴露）。现剔除已了结账号（success/already），
    # 只重跑未完成者；全部已了结则静默结束（退出码 0，不空跑一轮）。
    # --only 手动签到不受影响。
    if not args.only and state_io._is_second_run():
        accounts = state_io._second_run_drop_done(accounts)
        if not accounts:
            logger.info("==== 补签轮：当日账号均已了结，无需重跑 ====")
            return 0

    # 进程级单实例锁（2026-08-20 对抗性审查 P2）：防 cron 全量队列与手动 --only
    # 并发签到同一账号。--only 被持有 → 留痕退出；全量被持有 → 等待至多
    # YIBAN_RUN_LOCK_WAIT 秒后继续（不因手动签到阻塞而漏签一整天）。
    try:
        _run_lock_fh = cli_support._acquire_run_lock(bool(args.only))
    except cli_support._RunLockHeld:
        logger.warning("已有签到进程在运行，本次手动签到跳过（防同账号并发，稍后可重试）")
        # 原 exit 0 让 web 把"静默跳过"当成功展示；3 = 队列忙，
        # 调用方可据此向用户如实提示（退出码语义见文件头/退出码表）
        return 3

    # 版本号写进轮次横幅：发布门槛靠它把"生产跑过的轮次"与提交对齐
    # （docs/dev/release-gate.md §4），生产日志本身不带版本信息。
    logger.info(
        f"==== 开始执行签到（v{RELEASE_VERSION}），共 {len(accounts)} 个账号，队列重试模式 ===="
    )
    # 状态文件以"尝试开始时刻"的日期命名（防跨午夜执行写错当天）
    attempt_date = clock.now().strftime("%Y-%m-%d")
    # 自动错峰（仅自动签到；--only 手动签到立即执行，不走计划）
    schedule = {} if args.only else schedule_mod.build_schedule(accounts)
    if schedule:
        # 容量预检（调度 v2 第三层）：可容纳账号数 < 待签到账号数 → 告警不静默
        # 用户自暂停账号不参与调度，也不计入容量
        _cfg = schedule_mod._schedule_config()
        _win = window.bounds(_cfg)
        # 预检按**剩余**有效窗口算：本进程此刻才起跑，已流逝的窗口签不了。
        # 原实现用完整窗口算，迟启动时按满容量放行且不告警，超出的账号只能落
        # skipped_window——管理员看不到任何提示。
        _rest_sec = _win.remaining_sec(clock.now())
        _win_end = window.to_dt(
            clock.now().replace(hour=0, minute=0, second=0, microsecond=0),
            _win.hi_min,
        ).strftime("%H:%M")
        active_n = sum(1 for a in accounts if not getattr(a, "user_paused", False))
        # 与 web 容量预估同一函数：账号间隔是「上一次完成 → 下一次开始」的下限，
        # 故单账号周期 = avg + gap（旧实现只算 n × avg，与预估口径相差 ~2.3 倍）
        _cap = schedule_mod.capacity_accounts(max(0.0, _rest_sec), gap_max, _cfg["avg_attempt_sec"])
        if _rest_sec <= 0:
            logger.warning(
                "容量预检: 本进程起跑时签到时段已结束（有效窗口至 %s），本轮不会发起任何请求",
                _win_end,
            )
            alerts._collect_admin_mail(
                "易班签到容量超载",
                f"本次签到进程起跑时已过有效签到窗口（窗口至 {_win_end}），"
                f"{active_n} 个账号本轮不会执行。如非预期，请检查触发时刻（cron / 容器调度）"
                "与签到窗口设置（YIBAN_SIGN_START / YIBAN_SIGN_END）。",
            )
        elif active_n > _cap:
            logger.warning(
                "容量预检: %d 个账号 > 剩余有效窗口 %d 秒可容纳的 %d 个"
                "（单账号 %.0fs + 账号间隔 %ds，窗口至 %s），部分账号可能无法在窗口内完成",
                active_n, int(_rest_sec), _cap, _cfg["avg_attempt_sec"], gap_max, _win_end,
            )
            # 超载提醒（对抗性审查补）：通知管理员，避免"超限只在日志里"无人知情。
            # A 线合并：并入任务结束汇总邮件；webhook 仍即时推送。
            alerts._collect_admin_mail(
                "易班签到容量超载",
                f"当前 {active_n} 个账号，剩余有效窗口 {int(_rest_sec)}s（至 {_win_end}）"
                f"仅可容纳 {_cap} 个"
                f"（单账号 {_cfg['avg_attempt_sec']}s + 账号间隔 {gap_max}s），"
                "部分账号可能无法在窗口内完成签到。\n"
                f"建议：增加窗口时长、缩短账号间隔或减少账号数量（.env 调整）。",
            )
            alerts.send_notification(
                "易班签到容量超载",
                f"当前 {active_n} 个账号，剩余有效窗口 {int(_rest_sec)}s（至 {_win_end}）"
                f"仅可容纳 {_cap} 个"
                f"（单账号 {_cfg['avg_attempt_sec']}s + 账号间隔 {gap_max}s），"
                "部分账号可能无法在窗口内完成签到。\n"
                f"建议：增加窗口时长、缩短账号间隔或减少账号数量（.env 调整）。",
                notify_url,
            )
        # 计划写入状态文件（pending 态展示"今日计划 HH:MM"）；执行时按时间点排序
        for acc in accounts:
            t = schedule.get(acc.phone)
            if t:
                state_io._write_sign_state(
                    acc.phone, STATUS_PENDING,
                    f"计划 {t.strftime('%H:%M')}", scheduled=t.strftime("%H:%M:%S"),
                )
        accounts = sorted(accounts, key=lambda a: schedule.get(a.phone, datetime.max))
        # 调度快照标记（2026-08-15 用户反馈：卡点缓冲）：web 端保存自选时以此时刻为
        # "今日/明日生效"分界——改选在快照后必为明日生效，提示与实际 100% 一致
        # （原固定"窗口起点+1 分钟"与 cron 实际读取时刻有几秒偏差窗口）
        try:
            _snap_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
            os.makedirs(_snap_dir, exist_ok=True)
            _snap_path = os.path.join(_snap_dir, f"sched-snapshot-{attempt_date}.json")
            _snap_tmp = _snap_path + ".tmp" + str(os.getpid())
            with open(_snap_tmp, "w", encoding="utf-8") as _f:
                json.dump({"snapshot_at": clock.now().strftime("%H:%M:%S")}, _f)
            os.replace(_snap_tmp, _snap_path)
        except OSError:
            pass  # 标记不可写时 web 端回退旧分界，不影响签到

    # 账密熔断状态：跨天计数（暂停账号零请求；手动签到 --only 不受限）
    cred_state = {} if args.only else state_io._load_cred_state()
    # 签到事件收集器——run_queue_retry 每次尝试/迁移经 sink 上报，
    # 任务结束后单事务批量落库（见 results 赋值后的 add_sign_events_batch）。
    event_rows = []
    delegated = set()   # 不在本执行体范围内的账号（多执行体分工，见 run_queue_retry 说明）
    results = round_mod.run_queue_retry(
        accounts, notify_url, start_delay_max, gap_max, schedule=schedule, cred_state=cred_state,
        event_sink=event_rows.append, delegated=delegated,
        # 手动指定账号（--only）允许重签当日已了结的账号：用户主动点的那一下应当照做
        reclaim=bool(args.only),
    )
    # 2026-08-20 对抗性审查修复（P1）：--only 此前无条件以本次（仅含目标账号的）状态
    # 整体覆盖保存——空 dict 时直接删除状态文件，其他账号的 fail_days/paused_since
    # 全部丢失，账密熔断保护被任意一次手动签到全局重置。现改为：--only 只把本次
    # 处理账号的熔断增量合并回存量状态（成功→清除该账号记录；凭据失败→按日累计；
    # 其他失败→不动），未处理账号保持原状。全量模式语义不变（本轮本就基于存量计算）。
    if args.only:
        # 增量合并（唯一入口内的读-改-写持锁）：只覆盖本次处理账号的熔断增量
        merged = state_io._load_cred_state()
        _merge_today = clock.now().strftime("%Y-%m-%d")
        for _acc in accounts:
            _res = results.get(_acc.phone)
            if _res is None:
                continue
            _ok, _msg, _skip, _status = _res
            _was_paused = bool(merged.get(_acc.phone, {}).get("paused_since"))
            attempts._update_cred_state(merged, _acc.phone, _ok, _msg, _merge_today)
            # 2026-08-21 对抗性审查补充：手动试探已暂停账号且凭据仍失败时，
            # 顺延下次试探日（对齐全量模式语义）——否则存量过期 probe_date 会让
            # 下一轮全量签到立即再试探，失去半开试探的间隔保护
            if (
                not _ok
                and _was_paused
                and attempts._is_credential_failure(_msg)
                and attempts._probe_due(merged.get(_acc.phone, {}), _merge_today)
            ):
                merged[_acc.phone]["probe_date"] = (
                    datetime.strptime(_merge_today, "%Y-%m-%d")
                    + timedelta(days=attempts.PROBE_INTERVAL_DAYS)
                ).strftime("%Y-%m-%d")
        state_io._save_cred_state(merged, touched={a.phone for a in accounts})
    else:
        # 全量轮：按账号增量合并（内存快照不能整体覆盖磁盘——见 _save_cred_state 文档）
        state_io._save_cred_state(cred_state, touched={a.phone for a in accounts})

    # 汇总（合并为一行统计；逐账号结果已在执行中输出，不再逐行重复）
    # 口径：成功=success/already；跳过=no_task+skipped（无需签到与时段外同列）；
    # 已执行=已了结（success/already/no_task），窗口外等跳过不算（7:10 还会再跑）。
    # 2026-09-01：no_position（易班侧无点位）归入跳过计数但单独展示——非账号失败，
    # 不参与 has_real_failure；但归入"未了结"（与容器调度器 _UNDONE_STATUSES 同语义），
    # 宿主 exit 2 / 容器 07:10 补签轮均会重跑一次——学校延迟放位时仍有兜底
    # （无点位账号 1 次即止、幂等无害）。
    has_real_failure = False
    has_executed = False
    # 窗口外/缺失（skipped_window/skipped_norange）属"未了结"——
    # 与容器调度器 _UNDONE_STATUSES（docker/scheduler.py）同一语义。宿主 run.sh 的
    # 07:10 补签闸门只认状态文件 SUCCESS 文本：若本轮有成功就把 skipped 账号的
    # 退出码判成 0，run.sh 写 SUCCESS → 补签被吞，被跳过的账号当天失去兜底
    # （容器侧已修此洞，宿主侧是本轮补齐）。
    has_window_skip = False
    ok_n = fail_n = skip_n = no_pos_n = other_n = 0
    for acc in accounts:
        if acc.phone in delegated:
            # 由其他执行体负责：既不算成功也不算失败。若把它当失败，多执行体形态下
            # 每个执行体都会把别人的活报成自己的失败（退出码与告警都会失真）。
            other_n += 1
            continue
        _s, _m, _sk, status = results.get(acc.phone, (False, "未执行", False, STATUS_PENDING))
        if status in (STATUS_SUCCESS, STATUS_ALREADY):
            ok_n += 1
        elif status in (STATUS_NO_TASK, STATUS_SKIPPED_WINDOW, STATUS_SKIPPED_NORANGE,
                        STATUS_PAUSED, STATUS_USER_CANCELLED, STATUS_NO_POSITION):
            skip_n += 1
            if status in (STATUS_SKIPPED_WINDOW, STATUS_SKIPPED_NORANGE):
                has_window_skip = True
            if status == STATUS_NO_POSITION:
                no_pos_n += 1
        else:
            fail_n += 1
            has_real_failure = True
        if status in (STATUS_SUCCESS, STATUS_ALREADY, STATUS_NO_TASK):
            has_executed = True
    summary = f"✅ {ok_n} 成功，❌ {fail_n} 失败"
    if skip_n:
        summary += f"，➖ {skip_n} 跳过"
    if no_pos_n:
        summary += f"，🚫 {no_pos_n} 无点位"
    if other_n:
        summary += f"，⇄ {other_n} 由其他执行体负责"
    logger.info(f"==== 签到汇总（v{RELEASE_VERSION}）：{summary} ====")

    # 窗口外未了结专项告警。
    # is_second_run：run.sh 补签轮（07:10）导出的 YIBAN_SECOND_RUN=1 优先
    # （首签子进程被 timeout 击杀、exit 124 未写 sched-run 标记时，
    # 标记兜底失效，必须靠 run.sh 的补签轮环境变量识别）；容器调度器 SECOND
    # 时段同样注入该变量；sched-run 标记作为兜底（手动/其他启动路径）。
    alerts._maybe_alert_zero_success(
        accounts, results, ok_n, is_second_run=state_io._is_second_run()
    )

    # 签到事件落库——v6 建了 sign_events 表但签到主流程零写入
    # （仅探针 stage=probe 有写入），统计/时间线读取函数零调用方，基础设施空转。
    # 现每次尝试与状态迁移落一行（stage=sign），批量单事务写入；失败仅告警
    # （add_sign_events_batch 内部捕获），不影响签到退出码。
    if event_rows:
        db.add_sign_events_batch(event_rows)

    # 写按日状态文件（供网页日历组件读取；窗口外跳过不写，当天留空）
    # 符号按状态码：success/already→✅、no_task→➖、failed→❌、no_position→🚫
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    try:
        os.makedirs(state_dir, exist_ok=True)
        # M14：汇总文件以写盘时日期命名（跨午夜不沿用启动时的 attempt_date）
        daily_path = os.path.join(state_dir, f"sign-daily-{clock.now().strftime('%Y-%m-%d')}.json")
        with cli_support._state_file_lock(daily_path):
            daily = {}
            if os.path.exists(daily_path):
                try:
                    with open(daily_path, encoding="utf-8") as f:
                        daily = json.load(f)
                except (OSError, ValueError, TypeError):
                    logger.warning("按日状态文件 %s 损坏，按空数据重建", daily_path)
                    daily = {}
            if not isinstance(daily, dict):
                logger.warning("按日状态文件 %s 非 dict，按空数据重建", daily_path)
                daily = {}
            for acc in accounts:
                _s, _m, _sk, status = results.get(acc.phone, (False, "未执行", False, STATUS_PENDING))
                if status in (STATUS_SUCCESS, STATUS_ALREADY, STATUS_NO_TASK,
                              STATUS_FAILED, STATUS_NO_POSITION):
                    daily[acc.phone] = STATUS_SYMBOL[status]
            # M15：tmp + os.replace 原子写，避免半截文件
            daily_tmp = daily_path + ".tmp" + str(os.getpid())
            with open(daily_tmp, "w", encoding="utf-8") as f:
                json.dump(daily, f, ensure_ascii=False)
            os.replace(daily_tmp, daily_path)
    except (OSError, ValueError, TypeError) as e:
        logger.warning("写入按日状态文件失败: %s", e)

    # A 线合并：签到任务彻底结束后，把运行期收集的管理员告警汇总成一封邮件发送。
    # 无异常则不发送（成功不打扰）；mailer 内部静默失败，不影响退出码。
    alerts._flush_admin_mail_summary()

    # 全量运行完成标记：调度器首签/补签闸门的事实源。
    # 仅全量模式写入；--only 手动签到不写——手动成功不得压制调度器当日判定。
    if not args.only:
        state_io._write_sched_done({"ok_n": ok_n, "fail_n": fail_n, "skip_n": skip_n})

    # 退出码（run.sh 依据退出码写状态文件）：
    # 0 - 全部成功（有实际签到执行；含"已签到""无需签到"，且无窗口外未了结账号）
    # 1 - 有真正的失败（登录失败、签到失败等）
    # 2 - 全部 skip 或存在窗口外未了结账号（无实际执行，或首签窗口外账号需 07:10 补签
    #     重跑；此时 run.sh 写 SKIPPED 而非 SUCCESS，补签 cron 才会继续尝试）
    #     由 run.sh 写 SKIPPED 而非 SUCCESS，避免备份等下游任务被吞
    if has_real_failure:
        return 1
    if not has_executed or has_window_skip:
        return 2
    return 0
