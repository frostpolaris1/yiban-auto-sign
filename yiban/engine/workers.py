# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
多执行体：`--workers N` 的监督进程与 `--fallback` 兜底常驻执行体。

两者都是"进程编排"而非签到逻辑本身——真正的活儿都交回 `round.run_queue_retry`，
它们只负责：谁持哪把锁（监督进程持全局锁、子进程各持自己的锁文件）、谁用哪个出口
代理（执行体清单 `YIBAN_EXECUTORS` 每行一个出口；清单缺失时回退旧三键
`YIBAN_PROXY_LIST` / `YIBAN_PROXY_FALLBACK`，见 `egress.resolve`）、每个并行执行体的
**心跳**（开始/存活期/收尾，按**槽位号**写在 `state_io`，供接口判存活四态）、
退出码怎么汇总（取最严重者，但补签轮判定的「需要补跑」原样透出）。清单里的拉起列表由
`runner` 取 `egress.launch_slots` 后按槽位传进来，故**停用行不会被拉起**。

**子进程入口是 `python -m yiban.cli sign`**：本模块是包内模块，不再能按文件路径直接
执行，故监督进程以模块方式拉起同一个 CLI（cwd 与 PYTHONPATH 都指向仓库根）。

**归属**
`yiban.engine` 的进程编排层；`runner` 在"多执行体监督"分支调用它。

**复用**
`run_supervisor` / `run_fallback` 是两条入口；`FALLBACK_LOCK_NAME`、`_GATE_REASON_TEXT`
供同族模块对齐；执行体清单与槽位语义与 `egress` 同源。

