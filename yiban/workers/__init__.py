# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""签名工作体层（workers）：子进程或常驻池里的执行体。

本层按目标架构契约（docs/dev/architecture-target.md 第 1、2、3 节）落位。
本层的存活判据＝进程句柄。本层一次只处理一个账号。

成员来自批 B 搬迁：
- `workers`：多执行体监督进程与兜底常驻执行体（原 `yiban.engine.workers` 模块）。

**层归属张力（如实登记，属第三刀）**：契约第 1 节的 APP 子图把「协调 · 派发与工作体池」
画在应用层（`app/coord`），而 `workers.py` 里监督进程与派发是**编排**。本批按第 3 节
映射表"移到 `workers/`"整文件搬入，未做拆分（本批不改行为）。第三刀须把编排面拆去
`app/coord.py`、并切出 `yiban/engine/executor_v3.py` 的工作体面。
"""
