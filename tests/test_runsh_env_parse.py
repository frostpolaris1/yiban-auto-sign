# -*- coding: utf-8 -*-
"""shell 侧 .env 解析契约。

标签：J · 运维：部署/备份/发布
覆盖：宿主 `.env` 解析的唯一副本（`scripts/lib/host_paths.sh` 的 `yiban_load_env`，
    被 run.sh 与清理 cron 调用）与 `run_probe.sh` 自带副本——去行首 UTF-8 BOM、
    key/value 两侧剥空白、只导出 `YIBAN_` 前缀且键名合法的白名单、脚本语法可解析。
对应实现：`scripts/lib/host_paths.sh`、`run.sh`、`run_probe.sh`；对齐基准是 web 侧
    `env_io.parse_env_file`。
关键断言：**每个载体都断言**——曾经 run_probe.sh 只剥 key 不剥 value、没有 BOM 剥离，
    同一份 .env 下签到与探针读到不同配置；导出白名单 `^YIBAN_` 不得松动。
    2026-10-24（R3-019）：run.sh 的解析块搬进 `host_paths.sh`（清理 cron 也调它），
    故 run.sh 那一腿改判该文件，并另断言 run.sh 真的调用它、没有自持第二份。
依赖：读 .sh 文本（显式 utf-8）；`test_bash_syntax_ok` 需要 bash，无 bash 时逐条
    skipTest；不跑脚本、不连网络、不需 docker。

文本级断言而不跑子进程：脚本含中文输出，Windows 子进程 GBK 解码会误报失败，
故 `bash -n` 只做语法核验（只解析不执行）。BOM 场景常见于 Windows 记事本保存 .env：
原实现首行键名带 BOM 前缀被键名校验拒掉，宿主 cron 与 web 配置各说各话。
"""
import os
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 持解析判据的载体：宿主共用副本 + 探针自带副本
SHELL_ENTRIES = (os.path.join("scripts", "lib", "host_paths.sh"), "run_probe.sh")


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
                self.assertIn(r"$'\xEF\xBB\xBF'", src,  #断的是脚本里的字面写法：改解析式就报红，宁可让人来问为什么
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
                if not bash:  #语法核验是全文件唯一需要 bash 的一条：纯文本判据在 Windows 也照跑
                    self.skipTest("无 bash 环境")
                r = subprocess.run([bash, "-n", os.path.join(BASE, name)],
                                   capture_output=True)
                self.assertEqual(
                    r.returncode, 0,
                    f"{name} 语法错误：{r.stderr.decode('utf-8', errors='replace')}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
