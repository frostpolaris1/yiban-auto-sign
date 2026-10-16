# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""签名工作体层（workers）：子进程或常驻池里的执行体。

本层按目标架构契约（docs/dev/architecture-target.md 第 1、2、3 节）落位。
本层的存活判据＝进程句柄。本层一次只处理一个账号。

目标成员来源＝`yiban/engine/workers.py`（监督进程 ＋ 兜底常驻执行体）与
`yiban/engine/executor_v3.py`（v3 执行体）的切面。两者都要求"一个文件拆成编排面
（`app/coord`）与工作体面（本层）"，属第三刀。故本层今天只有地图，没有成员。
"""
