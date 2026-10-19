# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""入口层（entry）：只翻译参数与鉴权。

本层按目标架构契约（docs/dev/architecture-target.md 第 1、2、3 节）落位。
本层不含业务判据。本层不碰存储实现。

成员（契约第 3 节目标树）：
- `cli/`：命令行入口。已落地（批 A1）；旧坐标转发壳已由 `145-E` 删净。

依赖方向：entry → app → core。本层不得被上层之外的层反向导入。
"""
