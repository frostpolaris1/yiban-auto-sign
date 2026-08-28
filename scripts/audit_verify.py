# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""审计日志 HMAC 哈希链校验工具（Phase 3）。

用法：
    python3 scripts/audit_verify.py [--db 路径]

输出：
- 校验通过：exit 0
- 校验失败：列出 broken 数量与首个断点 id，exit 1
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db


def main():
    parser = argparse.ArgumentParser(description="校验审计日志 HMAC 哈希链")
    parser.add_argument("--db", default=None, help="yiban.db 路径（默认环境变量/相对路径）")
    args = parser.parse_args()
    # 批次7 P2-6：只读校验语义三件套——
    # 1) 库文件必须已存在：sqlite3.connect 缺库即建空库，空链 verify"通过"会对
    #    真实库是否被篡改什么都没说（路径写错时静默误报通过）；
    # 2) 不执行迁移（migrate=False）：迁移会用当前密钥重写审计链，抹平篡改痕迹；
    # 3) 不执行启动清理（cleanup=False）。
    db_path = args.db or os.environ.get("YIBAN_DB_FILE", db.DB_DEFAULT)
    if not os.path.exists(db_path):
        print(f"审计校验中止：数据库文件不存在: {db_path}（拒绝新建空库误报通过）")
        sys.exit(2)
    db.init_db(db_file=args.db, cleanup=False, migrate=False)
    ok, broken, first_broken = db.verify_audit_chain()
    if ok:
        print("审计哈希链校验通过")
        return
    if broken == -1:
        print("审计哈希链校验过程异常，请查看日志")
        sys.exit(2)
    print(f"审计哈希链校验失败：broken={broken}, first_broken_id={first_broken}")
    sys.exit(1)


if __name__ == "__main__":
    main()
