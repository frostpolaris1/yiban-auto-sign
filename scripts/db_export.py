# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""旧 SQLite → JSONL 导出器（P8-A）：迁移窗口的一次性逃生门。

用法：python3 scripts/db_export.py [--out 输出目录] [--db yiban.db 路径]
       [--force] [--dry-run]

输出（一个目录，七个文件；产物不入仓库，落运行目录或 work/）：
- users.jsonl / schools.jsonl / accounts.jsonl / time_prefs.jsonl / audit.jsonl：
  每行一个 JSON 对象（键排序，UTF-8），行序固定（幂等）。
- reconciliation.jsonl：对账表，每行 {"table","key","hash"}，与导入器（P8-B）
  产出的对账表逐行比对。
- summary.json：行数、孤儿/重复/软删/明文驻留计数与对账口径声明。

契约（与 P8-B 导入器钉死，2026-10-27 派单稿「切片拆分」节）：
- accounts 行的 password/phone_code 只允许两种形态：空串，或旧信封对象
  （v/kid/nonce/ct/tag 十六进制，AAD=手机号）。源库中解析不出信封形状的
  值（明文驻留/损坏）一律置空串并在 summary.json 计数，绝不透传原串。
- 对账哈希：手机号先掩码后哈希。掩码＝11 位号码前 3 后 4、中间 4 个 `*`
  （yiban.core.masking.mask_phone 同一实现）；哈希＝sha256，规范串为
  `table \\x1f key \\x1f` + `\\x1e` 连接的 `k=v`（键取导出行全字段排序，
  phone 字段先掩码）。key：users/accounts/audit 用行 id（十进制串），
  time_prefs 用掩码手机号。
- schools：旧库 schema 无学校表、accounts 无学校引用列（考古见
  migrations.py 基线建表），按契约仍产出空 .jsonl；旧库真实存在的孤儿
  形状是「time_prefs.phone 无对应账号」，按此口径计数。

安全设计：
- 全程只读：connection.open_readonly 打开，不 init_db、不迁移、不触发清理、
  不做明文自愈回写、不写导出留痕审计（旧工具经 init_db 写审计链，会让
  "导出"变成对源库的写动作；本工具以 summary.json 与 stdout 留痕）。
