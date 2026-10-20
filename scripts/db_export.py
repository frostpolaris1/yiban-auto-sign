# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""db → JSON 回滚逃生门：把 yiban.db 导出为 accounts.json / users.json（降级/迁移用）。

用法：python3 scripts/db_export.py [--out 输出目录] [--db yiban.db 路径] [--plaintext]
       [--force] [--dry-run]

安全设计：
- 默认导出**密文**（password/phone_code 保持 AES-GCM 密文对象），不产生明文凭据文件；
- 仅显式 --plaintext 才导出解密后的明文密码（运维恢复演练等确需场景），并**先**打印醒目警告；
- 导出文件以 0600 创建（含凭据数据，防同主机其他用户读取）；
- **默认拒绝覆盖既有文件**（工单 ba-p11-06）：缺省 `--out .` 与 web 侧
  `accounts.json` 回落名逐字相同，无条件覆写会把 JSON→SQLite 的迁移基线/回滚源
  就地截断。`--force` 才允许替换；`--dry-run` 只报告目标与行数，不写盘。
"""
import argparse
import contextlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

_TARGET_NAMES = ("accounts.json", "users.json")


def _existing_targets(out_dir):
    """返回已存在、且不是普通文件的目标项（不能安全替换）。"""
    blockers = []
    for name in _TARGET_NAMES:
        path = os.path.join(out_dir, name)
        if os.path.exists(path) and not os.path.isfile(path):
            blockers.append(path)
    return blockers


def _refuse_overwrite(out_dir):
    """返回既有的普通文件清单（未给 --force 时须拒绝）。"""
    return [os.path.join(out_dir, name) for name in _TARGET_NAMES
            if os.path.isfile(os.path.join(out_dir, name))]


def main(argv=None):
    parser = argparse.ArgumentParser(description="导出 SQLite 数据为 JSON（回滚逃生门）")
    parser.add_argument("--out", default=".", help="输出目录（默认当前目录；建议显式指定）")
    parser.add_argument("--db", default=None, help="yiban.db 路径（默认环境变量/相对路径）")
    parser.add_argument("--env", default=None, help=".env 路径（默认项目根 .env）")
    parser.add_argument(
        "--plaintext",
        action="store_true",
        help="导出解密后的明文密码（默认导出密文，避免明文凭据落盘）",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="允许覆盖输出目录里已存在的 accounts.json / users.json（默认拒绝）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只报告将写出的目标与行数，不写盘、不写审计",
    )
    args = parser.parse_args(argv)
    if args.db:
        os.environ["YIBAN_DB_FILE"] = args.db
    # 导出是"逃生门"，绝不允许在导出前触发破坏性清理
    # （init_db 缺省 cleanup=True 会物理清掉过期软删账号/注销用户/旧审计——
    # 保留期边界导出的数据会静默缺一批且无法区分"本来没有"还是"被清理"）
    # 补 migrate=False——迁移会用当前密钥回填审计链（v3），
    # 使"被导出对象在校验/导出过程中被改动"（与 audit_verify.py 同口径，
    # 本工具当时漏网）。旧 schema 库导出报错属预期：逃生门要求用配套版本工具。
    db.init_db(env_file=args.env, cleanup=False, migrate=False)
    accounts = db.load_accounts() if args.plaintext else db.load_accounts_raw()
    users = db.load_users()

    blockers = _existing_targets(args.out)
    existing = _refuse_overwrite(args.out)
    if args.dry_run:
        print(f"[dry-run] 将导出 {len(accounts)} 个账号 / {len(users)} 个用户 → {args.out}/")
        for name in _TARGET_NAMES:
            path = os.path.join(args.out, name)
            note = "（已存在，需 --force）" if os.path.isfile(path) else ""
            if os.path.exists(path) and not os.path.isfile(path):
                note = "（不是普通文件，无法替换）"
            print(f"  {path}{note}")
        return 0

    if blockers:
        print("拒绝执行：以下目标不是普通文件，无法安全替换：" + "、".join(blockers),
              file=sys.stderr)
        return 2
    if existing and not args.force:
        print("拒绝执行：目标文件已存在，覆盖会替换既有 JSON 回滚基线："
              + "、".join(existing), file=sys.stderr)
        print("  确认要替换时加 --force；只想看目标时加 --dry-run。", file=sys.stderr)
        return 2

    if args.plaintext:
        # 警告必须在写盘**之前**：写完之后才提示，读者可能已经离开终端。
        print("⚠️  即将导出明文凭据（--plaintext），请立即转移到安全位置并删除该文件",
              file=sys.stderr)
    os.makedirs(args.out, exist_ok=True)
    for name, data in (("accounts.json", accounts), ("users.json", users)):
        path = os.path.join(args.out, name)
        flags = os.O_WRONLY | os.O_CREAT
        flags |= os.O_TRUNC if args.force else os.O_EXCL
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # 显式收紧权限：新建时由 os.open 0600 保证，已存在文件也统一覆盖为 0600
        os.chmod(path, 0o600)
    # 导出动作留痕审计链（此前全程零审计，事后无法证明谁在
    # 何时导出过数据）。尽力而为：审计失败不阻断导出（每日写入欠账告警兜底）。
    with contextlib.suppress(Exception):
        db.audit(
            "db-export-tool",
            "db_export",
            "",
            f"导出 {len(accounts)} 账号 / {len(users)} 用户"
            f"（{'明文凭据' if args.plaintext else '密文'}，escape hatch）",
        )
    print(f"已导出 {len(accounts)} 个账号 / {len(users)} 个用户 → {args.out}/")


if __name__ == "__main__":
    sys.exit(main())
