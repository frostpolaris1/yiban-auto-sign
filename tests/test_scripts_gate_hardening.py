# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""部署/闸门脚本加固批的行为与结构判据（P12-15/16/19/20/23, P13-10, 2k1i）。

标签：J · 运维：部署/备份/发布
覆盖：
  ① pull-prod-backup：--check 必须接住新鲜度自检的返回值（P12-15）；STALE_DAYS /
     LOCAL_KEEP 非法时在跑任何远端命令之前以 rc=3 拒绝（P12-16）。
  ② check-cron-provenance：`--cron-dir/--manifest` 缺值必须报用法并 rc=2，
     不得被 set -u 折成「1=有违规」（P12-19）。
  ③ 02:00 备份 wrapper 的 cron 行必须入库、进 manifest、可被来源门回指（P12-20）。
  ④ check-deploy-target：FETCH_HEAD 解析失败必须 rc=2（解析类失败），不得与
     「远端分支不含目标提交」共用 rc=1（P12-23）。
  ⑤ ci.yml 必须有 concurrency 组且 cancel-in-progress（P13-10）。
  ⑥ 生产 cron 里调用 run.sh 的身份必须是 yiban（2k1i：调用方清单与身份门）。
  ⑦ 备份口令文件不得被 git 跟踪（P13-01）。

依赖：bash（Git Bash/WSL）；远端用本地 stub ssh/git，不联网、不触生产。
"""
import os
import shutil
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PULL = os.path.join(BASE, "scripts", "pull-prod-backup.sh")
CRON_GATE = os.path.join(BASE, "scripts", "check-cron-provenance.sh")
DEPLOY_GATE = os.path.join(BASE, "scripts", "check-deploy-target.sh")
CI = os.path.join(BASE, ".github", "workflows", "ci.yml")
CRON_DIR = os.path.join(BASE, "deploy", "prod", "cron.d")
MANIFEST = os.path.join(BASE, "deploy", "prod", "manifest.tsv")
GITIGNORE = os.path.join(BASE, ".gitignore")

BASH = shutil.which("bash")
GIT = shutil.which("git")


def _write(path, text, mode=0o755):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.chmod(path, mode)


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class PullProdCheckHardeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pull-prod-hardening-")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.mirror = os.path.join(self.tmp, "mirror")
        os.makedirs(self.mirror)
        # 远端 stub：只回应 ls -1t 的列表；sha256sum 不在 --check 路径上。
        _write(os.path.join(self.bin, "ssh"), """#!/usr/bin/env bash
for a in "$@"; do
  case "$a" in
    *"ls -1t"*) printf '%s\\n' '/backup/yiban-2026-10-20.tar.gz.gpg'; exit 0 ;;
    *sha256sum*) echo stubhash; exit 0 ;;
  esac
done
exit 0
""")
        self.env = {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "HOME": self.tmp,
            "YIBAN_SSH_HOST": "fixture-host",
            "REMOTE_BACKUP_DIR": "/backup",
            "LOCAL_MIRROR_DIR": self.mirror,
            "PULL_MAX_FETCH": "10",
        }
        # 本地只有旧副本：新鲜度应判「本地落后于远端」
        old = os.path.join(self.mirror, "yiban-2026-10-01.tar.gz.gpg")
        _write(old, "old", 0o600)
        _write(old + ".sha256",
               subprocess.run(["sha256sum", old], capture_output=True, text=True,
                              check=True).stdout)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args, env=None):
        e = dict(self.env)
        if env:
            e.update(env)
        return subprocess.run([BASH, PULL, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=e,
                              cwd=self.tmp, timeout=120)

    def test_check_mode_fails_when_local_is_stale(self):
        r = self._run("--check")
        out = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(1, r.returncode,
                         f"--check 丢弃了新鲜度结论（本地落后仍报成功）：{out}")

    def test_invalid_stale_days_is_rejected_before_remote(self):
        r = self._run(env={"STALE_DAYS": "abc"})
        out = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(3, r.returncode, f"STALE_DAYS 非法未以 rc=3 拒绝：{out}")
        self.assertIn("STALE_DAYS", out)

    def test_invalid_local_keep_is_rejected_before_remote(self):
        r = self._run(env={"LOCAL_KEEP": "abc"})
        out = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(3, r.returncode, f"LOCAL_KEEP 非法未以 rc=3 拒绝：{out}")
        self.assertIn("LOCAL_KEEP", out)


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class CheckCronProvenanceArgsTest(unittest.TestCase):
    def test_missing_value_is_usage_error_not_violation(self):
        for flag in ("--cron-dir", "--manifest"):
            r = subprocess.run([BASH, CRON_GATE, flag], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60)
            out = (r.stdout or "") + (r.stderr or "")
            self.assertEqual(2, r.returncode,
                             f"{flag} 缺值应以 rc=2 报用法错误（不得与 1=违规 混码）：{out}")
            self.assertRegex(out, r"用法|缺少|参数", f"{flag} 缺值没有可读诊断：{out}")


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class BackupCronEntryTest(unittest.TestCase):
    def test_backup_cron_entry_is_tracked_and_provenanced(self):
        path = os.path.join(CRON_DIR, "yiban-backup")
        self.assertTrue(os.path.isfile(path),
                        "02:00 备份 wrapper 的 cron 行没有入库原件（只活在注释里）")
        with open(path, encoding="utf-8") as f:
            body = f.read()
        self.assertRegex(body, r"(?m)^0 2 \* \* \* root /usr/local/sbin/yiban-backup-wrapper\.sh\b",
                         f"cron 行不是 02:00 root wrapper：{body}")
        with open(MANIFEST, encoding="utf-8") as f:
            manifest = f.read()
        self.assertIn("deploy/prod/cron.d/yiban-backup\t/etc/cron.d/yiban-backup", manifest,
                      "新 cron 表没有进 manifest（install.sh 不会装它）")
        r = subprocess.run([BASH, CRON_GATE], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(0, r.returncode, (r.stdout or "") + (r.stderr or ""))


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class CheckDeployTargetExitCodeTest(unittest.TestCase):
    def test_fetch_head_parse_failure_is_exit_2(self):
        tmp = tempfile.mkdtemp(prefix="deploy-target-hardening-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bin_dir = os.path.join(tmp, "bin")
        os.makedirs(bin_dir)
        _write(os.path.join(bin_dir, "git"), """#!/usr/bin/env bash
