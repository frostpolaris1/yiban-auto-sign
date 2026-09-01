# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""YIBAN_ACCOUNTS_KEY 轮换工具：旧钥解密 → 新钥重加密 → 校验 → 更新 .env。

批次11 N5：SSH 失陷后攻击者可能已持有旧密钥（.env 为 0600 但 root 可读）。
运营者换钥后，若存量密文不重加密，新钥将无法解密旧数据（数据不可追回）；
本工具在单事务内完成全量重加密并自校验，最后才更新 .env。

用法（务必先停服——批次12 B12-6/B12-5：Docker 用 `docker compose stop yiban`，
systemd 用 `systemctl stop yiban-web`；容器内 web/scheduler 是 supervisord 子进程，
`stop web scheduler` 服务名不存在。不停服时签到/探针进程会用旧钥写入新密文，
造成"混合密钥状态"：切钥后新行永久不可解。工具也会扫描进程并在发现存活的
web/signin/scheduler 时拒绝执行，--force 可跳过该探活（自担风险））：
    python3 scripts/rekey_accounts.py --generate
    python3 scripts/rekey_accounts.py --new-key <64位十六进制>   # 批次17 P3-2：密钥会暴露在
            # 进程列表（ps / /proc/<pid>/cmdline）与 shell 历史，同机其他用户可读；
            # 工具会告警并尽力擦除 argv，仍建议优先使用 --new-key-file
    python3 scripts/rekey_accounts.py --new-key-file newkey.txt   # 文件内容为首行密钥
    可选：--db yiban.db --env .env（默认取环境变量/默认路径；显式指定的 --env
           必须是已存在的文件，路径打错时工具直接拒绝，不会在该路径新建 .env
           并生成新审计密钥，批次14 修复轮1③）
    可选：--skip-notify（不迁移推送密文 YIBAN_NOTIFY_SECRET_ENC，批次14 P2-2）

流程（崩溃安全，.env 最后写；批次12 B12-5 加固）：
    0. 新钥生成后**立即写入 0600 暂存文件**（<env>.rekey-staging）——
       此前 --generate 的新钥只存在于内存，第 2 步提交后、第 4 步写 .env 前
       崩溃 = 新钥永久丢失，库内密文随之整体不可解（仅剩 ≤24h 备份可救）。
    1. 全量读 accounts 表，旧钥解密全部 password/phone_code——任何一行失败
       立即中止且不写库（密钥不对就不动数据）
    2. 单事务（BEGIN IMMEDIATE，busy_timeout 15s）用新钥重加密写回全部行
    3. 事务提交后全量用新钥解密，与第 1 步明文逐一比对
    4. 校验通过才更新 .env 的 YIBAN_ACCOUNTS_KEY（原子替换、0600），随后
       删除暂存文件
    4b. 推送密文随轮换迁移（批次14 P2-2）：.env 里的 YIBAN_NOTIFY_SECRET_ENC
       （Server酱 SendKey / 自定义 webhook URL，用 YIBAN_ACCOUNTS_KEY 加密）
       先以旧钥解密、再以新钥重加密，与账号密钥**同一次原子替换**落盘。
       漏了这一步 = 换钥后 notify.get_secret() 解不开而静默返回空，推送通道
       无声死亡（恰在盗号/异常告警最需要它的时候）。该键未配置或解不开时
       不中止轮换（首要目标是账号凭据不丢），只记 ERROR 并在收尾自检行提示
       "需在设置页重新配置消息推送"；--skip-notify 可显式跳过本步。
       读-解密-重加密-写回整段在**同一把 env_lock 内**完成（批次14 修复轮1②）：
       否则 --force 不停服轮换时，期间设置页改过的推送配置会被工具启动时的
       陈旧快照覆盖回去。
    崩溃恢复（按中断点区分，批次12 修正——旧文案"改回旧钥即可恢复"对第 2 步
    之后的中断是**错误**指引，库内已是新钥密文，旧钥解不开）：
    - 第 2 步提交**前**中断：库未变更，.env 旧钥仍然有效，直接重跑本工具即可；
    - 第 2 步提交**后**、第 4 步前中断：库内已是新钥密文，.env 仍是旧钥——
      新钥就在暂存文件 <env>.rekey-staging（0600）里，把它写回 .env 的
      YIBAN_ACCOUNTS_KEY（或重跑 `--env-only --new-key-file <暂存文件>` 补完），
      服务即可恢复。
    --env-only 补完前会用新钥**抽样试解一行库内密文**，密钥不对即拒绝写 .env
    （防误传新生成的随机钥造成第三把钥、彻底不可恢复）。

