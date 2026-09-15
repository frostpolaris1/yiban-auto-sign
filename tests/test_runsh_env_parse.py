# -*- coding: utf-8 -*-
"""shell 侧 .env 解析契约（2026-09-08 起；2026-09-15 扩到 run_probe.sh）。

`run.sh`（宿主签到入口）与 `run_probe.sh`（探针入口）读同一份 `.env`，两者的解析
口径必须与 web 侧 `env_io.parse_env_file` 一致：
- 去行首 UTF-8 BOM（Windows 记事本保存常见；原实现首行键名带 BOM 前缀被
  键名校验拒掉，宿主 cron 与 web 配置各说各话）；
- key/value 两侧剥空白（原实现 value 只剥尾部 CR，`KEY = v` / 值带尾随
  空格时两侧读法漂移）。

**两份脚本都断言**：曾经 run_probe.sh 只剥 key 不剥 value、也没有 BOM 剥离，
导致同一份 `.env` 下签到与探针读到不同配置。

文本级断言（显式 utf-8 读脚本）：脚本含中文输出，Windows 子进程 GBK 解码会
误报失败，故不跑子进程；bash 可用时另以 `bash -n` 做语法核验（只解析不执行）。
"""
import os
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 读同一份 .env 的两个入口脚本：解析口径必须一致
SHELL_ENTRIES = ("run.sh", "run_probe.sh")


class RunshEnvParseTest(unittest.TestCase):
    """run.sh / run_probe.sh 的 .env 解析与 env_io.parse_env_file 同口径。"""

    @classmethod
    def setUpClass(cls):
        cls.srcs = {}
        for name in SHELL_ENTRIES:
            with open(os.path.join(BASE, name), encoding="utf-8") as f:
                cls.srcs[name] = f.read()

    def test_strips_utf8_bom_from_key(self):
        for name, src in self.srcs.items():
            with self.subTest(script=name):
                self.assertIn(r"$'\xEF\xBB\xBF'", src,
                              "须剥离行首 UTF-8 BOM（Windows 记事本保存常见，否则首键被拒）")
                # BOM 剥离作用于 key（行首），而非 value
                self.assertIn('key="${key#$', src)

    def test_strips_value_whitespace_both_sides(self):
        for name, src in self.srcs.items():
            with self.subTest(script=name):
                # key 两侧剥空白为既有行为；value 两侧剥空白为对齐点
                self.assertIn('value="${value#"${value%%[![:space:]]*}"}"', src)
                self.assertIn('value="${value%"${value##*[![:space:]]}"}"', src)

    def test_export_rule_unchanged(self):
        for name, src in self.srcs.items():
            with self.subTest(script=name):
                # 只导出 YIBAN_ 前缀 + 合法键名的既有白名单不得松动
                self.assertIn('[[ ! "$key" =~ ^YIBAN_ ]]', src)

    def test_bash_syntax_ok(self):
        for name in SHELL_ENTRIES:
            with self.subTest(script=name):
                bash = shutil.which("bash")
                if not bash:
                    self.skipTest("无 bash 环境")
                r = subprocess.run([bash, "-n", os.path.join(BASE, name)],
                                   capture_output=True)
                self.assertEqual(
                    r.returncode, 0,
                    f"{name} 语法错误：{r.stderr.decode('utf-8', errors='replace')}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
