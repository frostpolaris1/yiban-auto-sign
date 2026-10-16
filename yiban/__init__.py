# SPDX-License-Identifier: AGPL-3.0-only
"""yiban 包：签到引擎与共享基础设施。

模块地图（目标分层见 `docs/dev/architecture-target.md` 第 3 节；批 B 后）：

- `entry/cli/`：命令行入口。旧坐标 `yiban/cli.py` 是转发壳。
- `app/`：用例编排（`round` / `runner` / `probe`）。旧坐标 `yiban.engine.{round,runner,probe}`
  是转发壳。`engine/` 尚未搬完的编排件（`executor_v3` / `attempts` / `planner` /
  `schedule` / `token_bucket` / `state_io` / `workers` / `alerts` 之外的支撑件）仍在原地。
- `core/`：纯函数层（`clock` / `status` / `window` / `masking` / `hrw` / `elbow`）。
  旧坐标 `yiban.{clock,status,window,masking}`、`yiban.engine.hrw` 是转发壳。
- `ports/`：端口声明。
- `adapters/`：唯一 I/O 面。`protocol_yiban/`（洁净室协议库：纯解析/构造；MIT，许可见其
  LICENSE）、`protocol_fake/`（测试假上游）、`notifier/`（邮件、推送与告警编排
  `report`）、`persistence/`（原 `yiban/store/`）、`statefiles/`、`config/`。
  旧坐标 `yiban.store.*`、`yiban.{state_gc,cred_state}`、`yiban.infra.{env_io,account_crypto}`、
  配置名册加载器与 `yiban.engine.alerts` 是转发壳。
- `workers/`：签名工作体（目标层，成员待第三刀搬入）。
- `engine/`、`store/`、`infra/`：迁移期形态。`store/` 已成转发壳；`engine/` 与 `infra/`
  仍有未搬成员（见各包 docstring）。

`infra` 不依赖本包其它模块；`core` 为纯常量与纯函数。
`adapters/protocol_yiban` 只做解析与构造（无网络、无会话、无日志）；`platform`
不得反向依赖 `store` / `security` / `client`（安全策略靠注入）。
"""

# 版本号**唯一来源**：网页页脚与引擎轮次横幅都读这里，别处不得再写一份字面量
# （发布门槛要靠"轮次横幅里的版本号"把生产跑过的轮次与提交对齐，见 docs/dev/release-gate.md）。
__version__ = "0.6.3"
