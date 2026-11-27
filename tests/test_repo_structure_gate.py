# -*- coding: utf-8 -*-
"""顶层结构门：跟踪集的顶层条目必须 ⊆ 已登记白名单（2026-10-21 立）。

标签：J · 运维：部署/备份/发布
覆盖：`git ls-files` 里的**顶层条目**（目录名与根文件）。判据＝每个顶层条目都在
    `ALLOWED_TOP` 里；出现未登记条目即红并点名。
对应实现：无（本门只读 git 跟踪集；`.gitignore` 决定什么不入库，本门决定"入库的顶层
    形状"）。
关键断言：
    ① 真实跟踪集的顶层条目 ⊆ 白名单（多一个即红，点名是哪个）；
    ② 扫描面**非空**且与 `git ls-files` 一致（"扫空不得当绿"）；
    ③ 扫描器对合成集里的未登记条目必须命中（牙齿自证）。
为什么有它：仓库整理的目标是"顶层形状固定、新东西不得悄悄出现"。此前只有
    `test_root_artifacts.py` 管"根目录不得有运行产物"，**没有任何门管结构本身**——
    一个新会话把 `_census/`、`out/`、`tmp/` 之类误入库，没人会红。
边界：本门只看**顶层**（子目录内部形状不在判据面；那是分层契约的事）。
依赖：`git ls-files`；非 git 树（临时目录）时由合成夹具驱动，不联网、不起应用。
"""
import os
import re
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 顶层条目白名单。新增一项**必须**同时改这里并写理由——这是本门唯一的人工闸门。
#: 判据：能在通行 Python/Web 项目里一眼认出的名字；本项目特有的目录另附一句用途。
ALLOWED_TOP = {
    # 通用文件
    "LICENSE", "README.md", "CHANGELOG.md", "USER_AGREEMENT.md", "PRIVACY_POLICY.md",
    "CONTRIBUTING.md",   # 贡献指南：本项消失即红
    "SECURITY.md",       # 安全政策：本项消失即红
    ".gitignore", ".gitattributes", ".editorconfig", ".dockerignore",
    "pyproject.toml",
    # 项目自有目录与编排件
    "deploy",        # 部署执行件（baremetal/container）
    "docs",          # 契约与开发文档
    "scripts",       # 门禁与发布脚本
    "tests",         # Python 门与 fixtures
    ".github",       # CI 与 PR 模板
    "server",        # Go 后端（结构与门禁见 server/structure.tsv）
    "ui",            # React 前端（Adminator）
    "STRUCTURE.md",  # 仓库结构说明
    "VERSION",       # 产品版本单源
}

#: 顶层条目数的下限：白名单再小也不会少于这个数。用于"扫空不得当绿"。
MIN_TOP = 18


def _git_top_entries(root):
    """→ 跟踪集的顶层条目集合；非 git 树返回 `None`（由调用方当环境错误处理）。"""
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                             capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        # worktree 的 .git 文件可能指向 Windows 盘符路径（本仓实验线 worktree 即此形态），
        # WSL 侧 git 解析不了。此处只处理一种形状：盘符路径换 /mnt/<盘小写>/。
        # dev-verify 的 resolve_gitdir 另含相对路径、\\?\ 前缀等分支，比这里全。
        # 覆盖不到的形状返回 None（本门不跳过：读不到跟踪集必须响亮，不是绿）。
        gitfile = os.path.join(root, ".git")
        if os.path.isfile(gitfile):
            try:
                with open(gitfile, encoding="utf-8", errors="replace") as fh:
                    line = fh.readline().strip()
                g = line[len("gitdir:"):].strip() if line.startswith("gitdir:") else ""
            except OSError:
                g = ""
            g = g.replace("\\", "/")
            if len(g) > 2 and g[1] == ":":
                g = "/mnt/" + g[0].lower() + g[2:]
            if g:
                out = subprocess.run(["git", "--git-dir", g, "ls-files", "-z"],
                                     capture_output=True, timeout=120)
    if out.returncode != 0:
        return None
    tops = set()
    for path in out.stdout.decode("utf-8", "replace").split(chr(0)):
        if not path:
            continue
        tops.add(path.split("/", 1)[0])
    return tops


