# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`python -m yiban.cli <子命令> [选项]`：agent 侧统一入口（契约见 `docs/dev/cli.md`）。

本模块是命令行**唯一入口**，`scripts/signin.py` / `scripts/db.py` / `scripts/state_cleanup.py`
是部署面（run.sh / cron / 容器调度器按文件路径调用）的兼容壳，二者行为同源。

## 硬性约定的落地方式（`docs/dev/cli.md` §2）

1. **不交互**：不读 stdin，不做 y/N 确认；不认识子命令就打一行用法到 stderr 并返回 2。
2. **stdout 只放结果**：`--json` 时是一整行 JSON 对象（`json.loads` 直接可解析）；
   人类可读汇总一律走 stderr（`_say`），日志走 stderr/日志文件。
3. **`--json` 字段名稳定**（见下表；新增字段只追加，不改名、不复用旧名）。
4. **破坏性操作默认只报告**：`state` 默认 dry-run、`db --backup` 不加 `--yes` 不写盘。
5. **配置只从 .env 与环境变量读**：命令行不接受任何敏感值（口令/密钥/代理凭据）。
6. **路径解析与 run.sh 同源**：`YIBAN_STATE_DIR` / `YIBAN_LOG_FILE` / `YIBAN_DB_FILE` /
   `YIBAN_ENV_FILE`，见 `_env_view`。
7. **未配置的可选能力不影响退出码**（如未配通知照常跑；`.env` 缺失按未配置处理）。

## 子命令与 `--json` 字段

| 子命令 | 做什么 | `--json` 顶层字段 | 退出码 |
|--------|--------|-------------------|--------|
| `sign` | 一轮签到（选项原样透传给引擎） | `command` `exit_code` | 0/1/2/3/10（引擎口径） |
| `probe` | 只读健康检查（引擎 `--probe` 语义） | `command` `exit_code` | 同上 |
| `config` | 账号配置检查（脱敏、不联网） | `command` `ok` `accounts` `accounts_missing_device` `phones_masked` `paths` `errors` | 0 正常 / 1 配置错误 |
| `capacity` | 容量建议（实测值 → 建议执行体数） | `command` `ok` `accounts` `accounts_total` `window_effective_sec` `avg_attempt_sec` `gap_sec` `capacity_per_executor` `measured_per_executor` `recommended_per_executor` `executors_needed` `paths` | 0 / 1 |
| `state` | 状态文件清理（默认 dry-run） | `command` `ok` `dry_run` `state_dir` `log_dir` `retention_days` `candidates` `removed` `detail` | 0 正常 / 1 保留期非法或目录不可用 |
| `db` | 数据库维护（状态/完整性/备份） | `command` `ok` `mode` `db_file` `user_version` `size_bytes` `tables` `accounts` `accounts_signable` `integrity_ok` `integrity_detail` `backup_path` `backup_exists` `dry_run` | 0 / 1 |
| `version` | 打印版本 | `command` `version` `python` `user_version` | 0 |

`capacity --measure` 与 `db --backup` 的取舍、以及"人类可读输出不进 stdout"的落地细节
见各自函数的文档字符串。
"""
import argparse
import json
import os
import pathlib
import sqlite3
import subprocess
import sys

from yiban import __version__ as RELEASE_VERSION
from yiban import state_gc, window
from yiban.engine import accounts as accounts_mod
from yiban.engine import runner
from yiban.engine import schedule as schedule_mod
from yiban.infra import env_io
from yiban.masking import mask_phone
from yiban.store import accounts as store_accounts
from yiban.store import db as store_db

USAGE = (
    "用法: python -m yiban.cli <子命令> [选项]\n"
    "子命令: sign | probe | config | capacity | state | db | version\n"
    "（见 `python -m yiban.cli <子命令> --help`；stdout 只放结果，人类可读汇总走 stderr）"
)

#: 仓库根（`yiban/cli.py` 上溯两层）。**只用来定位转发给子进程的工具脚本**，
#: 不是包导入引导：本模块是包内模块，入口一律 `python -m yiban.cli`（以文件路径
#: 直接执行既不支持、也不需要），故这里没有也不需要 sys.path 操作。
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 容量基准工具（需 root 的隔离测试机；CLI 只负责转发，不重复实现测量逻辑）
CAPACITY_PROBE = os.path.join(_REPO_DIR, "scripts", "loadtest", "capacity_probe.py")

#: 参数原样透传给引擎、CLI 只接 `--json` 的子命令
_PASSTHROUGH = ("sign", "probe")


# ---------------------------------------------------------------------------
# 输出：结果进 stdout（单行 JSON），人话进 stderr
# ---------------------------------------------------------------------------
def _emit_json(payload):
    """把结果对象打成**一整行** JSON 写 stdout（调用方直接 `json.loads`）。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _say(message):
    """人类可读汇总：一律走 stderr（stdout 必须保持"只有结果"）。"""
    sys.stderr.write(str(message) + "\n")
    sys.stderr.flush()


