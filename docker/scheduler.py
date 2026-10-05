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
- 每日 02:00 定时备份（2026-10-01 · M44）：容器形态过去**没有任何备份**——宿主形态有
  cron 那条 02:00 行，而容器部署的用户根本没有宿主 cron，容器调度器也漏了这个挂点，
  等于"看着在跑、其实从没备份过"。这里补上：复用 `docker/backup-docker.sh`（上一批刚
  做过加固：umask 077 / 口令走 _FILE / RETAIN_DAYS 校验 / 解包护栏），备份落 compose
  声明的独立卷（不在 /data 内，避免下一轮 tar 把上一轮备份再打进去）。

数据/配置路径按 `env_io.resolve_path` 解析（进程环境 → `.env` → 容器默认值）：
compose 注入的 `YIBAN_*` 仍然优先，只写进 `.env` 的部署也不再被静默忽略。
同时把 `YIBAN_ENV_FILE` 指向的 .env（Web 设置页写入）解析后注入子进程环境——否则
Web 后台改的探针/周日/暂停等开关对 signin 子进程不可见（2026-08-27 对抗性审查 P1-1：
原实现只继承 compose 环境变量，Docker 部署下这些设置静默失效）。

tick 采用「分钟级到点闩锁」而非「秒==0 命中」：调度循环被签到子进程阻塞、
错过整分第 0 秒时，进入目标分钟后仍会补触发一次（同日去重防重复），
不再整天丢失（P2-10）。
"""
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# 包导入引导（探活快路也要用它，故排在业务导入图之前）
# ---------------------------------------------------------------------------
# 本文件在仓库里是 `docker/scheduler.py`、在镜像里被复制为
# `scripts/container_scheduler.py`——两处都比仓库根低一层，但**同目录的兄弟模块**
# （signin/child_env/env_io）只在 scripts/ 一侧，而 `yiban/` 只在仓库根。故三个路径
# 都补上，两种位置都能直接跑（2026-09-15 容器冒烟实测：漏引导会让 sched 进程
# ModuleNotFoundError → supervisord 反复重启 → FATAL）。过渡机制，随 M1③ 清 sys.path 注入移除。
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
for _p in (_HERE, os.path.join(_REPO_ROOT, "scripts"), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 路径键的唯一解析口径在这里。快路只多导这一个模块。`yiban.infra` 不导入业务模块。
# signin 等重导入链仍在 `--check-health` 之后。这笔开销必须付：状态目录可以只写在
# `.env` 里。写心跳的一方与探活读的一方要算出同一个目录。读方若只认进程环境，它会去
# 别的目录找心跳。健康检查于是恒判不健康，容器被反复重启。
from yiban.infra import env_io  # noqa: E402

# ---------------------------------------------------------------------------
# 活体心跳与探活快路（必须位于业务导入图之前）
# ---------------------------------------------------------------------------
# compose 的 healthcheck 原本只 curl web 端口——sched 崩溃/躺平（supervisord
# FATAL）时容器依旧"健康"，`restart: unless-stopped` 永不救。现在调度进程用
# 守护线程周期性落一个心跳文件（首签/补签子进程 wait 期间主循环合法阻塞数分钟
# ~数小时，tick-only 心跳会在正常工作状态下"陈旧"误报；线程随进程死——进程
# 没了心跳断流，正是探活要抓的形态），探活只读一个事实：心跳 mtime。
# `--check-health` 在 import signin 与 yiban 业务模块之前退出：30s 一次的探测不该拖着
# 整条业务导入链（耗时）。它躲不开第三方依赖：快路为解析 STATEDIR 导 `yiban.infra.env_io`，
# 连带 `env_lock → yiban.infra.locks → portalocker`。"观测件的判据必须比被观测对象更
# 简单"从此只在业务模块图上成立，依赖图上快路与 sched 本体共用 portalocker。
# 这笔导入必须付：状态目录可以只写在 `.env` 里。写心跳的一方与探活读的一方必须算出同
# 一个目录——读方只认进程环境时它会去缺省目录找心跳，健康检查恒判不健康，容器被反复重启。
# 残余暴露（2026-10-05 容器演练实测；风险没消除，这里只写什么条件会亮）：
# - 摘掉 portalocker → `--check-health` rc=1（同机同目录，改动前的版本 rc=0）。只装载
#   调度器模块同样 IMPORT_FAILED，新起的 sched 进程起不来；已在跑的 sched 因包已载入
#   继续落心跳，所以"sched 健康 + 探活红"只存在于运行中被摘包的那一段，不是稳态。
# - 读方读不到 `.env` → resolve_path 回落缺省目录 → 假不健康。两半同时成立才亮：部署把
#   `YIBAN_STATE_DIR` 只写进 `.env`，且探活降权。现形态两半都不成立——healthcheck 以 root
#   跑（镜像无 USER 指令，supervisord 以 root 起），compose 又把 `YIBAN_STATE_DIR` 注入
#   进程环境让读方停在解析第一层。实测：无特权 uid + 键在进程环境 rc=0；无特权 uid +
#   键只在 `.env` 时读方算出 /data/state、rc=1。给镜像加 USER 或给 healthcheck 降权前
#   先读本段。
# 容器内整链生效待生产演练。
STATEDIR = env_io.resolve_path("YIBAN_STATE_DIR", "/data/state")
HEARTBEAT_FILE = "sched-heartbeat.json"
HEARTBEAT_INTERVAL = 10          # 守护线程落盘周期（秒）
HEARTBEAT_MAX_AGE_SECONDS = 60   # 容忍窗 = 6 个落盘周期；陈旧/缺失一律不健康


def _heartbeat_is_fresh(path=None):
    """心跳 mtime 在容忍窗内 ⇒ True；文件缺失/不可读 ⇒ False（fail-closed）。"""
    p = path or os.path.join(STATEDIR, HEARTBEAT_FILE)
    try:
        return (time.time() - os.path.getmtime(p)) < HEARTBEAT_MAX_AGE_SECONDS
    except OSError:
        return False


if __name__ == "__main__" and "--check-health" in sys.argv[1:]:
    sys.exit(0 if _heartbeat_is_fresh() else 1)

# ---------------------------------------------------------------------------

# .env 解析与子进程环境构建与 run.sh / web 共用口径（共享模块）；
# signin：补签轮判定与未了结状态码的单一事实源（宿主 run.sh 的进程内补签轮
# 复用同一套函数，两侧不再各写一份判定）。
import signin  # noqa: E402
from child_env import build_child_env, parse_env_file  # noqa: E402

from yiban import clock, state_gc, window  # noqa: E402
from yiban.engine import schedule, workers  # noqa: E402
from yiban.infra import private_json  # noqa: E402  （状态文件私有写单通道）
from yiban.logging_ext import MaskingFormatter  # noqa: E402

LOGDIR = os.path.dirname(os.environ.get("YIBAN_LOG_FILE", "/data/logs/sign.log"))
# 与 web / 引擎同一读法：`.env` 里的指针也要认得（基线见 env_io.env_path——进程环境
# 未给指针时按 cwd 的 .env 定位，容器里 cwd 是 /app）。
ENV_FILE = env_io.resolve_path("YIBAN_ENV_FILE", "/data/.env")

logger = logging.getLogger("scheduler")

# 日志装配幂等标记（见 _setup_logging）
_logging_ready = False


def _setup_logging():
    """容器调度进程日志装配：stdout 处理器挂输出面脱敏 formatter，只在常驻入口调用。

    本进程会转调引擎模块（signin/window/schedule/state_gc 的读盘与判定路径都可能经
    logging 出声），而 sched 常驻此前没有任何日志装配：root 无 handler 时 WARNING+
    由 lastResort 裸写 stderr——CLI 与 web 入口都挂的出站手机号兜底在这里缺席。
    补挂同一个 `MaskingFormatter`（对最终成文幂等遮 11 位号）后，容器入口的日志面
    与 CLI/web 同口径；进程自身的留痕也统一走 logging 而非裸 print，新写的日志天然
    在防线内。

    装配放 `__main__` 而不在模块导入期：测试按文件路径装载本模块复用内部函数，
    导入期挂 root handler 会污染同进程的全部用例。级别口径对齐 web 入口——root 保持
    WARNING（第三方库 INFO 不进常驻 sched.log），自有组件单独放开 INFO。
    """
    global _logging_ready
    if _logging_ready:
        return
    _logging_ready = True
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(MaskingFormatter(
        "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    for _name in ("yiban", "signin", "scheduler"):
        logging.getLogger(_name).setLevel(logging.INFO)
# （STATEDIR 与心跳常量在文件头的探活快路区，探活不得依赖本行之后的导入图）


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
    """落盘「该时段已 spawn 过子进程」标记（状态文件私有写单通道，tmp + os.replace）。

    原实现直接 `open(path, "w")`：容器在写入中途被杀会留下半截 JSON，
    `_slot_done` 的 json.load 恒失败 → 判定为「本时段没跑过」，
    hm >= FIRST/SECOND 的无上界判定于是再触发一轮全站登录（幂等但多一轮真实请求，
    且覆盖当日已 success 的状态文件）。与 signin.py 的状态文件写入同口径。
    """
    # 写失败与读失败同向（退化为既有闩锁语义：读不到即允许触发），不告警；
    # 半成品 tmp 已由单通道就地清掉，残留兜底归 state_gc
    with contextlib.suppress(OSError):
        private_json.write_private_json(
            _slot_marker(kind),
            {"triggered_at": clock.now().strftime("%H:%M:%S")})


def _heartbeat_path(state_dir=None):
    return os.path.join(state_dir or STATEDIR, HEARTBEAT_FILE)


def _touch_heartbeat(state_dir=None):
    """落活体心跳（pid + 业务时刻；tmp + os.replace 原子写，同 _mark_slot 口径）。

    只写进**已存在**的目录：心跳是旁路观测件，不承担建目录职责（容器入口
    entrypoint.sh 已建 /data/state）。目录不在就静默跳过——写失败同样静默：
    探活侧看到的"心跳断流"正是期望中的不健康信号，观测件绝不反噬调度循环，
    也不许把已清理的目录"复活"（守护线程生命周期长于单次用例）。
    """
    path = _heartbeat_path(state_dir)
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        return
    # 写失败静默：探活侧看到的"心跳断流"正是期望中的不健康信号；半成品归单通道清
    with contextlib.suppress(OSError):
        # 内容用系统钟：与 mtime（探活真正的判据）同钟同口径，仅作人工排查
        # 现场；业务钟不参与——观测链路不该依赖调度语义，也不得反过来
        # 抢占/干扰主循环的判据时钟。
        # ensure_dir=False：旁路观测件不得把已清理的目录"复活"（上面 isdir 门 +
        # 不建目录，模式仍由单通道钉死 0600）。
        private_json.write_private_json(
            path,
            {"pid": os.getpid(),
             "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            ensure_dir=False)


_heartbeat_started = False
_heartbeat_stop = threading.Event()


def _start_heartbeat():
    """主循环入口：先同步落一次心跳（首刻即可被探活），再起守护线程周期重写。

    每进程只起一条心跳线程（重复调用只补落一次）；daemon=True——它是进程
    活体的旁路证明，不该独立于主进程存活。节拍用 Event.wait 而非 time.sleep：
    心跳线程与调度循环各走各的时钟通道，测试/桩替换主循环的 sleep 不得
    顺带劫持观测件。
    """
    global _heartbeat_started
    _touch_heartbeat()
    if _heartbeat_started:
        return
    _heartbeat_started = True

    def _beat():
        while not _heartbeat_stop.wait(HEARTBEAT_INTERVAL):
            try:
                _touch_heartbeat()
            except Exception as e:
                # 单次意外（磁盘抖动等）不该永久杀死心跳线程：线程一退，心跳
                # 断流就是一份无人可恢复的假不健康。留痕一行后进下一拍继续。
                logger.warning("[sched-heartbeat] 心跳落盘异常，下一拍重试: %r", e)

    threading.Thread(target=_beat, daemon=True, name="sched-heartbeat").start()


def healthcheck_main(state_dir=None):
    """模块级探活入口：与 `__main__` 的 --check-health 快路共用同一判据
    （_heartbeat_is_fresh），供常驻进程与测试直接复用。"""
    return 0 if _heartbeat_is_fresh(_heartbeat_path(state_dir)) else 1


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
            logger.info("已清理 %d 个过期状态/日志文件", removed)
    except (OSError, ValueError) as e:
        logger.warning("状态清理失败（不影响调度）: %s", e)


# ---------------------------------------------------------------------------
# 每日 02:00 定时备份（M44：容器形态过去完全没有备份）
# ---------------------------------------------------------------------------
# 为什么是"调度器内的挂点"而不是"让部署者自己加宿主 cron"：容器部署的用户往往只有
# `docker compose up`，没有宿主 root、也不该给他们装 cron。备份是最后一道恢复底座，
# 漏装一次就等于静默丢失——所以它必须**跟着容器一起来**，而不是跟着一份可漏抄的
# 部署文档走。挂点复用与首签/补签同一套"分钟级闩锁 + 按日落盘槽位"语义。
#
#: 备份脚本候选路径。仓库里它在 `docker/`，镜像里 Dockerfile 把它 COPY 到
#: `scripts/`（与 scheduler.py 同处，两处都靠候选列表而不是单一硬编码路径解析）。
BACKUP_SCRIPT_CANDIDATES = ("docker/backup-docker.sh", "scripts/backup-docker.sh")

#: 失败重试：一次失败不等于"今天没有备份"（磁盘抖动/gpg 短暂不可用都是可重试的）。
#: 重试间隔与最大次数都刻意保守，且**到点就收手**（见 _backup_due 的截止时刻）——
#: 备份子进程最长 BACKUP_TIMEOUT 秒，密集重试会把整点前的调度循环占死。
BACKUP_RETRY_SECONDS = 600
BACKUP_MAX_TRIES = 3
BACKUP_TIMEOUT = 1800


def _backup_script_path():
    """定位容器内可执行的备份脚本；一个都不存在时返回 None。"""
    for rel in BACKUP_SCRIPT_CANDIDATES:
        path = os.path.join(_REPO_ROOT, rel)
        if os.path.isfile(path):
            return path
    return None


def _backup_env():
    """备份子进程的环境。

    口径与全仓一致（**`.env` 优先、进程环境只补缺**，见 M27）：容器调度器的兜底门
    已经这么判（`_fallback_gate_env` 的注释），备份的路径类参数必须同口径，否则
    compose 里显式注入的 `YIBAN_BACKUP_*` 会被 Web 设置页/`.env` 里的同名键悄悄顶掉。
    口令本身**不入环境**：`backup-docker.sh` 只从 `YIBAN_BACKUP_PASSPHRASE_FILE` 读
    0600 文件（M97），绝不用 `_PASSPHRASE` 环境变量形态。
    """
    src = dict(os.environ)
    src.update(parse_env_file(ENV_FILE))   # `.env` 优先；它只认 YIBAN_ 前缀的键
    env = dict(src)
    # 调度器的 YIBAN_BACKUP_* → backup-docker.sh 自己的参数名（后者才是脚本认的）。
    # 显式设了 YIBAN_BACKUP_* 就**压住**同名的进程环境变量（否则 compose 注入的
    # BACKUP_DIR 会反过来盖掉 .env —— 正是 M27 要消灭的那种"两处打架"）。
    for script_key, sched_key, default in (
            ("DATA_DIR", "YIBAN_BACKUP_DATA_DIR", "/data"),
            ("BACKUP_DIR", "YIBAN_BACKUP_DIR", "/backups"),
            ("RETAIN_DAYS", "YIBAN_BACKUP_RETAIN_DAYS", "30")):
        if sched_key in src:
            env[script_key] = src[sched_key]
        else:
            env.setdefault(script_key, default)
    # gpg 需要一个可写的家目录（即使对称加密不建密钥环，也会拿它放临时文件）；
    # 容器里 HOME 可能不可写，固定指向状态目录下的私有子目录。
    env.setdefault("GNUPGHOME", os.path.join(STATEDIR, "gnupg"))
    return env


def _backup_configured(env=None):
    """备份是否可用：口令文件**配了且真的可读**才算可用。

    为什么不用"键非空"当判据：`backup-docker.sh` 拒绝产出明文包，所以没口令就等于
    没备份；而 compose 里那行 `YIBAN_BACKUP_PASSPHRASE_FILE` 是常驻的（默认部署没挂
    那个文件，见 compose 注释），若按"键非空"判，**默认部署会天天三次重试后报
    ERROR**——把一条"没启用"说成"失败了"。判可读，缺的只是那一次挂载。
    """
    env = env if env is not None else _backup_env()
    path = str(env.get("YIBAN_BACKUP_PASSPHRASE_FILE", "")).strip()
    return bool(path) and os.path.isfile(path) and os.access(path, os.R_OK)


def _run_backup():
    """跑一次 `docker/backup-docker.sh`；返回 (是否成功, 可读的原因)。

    失败不抛：主循环的单 tick 兜底不该被一次备份异常带崩（那等于备份故障顺带停掉
    全天签到）。子进程 spawn 失败同样接住——同 `_run_signin_child` 的判法。
    """
    script = _backup_script_path()
    if script is None:
        return False, "镜像里找不到 backup-docker.sh（Dockerfile 未 COPY？）"
    env = _backup_env()
    # 建不出来就由脚本/gpg 自己去失败，不在这里替它判（contextlib 已在本文件导入）
    with contextlib.suppress(OSError):
        os.makedirs(env.get("GNUPGHOME") or os.path.join(STATEDIR, "gnupg"), exist_ok=True)
    try:
        proc = subprocess.run(["bash", script], cwd=_REPO_ROOT, env=env,
                              timeout=BACKUP_TIMEOUT, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        logger.warning("容器备份超时（>%ds）被终止", BACKUP_TIMEOUT)
        return False, f"备份超时（>{BACKUP_TIMEOUT}s）"
    except OSError as e:
        logger.warning("备份脚本拉起失败: %s", e)
        return False, f"备份脚本拉起失败：{e}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        logger.warning("容器备份失败（rc=%s）: %s", proc.returncode, " / ".join(tail))
        return False, f"rc={proc.returncode} " + " / ".join(tail)
    logger.info("容器备份完成（目录 %s）", env.get("BACKUP_DIR"))
    return True, env.get("BACKUP_DIR", "")


def _backup_due(now, last_backup_try, tries):
    """此刻是否该跑备份 → bool。

    与首签/补签同一套闩锁语义：`now.hour >= BACKUP_AT` 且当日槽位标记没落过。**为什么
    用无上界比较而不是 `hm == (2, 0)`**：容器 02:00 时可能在打镜像升级或刚重启，
    整分命中会整天丢掉这一天的备份（这正是本条要补的洞，不能自己再挖一个）。
    """
    if tries >= BACKUP_MAX_TRIES:
        return False
    if last_backup_try is not None and (now - last_backup_try).total_seconds() < BACKUP_RETRY_SECONDS:
        return False
    return now.hour >= BACKUP_AT[0] and not _slot_done("backup")


def _tick_backup(now, state):
    """02:00 备份挂点的一次 tick。`state` 是跨 tick 记账的 dict（重试次数 + 上次尝试）。

    记账刻意放在函数外的 dict 里：main_loop 里已经有一堆 `done_*` 局部变量，再塞两个
    局部量会让那个 `try` 块更长，而 `_cleanup_state` 那条路径证明记账不必是局部量。
    """
    if not _backup_configured():
        # 未配置口令文件 = 部署者没启用备份。静默跳过（每天只在首次记一行），
        # 且**不消耗重试预算**——没配就是没配，重试 3 次也还是没配。
        # 落槽位标记的代价要认：当天后来才补挂口令文件的话，要等次日 02:00 才生效
        # （标记跨重启也认，避免每分钟重判一次）。这是"未启用"这一稳态的合理代价。
        if not state.get("warned"):
            state["warned"] = True
            _mark_slot("backup")
            logger.info("容器备份未启用（未挂载可读的 YIBAN_BACKUP_PASSPHRASE_FILE），跳过")
        return False
    state["tries"] = state.get("tries", 0) + 1
    state["last"] = now
    ok, detail = _run_backup()
    if ok:
        _mark_slot("backup")
        state["tries"] = 0
        return True
    if state["tries"] >= BACKUP_MAX_TRIES:
        # 收手：继续重试只会把签到前的调度循环占死。放弃当天并在下一分钟落槽位，
        # 让"今天没备份"这件事安静下来（备份缺失由宿主形态的哨兵/本页日志承担；
        # 容器形态没有哨兵，故这里必须留一行 ERROR 级日志）。
        _mark_slot("backup")
        logger.error("容器备份连续 %d 次失败，当天放弃：%s", BACKUP_MAX_TRIES, detail)
    return False


# 首签 / 补签 时间点（分钟级），用「已进入该分钟且当天未执行过」的闩锁语义，
# 见 main_loop。
# 探针为「周期尝试」而非固定时刻（2026-08-31 修复）：原 PROBE_AT=(23,55) 与
# 设置页 YIBAN_PROBE_TIME 两套时钟脱钩——容器内改探针时间不生效（同宿主 cron
# 写死 23:55 的问题）。现每 PROBE_TRY_SECONDS 尝试一次 --probe（探针未开启不
# spawn），signin.py 内部按 PROBE_TIME / 频率 / 当日防重裁决是否真正探测，
# 与宿主 cron */10 轮询语义对齐。
FIRST, SECOND = (6, 31), (7, 10)
#: 每日定时备份时刻（M44），与宿主 cron 的 `0 2 * * *` 同点。取 02:00 而非更晚：
#: 赶在首签（06:31）与状态清理（03:00）之前，把"昨天一天的数据"完整封存，也给
#: 备份子进程留出足够时间在早上之前跑完。
#: **只用 `[0]`（小时），分钟位是意图标注不是判据**：`_backup_due` 走
#: `now.hour >= BACKUP_AT[0]` 的无上界判定（容器 02:00 恰逢重启/打镜像时不能整天
#: 丢掉备份）。把它"修正"成整分命中即重犯 M44。
BACKUP_AT = (2, 0)
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
    # 与同文件 `_start_fallback_child` 调用点同一判法：OSError（解释器丢失、fork
    # 资源耗尽等）接住留痕返回，**绝不穿透 main_loop**——同一文件不许出现两种判法
    # （兜底接、首签不接，等于一次 spawn 失败带崩全天调度）。失败后调用方仍照常
    # 落槽位标记：同一时段不会逐秒反复 spawn（容器重启即双跑，监督重启同罪）。
    try:
        proc = subprocess.Popen(cmd, cwd="/app", env=env)
    except OSError as e:
        logger.warning("签到子进程拉起失败: %s", e)
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        logger.warning("签到子进程超时（>%ds）被终止，已留痕继续调度", timeout)


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
        logger.warning("拉起兜底常驻执行体失败: %s", e)
        return False
    logger.info("已在签到窗口内拉起兜底常驻执行体（窗口结束自行退出）")
    return True


def main_loop(sleep_seconds=1):
    """调度主循环。闩锁按 (任务, 当日) 记账，并落盘到 sched-slot 标记——
    进程重启后当日已触发过的时段不再二次触发（闩锁内存态重启即丢），
    未完成账号仍由另一时段的闸门（全量标记缺失/存在未了结）兜底。

    SECOND 不受 FIRST 影响：若首签子进程一直占用到越过 07:10，循环恢复后
    hm>=SECOND 仍会补一次（signin 内状态文件已防重），不再全天丢失补签。

    单 tick 兜底：循环体内任意异常记日志后进下一 tick——一次闸门/落盘/清理的
    意外不得让首签/补签/探针/兜底/清理同进程全废全天。兜底边界是 Exception：
    KeyboardInterrupt/SystemExit 直通（监督停机与容器 stop 依赖的信号语义不许
    被吞）。time.sleep 在兜底之外，异常 tick 同样照常歇到下一拍。
    """
    done_sign_first = None   # date | None
    done_sign_second = None
    last_probe_try = None    # datetime | None：上次尝试探针的时刻（周期尝试）
    last_fallback_try = None  # datetime | None：上次检查兜底常驻的时刻（周期检查）
    last_clean = None
    backup_state = {}         # 备份重试记账（M44，见 _tick_backup）
    _start_heartbeat()
    while True:
        try:
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
                # 开关真值口径单源在 schedule._env_flag（= env_io.parse_env_flag：
                # 1/true/on/yes，大小写与两侧空白不敏感），与裸机 run_probe.sh 同口径
                if schedule._env_flag("YIBAN_PROBE_ENABLE", env):
                    _run_signin_child(extra=["--probe"], env=env)
            if (last_fallback_try is None
                    or (now - last_fallback_try).total_seconds() >= FALLBACK_TRY_SECONDS):
                # 兜底常驻：窗口内拉起、窗口结束由进程自行退出。判据只看 `.env`
                # （网页写入的开关与窗口设置），不构造完整子进程环境——未开时不产生
                # 任何副作用（不 spawn、不打日志）。
                last_fallback_try = now
                _tick_fallback(now)
            # M44：每日定时备份（02:00）。放在清理之前判，是为了让"备份成功"这件事
            # 早于任何一次可能耗时的清理落定；两者都失败也各自兜底，不互相牵连。
            if _backup_due(now, backup_state.get("last"), backup_state.get("tries", 0)):
                _tick_backup(now, backup_state)
            if now.hour >= 3 and last_clean != today:
                _cleanup_state()
                last_clean = today
        except (KeyboardInterrupt, SystemExit):
            raise  # 停机/中断语义直通，兜底只吃 Exception
        except Exception as e:
            # 单 tick 兜底（本文件唯一一处 Exception 级判法，其余按类型接）：
            # 记痕进下一 tick，绝不让一次意外废掉全天调度
            logger.warning("本 tick 异常，记日志后进下一 tick: %r", e)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    # --check-health 已在文件头（业务导入图之前）短路退出，能走到这里的
    # 只有常驻调度主循环（supervisord 的启动形态）。
    _setup_logging()
    main_loop()
