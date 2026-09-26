#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""造 N 个测试账号 + 写测试 .env（仅在测试机/本地沙箱使用）。

- 账号写入独立测试库（SQLite），敏感字段用**测试环境自造**的 YIBAN_ACCOUNTS_KEY
  加密（密钥由 db 层生成进测试 .env，绝不复用生产密钥）；
- 同时把窗口 / 间隔 / 调度模式等压测参数写进测试 .env，供 signin.py 读取；
- 幂等：同一 --n 重复执行结果一致（先清空账号相关表再重建）。
- **清空/改写目标库是不可逆动作**：必须回显由目标库内容派生的指纹（`--fingerprint`）
  加 `--yes` 才放行；库内已有非压测账号（owner 不匹配 `@mock.invalid`）视为误指
  生产库并拒绝；每次真实清空写一条审计留痕，留痕写不进去就放弃清空（fail-closed）。

用法：
  python3 seed_accounts.py --n 240 --db /opt/yiban-loadtest/data/yiban.db \\
      --env /opt/yiban-loadtest/test.env --dry-run          # 先看将清什么
  python3 seed_accounts.py --n 240 --db ... --env ... --yes --fingerprint <上面打印的指纹>
  python3 seed_accounts.py --n 240 --db ... --env ... \\
      --gap 10 --window-start 06:30 --window-end 07:50
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import db  # noqa: E402  （scripts/db.py 兼容壳；导入即补仓库根到 sys.path）

from yiban.infra import (  # noqa: E402
    env_io,
    env_lock,
)
from yiban.store import purge_guard  # noqa: E402

# 需要在测试 .env 中落地的压测参数（键 -> 说明），值由参数决定
_ENV_HEADER = "# 压测专用 .env（由 scripts/loadtest/seed_accounts.py 生成，绝不复用生产密钥）\n"

# 清空顺序（初始化时 foreign_keys=OFF，顺序只影响可读性）
_WIPE_ORDER = ("accounts", "users", "session_cache", "sign_events", "time_prefs")

# 压测账号归属域：判"这个库像不像压测库"（误指生产库的最后一道内容判据）
_LOADTEST_OWNER_SUFFIX = "@mock.invalid"


def _looks_like_loadtest_db(db_path):
    """→ 库内非空 accounts 是否**全部**是压测账号（没有 accounts 表/空表都算像）。

    按库内容判定，不看路径：生产库的账号 owner 是真实邮箱，一眼可分。读不到结构
    （旧 schema）也不因此放行——读一次 accounts 失败即视为"不像"。
    """
    if not os.path.exists(db_path):
        return True
    try:
        conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return False
    try:
        try:
            owners = [r[0] or "" for r in conn.execute("SELECT owner FROM accounts")]
        except sqlite3.Error:
            return False
    finally:
        conn.close()
    return all(o.endswith(_LOADTEST_OWNER_SUFFIX) for o in owners)


def upsert_env(path, updates):
    """按行更新/追加 KEY=VALUE，保留其它键（含 db 生成的密钥）。

    写入并入全项目唯一的 `.env` 行模型：键/值走 `env_io.validate_env_updates` 同一套
    校验（禁换行族、键名白名单、值长度上限），折叠同键旧行、保留注释与其余行，原子
    0600 替换（`env_io.write_env_keys`，落盘即 0600）。`delete_empty=False` 保持本工具
    原语义——空值仍写一行 `KEY=`，不删键（压测参数全是有值写入）。本工具是单进程
    离线造数、无并发写方，但仍按 `write_env_keys` 的调用契约自持写锁。
    """
    with env_lock.env_write_lock(path):
        env_io.write_env_keys(path, updates)