case "$*" in
  *"rev-parse --short FETCH_HEAD"*) exit 1 ;;
  *"rev-parse --verify"*) echo deadbeef; exit 0 ;;
  *) exit 0 ;;
esac
""")
        env = {"PATH": bin_dir + os.pathsep + os.environ.get("PATH", ""), "HOME": tmp}
        r = subprocess.run([BASH, DEPLOY_GATE, "fixture", "main", "HEAD"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, cwd=tmp, timeout=120)
        out = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(2, r.returncode,
                         f"FETCH_HEAD 解析失败被折成 rc=1（与「发布线落后」同码）：{out}")
        self.assertRegex(out, r"FETCH_HEAD|解析|读取", out)


class CiConcurrencyTest(unittest.TestCase):
    def test_ci_workflow_has_concurrency_with_cancel(self):
        with open(CI, encoding="utf-8") as f:
            body = f.read()
        self.assertRegex(body, r"(?m)^concurrency:\s*$",
                         "ci.yml 没有顶层 concurrency 组")
        self.assertRegex(body, r"(?m)^\s+group:\s*ci-\$\{\{\s*github\.ref\s*\}\}",
                         "concurrency.group 未按 ref 分组")
        self.assertRegex(body, r"(?m)^\s+cancel-in-progress:\s*true\s*$",
                         "concurrency 未启用 cancel-in-progress")


class RunShCronIdentityTest(unittest.TestCase):
    """2k1i：生产 cron 调用 run.sh 的身份必须是 yiban（root 预建日志会静默不执行）。"""

    def test_all_run_sh_cron_lines_run_as_yiban(self):
        path = os.path.join(CRON_DIR, "yiban-sign")
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        seen = 0
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "run.sh" not in stripped:
                continue
            fields = stripped.split()
            self.assertGreaterEqual(len(fields), 7, f"无法拆解 cron 行：{line}")
            user = fields[5]
            self.assertEqual("yiban", user,
                             f"run.sh 的 cron 行身份是 {user}，必须 yiban：{line}")
            seen += 1
        self.assertGreaterEqual(seen, 1, "没有任何调用 run.sh 的 cron 行可校验")


@unittest.skipIf(GIT is None, "需要 git 判断忽略规则")
class BackupPassphraseIgnoredTest(unittest.TestCase):
    def test_backup_passphrase_file_is_ignored(self):
        # 用临时 git 仓装载仓库根的 .gitignore 再判：worktree 的 .git 文件在
        # Windows 侧写出的是 Windows 路径，WSL 的 git 读不了（环境形态，不是判据）。
        tmp = tempfile.mkdtemp(prefix="gitignore-check-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        shutil.copyfile(GITIGNORE, os.path.join(tmp, ".gitignore"))
        subprocess.run([GIT, "init", "-q"], cwd=tmp, check=True,
                       capture_output=True, timeout=60)
        r = subprocess.run([GIT, "check-ignore", "-v", "backup-passphrase"],
                           cwd=tmp, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        self.assertEqual(0, r.returncode,
                         "仓库根的 backup-passphrase 未被忽略：一次 git add -A 即泄密")


if __name__ == "__main__":
    unittest.main(verbosity=2)
