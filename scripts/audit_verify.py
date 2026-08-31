# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""审计日志 HMAC 哈希链校验工具（Phase 3）。

用法：
    python3 scripts/audit_verify.py [--db 路径] [--env .env 路径]

输出：
- 校验通过：exit 0
- 校验失败：列出 broken 数量与首个断点 id，exit 1

--env：审计密钥（YIBAN_AUDIT_KEY）所在 .env 路径。批次14 P2-5——不指定时取
环境变量 YIBAN_ENV_FILE，两者都没有才回落到当前目录 .env；取证时请在任意
目录下显式指定，否则会拿错密钥把完好链判成断链。显式指定的路径必须已存在
（批次14 修复轮1③：打错路径时直接 exit 2，不会在该位置新建 .env/生成新密钥）。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db


def main():
    parser = argparse.ArgumentParser(description="校验审计日志 HMAC 哈希链")
    parser.add_argument("--db", default=None, help="yiban.db 路径（默认环境变量/相对路径）")
    parser.add_argument("--env", default=None,
                        help="密钥来源 .env 路径（默认取 YIBAN_ENV_FILE；批次14 P2-5 "
                             "用于摆脱对当前目录的依赖；显式指定时必须已存在）")
    args = parser.parse_args()
    # 批次7 P2-6：只读校验语义三件套——
    # 1) 库文件必须已存在：sqlite3.connect 缺库即建空库，空链 verify"通过"会对
    #    真实库是否被篡改什么都没说（路径写错时静默误报通过）；
    # 2) 不执行迁移（migrate=False）：迁移会用当前密钥重写审计链，抹平篡改痕迹；
    # 3) 不执行启动清理（cleanup=False）。
    # 批次14 修复轮1③：显式 --env 也必须已存在——打错路径时后续工具（同一套回落
    # 逻辑的 rekey/重置/清点）会在该位置新建 .env 并生成新审计密钥，把留痕用
    # 第三把钥匙签坏；四条取证 CLI 统一在碰任何数据前先拒绝。
    try:
        env_file = db.require_existing_env_file(args.env)
    except ValueError as e:
        print(f"审计校验中止：{e}")
        sys.exit(2)
    db_path = args.db or os.environ.get("YIBAN_DB_FILE", db.DB_DEFAULT)
    if not os.path.exists(db_path):
        print(f"审计校验中止：数据库文件不存在: {db_path}（拒绝新建空库误报通过）")
        sys.exit(2)
    db.init_db(db_file=db_path, cleanup=False, migrate=False, env_file=env_file)
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
