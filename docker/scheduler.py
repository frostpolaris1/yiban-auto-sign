#!/usr/bin/env python3
"""容器内签到调度：复刻宿主 cron 的语义。

- 06:31 首签 / 07:10 补签（闸门：当日全量标记 sched-run-<date>.json 不存在才跑
  首签；标记缺失或存在 failed/retrying/pending 未了结账号才跑补签——
  旧「任一账号 success 即跳过」会误吞全站首签与失败账号的兜底）
- 探针：每 10 分钟尝试一次入口（signin.py --probe 内部自判触发时间/频率/当日防重）
- 兜底常驻执行体：每 60 秒检查一次，**开关开着 + 在有效签到窗口内 + 今天没被周末门/
  一键暂停挡下**时拉起 `sign --fallback`，窗口结束由进程自己退出（判据全部复用引擎
  实现，见 `_fallback_should_run`；与宿主 cron 的 `scripts/yiban-fallback.sh` 同语义）
- 每日 03:00 清理 /data/logs 与 /data/state 下过期的按天日志/状态文件
  （策略唯一在 yiban/state_gc.py，与宿主 cron 共用一张表）

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
from datetime import datetime

# 包导入引导：本文件在仓库里是 `docker/scheduler.py`、在镜像里被复制为
# `scripts/container_scheduler.py`——两处都比仓库根低一层，但**同目录的兄弟模块**
# （signin/child_env/env_io）只在 scripts/ 一侧，而 `yiban/` 只在仓库根。故三个路径
# 都补上，两种位置都能直接跑（2026-09-15 容器冒烟实测：漏引导会让 sched 进程
# ModuleNotFoundError → supervisord 反复重启 → FATAL）。过渡机制，随 M1③ 清 sys.path 注入移除。
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
for _p in (_HERE, os.path.join(_REPO_ROOT, "scripts"), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# .env 解析与子进程环境构建与 run.sh / web 共用口径（共享模块）；
# signin：补签轮判定与未了结状态码的单一事实源（宿主 run.sh 的进程内补签轮
# 复用同一套函数，两侧不再各写一份判定）。
import signin  # noqa: E402
from child_env import build_child_env, parse_env_file  # noqa: E402

from yiban import clock, state_gc, window  # noqa: E402
from yiban.engine import schedule, workers  # noqa: E402

STATEDIR = os.environ.get("YIBAN_STATE_DIR", "/data/state")
LOGDIR = os.path.dirname(os.environ.get("YIBAN_LOG_FILE", "/data/logs/sign.log"))
ENV_FILE = os.environ.get("YIBAN_ENV_FILE", "/data/.env")


# 当日状态/全量标记的路径与判定统一在 signin（full_run_done_today /
# has_undone_accounts_today 接受 state_dir 参数），本模块不再各自拼路径。


# 视为"未了结"的状态码：补签闸门据此判断当日是否需要重跑
# （signin 侧有按账号防重与服务器 already 兜底，重跑幂等）
# skipped_window / skipped_norange 计入未了结——学校签到窗口晚于
# 本地配置（或 Range 延迟放出）时，06:31 首签可能全员落"窗口外跳过"；skip 类
# 状态若不算未了结，sched-run 又无条件写 completed=True，07:10 补签会被闸门
# 判为"全员了结"吞掉 → 全天零签到且无任何重试机会与告警。skip 既非"未了结"
# 也非"已完成"，宿主 run.sh 用退出码 2 写 SKIPPED（cron 会重跑）无此洞。
# no_position：易班侧无签到点位（登录成功但 Position 为空）为独立状态码，
# **计入**未了结——与宿主 run.sh 语义对齐：无点位账号在 signin.py main() 汇总
# 归 skip 且不触发失败告警，但退出码 2 → SKIPPED → 07:10 补签轮重跑兜底（学校
# 上午任务未配置=无点位，07:10 已配置=顺带补上）。重试 1 次即止
# （signin.NO_POSITION_MAX_ATTEMPTS=1，signin 内部 retry budget 不进入失败重试），
# 无点位账号被 07:10 整轮顺带重跑一次幂等无害，不会白跑太多。
# 未了结状态码集合：定义在 signin.UNDONE_STATUSES（单一事实源），此处仅别名引用。
# 含义见上方长注释——skip 类若不视为未了结，补签会被闸门吞掉造成全天零签到。
_UNDONE_STATUSES = signin.UNDONE_STATUSES


def _full_run_done_today():
    """当日全量签到是否已运行过（sched-run-<date>.json 标记）。

    原 `_signed_today()`「任一账号 success 即视为已签」会把
    用户手动签到、首签部分成功误判为全站已签——06:31 首签整体跳过（其余账号
    全天无人代签）、07:12 补签也被跳过（失败账号失去当日兜底）。
    新语义只认 signin 全量收尾写的标记；手动签到（--only）不写标记。

    2026-09-10（批次20 B3）：判定实现收敛到 signin.full_run_done_today()——
    宿主 run.sh 的进程内补签轮用同一套判定（signin.need_second_run），两侧共用
    单一事实源，避免"宿主改了容器没改"的语义漂移。
    """
    return signin.full_run_done_today(STATEDIR)


def _has_undone_today():
    """当日是否存在未了结账号（failed/retrying/pending/skipped_*/no_position；
    no_position 与宿主 run.sh SKIPPED 语义一致计入未了结，07:12 顺带重试一次，
    详见 signin.UNDONE_STATUSES）。

    标记存在但存在未了结账号 → 补签应重跑；无记录/文件缺失按「未跑过」
    处理（允许触发，避免漏签）。实现同 signin.has_undone_accounts_today()。
    """
    return signin.has_undone_accounts_today(STATEDIR)


def _slot_marker(kind):
    """当日该触发点已 spawn 过子进程的落盘标记（sched-slot-<kind>-<date>.json）。

    hm >= FIRST/SECOND 是无上界判定，而闩锁（done_sign_*）只存进程内存：
    容器在 08:00/15:00 重启时闩锁归零，补签闸门（存在未了结账号）恒真 →
    追加一轮必然 skipped_window 的全站负载并覆盖当日已 success 的状态。
    落盘标记使「同一时段二次触发不重跑」跨重启成立；标记不可写时退化为
    既有闩锁语义。按日命名，跨日自动失效。"""
    return os.path.join(STATEDIR, f"sched-slot-{kind}-{clock.now():%Y-%m-%d}.json")


def _slot_done(kind):
    try:
        with open(_slot_marker(kind), encoding="utf-8") as fh:
            json.load(fh)
        return True
    except (OSError, ValueError):
        return False


def _mark_slot(kind):
    """落盘「该时段已 spawn 过子进程」标记（tmp + os.replace 原子写）。

    原实现直接 `open(path, "w")`：容器在写入中途被杀会留下半截 JSON，
    `_slot_done` 的 json.load 恒失败 → 判定为「本时段没跑过」，
    hm >= FIRST/SECOND 的无上界判定于是再触发一轮全站登录（幂等但多一轮真实请求，
    且覆盖当日已 success 的状态文件）。与 signin.py 的状态文件写入同口径。
    """
    path = _slot_marker(kind)
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        os.makedirs(STATEDIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"triggered_at": clock.now().strftime("%H:%M:%S")}, fh)
        os.replace(tmp, path)
    except OSError:
        # 写失败与读失败同向（退化为既有闩锁语义：读不到即允许触发），
        # 仅清掉自己的半成品，不告警
        with contextlib.suppress(OSError):
            os.remove(tmp)


def _cleanup_state():
    """按天状态文件的过期清理（策略唯一在 `yiban/state_gc.py`，与宿主 cron 同一份）。

    原实现只清按天日志（`sign-*.log`）而不管状态目录：容器形态下
    `/data/state` 里的 `sched-run-*` / `sched-slot-*` / `sign-daily-*` /
    `mail-user-fail-*` 从不清理，条目随天数线性膨胀。宿主侧同一批文件过去也漏清，
    两侧现在共用一张表；保留期取同一组环境变量（默认日志/状态 365 天、快照 7 天）。
    清理失败仅记日志：它是后台维护动作，不该影响调度循环。
    """
    try:
        removed, _detail = state_gc.sweep(STATEDIR, LOGDIR)
        if removed:
            print(f"[scheduler] 已清理 {removed} 个过期状态/日志文件", flush=True)
    except (OSError, ValueError) as e:
        print(f"[scheduler] 状态清理失败（不影响调度）: {e}", flush=True)


# 首签 / 补签 时间点（分钟级），用「已进入该分钟且当天未执行过」的闩锁语义，
# 见 main_loop。
# 探针为「周期尝试」而非固定时刻（2026-08-31 修复）：原 PROBE_AT=(23,55) 与
# 设置页 YIBAN_PROBE_TIME 两套时钟脱钩——容器内改探针时间不生效（同宿主 cron
# 写死 23:55 的问题）。现每 PROBE_TRY_SECONDS 尝试一次 --probe（探针未开启不
# spawn），signin.py 内部按 PROBE_TIME / 频率 / 当日防重裁决是否真正探测，
# 与宿主 cron */10 轮询语义对齐。
FIRST, SECOND = (6, 31), (7, 10)
PROBE_TRY_SECONDS = 600

# 兜底常驻执行体的检查周期（秒）：窗口开始时最多晚这么久拉起，窗口结束后最多晚
# 这么久停止尝试（进程本身按引擎自己的判定退出，见 _tick_fallback）。取 60 与
# 兜底引擎的扫描间隔（YIBAN_FALLBACK_INTERVAL 默认 60）同一量级。
FALLBACK_TRY_SECONDS = 60


def _child_timeout(env):
    """子进程超时：默认按签到窗口动态计算，与宿主 run.sh 同口径。

    原固定 7200s 与可配置窗口脱钩：窗口整体晚于触发点约 2 小时（如 10:00~11:00）
    时，首签/补签子进程在 sleep 等窗口途中即被杀，全天漏签。现默认 = 当日窗口
    结束（YIBAN_SIGN_END，与 signin.py _schedule_config / run.sh 同一事实源，
    默认 07:50）− 当前时刻 + 5 分钟余量，下限 600s；YIBAN_RUN_TIMEOUT_SEC 显式
    设置时优先（管理员手动覆盖）。键来源口径与 build_child_env 一致：
    .env（env，Web 设置页写入）优先于进程环境（compose 注入）——否则
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
    # 格式校验与 run.sh / yiban.window.parse_hhmm 一致（接受 7:50 与 07:50）；非法回退
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", end_hhmm):
        end_hhmm = "07:50"
    try:
        end_dt = datetime.strptime(f"{clock.now():%Y-%m-%d} {end_hhmm}", "%Y-%m-%d %H:%M")
    except ValueError:
        end_dt = datetime.strptime(f"{clock.now():%Y-%m-%d} 07:50", "%Y-%m-%d %H:%M")
    return max(600, int((end_dt - clock.now()).total_seconds()) + 300)


