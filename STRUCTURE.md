# 仓库结构

> 目标：初学者 5 分钟看懂。看完本页，你应能回答「我要加一个功能，代码放哪」。

## 1. 一分钟版本

本仓有**两套系统**，互不改写：

- **新系统** = `server/`（Go 后端）+ `ui/`（React 前端）。新功能只写这里。
- **旧系统** = `yiban/`、`web/`、`frontend/`、`tests/`。Python/Vue 旧栈，**冻结**。只在被指派的缺陷修复中改动。

其余是文档、部署与过程稿。

## 2. 顶层目录

| 目录 | 作用 | 状态 |
|---|---|---|
| `server/` | Go 后端。契约、领域、适配器、HTTP 入口 | 新，活跃 |
| `ui/` | React 前端。Adminator 视觉语言 | 新，活跃 |
| `docs/` | 入库文档。架构、运行手册、重构记录 | 共用 |
| `work/` | 过程稿：计划、交接、审查产物。**不入库** | 本地 |
| `deploy/`、`docker/`、`docker-compose.yml` | 旧部署件 | 旧，冻结 |
| `scripts/` | 旧运维脚本 | 旧，冻结 |
| `yiban/` | Python 旧引擎：调度、领取、协议 | 旧，冻结 |
| `web/` | Python 旧 Web 与静态资源 | 旧，冻结 |
| `frontend/` | Vue 旧前端 | 旧，冻结 |
| `tests/` | Python 旧测试 | 旧，冻结 |
| `config/`、`data/`、`out/` | 运行期产物 | 忽略 |

规则：新代码不得写入旧系统目录。跨系统共享的东西只有 `docs/`。

## 3. 后端 `server/`

```
server/
├── cmd/            进程入口
│   ├── server/     主服务
│   └── cli/        运维命令
├── contracts/      OpenAPI 契约，唯一事实源
├── migrations/     PostgreSQL 迁移，按编号排序
├── internal/
│   ├── core/       纯领域：无 IO、无框架、可单测
│   ├── app/        用例与事务编排
│   ├── adapters/   IO：数据库、会话、配置、审计、邮件
│   ├── entry/      HTTP 入口：路由、处理器、生成代码
│   └── signing/    签到 Provider 契约与实现
├── tools/          门与代码生成
├── scripts/        verify、generate
└── canonical.tsv   单一事实源名册
```

依赖方向只有一条：`entry → app → core`。`app` 只依赖 `core` 定义的接口。`adapters` 实现这些接口，由 `cmd` 装配注入。`app` 不得导入 `adapters`。反向导入由 `canonical.tsv` 的 `boundary.*` 门拦截。

### 3.1 域名单

十个域，Go 与 UI 同名同词：

`auth`、`user`、`school`、`account`、`schedule`、`signing`、`photo`、`audit`、`notify`、`diagnostic`

### 3.2 每个域落在哪

| 层 | 路径 | 放什么 |
|---|---|---|
| 纯领域 | `internal/core/<域>/` | 值对象、规则、状态机。不碰数据库与 HTTP |
| 用例 | `internal/app/<域>/` | 一个用户动作一个函数。开事务、调仓储、写审计 |
| 适配器 | `internal/adapters/<设施>/` | 按**设施**分，不按域分：`pg`、`session`、`config`、`audit`、`mail`、`sse`、`image`、`upstream` |
| HTTP | `internal/entry/http/<域>/` | 解析请求、调用用例、写响应 |
| 生成 | `internal/entry/http/oapi/` | OpenAPI 生成物，不手改 |

### 3.3 后端命名

1. 包目录：**单数、小写、无缩写**。`account`，不写 `acc`。
2. 表名：**复数、下划线**。`accounts`、`password_resets`。
3. 路由：`/api/<复数资源>`。`/api/accounts`。
4. operationId：`<Domain><Verb>`。`AccountList`、`AccountCreate`。
5. 迁移：`migrations/postgres/000NN_<域>_<动作>.sql`。
6. 测试：与被测文件同目录，`_test.go` 结尾。

## 4. 前端 `ui/`

```
ui/src/
├── app/            启动、路由、Provider、守卫
├── pages/          路由页，一页一文件，文件名以 Page 结尾
├── features/       按域组织：auth / dashboard / school / account / ...
├── components/
│   ├── shell/      Sidebar / Topbar / Footer / AppShell
│   └── ui/         无域原语：Button / Input / Card / Table / Modal / Tabs / Switch
├── lib/            api / query / theme / format / cx
└── styles/         tokens.css / adminator.css / app.css
```

依赖方向：`pages → features → components/ui → lib`。

- `components` 不得导入 `features` 或 `pages`。
- `features` 之间不得互相导入；共用逻辑下沉到 `lib` 或 `components`。
- `styles/tokens.css` 是唯一定义颜色、圆角、阴影的文件。

### 4.1 前端命名

1. 页面：`XxxPage.tsx`。
2. 组件：`PascalCase.tsx`，导出同名。
3. 用具名导出，不用 `export default`。
4. 路径别名 `@/` 指向 `ui/src`。
5. `lib/api` 按域拆文件：`auth.ts`、`school.ts`、`account.ts`。

## 5. 「我要加一个功能」怎么放

例：管理员暂停账号。

| 步骤 | 落点 |
|---|---|
| 契约 | `server/contracts/openapi.yaml` 加 `AccountPause` |
| 生成 | 跑 `server/scripts/generate.sh` |
| 领域规则 | `server/internal/core/account/` |
| 用例 | `server/internal/app/account/pause.go` |
| 存储 | `server/internal/adapters/pg/account.go` |
| HTTP | `server/internal/entry/http/account/pause.go` |
| 迁移 | `server/migrations/postgres/000NN_account_pause.sql` |
| 前端页 | 不改页面时只加 `ui/src/features/account/` 的按钮与 hook |
| 契约测试 | `server/internal/entry/http/account/pause_test.go` |
| 前端测试 | `ui/src/features/account/*.test.tsx` |

## 6. 门

结构不能只靠自觉。`server/tools/structure` 读 `server/structure.tsv`：

1. 顶层目录必须在名册内。
2. `server/internal/*` 与 `ui/src/*` 的子目录必须在名册内。
3. 新增域必须登记，否则门红。
4. 名册为空、作用域不存在、扫描集为空时门报错，不当绿。未到阶段的作用域用 `?` 标记。

前端导入方向由 `ui/scripts/structure-gate.mjs` 解析模块路径后判定。

两条入口：

- 旧 Python 栈：`scripts/dev-verify.sh`。
- 新 Go + React 栈：`bash scripts/verify-stacks.sh`。
- CI 的两个 job 分别跑这两条。

运行：`cd server && ./scripts/verify.sh`。

## 7. 五分钟检查表

1. 新功能写哪里？`server/` 或 `ui/`，不写旧栈。
2. 找领域规则？`server/internal/core/<域>/`。
3. 找数据库语句？`server/internal/adapters/pg/`。
4. 找接口形状？`server/contracts/openapi.yaml`。
5. 找页面？`ui/src/pages/<Xxx>Page.tsx`。
6. 找可复用组件？`ui/src/components/ui/`。
7. 找颜色与间距？`ui/src/styles/tokens.css`。
