# 分支分级与发布门槛

> 2026-11-27 旧 Python 栈退役后，旧 CLI、旧演练脚本与 `deploy/prod/` 已删除。
> 本文只保留 Go 栈可执行的命令与判据，不把历史命令当现役证据。

## 1. 分支阶梯

| 分支 | 含义 | 允许推送的条件 |
|------|------|----------------|
| `feature/<主题>` | 单条工作线 | 不含真实凭据、部署与备案信息 |
| `develop` | 集成基线 | 只收 `feature/*` 合并；下列 required checks 全绿 |
| `main` | 对外默认可见的已验证分支 | 只接受 `develop` 候选提交的晋升 PR，且 §5 证据齐全 |

`main` 是展示与追溯分支，不是生产部署源。生产部署以签名发布件为准；
晋升 `main` 不改变生产正在运行的版本。旧部署分支已随旧栈退役，不再作为晋升路径。

## 2. Required checks（8 项）

| 检查 | 来源 | 管什么 |
|---|---|---|
| `verify` | `.github/workflows/ci.yml` | 门禁测试（ruff + `pytest tests/`） |
| `tooling-gate` | 同上 | actionlint、shellcheck、golangci-lint、govulncheck、npm audit |
| `contract-gate` | 同上 | spectral lint ＋ oasdiff 破坏性变更 |
| `stacks` | 同上 | `scripts/verify-stacks.sh`（Go 全量 + UI 构建/校验） |
| `ui-e2e (behavior)` | 同上 | Playwright 行为分片 |
| `ui-e2e (shots)` | 同上 | Playwright 截图分片 |
| `container-deploy-e2e` | `.github/workflows/deploy-gate.yml` | 干净容器装成、备份恢复、SIGTERM |
| `update-rollback-e2e` | 同上 | 一键更新干跑契约 |

分支保护已同步为这 8 个 context；旧线 `slow-lane`、`dependency-audit`、
`frontend`、`deploy-gate` 四个 context 已随对应 job 删除而移除。

## 3. 本地门（提交前）

```bash
# 门禁测试：ruff + pytest tests/
bash scripts/dev-verify.sh --ci

# Go + UI：先 server 后 ui，缺工具链 exit 2（不是绿）
YIBAN_TEST_DATABASE_URL='<测试库 DSN>' bash scripts/verify-stacks.sh

# 发布清单（发布时跑：version/go-version 在打 tag 前预期 FAIL）
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

一键更新在 CI 只跑干跑契约（`update-rollback-e2e`）；全矩阵在
`update-rollback-privileged.yml` 手动触发、自托管 runner 执行，非 0 原样判红。

## 5. main 晋升（Go 口径）

### 5.1 有效轮次

一个有效轮次是一个业务日（`Asia/Shanghai`）的完整签到结果。Go 进程在每个
业务日切换时写一条 `round summary`；进程重启会重复汇总上一日，同一 `day`
出现多行时按至少一次语义合并，不重复计数。

一条有效轮次必须同时满足：

1. 该日 `round summary` 的 `success > 0`、`failed = 0`、无 `pending`
   （字段缺省即 0）；`skipped` 可以存在，但不能替代 `success`；
2. 用 §5.2 的 SQL 复核，`success > 0`、`failed = 0`，且没有
   `pending` / `running`；
3. 该日没有 `round summary failed`，没有服务重启、人工干预或回滚；
4. 生产实例的 `yiban-server --version` 三行与发布清单的版本、提交一致。

空转日、补签日不算；同一业务日不重复计数。晋升要求 **≥3 个有效轮次、跨 ≥2 个
自然日**；由于每业务日至多一轮，实际为 ≥3 个业务日。

### 5.2 采集命令

```bash
# 版本身份：三行必须与发布清单一致（裸机 /usr/local/bin，容器按镜像入口）
sudo -u yiban /usr/local/bin/yiban-server --version

# 服务状态：ActiveState=active，NRestarts 在观察窗口内不增长
systemctl show yiban -p ActiveState -p NRestarts -p ExecMainStartTimestamp

# 轮末汇总：只取当前业务日，优先看 failed=0 且 success>0
journalctl -u yiban --since '<YYYY-MM-DD> 00:00' --until '<次日> 00:00' \
  | grep 'round summary'
```

容器形态把 `systemctl` 换成 `docker inspect`（`State.StartedAt`、`RestartCount`），
`journalctl` 换成 `docker logs`；判据不变。

```sql
SELECT state, count(*) AS n
FROM sign_tasks
WHERE day = DATE '<YYYY-MM-DD>'
GROUP BY state
ORDER BY state;
```

预期结果：`success > 0`、`failed = 0`，且结果中没有 `pending` 或 `running`。
若出现 `round summary failed` 或 `outcome_counts`，该日不算有效轮次。

证据包在发布记录中留存：版本三行、journal 汇总行、SQL 输出、服务状态。
不进仓库，不在公开 issue 贴真实计数。长驻进程没有“每轮退出码 0”这一项；
用服务状态与轮末汇总替代，不能拿旧线退出码当证据。

### 5.3 晋升排程与首次对账

晋升不设自动定时器。每次发布后人工排期，建议在候选提交合并 `develop` 后的
第 4 个业务日：前 3 个业务日的汇总已落库，且当天没有新故障。

`main` 与 `develop` 的历史已分叉，首次晋升必须做一次显式对账：

1. 从 `main` 开 `chore/promote-main-<version>`，禁止 force-push 或 reset `main`；
2. `git merge --no-ff <candidate>`，冲突按“删除全部旧栈路径、保留候选树”解决；
3. 用 `git diff --stat <candidate>` 证明晋升树与候选提交一致；
4. 开 PR 到 `main`，跑完 §2 的 8 项检查后合并，保留两个父提交。

首次对账完成后，`develop` 成为 `main` 的祖先，后续晋升可走普通 merge PR。
发布 tag 打在候选提交上，不打在 `main` 的合并提交上；tag 与 `VERSION` 一致。
晋升后发现文档或展示问题，用 revert 提交修复 `main`，不重写历史；生产回退走
`update.sh` 的回滚点，见 `deploy/README.md`。

## 6. 证据

每批在 PR 或过程稿里留门禁证据：命令、退出码、汇总行、环境。没有这段证据，
等同于没通过门禁。SKIP 必须写明原因，不算绿。

## 7. 历史归档

旧线的四道通用门（安全/压力/稳定性/脱敏）、旧生产演练与 `deploy/prod/install.sh`
步级名册已随目录删除；需要考古时看 git 历史，不得把历史命令当现役判据。