def _fail(command, code, errors, json_mode, **extra):
    """失败路径：错误信息进 stderr；`--json` 时仍打一行 `ok=false` 的对象后返回退出码。

    失败也保持"stdout 可解析"是刻意的：调用方不必先看退出码再决定怎么解析输出，
    按 `ok` 分支即可（退出码仍按契约表返回）。
    """
    for line in errors:
        _say(line)
    if json_mode:
        payload = {"command": command, "ok": False, "errors": list(errors)}
        payload.update(extra)
        _emit_json(payload)
    return code


# ---------------------------------------------------------------------------
# 配置视图与路径（与 run.sh 同源）
# ---------------------------------------------------------------------------
def _env_view():
    """.env 文件 + 进程环境变量 → 配置视图（**环境变量优先**，只读、不写盘）。

    为什么两者都读：run.sh 先把 `.env` 逐键导出为环境变量，引擎读的正是那套值；
    而 agent/CI 直接调用 CLI 时没有宿主导出，此时 `.env` 就是部署配置的载体。
    两者取并集后，同一个键在两条路径下解析到同一个值——不会出现"配置一样、
    清理目录不同"（`docs/dev/cli.md` §2 第 8 条）。`.env` 缺失 = 未配置（不是错误）。
    """
    view = dict(env_io.parse_env_file(env_io.env_path()))
    for key, value in os.environ.items():
        if key.startswith("YIBAN_"):
            view[key] = value
    return view


def _cfg_value(view, key, default=""):
    """取字符串配置：两侧去空白，空值按缺省（与引擎读环境变量同口径）。"""
    return str(view.get(key, "") or "").strip() or default


def _cfg_int(view, key, default, lo=None, hi=None):
    """取整数配置：缺失/非法/越界一律回退默认（口径同 `config_check.parse_env_int` 与
    `schedule._env_int`——配置校验"回退 + 不崩溃"，绝不让一个坏数字把命令打挂）。"""
    raw = _cfg_value(view, key)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        return default
    return value


def _paths(view):
    """四个路径键（与 run.sh 同源）→ dict；`db_file` 的缺省取自数据层常量。"""
    state_dir = state_gc.state_dir_from_env(view)
    return {
        "env_file": env_io.env_path(),
        "state_dir": state_dir,
        "log_file": _cfg_value(view, "YIBAN_LOG_FILE") or os.path.join(state_dir, "sign.log"),
        "db_file": _cfg_value(view, "YIBAN_DB_FILE") or store_db.DB_DEFAULT,
    }


# ---------------------------------------------------------------------------
# SQLite：只读打开与账号计数
# ---------------------------------------------------------------------------
def _open_ro(db_file):
    """只读打开 SQLite；文件不存在返回 None。

    维护类子命令**不得**顺手建库或跑迁移（`db.get_conn()` 会 `init_db()` 建表 + 迁移），
    故这里直连并对文件缺失显式返回 None，由调用方决定是"报 0"还是"响亮失败"。
    WAL 库在无 -shm 可写的场景下只读打开可能失败，此时退化为普通连接 + `query_only`。
    """
    if not os.path.isfile(db_file):
        return None
    uri = pathlib.Path(os.path.abspath(db_file)).as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        conn = sqlite3.connect(db_file)
        conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _account_counts(conn):
    """→ (未删除账号数, 会签到的账号数)。

    "会签到"的判据与 web/signin 同源：未删除且审核态不在
    `yiban.store.accounts.ACCOUNT_AUDIT_INACTIVE`——那个元组是**库内落库值**的唯一
    列举点（`signs_in` 用的就是它），此处按它拼 SQL 条件，不另写一套字面量。
    """
    inactive = tuple(store_accounts.ACCOUNT_AUDIT_INACTIVE)
    total = conn.execute("SELECT COUNT(*) FROM accounts WHERE deleted=0").fetchone()[0]
    marks = ",".join("?" * len(inactive))
    signable = conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE deleted=0"
        f" AND (status IS NULL OR status NOT IN ({marks}))",
        inactive,
    ).fetchone()[0]
    return int(total), int(signable)


