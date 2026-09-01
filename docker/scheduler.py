#!/usr/bin/env python3
"""容器内签到调度：复刻宿主 cron 的语义。

- 06:31 首签 / 07:10 补签（闸门：当日全量标记 sched-run-<date>.json 不存在才跑
  首签；标记缺失或存在 failed/retrying/pending 未了结账号才跑补签——批次7 P1-1，
  旧「任一账号 success 即跳过」会误吞全站首签与失败账号的兜底）
- 探针：每 10 分钟尝试一次入口（signin.py --probe 内部自判触发时间/频率/当日防重）
- 每日 03:00 清理 /data/logs 下 365 天前的按天日志

数据/配置路径由 compose 注入的 YIBAN_* 环境变量决定；同时把 YIBAN_ENV_FILE
指向的 .env（Web 设置页写入）解析后注入子进程环境——否则 Web 后台改的
探针/周日/暂停等开关对 signin 子进程不可见（2026-08-27 对抗性审查 P1-1：
原实现只继承 compose 环境变量，Docker 部署下这些设置静默失效）。

tick 采用「分钟级到点闩锁」而非「秒==0 命中」：调度循环被签到子进程阻塞、
错过整分第 0 秒时，进入目标分钟后仍会补触发一次（同日去重防重复），
不再整天丢失（P2-10）。
"""
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

# .env 解析与子进程环境构造与 run.sh / web 共用口径（批次7 P2-10 提为共享模块）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from child_env import build_child_env

STATEDIR = os.environ.get("YIBAN_STATE_DIR", "/data/state")
LOGDIR = os.path.dirname(os.environ.get("YIBAN_LOG_FILE", "/data/logs/sign.log"))
ENV_FILE = os.environ.get("YIBAN_ENV_FILE", "/data/.env")


def _state_file():
    """容器内签到脚本写的当日结构化状态（signin.py:1801）。"""
    return os.path.join(STATEDIR, f"sign-state-{datetime.now():%Y-%m-%d}.json")


def _sched_run_file():
    """当日全量签到完成标记（signin.py 全量收尾写，P1-1 闸门事实源）。"""
    return os.path.join(STATEDIR, f"sched-run-{datetime.now():%Y-%m-%d}.json")


# 视为"未了结"的状态码：补签闸门据此判断当日是否需要重跑
# （signin 侧有按账号防重与服务器 already 兜底，重跑幂等）
# 批次12 B12-2：skipped_window / skipped_norange 计入未了结——学校签到窗口晚于
# 本地配置（或 Range 延迟放出）时，06:31 首签可能全员落"窗口外跳过"；skip 类
# 状态若不算未了结，sched-run 又无条件写 completed=True，07:10 补签会被闸门
# 判为"全员了结"吞掉 → 全天零签到且无任何重试机会与告警。skip 既非"未了结"
# 也非"已完成"，宿主 run.sh 用退出码 2 写 SKIPPED（cron 会重跑）无此洞。
# 批次16 no_position：易班侧无签到点位（登录成功但 Position 为空）为独立状态码，
# **计入**未了结——与宿主 run.sh 语义对齐：无点位账号在 signin.py main() 汇总
# 归 skip 且不触发失败告警，但退出码 2 → SKIPPED → 07:10 补签轮重跑兜底（学校
# 上午任务未配置=无点位，07:10 已配置=顺带补上）。重试 1 次即止
# （signin.NO_POSITION_MAX_ATTEMPTS=1，signin 内部 retry budget 不进入失败重试），
# 无点位账号被 07:10 整轮顺带重跑一次幂等无害，不会白跑太多。
_UNDONE_STATUSES = frozenset((
    "failed", "retrying", "pending",
    "skipped_window", "skipped_norange",
    "no_position",
))


