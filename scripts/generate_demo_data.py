# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""生成本地 demo 数据库（大量占位用户/账号/统计事件），用于 WebUI 对接测试。

用法：
    python3 scripts/generate_demo_data.py --dry-run            # 只看将清哪些表、多少行
    python3 scripts/generate_demo_data.py --yes --fingerprint <上面打印的指纹>

说明：
- 仅用于本地测试，不部署。
- 会生成 users / accounts / audit_logs / time_prefs / sign_events。
- 使用固定随机种子，结果可复现。
- **清空旧 demo 数据是不可逆动作**：必须回显由目标库内容派生的指纹（`--fingerprint`）
  加 `--yes` 才放行；缺确认或指纹不匹配一律拒绝且零删除。每次真实清空写一条审计留痕，
  留痕写不进去就放弃清空（fail-closed）。
"""
import argparse
import datetime
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db

from yiban.store import purge_guard

DEFAULT_DEMO_DB = "demo-log/demo.db"

# 清空顺序：先事件/日志后账号/用户（初始化时 foreign_keys=OFF，顺序只影响可读性）
_WIPE_ORDER = ("sign_events", "time_prefs", "audit_logs", "accounts", "users")


def _phone(i):
    return f"13{100000000 + i:09d}"


def _email(i):
    return f"user{i:04d}@demo.local"


def _ts(days_ago=0, hour=8, minute=0):
    d = datetime.datetime.now() - datetime.timedelta(days=days_ago)
    return d.replace(hour=hour, minute=minute, second=0, microsecond=0).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成本地 demo 数据库")
    parser.add_argument("--db", default=DEFAULT_DEMO_DB, help="demo 数据库路径")
    parser.add_argument("--yes", action="store_true", help="确认清空（须与 --fingerprint 同时给出）")
    parser.add_argument("--fingerprint", default="",
                        help="回显目标库指纹（由库内容派生；先 --dry-run 查看）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将清空的表与行数，不做任何改动")
    parser.add_argument("--env", default=None, help="demo .env 路径（默认使用项目根 .env）")
    parser.add_argument("--users", type=int, default=500, help="普通用户数量")
    parser.add_argument("--admin-accounts", type=int, default=10, help="admin 共享账号数量")
    parser.add_argument("--events-per-day", type=int, default=200, help="每天签到事件数量")
    parser.add_argument("--days", type=int, default=30, help="统计事件覆盖天数")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as e:  # argparse 的 --help(0)/用法错误(2) 转成返回值
        code = getattr(e, "code", 2)
        return code if isinstance(code, int) else 2

    # 目标指纹由库内容（各表行数 + 规模）派生：路径写对不代表库指对，指纹要让人
    # 一眼看出"这是 demo 库还是生产库"（行数差异天然可辨）。
    fingerprint, summary = purge_guard.db_content_fingerprint(args.db)
    counts = purge_guard.table_counts(args.db)
    total = sum(counts.get(t, 0) for t in _WIPE_ORDER)
    print(f"目标库指纹: {fingerprint}")
    for line in summary:
        print(f"  {line}")

    if args.dry_run:
        print("dry-run：以下表将被清空并重建 demo 数据（未做任何改动）：")
        for table in _WIPE_ORDER:
            print(f"  {table}: {counts.get(table, 0)} 行")
        print(f"  合计 {total} 行")
        return 0

    if not (args.yes and purge_guard.confirmation_ok(fingerprint, args.fingerprint)):
        print("拒绝执行：清空 demo 库需同时提供 --yes 与 --fingerprint（逐字回显上面打印的指纹）。",
              file=sys.stderr)
        print(f"  目标库指纹: {fingerprint}", file=sys.stderr)
        return 2

    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    random.seed(args.seed)

    # 与 db_export 同理——demo 脚本不得在初始化时触发破坏性清理
    db.init_db(args.db, env_file=args.env, cleanup=False)

    # 清空前的审计能力前置校验：留痕写不进去就不许动（fail-closed）。这条随后会被
    # 清空 audit_logs 一并删掉，它的意义是"在不可逆动作之前证明留痕可用"。
    if not db.audit("demo-data", "demo_purge_begin", fingerprint,
                    f"授权清空 demo 库（原 {total} 行）"):
        print("拒绝执行：清库留痕写入失败，按 fail-closed 放弃清空（未删除任何行）。",
              file=sys.stderr)
        return 1

    conn = db.get_conn()

    # 清空旧 demo 数据（含 audit_logs：demo 审计链整体重建）
    print(f"将清空目标数据库: {args.db}（指纹 {fingerprint}）")
    for table in _WIPE_ORDER:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()

    # 清空后写一条留痕，作为重建后 demo 审计链的起点。写不进去必须响亮失败——
    # "删了但没留痕"正是要防的形态。
    if not db.audit("demo-data", "demo_purge", fingerprint,
                    f"已清空 5 表（原 {total} 行）并重建 demo 数据"):
        print("清库留痕写入失败（数据已清、审计未落），按失败退出。", file=sys.stderr)
        return 1

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
    # 取值必须与 scripts/signin.py 的 STATUS_* 常量、以及 stage 的两种口径对齐，
    # 否则前端的分类统计会得到与真实运行不符的演示结果（stage 只有 sign/probe 两种）。
    statuses = ["success", "already", "no_task", "failed", "retrying",
                "skipped_window", "no_position", "paused", "user_cancelled"]
    for day in range(args.days):
        for _ in range(args.events_per_day):
            db.add_sign_event(
                _ts(days_ago=day, hour=random.randint(6, 7), minute=random.randint(0, 59)),
                _phone(random.randrange(users)),
                random.choice(statuses),
                "demo",
                "sign",
                random.randint(1, 3),
            )
        # 探针按固定周期执行，量级远小于签到；与签到共用 sign_events 表，
        # 保留少量样本用于验证前端是否按 stage 正确区分统计口径。
        for _ in range(random.randint(2, 4)):
            db.add_sign_event(
                _ts(days_ago=day, hour=random.randint(0, 23), minute=random.randint(0, 59)),
                _phone(random.randrange(users)),
                random.choice(["success", "failed"]),
                "demo probe",
                "probe",
                1,
            )

    final = {
        "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "accounts": conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
        "audit_logs": conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0],
        "sign_events": conn.execute("SELECT COUNT(*) FROM sign_events").fetchone()[0],
    }
    print("demo 数据生成完成：")
    for k, v in final.items():
        print(f"  {k}: {v}")

    ok, broken, first = db.verify_audit_chain()
    # 清空 audit_logs 是显式动作：空链/新链的 verify 结果不能按"通过"一语带过，
    # 必须把"审计表被本工具清空过（原 N 条）"与链结果一起报出来。
    print(f"审计链：本库 audit_logs 已被清空（原 {counts.get('audit_logs', 0)} 条）并重建；"
          f"哈希链校验 ok={ok}, broken={broken}, first_broken_id={first}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
