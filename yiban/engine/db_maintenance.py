# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
`db` 子命令的实现：只读状态 / 完整性检查、在线备份（覆盖守卫）、从副本恢复（恢复前校验
+ 覆盖前副本 + 指纹确认）。

**为什么单独成模块**：`yiban/cli.py` 是命令行门面的唯一宿主，已贴近体量门上限；`db` 维护
这一族（快照读取、备份、恢复）服务的是"库文件本身的运维动作"这一独立变更轴，且被
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
输入：argparse 解析出的 `db` 命名空间（`--status` / `--integrity` / `--backup` /
`--restore` / `--yes` / `--dry-run` / `--force` / `--fingerprint` / `--json`）与 `_paths`
解析出的路径 dict。
输出：stdout 一行 JSON（`--json`）或 stderr 人类可读汇总；退出码由 `cmd_db` 返回（0 / 1 /
2，含义见 `docs/dev/cli.md` §3）。
调用谁：`yiban.store.db`（只读打开、账号只读装载）、`yiban.store.purge_guard`（内容指纹与
回显比对）、`yiban.engine.cli_support`（输出收口）。
谁调用：`yiban/cli.py` 的 `_dispatch`。
"""
import datetime
import os
import sqlite3

from yiban.engine.cli_support import _emit_json, _fail, _say
from yiban.store import accounts as store_accounts
from yiban.store import db as store_db
from yiban.store import purge_guard


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
    破坏性路径（backup / restore）**不加 `--yes` 只报告计划**（`docs/dev/cli.md` §2
    第 6 条）；backup 目标已存在时还需 `--force`（不得静默顶掉上一份副本）；restore 会
    覆盖当前库，`--yes` 时必须逐字回显目标指纹，且恢复前先留一份覆盖前副本。
    """
    db_file = paths["db_file"]
    if args.backup is not None:
        return _db_backup(args, db_file)
    if args.restore is not None:
        return _db_restore(args, paths)
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


# ---------------------------------------------------------------------------
# 恢复（破坏性：校验 + 指纹确认 + 覆盖前副本）
# ---------------------------------------------------------------------------
def _same_file(a, b):
    """两路径是否指向同一文件（realpath 归一，软链/相对路径/`..` 都逃不过）。"""
    return os.path.samefile(os.path.realpath(a), os.path.realpath(b))


def validate_restorable(backup, env_file=None):
    """校验备份**可读且可恢复**：→ `(ok, detail, user_version)`，全程只读、不建文件。

    三层判据缺一不可（只查"文件存在"会把一个空壳/半截副本当可用件）：
    1. 只读打开 + `PRAGMA integrity_check` 全 ok（文件不是坏掉的半截副本）；
    2. 结构是本项目的库（有 `accounts` 表，而非随便一个 SQLite 文件）；
    3. 账号密文能用**当前**密钥解开（`load_accounts_readonly`，与运行期同一份 AES-GCM
       口径）——密钥不匹配/密文损坏的副本恢复出来是打不开账号的库，必须在覆盖前拦住。
    """
    conn = open_ro(backup)
    if conn is None:
        return False, "备份不可读（不是 SQLite 库文件）", None
    try:
        rows = [str(r[0]) for r in conn.execute("PRAGMA integrity_check")]
        if rows != ["ok"]:
            return False, "完整性检查未通过: " + "; ".join(rows[:5]), None
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "accounts" not in tables:
            return False, "库结构不是本项目（缺 accounts 表）", None
        user_version = read_user_version(conn)
    except sqlite3.Error as e:
        return False, f"备份读取失败: {e}", None
    finally:
        conn.close()
    try:
        store_db.load_accounts_readonly(backup, env_file=env_file)
    except (RuntimeError, ValueError) as e:
        return False, f"账号密文不可解密（{e}）", None
    return True, f"user_version={user_version}，完整性 ok，账号可解密", user_version


