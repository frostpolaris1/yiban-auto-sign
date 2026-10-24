# -*- coding: utf-8 -*-
"""备份的跨进程互斥：并行的第二跑必须明确失败（R3-020）。

标签：J · 运维：部署/备份/发布

模拟的真实用户动作：cron 02:00 的备份还没跑完，运维又手跑了一次（或两条 cron
重叠）。用户可见结果 = 不会静默丢掉一轮、不会两轮同时写同一份归档。

缺陷形状：脚本没有任何跨进程锁。两跑并行都 `rc=0`，轮转后只剩一份归档——
"跑了两轮"这件事在任何输出里都不成立。

判据（真起两个进程）：
  ① 第一跑持锁期间，第二跑 `rc=1` 且点名"已有备份进程在运行"；
  ② 第二跑没有真的进入备份步骤（gpg 桩只被执行一次）；
  ③ 第一跑结束后，再跑一次能拿到锁（锁不是永久占用的死锁）。
"""
import io
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "scripts", "backup.sh")
BASH = shutil.which("bash")

FAKE_PASS = "e2e-r3b-lock-not-a-real-passphrase"

#: 透传型 gpg 桩 + 起止留痕 + 人为拉长窗口（让第二跑必然撞上第一跑的锁）。
FAKE_GPG_SLOW = r"""#!/usr/bin/env bash
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
echo "gpg-start $PPID" >> "$GPG_LOG"
sleep "${GPG_SLEEP:-2}"
case "$mode" in
  enc) cat "$in" > "$out" ;;
  dec) if [ "$out" = "-" ]; then cat "$in"; else cat "$in" > "$out"; fi ;;
  *) exit 2 ;;
esac
echo "gpg-end $$" >> "$GPG_LOG"
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
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, phone TEXT)")
        con.execute("INSERT INTO accounts (phone) VALUES ('13800000000')")
        con.commit()
    finally:
        con.close()


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class BackupCrossProcessLockTest(unittest.TestCase):
    """R3-020：同一时刻只允许一轮备份。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backup-lock-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app = os.path.join(self.tmp, "app")
        self.data = os.path.join(self.app, "data")
        self.state = os.path.join(self.tmp, "state")
        self.backups = os.path.join(self.tmp, "backups")
        for d in (self.data, self.state, self.backups):
            os.makedirs(d)
        _write(os.path.join(self.data, ".env"), "YIBAN_STATE_DIR=%s\n" % self.state)
        _make_db(os.path.join(self.data, "yiban.db"))
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        _write(os.path.join(self.bin, "gpg"), FAKE_GPG_SLOW, 0o755)
        self.gpg_log = os.path.join(self.tmp, "gpg.log")
        self.env = dict(os.environ)
        self.env.update({
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "HOME": self.tmp,
            "APP_DIR": self.app,
            "YIBAN_APP_DIR": self.app,
            "BACKUP_DIR": self.backups,
            "RETENTION_DAYS": "30",
            "TMPDIR": self.tmp,
            "BACKUP_GPG_PASSPHRASE": FAKE_PASS,
            "GPG_LOG": self.gpg_log,
            "GPG_SLEEP": "2",
        })

    def _start(self):
        return subprocess.Popen([BASH, SCRIPT], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=self.env, cwd=self.tmp)

    def _run(self):
        return subprocess.run([BASH, SCRIPT], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=self.env,
                              cwd=self.tmp, timeout=300)

    def test_second_concurrent_run_fails_loudly(self):
        first = self._start()
        time.sleep(0.4)                      # 让第一跑先拿到锁并进入加密步
        second = self._run()
        out1 = first.communicate(timeout=300)
        out1_txt = (out1[0] or b"").decode("utf-8", "replace") + \
                   (out1[1] or b"").decode("utf-8", "replace")
        second_txt = (second.stdout or "") + (second.stderr or "")
        # 第一跑：正常完成（缺 sqlite3 时按既有契约退 5，仍算跑过一轮）
        self.assertIn(first.returncode, (0, 5), "第一跑没有正常结束：\n" + out1_txt)
        # 第二跑：明确失败，且说清原因（不静默丢一轮）
        self.assertEqual(second.returncode, 1,
                         "并行的第二跑没有明确失败（rc=%s）：\n%s"
                         % (second.returncode, second_txt))
        self.assertIn("已有备份进程在运行", second_txt,
                      "第二跑的失败没有说明是锁冲突：\n" + second_txt)
        # 第二跑没有真的进入备份步骤：gpg 桩只被**一轮**备份调用过。
        # 记父进程 PID 而不是桩自身 PID——一轮备份会调用 gpg 两次（加密 + 回环解密），
        # 但两次的父进程都是同一个 backup.sh。
        parents = set()
        if os.path.exists(self.gpg_log):
            with io.open(self.gpg_log, encoding="utf-8") as f:
                parents = {ln.split()[1] for ln in f if ln.startswith("gpg-start")}
        self.assertEqual(1, len(parents),
                         "两跑都进了备份步骤（锁没有互斥）：%r" % sorted(parents))

    def test_lock_is_released_after_the_run(self):
        r1 = self._run()
        self.assertIn(r1.returncode, (0, 5), (r1.stdout or "") + (r1.stderr or ""))
        r2 = self._run()
        self.assertNotEqual(r2.returncode, 1,
                            "上一轮结束后锁没有释放（永久占用）：\n"
                            + (r2.stdout or "") + (r2.stderr or ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