注意：
- 轮换后须重启所有使用该库的进程（web/signin/scheduler/tui），并同步更新
  环境变量里的 YIBAN_ACCOUNTS_KEY（环境变量优先级高于 .env，旧值会压过新钥）。
- 旧密钥视为已泄露：重加密不改变"泄露密钥曾可解密全部历史密文"的事实，
  攻击者若已拷贝数据库文件，历史数据仍应视为已泄露（需另行通知受影响用户改密）。
"""
import argparse
import contextlib
import json
import logging
import os
import secrets
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import account_crypto
import db

logger = logging.getLogger("yiban.rekey")


# 进程探活的关键字：命中即认为可能有进程持旧钥运行（批次12 B12-5）。
# 匹配对象是 /proc/*/cmdline（Linux）；覆盖容器（gunicorn web.app /
# container_scheduler / scripts/signin.py）与裸机（web/app.py / tui）两种部署
# 形态。刻意不含宽泛的 "yiban"（仓库路径本身含 yiban，会误报无关进程）。
_PROCESS_HINTS = (
    "gunicorn",
    "web.app",
    "web/app.py",
    "signin.py",
    "scheduler.py",
    "tui",
)


def _yiban_processes_running():
    """粗粒度探活：扫描 /proc 找可能持旧钥的存活进程。

    返回 (supported, hits)：supported=False 表示当前平台无法判定（非 Linux /
    无 /proc），调用方降级为警告；hits 为 [(pid, cmdline 前 120 字符)]。
    刻意保守：宁可误报（有 --force 可跳过），不可漏报——不停服轮换的代价是
    混合密钥态（新行切钥后永久不可解）。
    """
    if os.name == "nt" or not os.path.isdir("/proc"):
        return False, []
    hits = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        if not cmd or "rekey_accounts" in cmd:
            continue
        low = cmd.lower()
        if any(h in low for h in _PROCESS_HINTS):
            hits.append((pid, cmd[:120]))
    return True, hits


def _staging_path(env_path):
    return f"{env_path}.rekey-staging"


def _write_staging_key(env_path, new_key):
    """新钥落 0600 暂存文件（崩溃恢复的事实源，批次12 B12-5）。

    返回暂存路径；写入失败抛 OSError（调用方中止——没有暂存就轮换等于
    把"崩溃丢钥"窗口敞开）。
    """
    path = _staging_path(env_path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new_key.hex() + "\n")
        f.flush()
        os.fsync(f.fileno())
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return path


def _read_new_key(args):
    """从 --new-key / --new-key-file / --generate 之一取得新密钥 bytes（校验格式）。

    批次17 P3-2：--new-key 把密钥写进进程 argv，对同机其他用户可见
    （ps / /proc/<pid>/cmdline / shell 历史 / 终端回滚）——读钥时醒目告警并
    建议改用 --new-key-file（0600 文件，首行为密钥）；调用方随后应 _wipe_argv()。
    """
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
    elif args.new_key:
        print(
            "警告：通过 --new-key 传入的新密钥会暴露在进程列表与 shell 历史中，"
            "同机其他用户可读取；建议改用 --new-key-file 从 0600 文件读取"
            "（文件首行为密钥），本工具会在读取后尽力擦除 argv。",
            file=sys.stderr,
        )
    try:
        return account_crypto._decode_key(raw)
    except ValueError as e:
        print(f"错误：新密钥格式非法: {e}")
        sys.exit(2)


def _wipe_argv():
    """尽力覆写 C argv 内存，使 /proc/<pid>/cmdline 不再显示 argv 里的密钥。

    Python 层改 sys.argv 只改到解释器内部的 str 拷贝，内核按进程启动时的
    C argv 内存区填充 /proc/<pid>/cmdline——要真正抹掉必须直接覆写那块内存。
    Linux/glibc 下经 __libc_argv 取到 argv 指针数组逐串清零；其它 libc /
    平台或失败时静默返回（无法判定是否成功，但暴露告警已在 _read_new_key 给出）。
    返回 True 表示已执行覆写；False 表示本平台不可行或失败。
    """
    if os.name != "posix":
        return False
    try:
        import ctypes
        libc = ctypes.CDLL(None)
        argv_var = ctypes.c_void_p.in_dll(libc, "__libc_argv")
        argv_base = ctypes.cast(argv_var, ctypes.POINTER(ctypes.c_void_p))[0]
        argv = ctypes.cast(argv_base, ctypes.POINTER(ctypes.c_char_p))
        for i in range(len(sys.argv)):
            buf = argv[i]
            if buf is None or buf.value is None:
                continue
            length = len(sys.argv[i].encode("utf-8", "replace"))
            if length:
                ctypes.memset(buf, 0, length)
        return True
    except Exception:
        return False


def rekey(db_path, old_key, new_key):
    """全量重加密。返回 (ok, 摘要文本)；任何一步失败返回 False 且库保持旧状态。

    批次12 B12-5：自校验循环改用 .get 读轮换前明文映射——原实现 plain_map[r["id"]]
    直接下标，快照 SELECT 与 BEGIN IMMEDIATE 之间如有进程写入新行（停服被忽略
    时的竞态），新行不在映射内会抛 KeyError 且只捕获 sqlite3.Error → 崩溃，
    库停留在混合密钥状态。现在：快照后出现的新行先按"旧钥能否解开"判定——
    能解开则一并重加密，不能则中止（该行是旧钥写入，下一轮再轮换），
    杜绝静默混合态。
    """
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
        # 竞态防御：BEGIN IMMEDIATE 后重读一遍行清单，快照窗口期新写入的行
        # 不在 plain_map 内——旧钥能解开的并入本轮重加密，解不开的说明是
        # 无法用旧钥处理的异常状态，中止并回滚（绝不留下混合密钥态）
        rows_now = conn.execute(
            "SELECT id, phone, password, phone_code FROM accounts"
        ).fetchall()
        new_rows = [r for r in rows_now if r["id"] not in plain_map]
        for r in new_rows:
            for col in ("password", "phone_code"):
                raw = r[col]
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                    if account_crypto.is_encrypted(obj):
                        account_crypto.decrypt_password(obj, old_key, r["phone"])
                    else:
                        return False, (
                            f"轮换窗口期发现新行 {r['id']} 的 {col} 为明文（并发写入）——"
                            "已中止且库保持旧状态，请停服后重跑"
                        )
                except (TypeError, ValueError) as e:
                    return False, (
                        f"轮换窗口期发现新行 {r['id']} 的 {col} 无法用旧钥处理（并发写入）: {e}"
                        "——已中止且库保持旧状态，请停服后重跑"
                    )
            plain_map[r["id"]] = (
                _decrypted_or_empty(r, "password", old_key),
                _decrypted_or_empty(r, "phone_code", old_key),
            )
            rows = [*rows, r]
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
            if r["id"] not in plain_map:
                return False, (
                    f"自校验发现未知行 {r['id']}（轮换期间被并发写入）——"
                    "请立即用备份恢复并排查；该行未纳入本轮重加密"
                )
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


def _decrypted_or_empty(row, col, key):
    """竞态防御辅助：按列解密（已在上游验证可解），失败返回空串（保持原值语义）。"""
    raw = row[col]
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
        if account_crypto.is_encrypted(obj):
            return account_crypto.decrypt_password(obj, key, row["phone"])
    except (TypeError, ValueError):
        pass
    return ""


def sample_verify_key(db_path, key):
    """用待写入 .env 的密钥抽样试解一行库内密文（批次12 B12-5）。

    --env-only 原实现不校验即覆盖 .env：崩溃补完场景误传 --generate（新随机钥）
    会造成"第三把钥"，全量数据彻底不可恢复。返回 (ok, message)。
    """
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, phone, password FROM accounts WHERE password != '' LIMIT 1"
        ).fetchone()
        if row is None:
            return True, "库内无加密行可抽样（跳过样本校验）"
        try:
            obj = json.loads(row["password"])
        except (TypeError, ValueError) as e:
            return False, f"样本行 {row['id']} 的 password 不是合法 JSON（库可能已损坏）: {e}"
        if not account_crypto.is_encrypted(obj):
            return False, f"样本行 {row['id']} 的 password 不是密文对象（库内存在明文，请先完成加密自愈）"
        try:
            account_crypto.decrypt_password(obj, key, row["phone"])
        except ValueError as e:
            return False, (
                f"样本校验失败：该密钥解不开库内密文（行 {row['id']}）: {e}\n"
                "已拒绝更新 .env——若为崩溃补完，请确认密钥来源（勿传新生成的随机钥；"
                "正确密钥应在 .rekey-staging 暂存文件中）"
            )
        return True, "样本校验通过（新钥可解开库内密文）"
    except sqlite3.Error as e:
        return False, f"样本校验查询失败: {e}"
    finally:
        conn.close()


def _write_env_key(env_path, new_key, extra=None):
    """把新密钥（及 extra 里的其它键）原子写回 .env。

    **调用方必须已持有 env_lock.env_write_lock(env_path)**——本函数不加锁，
    目的是让"读现值 → 算新值 → 落盘"能整体收在同一把锁里（见 rotate_and_write_env）。
    extra 值为 None/空串的项跳过不写（不删除既有键：删除语义归设置页）。
    """
    extra = {k: v for k, v in (extra or {}).items() if v}
    lines = []
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8-sig") as f:  # utf-8-sig：兼容 BOM
            lines = f.read().splitlines()
    changed = ["YIBAN_ACCOUNTS_KEY", *extra]
    out = [ln for ln in lines if not ln.strip().startswith(tuple(f"{k}=" for k in changed))]
    out.append(f"YIBAN_ACCOUNTS_KEY={new_key.hex()}")
    for k, v in extra.items():
        out.append(f"{k}={v}")
    tmp = f"{env_path}.tmp{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, env_path)
    with contextlib.suppress(OSError):
        os.chmod(env_path, 0o600)


def update_env_key(env_path, new_key, extra=None):
    """把新密钥写入 .env（原子替换、0600；持 env_lock 与 web/tui 写 .env 互斥）。

    extra：其它需一并落盘的 .env 键值（批次14 P2-2 用它写入用新钥重加密后的
    YIBAN_NOTIFY_SECRET_ENC）。刻意与 YIBAN_ACCOUNTS_KEY 合进同一次原子替换——
    分两次写就会出现"新钥已落盘、推送密文还是旧钥的"中间态（换钥后通道静默死亡）。
    extra 的值必须是**调用方在锁内读到的现值算出来的**；若需要在写盘前读 .env，
    请改用 rotate_and_write_env（批次14 修复轮1②）。
    """
    import env_lock

    with env_lock.env_write_lock(env_path):
        _write_env_key(env_path, new_key, extra)


# 推送密钥在 .env 中的键名（值 = json.dumps(account_crypto.encrypt_text(...))）
NOTIFY_ENC_KEY = "YIBAN_NOTIFY_SECRET_ENC"
# 收尾自检行文案（批次14 P2-2）：键为 rotate_notify_secret 返回的状态
NOTIFY_SELF_CHECK_NOTE = {
    "rotated": "已随换钥迁移（消息推送无需重新配置）",
    "unset": "未配置（无需迁移）",
    "failed": "需重新配置：旧密文用换钥前的密钥解不开，请在设置页重新配置消息推送",
    "skipped": "需重新配置：本次按 --skip-notify 未迁移，换钥后请在设置页重新配置消息推送",
}


def rotate_notify_secret(env_path, old_key, new_key, skip=False):
    """换钥时同步重加密推送密文；返回 (state, new_raw)，new_raw=None 表示不改动该键。

    为什么必须做（批次14 P2-2）：Server酱 SendKey / 自定义 webhook URL 是用
    YIBAN_ACCOUNTS_KEY 加密后存进 .env 的。轮换账号密钥而不重加密，
    notify.get_secret() 会解不开并返回空——推送通道【静默死亡】，运营者在最需要
    通知的时候（盗号/异常告警）收不到任何消息，且日志里只有一条 WARNING。

    刻意"尽力而为"（用户裁决）：首要目标是账号凭据不丢，本步骤任何失败都不得
    让轮换本身失败，只记 ERROR 并在收尾自检行提示需在设置页重新配置。
    因此连"读 .env"这一步也要包起来：account_crypto._parse_env_file 对"文件存在
    但读取失败"（权限/占用）会**重抛 OSError**（那是 load_key 侧刻意的 fail-loud，
    防误判未配置而生成新钥覆盖旧钥），而这里正处在"库里已是新钥、.env 尚未写"
    的窗口——让它穿透 main 就会留下"库=新钥 / env=旧钥"的不一致态（修复轮1①）。
    读失败同样报"未迁移，需在设置页重新配置"，轮换本身继续成功（--skip-notify 时
    本就不迁移，读失败仍按"跳过"计）。

    调用方必须在 env_lock 写锁内调用本函数（修复轮1②）：读到的必须是即将被
    覆盖的那份 .env 的现值，否则不停服轮换时会用陈旧快照盖掉期间设置页改过的配置。
    """
    try:
        raw = account_crypto._parse_env_file(env_path).get(NOTIFY_ENC_KEY, "").strip()
    except Exception as e:  # 尽力而为：读不到密文只影响推送通道，绝不拖垮账号凭据轮换
        if skip:
            # --skip-notify 本就不迁移，读失败不改变结论
            return "skipped", None
        logger.error(
            "推送密钥无法随轮换重加密（未配置或已损坏），换钥后需在设置页重新配置消息推送"
            "（.env 读取失败）: %s", e
        )
        return "failed", None
    if not raw:
        return "unset", None
    if skip:
        return "skipped", None
    try:
        plain = account_crypto.decrypt_text(json.loads(raw), old_key)
        enc = account_crypto.encrypt_text(plain, new_key)
        return "rotated", json.dumps(enc, ensure_ascii=False)
    except Exception as e:  # 尽力而为：不得因推送配置拖垮轮换（首要目标是账号凭据不丢）
        logger.error(
            "推送密钥无法随轮换重加密（未配置或已损坏），换钥后需在设置页重新配置消息推送: %s", e
        )
        return "failed", None


def rotate_and_write_env(env_path, new_key, old_key, skip_notify=False):
    """在**同一把 env 写锁内**读现值 → 迁移推送密文 → 与新账号钥一次原子落盘。

    返回收尾自检状态（NOTIFY_SELF_CHECK_NOTE 的键）。
    为什么读也要放进锁里（批次14 修复轮1②）：本工具正常路径要求停服，但 --force
    是文档允许的用法，不停服时设置页可能随时重写 YIBAN_NOTIFY_SECRET_ENC；
    锁外快照 + 锁内写入 = 把用户期间的修改覆盖回旧值。
    """
    import env_lock

    with env_lock.env_write_lock(env_path):
        state, raw = rotate_notify_secret(env_path, old_key, new_key, skip=skip_notify)
        _write_env_key(env_path, new_key, {NOTIFY_ENC_KEY: raw} if raw else None)
    return state


def _audit_rotate(db_path, action, detail, env_file=None):
    """轮换结果写入审计链（批次12 B12-14：rekey 此前全程零审计）。尽力而为：
    审计失败不阻断轮换结果（失败会由每日校验的写入欠账告警兜住）。

    env_file 应由调用方显式传入（批次14 P2-5）：留痕要用真实密钥源。留空时 db 层只能按
    YIBAN_ENV_FILE → 当前目录 ".env" 回落，在应用根之外运行会读不到旧钥、就地
    生成游离密钥，这条审计行随之用错密钥签名——取证工具反过来破坏取证对象。
    """
    try:
        db.init_db(db_file=db_path, cleanup=False, migrate=False, env_file=env_file)
        db.audit("rekey-tool", action, "", detail[:200])
    except Exception as e:
        print(f"提示：审计留痕失败（不影响轮换结果）: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="YIBAN_ACCOUNTS_KEY 轮换：旧钥解密 → 新钥重加密 → 自校验 → 更新 .env"
    )
    parser.add_argument("--db", default=None, help="yiban.db 路径（默认 YIBAN_DB_FILE/默认路径）")
    parser.add_argument("--env", default=None,
                        help="密钥来源 .env 路径（默认 YIBAN_ENV_FILE/当前目录 .env）；"
                             "既是账号密钥也是审计密钥来源，在应用根之外运行时请显式指定"
                             "（批次14 P2-5）；显式指定时该文件必须已存在（修复轮1③，"
                             "路径打错直接拒绝，不在错误位置新建密钥）")
    parser.add_argument("--new-key", default="",
                        help="新密钥（64 位十六进制）；会暴露在进程列表与 shell 历史，"
                             "建议改用 --new-key-file（批次17 P3-2）")
    parser.add_argument("--new-key-file", default="", help="从文件首行读取新密钥")
    parser.add_argument("--generate", action="store_true", help="自动生成随机新密钥")
    parser.add_argument("--skip-notify", action="store_true",
                        help="不迁移推送密文 YIBAN_NOTIFY_SECRET_ENC（批次14 P2-2 默认会"
                             "用新钥重加密；跳过则换钥后须在设置页重新配置消息推送）")
    parser.add_argument("--env-only", action="store_true",
                        help="仅更新 .env 密钥（不重加密；用于第 4 步中断后的补完，"
                             "会先用新钥抽样试解库内密文）")
    parser.add_argument("--force", action="store_true",
                        help="跳过存活进程探活（确认已停服/接受混合密钥风险时使用）")
    args = parser.parse_args()

    db_path = args.db or os.environ.get("YIBAN_DB_FILE", db.DB_DEFAULT)
    # 批次14 修复轮1④：--env / YIBAN_ENV_FILE 统一 strip 后**只解析一次**，账号钥与
    # 审计钥共用同一结果。此前 env_path 不 strip、key_source 走 strip，
    # YIBAN_ENV_FILE 为空串或带空白时两者会指向不同文件（账号钥写 A、审计钥读 B）。
    # 无任何显式指定时 key_source=None（交给 db 层回落链判定并触发防游离检查），
    # 而账号钥必须有一个具体路径可读写，故 env_path 才兜底到 DEFAULT_ENV_FILE。
    try:
        key_source = db.require_existing_env_file(args.env)
    except ValueError as e:
        # 修复轮1③：显式 --env 指向不存在的文件 → 立即退出。本工具随后会写 .env、
        # 写暂存文件、写审计链，路径打错时若继续就会在该位置凭空造出一份密钥源，
        # 把留痕用第三把钥匙签坏（正是本任务要治的病症）。
        print(f"错误：{e}")
        sys.exit(2)
    env_path = key_source or account_crypto.DEFAULT_ENV_FILE
    if not os.path.exists(db_path):
        print(f"错误：数据库不存在: {db_path}（拒绝新建空库）")
        sys.exit(2)

    new_key = _read_new_key(args)
    # 批次17 P3-2：密钥一旦读入内存就立刻擦除 argv——进程存活期间
    # ps / /proc/<pid>/cmdline 对同机其他用户可见，越早抹越短暴露窗口。
    # --new-key-file 路径同样调用（argv 里无密钥时是无害空操作）。
    _wipe_argv()

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

    # 停服探活（批次12 B12-5）：不停服轮换 = 签到/探针用旧钥写新密文 → 混合密钥态
    supported, hits = _yiban_processes_running()
    if supported and hits:
        print("错误：检测到可能仍在运行的 yiban 相关进程，拒绝轮换（防止混合密钥状态）：")
        for pid, cmd in hits[:10]:
            print(f"  pid={pid}  {cmd}")
        print("请先停服：docker compose stop yiban（Docker）/ systemctl stop yiban-web（裸机），"
              "或确认均为无关进程后用 --force 强制执行。")
        sys.exit(2)
    if not supported and not args.force:
        print("警告：当前平台无法探活 yiban 进程，请自行确认已停服"
              "（docker compose stop yiban / systemctl stop yiban-web）后再继续；"
              "或用 --force 跳过本提示。")

    staging = None
    if os.path.exists(_staging_path(env_path)):
        print(f"提示：发现此前的暂存文件 {_staging_path(env_path)}——上次轮换可能未完成，"
              "其内容是上次生成的新钥，可用于恢复或补完。")
    if not args.env_only:
        try:
            staging = _write_staging_key(env_path, new_key)
            os.chmod(staging, 0o600)
        except OSError as e:
            print(f"错误：新钥暂存文件写入失败（中止轮换——没有暂存就没有崩溃恢复）: {e}")
            sys.exit(1)
        print(f"新钥已暂存: {staging}（0600；轮换完成后自动删除）")

    print(f"目标库: {db_path}\n目标 .env: {env_path}")
    if not args.env_only:
        ok, note = rekey(db_path, old_key, new_key)
        print(note)
        if not ok:
            print("轮换中止：库未变更、.env 未变更。"
                  f"新钥仍保留在暂存文件 {staging}，可核查后删除。")
            _audit_rotate(db_path, "accounts_key_rekey_failed", note, env_file=key_source)
            sys.exit(1)
    else:
        # --env-only 补完：先抽样验证新钥确实解得开库内密文，再写 .env
        ok, note = sample_verify_key(db_path, new_key)
        print(note)
        if not ok:
            print("--env-only 中止：.env 未变更。")
            sys.exit(1)
    # 推送密文随换钥迁移（批次14 P2-2）：必须在写 .env 之前算好，与新钥同一次
    # 原子替换落盘——否则新钥已生效而密文仍是旧钥的，通道静默死亡。
    # 修复轮1②：读现值也搬进这把 env 锁（rotate_and_write_env），--force 不停服时
    # 期间设置页改过的推送配置才不会被启动时的陈旧快照覆盖回去。
    # 尽力而为：本步骤失败只提示，不改变轮换结果与退出码。
    notify_state = rotate_and_write_env(
        env_path, new_key, old_key, skip_notify=args.skip_notify)
    if staging:
        with contextlib.suppress(OSError):
            os.remove(staging)
        print(f"已删除暂存文件: {staging}")
    print("已更新 .env 的 YIBAN_ACCOUNTS_KEY。")
    print(f"推送通道自检：{NOTIFY_SELF_CHECK_NOTE[notify_state]}")
    _audit_rotate(
        db_path,
        "accounts_key_rekey",
        ("ENV-ONLY 补完" if args.env_only else "全量重加密完成并更新 .env")
        + f"；推送通道：{NOTIFY_SELF_CHECK_NOTE[notify_state]}",
        env_file=key_source,
    )
    print("后续步骤：重启 web/signin/scheduler 等全部进程；若 shell/容器环境变量中"
          "仍设有旧 YIBAN_ACCOUNTS_KEY，请同步更新（环境变量优先于 .env）。")
    print("提醒：旧密钥应视为已泄露——若攻击者曾拷贝数据库文件，历史密文仍需按"
          "泄露处理（通知用户改密）。")


if __name__ == "__main__":
    main()
