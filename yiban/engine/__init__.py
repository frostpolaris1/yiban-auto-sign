# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`yiban.engine`：签到引擎，按"执行一轮"的边界切分为下列模块。

- `cli_support`：CLI 日志装配、进程级运行锁、状态文件读改写锁；
- `accounts`：账号装载（数据库 / JSON 环境变量 / 旧格式环境变量）；
- `schedule`：容量预估与调度 v2（统一填充框架）、签到窗口判定；
- `planner`：每日计划生成器（双粒度时间分片 + HRW 分工 + 幂等落库）；
- `hrw`：HRW 确定性分工（虚分片 + argmax 归属，纯函数）；
- `state_io`：状态文件读写与判定（按日状态 / 全量收尾标记 / 账密熔断 / 兜底心跳）；
- `alerts`：告警与邮件（管理员汇总、用户失败提醒、Webhook 推送）；
- `probe`：探针与只读健康检查；
- `config_check`：配置读取与脱敏打印；
- `attempts`：单账号一次尝试（登录 + 签到）、失败分级、账密熔断计数；
- `round`：一轮队列（分级重试、领取池分工、窗口收尾）；
- `workers`：多执行体监督进程与兜底常驻执行体；
- `runner`：入口与轮次编排（`main(argv) -> int`，退出码由调用方抛出）。

命令行入口是 `yiban/cli.py`（`python -m yiban.cli sign [...]`），旧路径
`scripts/signin.py` 只剩兼容壳（引导 + 全量转发 + `sys.exit`）。

两条依赖纪律（与 `yiban/` 其它包一致，且直接决定既有测试的打桩是否生效）：

1. **包内不得以裸名导入 `db` / `mailer` / `notify` / `signin`**——等价物是
   `from yiban.store import db` 与 `from yiban import mail, notify`；
2. **跨模块调用走模块属性**（`schedule.build_schedule(...)`），**不要**
   `from yiban.engine.schedule import build_schedule`：后者在导入期就把引用钉死，
   而测试以 `signin.build_schedule = 替身` 打桩时，兼容壳会把写入转发到实现模块，
   内部调用点看到的仍是旧对象，打桩**静默失效**；同一模块内部的裸名调用不受此限
   （模块字典是运行期查找）。与本模块内局部量同名时（如 `round` 里的局部
   `schedule`/`attempts`），以别名导入该模块（`schedule_mod`）。
"""
