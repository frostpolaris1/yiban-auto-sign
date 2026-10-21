# -*- coding: utf-8 -*-
"""`docs/dev/release-gate.md` 点名的脚本必须入库（工单 fdfn）。

标签：J · 运维：部署/备份/发布
覆盖：发布门槛文档里出现的每个 `scripts/*.sh|py`，都必须被 git 跟踪；两种演练形态
    （裸机 / 容器）的脚本必须被 §2④ 点名。
对应实现：`docs/dev/release-gate.md`；`scripts/drill-baremetal.sh`、
    `scripts/drill-container.sh`。
关键断言：**文档点名的东西读者拿得到**。演练手段只活在本地 `work/`（被 .gitignore
    排除）时，删树即丢，发布门槛的执行方法就没有载体（AGENTS §24）。
依赖：读文档 + `git ls-files`；不执行脚本、不联网。
"""
import os
import re
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE_GATE = os.path.join(BASE, "docs", "dev", "release-gate.md")
SCRIPT_REF = re.compile(r"scripts/[A-Za-z0-9_.-]+\.(?:sh|py)")
DRILL_SCRIPTS = ("scripts/drill-baremetal.sh", "scripts/drill-container.sh")


def _script_refs(text):
    return sorted(set(SCRIPT_REF.findall(text)))


def _untracked(refs, tracked):
    return [ref for ref in refs if ref not in tracked]


class ReleaseGateScriptsTest(unittest.TestCase):
    """发布门槛文档引用的脚本必须入库；演练脚本必须被 §2④ 点名。"""

    def _tracked(self):
        args = ["git"]
        dot_git = os.path.join(BASE, ".git")
        if os.path.isfile(dot_git):
            # linked worktree：.git 是「gitdir: <Windows 绝对路径>」指针，WSL 侧直接
            # 交给 git 会报 not a git repository。翻成 /mnt/<盘>/… 后照常可查。
            with open(dot_git, encoding="utf-8") as f:
                text = f.read().strip()
            prefix = "gitdir: "
            if text.startswith(prefix):
                target = text[len(prefix):].strip()
                m = re.match(r"^([A-Za-z]):[\\/](.*)$", target)
                if m:
                    target = "/mnt/%s/%s" % (m.group(1).lower(),
                                              m.group(2).replace("\\", "/"))
                args += ["--git-dir", target, "--work-tree", BASE]
        r = subprocess.run([*args, "ls-files"], cwd=BASE, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, "git ls-files 失败：%s" % (r.stderr or r.stdout))
        return {line.strip() for line in r.stdout.splitlines() if line.strip()}
        return {line.strip() for line in r.stdout.splitlines() if line.strip()}

    def test_referenced_scripts_are_tracked(self):
        with open(RELEASE_GATE, encoding="utf-8") as f:
            text = f.read()
        refs = _script_refs(text)
        self.assertGreaterEqual(len(refs), 5, "文档引用面塌了，门会假绿")
        missing = _untracked(refs, self._tracked())
        self.assertEqual(missing, [], "release-gate 点名的脚本未入库（删树即丢）")

    def test_referenced_scripts_parse(self):
        """文档点名的脚本不仅要存在，还要能过语法检查（坏脚本＝手段失效）。"""
        with open(RELEASE_GATE, encoding="utf-8") as f:
            refs = _script_refs(f.read())
        for ref in refs:
            path = os.path.join(BASE, ref)
            self.assertTrue(os.path.isfile(path), "文档点名的脚本不存在：%s" % ref)
            if ref.endswith(".sh"):
                r = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0,
                                 "bash -n 失败：%s\n%s" % (ref, r.stderr))
            else:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    compile(f.read(), ref, "exec")

    def test_both_drill_forms_are_named(self):
        with open(RELEASE_GATE, encoding="utf-8") as f:
            text = f.read()
        for script in DRILL_SCRIPTS:
            self.assertIn(script, text,
                          "发布门槛必须点名该演练形态的脚本：%s（判据有了、手段没有载体）"
                          % script)

    def test_detector_has_teeth(self):
        self.assertEqual(
            _untracked(["scripts/a.sh", "scripts/b.py"], {"scripts/a.sh"}),
            ["scripts/b.py"])
