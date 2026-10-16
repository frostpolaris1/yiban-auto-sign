# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""端口层（ports）：只声明接口。不含实现，不含 I/O。

本层按目标架构契约（docs/dev/architecture-target.md 第 1、2、3 节）落位。
adapters 实现本层声明的接口。本层只声明形状，不判业务。

目标成员（契约第 3 节目标树）：
- `store.py` 持久化契约；`protocol.py` 协议契约；`notifier.py` 通知契约。

上述三件至今尚未落位。它们随批 B 落位。故本层今天只有地图，没有成员。
"""
