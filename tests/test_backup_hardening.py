# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""backup.sh 加固批（P12-03/04/05/06/07/26 + P12-17 业务日降级链）的行为判据。

标签：J · 运维：部署/备份/发布
覆盖：
  ① 回环自检不得把"没做 integrity"说成"通过"：包内缺库、缺 sqlite3、公钥/age
     仅尺寸下限三种形态都必须以可区分的声音收场（P12-03/P12-26）。
  ② `.env` 是配置事实源：同名键末写生效，且 `.env` 优先于进程环境（M27，P12-04）。
  ③ 通知账本/节流表的真实落点（`.env` 无 YIBAN_STATE_DIR 时 = `dirname(.env)`）
     必须进包（P12-05）。
  ④ 源库损坏时归档照留、rc=4 可达（P12-06）；默认备份目录不得是共享的
     `/var/backups`（P12-07）。
  ⑤ 业务日三级降级链的四份副本都必须能判定 tzdata 是否生效（P12-17）。

对应实现：`scripts/backup.sh` 的 `verify_encrypted_archive` / `env_get` /
`business_day`，以及 `scripts/{yiban-fallback,pull-prod-backup}.sh`、`run.sh`
的同名 business_day 副本。

关键断言全部是**行为断言**：跑真脚本、解真归档、读真退出码与措辞；
P12-07/P12-17 的"默认值/四份副本一致性"属结构不变量，用最小源码断言钉住。
测试夹具里的口令/密钥全部是明显假值，不含任何真实凭据。
"""
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "scripts", "backup.sh")
RUN_SH = os.path.join(BASE, "run.sh")
FALLBACK = os.path.join(BASE, "scripts", "yiban-fallback.sh")
PULL = os.path.join(BASE, "scripts", "pull-prod-backup.sh")

BASH = shutil.which("bash")
SQLITE3 = shutil.which("sqlite3")

FAKE_PASS = "e2e-hardening-not-a-real-passphrase"

# 透传型 gpg 桩：加密 = 把 stdin 原样写到 -o 目标；解密 = 把输入文件原样写出。
# 只覆盖 backup.sh 实际用到的参数形态。
FAKE_GPG_PASSTHROUGH = r"""#!/usr/bin/env bash
out="" in="" mode="" drain=0 list=0
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2;;
    --passphrase-fd) drain=1; shift 2;;
    --cipher-algo|--recipient) shift 2;;
    --list-secret-keys) list=1; shift;;
    --symmetric|--encrypt) mode=enc; shift;;
    --decrypt) mode=dec; shift;;
    --batch|--yes) shift;;
    -*) shift;;
    *) in="$1"; shift;;
  esac
done
[ "$drain" -eq 1 ] && cat > /dev/null
[ "$list" -eq 1 ] && exit 0
case "$mode" in
  enc) cat "$in" > "$out" ;;
  dec) if [ "$out" = "-" ]; then cat "$in"; else cat "$in" > "$out"; fi ;;
  *) exit 2 ;;
esac
"""

# 公钥模式桩：能"加密"，但本机没有对应私钥（--list-secret-keys 无输出，解密失败）。
FAKE_GPG_PUBKEY = r"""#!/usr/bin/env bash
out="" in="" mode="" drain=0 list=0
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2;;
    --passphrase-fd) drain=1; shift 2;;
    --cipher-algo|--recipient) shift 2;;
    --list-secret-keys) list=1; shift;;
    --symmetric|--encrypt) mode=enc; shift;;
    --decrypt) mode=dec; shift;;
    --batch|--yes) shift;;
    -*) shift;;
    *) in="$1"; shift;;
  esac
done
[ "$drain" -eq 1 ] && cat > /dev/null
[ "$list" -eq 1 ] && exit 0
case "$mode" in
  enc) cat "$in" > "$out" ;;
  dec) exit 2 ;;
  *) exit 2 ;;
esac
"""

# 加密时把 data/yiban.db 从归档里摘掉的桩：模拟"包里到底有没有库"的漏备形态。
FAKE_GPG_STRIP_DB = r"""#!/usr/bin/env bash
out="" in="" mode="" drain=0
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2;;
    --passphrase-fd) drain=1; shift 2;;
    --cipher-algo|--recipient) shift 2;;
    --symmetric|--encrypt) mode=enc; shift;;
    --decrypt) mode=dec; shift;;
    --list-secret-keys) exit 0;;
    --batch|--yes) shift;;
    -*) shift;;
    *) in="$1"; shift;;
  esac
