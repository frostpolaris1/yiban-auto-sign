# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
`egress` 子命令的实现：列出各出口的令牌桶状态，把速率复位到出厂值（或显式值）。

用途是给运维一条**受支持的撤销**：风控信号把某出口砍到 1/4 后，AIMD 要约 8 个干净轮
才能爬回；此前只能直接改库。复位写的是库里的记忆，进程内的桶不受影响（见下"边界"）。

**为什么单独成模块**：命令行门面 `yiban/cli.py` 的体量已近上限。本族只服务
"限速自适应的人工撤销"一条变更轴，与 `db` 维护族（库文件本身的运维动作）不同，
故按同族先例（`yiban/engine/db_maintenance.py`）外移。

**归属**
`yiban.engine` 的命令行子命令实现层。速率的域名只有一份，在
`yiban.engine.token_bucket`；出口速率的键名与出厂值都只由
`yiban.engine.schedule.planner_config()` 取用。

**复用**
`token_bucket.EgressBucket`（速率域的判据：构造时经它自己的夹取）、
`schedule.planner_config()`（出口速率键的唯一取值点）、
`store.db.open_readonly`（只读打开：不建库、不建表、不迁移）、
`egress.owner_tag`（角色与槽位标签的唯一渲染处）、`clock`（时间域）、
`engine.cli_support`（`_say` / `_emit_json` / `_fail` 输出收口）。

**通信**
输入：argparse 的 `egress` 命名空间、`_paths` 解析出的路径 dict、配置视图 `view`。
输出：stdout 一行 JSON（`--json`）或 stderr 人类可读汇总；退出码 0 / 1 / 2
（含义见 `docs/dev/cli.md` §3）。
调用谁：`yiban.engine.token_bucket`、`yiban.engine.schedule`、`yiban.egress`、
`yiban.store.db`、`yiban.clock`、`yiban.engine.cli_support`。
谁调用：`yiban/cli.py` 的 `_dispatch`。

**三条边界**
1. 出口键含主机名，属部署信息。本模块的**任何输出都不回原键**，只回角色与槽位标签
   （`egress.owner_tag` 的口径）。选择串也照此：写 `fallback` / `worker-2` / `single`。
   逐字键仍可作为逃生口（多主机同名时才需要），但它不会被打印出来。
2. 维护类子命令不得顺手建库、建表、跑迁移或跑启动清理（`db_maintenance` 的同一条红线）。
   故只读面走 `open_readonly`，写入只发一条 `UPDATE`，都不经 `db.get_conn()`。
3. 活进程持有内存态时，复位只落到库里：执行体每 10s 把内存态落库一次，会把复位覆盖回去。
   故输出里给 `held_by_live_process`（按 `updated_at` 是否在一分钟内），操作者可据此决定
   是先停执行体还是直接复位。复位本身也刷 `updated_at`（那是全库统一的"上次写入时刻"），
   故复位后一分钟内的 `--status` 会把该行标为可能被持有——方向保守，不是故障。
