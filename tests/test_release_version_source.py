# -*- coding: utf-8 -*-
"""版本号单一来源 + 轮次横幅带版本号（发布门槛的自证前提）。

标签：J · 运维：部署/备份/发布
覆盖：版本字面量只在 `yiban/__init__.py` 出现（CHANGELOG 除外）、web 侧从包引用版本、
    CHANGELOG 顶部条目与包版本一致、引擎轮次横幅与汇总行确实打了版本号、
    用的是本项目版本而非易班 App 版本。
对应实现：`yiban/__init__.py`（唯一来源）、`web/app.py`、`yiban/engine/runner.py`
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

    def test_web_side_takes_version_from_the_package(self):
        """网页侧从 yiban 取版本，不自己定义（`web/__init__.py` 是纯包说明）。

        运行时取值的一致性由 `test_web_security_gates::test_version_synced` 断言
        （那里用的是隔离加载的 app 模块）；本文件只做静态检查，避免在导入期碰 .env。
        """
        self.assertIn("from yiban import __version__ as APP_VERSION", _read("web/app.py"))
        self.assertNotIn("__version__ =", _read("web/__init__.py"))

    def test_changelog_top_entry_matches(self):
        from yiban import __version__ as v
        head = "\n".join(_read("CHANGELOG.md").splitlines()[:8])  #只看顶部 8 行：对的是最新条目，全文件搜会命中旧版本而恒成立
        self.assertIn(f"v{v}", head, "CHANGELOG 顶部条目与本项目版本号不一致")


class EngineLogsReleaseVersionTest(unittest.TestCase):
    """引擎轮次横幅/汇总行必须带版本号——台账的唯一对齐依据。"""

    def setUp(self):
        # 轮次横幅与汇总行随"执行一轮"的入口一起迁进了引擎（`yiban/engine/runner.py`），
        # 兼容壳 `scripts/signin.py` 只剩转发：两处一起读，迁移前后都成立。
        self.src = _read("yiban/engine/runner.py") + _read("scripts/signin.py")

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