**通信**
输入：执行体清单（环境变量 `YIBAN_EXECUTORS`）、槽位数与 `argv`（`--workers` 及其后随值
不透传给子进程）。输出：子进程退出码、心跳与日志。
调用谁：执行路径按 `YIBAN_SCHEDULER_V3` 分流——关时 `round.run_queue_retry`（真正干活），
开时 `executor_v3.run_executor_v3`（兜底身份用 `claim_all` 扫全分片、`requeue_during_run`
会话内回炉默认档）；`state_io`（心跳）、`cli_support`、`egress`。
谁调用：`runner` 的多执行体分支。
前端调用点：执行体存活四态由 `/api/scheduler/executors*` 族读写
（`web/static/js/components/settings-executors.js`、`settings-quota.js`），清单由系统设置页
`/api/settings` 写入 .env——心跳与退出码口径变化会改变这些页面的执行体行与容量提示。
跨模块一律走模块属性访问。
"""
import logging
import os
import subprocess
import sys
import time

from yiban import clock, egress
from yiban import status as yiban_status
from yiban.engine import accounts as accounts_mod
from yiban.engine import cli_support, executor_v3, schedule, state_io
from yiban.engine import round as round_mod
from yiban.store import db, queue_store

logger = logging.getLogger("yiban")

#: 兜底常驻执行体的锁文件名（与定时全量/手动签到并存，互斥交给领取池）
FALLBACK_LOCK_NAME = "signin-run.lock.fallback"

#: **无领取池**（纯状态文件部署）时给全量轮整段让位的轮询间隔（秒）：只是一次 flock
#: 探测。有池的部署不走这条路——让位收窄到"同一账号"，由领取池仲裁谁在做谁。
_YIELD_POLL_SEC = 30

#: 扫空后轮询领取池事件签名的短间隔（秒）：兜底的"失败即入队"读取端。别的执行体
#: 弃权（retry: 档）就是入队，签名一变立刻接手，失败账号最坏只等这一拍而不是等满
#: 一个扫描间隔；只是一次 COUNT/MAX 读，代价远低于一轮签到。等待总预算仍以扫描
#: 间隔为上限（新账号没有池行、无事件可感，照旧由全量节律发现）。
_POOL_WATCH_SEC = 5

#: 三道门（周日/周六未开、一键暂停）的日志措辞——门本身在 `schedule.day_off`（唯一实现），
#: 这里只给兜底自己的说法（定时轮的措辞在 `runner._GATE_SKIP_MESSAGES`）。
_GATE_REASON_TEXT = {
    schedule.DAY_OFF_SUNDAY: "周日签到未开启（系统设置里可开启）",
    schedule.DAY_OFF_SATURDAY: "周六签到已关闭（系统设置里可开启）",
    schedule.DAY_OFF_PAUSED: "管理员已一键暂停签到",
}

# 状态码别名（与 yiban.status 同一对象）
STATUS_FAILED = yiban_status.STATUS_FAILED

#: 仓库根（`yiban/engine/<模块>.py` 上溯三层）。**不是**导入引导（包内模块本就靠
#: `python -m` / 测试的 pythonpath 找到包）：这里只用来给拉起的子进程设 cwd 与
#: PYTHONPATH——`python -m yiban.cli` 要求仓库根在导入路径上。
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 等待子进程退出的轮询粒度（秒）。取 1s：子进程一退出就补收尾标记（页面立刻从
#: "在线"变"已跑完"），又不至于把监督进程变成忙等。心跳的刷新节流由
#: `state_io.WORKER_HEARTBEAT_SEC` 决定，与这个粒度无关。
_WAIT_POLL_SEC = 1.0

#: 补签轮判定「需要补跑」的退出码：与 `runner.SECOND_RUN_CHECK_NEED` 是同一契约值
#: （`docs/dev/cli.md` §3）。此处复写而非从 runner 取，是因为 runner 反向依赖本模块，
#: 导入期取不到它；两处一致性由测试对账。子进程带它退出时监督进程必须原样透出——
#: 归一成 0 会让宿主 run.sh 把「需要补签」读成「一切正常」，补签轮被静默吞掉。
_SECOND_RUN_CHECK_NEED = 10


def run_worker_supervisor(n, argv, slots=None, migrate=True):
    """拉起 n 个执行体子进程并汇总退出码（`--workers N`）。

    - **全局锁由本进程持有**：散落的另一轮全量（cron 与手动）仍会被挡住；
    - 子进程各持自己的锁文件 + 各自的执行体身份（领取池据此分工）；
    - 每个子进程可配一个独立出口代理（`egress.resolve`）；
    - 子进程的 argv 是本进程 argv 去掉 `--workers` 及其后随值（`--workers=N` 同样）后的
      逐字复刻：多执行体的身份靠注入的 `YIBAN_EXECUTOR_ID` 表达，`--workers` 下传只会
      让子进程再当一次监督进程；
    - `slots` = **执行体清单给出的槽位号**（拉起列表，`egress.launch_slots`）。省略时
      用旧口径的 `0..n-1`（行为逐字不变）。传了槽位时子进程的身份/锁/心跳都按
      **槽位号**算，故清单里**停用/删除的行不会被拉起**，且删中间行不影响其余槽位；
    - `migrate`：False = 本轮的配置预检也不跑 schema 迁移（`--check-config` 只读校验；
      子进程各自按同口径处理，监督进程先迁移会把"只读校验"打成写库）；
    - 退出码汇总取"最严重"的一个：schema 迁移完整性拒启(4)（MF-40，整轮不可信）最优先，
      其次补签轮判定的「需要补跑」(10) 原样透出且优先于其余判定
      （它表达调用方必须区分的语义，归一成 0 会让补签轮被静默吞掉），再其后才是
      真失败(1) > 锁忙(3) > 跳过/窗口外(2) > 全成功(0)。**任何**非零子退出码都会被
      接住：信号杀（负数）与契约外的未知码统一折进真失败(1)——"其余归 0"曾让
      被 SIGKILL 的执行体把整轮报成成功，宿主据此写 SUCCESS 并弹开当日恢复腿；
    - 全量完成标记（sched-run-<日期>.json）由**本函数单点写**（仅在全部子执行体
      正常退出、且无迁移拒启时写一次）；子执行体不各写一份，"当日全量已收尾"
      因此重新等于事实；
      调用方（run.sh）据此判断本轮是否需要补签，语义与单执行体一致；容器侧不消费本
      退出码（docker/scheduler.py 的补签闸门读状态文件判定）。
    - 全局锁拿不到时**不拉起任何子进程**，直接以 3（锁忙族）返回（fail-closed，
      理由见函数体内注释）；被信号杀死的子进程（返回码为负）在轮末由
      `_reap_dead_worker` 显式了结它在领的行。
    """
    slot_list = list(range(n)) if slots is None else list(slots)
    n = len(slot_list)
    # 全局锁：本进程持有直到子进程全部结束（句柄必须保活，不能只用一次就丢）。
    # 拿不到即拒绝拉起：无锁跑等于"散落的另一轮全量"与本轮执行体同时签同一批账号，
    # 而领取池只保证同一账号被领一次、并不阻止两轮各自把没领到的当"别人的活"。
    # 退出码取 3（"锁忙"族，与 `runner` 的手动/兜底分支同一处置），不新增码值。
    try:
        _global_lock = cli_support._acquire_run_lock(False)
    except cli_support._RunLockUnavailable as e:
        logger.error("多执行体：运行锁不可用，本次拒绝拉起执行体: %s", e)
        return 3
    # 先在本进程把库初始化/迁移做完并校验账号配置：否则 N 个子进程会在同一秒
    # 抢着 init_db（实测 `PRAGMA journal_mode=WAL` 会报 "database is locked"），
    # 而且配置错误的报错会变成 N 份、互相淹没。
    # `--check-config` 只读校验不在此迁移（migrate=False）：否则宣称只读的校验
    # 仍会把目标库改一遍（2026-09-21 测试机 47 E2E）。
    try:
        loaded_accounts = accounts_mod.load_accounts(migrate=migrate)
    except db.MigrationIntegrityError as e:
        # 迁移完整性拒启（MF-40）：与 runner.main 同口径——点名缺哪条迁移，独立码 4
        # （0/1/2/3/10 家族只增不改），不得折叠进配置错误(1)。
        logger.error(f"schema 迁移完整性校验失败，拒绝启动: {e}")
        cli_support.report_fatal_error(
            f"schema 迁移完整性校验失败，拒绝启动: {e}")
        return cli_support.EXIT_SCHEMA_MIGRATION
    except (RuntimeError, ValueError) as e:  # ValueError=账号字段缺失，同按配置错误处理
        logger.error(f"配置加载失败: {e}")
        cli_support.report_fatal_error(f"配置加载失败: {e}")
        return 1
    if not loaded_accounts:
        logger.error("未配置任何账号，不拉起执行体")
        cli_support.report_fatal_error(
            "未配置任何账号：请配置 yiban.db（网页后台添加）、YIBAN_ACCOUNTS_JSON、"
            "YIBAN_ACCOUNTS 或 YIBAN_PHONE/YIBAN_PASSWORD（配置方法详见日志）")
        return 1
    logger.info("多执行体：共 %d 个账号待签，拉起 %d 个执行体（槽位 %s）",
                len(loaded_accounts), n, slot_list)
    children = []
    for i, slot in enumerate(slot_list):
        env = os.environ.copy()
        # 身份串的唯一构造处在 egress（写入与解析同一份口径）：这里传的稳定槽位名
        # `worker-{槽位}@{主机名}` 跨重启不变（界面对象、槽位号与持久化键的口径），
        # 但**不再等于重启后立刻认领自己上一轮的在飞账号**——重入须出示上一代 epoch，
        # 重启后的新进程没有它，只能等租约过期或心跳回收。代价是同一槽位名不得两台机器
        # 同时跑（跨主机靠 @主机名 区分，同机由本进程持有的全局锁 signin-run.lock 挡住，
        # 故那把锁不能去掉）。
        env["YIBAN_EXECUTOR_ID"] = egress.worker_owner(slot)
        env["YIBAN_RUN_LOCK_NAME"] = f"signin-run.lock.w{slot}"
        proxy = egress.resolve(egress.ROLE_WORKER, slot)
        if proxy:
            env["YIBAN_PROXY"] = proxy
        # 复刻本轮其余参数：剔除 `--workers` **及其后随值**（按 argv 序位解析，与
        # argparse 同语义；`--workers=N` 形态一并剔除）。不能按值匹配：清单模式下拉起
        # 的槽位数与命令行 `--workers N` 的 N 可以不等——网页改执行体清单只写
        # YIBAN_EXECUTORS、不回写 YIBAN_WORKERS，于是 N 留在 argv 里变成子进程的位置
        # 参数，argparse 直接报错退出 → 每个执行体都零请求退出，全天静默不签到。
        child_argv = []
        _skip_next = False
        for a in argv:
            if _skip_next:          # 上一个是 `--workers`：本项是它的后随值，一并剔除
                _skip_next = False
                continue
            if a == "--workers":
                _skip_next = True
                continue
            if a.startswith("--workers="):
                continue
            child_argv.append(a)
        # 子进程入口：模块方式执行同一个 CLI（cwd=仓库根 + PYTHONPATH 含仓库根，
        # `python -m` 才能找到 yiban 包）。子进程收到的命令行参数与旧版逐字相同。
        cmd = [sys.executable, "-m", "yiban.cli", "sign", *child_argv]
        logger.info("执行体 %d/%d 启动（槽位 %d，出口: %s）",
                    i + 1, n, slot, egress.describe(proxy))
        children.append(subprocess.Popen(
            cmd, env=_child_env_with_repo_root(env), cwd=_REPO_DIR,
        ))
        # 开始心跳：本槽位"本轮已启动"的事实。放在 Popen 之后，故页面上"在跑"的
        # 槽位必然真有子进程（不是拿"文件在不在"猜）。
        state_io.mark_worker_started(slot)
        # 错开启动：既避开"同一秒争库"，也让首轮请求不要在同一瞬间齐发（风控面）
        if i + 1 < n:
            time.sleep(0.5)

    codes = _await_workers(children, slot_list)
    for i, rc in enumerate(codes):
        logger.info("执行体 %d/%d（槽位 %d）结束，退出码 %s", i + 1, n, slot_list[i], rc)
    # 轮末收尸：被信号杀死的执行体（rc < 0）来不及自己收尾，它在领的账号会一直挂着
    # `claimed` 到 900s 租约过期才可能被别人接管。监督进程**直接观测到了**这次异常退出
    # （比"心跳过期"更强的证据），故在轮末显式了结这些行，不留给下一轮按在飞误判。
    for i, rc in enumerate(codes):
        if rc is not None and rc < 0:
            _reap_dead_worker(slot_list[i])

    # rc 契约（宿主 run.sh 与容器读的是**同一份码**）：任何非零子退出码都必须被汇总
    # 结果接住，**不得归 0**——`Popen.poll()` 对信号杀返回负数（-9 = SIGKILL），
    # 外部击杀/解释器异常还可能给出契约外的正码（如 124）；这些旧实现一律折进
    # "其余归 0"，宿主据此写 SUCCESS，补签判定被同一份坏 rc 否决、当日恢复腿整条弹开。
    # 负数与未知码统一折进"真失败"(1)，码值不新增；已知优先序逐字不变。
    if any(c == cli_support.EXIT_SCHEMA_MIGRATION for c in codes):
        # 迁移完整性拒启（MF-40）：任一执行体判定 schema 半升级，本轮账目整体不可信
        # ⇒ 优先于补签判定透出。
        _round_settled = False
        _final = cli_support.EXIT_SCHEMA_MIGRATION
    elif any(c == _SECOND_RUN_CHECK_NEED for c in codes):
        _round_settled = all(_settled_child_code(c) for c in codes)
        _final = _SECOND_RUN_CHECK_NEED
    elif any(c is None or c < 0 or c == 1 or c not in (0, 1, 2, 3) for c in codes):
        # 真失败(1)：契约内的 1 原样透出；信号杀（负数）、`None`、契约外的未知码
        # （外部击杀的 124、解释器异常的杂码）**统一折进同一支**——rc 契约不新增
        # 码值，两种来源共享"真失败"这一个处置。
        _round_settled = all(_settled_child_code(c) for c in codes)
        _final = 1
    elif any(c == 3 for c in codes):
        _round_settled = all(_settled_child_code(c) for c in codes)
        _final = 3
    elif any(c == 2 for c in codes):
        _round_settled = all(_settled_child_code(c) for c in codes)
        _final = 2
    else:
        _round_settled = all(_settled_child_code(c) for c in codes)
        _final = 0
    # 全量完成标记（sched-run-<日期>.json）**单点写在本函数**：旧实现由每个子执行体
    # 在 `runner.main` 末尾各写一份——先收尾的那份会把"还有执行体被杀/没跑完"的
    # 事实盖掉，"当日全量已收尾"从此不可信。现在只有监督进程在全员正常退出后写一次；
    # 子执行体侧的写入口按身份（`YIBAN_EXECUTOR_ID`）关停，见 `runner.main`。
    # 只读校验形态（--check-config / --probe / --second-run-check 派发给子执行体）
    # 不作证"全量已收尾"：那类轮次一个账号都不签，写标记等于谎报。
    _readonly_round = any(a in ("--check-config", "--probe", "--second-run-check")
                          for a in argv)
    if _round_settled and not _readonly_round:
        state_io._write_sched_done()
    return _final


def _settled_child_code(rc):
    """该子退出码是否代表"跑到了自己的收尾"（可为全量完成标记作证）。

    只认契约内的正常退出族（0/1/2/3 与透出的 10）：负数（信号杀）、`None`、迁移拒启(4)
    以及契约外的未知码（外部击杀给出的 124、解释器异常的杂码）**都不算**——这些情形
    执行体没走到自己的收尾链路，整轮是否了结必须由下一触发按库内事实重判，
    标记一次都不该替它说"已做完"。
    """
    return rc in (0, 1, 2, 3, _SECOND_RUN_CHECK_NEED)


def _reap_dead_worker(slot):
    """轮末收尸：显式了结某个**已确认死亡**的执行体槽位名下的在领记录。

    判据不是心跳而是"监督进程看到它异常退出（返回码为负）"：这与"租约过期 ⇒ 可能死了"
    是两个强度不同的证据——此处是**已知死亡**，故不必等满 900s。只按槽位身份前缀匹配
    （`claims.reap_abandoned`），已被别人接管的行 owner 已换、不会被误动；代次同时自增，
    任何迟到的旧代写仍被 fence。

    **两套持有记录都要收**：旧领取表 `sign_claims`（`claims.reap_abandoned`）与 v3 任务
    队列 `sign_tasks`（`queue_store.reap_abandoned`）各自记着自己的在领行，只收一侧会让
    另一侧的行干等到 `reap_expired` 的租约 + 宽限期。两者身份前缀同源（都取本槽位的稳定
    槽位名），故同一个 `owner` 传两处。

    收尸失败只留日志：它不影响本轮的退出码汇总，下一轮起跑仍会走"租约过期接管"兜住。
    """
    stable = egress.worker_owner(slot)
    try:
        n = db.claim_reap_abandoned(stable)
    except Exception as e:
        logger.debug("轮末收尸失败（不影响退出码，下一轮仍可接手）: %s", e)
        n = 0
    if n:
        logger.warning("执行体槽位 %d 异常退出，轮末收尸：%d 条在领记录已显式了结", slot, n)
    try:
        m = queue_store.reap_abandoned(stable)
    except Exception as e:
        logger.debug("轮末收尸 v3 任务失败（不影响退出码，下一轮仍可接手）: %s", e)
        return
    if m:
        logger.warning("执行体槽位 %d 异常退出，轮末收尸：%d 条在领任务已回退待领", slot, m)


def run_fallback_worker(argv_rest, interval=None, deadline=None):
    """兜底常驻执行体：窗口内反复扫"还没了结"的账号并接手，窗口关闭即退出。

    为什么需要它：学校晚放号、窗口内新审核通过的账号、被慢账号拖住的、失败待重试的
    ——都能被**随手接手**，而不是等下一轮定时任务。

    做法刻意简单可靠：每一轮重新加载账号并调用同一条执行路径（v2 为 `run_queue_retry`，
    schedule 为空=立即执行；`YIBAN_SCHEDULER_V3` 开闸时分流到 `run_executor_v3`——
    只换执行体实现，档位纪律不变，见分流处注释）。**分工由领取池/任务队列承担**：
    已了结的账号领不到、别的执行体正在做的领不到，所以"全量账号列表"作为输入也不会
    重复签——不需要在这里再写一套筛选。

    独立锁 `signin-run.lock.fallback` **由调用方**（`runner.main` 的兜底分支）取：
    本函数只把锁名写进环境变量，好让子路径/日志口径一致；撞上已在跑的兜底时由调用方
    退出 3。故它与定时全量、手动签到、其他执行体都能并存（账号级互斥交给领取池）。

    **运行前会先过四道关**（每轮重判，不是启动时判一次）：周末签到未开、一键暂停
    （都由 `schedule.day_off` 判定，与定时轮同源）→ 直接退出；签到时段**尚未开始**
     → 等到开始再扫（提前拉起是 cron 模板的常态，窗口外发请求等于白登陆一次）；
     **全量轮正在跑且无领取池**（纯状态文件部署，没有账号级互斥可依赖）→ 整段让位。
    有池在场时**让位只让"同一账号"**：全量轮持锁期间照常扫，池对在飞/已了结的账号
    拒领（见循环内注释），该轮没碰的与中途弃权的照接。扫空后的等待是**事件驱动**的：
    短轮询领取池"默认可接手未了结"签名（`_await_pool_event`），别的执行体一弃权
    就接手，等待上限仍是扫描间隔。故它是**捡漏**的那个——与定时轮/多执行体并发，
    但"谁在做谁"由池仲裁，第一红线（同一账号当日一次真实登录）不因此松动。

    退出：窗口关闭 / 三道门命中 / 到达 `deadline` / 账号列表为空且已过窗口。
    返回退出码语义与单执行体一致（0 全成功、1 有真失败、2 存在窗口外未了结）。
    """
    interval = interval or schedule._env_int("YIBAN_FALLBACK_INTERVAL", 60, 5, 3600)
    os.environ.setdefault("YIBAN_RUN_LOCK_NAME", FALLBACK_LOCK_NAME)
    # 身份串：兜底常驻与单执行体/并行执行体各用**稳定的槽位名**（`fallback@{主机名}`），
    # 跨重启不变只服务界面与持久化键；**不等于重启后立刻接手自己上一轮的在飞账号**——
    # 重入须出示上一代 epoch，重启后没有它，只能等租约过期或心跳回收。代价是同一槽位名
    # 不得两台机器同时跑（跨主机靠 @主机名 区分，同机由下面那把 `signin-run.lock.fallback` 挡住）。
    os.environ.setdefault("YIBAN_EXECUTOR_ID", egress.fallback_owner())
    proxy = egress.resolve(egress.ROLE_FALLBACK)
    logger.info("兜底执行体启动（出口: %s，扫描间隔 %ss）", egress.describe(proxy), interval)
    if proxy:
        os.environ["YIBAN_PROXY"] = proxy

    sch_cfg = None   # 每轮在循环内重读（见下）
    last_code = 0
    while True:
        now = clock.now()
        # 配置每轮重读，并把本轮时刻交给 `_schedule_config`（与下面 `day_off` 的每轮重判
        # 同口径：管理员中途改窗口/开关，下一轮即生效）；顺手传时刻是为了让它的告警复位点
        # 不必再取一次时钟——常驻进程每秒级的取时不该翻倍。
        sch_cfg = schedule._schedule_config(now)
        if deadline is not None and now >= deadline:
            logger.info("兜底执行体：到达截止时刻，退出")
            break
        # 周末门 / 一键暂停门：走**与定时轮同一实现**（schedule.day_off）。
        # 这两道门原先只写在 runner.main 里，而本进程在它之前就 return（见分支顺序），
        # 于是管理员关掉周末签到或点了一键暂停，兜底照签（2026-09-17 实测）。
        # 每轮重判（不是启动时判一次）⇒ 窗口中途改设置也能在下一次扫描生效。
        gate = schedule.day_off(now)
        if gate:
            logger.info("兜底执行体：%s，退出（本日不签到）", _GATE_REASON_TEXT[gate])
            break
        if schedule._window_closed(sch_cfg, now):
            logger.info("兜底执行体：签到时段已结束，退出")
            break
        if not schedule._window_open(sch_cfg, now):
            # 提前拉起（cron 模板 06:05、窗口 06:30）时**必须等**，不能照走：
            # 窗口外每次尝试都是一次真实登录（易班侧照实计数），而且可能把账号签在
            # 管理员配置的窗口之外——`run_queue_retry` 的手动链路本身不判本项目的窗口。
            opens_in = int(schedule._window_opens_in(sch_cfg, now))
            wait = min(interval, max(1, opens_in))
            logger.info("兜底执行体：签到时段尚未开始（%d 秒后开始），%d 秒后再看", opens_in, wait)
            time.sleep(wait)
            continue
        if not db.pool_db_declared() and cli_support._run_lock_held():
            # **让位的颗粒度是"全量轮正在做的那一个账号"，仲裁者是领取池不是这把锁**：
            # 有池在场时这里不让位、照常扫——轮在飞的行 `try_claim` 拒领（在飞未过期
            # =领不到，done=领不到，retry: 档弃权行=默认可接手），兜底只接"该轮没碰的
            # / 该轮中途弃权的"账号。旧形态"全局锁被持有=整段停摆"让兜底恰好在失败
            # 高峰（全量轮正在批量弃权）时段完全不可用——那正是它该捡漏的时刻。
            # 例外是**没有池可仲裁**的纯状态文件部署：账号级互斥不存在，并发跑等于
            # 同一账号两次真实登录（第一红线），所以只有那里保留整段停摆。
            # 让位期间的轮询比常规扫描密：全量轮一结束就接手，而这次探测只是一次 flock。
            logger.info("兜底执行体：全量轮正在运行且无领取池，整段让位（%d 秒后再看）",
                        _YIELD_POLL_SEC)
            time.sleep(_YIELD_POLL_SEC)
            continue

        state_io._write_fallback_alive(now)
        try:
            accounts = accounts_mod.load_accounts()
        except db.MigrationIntegrityError as e:
            # 迁移完整性拒启（MF-40）：schema 半升级不是配置错误，独立码 4 透出，
            # 常驻循环不得吞掉它继续空转。
            logger.error(f"兜底执行体：schema 迁移完整性校验失败，拒绝启动: {e}")
            state_io._clear_fallback_alive()
            return cli_support.EXIT_SCHEMA_MIGRATION
        except (RuntimeError, ValueError) as e:  # ValueError=账号字段缺失，同按配置错误处理
            logger.error("兜底执行体：配置加载失败: %s", e)
            state_io._clear_fallback_alive()
            return 1
        if not accounts:
            logger.info("兜底执行体：当前没有账号，%ss 后再看", interval)
            time.sleep(interval)
            continue

        delegated = set()
        # 本轮熔断状态快照：`read()` 每轮返回新 dict，执行路径就地改它，
        # 故必须持有引用以便轮末写回（不能像过去那样现取现传、写回时已无对象）。
        cred_state = state_io._load_cred_state()
        if executor_v3.scheduler_v3_enabled():
            # v3 分流（开关每轮重读，与窗口/三道门的重判同节拍）：兜底腿只换执行体
            # 实现，档位纪律不变。
            # - `claim_all`：兜底身份不是执行体清单成员，HRW 分片集对它为空——不放宽
            #   领取范围就是"看着在跑、零领取"的静默空转；互斥仍由 state+epoch 门兜住。
            # - `requeue_during_run`：本轮会话刚弃权的 retry: 档由恢复周期就地回炉，
            #   这是兜底"失败当日接手"的 v3 等价物，不等下一场会话。
            # - **不传 `requeue_final`**：兜底是常驻无界循环，不是"有界的一次性显式
            #   路径"（补签轮/手动才传）——每 ~60s 重扫一遍的循环若把预算耗尽/风控档
            #   与无前缀历史行一并复活，等于让熔断账号每轮再真实登录一次。这类账号的
            #   第二次机会只留给一次性补签轮与手动。
            results = executor_v3.run_executor_v3(
                accounts, day=now.strftime("%Y-%m-%d"),
                notify_url=os.environ.get("YIBAN_NOTIFY_URL", ""),
                cred_state=cred_state, delegated=delegated,
                claim_all=True, requeue_during_run=True)
        else:
            results = round_mod.run_queue_retry(accounts, os.environ.get("YIBAN_NOTIFY_URL", ""), 0,
                                                schedule._env_int("YIBAN_ACCOUNT_GAP_MAX", 10, 0, 3600),
                                                schedule=None, cred_state=cred_state,
                                                delegated=delegated,
                                                # 兜底是"替全量轮捡漏"：窗口已关就该停手，
                                                # 一轮扫描内部不再对剩余账号发起真实登录
                                                window_guard=True,
                                                # **不给 retry_failed**（取默认 False）：它要求调用方
                                                # 是"有界的一次性显式路径"（补签轮/手动 `--only`），
                                                # 而兜底是常驻无界循环、每 ~60s 就重扫一遍——传 True
                                                # 会让预算耗尽/风控档账号在窗口内每轮都被重领重登一次
                                                # （一轮扫描一次真实登录，四小时窗口下以百计）。
                                                # 这类账号的第二次机会只留给一次性补签轮与手动。
                                                # v3 分支的 `requeue_final` 缺省同为纪律另一面。
                                                )
        # 轮末写回熔断计数（口径与 runner 全量轮一致：按本轮账号增量合并）。不写回则
        # "连续凭据失败达阈值 → 暂停"只在磁盘上不存在：下一轮 read() 又从零开始，
        # 错密码账号被无限次真实登录（易班侧照实计数，加重风控）。
        state_io._save_cred_state(cred_state, touched={a.phone for a in accounts})
        settled = 0
        for acc in accounts:
            if acc.phone in delegated:
                settled += 1
        own = len(results)
        # 本轮自己没活干（全部已被别人接手/已了结）→ 睡一会儿再看
        logger.info("兜底执行体：本轮处理 %d 个账号（%d 个已由他人负责），%ss 后再扫",
                    own, settled, interval)
        for _ok, _msg, skip, status in results.values():
            if status in (STATUS_FAILED,) and not skip:
                last_code = 1
        if own == 0:
            if db.is_initialized():
                # **事件驱动**：扫空后不再盲睡满间隔，而是轮询领取池的事件签名——别的
                # 执行体把账号弃权到默认档就是"入队"（见 `claims.fallback_event`），
                # 一变即接手。等待总预算仍是 interval：新审核账号没有池行、无事件可感，
                # 由全量节律兜底发现。
                _await_pool_event(now.strftime("%Y-%m-%d"), interval)
            else:
                # 无池可轮询（纯状态文件部署）：退回盲的整段间隔原节律
                time.sleep(interval)
        else:
            # 有活干就连续扫（不睡满间隔），直到没活为止——窗口是有限的
            time.sleep(min(interval, 5))
    state_io._clear_fallback_alive()
    logger.info("兜底执行体已退出（心跳已清除）")
    return last_code


def _await_pool_event(day, budget_sec):
    """扫空后的等待：短轮询领取池事件签名，变化即返回，安静则等满 `budget_sec`。

    取舍（对"失败即入队"字面另建一条队列）：全量轮与兜底是**两个进程**，`give_up`
    在同一事务里把行置 failed 并即刻放开租约——这一行迁移本身就是入队动作，队列就是
    领取池，`try_claim` 就是出队。再造一条独立队列必然与领取层漂移成"谁持有谁"的两套
    事实；因此这里的事件源是池的签名，不是新管道。签名只数兜底默认档接得动的行
    （`fallback_event`：`retry:` 档、剔除自己的弃权），唤醒频率与可接手频率同集。
    读不到签名（库抖动）按"无事件"处理——事件驱动是延迟优化、不是正确性依赖，
    最坏仍由扫描间隔这个上限兜住。
    """
    owner = os.environ.get("YIBAN_EXECUTOR_ID", "").strip() or egress.fallback_owner()
    last = db.claim_fallback_event(day, owner)
    # 按剩余预算切分睡眠：节拍取 `min(间隔, 剩余)`——节拍数向上取整会睡超预算，
    # 预算短于节拍时更不该被迫睡满一整拍才到点。
    remaining = float(budget_sec)
    while remaining > 0:
        step = min(_POOL_WATCH_SEC, remaining)
        time.sleep(step)
        remaining -= step
        sig = db.claim_fallback_event(day, owner)
        if sig is None:
            continue
        if sig != last:
            logger.info("兜底执行体：领取池事件签名变化 %s（业务日 %s），立即接手", sig, day)
            return
        last = sig


def _await_workers(children, slots=None):
    """等全部子进程结束，期间按心跳周期刷新各槽位心跳；返回按槽位排列的退出码。

    `slots` = 各子进程对应的**槽位号**（省略 = 旧口径的 `0..len-1`）：心跳文件按槽位
    命名，清单模式下槽位号可以不连续（删中间行不重排），故不能拿"第几个子进程"当槽位。

    为什么不再逐个 `child.wait()`：心跳要覆盖**进程存活期间**（一轮可能十几分钟），
    只在开始/结束两个时刻写盘会让长轮次过了 2 × 周期就被判成"过期未收尾"、页面把
    正常在跑的轮次报成异常。轮询 `poll()` 在子进程退出的下一秒就能补收尾标记；
    退出码语义与逐个 wait **完全一致**（仍是每个子进程的真实返回码，按槽位排列）。

    收尾标记只在**正常退出**（返回码 >= 0）时写：被信号杀掉（返回码为负，如宿主
    `timeout` 的 SIGTERM/SIGKILL）时留"有开始、无收尾"，心跳过期后由接口判成
    `stale`——那正是需要用户注意的那种异常。
    """
    slots = list(range(len(children))) if slots is None else list(slots)
    codes = [None] * len(children)
    alive = set(range(len(children)))
    last_beat = {i: time.monotonic() for i in alive}
    while alive:
        time.sleep(_WAIT_POLL_SEC)
        for i in sorted(alive):
            rc = children[i].poll()
            if rc is None:
                if time.monotonic() - last_beat[i] >= state_io.WORKER_HEARTBEAT_SEC:
                    state_io.mark_worker_beat(slots[i])
                    last_beat[i] = time.monotonic()
                continue
            alive.discard(i)
            codes[i] = rc
            if rc >= 0:
                state_io.mark_worker_finished(slots[i], rc)
    return codes


def _child_env_with_repo_root(env):
    """在子进程环境里确保仓库根在 `PYTHONPATH` 上（已含则不重复追加）。"""
    paths = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if _REPO_DIR not in paths:
        paths.insert(0, _REPO_DIR)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env