- 输出文件 0600 创建；默认拒绝覆盖既有输出，--force 才替换。
- 明文口令/验证码不存在于任何导出物（判据 tests/test_db_export_jsonl.py）。
"""
import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from yiban.adapters.config import env_io
from yiban.adapters.persistence import connection
from yiban.core.masking import mask_phone

#: 四张导出表：(导出名, 库内表名, 输出文件名, 排序, 行键字段)。
#: schools 不在表内：旧库无此表，按契约产出空文件（见模块说明）。
_TABLES = (
    ("users", "users", "users.jsonl", "id", "id"),
    ("accounts", "accounts", "accounts.jsonl", "sort_order, id", "id"),
    ("time_prefs", "time_prefs", "time_prefs.jsonl", "phone", "phone"),
    ("audit", "audit_logs", "audit.jsonl", "id", "id"),
)
_REQUIRED = {t[1] for t in _TABLES}
_OUTPUT_NAMES = (*tuple(t[2] for t in _TABLES), "schools.jsonl",
                 "reconciliation.jsonl", "summary.json")
#: 凭据字段：值必须是空串或旧信封对象；其他形状置空并计数。
_CREDENTIAL_FIELDS = ("password", "phone_code")
_UNIT = "\x1f"
_ITEM = "\x1e"


def _read_rows(conn, table, order_by):
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}")]
    return rows


def _missing_tables(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    return sorted(_REQUIRED - names)


def _envelope_or_empty(raw):
    """凭据字段 → (导出值, 是否明文驻留)。信封形状见模块说明；其余一律置空。"""
    if not raw:
        return "", False
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        obj = None
    if isinstance(obj, dict) and {"v", "nonce", "ct", "tag"} <= set(obj):
        return obj, False
    return "", True


def _row_key(key_field, row):
    """对账行键：id 表用十进制串；time_prefs 用掩码手机号（对账表零手机号明文）。"""
    value = str(row[key_field])
    return mask_phone(value) if key_field == "phone" else value


def _recon_hash(table, key, row):
    """对账哈希：row 的 phone 字段先掩码再进规范串；口径见模块说明。

    字段值序列化：字符串原样；其他类型（信封对象等）用 sort_keys 的紧凑 JSON——
    dict 的 repr 依赖键序，JSONL 里的键序（已排序）与源库解析出的键序不同，
    两侧必须用同一份序列化才能逐行比对。
    """
    masked = {k: (mask_phone(str(v)) if k == "phone" else v)
              for k, v in sorted(row.items())}
    body = _ITEM.join(f"{k}={v if isinstance(v, str) else _dump_line(v)}"
                      for k, v in masked.items())
    canonical = f"{table}{_UNIT}{key}{_UNIT}{body}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _dump_line(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _collect(conn):
    """读全部表 → (每表导出行, 对账行, 汇总)。纯函数：输入定则输出定（幂等）。"""
    exported, recon = {}, []
    account_phones = set()
    plaintext = {f"accounts.{f}": 0 for f in _CREDENTIAL_FIELDS}
    for table, db_table, _name, order_by, key_field in _TABLES:
        rows = []
        for raw in _read_rows(conn, db_table, order_by):
            if table == "accounts":
                for field in _CREDENTIAL_FIELDS:
                    raw[field], resident = _envelope_or_empty(raw.get(field))
                    if resident:
                        plaintext[f"accounts.{field}"] += 1
                account_phones.add(str(raw["phone"]))
            row = dict(raw)
            rows.append(row)
            key = _row_key(key_field, row)
            recon.append({"table": table, "key": key,
                          "hash": _recon_hash(table, key, row)})
        exported[table] = rows
    prefs = {str(r["phone"]) for r in exported["time_prefs"]}
    orphans = sum(1 for p in prefs if p not in account_phones)
    dup = {}
    for table, field in (("accounts", "phone"), ("users", "email")):
        seen = set()
        dup[table] = 0
        for row in exported[table]:
            if row[field] in seen:
                dup[table] += 1
            seen.add(row[field])
    summary = {
        "format": "yiban-legacy-export/1",
        "mask_rule": "mask_phone: 11-digit CN mobile -> first3 + '****' + last4; "
                     "already-masked or non-phone values unchanged",
        "recon_hash": "sha256 hex of canonical string: table + 0x1f + key + 0x1f "
                      "+ 0x1e-joined 'k=v' over all exported fields sorted by key; "
                      "phone values masked before hashing; string values verbatim, "
                      "non-string values as sort_keys compact JSON",
        "rows": {t[0]: len(exported[t[0]]) for t in _TABLES} | {"schools": 0},
        "orphans": {"time_prefs_phone_without_account": orphans},
        "duplicates": {"accounts_by_phone": dup["accounts"],
                       "users_by_email": dup["users"]},
        "soft_deleted": {
            "accounts": sum(1 for r in exported["accounts"] if r.get("deleted")),
            "users": sum(1 for r in exported["users"] if r.get("deleted")),
        },
        "plaintext_resident": plaintext,
    }
    return exported, recon, summary


def _blockers(out_dir):
    return [os.path.join(out_dir, n) for n in _OUTPUT_NAMES
            if os.path.exists(os.path.join(out_dir, n)) and not os.path.isfile(
                os.path.join(out_dir, n))]


def _existing(out_dir):
    return [os.path.join(out_dir, n) for n in _OUTPUT_NAMES
            if os.path.isfile(os.path.join(out_dir, n))]


def _write_all(out_dir, exported, recon, summary, force):
    os.makedirs(out_dir, exist_ok=True)
    payloads = {name: "".join(_dump_line(obj) + "\n" for obj in exported[table])
                for table, _db, name, _o, _k in _TABLES}
    payloads["schools.jsonl"] = ""
    payloads["reconciliation.jsonl"] = "".join(
        _dump_line(line) + "\n" for line in recon)
    payloads["summary.json"] = _dump_line(summary) + "\n"
    for name in _OUTPUT_NAMES:
        path = os.path.join(out_dir, name)
        flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payloads[name])
        os.chmod(path, 0o600)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="旧 SQLite → JSONL 导出器（迁移用；全程只读，明文零落盘）")
    parser.add_argument("--out", default=".", help="输出目录（默认当前目录；建议显式指定）")
    parser.add_argument("--db", default=None, help="yiban.db 路径（默认环境变量/.env/默认路径）")
    parser.add_argument(
        "--force", action="store_true",
        help="允许覆盖输出目录里已存在的导出文件（默认拒绝）")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只报告将写出的目标与行数，不写盘")
    args = parser.parse_args(argv)
    if args.db:
        # 唯一写环境变量的地方：路径解析唯一入口仍是 env_io.resolve_path
        #（进程环境 → .env → 默认值，scripts/gate/shared-facts.tsv 口径）。
        os.environ["YIBAN_DB_FILE"] = args.db
    db_path = env_io.resolve_path("YIBAN_DB_FILE", env_io.DEFAULT_DB_FILE)
    conn = connection.open_readonly(db_path)
    if conn is None:
        print(f"拒绝执行：数据库文件不存在: {db_path}", file=sys.stderr)
        return 2
    try:
        missing = _missing_tables(conn)
        if missing:
            print("拒绝执行：缺表 " + "、".join(missing)
                  + "；旧 schema 库请用配套版本的导出工具。", file=sys.stderr)
            return 2
        exported, recon, summary = _collect(conn)
    finally:
        conn.close()

    if args.dry_run:
        print("[dry-run] 将导出 " + " / ".join(
            f"{len(exported[t[0]])} 行 {t[0]}" for t in _TABLES) + f" → {args.out}/")
        for name in _OUTPUT_NAMES:
            print(f"  {os.path.join(args.out, name)}")
        return 0

    blockers = _blockers(args.out)
    if blockers:
        print("拒绝执行：以下目标不是普通文件，无法安全替换：" + "、".join(blockers),
              file=sys.stderr)
        return 2
    existing = _existing(args.out)
    if existing and not args.force:
        print("拒绝执行：目标文件已存在，覆盖会替换既有导出物："
              + "、".join(existing), file=sys.stderr)
        print("  确认要替换时加 --force；只想看目标时加 --dry-run。", file=sys.stderr)
        return 2
    _write_all(args.out, exported, recon, summary, force=args.force)
    print("已导出 " + " / ".join(
        f"{len(exported[t[0]])} 行 {t[0]}" for t in _TABLES)
        + f"；孤儿 {summary['orphans']['time_prefs_phone_without_account']}"
        + f"；明文驻留置空 {sum(summary['plaintext_resident'].values())}"
        + f" → {args.out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
