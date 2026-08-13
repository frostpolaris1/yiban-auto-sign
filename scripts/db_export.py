# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""db → JSON 回滚逃生门：把 yiban.db 导出为 accounts.json / users.json（降级/迁移用）。

用法：python3 scripts/db_export.py [--out 输出目录] [--db yiban.db 路径]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db


def main():
    parser = argparse.ArgumentParser(description="导出 SQLite 数据为 JSON（回滚逃生门）")
    parser.add_argument("--out", default=".", help="输出目录（默认当前目录）")
    parser.add_argument("--db", default=None, help="yiban.db 路径（默认环境变量/相对路径）")
    args = parser.parse_args()
    if args.db:
        os.environ["YIBAN_DB_FILE"] = args.db
    db.init_db()
    accounts = db.load_accounts()
    users = db.load_users()
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "accounts.json"), "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "users.json"), "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    print(f"已导出 {len(accounts)} 个账号 / {len(users)} 个用户 → {args.out}/")


if __name__ == "__main__":
    main()
