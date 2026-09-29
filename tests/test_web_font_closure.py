# -*- coding: utf-8 -*-
"""回归守卫：CJK 分片脚本 outdir 白名单的进程级行为。

标签：F · 前端与界面守卫
覆盖：分片脚本（`scripts/build_cjk_font_slices.py`）对越界 outdir 以退出码拒绝且**目标目录未被动过**（它是位置参数 + 整目录 rmtree）
对应实现：`scripts/build_cjk_font_slices.py` 的白名单判定
关键断言：白名单外必须 returncode==2 且 stderr 点名「白名单」，落盘哨兵文件与目录本体都不动；白名单根自身与「前缀相似」的同级目录都不算命中（后者防字符串前缀误判）；白名单内的合法落点必须过闸
依赖：⚠ 需要**真跑子进程**——脚本导入期即依赖 fonttools（构建期依赖），缺依赖时整组 SkipTest

> 批 6c3-A 说明：旧 `FontStackClosureTest`（字体栈令牌正确性 / 无旁路 / Adminator
> 收口对账，共 3 条）为「精确源码 grep」型静态守卫、无行为 owner，按对表裁撤。
"""

import os
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FontSliceOutdirGuardTest(unittest.TestCase):
    """分片脚本的 outdir 是位置参数、随后被整目录 `rmtree`：白名单外必须拒绝。

    起子进程断言进程级行为（退出码 + 目标目录未被动过）。脚本导入期即依赖
    fonttools（**构建期**依赖，不在 requirements 里），缺依赖时跳过本组。
    """

    SCRIPT = os.path.join(BASE, "scripts", "build_cjk_font_slices.py")

    @classmethod
    def setUpClass(cls):
        import importlib.util
        if importlib.util.find_spec("fontTools") is None:
            raise unittest.SkipTest("fonttools 未安装（构建期依赖，本组跳过）")

    def _run(self, outdir):
        return subprocess.run(
            [sys.executable, self.SCRIPT, outdir, "--font", "fake.ttf"],
            capture_output=True, text=True, timeout=120, cwd=BASE)

    def test_outside_repo_is_refused_and_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            keep = os.path.join(d, "keep.txt")
            with open(keep, "w", encoding="utf-8") as f:
                f.write("sentinel")
            r = self._run(d)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("白名单", r.stderr)
            self.assertTrue(os.path.isfile(keep), "被拒绝的输出目录不得被动过")
            self.assertTrue(os.path.isdir(d), "被拒绝的输出目录不得被删")

    def test_whitelist_root_and_prefix_sibling_refused(self):
        """白名单根自身与"前缀相似"的同级目录都不算命中（后者防字符串前缀误判）。"""
        for rel in ("web/static", "web/static-evil"):
            with self.subTest(rel=rel):
                r = self._run(rel)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("白名单", r.stderr)

    def test_under_whitelist_passes_the_guard(self):
        """白名单内的合法落点必须过闸（后续因假字体失败，但不再是白名单拒绝）。"""
        r = self._run("web/static/vendor/fonts/notosanssc-guardprobe")
        self.assertNotEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("白名单", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
