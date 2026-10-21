# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""领域核心层（core）：纯函数，无 I/O、无时钟、无随机。

本层按目标架构契约（docs/dev/architecture-target.md 第 1、3 节）落位：同一输入必得同一
输出，故可不用 mock 测。本层不 import 任何 I/O 面，也不自己取时钟——时钟一律由调用方以
形参注入。层准入判据见契约第 2 节。

当前成员：
- `elbow`（领取肘界谓词，2026-10-12 落位）；
- `clock` / `status` / `window` / `masking` / `hrw`（批 B 从 `yiban/` 根级与
  `yiban/engine/` 搬入）。

**未搬入（需拆分，属第三刀）**：契约第 3 节把 `engine/{planner,token_bucket,schedule}`
映射到本层（`plan` / `rate`），把 `engine/attempts` 映射成 `classify` ＋ `policy`。
但代码面实测：`planner` / `token_bucket` / `schedule` 直接 import `yiban.adapters.persistence` 与
`yiban/infra`（有 I/O）；`attempts` 携带整条客户端链。整文件搬进本层会造出
`core → adapters` 的跨层边（契约 `core-no-upward` 会红），须先把纯算法与 I/O 切开。
`security`（→ `security_policy`）经 `yiban.challenge` 间接到 `adapters`，同理。
故本批不搬，留待第三刀。
"""
