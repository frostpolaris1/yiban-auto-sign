# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
入口与轮次编排：`main(argv=None) -> int`（装载配置 → 分支 → 一轮队列 → 汇总退出码）。

**退出码由本函数返回、不再自己 `sys.exit`**（退出码语义逐字不变，见
`docs/dev/cli.md` §3）：这样 `yiban/cli.py` 与 `scripts/signin.py` 兼容壳可以统一
`sys.exit(main(argv))`，同时保留"返回码"这一可被直接断言的形式。

分支顺序是有意为之，改动前先读注释：兜底常驻 → 补签轮判定（只读本地状态）→
多执行体监督（派发前先过周末/暂停门）→ SIGTERM 兜底 → 加载账号 → 探针 → 零账号守卫 →
`--only` 过滤 → `--check-config` → 周末门/全局暂停 → 补签轮定向重跑 → 进程级单实例锁 →
一轮队列 → 汇总与退出码。

**归属**
`yiban.engine` 的编排顶层，也是命令行签到路径的入口实现：`yiban/cli.py` 的
`sign` / `probe` 子命令、部署面的 `scripts/signin.py` 兼容壳与 `run.sh`、容器调度器
都落到这里。真正的签到逻辑在 `round`，进程编排在 `workers`。

**复用**
`main()` 是唯一入口；`SUNDAY_SIGN` / `SATURDAY_SIGN`、退出码常量与
`_GATE_SKIP_MESSAGES` 供同族模块对齐口径；单实例锁与 `cli_support` 共用同一原语。

