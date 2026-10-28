# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""SSE 缺省值门：路由不再写 15/500 字面量，缺省只从配置名册取。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`web.services.run_stream.stream_config` 的 `heartbeat_sec/gap_max` 缺省、
    `heartbeat_default_sec()` 与 `web.routes.stream_api._stream_generator` 的取值行。
对应实现：缺省唯一定义点是 `config/registry.json` 的
    `YIBAN_SSE_HEARTBEAT_SEC`（15）与 `YIBAN_SSE_GAP_MAX`（500）。
关键断言：
  ① 名册缺省值仍是 15 / 500（本批零行为）；
  ② 空覆盖与非法覆盖都回落名册缺省；
  ③ `gap_max` 在任何输入下都是整数（证明路由删掉的 `else 500` 是不可达分支）；
  ④ 显式写 0 时心跳回落到 `heartbeat_default_sec()`（保留旧行为）；
  ⑤ 路由源码里不再出现 `or 15` / `else 500` 形态（检测器对合成文本自证）。
依赖：只读名册与源码；不起服务器、不发网络请求。
"""

import os
import re
import unittest

from web.services import run_stream

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STREAM_API = os.path.join(BASE, "web", "routes", "stream_api.py")

#: 路由侧字面量缺省的两种历史形态。
BARE_DEFAULT = re.compile(r"\bor\s+15\b|\belse\s+500\b")


def _cfg(overrides=None):
    env = dict(overrides or {})
    return run_stream.stream_config("unused.env", lambda _path: env)


def bare_defaults(text):
    """源码 → 命中字面量缺省的行列表；空文本回空列表。"""
    return [line.strip() for line in text.splitlines() if BARE_DEFAULT.search(line)]


class SseDefaultTest(unittest.TestCase):
    """名册缺省、回落行为与源码形态。"""

    def test_registry_defaults_are_the_documented_values(self):
        self.assertEqual(
            run_stream.config_loader.default_of("YIBAN_SSE_HEARTBEAT_SEC"), 15)
        self.assertEqual(
            run_stream.config_loader.default_of("YIBAN_SSE_GAP_MAX"), 500)

    def test_empty_env_uses_registry_defaults(self):
        cfg = _cfg()
        self.assertEqual(cfg["heartbeat_sec"], 15)
        self.assertEqual(cfg["gap_max"], 500)

    def test_invalid_values_fall_back_to_registry_defaults(self):
        cfg = _cfg({"YIBAN_SSE_HEARTBEAT_SEC": "abc", "YIBAN_SSE_GAP_MAX": "-1"})
        self.assertEqual(cfg["heartbeat_sec"], 15)
        self.assertEqual(cfg["gap_max"], 500)

    def test_gap_max_is_never_none(self):
        for overrides in ({}, {"YIBAN_SSE_GAP_MAX": "0"},
                          {"YIBAN_SSE_GAP_MAX": "nope"}):
            with self.subTest(overrides=overrides):
                self.assertIsInstance(_cfg(overrides)["gap_max"], int)

    def test_heartbeat_zero_falls_back_to_registry_default(self):
        # 旧行是 `cfg["heartbeat_sec"] or 15`：0 被当假值回落到 15。删字面量后
        # 必须仍回落到**同一个值**，只是它现在从名册取。
        self.assertEqual(_cfg({"YIBAN_SSE_HEARTBEAT_SEC": "0"})["heartbeat_sec"], 0)
        self.assertEqual(run_stream.heartbeat_default_sec(), 15)
        self.assertEqual(0 or run_stream.heartbeat_default_sec(), 15)

    def test_route_source_has_no_literal_defaults(self):
        with open(STREAM_API, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(bare_defaults(text), [],
                         "stream_api.py 又出现 15/500 字面量缺省")
        self.assertIn("heartbeat_default_sec()", text,
                      "心跳兜底未改读名册缺省")


class DetectorTeethTest(unittest.TestCase):
    """检测器对两种历史形态都必须报红。"""

    def test_detects_both_shapes(self):
        text = 'a = cfg["h"] or 15\nb = cfg["g"] if cfg["g"] is not None else 500\n'
        self.assertEqual(len(bare_defaults(text)), 2)

    def test_accepts_registry_reference(self):
        text = 'a = cfg["h"] or run_stream.heartbeat_default_sec()\nb = cfg["g"]\n'
        self.assertEqual(bare_defaults(text), [])


if __name__ == "__main__":
    unittest.main()
