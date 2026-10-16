# SPDX-License-Identifier: AGPL-3.0-only
"""yiban 包：签到引擎与共享基础设施。

模块地图（目标分层见 `docs/dev/architecture-target.md` 第 3 节）：

- 根级：`client`（客户端外观）、`platform`（易班平台事实与登录/签到编排）、
  `geo`（多边形内定位采样）、`challenge`（风控挑战检测）、`clock` / `window` /
  `status`（北京时间基准、签到窗口、状态码词汇表）、`masking` / `security`
  （脱敏与安全策略）、`egress`（出口分配）、`cred_state`、`state_gc`、`logging_ext`。
- `entry/cli/`：命令行入口。旧坐标 `yiban/cli.py` 是转发壳。
- `adapters/`：唯一 I/O 面。`protocol_yiban/`（洁净室协议库：纯解析/构造；MIT，
  许可见其 LICENSE）、`protocol_fake/`（测试假上游）、`notifier/`（邮件与推送；
  旧坐标 `yiban/notify/` 与 `yiban/mail/` 是转发壳）。
- `core/`：纯函数层（现只有 `elbow`）。
- `infra/`：无项目内依赖的基础设施（env 读写、跨进程锁、凭据加解密）。
- `store/` 与 `engine/` 与 `attempt/`：持久化、轮次编排、单次尝试。
- `app/`、`ports/`、`workers/`：目标分层目录，成员待搬入。

`infra` 不依赖本包其它模块；`status` / `masking` / `clock` 为纯常量与纯函数。
`adapters/protocol_yiban` 只做解析与构造（无网络、无会话、无日志）；`platform`
不得反向依赖 `store` / `security` / `client`（安全策略靠注入）。
"""

# 版本号**唯一来源**：网页页脚与引擎轮次横幅都读这里，别处不得再写一份字面量
# （发布门槛要靠"轮次横幅里的版本号"把生产跑过的轮次与提交对齐，见 docs/dev/release-gate.md）。
__version__ = "0.6.3"
