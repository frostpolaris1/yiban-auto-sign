# -*- coding: utf-8 -*-
"""宿主备份在源库缺失时必须失败（R3-017）。

标签：J · 运维：部署/备份/发布

模拟的真实用户动作：cron 每日 02:00 跑 `/usr/local/sbin/yiban-backup-wrapper.sh`，
运维次日只看退出码与归档。用户可见结果 = "备份可用"。

缺陷形状：源库不存在时脚本只打一行「跳过（不存在）」，照常产出归档并以 0 收场。
退出码判据写成「PRESENT=1 时才校验库是否进包」，故缺库这一支永远不参与判定。
灾难恢复日才会发现归档里没有库。

判据：
  ① 源库缺失 ⇒ rc≠0，且不产出归档（"跳过"不许冒充"备份完成"）；
  ② 源库存在 ⇒ rc=0，且归档里确有 data/<库名>。
显式开关 `--allow-missing-db` 是唯一的例外通道（部署首次初始化等场景），见 backup.sh 头注释。
"""
import glob
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "scripts", "backup.sh")
BASH = shutil.which("bash")
SQLITE3 = shutil.which("sqlite3")

FAKE_PASS = "e2e-r3b-not-a-real-passphrase"

#: 透传型 gpg 桩：加密 = 把输入原样写到 -o 目标；解密 = 原样写出。
#: 只覆盖 backup.sh 实际用到的参数形态（与 tests/test_backup_hardening.py 同形）。
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


def _write(path, text, mode=None):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def _make_db(path):
    import sqlite3
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, phone TEXT)")
        con.execute("INSERT INTO accounts (phone) VALUES ('13800000000')")
        con.commit()
    finally:
        con.close()


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class BackupMissingSourceDbTest(unittest.TestCase):
    """R3-017：缺库不许当成功。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backup-db-presence-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app = os.path.join(self.tmp, "app")
        self.data = os.path.join(self.app, "data")
        self.state = os.path.join(self.tmp, "state")
        self.backups = os.path.join(self.tmp, "backups")
        for d in (self.data, self.state, self.backups):
            os.makedirs(d)
        _write(os.path.join(self.data, ".env"), "YIBAN_STATE_DIR=%s\n" % self.state)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        _write(os.path.join(self.bin, "gpg"), FAKE_GPG_PASSTHROUGH, 0o755)
        self.env = {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "HOME": self.tmp,
            "APP_DIR": self.app,
            "YIBAN_APP_DIR": self.app,
            "BACKUP_DIR": self.backups,
            "RETENTION_DAYS": "30",
            "TMPDIR": self.tmp,
            "BACKUP_GPG_PASSPHRASE": FAKE_PASS,
        }

    def _run(self, *args):
        return subprocess.run([BASH, SCRIPT, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=self.env,
                              cwd=self.tmp, timeout=300)

    def _archives(self):
        names = []
        for pat in ("yiban-*.tar.gz*",):
            names.extend(os.path.basename(p) for p in glob.glob(os.path.join(self.backups, pat)))
        return sorted(names)

    def test_missing_source_db_fails_and_writes_no_archive(self):
        r = self._run()
        out = (r.stdout or "") + (r.stderr or "")
        self.assertNotEqual(r.returncode, 0,
                            "源库缺失仍以 0 收场（归档里没有库也不会有人发现）：\n" + out)
        self.assertEqual(self._archives(), [],
                         "源库缺失却产出了归档（一份不可恢复的'备份'）：\n" + out)
        self.assertIn("yiban.db", out, "失败信息必须点名缺的是哪个库：\n" + out)

    def test_explicit_allow_missing_db_is_the_only_way_out(self):
        """显式开关是唯一例外通道；它照样留下"本轮不含库"的响亮说明。"""
        r = self._run("--allow-missing-db")
        out = (r.stdout or "") + (r.stderr or "")
        self.assertIn("--allow-missing-db", out,
                      "允许无库备份时必须说明是哪个人工开关放行的：\n" + out)
        self.assertIn("yiban.db", out, "允许无库时仍须点名缺的库：\n" + out)

    @unittest.skipIf(SQLITE3 is None, "需要 sqlite3 命令行（一致性快照与 integrity_check）")
    def test_present_source_db_lands_in_the_archive(self):
        _make_db(os.path.join(self.data, "yiban.db"))
        r = self._run()
        out = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(r.returncode, 0, "库存在时备份必须成功：\n" + out)
        archives = self._archives()
        self.assertTrue(archives, "库存在却没有归档：\n" + out)
        target = os.path.join(self.backups, archives[0])
        with tarfile.open(target) as t:
            names = t.getnames()
        self.assertTrue(any(n.endswith("data/yiban.db") for n in names),
                        "归档内没有库本体（恢复时无库可用）：\n" + "\n".join(names))


if __name__ == "__main__":
    unittest.main(verbosity=2)
