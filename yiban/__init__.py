# SPDX-License-Identifier: AGPL-3.0-only
"""yiban 包：签到引擎与共享基础设施。

模块地图（随重构推进逐步落位）：

- `status`：签到状态码词汇表（唯一事实源）与未了结状态集合
- `masking`：脱敏函数（手机号 / 邮箱）唯一实现
- `infra/`：无项目内依赖的基础设施——env 读写、跨进程锁、凭据加解密、子进程环境、日志

**依赖方向**：`infra` 不依赖本包其它模块；`status` / `masking` 为纯常量与纯函数，
不依赖任何项目模块。引擎（client / schedule / queue…）与 store 在后续里程碑落位。
"""

# 版本号**唯一来源**：网页页脚与引擎轮次横幅都读这里，别处不得再写一份字面量
# （发布门槛要靠"轮次横幅里的版本号"把生产跑过的轮次与提交对齐，见 docs/dev/release-gate.md）。
__version__ = "0.4.4"
