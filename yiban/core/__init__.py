# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""领域核心层（core）：纯函数，无 I/O、无时钟、无随机。

本层按目标架构契约（docs/dev/architecture-target.md 第 1、3 节）落位：同一输入必得同一
输出，故可不用 mock 测。本层不 import 任何 I/O 面，也不自己取时钟——时钟一律由调用方以
形参注入。层准入判据见契约第 2 节。

当前成员：`elbow`（领取肘界谓词）。

目标成员（契约第 3 节目标树）：`policy`、`plan`、`rate`、`classify`、
`idempotency`、`masking`、`security_policy`。它们当前仍在 `yiban/` 根级与
`yiban/engine/`。那批搬迁属批 B，本层今天不搬它们。
"""
