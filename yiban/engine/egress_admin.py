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
`yiban.engine` 的命令行子命令实现层。速率的**引擎域**只有一份，在
`yiban.engine.token_bucket`——本模块回问桶自己的夹取，不自建字面量。出口速率的键名与
出厂值都只由 `yiban.engine.schedule.planner_config()` 取用。名册另声明了该键的域，两者
不一致（域漂移，登记在 `scripts/gate/shared-facts.tsv` 的已知盲点）。

**复用**
`token_bucket.EgressBucket`（速率域的判据：构造时经它自己的夹取）、
`schedule.planner_config()`（出口速率键的唯一取值点）、
`store.db.open_readonly`（只读打开：不建库、不建表、不迁移）、
`egress.outlet_label`（出口标识的展示渲染处）、`egress.is_outlet_identity`
（区分出口标识与旧执行体身份键）、`clock`（时间域）、
`engine.cli_support`（`_say` / `_emit_json` / `_fail` 输出收口）。

**通信**
输入：argparse 的 `egress` 命名空间、`_paths` 解析出的路径 dict、配置视图 `view`。
输出：stdout 一行 JSON（`--json`）或 stderr 人类可读汇总；退出码 0 / 1 / 2
（含义见 `docs/dev/cli.md` §3）。
调用谁：`yiban.engine.token_bucket`、`yiban.engine.schedule`、`yiban.egress`、
`yiban.store.db`、`yiban.clock`、`yiban.engine.cli_support`。
谁调用：`yiban/cli.py` 的 `_dispatch`。

**四条边界**
1. 桶键现在是**出口标识**（`direct` 或去 userinfo 的 `scheme://host[:port]`，工单 2cwd）。
   本模块的**任何输出都不回含主机名的旧格式键**：`egress.outlet_label` 把直连渲染为
   「直连（本机出口）」、把不是出口标识的旧键渲染为「已弃用」，两者都不回原串。
   逐字键仍可作为逃生口（`selector` 列就是库里那一列），但它不会被打印出来。
2. 维护类子命令不得顺手建库、建表、跑迁移或跑启动清理（`db_maintenance` 的同一条红线）。
   故只读面走 `open_readonly`，写入只发一条 `UPDATE`，都不经 `db.get_conn()`。
3. 活进程持有内存态时，复位只落到库里：执行体每 10s 把内存态落库一次，会把复位覆盖回去。
   故输出里给 `held_by_live_process`（按 `updated_at` 是否在一分钟内），操作者可据此决定
   是先停执行体还是直接复位。复位本身也刷 `updated_at`（那是全库统一的"上次写入时刻"），
   故复位后一分钟内的 `--status` 会把该行标为可能被持有——方向保守，不是故障。
4. 两条速率来源（配置键与 `--rate`）过**同一道**域校验。该键的名册声明域宽于引擎域，中间值
   引擎会静默夹回，故本模块对两条来源都拒绝（不夹、不猜）。配置键越域时只读面也响亮失败：
   那是配置错误，报一个引擎不会用的速率才是误导。
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
def _check_rate(rate):
    """速率是否与引擎域一致 → `(rate, 错误文本)`；域的唯一真值源是桶自己的夹取。

    判据不是"数值落在某个区间里"，而是"引擎装回这个值后还是不是它"。静默夹值会把
    "我设了 50"变成"实际 4.0"而不留痕；拒绝并报出引擎会用的值更诚实。
    """
    from yiban.engine import token_bucket
    effective = token_bucket.EgressBucket("rate-probe", rate=rate).rate
    if effective != rate:
        return None, (f"{rate} 不在引擎的速率域内：装载后会变成 {effective}，"
                      "这里拒绝而不是静默夹值")
    return rate, ""


def _parse_rate(text):
    """显式速率文本 → `(rate, 错误文本)`：非数值或越域一律拒绝，不夹、不猜。"""
    try:
        rate = float(text)
    except (TypeError, ValueError):
        return None, f"--rate 不是数值: {text!r}"
    return _check_rate(rate)


