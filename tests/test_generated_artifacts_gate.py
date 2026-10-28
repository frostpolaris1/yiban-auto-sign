# -*- coding: utf-8 -*-
"""生成物脚本的门：字体分片打标、Lucide 精灵图与 OpenAPI 生成器（ba-p11-11 / ba-p11-12 / but）。

标签：J · 运维：部署/备份/发布
覆盖：
  · `scripts/stamp_font_versions.py --check` 在真实字体树上必须绿（这条用例就是
    它此前缺失的调用方）；分片内容变化必须红；空目录/无可打标 URL 退 2（量具
    没跑不等于"0 命中"，AGENTS §十三 第 30 条）。
  · `scripts/build_lucide_sprite.py --check`：生成物与来源一致时绿、被就地改动后红；
    来源 `package.json` 版本与 `LUCIDE_VERSION` 不一致时退 2；已提交的 `_sprite.svg`
    头部版本/commit/图标数必须与脚本常量一致（出处记录不许与内容分叉）。
  · `server/scripts/generate.sh` 写模式失败关闭：生成器非零退出、零退出零输出、
    写坏部分后非零，三种形状都必须非零退出且不改动已提交的生成物；成功路径不变。
    脚本字节原样复制进临时树，用假生成器驱动，不触碰仓库里的 oapi.gen.go。
关键断言：钉"生成物可复核、来源可校验、量具没跑不得当绿、写模式失败不得报成功"
四条性质；不钉图标内容。
依赖：只读真实字体树与已提交 sprite；Lucide 用例在临时目录造 70 个最小 SVG。
"""
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAMP = os.path.join(BASE, "scripts", "stamp_font_versions.py")
LUCIDE = os.path.join(BASE, "scripts", "build_lucide_sprite.py")
SPRITE = os.path.join(BASE, "web", "static", "vendor", "lucide", "_sprite.svg")
OPENAPI_GENERATE = os.path.join(BASE, "server", "scripts", "generate.sh")


def _run(script, argv):
    return subprocess.run(
        [sys.executable, script, *argv], cwd=BASE, capture_output=True,
        text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
        timeout=180,
    )


def _load_lucide():
    spec = importlib.util.spec_from_file_location("build_lucide_sprite", LUCIDE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class StampFontVersionsCheckTest(unittest.TestCase):
    def test_real_font_tree_passes_check(self):
        r = _run(STAMP, ["--check"])
        self.assertEqual(r.returncode, 0,
                         "真实字体树存在未打标分片（重新运行 stamp_font_versions.py）：\n"
                         + r.stdout + r.stderr)
        self.assertIn("扫描面：css=", r.stdout)

    def test_fragment_change_reds_the_check(self):
        with tempfile.TemporaryDirectory(prefix="yiban-stamp-mut-") as root:
            with open(os.path.join(root, "x.woff2"), "wb") as f:
                f.write(b"fragment-one")
            css = os.path.join(root, "a.css")
            with open(css, "w", encoding="utf-8", newline="") as f:
                f.write("url('x.woff2')\n")
            first = _run(STAMP, [root])
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            clean = _run(STAMP, ["--check", root])
            self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
            # 突变：分片内容变了，固定 URL 必须变 → --check 红
            with open(os.path.join(root, "x.woff2"), "wb") as f:
                f.write(b"fragment-two")
            mutated = _run(STAMP, ["--check", root])
            self.assertEqual(mutated.returncode, 1,
                             "分片内容变化后 --check 必须红：\n" + mutated.stdout + mutated.stderr)
            self.assertIn("待更新", mutated.stderr)

    def test_empty_dir_is_an_environment_error(self):
        with tempfile.TemporaryDirectory(prefix="yiban-stamp-empty-") as root:
            r = _run(STAMP, ["--check", root])
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("没有任何 CSS", r.stderr)

    def test_css_without_local_woff2_is_an_environment_error(self):
        with tempfile.TemporaryDirectory(prefix="yiban-stamp-nourl-") as root:
            with open(os.path.join(root, "a.css"), "w", encoding="utf-8") as f:
                f.write("url('https://cdn.example/x.woff2')\n")
            r = _run(STAMP, ["--check", root])
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("没有引用本地 woff2", r.stderr)


class LucideSpriteCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="yiban-lucide-")
        cls.repo = os.path.join(cls.tmp.name, "lucide-repo")
        cls.icons = os.path.join(cls.repo, "icons")
        os.makedirs(cls.icons)
        cls.mod = _load_lucide()
        cls._write_manifest(cls.mod.LUCIDE_VERSION)
        for name in cls.mod.ICONS:
            with open(os.path.join(cls.icons, name + ".svg"), "w", encoding="utf-8") as f:
                f.write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
                        '<path d="M1 1h2v2z"/></svg>')
        cls.out = os.path.join(cls.tmp.name, "sprite.svg")

    @classmethod
    def _write_manifest(cls, version):
        with open(os.path.join(cls.repo, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "lucide", "version": version}, f)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_build_then_check_is_green_and_detects_hand_edit(self):
        built = _run(LUCIDE, [self.repo, self.out])
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        clean = _run(LUCIDE, [self.repo, self.out, "--check"])
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        with open(self.out, "a", encoding="utf-8") as f:
            f.write("\n<!-- hand edit -->\n")
        mutated = _run(LUCIDE, [self.repo, self.out, "--check"])
        self.assertEqual(mutated.returncode, 1,
                         "就地热修后 --check 必须红：\n" + mutated.stdout + mutated.stderr)
        # 不带 --check 重建 → 覆盖回来源内容，再 check 绿
        rebuilt = _run(LUCIDE, [self.repo, self.out])
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stdout + rebuilt.stderr)
        self.assertEqual(_run(LUCIDE, [self.repo, self.out, "--check"]).returncode, 0)

    def test_source_version_mismatch_is_an_environment_error(self):
        self._write_manifest("9.9.9")
        try:
            r = _run(LUCIDE, [self.repo, self.out, "--check"])
        finally:
            self._write_manifest(self.mod.LUCIDE_VERSION)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("来源版本不匹配", r.stderr)
        self.assertIn("9.9.9", r.stderr)

    def test_missing_manifest_is_an_environment_error(self):
        manifest = os.path.join(self.repo, "package.json")
        os.remove(manifest)
        try:
            r = _run(LUCIDE, [self.repo, self.out, "--check"])
        finally:
            self._write_manifest(self.mod.LUCIDE_VERSION)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("无法校验版本", r.stderr)

    def test_committed_sprite_header_matches_constants(self):
        with io.open(SPRITE, encoding="utf-8") as f:
            text = f.read()
        self.assertIn(f"Version: {self.mod.LUCIDE_VERSION}", text)
        self.assertIn(f"Commit: {self.mod.LUCIDE_COMMIT}", text)
        m = re.search(r"subset, (\d+) icons", text)
        self.assertIsNotNone(m, "生成物头部缺图标数")
        self.assertEqual(int(m.group(1)), len(self.mod.ICONS),
                         "头部图标数与脚本 ICONS 不一致：生成物需要重建")
        symbols = re.findall(r'<symbol id="i-([^"]+)"', text)
        self.assertEqual(len(symbols), len(self.mod.ICONS))
        self.assertEqual(sorted(symbols), sorted(self.mod.ICONS))