def _read_user_version(conn):
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _db_snapshot(db_file):
    """→ (conn, user_version, 表清单, 未删除账号数, 会签到账号数)；库文件不存在返回 None。

    sqlite3.Error 原样抛出，由调用方按"响亮失败"处理（不静默报 0——那会让人以为
    库是空的）。
    """
    conn = _open_ro(db_file)
    if conn is None:
        return None
    try:
        user_version = _read_user_version(conn)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        accounts, signable = _account_counts(conn)
    except sqlite3.Error:
        conn.close()
        raise
    return conn, user_version, tables, accounts, signable


# ---------------------------------------------------------------------------
# 子命令：sign / probe（引擎参数的透传）
# ---------------------------------------------------------------------------
def _cmd_sign(json_mode, extra):
    """一轮签到：`--workers` / `--fallback` / `--only` / `--second-run-check` 等原样透传。

    引擎的机器可读输出**就是退出码**（`docs/dev/cli.md` §3），故 `--json` 只把退出码
    也写成一行对象；人类可读日志仍由引擎写往 stderr / 日志文件。`--json` 在透传前
    已被 argparse 取走，不会漏给子进程（`--workers N` 拉起的执行体不会各打一行 JSON
    把 stdout 撑花）。
    """
    code = runner.main(extra)
    if json_mode:
        _emit_json({"command": "sign", "exit_code": code})
    return code


def _cmd_probe(json_mode, extra):
    """只读健康检查：转引擎的 `--probe` 语义（是否真跑由探针开关/频率/暂停门决定）。"""
    code = runner.main(["--probe", *extra])
    if json_mode:
        _emit_json({"command": "probe", "exit_code": code})
    return code


# ---------------------------------------------------------------------------
# 子命令：config（脱敏、不联网）
# ---------------------------------------------------------------------------
def _cmd_config(args, view):
    """账号配置检查：只读配置与账号，不发任何网络请求（与引擎 `--check-config` 同源）。

    退出码 0 正常 / 1 配置错误（账号加载失败，或一个账号都没有 —— 与引擎入口的
    "零账号守卫"同一判据，免得"CLI 说没问题、签到却直接报未配置"）。
    """
    paths = _paths(view)
    try:
        accounts = accounts_mod.load_accounts()
    except (RuntimeError, ValueError) as e:  # ValueError=账号字段缺失，同按配置错误处理
        return _fail("config", 1, [f"配置加载失败: {e}"], args.json,
                     accounts=0, phones_masked=[], paths=paths)
    if not accounts:
        return _fail("config", 1, ["未配置任何账号（数据库 / YIBAN_ACCOUNTS_JSON / "
                                  "YIBAN_ACCOUNTS / YIBAN_PHONE 均为空）"], args.json,
                     accounts=0, phones_masked=[], paths=paths)
    masked = [mask_phone(a.phone) for a in accounts]
    missing_device = sum(1 for a in accounts if not a.has_device_info)
    payload = {
        "command": "config",
        "ok": True,
        "accounts": len(accounts),
        "accounts_missing_device": missing_device,
        "phones_masked": masked,
        "paths": paths,
        "errors": [],
    }
    _say(f"==== 账号配置检查 ==== 共 {len(accounts)} 个账号，配置检查通过")
    for i, (acc, shown) in enumerate(zip(accounts, masked, strict=True), 1):
        device = (f"设备: {acc.phone_model} / 识别码已配置" if acc.has_device_info
                  else "设备: 未配置（如学校开启设备绑定，签到将失败）")
        _say(f"  {i}. {shown} | 密码: {'*' * 8} | {device}")
    if missing_device:
        _say(f"警告: {missing_device} 个账号未配置设备信息")
    _say(f"路径: {paths['env_file']} | 状态目录: {paths['state_dir']} | 库: {paths['db_file']}")
    if args.json:
        _emit_json(payload)
    return 0


