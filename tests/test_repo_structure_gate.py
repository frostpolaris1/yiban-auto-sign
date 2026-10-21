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
边界：本门只看**顶层**（`web/`、`yiban/` 内部形状不在判据面；那是分层契约的事）。
依赖：`git ls-files`；非 git 树（临时目录）时由合成夹具驱动，不联网、不起应用。
"""
import os
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 顶层条目白名单。新增一项**必须**同时改这里并写理由——这是本门唯一的人工闸门。
#: 判据：能在通行 Python/Web 项目里一眼认出的名字；本项目特有的目录另附一句用途。
ALLOWED_TOP = {
    # 通用文件
    "LICENSE", "README.md", "CHANGELOG.md", "USER_AGREEMENT.md", "PRIVACY_POLICY.md",
    ".gitignore", ".gitattributes", ".editorconfig", ".dockerignore", ".importlinter",
    ".env.example", ".env.docker.example",   # 配置模板：只放占位值，真值不入库
    "pyproject.toml", "requirements.txt", "requirements.lock",
    # 项目自有入口与编排件
    "run.sh", "run_probe.sh", "docker-compose.yml",
    "config",        # 配置名册（config/registry.json）：加载器与配置门共用的唯一定义点
    "deploy",        # 部署执行件（prod 清单、cron、备份 wrapper）
    "docker",        # 容器形态（Dockerfile、supervisord、nginx）
    "docs",          # 只放行契约类（.gitignore 白名单；过程稿不进库）
    "frontend",      # Vue 源码与前端 e2e（产物在 web/static/vue）
    "scripts",       # CLI 入口与门（.sh 用连字符、.py 用下划线，见实现笔记）
    "tests",         # 测试（test_*.py 平铺 + protocol/ 与 golden/ 子目录）
    "web",           # 管理后端（app/routes/services/templates/static）
    "yiban",         # 后端命名空间（core/ports/adapters/engine/app/entry/workers/infra/attempt）
    ".github",       # CI 与 PR 模板
}

#: 顶层条目数的下限：白名单再小也不会少于这个数。用于"扫空不得当绿"。
MIN_TOP = 20


def _git_top_entries(root):
    """→ 跟踪集的顶层条目集合；非 git 树返回 `None`（由调用方当环境错误处理）。"""
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                             capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
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


class ScannerHasTeethTest(unittest.TestCase):
    """门自身要有牙：合成集里的未登记条目必须被点名。"""

    def test_synthetic_unregistered_entry_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "_census"))
            os.makedirs(os.path.join(tmp, "web"))
            with open(os.path.join(tmp, "web", "x.py"), "w", encoding="utf-8") as f:
                f.write("")
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
