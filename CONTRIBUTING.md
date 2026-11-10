# 贡献指南

本文件面向外部贡献者。
它说明项目方向、可认领的任务、以及不接受外部 PR 的边界。

## 1. 项目方向

本仓有两套系统。

- **新系统**：`server/`（Go 后端）＋ `ui/`（React 前端）。**唯一活跃开发线**。
- **旧系统**：`yiban/`、`web/`、`frontend/`、`tests/`。Python/Vue 旧栈，**已冻结**。不再接受功能 PR，也不再接受缺陷修复 PR。

新功能只写 `server/` 与 `ui/`。
目录地图见 `STRUCTURE.md`。
后端分层与命令见 `server/README.md`。
前端结构、门与测试命令见 `ui/README.md`。

### 1.1 当前阶段

1. **加固（已完成）**：认证、限流、调度、可靠性、发布门。
2. **功能对齐（进行中）**：用户与账号管理、运行页、审计页、执行监控、系统设置。
3. **发布准备**：容量实测、影子运行、回滚演练。
4. **产品池**：夜间考勤、归档导出、自动审批、通知中心。

阶段是方向，不是承诺的交付日期。
维护者内部有排期台账。
公开 issue 是外部贡献的唯一跟踪单位。

## 2. 可认领的方向

| 方向 | 适合谁 | 入口 | 状态 |
|---|---|---|---|
| 前端体验 | React / TypeScript / CSS | `ui/src/`、`ui/e2e/` | 开放 |
| 文档 | 技术写作 | `README.md`、`docs/`、`server/README.md`、`ui/README.md` | 开放 |
| 测试与质量 | Go / TypeScript 测试 | `server/internal/`、`ui/e2e/` | 开放 |
| 部署体验 | Shell / 反向代理 | `deploy/baremetal/`、`docs/dev/deploy-e2e.md` | 部分开放，见 §3 |
| 独立验证 | 测试 / 审计 | 公开 issue | 开放 |

## 3. 具体任务

### 3.1 可立即认领

| 任务 | 范围 | 验收 | 难度 |
|---|---|---|---|
| 顶层 README 重写 | 把 `README.md` 的主线改成 Go＋React；旧 Python 内容折叠为附录或移入归档文档 | 新读者 5 分钟内找到安装、配置、测试命令；引用路径都在库内；文档门绿 | 中 |
| 升级维护页 | 在 `deploy/baremetal/` 下新增静态维护页，并在 `docs/dev/deploy-e2e.md` 写反代片段 | 服务停止时反代能返回该页；无 JavaScript；键盘可达；文案说明升级进度 | 小 |
| 测试时序清理 | 把 `ui/e2e/` 里的固定等待改为等待条件 | 不改变任何断言；`test:behavior` 连跑 5 次稳定；不许放宽超时掩盖抖动 | 小 |
| 部署故障排查 | 扩充 `docs/dev/troubleshooting.md` 的 Go 线条目 | 每类故障给症状、命令、期望输出；只写可复现内容 | 小 |
| 可访问性巡检 | 检查 `ui/src/` 的键盘可达、`label` 关联、焦点可见 | 先交报告与问题清单；修复按页分批；不新增依赖 | 中 |
| 合成夹具扩展 | 在 `tests/synthetic-fixtures.json` 登记新的合成值，并补对应测试 | 只使用登记过的合成值；数据门绿；不得出现看似真实的数据 | 小 |

### 3.2 等待内部基础件

以下任务价值明确，但依赖正在开发的基础件。
**先开 issue**，等维护者确认基础件合并后再动手。

| 任务 | 依赖 | 说明 |
|---|---|---|
| 统一加载态 | 前端共用件 | 骨架屏、按钮在途、路由兜底；共用件未合并前不做页面级改动 |
| 签到日历 | 运行页的任务查询 | 用户看近 30 天；管理员按账号查看 |
| 批量操作 | 用户管理与横向修形 | 批量签到/暂停、批量停用/重置；逐条审计、部分失败可见 |

### 3.3 先讨论再定判据

| 任务 | 先讨论什么 |
|---|---|
| Webhook 第二通道 | 与通知核心的边界；URL 校验、超时、重试、脱敏；先定判据再写代码 |
| 自定义默认落地页 | 页面清单与权限；是否只做偏好，不做布局编辑器 |
| 邮件通知粒度 | 类别、渠道、聚合窗口；不得按条发送失败邮件 |

## 4. 不接受外部 PR 的面

