# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""YIBAN_ACCOUNTS_KEY 轮换工具：旧钥解密 → 新钥重加密 → 校验 → 更新 .env。

批次11 N5：SSH 失陷后攻击者可能已持有旧密钥（.env 为 0600 但 root 可读）。
运营者换钥后，若存量密文不重加密，新钥将无法解密旧数据（数据不可追回）；
本工具在单事务内完成全量重加密并自校验，最后才更新 .env。

用法（建议停服窗口执行：docker compose stop web scheduler，或停止对应 systemd
服务——避免轮换期间签到/探针进程用旧钥写入新密文造成混合密钥状态）：
    python3 scripts/rekey_accounts.py --generate
    python3 scripts/rekey_accounts.py --new-key <64位十六进制>
    python3 scripts/rekey_accounts.py --new-key-file newkey.txt   # 文件内容为首行密钥
    可选：--db yiban.db --env .env（默认取环境变量/默认路径）

流程（崩溃安全，.env 最后写）：
    1. 全量读 accounts 表，旧钥解密全部 password/phone_code——任何一行失败
       立即中止且不写库（密钥不对就不动数据）
    2. 单事务（BEGIN IMMEDIATE，busy_timeout 15s）用新钥重加密写回全部行
    3. 事务提交后全量用新钥解密，与第 1 步明文逐一比对
    4. 校验通过才更新 .env 的 YIBAN_ACCOUNTS_KEY（原子替换、0600）
    崩溃恢复：若在第 4 步前中断，.env 仍是旧钥而库内已是新钥密文——把 .env 的
    YIBAN_ACCOUNTS_KEY 临时改回旧钥即可恢复服务，随后重跑本工具补完第 4 步。

注意：
- 轮换后须重启所有使用该库的进程（web/signin/scheduler/tui），并同步更新
  环境变量里的 YIBAN_ACCOUNTS_KEY（环境变量优先级高于 .env，旧值会压过新钥）。
- 旧密钥视为已泄露：重加密不改变"泄露密钥曾可解密全部历史密文"的事实，
  攻击者若已拷贝数据库文件，历史数据仍应视为已泄露（需另行通知受影响用户改密）。
