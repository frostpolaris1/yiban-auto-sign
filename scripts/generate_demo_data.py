# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""生成本地 demo 数据库（大量占位用户/账号/统计事件），用于 WebUI 对接测试。

用法：
    python3 scripts/generate_demo_data.py [--db demo-log/demo.db] [--users 500]

说明：
- 仅用于本地测试，不部署。
- 会生成 users / accounts / audit_logs / time_prefs / sign_events / page_visits / server_metrics。
- 使用固定随机种子，结果可复现。
"""
import argparse
import datetime
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db


def _phone(i):
    return f"13{100000000 + i:09d}"


def _email(i):
    return f"user{i:04d}@demo.local"


def _ts(days_ago=0, hour=8, minute=0):
    d = datetime.datetime.now() - datetime.timedelta(days=days_ago)
    return d.replace(hour=hour, minute=minute, second=0, microsecond=0).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def main():
    parser = argparse.ArgumentParser(description="生成本地 demo 数据库")
    parser.add_argument("--db", default="demo-log/demo.db", help="demo 数据库路径")
    parser.add_argument("--env", default=None, help="demo .env 路径（默认使用项目根 .env）")
    parser.add_argument("--users", type=int, default=500, help="普通用户数量")
    parser.add_argument("--admin-accounts", type=int, default=10, help="admin 共享账号数量")
    parser.add_argument("--events-per-day", type=int, default=200, help="每天签到事件数量")
    parser.add_argument("--days", type=int, default=30, help="统计事件覆盖天数")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    random.seed(args.seed)

    db.init_db(args.db, env_file=args.env)
    conn = db.get_conn()

    # 清空旧 demo 数据（只清 demo 相关表，避免误伤真实库）
    for table in ("server_metrics", "page_visits", "sign_events", "time_prefs",
                  "audit_logs", "accounts", "users"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()

    users = args.users
    print(f"生成 {users} 个普通用户 ...")
    for i in range(users):
        db.create_user(
            _email(i),
            "demo-hash",
            role="admin" if i < 10 else "user",
            created_at=_ts(days_ago=random.randint(0, 300)),
            pw_version=1,
        )

    print(f"生成 {users} 个普通用户账号（每 10 人附带 1 个已删除账号）...")
    for i in range(users):
        email = _email(i)
        # 部分用户先创建一个已删除账号，再创建生效账号（兼容一人一号约束）
        if i % 10 == 0:
            deleted_id = db.add_account({
                "name": f"旧账号{i}",
                "phone": _phone(i + 100000),
                "password": "old-pass",
                "phone_model": "",
                "phone_code": "",
                "owner": email,
                "status": "active",
                "reject_reason": "",
            })
            db.set_account_deleted(deleted_id, 1, _ts(days_ago=random.randint(1, 30)))
        status = random.choice(["active", "pending", "rejected"])
        db.add_account({
            "name": f"用户{i}",
            "phone": _phone(i),
            "password": f"pass-{i}",
            "phone_model": "DemoPhone",
            "phone_code": "",
            "owner": email,
            "status": status,
            "reject_reason": "演示拒绝" if status == "rejected" else "",
        })

    print(f"生成 {args.admin_accounts} 个 admin 共享账号 ...")
    for i in range(args.admin_accounts):
        db.add_account({
            "name": f"管理员账号{i}",
            "phone": _phone(200000 + i),
            "password": "admin-pass",
            "phone_model": "",
            "phone_code": "",
            "owner": "admin",
            "status": "active",
            "reject_reason": "",
        })

    print("生成自选时间片 ...")
    for i in range(0, users, 3):
        db.set_time_pref(_phone(i), random.choice([0, 5, 10, 15, 20]), _ts(days_ago=1))

    print("生成审计日志 ...")
    actions = ["account_add", "account_update", "account_batch", "user_role", "settings_save"]
    for i in range(users * 2):
        db.audit(
            random.choice([_email(random.randrange(users)), "admin"]),
            random.choice(actions),
            _phone(random.randrange(users)),
            f"demo audit {i}",
        )

    print("生成签到事件 ...")
    statuses = ["success", "failed", "retrying", "paused", "skip"]
    stages = ["login", "signin", "queue"]
    for day in range(args.days):
        for _ in range(args.events_per_day):
            db.add_sign_event(
                _ts(days_ago=day, hour=random.randint(6, 7), minute=random.randint(0, 59)),
                _phone(random.randrange(users)),
                random.choice(statuses),
                "demo",
                random.choice(stages),
                random.randint(1, 3),
            )

    print("生成页面访问事件 ...")
    roles = ["admin", "user", "anonymous"]
    paths = ["/", "/login", "/user", "/api/accounts", "/api/settings"]
    for _ in range(users * 10):
        db.add_page_visit(
            _ts(days_ago=random.randint(0, args.days), hour=random.randint(0, 23),
                minute=random.randint(0, 59)),
            random.choice(roles),
            random.choice(paths),
            db.hash_ip(f"10.0.{random.randrange(256)}.{random.randrange(256)}"),
            "DemoUA",
            random.randint(0, 5000),
        )

    print("生成服务器性能采样 ...")
    for hour in range(24 * 7):
        ts = (datetime.datetime.now() - datetime.timedelta(hours=hour)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        db.add_server_metric(
            ts,
            cpu=random.uniform(0, 80),
            mem_pct=random.uniform(20, 90),
            disk_pct=random.uniform(30, 80),
            net_in=random.uniform(0, 5000),
            net_out=random.uniform(0, 5000),
            load1=random.uniform(0, 4),
            load5=random.uniform(0, 4),
            load15=random.uniform(0, 4),
            proc_count=random.randint(50, 300),
        )

    counts = {
        "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "accounts": conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
        "audit_logs": conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0],
        "sign_events": conn.execute("SELECT COUNT(*) FROM sign_events").fetchone()[0],
        "page_visits": conn.execute("SELECT COUNT(*) FROM page_visits").fetchone()[0],
        "server_metrics": conn.execute("SELECT COUNT(*) FROM server_metrics").fetchone()[0],
    }
    print("demo 数据生成完成：")
    for k, v in counts.items():
        print(f"  {k}: {v}")

    ok, broken, first = db.verify_audit_chain()
    print(f"审计哈希链校验: ok={ok}, broken={broken}, first_broken_id={first}")


if __name__ == "__main__":
    main()
