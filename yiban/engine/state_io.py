# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""状态文件读写与判定：按日状态、全量收尾标记、账密熔断状态、执行体心跳。

两类执行体心跳都在这里：兜底常驻的执行体心跳（写侧 `_write_fallback_alive`、读侧
`fallback_alive`）与并行执行体心跳（写侧 `mark_worker_*`、读侧四态判定的
`worker_presence`）。

这些文件是**跨进程事实源**：网页日历读按日状态，容器调度器与宿主 `run.sh` 读全量
收尾标记与"未了结账号"，告警读兜底心跳与 sched-run 标记，执行体接口读并行心跳。
因此三条纪律不能破：

1. **原子写**（tmp + `os.replace`）——半截 JSON 会被下游误读成"没跑过"；
2. **带锁读改写**（`cli_support._state_file_lock`）——签到主进程、手动 `--only`、
   探针三个进程会同时碰这些文件；
3. **失效方向偏安全**——文件缺失/损坏一律按"尚未了结"处理，宁可多跑一轮不漏签。

状态码别名取自 `yiban.status`（唯一事实源）。

**依赖方向**：本模块只额外依赖 `schedule` 的 `_env_int`（兜底心跳的扫描间隔下限），
而 `schedule` 不依赖本模块，故模块级导入安全；`alerts` 反向依赖本模块（告警要读
sched-run 标记与心跳）与 `schedule`（判窗口是否还开着），那条环路由 alerts 侧断开。

