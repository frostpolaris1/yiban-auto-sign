# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""主仓 git config 的 core.worktree 卫生门。

标签：J · 运维：部署/备份/发布
覆盖：WSL 侧 git 把 core.worktree=/mnt/... 写进共享 config 后，Windows 侧主树全部
    git 命令报 Invalid path '/mnt' 且无声。门要求：主仓 config 出现该键即红并给出
    修复命令；无该键放行；链接 worktree 形态要沿 .git 指针对到主仓配置；段名/键名
    按 git 语义大小写不敏感；配置不可读响亮退出 2（不 fail-open）。
关键断言：钉「脏键可见、修复命令可执行、解析失败不当放行」三条性质。
依赖：只读临时目录 + 本机 bash/awk；无网络、无第三方库。
"""
import os
import shutil
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_bash(script, *argv, env=None):
    return subprocess.run(
        ["bash", os.path.join(BASE, "scripts", script), *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, timeout=60, env=env,
    )


class GitConfigHygieneGuardTest(unittest.TestCase):
    """c8j：主仓 .git/config 的 core.worktree 卫生门。

    形状：WSL 侧 git 把 core.worktree=/mnt/... 写进共享 config ⇒ Windows 侧
    主树全部 git 命令报 Invalid path '/mnt'，且无声。门要求：主仓 config
    出现该键即红并给出修复命令；无该键放行；链接 worktree 形态要沿 .git
    指针对到主仓配置（H1：日常都从 worktree 干活，只查主工作树等于没门）；
    段名/键名按 git 语义大小写不敏感（M1）；配置不可读响亮退出 2（不 fail-open）。
    """

    def _repo_with_config(self, root, text, *, linked=False):
        main_git = os.path.join(root, "main", ".git")
        os.makedirs(main_git, exist_ok=True)
        with open(os.path.join(main_git, "config"), "w", encoding="utf-8") as f:
            f.write(text)
        if linked:
            wt = os.path.join(root, "wt")
            os.makedirs(os.path.join(main_git, "worktrees", "wt"), exist_ok=True)
            os.makedirs(wt, exist_ok=True)
            with open(os.path.join(wt, ".git"), "w", encoding="utf-8") as f:
                f.write("gitdir: %s\n" % os.path.join(main_git, "worktrees", "wt"))
            return wt
        return os.path.join(root, "main")

    @unittest.skipUnless(shutil.which("bash"), "bash 不可用：配置卫生门无法就地跑")
    def test_main_config_with_core_worktree_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo_with_config(
                root, "[core]\n\trepositoryformatversion = 0\n\tworktree = /mnt/d/example/repo\n")
            r = _run_bash("check-git-config.sh", repo)
            self.assertEqual(r.returncode, 1, f"argv={r.args!r} out={r.stdout!r} err={r.stderr!r}")
            self.assertIn("core.worktree", r.stderr)
            self.assertIn("--unset core.worktree", r.stderr, "修复命令必须点名")

    @unittest.skipUnless(shutil.which("bash"), "bash 不可用：配置卫生门无法就地跑")
    def test_clean_main_config_passes(self):
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo_with_config(root, "[core]\n\trepositoryformatversion = 0\n")
            r = _run_bash("check-git-config.sh", repo)
            self.assertEqual(r.returncode, 0, f"out={r.stdout!r} err={r.stderr!r}")

    @unittest.skipUnless(shutil.which("bash"), "bash 不可用：配置卫生门无法就地跑")
    def test_linked_worktree_reads_the_main_config(self):
        # H1：worktree 的 .git 是文件；脏键在主仓 config——必须沿指针查到并判红。
        with tempfile.TemporaryDirectory() as root:
            wt = self._repo_with_config(
                root, "[core]\n\tworktree = /mnt/d/example/repo\n", linked=True)
            r = _run_bash("check-git-config.sh", wt)
            self.assertEqual(r.returncode, 1, f"out={r.stdout!r} err={r.stderr!r}")
            self.assertIn("core.worktree", r.stderr)

    @unittest.skipUnless(shutil.which("bash"), "bash 不可用：配置卫生门无法就地跑")
    def test_linked_worktree_clean_main_config_passes(self):
        with tempfile.TemporaryDirectory() as root:
            wt = self._repo_with_config(root, "[core]\n\trepositoryformatversion = 0\n", linked=True)
            r = _run_bash("check-git-config.sh", wt)
            self.assertEqual(r.returncode, 0, f"out={r.stdout!r} err={r.stderr!r}")

    @unittest.skipUnless(shutil.which("bash"), "bash 不可用：配置卫生门无法就地跑")
    def test_section_and_key_are_case_insensitive(self):
        # M1：git config 语义大小写不敏感；[Core]/Worktree 同样要判红。
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo_with_config(root, "[Core]\n\tWorktree = /mnt/d/x\n")
            r = _run_bash("check-git-config.sh", repo)
            self.assertEqual(r.returncode, 1, f"out={r.stdout!r} err={r.stderr!r}")

    @unittest.skipUnless(shutil.which("bash"), "bash 不可用：配置卫生门无法就地跑")
    def test_relative_gitdir_that_cannot_resolve_exits_two(self):
        # 相对 gitdir 指向不存在目录：无法判定 = 退 2（不能把解析失败当放行）。
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, ".git"), "w", encoding="utf-8") as f:
                f.write("gitdir: ../nowhere/.git/worktrees/wt\n")
            r = _run_bash("check-git-config.sh", root)
            self.assertEqual(r.returncode, 2, f"out={r.stdout!r} err={r.stderr!r}")
            self.assertIn("拒绝放行", r.stderr)

    @unittest.skipUnless(shutil.which("bash"), "bash 不可用：配置卫生门无法就地跑")
    def test_unreadable_config_exits_two(self):
        # L2：解析器失败必须退 2（响亮），不能静默当绿。用假 awk 注入失败。
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as shim:
            repo = self._repo_with_config(root, "[core]\n\trepositoryformatversion = 0\n")
            fake = os.path.join(shim, "awk")
            with open(fake, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\nexit 3\n")
            os.chmod(fake, 0o755)
            env = dict(os.environ)
            env["PATH"] = shim + os.pathsep + env.get("PATH", "")
            r = _run_bash("check-git-config.sh", repo, env=env)
            self.assertEqual(r.returncode, 2, f"out={r.stdout!r} err={r.stderr!r}")
            self.assertIn("拒绝放行", r.stderr)


if __name__ == "__main__":
    unittest.main()