done
[ "$drain" -eq 1 ] && cat > /dev/null
if [ "$mode" = enc ]; then
  tmp="$(mktemp -d)"
  mkdir -p "$tmp/x"
  tar -xzf "$in" -C "$tmp/x"
  rm -f "$tmp/x/data/yiban.db"
  tar -czf "$out" -C "$tmp/x" .
  rm -rf "$tmp"
  exit 0
fi
if [ "$mode" = dec ]; then
  if [ "$out" = "-" ]; then cat "$in"; else cat "$in" > "$out"; fi
  exit 0
fi
exit 2
"""


def _write(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, mode)


def _make_db(path, table="t"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"CREATE TABLE {table} (x)")
        conn.execute("INSERT INTO %s VALUES (1)" % table)
        conn.commit()
    finally:
        conn.close()


def _archive_names(archive):
    with tarfile.open(archive) as t:
        return sorted(m.name for m in t.getmembers())


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class BackupHardeningBase(unittest.TestCase):
    gpg_stub = FAKE_GPG_PASSTHROUGH

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backup-hardening-")
        self.app = os.path.join(self.tmp, "app")
        self.data = os.path.join(self.app, "data")
        self.state = os.path.join(self.tmp, "state")
        self.backups = os.path.join(self.tmp, "backups")
        for d in (self.data, self.state, self.backups):
            os.makedirs(d)
        # 默认把状态目录钉在临时树里：不读本机 /var/log/yiban 的现存日志/状态。
        # 需要测「.env 未设 YIBAN_STATE_DIR」的用例会覆盖这个文件。
        _write(os.path.join(self.data, ".env"), f"YIBAN_STATE_DIR={self.state}\n")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        gpg = os.path.join(self.bin, "gpg")
        _write(gpg, self.gpg_stub, 0o755)
        self.env = {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "HOME": self.tmp,
            "APP_DIR": self.app,
            "BACKUP_DIR": self.backups,
            "RETENTION_DAYS": "30",
            "YIBAN_APP_DIR": self.app,
            "TMPDIR": self.tmp,
            # 隔离宿主真实状态/日志目录（不设 YIBAN_STATE_DIR：P12-05 要测它缺席的形态）
            "SIGN_STATE_DIR": self.state,
            "SIGN_LOG_DIR": os.path.join(self.tmp, "logs-isolated"),
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args, extra_env=None):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run([BASH, SCRIPT, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env,
                              cwd=self.tmp, timeout=300)

    def _out(self, r):
        return (r.stdout or "") + (r.stderr or "")


class VerifyIntegrityIsExplicitTest(BackupHardeningBase):
    """P12-03 / P12-26：没做 integrity 就不得说「回环自检通过」。"""

    @unittest.skipIf(SQLITE3 is None, "需要 sqlite3")
    def test_missing_db_in_archive_is_a_failure(self):
        """源库在、归档里却没有库：不得删明文并报「通过」（P12-03）。"""
        _make_db(os.path.join(self.data, "yiban.db"))
        _write(os.path.join(self.bin, "gpg"), FAKE_GPG_STRIP_DB, 0o755)
        r = self._run(extra_env={"BACKUP_GPG_PASSPHRASE": FAKE_PASS})
        out = self._out(r)
        self.assertNotEqual(r.returncode, 0, f"包内缺库仍按成功收场：{out}")
        self.assertNotIn("回环自检通过", out, f"包内缺库仍宣告自检通过：{out}")

    @unittest.skipIf(SQLITE3 is None, "需要 sqlite3")
    def test_missing_sqlite3_uses_degraded_wording(self):
        """解密解包成功但缺 sqlite3：必须说「未做 integrity」，不能说「通过」（P12-03）。"""
        _make_db(os.path.join(self.data, "yiban.db"))
        # 构造一个没有 sqlite3 的 PATH（保留脚本需要的其余命令）
        nobin = os.path.join(self.tmp, "nobin")
        os.makedirs(nobin)
        keep = ("bash", "sh", "cat", "cp", "mv", "rm", "mkdir", "chmod", "mktemp",
                "date", "tar", "gzip", "find", "sed", "tr", "head", "tail", "wc",
                "sort", "awk", "grep", "du", "sha256sum", "dirname", "basename",
                "printenv", "python3", "ls", "rmdir", "sleep", "touch", "stat",
                "readlink", "env")
        for name in keep:
            src = shutil.which(name)
            if src:
                os.symlink(src, os.path.join(nobin, name))
        for name in ("gpg",):
            os.symlink(os.path.join(self.bin, name), os.path.join(nobin, name))
        r = self._run(extra_env={"PATH": nobin,
                                 "BACKUP_GPG_PASSPHRASE": FAKE_PASS})
        out = self._out(r)
        self.assertNotIn("回环自检通过", out,
                         f"缺 sqlite3 仍宣告「回环自检通过」：{out}")
        self.assertIn("integrity", out.lower(),
                      f"缺 sqlite3 时没有说明「未做 integrity」：{out}")

    def test_pubkey_mode_does_not_claim_full_pass(self):
        """公钥模式只能做尺寸下限：措辞必须降级（P12-26）。"""
        stub = os.path.join(self.bin, "gpg")
        _write(stub, FAKE_GPG_PUBKEY, 0o755)
        _make_db(os.path.join(self.data, "yiban.db"))
        r = self._run(extra_env={"BACKUP_GPG_RECIPIENT": "drill@example.invalid"})
        out = self._out(r)
        self.assertNotIn("回环自检通过", out,
                         f"仅尺寸下限却宣告「回环自检通过」：{out}")
        self.assertRegex(out, r"仅尺寸|尺寸下限|未做.*完整性|未做.*integrity",
                         f"降级分支没有可辨认的措辞：{out}")


class EnvGetPriorityTest(BackupHardeningBase):
    """P12-04：`.env` 同名键末写生效，且 `.env` 优先于进程环境（M27）。"""

    def _state_file(self, d, name):
        os.makedirs(d, exist_ok=True)
        _write(os.path.join(d, name), "{}")

    def test_duplicate_key_last_wins_and_dotenv_beats_process_env(self):
        first = os.path.join(self.tmp, "state-first")
        second = os.path.join(self.tmp, "state-second")
        proc = os.path.join(self.tmp, "state-proc")
        # 三份文件各用不同日期名：归档里出现哪一枚，就说明选中了哪个目录。
        self._state_file(first, "sign-state-2026-10-01.json")
        self._state_file(second, "sign-state-2026-10-02.json")
        self._state_file(proc, "sign-state-2026-10-03.json")
        _write(os.path.join(self.data, ".env"),
               f"YIBAN_STATE_DIR={first}\nYIBAN_STATE_DIR={second}\n")
        _make_db(os.path.join(self.data, "yiban.db"))
        r = self._run(extra_env={"YIBAN_STATE_DIR": proc,
                                 "BACKUP_GPG_PASSPHRASE": FAKE_PASS})
        out = self._out(r)
        self.assertEqual(r.returncode, 0, out)
        archives = glob.glob(os.path.join(self.backups, "yiban-*.tar.gz.gpg"))
        self.assertEqual(1, len(archives), out)
        names = _archive_names(archives[0])
        # 加密桩是透传，归档本身可直接读
        self.assertIn("state/sign-state-2026-10-02.json", names,
                      f"没有选中 .env 的末个同名值（M27 末写生效）：{names}\n{out}")
        self.assertNotIn("state/sign-state-2026-10-01.json", names,
                         f"仍按首个匹配行选目录（head -1）：{names}\n{out}")
        self.assertNotIn("state/sign-state-2026-10-03.json", names,
                         f"进程环境压过了 .env：{names}\n{out}")


class NotifyStateLandsInArchiveTest(BackupHardeningBase):
    """P12-05：`.env` 未设 YIBAN_STATE_DIR 时，通知账本落在 dirname(.env)。"""

    def test_notify_ledger_and_throttle_are_archived(self):
        _make_db(os.path.join(self.data, "yiban.db"))
        _write(os.path.join(self.data, ".env"), "")
        _write(os.path.join(self.data, "notify-ledger.json"), "{}")
        _write(os.path.join(self.data, "notify-throttle.json"), "{}")
        r = self._run(extra_env={"BACKUP_GPG_PASSPHRASE": FAKE_PASS})
        out = self._out(r)
        self.assertEqual(r.returncode, 0, out)
        with tarfile.open(glob.glob(os.path.join(self.backups, "yiban-*.tar.gz.gpg"))[0]) as t:
            names = [m.name for m in t.getmembers()]
        self.assertIn("state/notify-ledger.json", names,
                      f"notify-ledger.json 未入包（备份抓错目录）：{names}\n{out}")
        self.assertIn("state/notify-throttle.json", names,
                      f"notify-throttle.json 未入包（备份抓错目录）：{names}\n{out}")


class CorruptSourceExitCodeTest(BackupHardeningBase):
    """P12-06：源库损坏时 rc=4 必须可达（不得被回环自检改写成 rc=7）。"""

    @unittest.skipIf(SQLITE3 is None, "需要 sqlite3")
    def test_corrupt_source_reaches_rc4(self):
        db = os.path.join(self.data, "yiban.db")
        conn = sqlite3.connect(db)
        try:
            conn.execute("CREATE TABLE t (x)")
            conn.execute("INSERT INTO t VALUES (1)")
            conn.execute("CREATE TABLE big (y)")
            for _i in range(4000):
                conn.execute("INSERT INTO big VALUES (?)", ("v" * 100,))
            conn.commit()
        finally:
            conn.close()
        # 破坏数据页（保留有效的 100 字节文件头，让 .backup 仍能复制页面）
        with open(db, "r+b") as f:
            f.seek(4096)
            f.write(b"\xde\xad\xbe\xef" * 64)
        r = self._run(extra_env={"BACKUP_GPG_PASSPHRASE": FAKE_PASS})
        out = self._out(r)
        self.assertEqual(4, r.returncode,
                         f"源库损坏未以 rc=4 收场（默认加密链上 rc=4 不可达）：{out}")
        self.assertIn("源库", out)


class BackupDirDefaultTest(BackupHardeningBase):
    """P12-07：默认备份目录必须是专用子目录；共享 /var/backups 不得被收紧。"""

    def test_default_dir_is_dedicated_subdir(self):
        with open(SCRIPT, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('BACKUP_DIR="${BACKUP_DIR:-/var/backups/yiban}"', src,
                      "BACKUP_DIR 默认值仍是共享的 /var/backups")

    def test_all_carriers_share_one_default(self):
        """跨载体对齐：写入端、哨兵、拉取端、配置名册必须是同一个默认值。"""
        with open(SCRIPT, encoding="utf-8") as f:
            backup_src = f.read()
        with open(PULL, encoding="utf-8") as f:
            pull_src = f.read()
        sentinel_py = os.path.join(BASE, "scripts", "backup_sentinel.py")
        with open(sentinel_py, encoding="utf-8") as f:
            sentinel_src = f.read()
        with open(os.path.join(BASE, "config", "registry.json"), encoding="utf-8") as f:
            registry = json.load(f)
        values = {
            "scripts/backup.sh": re.search(
                r'BACKUP_DIR="\$\{BACKUP_DIR:-([^}]+)\}"', backup_src).group(1),
            "scripts/pull-prod-backup.sh": re.search(
                r'REMOTE_DIR="\$\{REMOTE_BACKUP_DIR:-([^}]+)\}"', pull_src).group(1),
            "scripts/backup_sentinel.py": re.search(
                r'DEFAULT_BACKUP_DIR\s*=\s*"([^"]+)"', sentinel_src).group(1),
            "config/registry.json": registry["YIBAN_BACKUP_DIR"]["default"],
        }
        self.assertEqual({"/var/backups/yiban"}, set(values.values()),
                         f"备份目录默认值跨载体漂移：{values}")

    def test_no_old_default_left_in_docs_or_wrapper(self):
        wrapper = os.path.join(BASE, "deploy", "prod", "yiban-backup-wrapper.sh")
        sentinel_sh = os.path.join(BASE, "scripts", "yiban-backup-sentinel.sh")
        for path in (wrapper, sentinel_sh, os.path.join(BASE, "README.md")):
            with open(path, encoding="utf-8") as f:
                body = f.read()
            self.assertNotIn("默认 /var/backups、", body,
                             f"{os.path.basename(path)} 仍写旧默认")
            self.assertNotIn("`/var/backups` 下", body,
                             f"{os.path.basename(path)} 仍把旧目录写成默认落点")
        with open(wrapper, encoding="utf-8") as f:
            self.assertIn("/var/backups/yiban", f.read(),
                          "wrapper 没有写明它继承的新默认")

    def test_shared_dir_is_refused_before_chmod(self):
        r = self._run(extra_env={"BACKUP_DIR": "/var/backups",
                                 "BACKUP_PLAINTEXT": "1"})
        out = self._out(r)
        self.assertNotEqual(r.returncode, 0, f"共享目录被接受并继续执行：{out}")
        self.assertIn("专用", out, f"拒绝共享目录时没有说明处置：{out}")


class BusinessDayFallbackChainTest(unittest.TestCase):
    """P12-17：四份 business_day 副本都必须判定 TZ 是否真的生效。"""

    FILES = (RUN_SH, SCRIPT, FALLBACK, PULL)

    def test_all_copies_check_tz_effective(self):
        for path in self.FILES:
            with open(path, encoding="utf-8") as f:
                src = f.read()
            self.assertIn("+0800", src,
                          f"{os.path.basename(path)} 的 business_day 未校验 tzdata 是否生效")
            self.assertNotIn('TZ=Asia/Shanghai date +%F 2>/dev/null && return 0',
                             src,
                             f"{os.path.basename(path)} 仍把 GNU date 的 0 当成功判据")
            self.assertIn("TZ=Asia/Shanghai date +%z", src,
                          f"{os.path.basename(path)} 未用 date +%z 判定时区生效")


if __name__ == "__main__":
    unittest.main(verbosity=2)
