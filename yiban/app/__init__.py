# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""应用层（app）：用例编排。无算法，无 SQL。

本层按目标架构契约（docs/dev/architecture-target.md 第 1、2、3 节）落位。
事务边界在本层。本层只调 core 的纯函数与 ports 的接口声明。

目标成员（契约第 3 节目标树）：
- `round.py` 跑一轮；`sign_one.py` 签一个；`ingest.py` 结果入库；
  `report.py` 汇总与告警；`coord.py` 派发与工作体池。

上述成员至今仍在 `yiban/engine/`。它们的搬迁属批 B。批 B 的开工条件＝执行核
解冻。故本层今天只有地图，没有成员。

依赖方向：entry → app → core。adapters 实现 ports，不得反向导入本层。
"""