# ---------------------------------------------------------------------------
# 子命令：capacity（容量建议；实测值与口径都与网页 /api/scheduler/executors 同源）
# ---------------------------------------------------------------------------
def _capacity_numbers(view, signable):
    """容量三数（**与网页 `/api/scheduler/executors` 同一口径**，不另写公式）。

    - `capacity_per_executor`：有效窗口能容纳的账号数 = `schedule.capacity_accounts`
      （窗口用 `yiban.window.bounds(...).full_sec()`——网页侧同样是"完整有效窗口"，
      即"这套配置能容纳几个"，不是"今天还剩几个"）；
    - `recommended_per_executor`：部署者实测值（`YIBAN_CAPACITY_MEASURED`，由容量基准
      工具写入）× 2/3（与网页建议值、基准工具的 `--ratio` 同一余量口径）；未实测为 None；
    - `executors_needed`：⌈账号数 ÷ 建议每执行体账号数⌉（与网页同一算法）。
    """
    start, end, _invalid = window.parse_window(view)
    front, back = window.parse_edges(view)
    bounds = window.bounds({
        "sign_start": start, "sign_end": end,
        "edge_front_sec": front, "edge_back_sec": back,
    })
    gap = _cfg_int(view, "YIBAN_ACCOUNT_GAP_MAX", 10, 0, 3600)
    avg = _cfg_int(view, "YIBAN_AVG_ATTEMPT_SEC", schedule_mod.avg_attempt_sec(), 1, 300)
    measured = _cfg_int(view, "YIBAN_CAPACITY_MEASURED", 0, 0)
    recommended = None
    needed = None
    if measured > 0:
        recommended = max(1, int(measured * 2 / 3))
        needed = -(-signable // recommended)
    return {
        "window_effective_sec": bounds.full_sec(),
        "avg_attempt_sec": avg,
        "gap_sec": gap,
        "capacity_per_executor": schedule_mod.capacity_accounts(bounds.full_sec(), gap, avg),
        "measured_per_executor": measured,
        "recommended_per_executor": recommended,
        "executors_needed": needed,
    }


def _cmd_capacity(args, view, extra):
    """容量建议：默认**只读**（读 .env 实测值 + 账号数 → 建议执行体数），不联网、不写盘。

    `--measure` 转发 `scripts/loadtest/capacity_probe.py`（需 root 的隔离测试机）：
    输出与退出码原样透传，故与 `--json` 互斥（转发工具的 stdout 自成一路，不该被
    再包一层 JSON）。
    """
    if args.measure:
        if not os.path.isfile(CAPACITY_PROBE):
            return _fail("capacity", 1, [f"容量基准工具不存在: {CAPACITY_PROBE}"], False)
        cmd = [sys.executable, CAPACITY_PROBE, *extra]
        _say("转发容量基准工具（需 root / 隔离测试机；输出与退出码原样透传）: "
             + " ".join(cmd[1:]))
        return subprocess.run(cmd).returncode
    paths = _paths(view)
    # 账号数口径与网页 `_capacity_account_count` 同源；库不存在按 0（新部署没有库很正常）
    try:
        snapshot = _db_snapshot(paths["db_file"])
    except sqlite3.Error as e:
        return _fail("capacity", 1, [f"读取账号数失败（{paths['db_file']}）: {e}"], args.json,
                     accounts=0, paths=paths)
    accounts, signable = (0, 0) if snapshot is None else (snapshot[3], snapshot[4])
    if snapshot is not None:
        snapshot[0].close()
    numbers = _capacity_numbers(view, signable)
    payload = {
        "command": "capacity",
        "ok": True,
        "accounts": signable,
        "accounts_total": accounts,
        "paths": paths,
        "errors": [],
        **numbers,
    }
    _say("==== 容量建议 ====")
    _say(f"账号数（会签到的）: {signable}（未删除合计 {accounts}）")
    _say(f"有效窗口 {numbers['window_effective_sec']}s ÷（单账号 {numbers['avg_attempt_sec']}s"
         f" + 账号间隔 {numbers['gap_sec']}s）→ 单执行体可容纳 "
         f"{numbers['capacity_per_executor']} 个")
    if numbers["measured_per_executor"]:
        _say(f"本机实测单执行体容量: {numbers['measured_per_executor']}"
             f"（余量 2/3 → 建议每执行体 {numbers['recommended_per_executor']} 个）")
        _say(f"建议执行体数: {numbers['executors_needed']}（建议，不是程序上限）")
    else:
        _say("尚无本机实测容量（YIBAN_CAPACITY_MEASURED 未配置）："
             "请在隔离测试机跑 `python -m yiban.cli capacity --measure`，"
             "把实测的「单执行体容量」写入 .env 后本命令会给出建议执行体数")
    if args.json:
        _emit_json(payload)
    return 0


# ---------------------------------------------------------------------------
# 子命令：state（状态文件清理，默认 dry-run）
# ---------------------------------------------------------------------------
def _cmd_state(args, view):
    """状态文件清理：**默认只报告不删**，`--yes` 才真删（`docs/dev/cli.md` §2 第 6 条）。

    路径与保留期口径全在 `yiban/state_gc.py`（宿主 `scripts/state_cleanup.py` 与容器
    调度器共用同一份），本子命令只做"报告 / 执行"与退出码翻译：
    0 正常（没有过期文件也是 0）/ 1 保留期配置非法或状态目录不可用。
    运维日志 `cleanup.log` 仍由宿主脚本记录——CLI 只做清理本身，不重复写一份日志。
    """
    state_dir = state_gc.state_dir_from_env(view)
    log_dir = state_gc.log_dir_from_env(state_dir, view)
    retention = {"log": None, "snapshot": None}
    if not os.path.isdir(state_dir):
        return _fail("state", 1, [f"状态目录不存在: {state_dir}"], args.json,
                     dry_run=not args.yes, state_dir=state_dir, log_dir=log_dir,
                     retention_days=retention, candidates=0, removed=0, detail=[])
    try:
        retention["log"] = state_gc.retention_days("log", view)
        retention["snapshot"] = state_gc.retention_days("snapshot", view)
        if args.yes:
            removed, detail = state_gc.sweep(state_dir, log_dir, view)
            if state_gc.sweep_empty_cred_state(state_dir):
                removed += 1
                detail.append("cred-state.json（空内容）")
            candidates = removed
        else:
            candidates, detail = state_gc.plan(state_dir, log_dir, view)
            removed = 0
    except ValueError as e:
        return _fail("state", 1, [f"保留期配置非法，未清理: {e}"], args.json,
                     dry_run=not args.yes, state_dir=state_dir, log_dir=log_dir,
                     retention_days=retention, candidates=0, removed=0, detail=[])
    payload = {
        "command": "state",
        "ok": True,
        "dry_run": not args.yes,
        "state_dir": state_dir,
        "log_dir": log_dir,
        "retention_days": retention,
        "candidates": candidates,
        "removed": removed,
        "detail": detail,
    }
    verb = "已清理" if args.yes else "将清理（dry-run，加 --yes 才动手）"
    _say("==== 状态文件清理 ====")
    _say(f"状态目录 {state_dir} | 日志目录 {log_dir} | 保留 "
         f"{retention['log']} 天（快照 {retention['snapshot']} 天）")
    if candidates:
        _say(f"{verb} {candidates} 个过期文件: " + "、".join(detail[:20])
             + ("…" if len(detail) > 20 else ""))
    else:
        _say("无过期文件")
    if args.json:
        _emit_json(payload)
    return 0


# ---------------------------------------------------------------------------
# 子命令：db（状态 / 完整性 / 备份）
# ---------------------------------------------------------------------------
def _cmd_db(args, view):
    """数据库维护：默认 `--status`（只读）；`--integrity` 跑完整性检查；
    `--backup [路径]` 用 SQLite 在线备份 API 写一致性副本。

    备份**不加 `--yes` 只报告计划**（`docs/dev/cli.md` §2 第 6 条：默认不加 `--yes`
    不动手；目标已存在时尤其不该默认覆盖）。状态与完整性检查全程只读——用只读连接
    直查，绝不顺手建库或跑迁移。
    """
    db_file = _paths(view)["db_file"]
    if args.backup is not None:
        return _db_backup(args, db_file)
    mode = "integrity" if args.integrity else "status"
    try:
        snapshot = _db_snapshot(db_file)
    except sqlite3.Error as e:
        return _fail("db", 1, [f"数据库不可读（{db_file}）: {e}"], args.json,
                     mode=mode, db_file=db_file)
    if snapshot is None:
        return _fail("db", 1, [f"数据库不存在: {db_file}"], args.json,
                     mode=mode, db_file=db_file)
    conn, user_version, tables, accounts, signable = snapshot
    payload = {
        "command": "db",
        "ok": True,
        "mode": mode,
        "db_file": db_file,
        "size_bytes": os.path.getsize(db_file),
        "user_version": user_version,
        "tables": tables,
        "accounts": accounts,
        "accounts_signable": signable,
        "integrity_ok": None,
        "integrity_detail": "",
    }
    if mode == "integrity":
        rows = [str(r[0]) for r in conn.execute("PRAGMA integrity_check")]
        conn.close()
        payload["integrity_ok"] = rows == ["ok"]
        payload["integrity_detail"] = "; ".join(rows[:5])
        _say("==== 数据库完整性检查 ====")
        _say(f"库 {db_file} | user_version={user_version} | "
             f"{'ok' if payload['integrity_ok'] else '发现问题'}: {payload['integrity_detail']}")
        if args.json:
            _emit_json(payload)
        return 0 if payload["integrity_ok"] else 1
    conn.close()
    _say("==== 数据库状态 ====")
    _say(f"库 {db_file} | {payload['size_bytes']} 字节 | user_version={user_version}")
    _say(f"表 {len(tables)} 张: " + "、".join(tables))
    _say(f"账号 {accounts} 个（其中会签到的 {signable} 个）")
    if args.json:
        _emit_json(payload)
    return 0


def _db_backup(args, db_file):
    """写一致性副本：源库用只读连接，目标用 SQLite **在线备份 API**（`Connection.backup`）。

    与"复制文件"的区别：备份 API 在事务快照上拷贝，`-wal` 里已提交但未合并的帧不会
    丢，外部进程正在写也不会拷到半截（本项目是 WAL + 多进程形态，直接 copy 不安全）。
    """
    target = args.backup or (db_file + ".backup")
    # 目标==源库必须拒绝（Low-2）：用 realpath 归一后比 inode——软链/相对路径/`..`
    # 都逃不过。WAL 库下原实现"报成功但副本就是活库本身"（误导运维），非 WAL 库
    # `src.backup(dst)` 直接无限阻塞（命令挂死）。放在 --yes 之前，dry-run 也拦。
    if os.path.exists(target) and os.path.exists(db_file) and \
            os.path.samefile(os.path.realpath(target), os.path.realpath(db_file)):
        return _fail("db", 1, [f"备份目标与源库是同一个文件，已拒绝: {target}"], args.json,
                     mode="backup", db_file=db_file, backup_path=target,
                     dry_run=not args.yes)
    exists = os.path.exists(target)
    payload = {
        "command": "db",
        "ok": True,
        "mode": "backup",
        "db_file": db_file,
        "backup_path": target,
        "backup_exists": exists,
        "dry_run": not args.yes,
        "size_bytes": None,
        "user_version": None,
    }
    if not args.yes:
        _say("==== 数据库备份（计划）====")
        _say(f"源 {db_file} → 目标 {target}"
             + ("（目标已存在，--yes 将覆盖）" if exists else "（目标不存在）"))
        _say("未加 --yes：只报告计划，未写盘")
        if args.json:
            _emit_json(payload)
        return 0
    try:
        src = _open_ro(db_file)
        if src is None:
            return _fail("db", 1, [f"数据库不存在: {db_file}"], args.json, mode="backup",
                         db_file=db_file, backup_path=target, dry_run=False)
        try:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
                payload["user_version"] = _read_user_version(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error as e:
        return _fail("db", 1, [f"备份失败（{db_file} → {target}）: {e}"], args.json,
                     mode="backup", db_file=db_file, backup_path=target, dry_run=False)
    payload["size_bytes"] = os.path.getsize(target)
    _say("==== 数据库备份 ====")
    _say(f"已写入一致性副本: {target}（{payload['size_bytes']} 字节，"
         f"user_version={payload['user_version']}）")
    if args.json:
        _emit_json(payload)
    return 0


# ---------------------------------------------------------------------------
# 子命令：version
# ---------------------------------------------------------------------------
def _cmd_version(args, view):
    """打印本项目版本（`yiban.__version__` 是唯一来源）+ Python 版本 + 库 schema 版本。

    库不可用时 `user_version` 报 null 并**照常返回 0**：查版本不该因为没建库而失败。
    """
    payload = {
        "command": "version",
        "version": RELEASE_VERSION,
        "python": "%d.%d.%d" % sys.version_info[:3],
        "user_version": None,
    }
    try:
        conn = _open_ro(_paths(view)["db_file"])
    except sqlite3.Error:
        conn = None
    if conn is not None:
        try:
            payload["user_version"] = _read_user_version(conn)
        except sqlite3.Error:
            payload["user_version"] = None
        finally:
            conn.close()
    if args.json:
        _emit_json(payload)
    else:
        _say(f"易班自动签到 v{payload['version']}（Python {payload['python']}，"
             f"库 schema user_version={payload['user_version']}）")
    return 0


# ---------------------------------------------------------------------------
# 参数解析与分发
# ---------------------------------------------------------------------------
def _build_parser():
    """构造根解析器与子解析器（每个子命令都有 `--help`；根解析器带用法错误处理）。"""
    parser = argparse.ArgumentParser(
        prog="python -m yiban.cli",
        description="易班自动签到统一入口（agent 侧；契约见 docs/dev/cli.md）",
        epilog=("stdout 只放结果（--json 时是一整行 JSON 对象），人类可读汇总走 stderr；"
                "本命令不读 stdin、不做交互确认。"),
    )
    subs = parser.add_subparsers(
        dest="command", metavar="{sign,probe,config,capacity,state,db,version}")
    sub_parsers = {}

    def _sub(name, **kwargs):
        """注册子命令并记账（分发阶段要用它打"该子命令"的用法错误）。"""
        sub = subs.add_parser(name, **kwargs)
        sub_parsers[name] = sub
        return sub

    p = _sub(
        "sign", help="执行一轮签到（选项原样透传给引擎）",
        description=("执行一轮签到。以下引擎选项原样透传：--workers N（多执行体）、"
                     "--fallback（兜底常驻）、--only 手机号（只签指定账号）、"
                     "--second-run-check（补签轮判定，退出码 10）。退出码语义见 "
                     "docs/dev/cli.md §3。"),
    )
    p.add_argument("--json", action="store_true",
                   help="结果打成一整行 JSON 写 stdout（字段 command/exit_code）")

    p = _sub(
        "probe", help="只读健康检查（引擎 --probe 语义）",
        description=("对全部账号做只读健康检查（登录 + 拉任务，不提交签到）。"
                     "是否真跑受 .env 的探针开关/频率、一键暂停与周末门约束，"
                     "与引擎 --probe 完全同一路径。"),
    )
    p.add_argument("--json", action="store_true",
                   help="结果打成一整行 JSON 写 stdout（字段 command/exit_code）")

    p = _sub(
        "config", help="配置检查（脱敏、不联网）",
        description="检查账号配置并脱敏打印，不发起任何网络请求；含本次解析到的路径。",
    )
    p.add_argument("--json", action="store_true", help="结果打成一整行 JSON 写 stdout")

    p = _sub(
        "capacity", help="容量建议（实测值 → 建议执行体数）",
        description=("默认只读：按 .env 的实测容量（YIBAN_CAPACITY_MEASURED）与账号数，"
                     "给出建议每执行体账号数与执行体数；口径与网页"
                     " /api/scheduler/executors 一致，未实测就不编数字。"),
    )
    p.add_argument("--json", action="store_true", help="结果打成一整行 JSON 写 stdout")
    p.add_argument("--measure", action="store_true",
                   help=("转发 scripts/loadtest/capacity_probe.py 现场实测（需 root 的"
                         "隔离测试机；输出与退出码原样透传，故与 --json 互斥）"))

    p = _sub(
        "state", help="状态文件清理（默认 dry-run）",
        description=("清理过期的按日状态文件（策略在 yiban/state_gc.py，与宿主清理脚本、"
                     "容器调度共用）。默认只报告不删；--yes 才真删。"),
    )
    p.add_argument("--yes", action="store_true", help="真的删除（默认只报告）")
    p.add_argument("--dry-run", action="store_true", help="只报告不删（默认行为，显式声明用）")
    p.add_argument("--json", action="store_true", help="结果打成一整行 JSON 写 stdout")

    p = _sub(
        "db", help="数据库维护（状态 / 完整性 / 备份）",
        description=("默认 --status（只读：user_version、表清单、账号数、文件大小）；"
                     "--integrity 跑 PRAGMA integrity_check；--backup 写一致性副本"
                     "（在线备份 API，不加 --yes 只报告计划）。全程只读连接，不建库、不迁移。"),
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="只读状态（默认）")
    group.add_argument("--integrity", action="store_true", help="SQLite 完整性检查")
    group.add_argument("--backup", nargs="?", const="", default=None, metavar="路径",
                       help="写一致性副本（默认 <库文件>.backup；不加 --yes 只报告计划）")
    p.add_argument("--yes", action="store_true", help="备份时真的写盘（默认只报告）")
    p.add_argument("--dry-run", action="store_true", help="只报告不写盘（默认行为，显式声明用）")
    p.add_argument("--json", action="store_true", help="结果打成一整行 JSON 写 stdout")

    p = _sub(
        "version", help="打印版本（本项目 / Python / 库 schema）",
        description="打印本项目版本（yiban.__version__ 是唯一来源）、Python 版本与库 user_version。",
    )
    p.add_argument("--json", action="store_true", help="结果打成一整行 JSON 写 stdout")

    return parser, sub_parsers


def _dispatch(args, extra, subs):
    """按子命令分发（返回值即退出码；用法错误走 argparse 的 2）。"""
    cmd = args.command
    if cmd in _PASSTHROUGH:
        handler = _cmd_sign if cmd == "sign" else _cmd_probe
        return handler(args.json, extra)
    if extra and not (cmd == "capacity" and args.measure):
        # 多余参数只在两处合法：sign/probe（原样透传给引擎，见上）与
        # `capacity --measure`（透传给容量基准工具）。其余是用法错误 → stderr 用法 + 退出码 2
        subs[cmd].error("无法识别的参数: " + " ".join(extra))
    # `--dry-run` / `--yes` 只有 state 与 db 定义（其余子命令没有这两个开关）
    if getattr(args, "dry_run", False) and getattr(args, "yes", False):
        subs[cmd].error("--dry-run 与 --yes 互斥（默认就是 dry-run）")
    if cmd == "capacity" and args.measure and args.json:
        subs[cmd].error("--measure 与 --json 不能同时使用（转发的工具自成一路输出）")
    view = _env_view()
    handlers = {
        "config": _cmd_config,
        "capacity": _cmd_capacity,
        "state": _cmd_state,
        "db": _cmd_db,
        "version": _cmd_version,
    }
    handler = handlers[cmd]
    if cmd == "capacity":
        return handler(args, view, extra)
    return handler(args, view)


def main(argv=None) -> int:
    """执行子命令并**返回**退出码（调用方决定是否 `sys.exit`；本函数不抛 SystemExit）。

    空子命令与未知子命令都会在 stderr 打用法并返回 2（`docs/dev/cli.md` §2 第 1/4 条），
    全程不读 stdin。`--help` 由 argparse 打印后返回 0。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    # 进程 umask 077——与引擎入口同口径：本进程创建的文件（数据库备份副本等）
    # 创建即 0600（Windows 无实际效果，忽略）。
    os.umask(0o077)
    parser, subs = _build_parser()
    try:
        args, extra = parser.parse_known_args(argv)
        if args.command is None:
            _say(USAGE)
            return 2
        return _dispatch(args, extra, subs)
    except SystemExit as e:
        # argparse 的 --help（0）与用法错误（2）都以此形式退出：转成返回值，
        # 使 `main(argv)` 对调用方始终是"返回码"而非异常。
        code = getattr(e, "code", 2)
        return code if isinstance(code, int) else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
