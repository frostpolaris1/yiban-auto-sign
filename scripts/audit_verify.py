# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""审计可追溯性取证校验工具（Phase 3 / 锚点比对补全）。

用法：
    python3 scripts/audit_verify.py [--db 路径] [--env .env 路径] [--anchor 路径]

一次跑齐三件事（与 web 每日线程调的同一个 db.audit_health()）：
  1. 链内 HMAC 哈希自洽      —— 检出改内容 / 删中间行；
  2. 库外锚点比对（定点+稠密+留痕）—— 检出删尾（含"删尾后再写"）、删前缀、
     整表清空、锚点文件自身被截断或改写；
  3. 审计写入欠账            —— 检出"业务已生效但审计没写进去"的静默丢失。

输出：
- 全部通过：exit 0
- 检出异常：打印各项结论与锚点判据说明，exit 1
- 无法定论（密钥缺失 / 校验过程异常）：exit 2

--anchor：外部锚点文件路径。默认 db.audit_anchor_path()（YIBAN_STATE_DIR，
裸机默认 /var/log/yiban，Windows 开发环境默认 cwd）。README 承诺"校验审计链并
比对 audit-anchor.log 外部锚点"——本工具此前只验链不比对锚点，删尾/清空/整表
被抹掉在这里完全静默；现不传参即按默认路径比对，传参可指向取证副本。

--env：审计密钥（YIBAN_AUDIT_KEY）所在 .env 路径。不指定时取
环境变量 YIBAN_ENV_FILE，两者都没有才回落到当前目录 .env；取证时请在任意
目录下显式指定，否则会拿错密钥把完好链判成断链。显式指定的路径必须已存在
（打错路径时直接 exit 2，不会在该位置新建 .env/生成新密钥）。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db


def main():
    parser = argparse.ArgumentParser(description="校验审计哈希链 + 比对库外锚点 + 审计写入欠账")
    parser.add_argument("--db", default=None, help="yiban.db 路径（默认环境变量/相对路径）")
    parser.add_argument("--env", default=None,
                        help="密钥来源 .env 路径（默认取 YIBAN_ENV_FILE；"
                             "用于摆脱对当前目录的依赖；显式指定时必须已存在）")
    parser.add_argument("--anchor", default=None,
                        help="外部锚点文件路径（默认 db.audit_anchor_path()，"
                             "即 <YIBAN_STATE_DIR>/audit-anchor.log）")
    args = parser.parse_args()
    # 只读校验语义三件套——
    # 1) 库文件必须已存在：sqlite3.connect 缺库即建空库，空链 verify"通过"会对
    #    真实库是否被篡改什么都没说（路径写错时静默误报通过）；
    # 2) 不执行迁移（migrate=False）：迁移会用当前密钥重写审计链，抹平篡改痕迹；
    # 3) 不执行启动清理（cleanup=False）。
    # 显式 --env 也必须已存在——打错路径时后续工具（同一套回落
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
    anchor_path = args.anchor or db.audit_anchor_path()
    db.init_db(db_file=db_path, cleanup=False, migrate=False, env_file=env_file)
    health = db.audit_health(path=anchor_path)
    # 链校验过程异常/密钥缺失（broken == -1）不是"检出篡改"而是"无法定论"——
    # 按中止处理（exit 2），绝不用 exit 1 冒充一次成功的取证。
    if health["broken"] == -1:
        print("审计校验中止：哈希链校验过程异常或未配置 YIBAN_AUDIT_KEY（无法定论）")
        sys.exit(2)
    print(f"锚点文件：{anchor_path}")
    print(f"链内自洽：{'通过' if health['chain_ok'] else '失败'}"
          f"（broken={health['broken']}）")
    print(f"锚点比对：{'通过' if health['anchor_ok'] else '失败'}"
          f"（{health['anchor_msg'] or '无提示'}）")
    print(f"写入欠账：{health['write_failures']} 次")
    print(f"全表重链留痕：{len(health['rechain_events'])} 条；"
          f"空 hash 行：{health['empty_hash_rows']} 条")
    if health["note"]:
        print(f"附加诊断：{health['note']}")
    if health["healthy"]:
        print("审计可追溯性校验通过（链自洽 + 锚点一致 + 无写入欠账）")
        return
    print("审计可追溯性校验失败：审计记录可能被篡改/删除，或存在未留痕的管理操作")
    sys.exit(1)


if __name__ == "__main__":
    main()