def _run_signin_child(extra=None, env=None):
    """运行签到/探针子进程（补超时——宿主 run.sh 有动态超时，
    容器内原实现无 timeout，单个子进程挂起即永久卡死全部调度且无告警）。

    env 由调用方传入时复用（探针周期尝试已为短路判断解析过一次），
    避免同一触发点重复读盘。
    超时先 SIGTERM 再 SIGKILL：signin 的 SIGTERM 处理器会把已收集的告警
    汇总在进程死亡前发出（原 subprocess.run 超时直接 SIGKILL，整轮汇总丢失）。
    """
    env = env if env is not None else build_child_env(ENV_FILE)
    # 覆盖注入「当天最后一轮」时刻 = 本进程实际用的补签触发点（SECOND）。
    # 容器形态的补签由本进程按 SECOND 触发，不读 YIBAN_SECOND_RUN_TIME；而
    # signin 的告警抑制要靠该键判断"是否还有下一轮兜底"。不注入时 signin 会按
    # 宿主默认值（07:12）判，与容器的 07:10 错位 → 07:10~07:12 之间的真异常
    # 被当成"还有兜底"而静默漏报。故无条件以实际值覆盖（与 _child_timeout 的
    # ".env 优先"口径不同：这里注入的是本进程的事实，不是可配置项）。
    env["YIBAN_SECOND_RUN_TIME"] = f"{SECOND[0]:02d}:{SECOND[1]:02d}"
    timeout = _child_timeout(env)
    cmd = ["python3", "scripts/signin.py"] + (extra or [])
    proc = subprocess.Popen(cmd, cwd="/app", env=env)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        print(f"[scheduler] 签到子进程超时（>{timeout}s）被终止，已留痕继续调度", flush=True)


