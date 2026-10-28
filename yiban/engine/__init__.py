# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`yiban.engine`：签到引擎。批 B 后这里是**迁移期形态**：部分成员已搬出。旧坐标转发壳已由 `145-E` 删净，本包不再提供这些名字。

仍在原地的成员（未搬，理由见各条）：
- `cli_support`：CLI 日志装配、进程级运行锁、状态文件读改写锁（支撑件，归属待定）；
- `accounts`：账号装载（数据库 / JSON 环境变量 / 旧格式环境变量）；
- `schedule`：容量预估与调度 v2（统一填充框架）、签到窗口判定（含 I/O，见下）；
- `planner`：每日计划生成器（双粒度时间分片 + HRW 分工 + 幂等落库）（含 I/O）；
- `token_bucket`：出口令牌桶（GCRA/TAT）+ AIMD 自适应 + 全局速率上界 Λ（含 I/O）；
- `config_check`：配置读取与脱敏打印；
- `attempts`：单账号一次尝试（登录 + 签到）、失败分级、账密熔断计数（携带整条客户端链）；
- `executor_v3`：调度 v3 的执行体（要求拆 `app/sign_one` ＋ `app/ingest`）；
- `crash_semantics`、`db_maintenance`、`egress_admin`：崩溃语义、库维护、出口管理。

已搬出的成员（本包不再提供这些名字；请直接 import 下列新坐标）：
- `hrw` → `yiban/core/hrw.py`；
- `round` → `yiban/app/round.py`；`probe` → `yiban/app/probe.py`；
  `runner` → `yiban/app/runner.py`；
- `alerts` → `yiban/adapters/notifier/report.py`；
- `state_io` → `yiban/adapters/statefiles/state_io.py`（本包导入 `cli_support` /
  `config_check` / `schedule`，如实登记为遗留向后边，反转属第三刀）；
- `workers` → `yiban/workers/workers.py`（其编排面按契约第 1 节应属 `app/coord`，
  拆分属第三刀）。

命令行入口是 `yiban/entry/cli/`（`python -m yiban.entry.cli sign [...]`）；
旧坐标转发壳已由 `145-E` 删净，`scripts/signin.py` 只剩兼容壳（引导 + 全量转发 + `sys.exit`）。

两条依赖纪律（与 `yiban/` 其它包一致，且直接决定既有测试的打桩是否生效）：

1. **包内不得以裸名导入 `db` / `mailer` / `notify` / `signin`**——等价物是
   `from yiban.adapters.persistence import db` 与 `from yiban.adapters.notifier import mail, notify`；
2. **跨模块调用走模块属性**（`schedule.build_schedule(...)`），**不要**
   `from yiban.engine.schedule import build_schedule`：后者在导入期就把引用钉死，
   而测试以 `signin.build_schedule = 替身` 打桩时，兼容壳会把写入转发到实现模块，
   内部调用点看到的仍是旧对象，打桩**静默失效**；同一模块内部的裸名调用不受此限
   （模块字典是运行期查找）。与本模块内局部量同名时（如 `round` 里的局部
   `schedule`/`attempts`），以别名导入该模块（`schedule_mod`）。
"""