class TopLevelStructureTest(unittest.TestCase):
    """真实跟踪集的顶层形状。"""

    def test_scan_is_not_empty(self):
        """扫空不得当绿：跟踪集读不到时上面的包含断言恒真。"""
        tops = _git_top_entries(BASE)
        self.assertIsNotNone(tops, "读不到 git 跟踪集：本门无法判定（不是绿）")
        self.assertGreaterEqual(len(tops), MIN_TOP,
                                f"顶层条目只有 {len(tops)} 个（下限 {MIN_TOP}）："
                                "扫描口径坏了，'⊆ 白名单'会变成恒真断言")

    def test_top_level_entries_are_all_registered(self):
        tops = _git_top_entries(BASE)
        self.assertIsNotNone(tops, "读不到 git 跟踪集：本门无法判定（不是绿）")
        unregistered = sorted(tops - ALLOWED_TOP)
        self.assertEqual(
            unregistered, [],
            "跟踪集里出现未登记的顶层条目：" + "；".join(unregistered)
            + "。新顶层条目必须在 tests/test_repo_structure_gate.py 的 ALLOWED_TOP 登记"
              "并写清用途；若它本不该入库，改 .gitignore 并把它从索引里删掉。")

    def test_allowlist_has_no_stale_entries(self):
        """反向：白名单里不该有人已经搬走/删掉的条目（防"白名单变成垃圾桶"）。"""
        tops = _git_top_entries(BASE)
        self.assertIsNotNone(tops, "读不到 git 跟踪集：本门无法判定（不是绿）")
        stale = sorted(ALLOWED_TOP - tops)
        self.assertEqual(stale, [], "白名单里的条目已不在跟踪集，应删掉登记：" + "；".join(stale))


class DocsIndexWhitelistTest(unittest.TestCase):
    """README 索引与 docs/dev 跟踪集一致，且每篇都被 .gitignore 放行。"""

    def test_every_indexed_doc_is_not_ignored(self):
        readme = os.path.join(BASE, "docs", "dev", "README.md")
        with open(readme, encoding="utf-8") as fh:
            text = fh.read()
        names = set(re.findall(r"^\|\s*`([^`]+\.md)`\s*\|", text, re.MULTILINE))
        tracked = subprocess.check_output(
            ["git", "-C", BASE, "ls-files", "docs/dev"], text=True).split()
        expected = {os.path.basename(p) for p in tracked} - {"README.md"}
        self.assertGreaterEqual(len(expected), 5,
                                "docs/dev 跟踪集为空或异常：本门扫空不得当绿")
        self.assertEqual(
            names, expected,
            "README 索引与跟踪集不一致：缺行=%s；多行=%s"
            % (sorted(expected - names), sorted(names - expected)))
        for name in sorted(names):
            rel = os.path.join("docs", "dev", name)
            self.assertTrue(os.path.isfile(os.path.join(BASE, rel)),
                            f"README 索引指向不存在的文件：{rel}")
            proc = subprocess.run(
                ["git", "-C", BASE, "check-ignore", "-q", "--no-index", rel],
                capture_output=True, timeout=120)
            self.assertEqual(
                proc.returncode, 1,
                f"{rel} 在 README 索引里但被 .gitignore 忽略（rc={proc.returncode}）；"
                "必须补 !docs/dev/<name> 白名单行，否则文件会被静默忽略")


class ScannerHasTeethTest(unittest.TestCase):
    """门自身要有牙：合成集里的未登记条目必须被点名。"""

    def test_synthetic_unregistered_entry_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "_census"))
            with open(os.path.join(tmp, "_census", "roster.csv"), "w", encoding="utf-8") as f:
                f.write("a,b\n")
            subprocess.run(["git", "init", "-q", tmp], check=True,
                           capture_output=True, timeout=120)
            subprocess.run(["git", "-C", tmp, "add", "-A"], check=True,
                           capture_output=True, timeout=120)
            tops = _git_top_entries(tmp)
            self.assertIsNotNone(tops, "合成 git 树应能读出跟踪集")
            self.assertEqual(sorted(tops - ALLOWED_TOP), ["_census"],
                             "未登记的顶层目录必须被点名（否则本门无牙）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