# ---------------------------------------------------------------------------
# 兜底常驻执行体（容器形态）：随签到窗口起停
# ---------------------------------------------------------------------------
# 开关 `YIBAN_FALLBACK_ENABLE` 与宿主**同一个键、同一套真值字面量**（网页设置页写入
# `.env`）；拉起时机、周末门/暂停门、窗口两段判定**全部复用引擎既有实现**，容器侧
# 不另写一套（判据见 _fallback_should_run）。拉起的就是宿主那条入口
# `sign --fallback`，故独立锁 `signin-run.lock.fallback` 与宿主形状完全一致。
#
#: 容器内托管的兜底常驻子进程（None = 当前没有）。模块级单例：调度进程只有一个，
#: 用它保证"窗口内重复检查不会叠进程"（句柄非空即视为已有）。
_fallback_proc = None


def _fallback_gate_env():
    """兜底开关/门/窗口的判据环境——**只读 `.env`**。

    开关与签到窗口由网页写进 `.env`（`YIBAN_FALLBACK_ENABLE` / `YIBAN_SIGN_START`
    等），而 compose 注入的是路径类变量。两者都从 `.env` 取，才不会出现"页面显示
    关闭、调度器按进程环境里的另一个值起进程"；宿主 `scripts/yiban-fallback.sh`
    同样以 `.env` 为开关事实源。
    """
    return parse_env_file(ENV_FILE)


