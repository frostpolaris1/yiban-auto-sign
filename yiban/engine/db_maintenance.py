# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
`db` 子命令的实现：只读状态 / 完整性检查与在线备份（覆盖守卫）。

**为什么单独成模块**：`yiban/cli.py` 是命令行门面的唯一宿主，已贴近体量门上限；`db` 维护
这一族（快照读取、备份）服务的是"库文件本身的运维动作"这一独立变更轴，且被
`capacity` / `version` 子命令复用（只读快照），故从入口模块外移（体量门登记的下一步拆法）。

**归属**
`yiban.engine` 的命令行子命令实现层（同族先例：`config_check` 承担 `config` 的输出装配）；
SQLite 只读打开、账号只读装载与目标指纹分别复用 `yiban.store` 的
`connection.open_readonly` / `accounts.load_accounts_readonly` / `purge_guard`，本模块不另写
第二套。

**复用**
`db_snapshot` / `open_ro` / `read_user_version` 同时供 `capacity` / `version` 子命令读取
只读库快照；`cmd_db` 是 `db` 子命令的唯一入口。

**通信**
输入：argparse 解析出的 `db` 命名空间（`--status` / `--integrity` / `--backup` / `--yes` /
`--dry-run` / `--force` / `--json`）与 `_paths` 解析出的路径 dict。
输出：stdout 一行 JSON（`--json`）或 stderr 人类可读汇总；退出码由 `cmd_db` 返回（0 / 1，
含义见 `docs/dev/cli.md` §3）。
调用谁：`yiban.store.db`（只读打开与账号计数）、`yiban.engine.cli_support`（输出收口）。
谁调用：`yiban/cli.py` 的 `_dispatch`。
"""
import os
import sqlite3

from yiban.engine.cli_support import _emit_json, _fail, _say
from yiban.store import accounts as store_accounts
from yiban.store import db as store_db


# ---------------------------------------------------------------------------
# 只读快照（status / integrity，以及 capacity / version 复用）
# ---------------------------------------------------------------------------
def open_ro(db_file):
    """只读打开 SQLite；文件不存在返回 None（判定统一收在 `store.connection.open_readonly`）。

    维护类子命令**不得**顺手建库或跑迁移（`db.get_conn()` 会 `init_db()` 建表 + 迁移），
    故这里直连并对文件缺失显式返回 None，由调用方决定是"报 0"还是"响亮失败"。
    """
    return store_db.open_readonly(db_file)


def account_counts(conn):
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


def read_user_version(conn):
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def db_snapshot(db_file):
    """→ (conn, user_version, 表清单, 未删除账号数, 会签到账号数)；库文件不存在返回 None。

    sqlite3.Error 原样抛出，由调用方按"响亮失败"处理（不静默报 0——那会让人以为
    库是空的）。调用方负责关闭返回的连接。
    """
    conn = open_ro(db_file)
    if conn is None:
        return None
    try:
        user_version = read_user_version(conn)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        accounts, signable = account_counts(conn)
    except sqlite3.Error:
        conn.close()
        raise
    return conn, user_version, tables, accounts, signable


# ---------------------------------------------------------------------------
# db 子命令入口与分发
# ---------------------------------------------------------------------------
def cmd_db(args, paths):
    """数据库维护：默认 `--status`（只读）；`--integrity` 跑完整性检查；
    `--backup [路径]` 用 SQLite 在线备份 API 写一致性副本；`--restore [路径]` 从该副本恢复。

    只读路径（status / integrity）全程用只读连接直查，绝不顺手建库或跑迁移。
    备份**不加 `--yes` 只报告计划**（`docs/dev/cli.md` §2 第 6 条）；目标已存在时还需
    `--force`，不得静默顶掉上一份副本。
    """
    db_file = paths["db_file"]
    if args.backup is not None:
        return _db_backup(args, db_file)
    mode = "integrity" if args.integrity else "status"
    try:
        snapshot = db_snapshot(db_file)
    except sqlite3.Error as e:
        return _fail("db", 1, [f"数据库不可读（{db_file}）: {e}"], args.json,
                     error_kind="runtime_error", mode=mode, db_file=db_file)
    if snapshot is None:
        return _fail("db", 1, [f"数据库不存在: {db_file}"], args.json,
                     error_kind="runtime_error", mode=mode, db_file=db_file)
    conn, user_version, tables, accounts, signable = snapshot
    payload = {
        "command": "db",
        "ok": True,
        "exit_code": 0,
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
        payload["exit_code"] = 0 if payload["integrity_ok"] else 1
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


# ---------------------------------------------------------------------------
# 备份（覆盖守卫）
# ---------------------------------------------------------------------------
def _db_backup(args, db_file):
    """写一致性副本：源库用只读连接，目标用 SQLite **在线备份 API**（`Connection.backup`）。

    与"复制文件"的区别：备份 API 在事务快照上拷贝，`-wal` 里已提交但未合并的帧不会
    丢，外部进程正在写也不会拷到半截（本项目是 WAL + 多进程形态，直接 copy 不安全）。

    目标已存在时**不得静默覆盖上一份**（MF-60）：不加 `--force` 的 `--yes` 直接拒绝
    （非 0、零写入），dry-run 只报告"需 --force"。选 `--force` 而非自动时间戳命名，
    是为了不改 `backup_path` 的字段语义（调用方按它取回副本）又不让旧副本被无声顶掉。
    """
    target = args.backup or (db_file + ".backup")
    # 目标==源库必须拒绝：用 realpath 归一后比 inode——软链/相对路径/`..`
    # 都逃不过。WAL 库下原实现"报成功但副本就是活库本身"（误导运维），非 WAL 库
    # `src.backup(dst)` 直接无限阻塞（命令挂死）。放在 --yes 之前，dry-run 也拦。
    if os.path.exists(target) and os.path.exists(db_file) and \
            os.path.samefile(os.path.realpath(target), os.path.realpath(db_file)):
        return _fail("db", 1, [f"备份目标与源库是同一个文件，已拒绝: {target}"], args.json,
                     error_kind="runtime_error",
                     mode="backup", db_file=db_file, backup_path=target,
                     dry_run=not args.yes)
    exists = os.path.exists(target)
    force = bool(getattr(args, "force", False))
    meta = {
        "mode": "backup",
        "db_file": db_file,
        "backup_path": target,
        "backup_exists": exists,
        "overwrite_allowed": (not exists) or force,
        "dry_run": not args.yes,
        "size_bytes": None,
        "user_version": None,
    }
    payload = {"command": "db", "ok": True, "exit_code": 0, **meta}
    if exists and not force:
        if not args.yes:
            _say("==== 数据库备份（计划）====")
            _say(f"源 {db_file} → 目标 {target}")
            _say("目标已存在：不加 --yes 只报告计划；真要覆盖还得加 --force"
                 "（拒绝静默顶掉上一份副本）")
            if args.json:
                _emit_json(payload)
            return 0
        return _fail("db", 1, [
            f"备份目标已存在: {target}",
            "拒绝静默覆盖上一份副本：确认要顶掉它请加 --force",
        ], args.json, error_kind="runtime_error", **meta)
    if not args.yes:
        _say("==== 数据库备份（计划）====")
        _say(f"源 {db_file} → 目标 {target}"
             + ("（目标不存在）" if not exists else "（--force 覆盖）"))
        _say("未加 --yes：只报告计划，未写盘")
        if args.json:
            _emit_json(payload)
        return 0
    try:
        src = open_ro(db_file)
        if src is None:
            return _fail("db", 1, [f"数据库不存在: {db_file}"], args.json,
                         error_kind="runtime_error", **{**meta, "dry_run": False})
        try:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
                payload["user_version"] = read_user_version(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error as e:
        return _fail("db", 1, [f"备份失败（{db_file} → {target}）: {e}"], args.json,
                     error_kind="runtime_error", **{**meta, "dry_run": False})
    payload["size_bytes"] = os.path.getsize(target)
    _say("==== 数据库备份 ====")
    _say(f"已写入一致性副本: {target}（{payload['size_bytes']} 字节，"
         f"user_version={payload['user_version']}）")
    if args.json:
        _emit_json(payload)
    return 0
