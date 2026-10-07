"""协议库测试（`yiban/protocol/`）的共享辅助。

**来源**：本目录与 `fixtures/` 随 2026-10-07 换核从独立库仓库的测试套件整体迁入，
import 一律改写为 `yiban.protocol`。

**来源与脱敏（如实）**：夹具源自**运营者自有账号**的旁路实拍。已替换为合成值的是
姓名 / 学号（`PersonId`）/ 学校名 / 电话 / 会话令牌；**坐标保留实拍值**（其合成性无法
自证，故不声称已替换）；`iframe_location.txt` 里的 `yb_uid=85118634` **未做哈希**，
按上游实拍原样保留。使用夹具时按"含真实场地坐标与一个未哈希用户标识"对待。

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
