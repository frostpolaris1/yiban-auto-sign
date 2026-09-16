# -*- coding: utf-8 -*-
"""按天状态文件的清理策略：**唯一事实源**（宿主 cron 与容器调度共用）。

状态目录里的"按日文件"由多处写入（signin 的签到状态/全量收尾标记/邮件额度账本、
容器调度的时段闩锁标记、调度快照、按天签到日志）。它们只在**当天**有意义（少数要供
日历回看），如果不清理就是无界增长：目录条目随天数线性膨胀，web 日历按前缀
`os.scandir` 整目录扫描（`sign-daily-*` 回退读取）也会随之变慢。

此前清理规则只写在一个 bash 脚本里、且只覆盖 3 个模式，容器侧只清日志目录——
新增一类按日文件时没有任何机制提醒补规则。本模块把"哪些文件按日生成、各保留多久"
收成一张表，两处调用点（`scripts/state_gc.py` 的 CLI、`docker/scheduler.py`）共用；
`tests/test_state_gc.py` 有一道元测试：代码里出现的每个按日状态文件名，都必须在本表里。

保留期分两档（键与部署文档同口径，可用环境变量覆盖）：
- `YIBAN_RETENTION_DAYS`（默认 365）：日志与"历史可回看"的状态文件；
- `YIBAN_SNAPSHOT_RETENTION_DAYS`（默认 7）：只在近几天有意义的标记/账本。

删除一律**按文件名里的日期**判定（比 mtime 精确：复制/恢复会重置 mtime）。
"""
import datetime
import os
import re

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


def _cutoff(days, now=None):
    base = (now or datetime.datetime.now()).date()
    return (base - datetime.timedelta(days=days)).strftime("%Y-%m-%d")


def sweep(state_dir, log_dir=None, env=None, now=None):
    """清理过期按日文件与孤儿临时文件；→ (删除数, 明细列表)。

    `log_dir` 默认为 state_dir（宿主形态两者同目录；容器形态分别是 /data/logs
    与 /data/state）。日志类文件只在 log_dir 里找，其余在 state_dir 里找。

    明细行形如 `sign-state-2025-01-02.json（过期）`，供调用方写清理日志/测试断言。
    目录不存在、单个文件删不掉（并发占用）都不算错误：跳过并继续。
    """
    env = os.environ if env is None else env
    log_dir = log_dir or state_dir
    cutoffs = {
        "log": _cutoff(retention_days("log", env), now),
        "snapshot": _cutoff(retention_days("snapshot", env), now),
    }
    removed, detail = 0, []
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
                    os.remove(path)
                    removed += 1
                    detail.append(f"{name}（{art.bucket} 过期）")
            except OSError:
                continue
    removed += _sweep_orphan_tmp(state_dir, detail, now)
    return removed, detail


def _sweep_orphan_tmp(state_dir, detail, now=None):
    """删除写盘中断留下的 `<name>.tmp<pid>`（按 mtime 判，超过 1 天必为孤儿）。"""
    if not os.path.isdir(state_dir):
        return 0
    removed = 0
    threshold = ((now or datetime.datetime.now()) - datetime.timedelta(
        seconds=_TMP_MAX_AGE_SEC)).timestamp()
    for name in sorted(os.listdir(state_dir)):
        if _TMP_MARK not in name:
            continue
        path = os.path.join(state_dir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < threshold:
                os.remove(path)
                removed += 1
                detail.append(f"{name}（中断的半成品）")
        except OSError:
            continue
    return removed


def sweep_empty_cred_state(state_dir):
    """删除内容为空（`{}` 或零字节）的 `cred-state.json`。

    熔断状态的**归属方**是 `yiban/cred_state.py`（它的写入路径已保证"空即删文件"），
    这里是兜底：手工/旧版本留下的空文件会让"无暂停"与"文件缺失"两种语义并存，
    排查时容易误判。有暂停记录则保留。返回是否删除。
    """
    path = os.path.join(state_dir, "cred-state.json")
    try:
        with open(path, encoding="utf-8") as f:
            content = re.sub(r"\s", "", f.read())
    except OSError:
        return False
    if content and content != "{}":
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False
