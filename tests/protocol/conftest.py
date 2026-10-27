"""协议库测试（`yiban/adapters/protocol_yiban/`）的共享辅助。

**来源**：本目录与 `fixtures/` 随 2026-10-07 换核从独立库仓库的测试套件整体迁入，
import 一律改写为 `yiban.adapters.protocol_yiban`。

**来源与脱敏（如实）**：夹具形状来自**运营者自有账号**的旁路实拍；入库值全部为合成值。
姓名 / 学号 / 学校名 / 电话 / 会话令牌 / 坐标 / 用户标识 / 学校标识都按固定规则替换：
坐标平移到 Null Island 测试网格，令牌换成等长重复字符，标识换成零前缀序号。
原始实拍件加密存放，不入仓库（AGENTS.md §十六）。使用夹具时按「全部为合成值」对待。

测试天然离线：只读取 `fixtures/` 下冻结的黄金夹具（绝不改动），不触网。
`test_identity.py` 与 `test_envelopes.py` 覆盖的 `parse_identity` /
`parse_usersure_response` **当前没有生产消费者**：这两组用例是**库资产保全**
（保住解析契约），不算主仓链路的回归。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _read_json(name: str):
    return json.loads(_read(name))


@pytest.fixture(scope="session")
def read_fixture():
    """返回一个按 UTF-8 读取夹具文本的可调用对象。"""
    return _read


@pytest.fixture(scope="session")
def read_fixture_json():
    """返回一个读取并 JSON 解码夹具的可调用对象。"""
    return _read_json