下列能力必须由本仓自持。
它们**不接受外部 PR**。
想动这些面，请先开 issue 讨论。
讨论定出判据后，才评审代码。

- 签到状态语义（状态枚举、完成判据、退出码）。
- 任务认领与互斥（CAS 判据、租约、回收与换代语义）。
- 容量与调度（签到窗口、容量公式、配速、出口绑定）。
- 隐私与脱敏（掩码口径、手机号零落盘、备份口令、环境变量面）。
- 审计完整性（审计链与校验、每账号当日终态口径）。
- 风控信号分类（什么算风控、什么算凭据错）。**验证码自动识别是红线**。
- 平台协议形状（请求链、信封容错）——以实拍为准，不采信第三方实现。
- 发布签名与验签、迁移执行、生产部署脚本、备份恢复。
- 判据与门本身（可以换实现，不得外包）。

## 5. 数据红线

公开仓库不得出现看似真实的信息。

- 手机号、邮箱、账号、学校、用户标识、会话令牌、设备码、凭据密文，一律不得入库。
- 测试数据只取 `tests/synthetic-fixtures.json` 里登记过的合成值。
- 真实数据不得进入 issue、PR、截图、日志或夹具。
- 违反即阻断合并。

## 6. 工作流

1. **先开 issue**。标题用 `[contrib] <任务名>`。写清范围、验收、预计改动的文件面。
2. **等维护者确认**。维护者回复 `accepted` 后再写代码。未确认的 PR 可能被关闭。
3. **从 `develop` 切分支**。分支名 `feat/<topic>` 或 `fix/<topic>`。不要从 `main` 切。
4. **先写测试**。先写会失败的用例，再改实现。断言不变式，不钉调用顺序。
5. **一个模块一个 PR**。不要混装机制替换、缺陷修复、新门与文档改写。
6. **提交信息**用 Conventional Commits。
7. **PR 指向 `develop`**。填写 `.github/pull_request_template.md`；UI 改动附截图。
8. **提交身份**：用你自己的 `git user.name` 与 `git user.email`；本地钩子与白名单细则见 §7.1。

## 7. 外部贡献者

本节只写外部贡献者与维护者不同、或最容易踩空的地方。流程仍以 §6 为准。

### 7.1 提交身份

用自己的 `git user.name` 与 `git user.email`，不要冒用维护者身份。

仓库自带的 `scripts/git-hooks/`（`pre-commit` 与 `commit-msg`）是维护者本地的卫生阀：

- 它**不会**自动启用。只有本地执行过 `git config core.hooksPath scripts/git-hooks` 才会生效；
  外部克隆不做这一步，钩子不触发。
- 它的白名单只允许维护者身份。你若复制了维护者的本地配置而被拒，
  用 `git commit --no-verify` 跳过本次校验——跳过只影响你本地，不影响 CI。

### 7.2 PR 必须过全部 CI 门禁

PR 上的全部检查绿，才进入人工审查。活跃线 `develop` 上共 12 个检查，
来自 11 个作业（`ui-e2e` 一个作业按 behavior / shots 两片各报一个检查）：

| 检查名 | 来源 | 管什么 |
|---|---|---|
| `verify` | `.github/workflows/ci.yml` | 主验证：Python 门、旧线测试子集、新栈门（按改动面分流） |
| `slow-lane` | 同上 | 慢集与串行轨 |
| `dependency-audit` | 同上 | `pip-audit`（`requirements.lock`） |
| `tooling-gate` | 同上 | actionlint、shellcheck、golangci-lint、govulncheck、npm audit |
| `contract-gate` | 同上 | spectral lint ＋ oasdiff 破坏性变更（只审契约面） |
| `frontend` | 同上 | Vitest、构建可复现、Playwright（旧 Vue 线） |
| `stacks` | 同上 | `scripts/verify-stacks.sh`（Go＋React 新栈门） |
| `ui-e2e (behavior)` | 同上 | 新栈 Playwright 行为分片 |
| `ui-e2e (shots)` | 同上 | 新栈 Playwright 截图分片 |
| `deploy-gate` | `.github/workflows/deploy-gate.yml` | 干净环境按 README 从零装成 |
| `container-deploy-e2e` | 同上 | 容器部署 e2e |
| `update-rollback-e2e` | 同上 | 一键更新的失败矩阵与回滚 |

任一检查红都会阻塞合并。修完推送后等 CI 重跑；不要靠反复重试掩盖抖动。
（`main` 上另有定时 workflow，不在 PR 检查里。）

