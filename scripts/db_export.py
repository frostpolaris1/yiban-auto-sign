# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""db → JSON 回滚逃生门：把 yiban.db 导出为 accounts.json / users.json（降级/迁移用）。

用法：python3 scripts/db_export.py [--out 输出目录] [--db yiban.db 路径] [--plaintext]

安全设计：
- 默认导出**密文**（password/phone_code 保持 AES-GCM 密文对象），不产生明文凭据文件；
- 仅显式 --plaintext 才导出解密后的明文密码（运维恢复演练等确需场景），并打印醒目警告；
- 导出文件统一 chmod 0600（含凭据数据，防同主机其他用户读取）。
"""
import argparse
import contextlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db


def main():
    parser = argparse.ArgumentParser(description="导出 SQLite 数据为 JSON（回滚逃生门）")
    parser.add_argument("--out", default=".", help="输出目录（默认当前目录；建议显式指定）")
    parser.add_argument("--db", default=None, help="yiban.db 路径（默认环境变量/相对路径）")
    parser.add_argument(
        "--plaintext",
        action="store_true",
        help="导出解密后的明文密码（默认导出密文，避免明文凭据落盘）",
    )
    args = parser.parse_args()
    if args.db:
        os.environ["YIBAN_DB_FILE"] = args.db
    db.init_db()
    accounts = db.load_accounts() if args.plaintext else db.load_accounts_raw()
    users = db.load_users()
    os.makedirs(args.out, exist_ok=True)
    for name, data in (("accounts.json", accounts), ("users.json", users)):
        path = os.path.join(args.out, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)  # 含凭据数据，收紧权限（Windows 无实际效果，忽略失败）
    if args.plaintext:
        print("⚠️  已导出明文凭据（--plaintext），请立即转移到安全位置并删除该文件")
    print(f"已导出 {len(accounts)} 个账号 / {len(users)} 个用户 → {args.out}/")


if __name__ == "__main__":
    main()