def seed(n, db_path, env_path, wipe=True, fingerprint=""):
    """建库/建账号；返回实际账号数（清空留痕写入失败时返回 None，调用方据此判失败）。"""
    os.environ["YIBAN_ENV_FILE"] = env_path
    os.environ["YIBAN_DB_FILE"] = db_path
    # 密钥来源显式指定（env_file 传入），允许 db 生成测试密钥写进该文件
    db.init_db(db_path, env_file=env_path, cleanup=False, migrate=True)
    conn = db.get_conn()
    if wipe:
        # 留痕先于不可逆删除：audit_logs 不在清空集内，这一行清空后仍在。写不进去
        # 就不许清（fail-closed）——否则会"删了但没留痕"。
        if not db.audit("seed-accounts", "loadtest_seed", fingerprint,
                        f"清空压测库 5 表并重建 {n} 个账号"):
            return None
        for table in _WIPE_ORDER:
            with contextlib.suppress(Exception):
                conn.execute(f"DELETE FROM {table}")  # 表缺失/结构差异时忽略
        conn.commit()
    for i in range(n):
        email = f"loadtest{i:05d}@mock.invalid"
        with contextlib.suppress(Exception):
            db.create_user(email, "mock-hash", role="user")  # 用户已存在不阻断
        phone = f"13{100000000 + i:09d}"  # 13100000000 起，11 位、唯一
        db.add_account({
            "name": f"loadtest{i:05d}",
            "phone": phone,
            "password": f"mock-pass-{i}",
            "phone_model": "MockPhone",
            "phone_code": "",
            "owner": email,
            "status": "active",
            "reject_reason": "",
        })
    return len(db.load_accounts())


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="seed_accounts.py",
        description="造 N 个测试账号并写测试 .env（测试机/沙箱专用）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--n", type=int, required=True, help="账号数量")
    ap.add_argument("--db", required=True, help="测试库路径（SQLite）")
    ap.add_argument("--env", required=True, help="测试 .env 路径（密钥与参数落地处）")
    ap.add_argument("--state-dir", default="", help="YIBAN_STATE_DIR（缺省 <db目录>/state）")
    ap.add_argument("--log-file", default="", help="YIBAN_LOG_FILE（缺省 <db目录>/logs/sign.log）")
    ap.add_argument("--window-start", default="06:30", help="签到窗口起点 HH:MM")
    ap.add_argument("--window-end", default="07:50", help="签到窗口终点 HH:MM")
    ap.add_argument("--gap", type=int, default=10, help="账号间隔 YIBAN_ACCOUNT_GAP_MAX（秒）")
    ap.add_argument("--order", default="sequence", choices=("sequence", "random"),
                    help="调度顺序 YIBAN_SIGN_ORDER")
    ap.add_argument("--dist", default="uniform", choices=("uniform", "normal"),
                    help="调度分布 YIBAN_SIGN_DIST")
    ap.add_argument("--avg-attempt-sec", type=int, default=2,
                    help="YIBAN_AVG_ATTEMPT_SEC（容量公式用；拟真档建议按实测填）")
    ap.add_argument("--no-wipe", action="store_true", help="不清空已有账号表（追加造数）")
    ap.add_argument("--yes", action="store_true",
                    help="确认清空/改写目标库（须与 --fingerprint 同时给出）")
    ap.add_argument("--fingerprint", default="",
                    help="回显目标库指纹（由库内容派生；先 --dry-run 查看）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将清空的表与行数，不做任何改动")
    args = ap.parse_args(argv)

    fingerprint, summary = purge_guard.db_content_fingerprint(args.db)
    counts = purge_guard.table_counts(args.db)
    print(f"目标库指纹: {fingerprint}")
    for line in summary:
        print(f"  {line}")

    if args.dry_run:
        print("dry-run：以下表将被清空并重建压测账号（未做任何改动）：")
        for table in _WIPE_ORDER:
            print(f"  {table}: {counts.get(table, 0)} 行")
        print(f"  然后重建 {args.n} 个压测账号")
        return 0

    if not (args.yes and purge_guard.confirmation_ok(fingerprint, args.fingerprint)):
        print("拒绝执行：造数会清空/改写目标库，需同时提供 --yes 与 --fingerprint"
              "（逐字回显上面打印的指纹）。", file=sys.stderr)
        print(f"  目标库指纹: {fingerprint}", file=sys.stderr)
        return 2
    if not args.no_wipe and not _looks_like_loadtest_db(args.db):
        print(f"拒绝执行：目标库已有非压测账号（owner 不以 {_LOADTEST_OWNER_SUFFIX} 结尾），"
              f"疑似误指生产库——拒绝清空。", file=sys.stderr)
        return 2

    for p in (args.db, args.env):
        d = os.path.dirname(os.path.abspath(p))
        if d:
            os.makedirs(d, exist_ok=True)
    if not os.path.exists(args.env):
        with open(args.env, "w", encoding="utf-8") as f:
            f.write(_ENV_HEADER)

    n = seed(args.n, args.db, args.env, wipe=not args.no_wipe, fingerprint=fingerprint)
    if n is None:
        print("拒绝执行：清库留痕写入失败，按 fail-closed 放弃清空（未删除任何行）。",
              file=sys.stderr)
        return 1

    db_abs = os.path.abspath(args.db)
    state_dir = args.state_dir or os.path.join(os.path.dirname(db_abs), "state")
    log_file = args.log_file or os.path.join(os.path.dirname(db_abs), "logs", "sign.log")
    os.makedirs(state_dir, exist_ok=True)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    updates = {
        "YIBAN_ENV_FILE": os.path.abspath(args.env),
        "YIBAN_DB_FILE": db_abs,
        "YIBAN_STATE_DIR": state_dir,
        "YIBAN_LOG_FILE": log_file,
        "YIBAN_SIGN_START": args.window_start,
        "YIBAN_SIGN_END": args.window_end,
        "YIBAN_ACCOUNT_GAP_MAX": str(args.gap),
        "YIBAN_START_DELAY_MAX": "0",
        "YIBAN_MIN_EXEC_GAP": "1",
        "YIBAN_EXEC_GAP_MIN": "0",
        "YIBAN_WINDOW_EDGE_FRONT_SEC": "0",
        "YIBAN_WINDOW_EDGE_BACK_SEC": "0",
        "YIBAN_SIGN_ORDER": args.order,
        "YIBAN_SIGN_DIST": args.dist,
        "YIBAN_AVG_ATTEMPT_SEC": str(args.avg_attempt_sec),
        "YIBAN_SUNDAY_SIGN": "1",
        "YIBAN_SATURDAY_SIGN": "1",
        "YIBAN_GLOBAL_PAUSE": "0",
        "YIBAN_NOTIFY_URL": "",
        "YIBAN_PROBE_ENABLE": "0",
        "YIBAN_ACCOUNT_VERIFY": "0",
        "YIBAN_LEGACY_LOGIN": "0",
    }
    upsert_env(args.env, updates)

    print(f"OK: DB={db_abs} accounts={n}")
    print(f"OK: ENV={os.path.abspath(args.env)} "
          f"window={args.window_start}-{args.window_end} gap={args.gap} "
          f"order={args.order}/{args.dist}")
    print(f"OK: STATE_DIR={state_dir} LOG_FILE={log_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
