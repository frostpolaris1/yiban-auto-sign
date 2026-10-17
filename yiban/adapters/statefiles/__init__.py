# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""状态文件适配器（adapters/statefiles）：磁盘状态 I/O（契约第 3 节）。

成员来自批 B 搬迁：
- `state_gc`：状态文件保留期回收（原 `yiban/state_gc.py`）；
- `cred_state`：账密熔断计数状态（原 `yiban/cred_state.py`）；
- `state_io`：状态文件读写与判定（原 `yiban/engine/state_io.py`）。

旧坐标留转发壳（`yiban/state_gc.py`、`yiban/cred_state.py`、`yiban/engine/state_io.py`），
读者无需改动。

**已知的向上边（如实登记）**：`state_io` 导入 `yiban.engine.{cli_support, config_check, schedule}`。
那是执行核搬家前遗留的向后边（日志装配、配置打印、调度），与 `adapters/notifier/report.py`
导入 `yiban.engine.*` 是同一形状。契约 `adapters-no-upward` 的禁列只含
`app` / `entry` / `workers`，故该边不判红。反转它属第三刀。

依赖方向：本层实现 ports。本层不得导入 `yiban.app` / `yiban.entry` / `yiban.workers`。
"""
