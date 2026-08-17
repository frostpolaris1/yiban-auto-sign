# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""列出“同一用户多个未删除账号”的重复 owner（Phase 2 人工清理辅助）。

用法：
    python3 scripts/list_duplicate_owners.py [--db 路径]

说明：
- 只统计 deleted=0 且 owner 非空、非 admin 的账号。
- 输出重复 owner 及其账号 id/手机号/状态/名称，方便管理员决定保留哪个。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db


def main():
    parser = argparse.ArgumentParser(description="列出重复 owner（每人限 1 账号冲突数据）")
    parser.add_argument("--db", default=None, help="yiban.db 路径（默认环境变量/相对路径）")
    args = parser.parse_args()
    if args.db:
        os.environ["YIBAN_DB_FILE"] = args.db

    conn = db.init_db(cleanup=False)
    rows = conn.execute(
        "SELECT owner, COUNT(*) AS cnt FROM accounts "
        "WHERE deleted=0 AND owner NOT IN ('', 'admin') "
        "GROUP BY owner HAVING COUNT(*) > 1 ORDER BY owner"
    ).fetchall()

    if not rows:
        print("未发现重复 owner（每人限 1 账号规则当前无冲突）")
        return

    print(f"发现 {len(rows)} 个重复 owner：")
    for r in rows:
        owner = r["owner"]
        print(f"\nowner: {owner}（{r['cnt']} 个未删除账号）")
        accs = conn.execute(
            "SELECT id, name, phone, status, deleted_at FROM accounts "
            "WHERE owner=? AND deleted=0 ORDER BY id",
            (owner,),
        ).fetchall()
        for a in accs:
            print(
                f"  id={a['id']} 手机号={a['phone']} 状态={a['status']} 名称={a['name']}"
            )

    print("\n请人工决定保留哪个账号，把多余的账号软删除或彻底删除后再重启系统。")


if __name__ == "__main__":
    main()
