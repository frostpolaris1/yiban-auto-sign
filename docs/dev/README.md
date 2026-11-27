# 开发文档（项目地图）

本目录是**可入库**的开发文档：架构、门、发布与运维手册。
项目地图与目录规则以根目录 [`../../STRUCTURE.md`](../../STRUCTURE.md) 为准。

> 2026-11-27 旧 Python 栈退役。下表标「旧」的文件仍留在库里供追溯，
> 内容描述已删除的目录或命令，**不要当作现行指引**；PR3 统一重写或删除。

## 现行文档

| 文件 | 内容 |
|------|------|
| `acceptance-criteria.md` | D1 验收（改坏守卫必须报警）、负数条款、对外交付逐条核对清单 |
| `dev-verify.md` | 统一跑测入口 `scripts/dev-verify.sh`（ruff + pytest） |
| `deploy-e2e.md` | 部署 e2e：容器与裸机两条干净环境走通命令、断言、CI 与手跑登记 |
| `release-gate.md` | 分支分级、8 个必需检查、发布门槛与本地门 |
| `release-signing.md` | 发布物签名：Ed25519 清单/签名/验签、信任锚、密钥红线、发布与升级流程 |
| `backup-restore.md` | 备份与恢复：容器与裸机两条路线、产物、校验与演练入口 |
| `retry-advice-wiring.md` | 接线 RetryAdvice：`After` 作等待下限、`MaxAttempts` 只压不抬 |
| `lease-deadline-guard.md` | 租约截止 / 回收冷却：双跑根因、7 格实测矩阵、生产参数建议 |
| `attempt-key-claim-cycle.md` | 幂等键 = 认领周期：回收后旧键折叠的根因、键形状决策、矩阵 distinct 1→2 |

## 旧栈文档（待 PR3 处理）

| 文件 | 原内容 |
|------|--------|
| `api-contract.md` | 旧栈 Web API 契约（已冻结） |
| `api-executors.md` | 执行体与出口接口契约（已冻结） |
| `architecture-target.md` | 旧栈目标架构：分层与依赖方向 |
| `cli.md` | 旧栈 CLI 契约 |
| `implementation-notes.md` | 旧栈模块树、签到流程、定位算法、重试机制 |
| `scheduler-v3.md` | 旧栈调度 v3 运维手册 |
| `time-prefs-backfill.md` | 旧自选时间片补录 |
| `troubleshooting.md` | 旧栈故障排查 Q1–Q10 |
| `umask-decision.md` | 旧生产入口 `umask 077` 评估 |

## 文稿归属

设计/调研/计划类文稿的**唯一家**是主仓 `docs/`。
工作树（`work/*`）里出现的同名文稿一律视为草稿；要长期保留的内容必须收进主仓
`docs/`。本目录不再做文档编号巡检。

与设计/调研类文稿分开——这里的每条内容都按脱敏红线自查过：
不含真实域名/IP/邮箱/手机号/密钥与站点注册信息，示例一律用占位值。