跨模块调用纪律见包说明：跨模块一律走模块属性访问。
"""
import contextlib
import json
import logging
import os
from datetime import datetime

from yiban import clock, cred_state
from yiban import status as yiban_status
from yiban.engine import cli_support, schedule
from yiban.masking import sanitize_text as _sanitize_text
from yiban.store import db

logger = logging.getLogger("yiban")

# 状态码别名（与 yiban.status 同一对象）
STATUS_SUCCESS = yiban_status.STATUS_SUCCESS
STATUS_ALREADY = yiban_status.STATUS_ALREADY
STATUS_NO_TASK = yiban_status.STATUS_NO_TASK
STATUS_PENDING = yiban_status.STATUS_PENDING

#: 兜底执行体的心跳文件（刷新时间戳；退出时删除）。用途：让"还有没有下一轮兜底"
#: 变成可查的**事实**，而不是靠猜时刻——告警抑制与网页展示都用它。
FALLBACK_ALIVE_FILE = "fallback-alive.json"

# 并行执行体心跳（写侧：监督进程；读侧：`worker_presence`）。
# 语义前提：并行执行体是**一轮就退出的短命进程**，不是常驻服务——"没在跑"在多数时间
# 是正常的（定时任务还没到 / 本轮已完成），故判定回四态而不是 alive 布尔，只有
# "有开始、无收尾且心跳过期"才判成异常，详见 `worker_presence`。
# 心跳文件用**固定名、每轮覆盖**（不按日命名）：文件数 = 执行体数，不随时间增长，
# 因此不需要清理策略。
#: 心跳文件名前缀：`worker-alive-<槽位序号>.json`。
WORKER_ALIVE_FILE_PREFIX = "worker-alive-"
#: 心跳周期（秒）。写侧按它节流刷新心跳，读侧按 2 × 它判"新鲜"。
WORKER_HEARTBEAT_SEC = 30
#: 四态取值（接口原样回给前端）。
WORKER_STATE_RUNNING = "running"
WORKER_STATE_FINISHED = "finished"
WORKER_STATE_IDLE = "idle"
WORKER_STATE_STALE = "stale"

#: 状态文件里的时间串格式（与全库统一口径一致）。
_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _has_conclusion(entry):
    """该账号当日是否已有"结论"（非空且非 pending）。

    `pending` 只是计划（"打算什么时候签"），success/failed 等才是事实；"空/缺失"
    等价于无记录。`_write_sign_state` 的 only_if_absent CAS 与窗口收尾快照共用
    这一口径。
    """
    if not isinstance(entry, dict):
        return False
    return str(entry.get("status", "")).strip() not in ("", STATUS_PENDING)


def _sched_marker_exists():
    """当日全量运行标记（sched-run-<date>.json）是否已存在。

    告警函数用它区分「首签轮」与「补签轮」——
    标记在首签轮收尾写入（_write_sched_done），因此：
      - 首签轮调用本函数时标记尚不存在 → 本轮是首签；
      - 补签轮（07:10）调用时标记已存在 → 本轮是补签。
    与容器 scheduler.py 的 _full_run_done_today() 语义一致（同一事实源）。
    """
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    path = os.path.join(state_dir, f"sched-run-{clock.now().strftime('%Y-%m-%d')}.json")
    return os.path.exists(path)


def _is_second_run():
    """本轮是否为补签轮（07:10）：run.sh 补签轮 / 容器 scheduler SECOND 时段注入的
    YIBAN_SECOND_RUN=1 优先，sched-run 标记兜底。

    首签子进程被宿主 timeout 击杀（exit 124）时收尾未执行、sched-run
    标记不写，07:10 补签轮仅靠标记会误判为首签轮 → 部分成功+窗口外零告警（B12-2
    分支复发）。环境变量由 run.sh 补签轮分支 / 容器 scheduler SECOND 时段显式注入，
    不依赖首签收尾，天然免疫 exit 124。
    """
    return os.environ.get("YIBAN_SECOND_RUN") == "1" or _sched_marker_exists()


def _sign_state_path():
    """当日 sign-state 状态文件路径（状态目录缺失/不可写由调用方处理）。"""
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    return os.path.join(state_dir, f"sign-state-{clock.now().strftime('%Y-%m-%d')}.json")


def _daily_statuses():
    """当日按日状态文件的 {phone: status}；缺失/损坏返回 {}（调用方按"无记录"处理）。"""
    try:
        with open(_sign_state_path(), encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {p: (v.get("status") if isinstance(v, dict) else "") for p, v in data.items()}


def _second_run_drop_done(accounts):
    """补签轮定向重跑：剔除当日已了结（success/already）的账号。

    依据当日 sign-state 状态文件（与容器调度器 _has_undone_today 同一事实源）：
    存在未了结账号才触发的补签轮此前会整站重跑，把当日已 success 的账号
    再次完整登录（风控暴露）。文件缺失/损坏时按「无记录」处理返回全量
    （宁可多跑，不可漏签）。
    """
    recorded = _daily_statuses()
    if not recorded:
        return accounts
    # 已了结 = success/already/**no_task**（按 main 自身的"已执行"口径：no_task 指
    # "今天没任务"，同样无需重跑）。原实现漏了 no_task，补签轮会对这些账号再走一遍
    # 完整登录——多一轮全站真实登录，且与 UNDONE_STATUSES 口径矛盾。
    done = {
        p for p, st in recorded.items()
        if st in (STATUS_SUCCESS, STATUS_ALREADY, STATUS_NO_TASK)
    }
    if not done:
        return accounts
    kept = [a for a in accounts if a.phone not in done]
    logger.info(
        "补签轮定向重跑：%d 个账号当日已了结不再重跑，本次执行 %d 个",
        len(accounts) - len(kept), len(kept),
    )
    return kept


def _write_sign_state(phone, status, message, scheduled=None, dur=None,
                      only_if_absent=False):
    """写按日结构化状态文件（web 状态显示的事实源，原子替换防半截文件）。

    文件：{YIBAN_STATE_DIR}/sign-state-YYYY-MM-DD.json
    结构：{phone: {status, message, time, task}}；task 预留多时段/多星期签到扩展。
    scheduled：今日计划签到时间（HH:MM:SS，自动错峰分配后写入，执行后保留）。
    dur：单次签到尝试耗时秒数（P6，2026-08-16：慢响应可据此判断网络/接口问题）。
    only_if_absent：仅当该账号**当日无结论**时才写入（return True），否则不覆盖
    并返回 False。窗口关闭收尾（`_mark_window_skip`）用它做 CAS：快照判"无记录"
    与落盘之间，另一执行体可能刚写入真实结论（failed 等）——锁内再判一次，
    绝不让"窗口外跳过"覆盖掉真实失败（否则 `has_real_failure` 变 False、失败告警
    被吞）。
    状态目录不可写时丢弃，不影响签到执行。
    """
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    path = _sign_state_path()
    try:
        os.makedirs(state_dir, exist_ok=True)
        # M12：读-改-写整体持有状态文件锁，避免并发覆盖丢失条目
        with cli_support._state_file_lock(path):
            data = {}
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError, TypeError, AttributeError):
                    logger.warning("状态文件 %s 损坏，按空数据重建", path)
                    data = {}
            if not isinstance(data, dict):
                logger.warning("状态文件 %s 非 dict，按空数据重建", path)
                data = {}
            now = clock.now()
            # 计划时间是当日事实：后续写入（执行结果/重试）未显式传 scheduled 时保留既有值
            existing = data.get(phone)
            if not scheduled and isinstance(existing, dict):
                scheduled = existing.get("scheduled")
            if only_if_absent and _has_conclusion(existing):
                return False
            # **计划态不覆盖已有结果**：`pending` 是"打算什么时候签"的预测，success/failed
            # 等是"已经发生"的事实——事实优先。单执行体形态下计划写在前、结果写在后，看不出
            # 差别；多执行体下每个执行体启动都会写一遍全量计划，晚启动者的计划会把先启动者
            # 已写完的结果抹回 pending（实测：4 执行体 40 账号，2 个账号被抹成 pending），
            # 既让日历显示"待签"，又让补签闸门把已签账号当未了结重跑一遍。
            if (
                status == STATUS_PENDING
                and isinstance(existing, dict)
                and str(existing.get("status", "")).strip() not in ("", STATUS_PENDING)
            ):
                if scheduled:
                    existing["scheduled"] = scheduled
                data[phone] = existing
                tmp = f"{path}.tmp{os.getpid()}"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp, path)
                return True
            entry = {
                "status": status,
                "message": message,
                "time": now.strftime("%H:%M:%S"),
                "task": "default",
            }
            if dur is not None:
                entry["dur"] = round(float(dur), 2)  # 单次尝试耗时秒数（P6）
            if scheduled:
                entry["scheduled"] = scheduled
            data[phone] = entry
            # 唯一临时名：防跨进程（cron + 手动 --only 并发）固定 .tmp 名互相覆盖（对抗性审查发现）
            tmp = f"{path}.tmp{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
    except (OSError, ValueError, TypeError, AttributeError) as e:
        # 状态目录不可写/写入异常时丢弃但不静默：debug 留痕（日志审查 D6，不影响签到执行）；
        # 异常消息经 _sanitize_text 脱敏（sqlite/json 异常可能回显 cookie/csrf 值，C-SIGN-02）
        logger.debug("写入状态文件失败（%s）: %s", path, _sanitize_text(e))

# ---------------------------------------------------------------------------
# 全量收尾标记（调度器首签/补签闸门的事实源）
# ---------------------------------------------------------------------------
def _write_sched_done(counts=None):
    """写入当日「全量签到已运行」标记（sched-run-<date>.json）——调度器闸门事实源。

    调度器原 `_signed_today()` 以「任一账号 success」判定当日已签，
    手动签到/部分成功都会压制全站首签与补签。新契约：仅全量模式在收尾写本标记
    （--only 手动签到与 --probe 探针不写），调度器据此判定：
    - 首签：标记不存在 → 执行；
    - 补签：标记不存在，或存在未了结账号（failed/retrying/pending）→ 执行。
    """
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    path = os.path.join(state_dir, f"sched-run-{clock.now().strftime('%Y-%m-%d')}.json")
    try:
        os.makedirs(state_dir, exist_ok=True)
        payload = {
            "completed": True,
            "finished_at": clock.now().strftime("%H:%M:%S"),
        }
        if isinstance(counts, dict):
            payload.update(counts)
        tmp = path + ".tmp" + str(os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("写入全量完成标记失败（调度器可能重复触发当日签到）: %s", e)

# ---------------------------------------------------------------------------
# 补签轮判定（宿主 run.sh 与容器 docker/scheduler.py 共用的单一实现）
# ---------------------------------------------------------------------------
# 背景（2026-09-10 批次20 B3）：宿主原先靠**第二个独立 cron**（07:12）做补签，
# 但首签进程要 sleep 到最晚自选时间片（生产实测 07:25）才结束，flock 由脚本持有至
# 退出 → 07:12 的 cron 每天撞锁 `exit 0`，补签轮从未真正执行（生产日志 12/12 天实证）。
# 修法（用户裁决方案一）：宿主改为与容器同语义——**同一进程内**首轮结束后再判定
# 一次"是否需要补跑"，判定口径收敛到此处，两侧不再各写一份。
#
# 判定 = 「当日全量未收尾」或「当日存在未了结账号」：
#   - 全量未收尾（sched-run-<date>.json 缺失/completed=false）：首轮被 timeout 击杀、
#     崩溃或压根没跑起来 → 必须补跑；
#   - 存在未了结账号 = 状态文件里任一账号落 UNDONE_STATUSES。
# 无状态文件/文件损坏一律按"未了结"处理（宁多跑一轮，不漏签）。
# 「未了结」状态集合：定义在 yiban.status（唯一事实源），此处为同一对象的别名
# （docker/scheduler.py 亦别名引用它；测试断言三者同一身份）。
UNDONE_STATUSES = yiban_status.UNDONE_STATUSES


def _state_dir():
    return os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")


def full_run_done_today(state_dir=None, day=None):
    """当日全量签到是否已收尾（sched-run-<date>.json 的 completed 标记）。"""
    d = state_dir or _state_dir()
    today = day or clock.now().strftime("%Y-%m-%d")
    try:
        # utf-8-sig：容错 Windows 手工/工具写入的 BOM（与 _load_cred_state 同口径）
        with open(os.path.join(d, f"sched-run-{today}.json"), encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("completed"))


def has_undone_accounts_today(state_dir=None, day=None):
    """当日是否存在未了结账号；无记录/文件缺失/损坏按"未了结"处理（fail-safe 侧）。

    **多执行体形态优先看领取池**：那里是账号级了结的事实源（`done`=当日了结，
    `claimed`/`failed`=未了结），而状态文件只能说"这个账号最后写成什么状态"。
    当池里当日有记录时以池为准；没有记录（无库/池未启用/当日还没人领过）再回退到
    状态文件——两条口径都可用时，池更准。
    """
    today = day or clock.now().strftime("%Y-%m-%d")
    try:
        if db.is_initialized():
            stats = db.claim_stats(today)
            if stats.get("total"):
                return stats.get("open", 0) > 0
    except Exception as e:      # 池不可用 → 回退状态文件（不影响签到主流程）
        logger.debug("读取领取池失败（回退状态文件口径）: %s", e)
    d = state_dir or _state_dir()
    try:
        with open(os.path.join(d, f"sign-state-{today}.json"), encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return True
    if not isinstance(data, dict) or not data:
        return True
    return any(
        isinstance(v, dict) and str(v.get("status", "")).strip() in UNDONE_STATUSES
        for v in data.values()
    )


def need_second_run(state_dir=None, day=None):
    """是否需要补跑第二轮（宿主 run.sh 与容器调度器共用）。

    True = 当日全量未收尾，或存在未了结账号。调用方（run.sh）在**首轮结束之后、
    仍持锁期间**调用，因此不用担心与其它进程的竞态；容器侧在首轮子进程 wait()
    返回后调用，语义一致。
    """
    return (not full_run_done_today(state_dir, day)) or has_undone_accounts_today(state_dir, day)


def _cred_state_path():
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    return os.path.join(state_dir, "cred-state.json")


def _load_cred_state():
    """读账密状态文件（唯一入口见 yiban/cred_state.py）。"""
    return cred_state.read()


def _save_cred_state(data, touched=None):
    """保存账密状态。

    `touched` 给出本次实际处理的手机号集合时**按账号增量合并**（磁盘最新值为准，
    其余账号不受影响）；为 None 时整体覆盖（保留给测试/极端场景）。

    增量合并是必需的：全量轮从启动起就持有内存快照，若收尾整体覆盖，运行期间
    Web 端刚清除的暂停会被重新写回——该账号继续用错密码登录、加重风控。
    """
    try:
        if touched is None:
            def _replace(d):  # 整体覆盖（兼容入口：仅测试与极端场景使用）
                d.clear()
                d.update(data)
                return True

            cred_state.update(_replace)
        else:
            cred_state.merge(touched, data)
    except Exception as e:  # 锁/磁盘异常都只告警：状态文件不得影响签到主流程
        logger.debug("写入账密状态失败（%s）: %s", _cred_state_path(), _sanitize_text(e))

# ---------------------------------------------------------------------------
# 兜底执行体心跳（读侧：告警据此判断"后面还有没有人接着跑"）
# ---------------------------------------------------------------------------
def _fallback_alive_path():
    return os.path.join(_state_dir(), FALLBACK_ALIVE_FILE)


def _write_fallback_alive(at=None):
    """刷新兜底执行体心跳（每轮扫描写一次）。失败静默——心跳不该影响签到。"""
    path = _fallback_alive_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"at": (at or clock.now()).strftime(_TS_FMT),
                       "pid": os.getpid()}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _clear_fallback_alive():
    with contextlib.suppress(OSError):
        os.remove(_fallback_alive_path())


def fallback_alive(interval_sec=None, now=None):
    """兜底执行体是否在跑（按心跳新鲜度判定：超过 2 个扫描间隔即视为已停）。

    返回 `(alive: bool, age_sec: float | None)`。判定用**文件里的时间戳**而非文件是否存在：
    进程被 kill -9 时不会执行清理，只靠"文件还在"会永远报"在跑"。
    """
    path = _fallback_alive_path()
    try:
        with open(path, encoding="utf-8") as f:
            at = json.load(f).get("at", "")
        stamp = datetime.strptime(at, _TS_FMT)
    except (OSError, ValueError, TypeError, AttributeError):
        return False, None
    now = now or clock.now()
    age = (now - stamp).total_seconds()
    limit = 2 * (interval_sec or schedule._env_int("YIBAN_FALLBACK_INTERVAL", 60, 5, 3600))
    return age <= limit, age


# ---------------------------------------------------------------------------
# 并行执行体心跳：写侧（监督进程调用）与读侧（执行体接口调用）
# ---------------------------------------------------------------------------
def worker_alive_path(index):
    """并行执行体心跳文件路径（`<状态目录>/worker-alive-<槽位序号>.json`）。"""
    return os.path.join(_state_dir(), f"{WORKER_ALIVE_FILE_PREFIX}{int(index)}.json")


def mark_worker_started(index, now=None):
    """记录"该槽位本轮已启动"（覆盖写，旧记录连同收尾标记一起被替换）。

    由监督进程在拉起子进程时调用——子进程是短命进程，且写心跳需要槽位序号，
    而序号只有监督进程有。失败只留 debug 痕迹：心跳不该影响签到主流程。
    """
    at = now or clock.now()
    path = worker_alive_path(index)
    try:
        with cli_support._state_file_lock(path):
            _write_private_json(path, _alive_record(index, at))
    except OSError as e:
        logger.debug("写入执行体心跳失败（不影响签到）: %s", _sanitize_text(e))


def mark_worker_beat(index, now=None):
    """刷新该槽位心跳的 `ts`（保留 `started_at`）——存活期间的周期写盘。"""
    at = now or clock.now()
    path = worker_alive_path(index)
    try:
        with cli_support._state_file_lock(path):
            data = _read_worker_alive(index) or _alive_record(index, at)
            data["ts"] = at.strftime(_TS_FMT)
            _write_private_json(path, data)
    except OSError as e:
        logger.debug("刷新执行体心跳失败（不影响签到）: %s", _sanitize_text(e))


def mark_worker_finished(index, exit_code=None, now=None):
    """记录"该槽位本轮已正常退出"：在同一文件上补收尾标记（`ended_at`）。

    被信号杀掉（退出码为负）时调用方**不调本函数**：留"有开始、无收尾"，心跳过期后
    由 `worker_presence` 判成 `stale`——那正是"疑似被强杀/超时杀掉"需要用户注意的状态。
    """
    at = now or clock.now()
    path = worker_alive_path(index)
    try:
        with cli_support._state_file_lock(path):
            data = _read_worker_alive(index) or _alive_record(index, at)
            data["ended_at"] = at.strftime(_TS_FMT)
            data["ts"] = at.strftime(_TS_FMT)
            if exit_code is not None:
                data["exit_code"] = int(exit_code)
            _write_private_json(path, data)
    except OSError as e:
        logger.debug("写入执行体收尾标记失败（不影响签到）: %s", _sanitize_text(e))


def worker_presence(index, now=None):
    """该并行执行体槽位此刻的存活四态 → `(state, last_seen_at)`。

    | state | 判据 | 页面该做什么 |
    |-------|------|--------------|
    | `running` | 有心跳且新鲜（`now - ts <= 2 × WORKER_HEARTBEAT_SEC`） | 在线（正在跑本轮） |
    | `finished` | 有本轮收尾标记（`ended_at`，正常退出） | 已跑完（灰） |
    | `idle` | 本业务日无该槽位记录（今天还没跑） | 未运行（灰） |
    | `stale` | 有开始记录、无收尾，且心跳已过期 | **异常**（可能被强杀/超时杀掉） |

    `last_seen_at` 是"最后一次见到它活着"的时间串（收尾态给 `ended_at`；无记录给
    None）。**为什么不回 alive 布尔**：短命进程"没在跑"多数时候是正常的，只有
    `stale` 才需要用户注意；把四态判定放在后端一处，前端不必自己拼（也不用知道
    心跳周期这种部署细节）。本函数只回与身份串无关的槽位事实，不含 pid/主机名。
    """
    now = now or clock.now()
    data = _read_worker_alive(index)
    # 无记录，或记录属于**别的业务日**（昨天的"已跑完"不等于今天已跑）→ 今天还没跑
    if not data or str(data.get("day") or "") != now.strftime("%Y-%m-%d"):
        return WORKER_STATE_IDLE, None
    if data.get("ended_at"):
        stamp = _parse_stamp(data.get("ended_at"))
        return WORKER_STATE_FINISHED, stamp.strftime(_TS_FMT) if stamp else None
    seen = _parse_stamp(data.get("ts") or data.get("started_at"))
    if seen is None:
        # 有记录却读不出时间：新鲜度无从判定。宁可提示"异常"也不静默当在线
        # （页面把 stale 提示成"可能被杀"，比漏报一个卡住的槽位安全）。
        return WORKER_STATE_STALE, None
    last_seen = seen.strftime(_TS_FMT)
    if (now - seen).total_seconds() <= 2 * WORKER_HEARTBEAT_SEC:
        return WORKER_STATE_RUNNING, last_seen
    return WORKER_STATE_STALE, last_seen


def _read_worker_alive(index):
    """读该槽位的心跳（缺失/损坏/非 dict → `{}`，与其余状态文件同口径不抛）。"""
    try:
        # utf-8-sig 容错 Windows 记事本/工具写入的 BOM（与 cred_state 同口径）
        with open(worker_alive_path(index), encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_private_json(path, payload):
    """原子写 JSON、**创建即 0600**：心跳含本机部署节律，不该对同机其他用户可读。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def _alive_record(index, at):
    """心跳记录的公共字段（开始与收尾写同一组，收尾只多 `ended_at`）。"""
    stamp = at.strftime(_TS_FMT)
    return {"day": at.strftime("%Y-%m-%d"), "role": "worker", "index": int(index),
            "started_at": stamp, "ts": stamp}


def _parse_stamp(value):
    """状态文件里的时间串 → datetime；读不出返回 None。"""
    try:
        return datetime.strptime(str(value or ""), _TS_FMT)
    except (TypeError, ValueError):
        return None
