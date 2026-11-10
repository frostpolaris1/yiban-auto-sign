# -*- coding: utf-8 -*-
"""版本号单一来源 + 轮次横幅带版本号（发布门槛的自证前提）。

标签：J · 运维：部署/备份/发布
覆盖：版本字面量只在 `yiban/__init__.py` 出现（CHANGELOG 除外）、web 侧从包引用版本、
    CHANGELOG 顶部条目等于两线之一（`__version__` 或根 VERSION）、引擎轮次横幅与
    汇总行确实打了版本号、用的是本项目版本而非易班 App 版本。
对应实现：`yiban/__init__.py`（唯一来源）、`web/app.py`、`yiban/app/runner.py`
    （横幅/汇总行，兼容壳 `scripts/signin.py` 只剩转发）。
关键断言：版本号一旦分叉，"日志里写的版本"就不再等于"代码的版本"，台账失效且无人察觉
    ——所以钉的是"只有一处定义"，不是"值等于某个字面量"。
依赖：全程静态（读源码文本 + os.walk 扫运行时目录），不联网、不起进程、不需 bash。

**为什么需要这组用例**：`main` 的门槛要求"同一提交在生产机上完成 ≥3 个有效轮次"
（PROMPT.md §6.10 / docs/dev/release-gate.md），生产日志不自带版本号，台账靠引擎
轮次横幅里的版本号与提交对齐。
用法（项目根目录）：python -m pytest tests/test_release_version_source.py -v
"""
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 版本字面量**允许**出现的位置（其余运行时目录出现即视为第二来源）
ALLOWED_LITERAL_FILES = {
    "yiban/__init__.py",   # 唯一来源
    "CHANGELOG.md",        # 人读的变更记录
}
SCAN_DIRS = ["scripts", "web", "yiban", "docker"]


def _read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


def _changelog_top_version(text):
    """取 CHANGELOG 首个 `## [vX.Y.Z]` 条目的版本（不带 v）；没有则 None。

    只认标题行形状，不数行号：顶部说明块变长不该让判据失效。
    """
    m = re.search(r"^## \[v(\d+\.\d+\.\d+)\]", text, re.M)
    return m.group(1) if m else None


def _read_root_version():
    """读仓库根 VERSION（Go 线产品版本单一来源）；缺失或格式坏返回 None。

    双行共库：旧 Python 线的 `__version__` 冻结，Go 线用根 VERSION。
    本判据只做「顶部是两线之一」，VERSION 自身格式由
    tests/test_build_version_wiring.py 与 release-check 的 go-version 项钉。
    """
    try:
        with open(os.path.join(BASE, "VERSION"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    if len(lines) != 1:
        return None
    v = lines[0].strip()
    return v if re.fullmatch(r"\d+\.\d+\.\d+", v) else None


def _version_mismatch(changelog_text, allowed):
    """返回不一致说明；一致返回 None。

    allowed 收两线版本（`__version__` 与根 VERSION）：CHANGELOG 只有一份，
    顶部只能写一个版本，故两线中较新的一行即可。
    """
    top = _changelog_top_version(changelog_text)
    if top is None:
        return "CHANGELOG 没有 ## [vX.Y.Z] 条目"
    if top not in allowed:
        return (
            f"CHANGELOG 顶部 v{top} 既不是 yiban/__init__.py 的 __version__ "
            f"也不是根 VERSION（允许：{sorted(allowed)}）"
        )
    return None


class SingleVersionSourceTest(unittest.TestCase):
    def test_version_defined_once(self):
        from yiban import __version__ as v
        self.assertTrue(re.fullmatch(r"\d+\.\d+\.\d+", v), f"版本号格式异常: {v!r}")
        literal = f'"{v}"'  #连引号一起搜：只搜数字会命中注释与 CHANGELOG 里的历史版本
        offenders = []
        for d in SCAN_DIRS:
            root = os.path.join(BASE, d)
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [x for x in dirnames if x != "__pycache__"]
                for name in filenames:
                    if not name.endswith(".py"):
                        continue
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, BASE).replace(os.sep, "/")
                    if rel in ALLOWED_LITERAL_FILES:
                        continue
                    if literal in _read(rel):
                        offenders.append(rel)
        self.assertEqual(
            offenders, [],
            f"版本字面量 {literal} 出现在多个文件：{offenders}；"
            "版本只能定义在 yiban/__init__.py，其它处请引用 __version__",
        )


    def test_changelog_top_entry_matches(self):
        from yiban import __version__ as v
        allowed = {v}
        root = _read_root_version()
        if root is not None:
            allowed.add(root)
        problem = _version_mismatch(_read("CHANGELOG.md"), allowed)
        self.assertIsNone(problem, problem)

    def test_changelog_top_entry_rejects_third_version(self):
        """顶部写成两线之外的第三个版本即红（夹具文本，不动真实 CHANGELOG）。"""
        from yiban import __version__ as v
        allowed = {v}
        root = _read_root_version()
        if root is not None:
            allowed.add(root)
        problem = _version_mismatch(
            "# 更新日志\n\n## [v9.9.9] - 2099-01-01 · 假条目\n", allowed)
        self.assertIsNotNone(problem, "顶部写成第三个版本必须判红")
        self.assertIn("9.9.9", problem)


class EngineLogsReleaseVersionTest(unittest.TestCase):
    """引擎轮次横幅/汇总行必须带版本号——台账的唯一对齐依据。"""

    def setUp(self):
        # 轮次横幅与汇总行随"执行一轮"的入口一起迁进了引擎（`yiban/app/runner.py`），
        # 兼容壳 `scripts/signin.py` 只剩转发：两处一起读，迁移前后都成立。
        self.src = _read("yiban/app/runner.py") + _read("scripts/signin.py")

    def test_round_banner_carries_version(self):
        m = re.search(r"开始执行签到（v\{RELEASE_VERSION\}）", self.src)
        self.assertIsNotNone(
            m, "轮次横幅缺少版本号：发布门槛无法把生产轮次与提交对齐"
        )

    def test_summary_line_carries_version(self):
        self.assertIn("签到汇总（v{RELEASE_VERSION}）", self.src)

    def test_release_version_is_our_version_not_yiban_app_version(self):
        """横幅里用的必须是**本项目**版本（勿与易班 App 版本 YIBAN_APP_VERSION 混同）。"""
        self.assertRegex(
            self.src, r"__version__ as RELEASE_VERSION",
            "引擎没有以 RELEASE_VERSION 名义引用本项目版本",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
