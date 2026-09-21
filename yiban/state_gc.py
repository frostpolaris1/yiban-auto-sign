# -*- coding: utf-8 -*-
"""**功能**
按天状态文件的清理策略：**唯一事实源**（宿主 cron 与容器调度共用）。

状态目录里的"按日文件"由多处写入（签到状态、全量收尾标记、邮件额度账本、容器调度
的时段闩锁标记、调度快照、按天签到日志）。它们只在**当天**有意义（少数要供日历回看），
不清理就是无界增长：条目随天数线性膨胀，web 日历按前缀 `os.scandir` 整目录扫描
（`sign-daily-*` 回退读取）也会随之变慢。本模块把"哪些文件按日生成、各保留多久"收成
一张表，两处调用点（`scripts/state_cleanup.py` 的 CLI、`docker/scheduler.py`）共用；
新增按日文件时**必须**在 `ARTIFACTS` 登记，否则它永远不被清理——`tests/test_state_gc.py`
有一道元测试双向核对代码里出现的按日前缀与本表。

保留期分两档（键与部署文档同口径，可用环境变量覆盖）：
- `YIBAN_RETENTION_DAYS`（默认 365）：日志与"历史可回看"的状态文件；
- `YIBAN_SNAPSHOT_RETENTION_DAYS`（默认 7）：只在近几天有意义的标记/账本。

删除一律**按文件名里的日期**判定（比 mtime 精确：复制/恢复会重置 mtime）。

**归属**
`yiban` 包根的清理策略模块；`scripts/state_cleanup.py`（宿主 cron）与
`docker/scheduler.py`（容器调度）共用同一份，CLI 子命令 `python -m yiban.cli state`
也走它。

**复用**
`ARTIFACTS`（按日文件登记表）、`state_dir_from_env` / `log_dir_from_env`（目录解析）
与清理函数是唯一事实源；目录解析复用 `yiban.infra.env_io.resolve_path`。

**通信**
输入：环境变量（`YIBAN_STATE_DIR` / `YIBAN_LOG_FILE` / 保留期两档）与当前日期。
输出：被删除的过期按日文件与清理结果（供调用方打印/记日志）。
调用谁：`yiban.clock`、`yiban.infra.env_io`。
谁调用：`scripts/state_cleanup.py`、`docker/scheduler.py`、`yiban/cli.py` 的 `state` 子命令。
前端调用点：容器调度器的清理结果与保留期设置经 `/api/settings`
（`web/static/js/components/settings-quota.js` 等设置页）暴露；登记表或保留期口径变化会改变运维
在这些页面看到的清理/容量信息。
"""
import datetime
import os
import re

from yiban import clock
from yiban.infra import env_io

# 保留期档位（默认值；调用方可用环境变量覆盖）
RETENTION_DAYS = 365
SNAPSHOT_RETENTION_DAYS = 7

# 状态文件名里的日期：一律是"去掉前后缀后"的最后 10 个字符
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class Artifact:
    """一类按日状态文件：前缀 + 后缀决定匹配，bucket 决定保留期，where 决定目录。"""

    __slots__ = ("bucket", "prefix", "suffix", "what", "where")

    def __init__(self, prefix, suffix, bucket, where, what):
        self.prefix = prefix
        self.suffix = suffix
        self.bucket = bucket
        self.where = where
        self.what = what


# 表里每一项都必须有对应写入点（元测试会双向核对）。新增按日文件时**必须**在此登记，
# 否则文件永远不被清理——这是本模块存在的主要理由。
ARTIFACTS = (
    Artifact("sign-", ".log", "log", "log", "按天签到日志（web 日志页按日期读取）"),
    Artifact("sign-state-", ".json", "log", "state", "按日结构化状态（签到日历数据源）"),
    Artifact("sign-daily-", ".json", "log", "state", "旧版按日状态（日历回退读取）"),
    Artifact("mail-user-fail-", ".json", "snapshot", "state", "用户失败提醒额度账本（仅当日有效）"),
    Artifact("sched-run-", ".json", "snapshot", "state", "当日全量签到收尾标记（闸门用）"),
    Artifact("sched-slot-", ".json", "snapshot", "state", "容器调度时段闩锁标记（按日失效）"),
    Artifact("sched-snapshot-", ".json", "snapshot", "state", "调度快照（仅近期有意义）"),
)

