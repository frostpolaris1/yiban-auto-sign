# -*- coding: utf-8 -*-
"""run.sh 契约（2026-09-08，静态源码核验）。

.env 解析口径与 web 侧 env_io.parse_env_file 对齐：
- 去行首 UTF-8 BOM（Windows 记事本保存常见；原实现首行键名带 BOM 前缀被
  键名校验拒掉，宿主 cron 与 web 配置各说各话）；
- key/value 两侧剥空白（原实现 value 只剥尾部 CR，`KEY = v` / 值带尾随
  空格时两侧读法漂移）。

文本级断言（显式 utf-8 读脚本）：脚本含中文输出，Windows 子进程 GBK 解码会
误报失败，故不跑子进程；bash 可用时另以 `bash -n` 做语法核验（只解析不执行）。
"""
import os
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RunshEnvParseTest(unittest.TestCase):
    """run.sh .env 解析与 env_io.parse_env_file 同口径。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BASE, "run.sh"), encoding="utf-8") as f:
            cls.src = f.read()

    def test_strips_utf8_bom_from_key(self):
        self.assertIn(r"$'\xEF\xBB\xBF'", self.src,
                      "须剥离行首 UTF-8 BOM（Windows 记事本保存常见，否则首键被拒）")
        # BOM 剥离作用于 key（行首），而非 value
        self.assertIn('key="${key#$', self.src)

    def test_strips_value_whitespace_both_sides(self):
        # key 两侧剥空白为既有行为；value 两侧剥空白为本次对齐点
        self.assertIn('value="${value#"${value%%[![:space:]]*}"}"', self.src)
        self.assertIn('value="${value%"${value##*[![:space:]]}"}"', self.src)

    def test_export_rule_unchanged(self):
        # 只导出 YIBAN_ 前缀 + 合法键名的既有白名单不得松动
        self.assertIn('[[ ! "$key" =~ ^YIBAN_ ]]', self.src)

    def test_bash_syntax_ok(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("无 bash 环境")
        r = subprocess.run([bash, "-n", os.path.join(BASE, "run.sh")],
                           capture_output=True)
        self.assertEqual(r.returncode, 0,
                         f"run.sh 语法错误：{r.stderr.decode('utf-8', errors='replace')}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