"""
import datetime
import os
import pathlib
import sqlite3

from yiban import clock
from yiban import egress as yb_egress
from yiban.engine.cli_support import _emit_json, _fail, _say
from yiban.store import db as store_db

#: 出口速率键名。键的**取值点只有一处**（`schedule.planner_config()`）；本模块只把这个值
#: 从配置视图导出到进程环境（与 `run.sh` 同源），不自己解析值、不写缺省字面量。
RATE_KEY = "YIBAN_EGRESS_RATE"

#: "可能有活进程持有"的判定窗（秒）。执行体每 10s 落库一次（`executor_v3.EGRESS_PERSIST_SEC`），
#: 故一分钟内有落库的行极可能有活执行体在持有。这是启发式，方向取保守：多报一次提醒无害，
#: 漏报会让操作者以为复位生效了。
LIVE_HOLD_SEC = 60

#: 速率比对的容差（浮点：`0.25` 这类值经往返序列化后不是精确相等）。
_RATE_EPS = 1e-9


# ---------------------------------------------------------------------------
# 速率：取值点、显式值校验
# ---------------------------------------------------------------------------
def _engine_rate(view):
    """引擎出厂速率（attempt/s）：导出配置视图后走**唯一取值点**取。

    为什么先导出：`run.sh` 先把 `.env` 逐键导出为环境变量再拉起引擎；直接跑
    `python -m yiban.cli` 时没有这一步，同一份配置在两条路径下会解析出不同的值。
    导出后两条路径同值，否则会出现"复位到 A、引擎却按 .env 的 B 起跑"。
    """
    value = str(view.get(RATE_KEY, "") or "").strip()
    if value:
        os.environ[RATE_KEY] = value
    from yiban.engine import schedule
    return float(schedule.planner_config()["bucket_rate"])


def _parse_rate(text):
    """显式速率 → `(rate, 错误文本)`：引擎装载时会改动的值一律拒绝，不夹、不猜。

    判据不是"数值落在某个区间里"，而是"引擎装回这个值后还是不是它"：速率域的唯一真值源
    是桶自己的夹取行为，故这里回问引擎一次。静默夹值会把"我设了 0.05"变成"实际 0.2"
    而不留痕；拒绝并报出引擎会用的值更诚实。
    """
    try:
        rate = float(text)
    except (TypeError, ValueError):
        return None, f"--rate 不是数值: {text!r}"
    from yiban.engine import token_bucket
    effective = token_bucket.EgressBucket("rate-probe", rate=rate).rate
    if effective != rate:
        return None, (f"--rate {rate} 不在引擎的速率域内：装载后会变成 {effective}，"
                      "这里拒绝而不是静默夹值")
    return rate, ""


# ---------------------------------------------------------------------------
# 读：只读快照
# ---------------------------------------------------------------------------
def _stale_sec(stamp):
    """`updated_at` 距现在多少秒；解析不出返回 None（照实回，不猜）。"""
    try:
        then = datetime.datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return (clock.now() - then).total_seconds()


def _row_view(row):
    """库里一行 → 输出视图：**不回含主机名的原键**，只回角色与槽位标签。

    标签由 `egress.owner_tag` 渲染（日志行的同一个词）；`selector` 是它去掉方括号的形态，
    可以直接拿去当 `--reset` 的取值，故不再另报角色与槽位两列。
    """
    tag = yb_egress.owner_tag(row["egress"])
    stale = _stale_sec(row["updated_at"])
    return {
        "tag": tag,
        "selector": tag.strip("[]"),
        "rate": float(row["rate"]),
        "burst": float(row["burst"]),
        "tat": float(row["tat"]),
        "updated_at": row["updated_at"],
        "stale_sec": stale,
        "held_by_live_process": (None if stale is None else stale < LIVE_HOLD_SEC),
    }


def _read_rows(db_file):
    """读全部 `egress_state` 行 → `(rows, error)`。

    库文件不存在 = 还没有任何桶状态：返回空列表且**不报错**（只读面不因未配置失败，
    与 `db --status` 对缺失库的处理同口径）。库在但表读不了（结构过旧、文件不是库）
    则如实报错，由调用方落成退出码 1。
    """
    conn = store_db.open_readonly(db_file)
    if conn is None:
        return [], ""
    try:
        cur = conn.execute(
            "SELECT egress, rate, burst, tat, updated_at FROM egress_state")
        rows = [{"egress": r["egress"], "rate": r["rate"], "burst": r["burst"],
                 "tat": r["tat"], "updated_at": r["updated_at"]} for r in cur.fetchall()]
    except sqlite3.Error as e:
        return [], f"读取出口桶状态失败（{db_file}）: {e}"
    finally:
        conn.close()
    return rows, ""


def _match_rows(raws, views, target):
    """按选择串挑**下标** → `(indexes, error)`。

    先按逐字出口键精确匹配（多主机同名时的逃生口），再按角色+槽位标签匹配
    （`fallback` / `worker-2` / `single`，与日志行的 `[fallback]` 同一个词）。
    标签命中多行时**拒绝**而不是任选一行：猜错出口等于把速率改到别的桶上。
    """
    key = str(target or "").strip()
    exact = [i for i, raw in enumerate(raws) if raw == key]
    if exact:
        return exact, ""
    label = key.strip("[]")
    hits = [i for i, v in enumerate(views) if v["selector"] == label]
    if len(hits) > 1:
        return [], (f"选择串 {target!r} 匹配到 {len(hits)} 行，无法唯一确定；"
                    "请改用逐字出口键（本命令的 `--status` 按角色+槽位输出，"
                    "不给含主机名的原键）")
    return hits, ""


# ---------------------------------------------------------------------------
# 写：一个事务内的若干条 UPDATE
# ---------------------------------------------------------------------------
def _write_rates(db_file, updates, stamp):
    """把若干行的 `rate` 写成目标值（**一个事务，只发 UPDATE**）→ `(written, error)`。

    为什么不经 `queue_store.save_egress_state`：那个入口走 `db.get_conn()`，未初始化时会
    隐式 `init_db()`——维护类命令不该顺手建库、建表、跑迁移与启动清理（`db_maintenance`
    的同一条红线）。连接用 `mode=rw`：库不存在时**拒绝**而不是被 `sqlite3.connect` 悄悄
    建出一个空库。本语句只改 `rate` 与 `updated_at` 两列：`burst` / `tat` 不进语句，
    逐字保留（`tat` 是单调钟域的浮点，装载时按新速率夹住，超前值不会锁桶）。
    """
    written = 0
    if not updates:
        return 0, ""
    uri = pathlib.Path(os.path.abspath(db_file)).as_uri() + "?mode=rw"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        try:
            for egress, rate in updates:
                conn.execute(
                    "UPDATE egress_state SET rate=?, updated_at=? WHERE egress=?",
                    (float(rate), stamp, egress))
                written += 1
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return 0, f"写入出口桶速率失败（{db_file}）: {e}"
    return written, ""


# ---------------------------------------------------------------------------
# 子命令入口
# ---------------------------------------------------------------------------
def _payload(*, action, db_file, dry_run, applied, target_rate, rate_source,
             rows, changes, written):
    """输出对象（`--json` 的字段名稳定；新增只追加，不改名、不复用旧名）。"""
    return {
        "command": "egress", "ok": True, "exit_code": 0,
        "dry_run": dry_run, "action": action, "db_file": db_file,
        "target_rate": target_rate, "target_rate_source": rate_source,
        "rows": rows, "changes": changes,
        "applied": applied, "written": written,
    }


def cmd_egress(args, paths, view):
    """`egress` 子命令入口 → 退出码（0 / 1 / 2，家族见 `docs/dev/cli.md` §3）。

    默认只读（`--status`）；`--reset <出口>` 与 `--reset-all` 默认只报告，`--yes` 才写。
    """
    db_file = paths["db_file"]
    json_mode = bool(args.json)
    resetting = bool(args.reset) or bool(args.reset_all)
    action = "reset" if resetting else "status"
    raw_rows, err = _read_rows(db_file)
    if err:
        return _fail("egress", 1, [err], json_mode, error_kind="runtime_error",
                     action=action, db_file=db_file, dry_run=not args.yes,
                     applied=False, written=0, rows=[], changes=[],
                     target_rate=None, target_rate_source=None)
    raws = [r["egress"] for r in raw_rows]
    views = [_row_view(r) for r in raw_rows]

    rate_source = "explicit" if args.rate is not None else "engine_default"
    if args.rate is not None and not resetting:
        return _fail("egress", 2, [
            "--rate 只在复位时有意义：请与 --reset 或 --reset-all 同用",
        ], json_mode, error_kind="usage_conflict", action=action, db_file=db_file,
            dry_run=True, applied=False, written=0, rows=views, changes=[],
            target_rate=None, target_rate_source=rate_source)
    if args.rate is not None:
        target_rate, err = _parse_rate(args.rate)
        if err:
            return _fail("egress", 1, [err], json_mode, error_kind="config_error",
                         action=action, db_file=db_file, dry_run=not args.yes,
                         applied=False, written=0, rows=views, changes=[],
                         target_rate=None, target_rate_source=rate_source)
    else:
        target_rate = _engine_rate(view)

    if action == "status":
        payload = _payload(action=action, db_file=db_file, dry_run=True,
                           applied=False, target_rate=target_rate,
                           rate_source=rate_source, rows=views, changes=[],
                           written=0)
        _report_status(db_file, views, target_rate, rate_source)
        if json_mode:
            _emit_json(payload)
        return 0

    if args.reset_all:
        picked = list(range(len(views)))
    else:
        picked, err = _match_rows(raws, views, args.reset)
        if err:
            return _fail("egress", 1, [err], json_mode, error_kind="runtime_error",
                         action=action, db_file=db_file, dry_run=not args.yes,
                         applied=False, written=0, rows=views, changes=[],
                         target_rate=target_rate, target_rate_source=rate_source)
        if not picked:
            known = "、".join(sorted(v["selector"] for v in views)) or "（无）"
            return _fail("egress", 1, [
                f"出口 {args.reset!r} 不在库中；库内现有出口：{known}",
            ], json_mode, error_kind="runtime_error", action=action, db_file=db_file,
                dry_run=not args.yes, applied=False, written=0, rows=views,
                changes=[], target_rate=target_rate, target_rate_source=rate_source)

    changes = [{"tag": views[i]["tag"], "selector": views[i]["selector"],
                "rate_before": views[i]["rate"], "rate_after": target_rate}
               for i in picked]
    if not args.yes:
        payload = _payload(action=action, db_file=db_file, dry_run=True,
                           applied=False, target_rate=target_rate,
                           rate_source=rate_source, rows=views, changes=changes,
                           written=0)
        _report_reset(changes, target_rate, rate_source, dry_run=True)
        if json_mode:
            _emit_json(payload)
        return 0

    updates = [(raws[i], target_rate) for i in picked
               if abs(views[i]["rate"] - target_rate) > _RATE_EPS]
    written, err = _write_rates(db_file, updates, clock.ts())
    if err:
        return _fail("egress", 1, [err], json_mode, error_kind="runtime_error",
                     action=action, db_file=db_file, dry_run=False, applied=False,
                     written=0, rows=views, changes=changes,
                     target_rate=target_rate, target_rate_source=rate_source)
    payload = _payload(action=action, db_file=db_file, dry_run=False, applied=True,
                       target_rate=target_rate, rate_source=rate_source,
                       rows=views, changes=changes, written=written)
    _report_reset(changes, target_rate, rate_source, dry_run=False, written=written)
    if json_mode:
        _emit_json(payload)
    return 0


# ---------------------------------------------------------------------------
# 人类可读汇总（stderr；stdout 只放结果）
# ---------------------------------------------------------------------------
def _held_note(row_view):
    """活进程持有提醒：复位会被 10s 落库覆盖，操作者需要知道这一点。"""
    return ("（`updated_at` 在一分钟内：极可能有活执行体持有该桶，"
            "复位可能被它的下一次落库覆盖）"
            if row_view["held_by_live_process"] else "")


def _report_status(db_file, views, target_rate, rate_source):
    _say("==== 出口令牌桶状态 ====")
    _say(f"库 {db_file} | 复位目标速率 {target_rate:.3f} attempt/s（{rate_source}）")
    if not views:
        _say("尚无出口桶记录（引擎还没落过库）")
        return
    for v in sorted(views, key=lambda x: x["selector"]):
        _say(f"出口 {v['tag']} 速率 {v['rate']:.3f} attempt/s，突发 {v['burst']:.1f}，"
             f"上次落库 {v['updated_at']}{_held_note(v)}")


def _report_reset(changes, target_rate, rate_source, dry_run, written=0):
    _say("==== 出口令牌桶复位 ====" + ("（计划）" if dry_run else ""))
    for c in changes:
        _say(f"出口 {c['tag']} 速率 {c['rate_before']:.3f} → {c['rate_after']:.3f} attempt/s")
    _say(f"目标速率 {target_rate:.3f} attempt/s（{rate_source}）")
    if dry_run:
        _say("未加 --yes：只报告计划，未写库")
    else:
        _say(f"已写库 {written} 行；活进程内的桶要重启执行体才会按新速率起跑")
