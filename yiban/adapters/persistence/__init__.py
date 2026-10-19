# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""持久化适配器（adapters/persistence）：命令面 + 查询面（目标架构契约第 3 节）。

本包是 `ports.store` 声明的持久化契约的 SQLite 实现，也是全仓唯一的 SQL 面。
成员来自批 B 搬迁：原 `yiban.store` 包整包搬入。批 B 留下的旧坐标转发壳已由
`145-E` 删净；旧坐标不再是合法 import 路径。

成员：
- `connection`：连接单例与数据库路径；
- `migrations`：schema 版本迁移与建表；
- `audit_chain`：审计哈希链（契约第 3 节目标名是 `audit.py`，本批按整包搬迁保留原名）；
- `db`：门面，只自持连接编排与写事务入口，并再导出各域名字（依赖方向 db → 域模块）；
- 其余各域模块：`accounts` / `events` / `session_cache` / `time_prefs` / `clock_meta` /
  `tracking` / `verify_jobs` / `queue_store` / `users` / `cleanup` / `claims` /
  `run_events` / `purge_guard`。

依赖方向：本层实现 ports。本层不得导入 `yiban.app` / `yiban.entry` / `yiban.workers`
（契约 `adapters-no-upward`）。已知一处向 `yiban.engine.alerts` 的延迟导入
（`claims` 放弃分支的告警），那是执行核搬家前遗留的向后边，本批不动。
"""
