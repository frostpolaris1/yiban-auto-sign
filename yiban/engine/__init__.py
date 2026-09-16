# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`yiban.engine`：签到引擎（原先全挤在 `scripts/signin.py` 里的"执行一轮"逻辑）。

已落位的模块（按"执行一轮"的边界切分）：

- `cli_support`：CLI 日志装配、进程级运行锁、状态文件读改写锁；
- `accounts`：账号装载（数据库 / JSON 环境变量 / 旧格式环境变量）；
- `schedule`：容量预估与调度 v2（统一填充框架）、签到窗口判定；
- `state_io`：状态文件读写与判定（按日状态 / 全量收尾标记 / 账密熔断 / 兜底心跳）；
- `alerts`：告警与邮件（管理员汇总、用户失败提醒、Webhook 推送）；
- `probe`：探针与只读健康检查；
- `config_check`：配置读取与脱敏打印。

两条依赖纪律（与 `yiban/` 其它包一致，且直接决定既有测试的打桩是否生效）：

1. **包内不得以裸名导入 `db` / `mailer` / `notify` / `signin`**——等价物是
   `from yiban.store import db` 与 `from yiban import mail, notify`；
2. **跨模块调用走模块属性**（`schedule.build_schedule(...)`），**不要**
   `from yiban.engine.schedule import build_schedule`：后者在导入期就把引用钉死，
   测试以 `signin.build_schedule = 替身` 打桩（兼容壳会把写入转发到实现模块）时，
   内部调用点看到的仍是旧对象，打桩会**静默失效**；同一模块内部的裸名调用不受此限
   （模块字典是运行期查找，打桩照常生效）。
"""