def _db_content_fingerprint(db_file):
    """库内容指纹（表行数 + 文件规模），**不新建任何文件**。

    不用 `purge_guard.db_content_fingerprint`：它用 `mode=ro`（非 immutable）打开 WAL 库，
    会在目录里留下 `-shm`/`-wal`——`db --restore` 的 dry-run 必须零新文件。这里改走
    `open_ro`（静默库用 `immutable=1`，SQLite 不会建 sidecar），内容口径与
    `purge_guard.PURGE_TABLES` 保持一致。
    """
    abs_path = os.path.abspath(db_file)
    size = os.path.getsize(abs_path) if os.path.exists(abs_path) else 0
    counts = {t: 0 for t in purge_guard.PURGE_TABLES}
    conn = open_ro(db_file)
    if conn is not None:
        try:
            for table in purge_guard.PURGE_TABLES:
                try:
                    counts[table] = int(
                        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                except sqlite3.Error:
                    counts[table] = 0
        finally:
            conn.close()
    rows = ",".join("%s=%d" % (t, counts[t]) for t in purge_guard.PURGE_TABLES)
    return purge_guard.content_fingerprint("db", [abs_path, size, rows])


def restore_fingerprint(db_file, backup):
    """恢复的目标指纹：由**当前库与备份副本的内容**共同派生（不是路径字符串比较）。

    误指生产库或拿错备份时，两侧行数/规模差异会让指纹明显不同；`--yes` 必须逐字回显它
    （与 `state --yes` 同款 `--fingerprint` 契约）。库缺失时按"库缺失 + 绝对路径"参与，
    仍然可区分。
    """
    parts = [os.path.abspath(db_file), _db_content_fingerprint(db_file),
             os.path.abspath(backup), _db_content_fingerprint(backup)]
    return purge_guard.content_fingerprint("db-restore", parts)


def _pre_restore_path(db_file):
    """覆盖前副本路径：`<库文件>.pre-restore-<本地时间戳>`；同秒重名自动加后缀。"""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{db_file}.pre-restore-{stamp}"
    path = base
    seq = 1
    while os.path.exists(path):
        path = f"{base}-{seq}"
        seq += 1
    return path


def _snapshot_copy(src_path, dst_path):
    """把 `src_path` 的**一致性快照**写到 `dst_path`（在线备份 API，不是文件复制）。"""
    src = open_ro(src_path)
    if src is None:
        raise sqlite3.OperationalError(f"源库不可读: {src_path}")
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _db_restore(args, paths):
    """从 `db --backup` 写出的 SQLite 副本恢复：**默认只报告**，`--yes` 才动手。

    守卫顺序（任一不满足都零写入、非 0 退出）：
    1. 备份存在、不是当前库自己、且 `validate_restorable` 通过（可读 + 完整 + 本项目结构
       + 账号可解密）；
    2. `--yes` 必须逐字回显目标指纹（`--fingerprint`，由 dry-run 打印）；
    3. 覆盖当前库前**自动留一份覆盖前副本**（时间戳命名）——恢复本身不可逆，留了副本
       才谈得上"恢复错了还能回去"；副本写不出来就拒绝恢复（fail-closed）；
    4. 覆盖后再校验结果（integrity ok 且 `user_version` 与备份一致），不一致即报失败并
       把覆盖前副本路径告诉运维。
    """
    db_file = paths["db_file"]
    env_file = paths["env_file"]
    backup = args.restore or (db_file + ".backup")
    plan = {
        "mode": "restore",
        "db_file": db_file,
        "restore_from": backup,
        "dry_run": not args.yes,
    }
    if not os.path.isfile(backup):
        return _fail("db", 1, [f"备份文件不存在: {backup}"], args.json,
                     error_kind="runtime_error", **plan)
    if os.path.exists(db_file) and _same_file(backup, db_file):
        return _fail("db", 1, [f"恢复源与目标库是同一个文件，已拒绝: {db_file}"], args.json,
                     error_kind="runtime_error", **plan)
    ok, detail, user_version = validate_restorable(backup, env_file=env_file)
    if not ok:
        return _fail("db", 1, [f"备份不可恢复（{backup}）: {detail}"], args.json,
                     error_kind="runtime_error", **plan)
    fingerprint = restore_fingerprint(db_file, backup)
    meta = {
        **plan,
        "fingerprint": fingerprint,
        "backup_user_version": user_version,
        "backup_size_bytes": os.path.getsize(backup),
        "pre_restore_copy": None,
        "integrity_ok": None,
    }
    if not args.yes:
        _say("==== 数据库恢复（计划）====")
        _say(f"源副本 {backup} → 目标库 {db_file}")
        _say(f"备份：{detail}")
        _say("未加 --yes：只报告计划，未改动当前库")
        _say(f"目标指纹（--yes 时需逐字回显）: {fingerprint}")
        if args.json:
            _emit_json({"command": "db", "ok": True, "exit_code": 0, **meta})
        return 0
    if not purge_guard.confirmation_ok(fingerprint, getattr(args, "fingerprint", "")):
        return _fail("db", 2, [
            "db --restore --yes 会覆盖当前库：请先跑 `db --restore --dry-run --json` "
            "拿到目标指纹，再以 `--yes --fingerprint <指纹>` 逐字回显确认",
            f"目标指纹: {fingerprint}",
        ], args.json, error_kind="confirmation_required", **meta)
    if os.path.exists(db_file):
        pre = _pre_restore_path(db_file)
        try:
            _snapshot_copy(db_file, pre)
        except sqlite3.Error as e:
            return _fail("db", 1, [
                f"覆盖前副本写入失败（{db_file} → {pre}）: {e}",
                "未改动当前库；恢复是覆盖操作，留不下可回退副本就不动手",
            ], args.json, error_kind="runtime_error", **meta)
        meta["pre_restore_copy"] = pre
    try:
        _snapshot_copy(backup, db_file)
    except sqlite3.Error as e:
        return _fail("db", 1, [f"恢复写入失败（{backup} → {db_file}）: {e}"], args.json,
                     error_kind="runtime_error", **meta)
    ok_after, detail_after, _uv = validate_restorable(db_file, env_file=env_file)
    meta["integrity_ok"] = bool(ok_after)
    if not ok_after:
        return _fail("db", 1, [
            f"恢复结果校验失败（{db_file}）: {detail_after}",
            (f"覆盖前副本: {meta['pre_restore_copy']}" if meta["pre_restore_copy"]
             else "无覆盖前副本（目标库此前不存在）"),
        ], args.json, error_kind="runtime_error", **meta)
    _say("==== 数据库恢复 ====")
    _say(f"已从副本恢复: {backup} → {db_file}（user_version={user_version}）")
    _say(f"覆盖前副本: {meta['pre_restore_copy'] or '（目标库此前不存在，无）'}")
    if args.json:
        _emit_json({"command": "db", "ok": True, "exit_code": 0, **meta})
    return 0
