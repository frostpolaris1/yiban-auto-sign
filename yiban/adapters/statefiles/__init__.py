# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""状态文件适配器（adapters/statefiles）：磁盘状态 I/O（契约第 3 节）。

成员来自批 B 搬迁：
- `state_gc`：状态文件保留期回收（原 `yiban/state_gc.py`）；
- `cred_state`：账密熔断计数状态（原 `yiban/cred_state.py`）。

旧坐标留转发壳（`yiban/state_gc.py`、`yiban/cred_state.py`），读者无需改动。

**未搬入**：`yiban/engine/state_io.py`。契约第 3 节把 `engine/state_io.py` 映射到本
包，但该模块导入 `yiban.engine.{cli_support, config_check, schedule}`（上层关注点：
日志装配、配置打印、调度）。本批是搬迁批、不改依赖结构；把它搬进本包会造出
`adapters → engine` 的向上边（契约 `adapters-no-upward` 允许 `engine` 不在禁列，
但语义方向错）。故本批不搬，留待把那段依赖反转（第三刀或后续批）。

依赖方向：本层实现 ports。本层不得导入 `yiban.app` / `yiban.entry` / `yiban.workers`。
"""
