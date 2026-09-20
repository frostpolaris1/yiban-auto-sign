# SPDX-License-Identifier: AGPL-3.0-only
"""yiban 包：签到引擎与共享基础设施。

模块地图：`client`（客户端外观）、`clock` / `window` / `status`（北京时间基准、
签到窗口、状态码词汇表）、`masking` / `security`（脱敏与安全策略）、`store/` 与
`engine/` 与 `attempt/`（持久化、轮次编排、单次尝试）、`notify/` 与 `mail/`（通知
与邮件）、`infra/`（无项目内依赖的基础设施：env 读写、跨进程锁、凭据加解密）。

`infra` 不依赖本包其它模块；`status` / `masking` / `clock` 为纯常量与纯函数。
"""

# 版本号**唯一来源**：网页页脚与引擎轮次横幅都读这里，别处不得再写一份字面量
# （发布门槛要靠"轮次横幅里的版本号"把生产跑过的轮次与提交对齐，见 docs/dev/release-gate.md）。
__version__ = "0.6.0"