# 与文件同名的 flock 伴生文件后缀（`locks.file_lock` 创建 `<path>.lock`）：
# 按日文件的锁也按日生成，同样要清；先剥掉它再匹配前后缀，孤儿锁也能被扫到。
_LOCK_SUFFIX = ".lock"

# 半成品临时文件：写盘走 tmp + os.replace，进程被杀会留下 `<name>.tmp<pid>`。
# 这类文件没有日期可判，按 mtime（超过 1 天必是孤儿）清理。
_TMP_MARK = ".tmp"
_TMP_MAX_AGE_SEC = 86400


def retention_days(bucket, env=None):
    """档位 → 保留天数；环境变量值非法时抛 ValueError（由调用方响亮失败）。"""
    env = os.environ if env is None else env
    key = "YIBAN_RETENTION_DAYS" if bucket == "log" else "YIBAN_SNAPSHOT_RETENTION_DAYS"
    default = RETENTION_DAYS if bucket == "log" else SNAPSHOT_RETENTION_DAYS
    raw = str(env.get(key, "") or "").strip()
    if not raw:
        return default
    days = int(raw)          # 非法值 → ValueError
    if days < 0:
        raise ValueError(f"{key} 不能为负: {raw}")
    return days


def match(name):
    """→ (Artifact, 日期字符串)；不匹配任何按日模式时返回 (None, None)。"""
    base = name[:-len(_LOCK_SUFFIX)] if name.endswith(_LOCK_SUFFIX) else name
    for art in ARTIFACTS:
        if not (base.startswith(art.prefix) and base.endswith(art.suffix)):
            continue
        core = base[len(art.prefix):len(base) - len(art.suffix)]
        if len(core) < 10:
            continue
        date = core[-10:]
        if _DATE_RE.fullmatch(date):
            return art, date
    return None, None


def state_dir_from_env(env=None):
    """状态目录：`env_io.resolve_path` 口径（进程环境 → .env → 默认 `/var/log/yiban`）。

    放在本模块是为了"清理目录与写目录同源"：宿主清理脚本、容器调度与 CLI
    （`python -m yiban.cli state`）都调这里，避免又出现一份各自的默认值。
    `env` 供测试注入映射（None = 读进程环境，与 resolve_path 同语义）。
    """
    return env_io.resolve_path("YIBAN_STATE_DIR", "/var/log/yiban", env=env)


def log_dir_from_env(state_dir, env=None):
    """日志目录：`dirname(YIBAN_LOG_FILE)` → 回退 state_dir。

    不取绝对路径：run.sh 用的是 `dirname "$LOG_FILE"`（相对值即相对当前目录），
    这里保持同一语义，避免"配置相同、清理目录不同"。
    `env` 供测试注入映射（None = 读进程环境，与 resolve_path 同语义）。
    """
    log_file = env_io.resolve_path("YIBAN_LOG_FILE", "", env=env)
    if not log_file:
        return state_dir
    return os.path.dirname(log_file) or state_dir


def sweep(state_dir, log_dir=None, env=None, now=None):
    """清理过期按日文件与孤儿临时文件；→ (删除数, 明细列表)。

    `log_dir` 默认为 state_dir（宿主形态两者同目录；容器形态分别是 /data/logs
    与 /data/state）。日志类文件只在 log_dir 里找，其余在 state_dir 里找。

    明细行形如 `sign-state-2025-01-02.json（过期）`，供调用方写清理日志/测试断言。
    目录不存在、单个文件删不掉（并发占用）都不算错误：跳过并继续。
    """
    env = os.environ if env is None else env
    log_dir = log_dir or state_dir
    removed, detail = 0, []
    for path, label in _iter_expired(state_dir, log_dir, _cutoffs(env, now), now):
        try:
            os.remove(path)
        except OSError:
            continue
        removed += 1
        detail.append(label)
    return removed, detail