def _fallback_should_run(env, now=None):
    """此刻是否该让兜底常驻在跑 → bool（容器形态的拉起判据，四道关全复用既有实现）。

    - **开关**：`YIBAN_FALLBACK_ENABLE`，真值字面量走 `schedule._env_flag`（与
      run.sh / 宿主兜底脚本同一套 1/true/on/yes）；未开 → 不起（静默，不刷日志）。
    - **周末门 / 一键暂停门**：`schedule.day_off(now, env=env)` 非空 → 不起
      （与定时轮、兜底引擎同一实现，改一处两边同时变）。
    - **窗口两段判定**：`window.from_env(env)` 的 `is_open` 且未 `is_closed` →
      **只在有效签到窗口内**拉起。窗口还没开不提前拉起（容器调度器是常驻进程，
      不必像宿主 cron 那样提前挂上等待）；窗口已关不重复拉起，已起的进程由引擎
      自己退出。
    """
    if not schedule._env_flag("YIBAN_FALLBACK_ENABLE", env):
        return False
    now = now or clock.now()
    if schedule.day_off(now, env=env):
        return False
    bounds = window.from_env(env)
    return bounds.is_open(now) and not bounds.is_closed(now)


def _start_fallback_child():
    """拉起兜底常驻子进程：与宿主**同一入口**（`sign --fallback`）、同一把独立锁。

    入口唯一在 `yiban.engine.workers.run_fallback_worker`（它把锁名落成
    `signin-run.lock.fallback`，与宿主 cron 完全一致，故容器内的兜底不占用也不影响
    宿主/定时轮的那把全局锁）。这里显式把锁名写进子进程环境，使"独立锁"成为容器
    路径的显式契约，而不依赖别人的默认值。

    输出走子进程自己的按天日志 handler（`/data/logs/sign-<业务日>.log`，与签到同源），
    不额外重定向——异常栈留在 sched 日志里便于排查。
    """
    env = build_child_env(ENV_FILE)
    env.setdefault("YIBAN_RUN_LOCK_NAME", workers.FALLBACK_LOCK_NAME)
    return subprocess.Popen(
        ["python3", "scripts/signin.py", "--fallback"], cwd="/app", env=env
    )


