#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""造 N 个测试账号 + 写测试 .env（仅在测试机/本地沙箱使用）。

- 账号写入独立测试库（SQLite），敏感字段用**测试环境自造**的 YIBAN_ACCOUNTS_KEY
  加密（密钥由 db 层生成进测试 .env，绝不复用生产密钥）；
- 同时把窗口 / 间隔 / 调度模式等压测参数写进测试 .env，供 signin.py 读取；
- 幂等：同一 --n 重复执行结果一致（先清空账号相关表再重建）。

用法：
  python3 seed_accounts.py --n 240 --db /opt/yiban-loadtest/data/yiban.db \\
      --env /opt/yiban-loadtest/test.env --gap 10 \\
      --window-start 06:30 --window-end 07:50
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

# 需要在测试 .env 中落地的压测参数（键 -> 说明），值由参数决定
_ENV_HEADER = "# 压测专用 .env（由 scripts/loadtest/seed_accounts.py 生成，绝不复用生产密钥）\n"


def upsert_env(path, updates):
    """按行更新/追加 KEY=VALUE，保留其它键（含 db 生成的密钥）。"""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    out, seen = [], set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    missing = [k for k in updates if k not in seen]
    if missing and out and out[-1].strip():
        out.append("")
    for k in missing:
        out.append(f"{k}={updates[k]}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def seed(n, db_path, env_path, wipe=True):
    """建库/建账号；返回实际账号数。"""
    os.environ["YIBAN_ENV_FILE"] = env_path
    os.environ["YIBAN_DB_FILE"] = db_path
    # 密钥来源显式指定（env_file 传入），允许 db 生成测试密钥写进该文件
    import db

    db.init_db(db_path, env_file=env_path, cleanup=False, migrate=True)
    conn = db.get_conn()
    if wipe:
        for table in ("accounts", "users", "session_cache", "sign_events", "time_prefs"):
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
    args = ap.parse_args(argv)

    for p in (args.db, args.env):
        d = os.path.dirname(os.path.abspath(p))
        if d:
            os.makedirs(d, exist_ok=True)
    if not os.path.exists(args.env):
        with open(args.env, "w", encoding="utf-8") as f:
            f.write(_ENV_HEADER)

    n = seed(args.n, args.db, args.env, wipe=not args.no_wipe)

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
