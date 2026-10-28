# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""ruff 门禁配置守卫（工单 yiban-auto-sign-n3il）。

本文件钉住四处**极易被无声摘除**的配置，防止回潮：
① `pyproject.toml` 的 `[tool.ruff.lint].select` 含 `DTZ`（时区静态守卫开着）。
② 同一 `ignore` 不含 `DTZ005` / `DTZ006`（旧的"本地时间有意设计"化石豁免已清除）。
③ `scripts/dev-verify.sh` 的两条 ruff 执行命令都扫到 `docker/` 与 `tools/`（扫描面是六路径）。
④ `tests/**` 的 DTZ 豁免在 `[tool.ruff.lint.per-file-ignores]` 注册在案（豁免面收窄即红）。

为什么需要它：三处都是"删一行就悄悄关掉一条门禁、测试仍全绿"的形状。
本文件只读配置文本，不做运行，故薄且快。
"""
import os
import unittest

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11 无 tomllib；门禁环境为 3.13+（WSL 固定 venv 为 3.14）
    import tomli as tomllib  # type: ignore[no-redef]

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYPROJECT = os.path.join(BASE, "pyproject.toml")
ENTRY = os.path.join(BASE, "scripts", "dev-verify.sh")


class RuffConfigGuardTest(unittest.TestCase):
    def setUp(self):
        with open(PYPROJECT, "rb") as f:
            self.cfg = tomllib.load(f)
        with open(ENTRY, encoding="utf-8") as f:
            self.entry = f.read()

    def test_dtz_is_selected(self):
        """DTZ 必须在 select 里：摘掉它 = 悄悄关掉时区静态守卫。"""
        select = self.cfg["tool"]["ruff"]["lint"]["select"]
        self.assertIn("DTZ", select, f"ruff select 丢了 DTZ：{select}")

    def test_dtz005_and_dtz006_are_not_ignored(self):
        """DTZ005/DTZ006 不得回到 ignore：那两行是时区缺陷修复前的化石豁免。"""
        ignore = self.cfg["tool"]["ruff"]["lint"]["ignore"]
        for code in ("DTZ005", "DTZ006"):
            self.assertNotIn(code, ignore, f"ruff ignore 又放回了 {code}：{ignore}")

    def test_tests_tree_dtz_per_file_ignore_is_registered(self):
        """tests/** 的 DTZ 整体豁免必须登记在 lint.per-file-ignores（生产面逐处 noqa）。"""
        pfi = self.cfg["tool"]["ruff"]["lint"]["per-file-ignores"]
        self.assertIn("DTZ", pfi.get("tests/**", []),
                      f'tests/** 的 DTZ 豁免丢了：{pfi}')

    def test_dev_verify_ruff_commands_scan_docker(self):
        """dev-verify.sh 的两条 ruff 执行命令都须含 docker/ 与 tools/（两种模式各一条）。"""
        for cmd in ('"$py" -m ruff check yiban/ tests/ scripts/ web/ docker/ tools/ --quiet',
                    '"$PY" -m ruff check yiban/ tests/ scripts/ web/ docker/ tools/ --quiet'):
            self.assertIn(cmd, self.entry,
                          f"dev-verify.sh 的 ruff 命令丢了 docker/：{cmd}")


if __name__ == "__main__":
    unittest.main()
