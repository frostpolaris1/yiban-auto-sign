# 分支分级与发布门槛

> 2026-11-27 旧 Python 栈退役后，原 `run.sh`、`scripts/backup.sh`、`deploy/prod/`
> 与旧演练脚本已删除；本文只保留 Go 栈可执行的命令与判据。旧线"生产机三轮签到"
> 的命令载体随旧栈消失，其要求本身仍约束 `main`，采集口径在 PR3 随 `main` 排程
> 一并重写；在那之前不得用本文旧命令充当证据。

## 1. 分支阶梯

| 分支 | 含义 | 允许推送的条件 |
|------|------|----------------|
| `feature/<主题>` | 单条工作线 | 不含真实凭据、部署与备案信息 |
| `develop` | 集成基线 | 只收 `feature/*` 合并；下列 required checks 全绿 |
| `main` | 对外默认可见的已验证分支 | 在 `develop` 基础上，同一提交在生产机完成 **≥3 个有效轮次、跨 ≥2 个自然日**，期间无失败、无异常、无人工干预、无回滚 |

`main` 天然落后于生产若干天，这是设计如此。旧线 `server-web` 部署分支随旧栈退役，
不再作为晋升路径。

## 2. Required checks（8 项）

| 检查 | 来源 | 管什么 |
|---|---|---|
| `verify` | `.github/workflows/ci.yml` | Python 面：ruff + `pytest tests/` |
| `tooling-gate` | 同上 | actionlint、shellcheck、golangci-lint、govulncheck、npm audit |
| `contract-gate` | 同上 | spectral lint ＋ oasdiff 破坏性变更 |
| `stacks` | 同上 | `scripts/verify-stacks.sh`（Go 全量 + UI 构建/校验） |
| `ui-e2e (behavior)` | 同上 | Playwright 行为分片 |
| `ui-e2e (shots)` | 同上 | Playwright 截图分片 |
| `container-deploy-e2e` | `.github/workflows/deploy-gate.yml` | 干净容器装成、备份恢复、SIGTERM |
| `update-rollback-e2e` | 同上 | 一键更新干跑契约 |

分支保护必须同步为这 8 个 context。旧线 `slow-lane`、`dependency-audit`、`frontend`、
`deploy-gate` 四个 context 在 job 删除后不会再上报，必须从保护里移除，否则 PR 会
永久停在 "Expected"。

## 3. 本地门（提交前）

```bash
# Python 面：ruff + pytest tests/
bash scripts/dev-verify.sh --ci

# Go + UI：格式、vet、全量测试、生成物 --check、结构/许可证门
YIBAN_TEST_DATABASE_URL='<测试库 DSN>' bash server/scripts/verify.sh

# 发布清单：版本、许可证、生成物、签名、部署与回滚入口
bash scripts/release-check.sh
```

`golangci-lint` 版本与规则以 CI 的 `tooling-gate` 为准（`server/.golangci.yml`）。

## 4. 发布前部署演练

```bash
# 容器形态：干净环境 build→起服→/healthz→备份恢复→SIGTERM
bash scripts/e2e/deploy-container-e2e.sh

# 裸机形态：干净 Ubuntu 容器里 install.sh + 真 systemd
bash scripts/e2e/deploy-baremetal-e2e.sh

# 一键更新与回滚：R1..R10 全矩阵（需 docker --privileged）
bash scripts/e2e/update-rollback-e2e.sh host --log-dir <日志目录>
```

CI 只跑干跑契约（`update-rollback-e2e`）；全矩阵在
`update-rollback-privileged.yml` 手动触发、自托管 runner 执行，非 0 原样判红。

## 5. 有效轮次的定义（沿用旧线口径，载体待 PR3）

一次完整执行且真实完成签到，同时满足：

1. 服务端日志有该轮开始记录，带版本号；
2. 汇总 0 失败；
3. 进程退出码 0；
4. 当日状态为成功。

空转轮、补签轮不算；同一自然日重复轮次不能替代跨日证据。生产在该提交上出现失败、
异常、人工干预或回滚，轮次计数从零重算。

旧线 `sign-status-<日期>.txt`、`run.sh 退出` 等采集命令已随旧栈删除；Go 栈的日志字段
与状态表在 PR3 的 `main` 排程文档中给出，届时以那份为准。

## 6. 证据

每批在 PR 或过程稿里留门禁证据：命令、退出码、汇总行、环境。没有这段证据，
等同于没通过门禁。SKIP 必须写明原因，不算绿。

## 7. 历史归档

旧线的四道通用门（安全/压力/稳定性/脱敏）、`server-web` 生产演练与
`deploy/prod/install.sh` 步级名册已随目录删除；需要考古时看 git 历史，
不得把历史命令当现役判据。
