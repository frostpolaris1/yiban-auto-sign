# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""应用层（app）：用例编排。无算法，无 SQL。

本层按目标架构契约（docs/dev/architecture-target.md 第 1、2、3 节）落位。
事务边界在本层。本层只调 core 的纯函数与 ports 的接口声明。

成员（批 B 从 `yiban/engine/` 搬入）：
- `round.py` 跑一轮（原 `engine/round.py`）；
- `runner.py` 入口与轮次编排（原 `engine/runner.py`）；
- `probe.py` 探针（原 `engine/probe.py`）。

旧坐标 `yiban.engine.{round,runner,probe}` 留转发壳。

**未搬入（需拆分，属第三刀）**：契约第 3 节把 `engine/executor_v3.py` 映射成
`app/sign_one` ＋ `app/ingest`（＋ `app/coord`）。那是"一个文件拆成多个"，本批是搬迁批、不拆。
同节映射表把 `engine/workers.py` 整文件移到 `workers/`（批 B 已搬妥，旧坐标留壳）；
但契约第 1 节把「协调 · 派发与工作体池」画在**本层**（`app/coord`）——那把 `workers.py`
的编排面拆上来的活，与 `executor_v3` 的拆分同属第三刀。

依赖方向：entry → app → core。adapters 实现 ports，不得反向导入本层。
"""
