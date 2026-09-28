# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""列出“同一用户多个未删除账号”的重复 owner（Phase 2 人工清理辅助）。

用法：
    python3 scripts/list_duplicate_owners.py [--db 路径] [--env .env 路径]

说明：
- 只统计 deleted=0 且 owner 非空、非 admin 的账号。
- 输出重复 owner 及其账号 id/手机号/状态/名称，方便管理员决定保留哪个。
- **只读**：走 `db.open_readonly`（不建库/不建表/不迁移/不切 WAL）——此前经
  `db.init_db` 会在空目录当场建库、并执行 v3 迁移（用审计 HMAC 密钥重写哈希链），
  一次"查看哪里有重复"会改动取证对象。
- --env：**兼容保留**。本工具已只读、不碰密钥，不再需要它；但显式给出的路径仍会校验
  存在性（打错路径原本 exit 2，保持同一退出码契约，不新建 .env）。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db

from yiban.infra import env_io


def main():
    parser = argparse.ArgumentParser(description="列出重复 owner（每人限 1 账号冲突数据）")
    parser.add_argument("--db", default=None, help="yiban.db 路径（默认环境变量/相对路径）")
    parser.add_argument("--env", default=None,
                        help="密钥来源 .env 路径（兼容保留；本工具只读、不再需要，"
                             "显式指定时必须已存在）")
    args = parser.parse_args()
    # 显式 --env 指向不存在的文件时先拒绝（保持打错路径 exit 2 的既有契约）。本工具
    # 已只读、不碰密钥，不再需要它来防"新建 .env 并生成游离密钥"。
    try:
        db.require_existing_env_file(args.env)
    except ValueError as e:
        print(f"错误：{e}")
        sys.exit(2)
    if args.db:
        os.environ["YIBAN_DB_FILE"] = args.db

    # 只读打开：不建库、不建表、不迁移、不切 WAL。库文件缺失时不 `init_db` 建空库
    # （空库上"未发现重复 owner"是假结论），直接以独立退出码报告。
    db_path = env_io.resolve_path("YIBAN_DB_FILE", db.DB_DEFAULT)
    conn = db.open_readonly(db_path)
    if conn is None:
        print(f"库文件不存在: {db_path}（只读列出，不建库；请核对 --db / YIBAN_DB_FILE）")
        sys.exit(2)
    try:
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
    finally:
        conn.close()


if __name__ == "__main__":
    main()