def _tick_fallback(now=None, env=None):
    """周期检查：该有兜底进程就拉起一个，已自行退出就回收（**同时最多一个**）。

    返回 True = 本次检查新拉起了一个进程（供日志与测试断言）。窗口内重复检查不会
    叠进程：句柄非空且仍在跑即视为已有。窗口结束/门命中/引擎异常后子进程**自行
    退出**，下一次检查回收句柄，并只在"当前仍该跑"时才重新拉起——容器侧不做强杀
    （引擎每一轮都会重判门与窗口，这正是它自己退出的原因）。
    """
    global _fallback_proc
    if _fallback_proc is not None and _fallback_proc.poll() is not None:
        _fallback_proc = None
    if _fallback_proc is not None:
        return False
    if not _fallback_should_run(_fallback_gate_env() if env is None else env, now):
        return False
    try:
        _fallback_proc = _start_fallback_child()
    except OSError as e:
        print(f"[scheduler] 拉起兜底常驻执行体失败: {e}", flush=True)
        return False
    print("[scheduler] 已在签到窗口内拉起兜底常驻执行体（窗口结束自行退出）", flush=True)
    return True


def main_loop(sleep_seconds=1):
    """调度主循环。闩锁按 (任务, 当日) 记账，并落盘到 sched-slot 标记——
    进程重启后当日已触发过的时段不再二次触发（闩锁内存态重启即丢），
    未完成账号仍由另一时段的闸门（全量标记缺失/存在未了结）兜底。

    SECOND 不受 FIRST 影响：若首签子进程一直占用到越过 07:10，循环恢复后
    hm>=SECOND 仍会补一次（signin 内状态文件已防重），不再全天丢失补签。
    """
    done_sign_first = None   # date | None
    done_sign_second = None
    last_probe_try = None    # datetime | None：上次尝试探针的时刻（周期尝试）
    last_fallback_try = None  # datetime | None：上次检查兜底常驻的时刻（周期检查）
    last_clean = None
    while True:
        now = clock.now()
        today = now.date()
        hm = (now.hour, now.minute)
        # 每次触发前重新解析 .env（2026-08-28 审查 F2）：
        # 子进程环境原先只在启动时构造一次，管理员在 Web 后台改的 YIBAN_GLOBAL_PAUSE
        # （一键暂停）/ YIBAN_SUNDAY_SIGN / YIBAN_SATURDAY_SIGN / YIBAN_PROBE_* 在容器
        # 重启前静默不生效。
        # 解析成本仅在真正触发的那一刻产生（每天 3 次），轮询循环内不读盘。
        if (hm >= FIRST and done_sign_first != today
                and not _full_run_done_today() and not _slot_done("first")):
            # 与 run.sh 唯一实质差异：容器内无需 flock/宿主绝对路径，状态文件已防重
            _run_signin_child()
            _mark_slot("first")
            done_sign_first = today
        if (hm >= SECOND and done_sign_second != today
                and (not _full_run_done_today() or _has_undone_today())
                and not _slot_done("second")):
            # 补签闸门：全量未跑过（首签错过的补偿）或存在未了结
            # 账号（failed/retrying/pending/skipped_window/skipped_norange/no_position）
            # 才执行；全员了结则跳过，不再被「任一账号
            # 成功」误导跳过失败账号的兜底。no_position（无点位）视为未了结
            # 与宿主 run.sh 退出码 2 → SKIPPED → 07:10 重跑一致，
            # 07:10 补签轮顺带重试一次（signin 内部 1 次即止，幂等无害）。
            # 补签轮注入 YIBAN_SECOND_RUN=1——与宿主 run.sh 补签轮
            # 导出的同一信号，signin.py 据此判定 is_second_run（环境变量优先，
            # sched-run 标记兜底），修复首签被 timeout 击杀时「部分成功+窗口外」
            # 零告警（B12-2 分支复发）
            env = build_child_env(ENV_FILE)
            env["YIBAN_SECOND_RUN"] = "1"
            _run_signin_child(env=env)
            _mark_slot("second")
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
        if (last_fallback_try is None
                or (now - last_fallback_try).total_seconds() >= FALLBACK_TRY_SECONDS):
            # 兜底常驻：窗口内拉起、窗口结束由进程自行退出。判据只看 `.env`
            # （网页写入的开关与窗口设置），不构造完整子进程环境——未开时不产生
            # 任何副作用（不 spawn、不打日志）。
            last_fallback_try = now
            _tick_fallback(now)
        if now.hour >= 3 and last_clean != today:
            _cleanup_state()
            last_clean = today
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main_loop()
