# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""docker/backup-docker.sh 与容器配置加固批（P12-08/09/21/22, P13-09）。

标签：J · 运维：部署/备份/发布
覆盖：
  ① 旗标解析扫全部参数：未知参数/移位 `--restore` 一律 rc=2，绝不静默落到备份模式
     （P12-08）；正常 `--restore <包> <目录>` 仍可恢复。
  ② 口令来源 `_FILE` 优先于环境变量，并在用 _FILE 时把环境键摘掉（P12-09）。
  ③ 审计锚点缺失即删产物并 rc=1（P12-21）；轮转第二条 pass 只删本项目命名（P12-22）。
  ④ 调度器构造备份子进程环境时剔除两个口令环境键（P12-09 的后半）。
  ⑤ compose healthcheck 的超时必须容得下两条 python3 探针（P13-09）。

依赖：bash；gpg 用本地 stub（只认脚本实际用到的参数形态），不联网、不触生产。
"""
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "docker", "backup-docker.sh")
SCHED = os.path.join(BASE, "docker", "scheduler.py")
COMPOSE = os.path.join(BASE, "docker-compose.yml")
BASH = shutil.which("bash")

FAKE_PASSPHRASE = "e2e-docker-file-passphrase"


def _write(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.chmod(path, mode)


# 透传 gpg：加密=把 stdin 写到 -o；解密=把输入文件写到 -o（"-" 即 stdout）。
# 另把 fd 3（脚本注入的口令）抄到 $GPGCAPTURE，供"用了哪份口令"的行为断言。
FAKE_GPG = r"""#!/usr/bin/env bash
out="" in="" mode=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2;;
    --passphrase-fd) shift 2;;
    --cipher-algo|--recipient) shift 2;;
    --symmetric|--encrypt) mode=enc; shift;;
    --decrypt) mode=dec; shift;;
    --batch|--yes) shift;;
    -*) shift;;
    *) in="$1"; shift;;
  esac
done
if [ -n "${GPGCAPTURE:-}" ]; then cat <&3 > "$GPGCAPTURE"; fi
case "$mode" in
  enc) cat > "$out" ;;
  dec) if [ "$out" = "-" ]; then cat "$in"; else cat "$in" > "$out"; fi ;;
  *) exit 2 ;;