def plan(state_dir, log_dir=None, env=None, now=None):
    """列出**将要**清理的条目但不动手；→ (条数, 明细列表)（CLI 的 dry-run 用）。

    明细行与 `sweep` 的逐字一致（同一套匹配与保留期判定），调用方可以先把结果
    报给人看、再由 `sweep` 执行同一批；不会出现"报告一套、执行另一套"。
    """
    env = os.environ if env is None else env
    log_dir = log_dir or state_dir
    detail = [label for _path, label in _iter_expired(state_dir, log_dir, _cutoffs(env, now), now)]
    if empty_cred_state_path(state_dir) is not None:
        detail.append("cred-state.json（空内容）")
    return len(detail), detail


def empty_cred_state_path(state_dir):
    """→ 内容为空（`{}` 或零字节）的 `cred-state.json` 路径；无需清理时返回 None。

    判定与删除分开，是为了让"只看不动手"的调用方（`plan` / CLI 的 dry-run）
    能报出同一条目，又不必先删再恢复。
    """
    path = os.path.join(state_dir, "cred-state.json")
    try:
        with open(path, encoding="utf-8") as f:
            content = re.sub(r"\s", "", f.read())
    except OSError:
        return None
    if content and content != "{}":
        return None
    return path


def sweep_empty_cred_state(state_dir):
    """删除内容为空（`{}` 或零字节）的 `cred-state.json`。

    熔断状态的**归属方**是 `yiban/cred_state.py`（它的写入路径已保证"空即删文件"），
    这里是兜底：手工/旧版本留下的空文件会让"无暂停"与"文件缺失"两种语义并存，
    排查时容易误判。有暂停记录则保留。返回是否删除。
    """
    path = empty_cred_state_path(state_dir)
    if path is None:
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


# ---- 内部实现 ----
def _cutoff(days, now=None):
    """保留期截止日（按业务钟：文件名日期都是北京时间写入的）。"""
    base = (now or clock.now()).date()
    return (base - datetime.timedelta(days=days)).strftime("%Y-%m-%d")


def _cutoffs(env, now):
    """两档保留期 → 截止日期（非法配置由 retention_days 抛 ValueError，调用方响亮失败）。"""
    return {
        "log": _cutoff(retention_days("log", env), now),
        "snapshot": _cutoff(retention_days("snapshot", env), now),
    }


def _iter_expired(state_dir, log_dir, cutoffs, now=None):
    """产出 (路径, 明细行)：过期按日文件（按 ARTIFACTS 顺序、目录项名排序）与孤儿临时文件。

    `sweep`（真删）与 `plan`（只看不动手）共用本迭代器——判定口径只有这一处，
    不会出现"报告要删 A、实际删了 B"。
    """
    for art in ARTIFACTS:
        target_dir = log_dir if art.where == "log" else state_dir
        if not os.path.isdir(target_dir):
            continue
        for name in sorted(os.listdir(target_dir)):
            got, date = match(name)
            if got is not art or date >= cutoffs[art.bucket]:
                continue
            path = os.path.join(target_dir, name)
            try:
                if os.path.isfile(path):
                    yield path, f"{name}（{art.bucket} 过期）"
            except OSError:
                continue
    if not os.path.isdir(state_dir):
        return
    # epoch 秒与文件 mtime 比较，属"物理时刻"语义而非业务日——保留宿主时间
    # （与 client.py 比对服务端时间戳那处同属刻意例外，见 clock 模块头注）。
    threshold = ((now or datetime.datetime.now()) - datetime.timedelta(
        seconds=_TMP_MAX_AGE_SEC)).timestamp()
    for name in sorted(os.listdir(state_dir)):
        if _TMP_MARK not in name:
            continue
        path = os.path.join(state_dir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < threshold:
                yield path, f"{name}（中断的半成品）"
        except OSError:
            continue