"""
import argparse
import contextlib
import json
import os
import secrets
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import account_crypto
import db


def _read_new_key(args):
    """从 --new-key / --new-key-file / --generate 之一取得新密钥 bytes（校验格式）。"""
    if sum(bool(x) for x in (args.new_key, args.new_key_file, args.generate)) != 1:
        print("错误：--new-key / --new-key-file / --generate 必须且只能提供一个")
        sys.exit(2)
    if args.generate:
        return secrets.token_bytes(32)
    raw = args.new_key
    if args.new_key_file:
        try:
            with open(args.new_key_file, encoding="utf-8") as f:
                raw = f.readline().strip()
        except OSError as e:
            print(f"错误：无法读取新密钥文件 {args.new_key_file}: {e}")
            sys.exit(2)
    try:
        return account_crypto._decode_key(raw)
    except ValueError as e:
        print(f"错误：新密钥格式非法: {e}")
        sys.exit(2)


def rekey(db_path, old_key, new_key):
    """全量重加密。返回 (ok, 摘要文本)；任何一步失败返回 False 且库保持旧状态。"""
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, phone, password, phone_code FROM accounts"
        ).fetchall()
        plain_map = {}  # id -> (plain_password, plain_phone_code)
        for r in rows:
            pw_plain, code_plain = "", ""
            for col, out in (("password", "pw"), ("phone_code", "code")):
                raw = r[col]
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except (TypeError, ValueError) as e:
                    return False, f"行 {r['id']} 的 {col} 不是合法 JSON（库可能未加密或已损坏）: {e}"
                if not account_crypto.is_encrypted(obj):
                    return False, f"行 {r['id']} 的 {col} 不是密文对象（库内存在明文，请先让服务完成加密自愈再轮换）"
                try:
                    plain = account_crypto.decrypt_password(obj, old_key, r["phone"])
                except ValueError as e:
                    return False, f"行 {r['id']} 的 {col} 用旧密钥解密失败: {e}"
                if out == "pw":
                    pw_plain = plain
                else:
                    code_plain = plain
            plain_map[r["id"]] = (pw_plain, code_plain)
        if not plain_map:
            return True, "库内无账号行，无需重加密"
        # 单事务重加密（AAD 仍绑手机号；手机号本轮不变更）
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("BEGIN IMMEDIATE")
        for rid, (pw_plain, code_plain) in plain_map.items():
            row = next(r for r in rows if r["id"] == rid)
            for col, plain in (("password", pw_plain), ("phone_code", code_plain)):
                if not plain and not row[col]:
                    continue  # 原为空，保持空
                enc = json.dumps(account_crypto.encrypt_password(plain, new_key, row["phone"]))
                conn.execute(f"UPDATE accounts SET {col}=? WHERE id=?", (enc, rid))
        conn.commit()
        # 提交后全量自校验：新钥解密必须与轮换前明文一致
        rows2 = conn.execute(
            "SELECT id, phone, password, phone_code FROM accounts"
        ).fetchall()
        for r in rows2:
            pw_plain, code_plain = plain_map[r["id"]]
            for col, expect in (("password", pw_plain), ("phone_code", code_plain)):
                if not expect and not r[col]:
                    continue
                obj = json.loads(r[col])
                got = account_crypto.decrypt_password(obj, new_key, r["phone"])
                if got != expect:
                    return False, f"自校验失败：行 {r['id']} 的 {col} 与轮换前明文不一致（请立即恢复备份并排查）"
        return True, f"重加密完成：{len(plain_map)} 个账号行 × 2 字段，自校验通过"
    except sqlite3.Error as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        return False, f"数据库操作失败（已回滚）: {e}"
    finally:
        conn.close()


def update_env_key(env_path, new_key):
    """把新密钥写入 .env（原子替换、0600；持 env_lock 与 web/tui 写 .env 互斥）。"""
    import env_lock

    with env_lock.env_write_lock(env_path):
        lines = []
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8-sig") as f:  # utf-8-sig：兼容 BOM
                lines = f.read().splitlines()
        out = [ln for ln in lines if not ln.strip().startswith("YIBAN_ACCOUNTS_KEY=")]
        out.append(f"YIBAN_ACCOUNTS_KEY={new_key.hex()}")
        tmp = f"{env_path}.tmp{secrets.token_hex(4)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, env_path)
        with contextlib.suppress(OSError):
            os.chmod(env_path, 0o600)


def main():
    parser = argparse.ArgumentParser(
        description="YIBAN_ACCOUNTS_KEY 轮换：旧钥解密 → 新钥重加密 → 自校验 → 更新 .env"
    )
    parser.add_argument("--db", default=None, help="yiban.db 路径（默认 YIBAN_DB_FILE/默认路径）")
    parser.add_argument("--env", default=None, help=".env 路径（默认 YIBAN_ENV_FILE/.env）")
    parser.add_argument("--new-key", default="", help="新密钥（64 位十六进制）")
    parser.add_argument("--new-key-file", default="", help="从文件首行读取新密钥")
    parser.add_argument("--generate", action="store_true", help="自动生成随机新密钥")
    parser.add_argument("--env-only", action="store_true",
                        help="仅更新 .env 密钥（不重加密；用于第 4 步中断后的补完）")
    args = parser.parse_args()

    db_path = args.db or os.environ.get("YIBAN_DB_FILE", db.DB_DEFAULT)
    env_path = args.env or os.environ.get("YIBAN_ENV_FILE", account_crypto.DEFAULT_ENV_FILE)
    if not os.path.exists(db_path):
        print(f"错误：数据库不存在: {db_path}（拒绝新建空库）")
        sys.exit(2)

    new_key = _read_new_key(args)

    # 旧密钥：环境变量/ .env 当前值（与 load_key 同优先级，但禁止"缺失时自动生成"）
    old_raw = os.environ.get("YIBAN_ACCOUNTS_KEY", "").strip()
    if not old_raw:
        old_raw = account_crypto._parse_env_file(env_path).get("YIBAN_ACCOUNTS_KEY", "").strip()
    if not old_raw:
        print("错误：未找到当前密钥（环境变量与 .env 均无 YIBAN_ACCOUNTS_KEY）")
        sys.exit(2)
    try:
        old_key = account_crypto._decode_key(old_raw)
    except ValueError as e:
        print(f"错误：当前密钥格式非法: {e}")
        sys.exit(2)

    if new_key == old_key:
        print("错误：新密钥与当前密钥相同，无需轮换")
        sys.exit(2)

    print(f"目标库: {db_path}\n目标 .env: {env_path}")
    if not args.env_only:
        ok, note = rekey(db_path, old_key, new_key)
        print(note)
        if not ok:
            print("轮换中止：库未变更、.env 未变更。")
            sys.exit(1)
    update_env_key(env_path, new_key)
    print("已更新 .env 的 YIBAN_ACCOUNTS_KEY。")
    print("后续步骤：重启 web/signin/scheduler 等全部进程；若 shell/容器环境变量中"
          "仍设有旧 YIBAN_ACCOUNTS_KEY，请同步更新（环境变量优先于 .env）。")
    print("提醒：旧密钥应视为已泄露——若攻击者曾拷贝数据库文件，历史密文仍需按"
          "泄露处理（通知用户改密）。")


if __name__ == "__main__":
    main()
