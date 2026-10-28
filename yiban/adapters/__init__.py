# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""适配器层（adapters）：唯一的 I/O 面。

本层按目标架构契约（docs/dev/architecture-target.md 第 1、2、3 节）落位。
本层实现 ports 声明的接口。所有网络、磁盘、时钟与随机都只在本层出现。

成员（契约第 3 节目标树）：
- `protocol_yiban/`：易班平台协议库（洁净室）。批 A2 落位。
- `protocol_fake/`：测试假上游。批 A3 落位。
- `notifier/`：邮件与推送。批 A4 落位；告警编排（原 `engine/alerts.py`）批 B 搬入
  `notifier/report.py`。
- `persistence/`、`statefiles/`、`config/`：批 B 落位（原 `store/`、
  `state_gc`/`cred_state`、`config_loader`/`infra.env_io`/`infra.account_crypto`）。

依赖方向：本层实现 ports。本层不得导入 app、entry、workers，即不得反向依赖。
已知一处向 `yiban.engine` 的延迟导入（`persistence.claims` 的告警编排），
那是执行核搬家前的遗留向后边，本批不动。
"""
