# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""OpenAPI 生成器写模式的失败关闭契约。

标签：J · 运维：部署/备份/发布
覆盖：server/scripts/generate.sh 写模式失败关闭——生成器非零退出、零退出零输出、
    写坏部分后非零，三种形状都必须非零退出且不改动已提交的生成物；成功路径不变。
    脚本字节原样复制进临时树，用假生成器驱动，不触碰仓库里的 oapi.gen.go。
关键断言：钉「生成没成功不得报 wrote、不得退出 0、不得改动已提交生成物」；
    不钉生成器内部实现。
依赖：只读脚本 + 临时目录；无网络、无第三方库。
"""
import os
import shutil
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPENAPI_GENERATE = os.path.join(BASE, "server", "scripts", "generate.sh")

# 假生成器的四种形状。成功形状写一个可复现的定值，便于断言内容。
FAKE_GENERATORS = {
    "ok": """#!/usr/bin/env bash
set -euo pipefail
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out=$2; shift 2; else shift; fi
done
printf '// generated\n' > "$out"
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