def _engine_rate(view):
    """引擎出厂速率 → `(rate, 错误文本)`。

    取值点只有一处（`schedule.planner_config()`）。取到的值再过**同一道**域校验：该键的
    名册声明域（`config/registry.json`）宽于引擎域，中间值（如大于 4.0）引擎装载时会静默
    夹回。不校验就会出现"命令报已写 50、引擎按 4.0 跑"（缺陷 D2）。

    为什么先把视图导出进程环境：`run.sh` 先把 `.env` 逐键导出为环境变量再拉起引擎；直接跑
    `python -m yiban.cli` 时没有这一步，同一份配置在两条路径下会解析出不同的值。
    """
    value = str(view.get(RATE_KEY, "") or "").strip()
    if value:
        os.environ[RATE_KEY] = value
    from yiban.engine import schedule
    rate = float(schedule.planner_config()["bucket_rate"])
    checked, err = _check_rate(rate)
    if err:
        return None, (f"引擎出厂速率 {err}。请把 {RATE_KEY} 改到引擎域内，"
                      "或用 --rate 指定一个域内值")
    return checked, ""


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
    """库里一行 → 输出视图。`egress_state.egress` 现在是**出口标识**（工单 2cwd：限速桶键
    = 出口标识，不再是执行体身份），故标签由 `egress.outlet_label` 渲染：直连回「直连（本机
    出口）」，代理回脱敏的 `scheme://host[:port]`，旧格式（执行体身份键）回「已弃用」。

    `selector` 只对**出口标识**回原值（供 `--reset` 精确命中）；旧格式键的 `selector` 留空—
    ——**绝不回含主机名的原键**（本模块边界 #1）。旧行是改键前的存量、改键后不再被引擎消费，
    其清理归 `migrate_v22`（一次性清空）。
    """
    key = str(row["egress"] or "")
    tag = f"[{yb_egress.outlet_label(key)}]"
    stale = _stale_sec(row["updated_at"])
    return {
        "tag": tag,
        "selector": key if yb_egress.is_outlet_identity(key) else "",
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

    先按逐字出口键精确匹配（`selector` 列就是库里那一列，`direct` 或
    `scheme://host[:port]`；多行同名时的逃生口），再按展示标签匹配（`[直连（本机出口）]` /
    `[http://host:port]` / `[已弃用（旧执行体身份键）]`）。
    标签命中多行时**拒绝**而不是任选一行：猜错出口等于把速率改到别的桶上。
    """
    key = str(target or "").strip()
    exact = [i for i, raw in enumerate(raws) if raw == key]
    if exact:
        return exact, ""
    label = key.strip("[]")
    hits = [i for i, v in enumerate(views) if v["tag"].strip("[]") == label]
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
    畸形入参（空选择串、越域速率）响亮失败，不退化成无操作的只读查询。
    """
    db_file = paths["db_file"]
    json_mode = bool(args.json)
    # `is not None` 而非真值判定：`--reset ''` 是一次**给了参数的复位请求**。
    # 按真值判会把空串当成"没给 --reset"，于是 `--yes` 被静默忽略、退出码 0（缺陷 D1）。
    resetting = args.reset is not None or bool(args.reset_all)
    action = "reset" if resetting else "status"
    if resetting and not bool(args.reset_all) and not str(args.reset).strip():
        return _fail("egress", 2, [
            "--reset 需要一个出口名（出口标识如 direct / http://host:port，"
            "或 `--status` 报出的方括号标签）；收到的选择串是空的",
        ], json_mode, error_kind="usage", action=action, db_file=db_file,
            dry_run=True, applied=False, written=0, rows=[], changes=[],
            target_rate=None, target_rate_source=None)
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
    # 两条速率来源走**同一道**域校验：键与 --rate 不得给出相反的结论（缺陷 D2）。
    if args.rate is not None:
        target_rate, err = _parse_rate(args.rate)
    else:
        target_rate, err = _engine_rate(view)
    if err:
        return _fail("egress", 1, [err], json_mode, error_kind="config_error",
                     action=action, db_file=db_file, dry_run=not args.yes,
                     applied=False, written=0, rows=views, changes=[],
                     target_rate=None, target_rate_source=rate_source)

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
            known = "、".join(sorted(v["tag"] for v in views)) or "（无）"
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