def _full_run_done_today():
    """当日全量签到是否已运行过（sched-run-<date>.json 标记）。

    批次7 P1-1：原 `_signed_today()`「任一账号 success 即视为已签」会把
    用户手动签到、首签部分成功误判为全站已签——06:31 首签整体跳过（其余账号
    全天无人代签）、07:10 补签也被跳过（失败账号失去当日兜底）。
    新语义只认 signin 全量收尾写的标记；手动签到（--only）不写标记。
    """
    try:
        with open(_sched_run_file(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("completed"))


def _has_undone_today():
    """当日是否存在未了结账号（failed/retrying/pending/skipped_*/no_position；
    no_position 与宿主 run.sh SKIPPED 语义一致计入未了结，07:10 顺带重试一次，
    详见 _UNDONE_STATUSES 说明）。

    标记存在但存在未了结账号 → 07:10 补签应重跑；无记录/文件缺失按「未跑过」
    处理（允许触发，避免漏签）。"""
    try:
        with open(_state_file(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return True
    if not isinstance(data, dict) or not data:
        return True
    return any(
        isinstance(v, dict) and str(v.get("status", "")).strip() in _UNDONE_STATUSES
        for v in data.values()
    )


def _cleanup_logs():
    """删除 365 天前的按天日志（sign-YYYY-MM-DD.log），对齐 scripts/yiban-cleanup.sh。"""
    cutoff = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    if not os.path.isdir(LOGDIR):
        return
    for name in os.listdir(LOGDIR):
        if name.startswith("sign-") and name.endswith(".log"):
            key = name[len("sign-"):-len(".log")]
            if key < cutoff:
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(LOGDIR, name))


# 首签 / 补签 时间点（分钟级），用「已进入该分钟且当天未执行过」的闩锁语义，
# 见 main_loop。
# 探针为「周期尝试」而非固定时刻（2026-08-31 修复）：原 PROBE_AT=(23,55) 与
# 设置页 YIBAN_PROBE_TIME 两套时钟脱钩——容器内改探针时间不生效（同宿主 cron
# 写死 23:55 的问题）。现每 PROBE_TRY_SECONDS 尝试一次 --probe（探针未开启不
# spawn），signin.py 内部按 PROBE_TIME / 频率 / 当日防重裁决是否真正探测，
# 与宿主 cron */10 轮询语义对齐。
FIRST, SECOND = (6, 31), (7, 10)
PROBE_TRY_SECONDS = 600


def _child_timeout(env):
    """子进程超时：默认按签到窗口动态计算，与宿主 run.sh 同口径（批次12 B12-3）。

    原固定 7200s 与可配置窗口脱钩：窗口整体晚于触发点约 2 小时（如 10:00~11:00）
    时，首签/补签子进程在 sleep 等窗口途中即被杀，全天漏签。现默认 = 当日窗口
    结束（YIBAN_SIGN_END，与 signin.py _schedule_config / run.sh 同一事实源，
    默认 07:50）− 当前时刻 + 5 分钟余量，下限 600s；YIBAN_RUN_TIMEOUT_SEC 显式
    设置时优先（管理员手动覆盖）。键来源口径与 build_child_env 一致（批次16
    P2-5）：.env（env，Web 设置页写入）优先于进程环境（compose 注入）——否则
    compose 显式设置会让设置页修改静默失效。
    """
    raw = str(env.get("YIBAN_RUN_TIMEOUT_SEC")
              or os.environ.get("YIBAN_RUN_TIMEOUT_SEC", "")).strip()
    if raw:
        try:
            return max(600, int(raw))
        except (TypeError, ValueError):
            pass
    end_hhmm = str(env.get("YIBAN_SIGN_END", "07:50")).strip()
    # 格式校验与 run.sh / signin.py _parse_hhmm 一致（接受 7:50 与 07:50）；非法回退
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", end_hhmm):
        end_hhmm = "07:50"
    try:
        end_dt = datetime.strptime(f"{datetime.now():%Y-%m-%d} {end_hhmm}", "%Y-%m-%d %H:%M")
    except ValueError:
        end_dt = datetime.strptime(f"{datetime.now():%Y-%m-%d} 07:50", "%Y-%m-%d %H:%M")
    return max(600, int((end_dt - datetime.now()).total_seconds()) + 300)


def _run_signin_child(extra=None, env=None):
    """运行签到/探针子进程（批次7 P3-14：补超时——宿主 run.sh 有动态超时，
    容器内原实现无 timeout，单个子进程挂起即永久卡死全部调度且无告警）。

    env 由调用方传入时复用（探针周期尝试已为短路判断解析过一次），
    避免同一触发点重复读盘。
    """
    env = env if env is not None else build_child_env(ENV_FILE)
    timeout = _child_timeout(env)
    cmd = ["python3", "scripts/signin.py"] + (extra or [])
    try:
        subprocess.run(cmd, cwd="/app", env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[scheduler] 签到子进程超时（>{timeout}s）被终止，已留痕继续调度", flush=True)


def main_loop(sleep_seconds=1):
    """调度主循环。闩锁按 (任务, 当日) 记账；进程重启视为新一天可再执行。

    SECOND 不受 FIRST 影响：若首签子进程一直占用到越过 07:10，循环恢复后
    hm>=SECOND 仍会补一次（signin 内状态文件已防重），不再全天丢失补签。
    """
    done_sign_first = None   # date | None
    done_sign_second = None
    last_probe_try = None    # datetime | None：上次尝试探针的时刻（周期尝试）
    last_clean = None
    while True:
        now = datetime.now()
        today = now.date()
        hm = (now.hour, now.minute)
        # 每次触发前重新解析 .env（2026-08-28 审查 F2）：
        # 子进程环境原先只在启动时构造一次，管理员在 Web 后台改的 YIBAN_GLOBAL_PAUSE
        # （一键暂停）/ YIBAN_SUNDAY_SIGN / YIBAN_SATURDAY_SIGN / YIBAN_PROBE_* 在容器
        # 重启前静默不生效。
        # 解析成本仅在真正触发的那一刻产生（每天 3 次），轮询循环内不读盘。
        if hm >= FIRST and done_sign_first != today and not _full_run_done_today():
            # 与 run.sh 唯一实质差异：容器内无需 flock/宿主绝对路径，状态文件已防重
            _run_signin_child()
            done_sign_first = today
        if hm >= SECOND and done_sign_second != today and (
            not _full_run_done_today() or _has_undone_today()
        ):
            # 补签闸门（批次7 P1-1）：全量未跑过（首签错过的补偿）或存在未了结
            # 账号（failed/retrying/pending/skipped_window/skipped_norange/no_position，
            # 批次12 B12-2 / 批次16）才执行；全员了结则跳过，不再被「任一账号
            # 成功」误导跳过失败账号的兜底。no_position（无点位）视为未了结
            # （批次16）：与宿主 run.sh 退出码 2 → SKIPPED → 07:10 重跑一致，
            # 07:10 补签轮顺带重试一次（signin 内部 1 次即止，幂等无害）。
            # 批次16 P2-4：补签轮注入 YIBAN_SECOND_RUN=1——与宿主 run.sh 补签轮
            # 导出的同一信号，signin.py 据此判定 is_second_run（环境变量优先，
            # sched-run 标记兜底），修复首签被 timeout 击杀时「部分成功+窗口外」
            # 零告警（B12-2 分支复发）
            env = build_child_env(ENV_FILE)
            env["YIBAN_SECOND_RUN"] = "1"
            _run_signin_child(env=env)
            done_sign_second = today
        if last_probe_try is None or (now - last_probe_try).total_seconds() >= PROBE_TRY_SECONDS:
            # 探针周期尝试：未开启不 spawn（避免无谓子进程）；开启则交由
            # signin.py 内部 _health_probe_due（PROBE_TIME / 频率 / 当日防重）
            # 裁决，未到触发点零请求退出。每次尝试重新解析 .env（F2），
            # Web 后台改探针开关 / 时间即时生效，无需重启容器。
            last_probe_try = now
            env = build_child_env(ENV_FILE)
            if str(env.get("YIBAN_PROBE_ENABLE", "0")).strip().lower() in ("1", "true", "on", "yes"):
                _run_signin_child(extra=["--probe"], env=env)
        if now.hour >= 3 and last_clean != today:
            _cleanup_logs()
            last_clean = today
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main_loop()