# 假生成器的四种形状。成功形状写一个可复现的定值，便于断言内容。
FAKE_GENERATORS = {
    "ok": """#!/usr/bin/env bash
set -euo pipefail
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out=$2; shift 2; else shift; fi
done
printf '// generated\\n' > "$out"
""",
    "fail": "#!/usr/bin/env bash\nexit 1\n",
    "silent": "#!/usr/bin/env bash\nexit 0\n",
    "partial": """#!/usr/bin/env bash
set -euo pipefail
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out=$2; shift 2; else shift; fi
done
printf 'partial' > "$out"
exit 3
""",
}

SENTINEL = "// checked-in generator output\n"


class OpenAPIGeneratorFailClosedTest(unittest.TestCase):
    """写模式失败开放（but）：生成没成功，脚本不得报 wrote、不得退出 0。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-openapi-generate-")
        self.root = os.path.join(self.tmp.name, "server")
        os.makedirs(os.path.join(self.root, "scripts"))
        shutil.copyfile(OPENAPI_GENERATE,
                        os.path.join(self.root, "scripts", "generate.sh"))
        for name, body in FAKE_GENERATORS.items():
            with open(os.path.join(self.root, "gen_%s.sh" % name),
                      "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
        self.out = os.path.join(self.root, "internal", "entry", "http", "oapi",
                                "oapi.gen.go")
        os.makedirs(os.path.dirname(self.out))
        self._write_out(SENTINEL)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_out(self, text):
        with open(self.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def _out(self):
        with open(self.out, encoding="utf-8") as f:
            return f.read()

    def _generate(self, fake, *argv):
        env = dict(os.environ, OAPI_CODEGEN="bash gen_%s.sh" % fake)
        return subprocess.run(
            ["bash", os.path.join(self.root, "scripts", "generate.sh"), *argv],
            cwd=self.root, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
            timeout=60,
        )

    def assert_not_a_write(self, r):
        self.assertNotEqual(r.returncode, 0, "生成失败必须非零退出：\n" + r.stdout + r.stderr)
        self.assertNotIn("wrote", r.stdout, "生成失败不得打印 wrote")
        self.assertEqual(self._out(), SENTINEL, "生成失败不得改动已提交的生成物")

    def test_failing_generator_is_not_a_write(self):
        self.assert_not_a_write(self._generate("fail"))

    def test_silent_generator_is_an_environment_error(self):
        r = self._generate("silent")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("DID NOT RUN", r.stderr)
        self.assertEqual(self._out(), SENTINEL)

    def test_partial_output_then_failure_is_not_a_write(self):
        self.assert_not_a_write(self._generate("partial"))

    def test_success_writes_and_check_agrees(self):
        r = self._generate("ok")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._out(), "// generated\n")
        self.assertIn("wrote internal/entry/http/oapi/oapi.gen.go", r.stdout)
        c = self._generate("ok", "--check")
        self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
        self.assertIn("up to date", c.stdout)


if __name__ == "__main__":
    unittest.main()
