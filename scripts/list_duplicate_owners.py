# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""列出“同一用户多个未删除账号”的重复 owner（Phase 2 人工清理辅助）。

用法：
    python3 scripts/list_duplicate_owners.py [--db 路径] [--env .env 路径]

说明：
- 只统计 deleted=0 且 owner 非空、非 admin 的账号。
- 输出重复 owner 及其账号 id/手机号/状态/名称，方便管理员决定保留哪个。
- --env：审计密钥来源 .env 路径（批次14 P2-5）。本工具会执行迁移，而 v3 迁移用
  审计 HMAC 密钥重写哈希链——密钥来源若靠当前目录兜底，在应用根之外运行会
  就地生成游离新钥并用它签坏真实链，故缺省取 YIBAN_ENV_FILE、建议显式指定。
  显式指定的路径必须已存在（批次14 修复轮1③：打错路径直接 exit 2，不新建 .env）。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db


def main():
    parser = argparse.ArgumentParser(description="列出重复 owner（每人限 1 账号冲突数据）")
    parser.add_argument("--db", default=None, help="yiban.db 路径（默认环境变量/相对路径）")
    parser.add_argument("--env", default=None,
                        help="密钥来源 .env 路径（默认取 YIBAN_ENV_FILE；批次14 P2-5 "
                             "用于摆脱对当前目录的依赖；显式指定时必须已存在）")
    args = parser.parse_args()
    # 批次14 修复轮1③：显式 --env 指向不存在的文件时先拒绝，再去碰库。本工具会跑
    # 迁移（v3 重链要用审计密钥），路径打错时会在该位置新建 .env 并生成新密钥，
    # 用第三把钥匙把真实链签坏——取证辅助工具反过来破坏取证对象。
    try:
        env_file = db.require_existing_env_file(args.env)
    except ValueError as e:
        print(f"错误：{e}")
        sys.exit(2)
    if args.db:
        os.environ["YIBAN_DB_FILE"] = args.db

    conn = db.init_db(cleanup=False, env_file=env_file)
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