**通信**
输入：`argv`（缺省取 `sys.argv[1:]`）与环境变量/.env（配置只从环境读，命令行不接受
敏感值）。输出：进程退出码（0/1/2/3/10 口径见 `docs/dev/cli.md` §3）与日志；`--json`
由 `cli.py` 包裹。
调用谁：`accounts` / `probe` / `workers` / `round` / `executor_v3` / `state_io` / `alerts`
/ `cli_support`（跨模块一律走模块属性访问）。
谁调用：`yiban/cli.py`（`python -m yiban.cli sign|probe`）、`scripts/signin.py` 兼容壳、
`docker/scheduler.py`。
前端调用点：手动签到 `/api/signin` 经 `web/services/manual_sign.py` 以子进程拉起
`scripts/signin.py --only`（`web/static/js/components/account-ops.js` 触发），执行体接口
`/api/scheduler/executors*`（`web/static/js/components/settings-executors.js`、`settings-quota.js`）
读执行体产生的存活态——退出码语义变化会改变这些页面的成功/失败提示与执行体行。
"""
import argparse
import json
import logging
import os
import signal
import sys
from datetime import datetime, timedelta

from yiban import __version__ as RELEASE_VERSION
from yiban import clock, egress, window
from yiban import status as yiban_status
from yiban.engine import accounts as accounts_mod

# 三个模块以带后缀的别名导入：本模块内部有同名局部量（`accounts` 本轮账号列表、
# `schedule` 本轮时间表、以及内置函数名 `round`），同名会互相遮蔽。
from yiban.engine import (
    alerts,
    attempts,
    cli_support,
    config_check,
    executor_v3,
    probe,
    state_io,
    workers,
)
from yiban.engine import round as round_mod
from yiban.engine import schedule as schedule_mod
from yiban.infra import env_io
from yiban.masking import mask_phone as _mask_phone
from yiban.store import db

logger = logging.getLogger("yiban")

# 周日签到开关：部分学校周日也有签到任务（默认关闭，与历史行为一致）
# 由网页系统设置页写入 .env（YIBAN_SUNDAY_SIGN=1），run.sh 加载后经环境变量传入
SUNDAY_SIGN = os.environ.get("YIBAN_SUNDAY_SIGN", "").strip().lower() in ("1", "true", "on", "yes")
# 周六签到开关：默认同样关闭，与周日同语义——缺省/空/非法一律视为关闭，
# 仅显式 1/true/on/yes 开启（缺省即开启的 fail-open 解析已废止）。
# 需要在周六签到的部署要在网页「系统设置 → 周末签到」开启，或 .env 写 YIBAN_SATURDAY_SIGN=1。
SATURDAY_SIGN = os.environ.get("YIBAN_SATURDAY_SIGN", "").strip().lower() in ("1", "true", "on", "yes")

# 签到状态码与日志/日历符号：**定义在 yiban.status（唯一事实源）**，此处为别名
# （此前 web/app.py 另有一份同名常量，两份会各自漂移，收口后只有一处定义）。
STATUS_SUCCESS = yiban_status.STATUS_SUCCESS
STATUS_ALREADY = yiban_status.STATUS_ALREADY
STATUS_NO_TASK = yiban_status.STATUS_NO_TASK
STATUS_FAILED = yiban_status.STATUS_FAILED
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

# 三道门（周日未开 / 周六未开 / 一键暂停）跳过时的日志文案：**逐字保留历史措辞**
# （运维与既有测试按它判断"这一轮为什么没跑"），因此仍写在本模块而不是门函数里。
_GATE_SKIP_MESSAGES = {
    schedule_mod.DAY_OFF_SUNDAY:
        "==== 周日签到未开启（系统设置中开启后周日也会尝试签到），跳过执行 ====",
    schedule_mod.DAY_OFF_SATURDAY:
        "==== 周六签到已关闭（系统设置中开启后周六也会尝试签到），跳过执行 ====",
    schedule_mod.DAY_OFF_PAUSED:
        "==== 签到已暂停（管理员通过 Web UI 一键暂停），跳过执行 ====",
}


def _day_off_skip():
    """周末（周六/周日未开）或一键暂停命中时打日志并返回 2（SKIPPED 语义），否则 None。

    门本身只有 `schedule.day_off` 一个实现，本函数只负责把「当前时刻 + 导入期开关
    快照」喂给它——多执行体派发前的提前拦截与单执行体路径的门必须给出**同一个判定、
    同一句措辞**，否则两条路径对"这一轮为什么没跑"的解释会漂移。
    """
    gate = schedule_mod.day_off(clock.now(), sat=SATURDAY_SIGN, sun=SUNDAY_SIGN)
    if not gate:
        return None
    logger.info(_GATE_SKIP_MESSAGES[gate])
    return 2  # run.sh 据此写 SKIPPED 状态，次日正常执行


def main(argv=None):
    """主函数：加载账号配置并执行签到，**返回**退出码（不抛 SystemExit）。

    支持：
    - 数据库 yiban.db（SQLite，web 后台写入）与 YIBAN_ACCOUNTS_JSON
    - 旧格式 YIBAN_ACCOUNTS 或 YIBAN_PHONE/YIBAN_PASSWORD（向后兼容）
    - 队列重试：失败账号分散重试——开启签到调度时重新安排到窗口内合适时间，否则放回队尾（分级上限）
    - 账号间隔：YIBAN_ACCOUNT_GAP_MAX（启动延迟 YIBAN_START_DELAY_MAX 已废弃，仅为兼容旧签名保留）
    - --only 指定手机号（逗号分隔），仅供手动签到单个账号
    - --check-config 仅检查配置，不发任何网络请求

    argv 为命令行参数（不含程序名）：默认取 `sys.argv[1:]`，与旧签名 `main()` 等价。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    # 进程 umask 077——状态/凭据/邮件配额文件（含完整手机号键）创建即 0600。
    # 宿主 run.sh 已有 umask 077；本处覆盖 web 子进程、容器 scheduler 与无宿主脚本的
    # 裸调路径（Windows 无实际效果）。
    os.umask(0o077)
    # 清空上一轮的致命错误摘要（进程内多次调用 main 时不得把上轮原因附到本轮）
    cli_support.clear_fatal_error()
    # 日志装配延迟到 CLI 入口（幂等；覆盖 --check-config / --probe / --only 全部路径）
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
        # 独立锁 `signin-run.lock.fallback`：**真的取**。此前只设了锁名、从没取过，
        # 于是"自己持独立锁"只是注释里的一句话——同一台机器上起第二个兜底不会被挡住
        # （重复登录虽由领取池兜住，但会白烧一轮登录，且"窗口结束自行退出"的判断也会
        # 被第二个进程重复执行）。取锁用**非阻塞**口径：已有兜底在跑就退出 3
        # （与手动签到撞锁同一语义），不排队；句柄在本进程存活期间必须保活。
        os.environ.setdefault("YIBAN_RUN_LOCK_NAME", workers.FALLBACK_LOCK_NAME)
        try:
            # 句柄必须保活到进程结束（flock 随句柄释放），故赋值给局部变量而不是丢弃
            _fallback_lock_fh = cli_support._acquire_run_lock(True)
        except cli_support._RunLockHeld:
            logger.warning("已有兜底常驻执行体在运行，本次不重复拉起（防同账号并发登录）")
            return 3
        return workers.run_fallback_worker(argv)

    # 补签轮判定必须最先处理：只读状态文件，不加载账号、不建连接、不发请求。
    # 宿主 run.sh 在首轮结束仍持锁时调用本开关，据退出码决定是否补跑第二轮
    # （与容器 docker/scheduler.py 的 SECOND 闸门同语义，判定实现在 need_second_run）。
    # 位置在多执行体派发之前：清单形态下监督进程会先拉起执行体（各自读配置、连库、
    # 载账号、真实登录），只读判定被挡在后面就做成了重活，退出码也传不出来。
    if args.second_run_check:
        if state_io.need_second_run():
            logger.info("补签轮判定：需要补跑（当日全量未收尾或存在未了结账号）")
            return SECOND_RUN_CHECK_NEED
        logger.info("补签轮判定：无需补跑（当日已收尾且无未了结账号）")
        return SECOND_RUN_CHECK_SKIP

    # 多执行体：本进程只做监督（持全局锁 + 汇总退出码），活儿由子进程干。
    # 拉起列表：优先执行体清单（`YIBAN_EXECUTORS` 里 type=worker 的行，**停用行不拉起**、
    # 删中间行不影响其余槽位）；清单缺失/非法 → 旧口径 `--workers N`（行为逐字不变）。
    # 清单里只有 1 个并行执行体时仍走进程内的单执行体路径（`single` 角色、出口读
    # `YIBAN_PROXY`）——与迁移前的 `YIBAN_WORKERS=1` 完全一致。
    #
    # **子进程不得再当监督进程**（2026-09-17 对抗性审查 H1）：清单是从**环境变量**读的，
    # 监督进程拉起的子进程会原样继承它，于是"父按清单拉 N 个 → 子也按清单拉 N 个"会递归
    # 成进程树；argv 侧去 `--workers` 的老办法挡不住（清单路径根本不经 argv）。
    # 子进程身份由监督进程注入 `YIBAN_EXECUTOR_ID`（`worker-{i}@{主机名}`），据此短路。
    _already_child = bool(os.environ.get("YIBAN_EXECUTOR_ID", "").strip())
    slots = None if _already_child else egress.launch_slots()
    # 派发监督进程的参数（None = 不派发，走下面的进程内单执行体路径）：清单缺失/非法 →
    # 旧口径 `--workers N`（槽位就是 0..N-1）；清单在场 → 数**拉起列表**的槽位
    # （停用/兜底行不在其中）。
    if slots is None:
        _dispatch = (args.workers, None) if (
            not _already_child and args.workers and args.workers > 1) else None
    else:
        _dispatch = (len(slots), slots) if len(slots) > 1 else None
    # 只有"真跑计划任务"的派发才在 spawn 之前过门：`--only` 是用户主动触发（必须放行）、
    # `--check-config` 是部署验证（哪天都要能验）、`--probe` 自带一道门且跳过语义是
    # `return 0` 而非 2——三者照旧派发，由子进程各自按既有语义处理，与门只写在下面时逐字一致。
    if _dispatch is not None:
        if not (args.only or args.check_config or args.probe):
            # 周末/暂停门必须在 spawn 之前拦下：否则先按清单拉起一批执行体子进程，再由每个
            # 子进程各自撞门退出（读配置、连库、载账号都白做一遍）。
            _skip = _day_off_skip()
            if _skip is not None:
                return _skip
        return workers.run_worker_supervisor(_dispatch[0], argv, slots=_dispatch[1],
                                             migrate=not args.check_config)

    # 超时击杀前的告警兜底：宿主 run.sh timeout / 容器 / 手动 terminate 均以
    # SIGTERM 结束子进程；注册在探针分支之前，签到与探针子进程同享。
    signal.signal(signal.SIGTERM, alerts._flush_mail_on_sigterm)

    notify_url = os.environ.get("YIBAN_NOTIFY_URL", "")

    # 加载账号配置（文件 > JSON 环境变量 > 旧格式，详见 load_accounts）
    # `--check-config` 是只读校验：不跑 schema 迁移（迁移会重写审计链，使"被校验
    # 对象在校验过程中被改动"；db.init_db 文档自述校验类工具应传 False）
    try:
        accounts = accounts_mod.load_accounts(migrate=not args.check_config)
    except (RuntimeError, ValueError) as e:
        # ValueError 来自 `_parse_account_dict` 的字段缺失（phone/password 为空）：
        # 配置错误同样要落成"配置加载失败 + 退出码 1"，不得变裸 traceback
        logger.error(f"配置加载失败: {e}")
        # stderr 一行摘要：日志只进按天文件，agent/CI 直调 CLI 时否则输出全空
        cli_support.report_fatal_error(f"配置加载失败: {e}")
        return 1

    # 探针模式必须先于「零账号守卫」处理：空账号部署误开探针时，走「未配置任何账号」
    # 的 ERROR 分支会夜夜报错；探针语义下零账号=无事可做，静默成功退出。
    if args.probe:
        # 探针对全部账号做完整登录（等同一次真实签到，风控敏感）：一键暂停 /
        # 周末签到关闭期间照跑会把暂停语义打穿。门在探针分支内部判定——
        # 不上移全局门，保住「探针先于零账号守卫」的既有语义与 --check-config 路径。
        if schedule_mod.day_off(clock.now(), sat=SATURDAY_SIGN, sun=SUNDAY_SIGN):
            logger.info("==== 签到已暂停/周末签到关闭，本轮探针跳过（避免暂停期完整登录） ====")
            return 0
        # 探针与真实签到必须互斥，否则探针会与手动签到并发登录同一账号
        try:
            _probe_lock_fh = cli_support._acquire_run_lock(only_mode=True)
        except cli_support._RunLockHeld:
            logger.warning("已有签到进程在运行，本轮探针跳过（防同账号并发）")
            return 0
        if accounts:
            probe.run_probe(accounts)
        return 0

    # 超期软删账号物理清理：cron/Actions 部署可能没有常驻 web 进程，
    # 每日签到进程是清理的唯一时机；失败不阻断签到。
    # `--check-config` 是只读校验：物理删行也是写库，同样跳过（与 migrate=False 同理由）。
    if not args.check_config:
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
        # stderr 一行摘要（详细配置方法同上留在日志里）：退出码 1 不得伴随全空输出
        cli_support.report_fatal_error(
            "未配置任何账号：请配置 yiban.db（网页后台添加）、YIBAN_ACCOUNTS_JSON、"
            "YIBAN_ACCOUNTS 或 YIBAN_PHONE/YIBAN_PASSWORD（配置方法详见日志）")
        return 1

    # --only 过滤：只保留指定手机号（手动签到单个账号）
    # 未命中号码逐号 warning；全不命中时报错退出（既有行为）。
    if args.only:
        accounts, _missing = config_check._apply_only_filter(accounts, args.only)
        if not accounts:
            # 与 _apply_only_filter 同口径脱敏（args.only 是完整裸号）
            _missing_shown = ", ".join(_mask_phone(p) for p in _missing)
            logger.error("--only 指定账号不在配置中: %s", _missing_shown)
            cli_support.report_fatal_error(f"--only 指定账号不在配置中: {_missing_shown}")
            return 1

    # 仅检查配置模式：不发任何网络请求，用于部署验证
    if args.check_config:
        config_check.print_config_summary(accounts)
        return 0

    # 启动延迟已废弃：仅为兼容旧调用签名而读取，值不再使用（run_queue_retry 里同样
    # 只占参数位）；账号间隔 gap_max 仍生效
    start_delay_max = config_check.parse_env_int("YIBAN_START_DELAY_MAX", 0)
    # 缺省 10 与 web 设置页「默认开启 10 秒」口径一致（web 端 DEFAULT_ACCOUNT_GAP_MAX）：
    # 纯 signin 部署（.env 未配置该键）升级后自动获得 10s 账号间隔
    gap_max = config_check.parse_env_int("YIBAN_ACCOUNT_GAP_MAX", 10)

    # 周日签到开关：关闭时周日跳过（cron 已改为每天执行，靠此开关维持周日不签）；
    # 周六同语义；一键暂停（管理员 Web UI）同理。
    # 三道门**共用 `schedule.day_off`（唯一实现）**：门只写在本函数里会被
    # `--fallback` 的分支顺序绕过（兜底在它之前 return，2026-09-17 实测），故
    # 兜底常驻（`workers.run_fallback_worker`）也走同一个函数。
    # 手动签到（--only）不受限——用户主动触发应当放行。
    if not args.only:
        _skip = _day_off_skip()
        if _skip is not None:
            return _skip

    # 补签轮定向重跑：存在未了结账号时补签闸门整站重跑，会把当日已 success 的
    # 账号再次完整登录（风控暴露）。现剔除已了结账号（success/already），
    # 只重跑未完成者；全部已了结则静默结束（退出码 0，不空跑一轮）。
    # --only 手动签到不受影响。
    if not args.only and state_io._is_second_run():
        accounts = state_io._second_run_drop_done(accounts)
        if not accounts:
            logger.info("==== 补签轮：当日账号均已了结，无需重跑 ====")
            return 0

    # 进程级单实例锁：防 cron 全量队列与手动 --only 并发签到同一账号。
    # --only 被持有 → 留痕退出；全量被持有 → 等待至多 YIBAN_RUN_LOCK_WAIT 秒后继续
    # （不因手动签到阻塞而漏签一整天）。
    try:
        _run_lock_fh = cli_support._acquire_run_lock(bool(args.only))
    except cli_support._RunLockHeld:
        logger.warning("已有签到进程在运行，本次手动签到跳过（防同账号并发，稍后可重试）")
        # 不能返回 0：web 会把"静默跳过"当成功展示。3 = 队列忙，
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
        # 取计划层同一份配置快照（planner_config 是 _schedule_config 的超集，多带桶速率与
        # 执行体清单）：K 与容量口径都从它取，不另读一份环境，免得两处口径分叉。
        _cfg = schedule_mod.planner_config()
        _win = window.bounds(_cfg)
        # 预检按**剩余**有效窗口算：本进程此刻才起跑，已流逝的窗口签不了。
        # 按完整窗口算会在迟启动时按满容量放行且不告警，超出的账号只能落
        # skipped_window——管理员看不到任何提示。
        _rest_sec = _win.remaining_sec(clock.now())
        _win_end = window.to_dt(
            clock.now().replace(hour=0, minute=0, second=0, microsecond=0),
            _win.hi_min,
        ).strftime("%H:%M")
        active_n = sum(1 for a in accounts if not getattr(a, "user_paused", False))
        # 与 web 容量预估同一函数（`capacity_of` 按开关分派）：账号间隔是「上一次完成 →
        # 下一次开始」的下限，故单账号周期 = avg + gap（只算 n × avg 会与预估口径相差约
        # 2.3 倍）。K 按当日活跃账号数自动定尺（`executor_count` 是 K 的唯一口径）：
        # v2 侧该值不参与（`capacity_of` 逐字走旧公式），故开关缺省时数值逐字不变。
        _cap = schedule_mod.capacity_of(
            max(0.0, _rest_sec), gap=gap_max, avg=_cfg["avg_attempt_sec"],
            k=schedule_mod.executor_count(
                active_n, max(0.0, _rest_sec), bucket_rate=_cfg["bucket_rate"],
                egress_count=len(_cfg["executors"]) or 1))
        if _rest_sec <= 0:
            logger.warning(
                "容量预检: 本进程起跑时签到时段已结束（有效窗口至 %s），本轮不会发起任何请求",
                _win_end,
            )
            # 与超载分支同口径：超载必须通知管理员，不能只留在日志里——
            # A 线并入任务结束汇总邮件，webhook 即时推送（只并汇总会让"起跑即窗口已过"
            # 这类必然全轮跳过的事故在任务结束前完全无声）。
            alerts.notify_admin_entry("易班签到容量超载", [
                ("状态", f"起跑时已过有效签到窗口（窗口至 {_win_end}）"),
                ("影响", f"{active_n} 个账号本轮不会执行"),
                ("请核查", "触发时刻（cron / 容器调度）与签到窗口设置"
                           "（YIBAN_SIGN_START / YIBAN_SIGN_END）"),
            ], notify_url)
        elif active_n > _cap:
            logger.warning(
                "容量预检: %d 个账号 > 剩余有效窗口 %d 秒可容纳的 %d 个"
                "（单账号 %.0fs + 账号间隔 %ds，窗口至 %s），部分账号可能无法在窗口内完成",
                active_n, int(_rest_sec), _cap, _cfg["avg_attempt_sec"], gap_max, _win_end,
            )
            # 超载必须通知管理员，不能只留在日志里。
            # A 线：并入任务结束汇总邮件；webhook 仍即时推送。
            # 整段文案原先在邮件与推送各写一遍同样的字面量，改一处必漏另一处，故共用一份。
            alerts.notify_admin_entry("易班签到容量超载", [
                ("当前账号", f"{active_n} 个"),
                ("剩余有效窗口", f"{int(_rest_sec)}s（至 {_win_end}），仅可容纳 {_cap} 个"),
                ("单账号耗时", f"{_cfg['avg_attempt_sec']}s + 账号间隔 {gap_max}s"),
                ("处置", "增加窗口时长、缩短账号间隔或减少账号数量（.env 调整）"),
            ], notify_url)
        # 计划写入状态文件（pending 态展示"今日计划 HH:MM"）；执行时按时间点排序
        for acc in accounts:
            t = schedule.get(acc.phone)
            if t:
                state_io._write_sign_state(
                    acc.phone, STATUS_PENDING,
                    f"计划 {t.strftime('%H:%M')}", scheduled=t.strftime("%H:%M:%S"),
                )
        accounts = sorted(accounts, key=lambda a: schedule.get(a.phone, datetime.max))
        # 调度快照标记：web 端保存自选时间片时以此时刻为"今日/明日生效"分界——
        # 快照后改选必为明日生效，提示与实际一致（web 侧兜底按"有效窗口起点"折算，
        # 与 cron 实际读取时刻仍有偏差窗口）
        try:
            _snap_dir = env_io.resolve_path("YIBAN_STATE_DIR", "/var/log/yiban")
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
    # 调度 v3 分流（`YIBAN_SCHEDULER_V3`，缺省关；缺省时下面两行逐字不变 = 开关即回滚）。
    # 分流点**选在执行调用这一行**（而不是更早的 `build_schedule` 处）：v3 只换执行体
    # 实现，v2 的容量预检 / 计划写状态文件 / `sched-snapshot` / 账密状态收尾 / 事件批量
    # 落库 / 全量收尾标记 / 退出码汇总全部原样复用——退出码契约 0/1/2/3/10 因此零改动。
    # 代价是 v2 的时间表白算一遍（可接受：它只是本地计算，不发请求、不落库）。
    # `--only` 手动签到不走 v3：用户主动触发应当放行，且它用 reclaim 语义重签已了结账号。
    if not args.only and executor_v3.scheduler_v3_enabled():
        results = executor_v3.run_executor_v3(
            accounts, notify_url=notify_url, cred_state=cred_state,
            event_sink=event_rows.append, delegated=delegated)
    else:
        results = round_mod.run_queue_retry(
            accounts, notify_url, start_delay_max, gap_max, schedule=schedule, cred_state=cred_state,
            event_sink=event_rows.append, delegated=delegated,
            # 手动指定账号（--only）允许重签当日已了结的账号：用户主动点的那一下应当照做
            reclaim=bool(args.only),
        )
    # --only 只能把本次处理账号的熔断增量合并回存量状态（成功→清除该账号记录；
    # 凭据失败→按日累计；其他失败→不动），未处理账号保持原状。
    # 不能用本次（仅含目标账号的）状态整体覆盖保存：空 dict 时会直接删除状态文件，
    # 其他账号的 fail_days/paused_since 全部丢失，账密熔断被任意一次手动签到全局重置。
    # 全量模式语义不变（本轮本就基于存量计算）。
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
            # 手动试探了已暂停账号且凭据仍失败时，顺延下次试探日（对齐全量模式语义）：
            # 否则存量过期的 probe_date 会让下一轮全量签到立即再试探，
            # 失去半开试探的间隔保护
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
    # 已执行=已了结（success/already/no_task），窗口外等跳过不算（补签轮还会再跑）。
    # no_position（易班侧无点位）归入跳过计数但单独展示——非账号失败，不参与
    # has_real_failure；但归入"未了结"（与容器调度器 _UNDONE_STATUSES 同语义），
    # 宿主 exit 2 / 容器补签轮均会重跑一次——学校延迟放位时仍有兜底
    # （无点位账号 1 次即止、幂等无害）。
    has_real_failure = False
    has_executed = False
    # 窗口外/缺失（skipped_window/skipped_norange）属"未了结"——
    # 与容器调度器 _UNDONE_STATUSES（docker/scheduler.py）同一语义。宿主 run.sh 的
    # 补签闸门只认状态文件 SUCCESS 文本：若本轮有成功就把 skipped 账号的退出码判成 0，
    # run.sh 写 SUCCESS → 补签被吞，被跳过的账号当天失去兜底。
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
    # is_second_run：run.sh 补签轮导出的 YIBAN_SECOND_RUN=1 优先（首签子进程被 timeout
    # 击杀、未写 sched-run 标记时标记兜底失效，必须靠该环境变量识别）；容器调度器
    # 补签时段同样注入它；sched-run 标记作为兜底（手动/其他启动路径）。
    alerts._maybe_alert_zero_success(
        accounts, results, ok_n, is_second_run=state_io._is_second_run()
    )

    # 签到事件落库：每次尝试与状态迁移落一行（stage="sign"，探针沿用 stage="probe"），
    # 批量单事务写入；失败仅告警（add_sign_events_batch 内部捕获），不影响退出码。
    if event_rows:
        db.add_sign_events_batch(event_rows)

    # 写按日状态文件（供网页日历组件读取；窗口外跳过不写，当天留空）
    # 符号按状态码：success/already→✅、no_task→➖、failed→❌、no_position→🚫
    state_dir = env_io.resolve_path("YIBAN_STATE_DIR", "/var/log/yiban")
    try:
        os.makedirs(state_dir, exist_ok=True)
        # 以写盘时日期命名（跨午夜不沿用启动时的 attempt_date）
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
            # tmp + os.replace 原子写，避免半截文件
            daily_tmp = daily_path + ".tmp" + str(os.getpid())
            with open(daily_tmp, "w", encoding="utf-8") as f:
                json.dump(daily, f, ensure_ascii=False)
            os.replace(daily_tmp, daily_path)
    except (OSError, ValueError, TypeError) as e:
        logger.warning("写入按日状态文件失败: %s", e)

    # A 线：签到任务彻底结束后，把运行期收集的管理员告警汇总成一封邮件发送。
    # 无异常则不发送（成功不打扰）；mailer 内部静默失败，不影响退出码。
    # 但异常不能逃逸：退出码是 run.sh/调度器的事实源，任何一个未捕获异常都会把
    # 收尾（sched-run 标记、退出码）打断。只记类型名不记 str(e)——异常文本
    # 可能内嵌 URL/token（M10）。
    try:
        alerts._flush_admin_mail_summary()
    except Exception as e:
        logger.warning("签到汇总邮件收尾异常（%s），不影响退出码", type(e).__name__)

    # 全量运行完成标记：调度器首签/补签闸门的事实源。
    # 仅全量模式写入；--only 手动签到不写——手动成功不得压制调度器当日判定。
    if not args.only:
        state_io._write_sched_done({"ok_n": ok_n, "fail_n": fail_n, "skip_n": skip_n})

    # 退出码（run.sh 依据退出码写状态文件）：
    # 0 - 全部成功（有实际签到执行；含"已签到""无需签到"，且无窗口外未了结账号）
    # 1 - 有真正的失败（登录失败、签到失败等）
    # 2 - 全部 skip 或存在窗口外未了结账号（无实际执行，或窗口外账号需补签重跑）
    #     此时必须让 run.sh 写 SKIPPED 而非 SUCCESS，补签 cron 才会继续尝试，
    #     且避免备份等下游任务被吞
    if has_real_failure:
        return 1
    if not has_executed or has_window_skip:
        return 2
    return 0
