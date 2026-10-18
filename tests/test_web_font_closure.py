# -*- coding: utf-8 -*-
"""回归守卫：CJK 分片脚本 outdir 白名单的进程级行为。

标签：F · 前端与界面守卫
覆盖：分片脚本（`scripts/build_cjk_font_slices.py`）对越界 outdir 以退出码拒绝且**目标目录未被动过**（它是位置参数 + 整目录 rmtree）
对应实现：`scripts/build_cjk_font_slices.py` 的白名单判定
关键断言：白名单外必须 returncode==2 且 stderr 点名「白名单」，落盘哨兵文件与目录本体都不动；白名单根自身与「前缀相似」的同级目录都不算命中（后者防字符串前缀误判）；白名单内的合法落点必须过闸
依赖：⚠ 需要**真跑子进程**；缺 fonttools（构建期依赖，不在 requirements 里）时**用替身包顶上**，不再整组 SkipTest

> 批 6c3-A 说明：旧 `FontStackClosureTest`（字体栈令牌正确性 / 无旁路 / Adminator
> 收口对账，共 3 条）为「精确源码 grep」型静态守卫、无行为 owner，按对表裁撤。

> 保真度车道 2026-10-16 订正：本组原先在 `setUpClass`
> 里 `raise SkipTest`，而 fonttools 在任何一轨 CI 上都不存在（本地权威 root 轨实测
> `SKIPPED [3]`）⇒ **这扇门在所有环境上都不执行**，而它挡的是"位置参数 outdir 被整目录
> `rmtree`"这类删除事故（AGENTS.md §十三.30：跳过 ≠ 绿；整套没跑却报绿等于门不存在）。
> 订正做法：白名单判定发生在**导入期依赖之后**，但那条判定不需要 fontTools 的**功能**。
> 故缺依赖时在子进程的 `PYTHONPATH` 前挂一个**替身包**（只提供导入面，不实现任何切片），
> 让白名单那条路径照跑。有真依赖时用真的（替身不挂）。
"""

import os
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: fontTools 的**导入面**替身（只让 `scripts/build_cjk_font_slices.py` 的模块级 import
#: 通过；白名单判定与它无关，越界路径在用到字体功能之前就已 `sys.exit(2)`）。
_STUB_FILES = {
    "fontTools/__init__.py": "",
    "fontTools/subset.py": "",
    "fontTools/ttLib/__init__.py": "class TTFont:\n    pass\n\n\ndef newTable(*a, **k):\n    return None\n",
    "fontTools/varLib/__init__.py": "instancer = None\n",
}


class FontSliceOutdirGuardTest(unittest.TestCase):
    """分片脚本的 outdir 是位置参数、随后被整目录 `rmtree`：白名单外必须拒绝。

    起子进程断言进程级行为（退出码 + 目标目录未被动过）。脚本导入期即依赖
    fonttools（**构建期**依赖，不在 requirements 里）：缺依赖时挂替身包，不跳过。
    """

    SCRIPT = os.path.join(BASE, "scripts", "build_cjk_font_slices.py")

    @classmethod
    def setUpClass(cls):
        import importlib.util
        #: 有真依赖就用真的；没有就挂替身。两条路都让本组**真的跑**。
        cls._stub_dir = None
        if importlib.util.find_spec("fontTools") is None:
            cls._stub_dir = tempfile.mkdtemp(prefix="yiban-fonttools-stub-")
            for rel, body in _STUB_FILES.items():
                path = os.path.join(cls._stub_dir, *rel.split("/"))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(body)

    @classmethod
    def tearDownClass(cls):
        if cls._stub_dir:
            import shutil
            shutil.rmtree(cls._stub_dir, ignore_errors=True)

    def _env(self):
        env = dict(os.environ)
        if self._stub_dir:
            env["PYTHONPATH"] = os.pathsep.join(
                [self._stub_dir] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        return env

    def _run(self, outdir):
        return subprocess.run(
            [sys.executable, self.SCRIPT, outdir, "--font", "fake.ttf"],
            capture_output=True, text=True, timeout=120, cwd=BASE, env=self._env())

    def test_依赖面真的就绪(self):
        """前置就绪：本组跑的每一轮都必须能真的到达白名单判定。

        判据：白名单外的 outdir 必须给出**白名单拒绝**（rc=2 且 stderr 点名「白名单」）。
        若子进程因缺依赖在导入期就 `sys.exit`，这里会是别的退出码与别的文案 ⇒ 红。
        少了这条，"本组其实整组没跑"与"本组全过"同形（AGENTS.md §十三.30）。
        """
        with tempfile.TemporaryDirectory() as d:
            r = self._run(d)
            self.assertEqual(
                r.returncode, 2,
                "前置就绪：子进程没能到达白名单判定（rc=%s）。stdout=%r stderr=%r"
                % (r.returncode, r.stdout[-300:], r.stderr[-300:]))
            self.assertIn("白名单", r.stderr)

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
