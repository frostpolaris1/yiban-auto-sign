# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
审计可追溯性取证校验工具。

一次跑齐三件事（与 web 每日线程调的同一个 db.audit_health()）：
  1. 链内 HMAC 哈希自洽      —— 检出改内容 / 删中间行；
  2. 库外锚点比对（定点+稠密+留痕）—— 检出删尾（含"删尾后再写"）、删前缀、
     整表清空、锚点文件自身被截断或改写；
  3. 审计写入欠账            —— 检出"业务已生效但审计没写进去"的静默丢失。

除三项结论外，还固定带出「累计留痕的审计清理条数」与「最近一次审计清理的截止点/
条数」——本机时钟被渐进拨快时，本机自校验防不住（守卫参照点会跟着推进），异机侧
只能靠这两个数字发现保留期清理被异常前移。

**归属**
取证/运维侧脚本（`scripts/`）；判据本体在 `yiban.store.audit_chain`，本脚本只做参数
解析与取证口径落定，与 web 每日线程共用同一 `audit_health()`。

**复用**
无对外可复用函数；核心判据复用 `yiban.store.audit_chain.audit_health` /
`audit_anchor_path`，web 与容器侧都应调那一份，不得另写第二套校验。

**通信**
用法：`python3 scripts/audit_verify.py [--db 路径] [--env .env 路径] [--anchor 路径]`
输入：命令行 `--db` / `--env` / `--anchor`（缺省走环境变量与默认路径）。
输出：三项结论 + 清理留痕数字到 stdout；退出码：全部通过 exit 0；检出异常 exit 1；
无法定论（密钥缺失 / 校验过程异常）exit 2。
调用谁：`db`（`yiban.store.db` / `audit_chain` 的兼容壳）。
谁调用：运维手工取证；`scripts/backup.sh` 恢复件双验（restore 流程在解包后调它做审计
链核验，非 0 视为恢复件不可信）；backup.sh 头部示例还给了 cron 每日 02:30 定排的一条
命令，把输出重定向到 `audit-verify.log`。只读，无写盘。

--anchor：外部锚点文件路径。默认 db.audit_anchor_path()（YIBAN_STATE_DIR，
裸机默认 /var/log/yiban，Windows 开发环境默认 cwd）。README 承诺"校验审计链并
比对 audit-anchor.log 外部锚点"，故不传参即按默认路径比对；"锚点与库必须同源"
的判据见 main() 的锚点推断分支。

--env：审计密钥（YIBAN_AUDIT_KEY）所在 .env 路径；回落次序与"显式指定的路径
必须已存在"的理由见 main() 的 env 守卫注释。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db


def _same_path(a, b):
    """两个路径是否指向同一个文件（不存在的路径按规范化字符串比）。"""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(os.path.abspath(a)) == os.path.realpath(os.path.abspath(b))