esac
"""


def _make_data(root, anchor=True):
    os.makedirs(os.path.join(root, "state"), exist_ok=True)
    _write(os.path.join(root, "yiban.db"), "not-a-real-db")
    _write(os.path.join(root, "state", "app.log"), "x")
    if anchor:
        _write(os.path.join(root, "state", "audit-anchor.log"), "anchor\n")


def _make_plain_archive(path):
    body = os.path.join(tempfile.mkdtemp(prefix="arch-src-"), "hello.txt")
    _write(body, "restored\n")
    with tarfile.open(path, "w:gz") as t:
        t.add(body, arcname="data/hello.txt")


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class DockerBackupArgParsingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backup-docker-hard-")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        _write(os.path.join(self.bin, "gpg"), FAKE_GPG, 0o755)
        self.data = os.path.join(self.tmp, "data")
        _make_data(self.data)
        self.pass_file = os.path.join(self.tmp, "pass")
        _write(self.pass_file, FAKE_PASSPHRASE + "\n")
        self.env = {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "HOME": self.tmp,
            "DATA_DIR": self.data,
            "BACKUP_DIR": os.path.join(self.tmp, "backups"),
            "YIBAN_BACKUP_PASSPHRASE_FILE": self.pass_file,
            "GPGCAPTURE": os.path.join(self.tmp, "captured"),
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args, env=None):
        e = dict(self.env)
        if env:
            e.update(env)
        return subprocess.run([BASH, SCRIPT, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=e,
                              cwd=self.tmp, timeout=180)

    def test_unknown_flag_is_rejected_not_silently_backed_up(self):
        r = self._run("--bogus")
        out = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(2, r.returncode,
                         f"未知参数落进了备份模式（演练被误当成备份）：{out}")
        self.assertIn("未知参数", out)
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "backups")),
                         "未知参数路径仍产出了备份")

    def test_shifted_restore_is_rejected(self):
        r = self._run("prefix", "--restore", "a.tar.gz.gpg", "dest")
        out = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(2, r.returncode, f"移位的 --restore 未被拒绝：{out}")
        self.assertIn("未知参数", out)

    def test_restore_with_missing_args_is_usage_error(self):
        r = self._run("--restore")
        self.assertEqual(2, r.returncode, (r.stdout or "") + (r.stderr or ""))

    def test_normal_restore_still_works(self):
        archive = os.path.join(self.tmp, "yiban-data-2026-10-20.tar.gz.gpg")
        _make_plain_archive(archive)
        dest = os.path.join(self.tmp, "restored")
        r = self._run("--restore", archive, dest)
        out = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(0, r.returncode, out)
        self.assertTrue(os.path.isfile(os.path.join(dest, "data", "hello.txt")), out)


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class DockerBackupPassphraseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backup-docker-pass-")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        _write(os.path.join(self.bin, "gpg"), FAKE_GPG, 0o755)
        self.data = os.path.join(self.tmp, "data")
        _make_data(self.data)
        self.pass_file = os.path.join(self.tmp, "pass")
        _write(self.pass_file, FAKE_PASSPHRASE + "\n")
        self.capture = os.path.join(self.tmp, "captured")
        self.env = {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "HOME": self.tmp,
            "DATA_DIR": self.data,
            "BACKUP_DIR": os.path.join(self.tmp, "backups"),
            "YIBAN_BACKUP_PASSPHRASE_FILE": self.pass_file,
            "GPGCAPTURE": self.capture,
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_file_beats_environment_and_env_key_is_dropped(self):
        env = dict(self.env)
        env["YIBAN_BACKUP_PASSPHRASE"] = "ENV-WINS-BUG"
        r = subprocess.run([BASH, SCRIPT], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env,
                           cwd=self.tmp, timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(0, r.returncode, out)
        with open(self.capture, encoding="utf-8") as f:
            used = f.read().strip()
        self.assertEqual(FAKE_PASSPHRASE, used,
                         f"口令来源与头注释相反（应 _FILE 优先）：{out}")


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class DockerBackupAnchorAndRotateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backup-docker-anchor-")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        _write(os.path.join(self.bin, "gpg"), FAKE_GPG, 0o755)
        self.pass_file = os.path.join(self.tmp, "pass")
        _write(self.pass_file, FAKE_PASSPHRASE + "\n")
        self.backups = os.path.join(self.tmp, "backups")
        os.makedirs(self.backups)
        self.env = {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "HOME": self.tmp,
            "BACKUP_DIR": self.backups,
            "RETAIN_DAYS": "1",
            "YIBAN_BACKUP_PASSPHRASE_FILE": self.pass_file,
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_anchor_deletes_product_and_exits_nonzero(self):
        data = os.path.join(self.tmp, "data-no-anchor")
        _make_data(data, anchor=False)
        env = dict(self.env, DATA_DIR=data)
        r = subprocess.run([BASH, SCRIPT], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env,
                           cwd=self.tmp, timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        self.assertNotEqual(0, r.returncode,
                            f"缺审计锚点仍按成功收场（自动化只看退出码）：{out}")
        products = [n for n in os.listdir(self.backups) if n.endswith(".gpg")]
        self.assertEqual([], products, f"缺锚点的产物未删除：{products}\n{out}")

    def test_rotate_does_not_touch_other_projects_sha256(self):
        data = os.path.join(self.tmp, "data")
        _make_data(data)
        foreign = os.path.join(self.backups, "other-project-2020-01-01.sha256")
        _write(foreign, "deadbeef  other-project.tar.gz\n")
        own = os.path.join(self.backups, "yiban-data-2020-01-01.tar.gz.gpg.sha256")
        _write(own, "deadbeef  yiban-data-2020-01-01.tar.gz.gpg\n")
        old = 40 * 86400
        for p in (foreign, own):
            os.utime(p, (os.path.getmtime(p) - old, os.path.getmtime(p) - old))
        env = dict(self.env, DATA_DIR=data)
        r = subprocess.run([BASH, SCRIPT], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env,
                           cwd=self.tmp, timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(0, r.returncode, out)
        self.assertTrue(os.path.exists(foreign),
                        f"轮转删掉了别的项目的 .sha256：{out}")
        self.assertFalse(os.path.exists(own),
                         f"本项目过期 .sha256 未被轮转：{out}")


class SchedulerBackupEnvTest(unittest.TestCase):
    """调度器给备份子进程的环境不得带任何口令键。"""

    def test_backup_env_strips_passphrase_keys(self):
        tmp = tempfile.mkdtemp(prefix="sched-backup-env-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env_file = os.path.join(tmp, ".env")
        _write(env_file, "YIBAN_BACKUP_PASSPHRASE=SECRET-FROM-DOTENV\n"
                         "YIBAN_BACKUP_DIR=/backups\n")
        old = {k: os.environ.get(k) for k in
               ("YIBAN_ENV_FILE", "YIBAN_BACKUP_PASSPHRASE",
                "BACKUP_GPG_PASSPHRASE", "YIBAN_STATE_DIR")}
        try:
            os.environ["YIBAN_ENV_FILE"] = env_file
            os.environ["YIBAN_BACKUP_PASSPHRASE"] = "SECRET-FROM-ENV"
            os.environ["BACKUP_GPG_PASSPHRASE"] = "SECRET-LEGACY"
            os.environ.setdefault("YIBAN_STATE_DIR", tmp)
            spec = importlib.util.spec_from_file_location("yiban_docker_scheduler", SCHED)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            env = mod._backup_env()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        for key in ("YIBAN_BACKUP_PASSPHRASE", "BACKUP_GPG_PASSPHRASE"):
            self.assertNotIn(key, env,
                             f"备份子进程环境仍带口令键 {key}（/proc/<pid>/environ 可读）")


class ComposeHealthcheckBudgetTest(unittest.TestCase):
    def test_healthcheck_timeout_fits_two_python_probes(self):
        with io.open(COMPOSE, encoding="utf-8") as f:
            body = f.read()
        idx = body.find("healthcheck:")
        self.assertGreaterEqual(idx, 0, "compose 没有 healthcheck")
        block = body[idx:idx + 800]
        mt = re.search(r"(?m)^\s+timeout:\s*(\d+)s\s*$", block)
        self.assertIsNotNone(mt, f"healthcheck 缺 timeout：{block}")
        self.assertGreaterEqual(int(mt.group(1)), 10,
                                f"healthcheck timeout={mt.group(1)}s 容不下两条 python3 探针")


if __name__ == "__main__":
    unittest.main(verbosity=2)
