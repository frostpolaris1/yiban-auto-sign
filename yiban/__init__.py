# SPDX-License-Identifier: AGPL-3.0-only
"""yiban 包：签到引擎与共享基础设施。

模块地图（目标分层见 `docs/dev/architecture-target.md` 第 3 节；批 B 后）：

- `entry/cli/`：命令行入口。
- `app/`：用例编排（`round` / `runner` / `probe`）。`engine/` 尚未搬完的件
  （`executor_v3` / `attempts` / `planner` / `schedule` /
  `token_bucket` 与 `cli_support` / `config_check` / `accounts` 等支撑件）仍在原地。
- `core/`：纯函数层（`clock` / `status` / `window` / `masking` / `hrw` / `elbow`）。
- `ports/`：端口声明。
- `adapters/`：唯一 I/O 面。`protocol_yiban/`（洁净室协议库：纯解析/构造；AGPL-3.0-only，
  许可见其 LICENSE）、`protocol_fake/`（测试假上游）、`notifier/`（邮件、推送与告警编排
  `report`）、`persistence/`（原 `yiban.store` 包整包搬入）、`statefiles/`（含原
  `yiban.adapters.statefiles.state_io` 模块）、`config/`。
- `workers/`：签名工作体（`workers`＝多执行体监督进程与兜底常驻，原
  `yiban.workers.workers` 模块。其编排面按契约第 1 节应属 `app/coord`，
  拆分属第三刀）。
- `engine/`：迁移期形态，仍有未搬成员（见各包 docstring）。

批 A1／A4／B 留下的旧坐标转发壳已由 `145-E` **删净**；旧坐标不再是合法 import 路径。

`infra` 不依赖本包其它模块；`core` 为纯常量与纯函数。
`adapters/protocol_yiban` 只做解析与构造（无网络、无会话、无日志）；`platform`
不得反向依赖 `store` / `security` / `client`（安全策略靠注入）。
"""

# 版本号**唯一来源**：网页页脚与引擎轮次横幅都读这里，别处不得再写一份字面量
# （发布门槛要靠"轮次横幅里的版本号"把生产跑过的轮次与提交对齐，见 docs/dev/release-gate.md）。
__version__ = "0.6.4"