def main():
    parser = argparse.ArgumentParser(description="校验审计哈希链 + 比对库外锚点 + 审计写入欠账")
    parser.add_argument("--db", default=None, help="yiban.db 路径（默认环境变量/相对路径）")
    parser.add_argument("--env", default=None,
                        help="密钥来源 .env 路径（默认取 YIBAN_ENV_FILE；"
                             "用于摆脱对当前目录的依赖；显式指定时必须已存在）")
    parser.add_argument("--anchor", default=None,
                        help="外部锚点文件路径（默认 db.audit_anchor_path()，"
                             "即 <YIBAN_STATE_DIR>/audit-anchor.log；"
                             "**校验非本部署的库时必须显式指定**——锚点与库必须同源，"
                             "否则两套数据的差异会被误报成审计被篡改）")
    args = parser.parse_args()
    # 只读校验三件套（理由见各自那一行）：--env 已存在、库文件已存在、不迁移不清理
    try:
        # 显式 --env 也必须已存在：打错路径时同一套回落逻辑的其余取证 CLI（如
        # `scripts/list_duplicate_owners.py`）会在该位置新建 .env 并生成新审计密钥，
        # 把留痕用的第三把钥匙签坏——取证类 CLI 一律在碰任何数据前先拒绝
        env_file = db.require_existing_env_file(args.env)
    except ValueError as e:
        print(f"审计校验中止：{e}")
        sys.exit(2)
    db_path = args.db or os.environ.get("YIBAN_DB_FILE", db.DB_DEFAULT)
    # 库必须已存在：sqlite3.connect 缺库即建空库，空链 verify"通过"对真实库有没有被
    # 篡改什么都没说（路径写错时静默误报通过）
    if not os.path.exists(db_path):
        print(f"审计校验中止：数据库文件不存在: {db_path}（拒绝新建空库误报通过）")
        sys.exit(2)
    anchor_path = args.anchor
    if anchor_path is None:
        # 锚点文件与审计库是**一套**数据，而锚点路径来自部署的状态目录（机器级路径），
        # 与 --db 无关。指向别的库（取证副本、备份恢复出来的库）却沿用本部署的锚点，
        # 比出来的差异说明不了任何事，还会给出"审计记录被删除"这种**假篡改结论**。
        # 这种情况按"无法定论"中止，并要求显式 --anchor（取证时把锚点一并拷来）。
        deployed_db = os.environ.get("YIBAN_DB_FILE", db.DB_DEFAULT)
        if not _same_path(db_path, deployed_db):
            print("审计校验中止：--db 指向的不是本部署的库，无法推断它对应的锚点文件"
                  "（锚点与库必须同源，否则会把两套数据误报成篡改）。"
                  "对副本取证请把该库的锚点一并拷来并用 --anchor 指定")
            sys.exit(2)
        anchor_path = db.audit_anchor_path()
    # migrate=False：迁移会用当前密钥重写整条链、抹平篡改痕迹；cleanup=False 同理不落写
    db.init_db(db_file=db_path, cleanup=False, migrate=False, env_file=env_file)
    health = db.audit_health(path=anchor_path)
    # 链校验过程异常/密钥缺失（broken == -1）不是"检出篡改"而是"无法定论"——
    # 按中止处理（exit 2），绝不用 exit 1 冒充一次成功的取证。
    if health["broken"] == -1:
        print("审计校验中止：哈希链校验过程异常或未配置 YIBAN_AUDIT_KEY（无法定论）")
        sys.exit(2)
    # 锚点自检"无法定论"（非法编码/坏行/读不出）同理：它既不是通过也不是确证篡改，
    # 编成 exit 1 会让运维把一次编码事故当成失陷响应，编成 0 等于把"没验成"印成通过。
    if health.get("anchor_status") == "indeterminate":
        print(f"审计校验中止：锚点自检无法定论——{health['anchor_msg']}")
        print("（这不等于审计被篡改，也不等于无异常；请修好锚点文件后重跑，"
              "确认之前不要据此下任何结论）")
        sys.exit(2)
    print(f"锚点文件：{anchor_path}")
    print(f"链内自洽：{'通过' if health['chain_ok'] else '失败'}"
          f"（broken={health['broken']}）")
    print(f"锚点比对：{'通过' if health['anchor_ok'] else '失败'}"
          f"（{health['anchor_msg'] or '无提示'}）")
    print(f"锚点独立见证：{health.get('anchor_witness') or '未知'}")
    print(f"写入欠账：{health['write_failures']} 次")
    print(f"全表重链留痕：{len(health['rechain_events'])} 条；"
          f"空 hash 行：{health['empty_hash_rows']} 条")
    # 清理量必须随取证输出带出：本机时钟被渐进拨快时本机自校验不会报警（守卫参照点
    # 每次都推进），异机侧只能靠"累计删除条数 + 最近 cutoff"判断清理是否异常前移。
    # 无留痕记录时也照打——运维要看得出"从哪一天起开始有"。
    _lc = health.get("last_cleanup") or {}
    print(f"累计留痕的审计清理条数：{health.get('purge_total', 0)} 条")
    print("最近一次审计清理：" + (
        f"{_lc.get('ts') or '?'} 截止 {_lc.get('cutoff') or '?'}，"
        f"删除 {_lc.get('deleted', 0)} 条" if _lc else "（无）"
    ))
    if health["note"]:
        print(f"附加诊断：{health['note']}")
    if health["healthy"]:
        print("审计可追溯性校验通过（链自洽 + 锚点一致 + 无写入欠账）")
        return
    print("审计可追溯性校验失败：审计记录可能被篡改/删除，或存在未留痕的管理操作")
    sys.exit(1)


if __name__ == "__main__":
    main()
