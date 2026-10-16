# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""签名工作体层（workers）：子进程或常驻池里的执行体。

本层按目标架构契约（docs/dev/architecture-target.md 第 1、2、3 节）落位。
本层的存活判据＝进程句柄。本层一次只处理一个账号。

目标成员（契约第 3 节目标树）：签名工作体模块，一律瘦身。
来源＝`yiban/engine/workers.py` 与 `yiban/engine/executor_v3.py` 的切面。
那两件都在执行核冻结名单内。故本层今天只有地图，没有成员。搬迁属批 B。
"""