### 7.3 合并方式

维护者人工审查通过后，由维护者 **squash 合并**到 `develop`。
外部贡献者不直接 push `develop`，也不自行合并 PR。
合并后的提交信息由维护者按 Conventional Commits 整理。

### 7.4 提交前本地门

不必等 CI 才发现低级问题。提交前在仓库根跑这几条：

```bash
# Go 线：格式、静态检查、全量验证（含 go vet、测试、生成物 --check、许可证门）
(cd server && gofmt -l .)                  # 必须无输出
(cd server && golangci-lint run ./...)
bash server/scripts/verify.sh              # 无 DSN 时 pg/auth/cli e2e 会显性 SKIP；SKIP 不等于绿

# Python 四门（对已提交树跑）
python -I tests/test_repo_structure_gate.py -q
python -I tests/test_gate_lane_roster.py -q
python -I tests/test_doc_reference_gate.py -q
python -I tests/test_public_exposure_gate.py -q

# 生成物一致性：手改生成物会在这里变红
(cd server && bash scripts/generate.sh --check) && (cd ui && node scripts/generate-api.mjs --check)
```

`golangci-lint` 的版本与规则以 CI 的 `tooling-gate` 为准（`server/.golangci.yml`）。

### 7.5 契约改动

- 契约的唯一事实源是 `server/contracts/openapi.yaml`；先改源，再重新生成。
- 生成物 **不得手改**：`server/internal/entry/http/oapi/oapi.gen.go` 与
  `ui/src/lib/api/schema.d.ts` 只由脚本产出。
- 两个 `--check` 必须在本地绿（命令见 §7.4）。
- 破坏性变更由 `contract-gate` 的 oasdiff 拦截；确属有意的破坏性变更，
  先在 issue 里说明理由，再由维护者走登记的豁免通道。

## 8. 本地命令

后端：

```bash
cd server
./scripts/verify.sh
go test -count=1 ./internal/<pkg>/...
```

前端：

```bash
cd ui
npm install
npx playwright install chromium
npm run verify
npx playwright test e2e/<spec>.spec.ts
```

工具链快门（与 CI 的 `tooling-gate` 作业同源）：

```bash
bash scripts/tooling-gate.sh
```

它要 `actionlint`、`shellcheck`、`golangci-lint`、`govulncheck` 与 `npm` 在 PATH；缺工具会以 exit 2 拒绝（跳过不等于通过）。npm 镜像不实现 audit 时，加 `npm_config_registry=https://registry.npmjs.org` 再跑。审计覆盖 `ui` 与 `server/contracts/tools` 两个锁。

契约门（与 CI 的 `contract-gate` 作业同源，只审 `server/contracts/`）：

```bash
(cd server/contracts/tools && npm ci --ignore-scripts)
go install github.com/oasdiff/oasdiff@v1.35.0
bash scripts/contract-gate.sh --base origin/develop
```

spectral 6.17.0 由 `server/contracts/tools/package-lock.json` 钉住，规则集是
`server/contracts/.spectral.yaml`（继承 `spectral:oas`，不关闭规则）；oasdiff 钉 v1.35.0，
只拦 ERR 级破坏性变更。不给 `--base` 时 oasdiff 显性 SKIP。

说明：

- 后端测试需要 PostgreSQL；连接串写在 `server/scripts/verify.sh` 的用法说明里。
- 前端需要 Node `^22.18` 或 `>=24.11`（Babel 8 的下限）。
- 后端用 Go 1.27.2+；`server/go.mod` 的 `toolchain` 行指向 1.27.3，默认的 `GOTOOLCHAIN=auto` 会自动取补丁版。许可证门固定在 1.27.2（上游 go-licenses 读不了 1.27.3 的 module 信息）；工具链快门固定用 1.27.3 分析 stdlib。
- 迭代只跑受影响的包或 spec。收口再跑全量。
- 不要用固定 sleep 等异步结果；用状态或条件等待。

## 9. 完成判据

- 有测试或等价门钉住用户可见结果。
- 相关门全绿，命令与退出码写进 PR 描述。
- 改动面与声明一致；没有顺手改无关文件。
- 文档与行为一致；引用的路径都在库内。
- 不含真实数据。
- 维护者完成审查后合并。

## 10. 许可证

本仓使用 GNU Affero General Public License v3.0。
全文见 `LICENSE`。
提交贡献即表示同意以同一许可证授权。
引用上游代码须保留其许可证声明。
