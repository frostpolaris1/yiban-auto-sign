# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""通知适配器（notifier）：邮件与 Webhook 推送两个组件。

本层按目标架构契约（docs/dev/architecture-target.md 第 1、3 节）落位。
它是适配器层唯一的"对外发通知"面。发邮件与发推送都只经本层。

成员：
- `mail/`：SMTP 邮件（管理员告警、用户签到结果）。原 `yiban/mail/`。
- `notify/`：Webhook 推送（Server酱 / 自定义 URL）。原 `yiban/notify/`。

**不在本层**：告警**编排**（`yiban/engine/alerts.py`）。
它决定"该不该告警、告警什么"，属应用层的用例编排，不是 I/O 适配。
它当前仍在 `yiban/engine/`，搬迁属批 B。

旧坐标 `yiban/mail` 与 `yiban/notify` 各留一个转发壳。壳的退役工单见本批交付报告。
"""
