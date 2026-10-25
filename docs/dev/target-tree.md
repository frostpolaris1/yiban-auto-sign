# 仓库目标树：职责、引用纪律与分包规范

> **性质**：设计契约。本文规定全仓目录、依赖方向、分包与命名。
> **范围**：全仓跟踪集。口径＝`git ls-files` 966 项（ref＝`origin/develop` `aa75e2b9`，2026-10-25）。
> **与既有契约的关系**：本文取代 `docs/dev/architecture-target.md` §3 的目标目录树。
> 该文件 §1 的分层红线、§4 运行时、§5 持久化、§6 配置、§7 施工判据、§8 回退闸门继续有效。
> **仲裁**：坐标以本文为准，引擎行为以 `docs/dev/architecture-target.md` 为准。
> **修订记录（2026-10-21，三轮自辩）**：撤销五处改名；保留 `yiban/engine/`；放宽
> `app` 与 `web/services` 的 `adapters` 依赖；批次从五批收到三批。依据见 §0。

---

## 0. 成本账

目标树的每一项都要有人付迁移成本。下表是 2026-10-21 HEAD 的现量（ref 与上文的
2026-10-25 跟踪集数不同；两个数不同源，不可相减）。

| 对象 | 提及文件 | 测试 import | 决定 |
|---|---|---|---|
| `docker/` | 76 | — | 保留，加 README |
| `web/services` | 83 | 60 | 保留，定边界加 README |
| `config/registry.json` | 21 | — | 保留，加 README |
| `web/static/vue` | 29 | — | 保留，加 `BUILT.md` |
| `yiban.engine` | — | 51 | 保留为执行机制层 |
| `yiban.app` | — | 28 | 改名 `yiban/usecases`（B3） |
| `scripts/child_env` | 14 | — | 搬 `yiban/infra/env_child.py` |
| `email_policy` | 5 | — | 搬 `yiban/core/email_policy.py` |
| 开发工具 6 个 | — | 运行时 0 import | 搬 `tools/`，移出镜像 |

口径：`grep -rl` 扫 HEAD，排除 `.venv`、`work`、`out`、`node_modules`。「测试 import」按
`from <包>` 或 `import <包>` 的真导入计数。

结论：五项结构改动（四处改名 + 删 `engine`）合计 260 个读者，行为收益为零。
两项依赖倒置修正的读者合计 19（`child_env` 14、`email_policy` 5）；6 个开发工具在运行时零 import。
本版只做后两项，加上四份 README（`config`、`deploy`、`docker`、`web/services`）、
一份产物说明（`web/static/vue/BUILT.md`）和四道门。

---

## 1. 三条判据

1. **职责单一**：目录名能回答一个问题。回答不了，就不是目录，是杂物间。
2. **引用单向**：上层可以依赖下层。下层不得依赖上层。同层不得成环。
3. **分包可命名**：包名是职责名词。模块名是动作或对象。两者都不含版本、工单、日期。

判据必须能被门检查。只写在注释里的判据不算数。

---

## 2. 目标树（带状态）

状态图例：

- `[保]` 保留现有路径与名字。
- `[移]` 迁移或改名，批次见 §8。
- `[可]` 可选，不占关键路径。
- `[新]` 新增，无前身。

```text
（仓库根）
├── yiban/                         [保] 签到域库：引擎与 web 共用
│   ├── core/                      [保] 纯领域：无 I/O、无时钟、无随机
│   │   ├── clock.py               [保]
│   │   ├── window.py              [保]
│   │   ├── status.py              [保]
│   │   ├── masking.py             [保]
│   │   ├── hrw.py                 [保]
│   │   ├── elbow.py               [保]
│   │   ├── email_policy.py        [移] 现 scripts/email_policy.py
│   │   ├── policy.py              [可] 该不该签
│   │   ├── plan.py                [可] 分片与密度
│   │   ├── rate.py                [可] 限速裁决
│   │   ├── classify.py            [可] 结果三分类
│   │   ├── geo.py                 [可] 需先注入随机源
│   │   └── security_policy.py     [可] WAF 词表与白名单
│   ├── engine/                    [保] 执行机制：排期、尝试、执行体、令牌桶
│   │   ├── schedule.py            [保]
│   │   ├── planner.py             [保]
│   │   ├── attempts.py            [保] 单账号签到尝试
│   │   ├── executor_v3.py         [保]
│   │   ├── token_bucket.py        [保]
│   │   ├── accounts.py            [保]
│   │   ├── cli_support.py         [保]
│   │   ├── config_check.py        [保]
│   │   ├── crash_semantics.py     [保]
│   │   ├── db_maintenance.py      [保]
│   │   └── egress_admin.py        [保]
│   ├── usecases/                  [移] 现 app/，B3 改名；用例编排
│   │   ├── runner.py              [移]
│   │   ├── round.py               [移]
│   │   ├── probe.py               [移]
│   │   └── verify_jobs.py         [移] 现 attempt/jobs.py
│   ├── workers/                   [保] 进程模型
│   │   └── workers.py             [保] 命名例外，见 §6.2
│   ├── adapters/                  [保] 唯一 I/O 面
│   │   ├── persistence/
│   │   ├── protocol_yiban/
│   │   ├── protocol_fake/         [保] 测试假上游，生产入口不得引用
│   │   ├── notifier/
│   │   ├── config/
│   │   └── statefiles/
│   ├── infra/                     [保] 叶子工具
│   │   ├── locks.py               [保]
│   │   ├── private_json.py        [保]
│   │   ├── env_child.py           [移] 现 scripts/child_env.py
│   │   └── logging.py             [可] 现 yiban/logging_ext.py
│   ├── ports/                     [保] 现存空壳（仅 __init__.py，无消费者）
│   │   └── __init__.py            [保] B1 不新增消费者；去留见 B3
│   └── entry/                     [保]
│       └── cli/                   [保]
│
├── web/                           [保] HTTP 适配进程
│   ├── app.py                     [保] 组合根：create_app + 中间件 + main
│   ├── routes/                    [保] 只翻译参数与鉴权
│   ├── services/                  [保] web 应用层：可碰 adapters
│   ├── render.py                  [保]
│   ├── security.py                [保]
│   ├── templates/                 [保] 旧页只减不增
│   ├── static/                    [保]
│   │   ├── css/
│   │   ├── js/
│   │   ├── vendor/
│   │   └── vue/                   [保] 构建产物，保留原名，加 BUILT.md
│   └── deploy/                    [保] systemd / nginx / logrotate
│
├── frontend/                      [保] Vue 源码与 Playwright
│   ├── src/
│   └── e2e/
│
├── scripts/                       [保] 被人或 shell 调用的程序
│   ├── compat/                    [新] 兼容壳，带删除条件
│   ├── gate/                      [保] 门禁数据与基线
│   ├── lib/                       [保] shell 库
│   └── ...                        [保] 运维与门保持平铺
│
├── tools/                         [新] 开发期构建与代码生成，不进镜像
│   ├── build_cjk_font_slices.py   [移]
│   ├── build_lucide_sprite.py     [移]
│   ├── stamp_font_versions.py     [移]
│   ├── dev_visual_seed.py         [移]
│   ├── generate_demo_data.py      [移]
│   └── elbow_shadow.py            [移]
│
├── deploy/                        [保] 部署执行件，三种形态保持分开
│   └── prod/                      [保] 裸机 cron、tmpfiles、install、备份包装
├── docker/                        [保] 镜像、entrypoint、supervisord、调度器
│
├── config/                        [保] 配置项名册，加 README
├── tests/                         [保] 平铺；protocol/ 与 golden/ 子目录
├── docs/                          [保]
├── .github/                       [保] CI 与 PR 模板
│
├── run.sh                         [保] 生产入口，路径稳定
├── run_probe.sh                   [保] 探针入口，路径稳定
├── docker-compose.yml             [保] 容器入口，路径稳定
├── pyproject.toml
├── requirements.txt
├── requirements.lock
├── .env.example
├── .env.docker.example
├── .editorconfig
├── .gitignore
├── .gitattributes
├── .dockerignore
├── .importlinter
├── LICENSE
├── README.md
├── CHANGELOG.md
├── USER_AGREEMENT.md
└── PRIVACY_POLICY.md
```

`yiban/ports/` 是现存空壳（只有 `__init__.py`，无消费者；由 `fc870cd8` 建立）。
B1 不新增其消费者；保留或删除由 B3 拍板。见 §4.4 与 §10。

---

## 3. 职责表

| 目录 | 回答的问题 | 放什么 | 不放什么 |
|---|---|---|---|
| `yiban/core` | 规则是什么 | 纯函数、常量、词汇表 | I/O、SQL、时钟、随机、`yiban.*`（现存数据文件读取例外见 §10） |
| `yiban/engine` | 一轮怎么执行 | 排期、尝试、执行体、令牌桶 | HTTP、参数解析、CLI 退出码 |
| `yiban/usecases` | 一次动作怎么编排 | runner、round、probe、verify_jobs | SQL、HTTP、进程入口 |
| `yiban/workers` | 进程怎么管 | 工作体池、监督、兜底 | 业务判定 |
| `yiban/adapters` | 外部世界怎么接 | 数据库、协议、通知、配置、状态文件 | 业务判定、用例编排 |
| `yiban/infra` | 叶子工具怎么用 | 锁、环境、私有 JSON、日志 | 业务判定、SQL、HTTP；`yiban.*` 导入（现存例外见 §10） |
| `yiban/entry` | 人怎么进来 | 参数翻译、退出码、装配 | 业务判据 |
| `web/routes` | HTTP 怎么收 | 参数翻译、鉴权、调用 service | SQL、adapters、业务编排 |
| `web/services` | web 用例怎么编排 | web 应用层、呈现辅助 | 路由注册、模板 |
| `frontend` | 界面怎么构建 | Vue 源码、Playwright | 构建产物 |
| `scripts` | 运维怎么操作 | 备份、清理、兜底、演练、门 | 可被运行时 import 的库 |
| `tools` | 开发怎么造数据 | 构建、代码生成、演示数据 | 生产运行时依赖 |
| `deploy` `docker` `web/deploy` | 怎么部署 | 三种形态各自的执行件 | 业务代码 |
| `config` | 配置项怎么定义 | 名册、默认值、合法域 | 用户配置值 |
| `tests` | 性质怎么钉住 | pytest、夹具、golden | 运维程序 |
| `docs` | 契约是什么 | 契约类文档 | 过程稿、审查记录 |

### 3.1 新目录准入

新增顶层目录前，必须回答五个问题。

1. 它属于哪条运行边界？
2. 为什么不能是现有目录的子目录？
3. 谁读它？读者在哪个进程？
4. 哪条门覆盖它？
5. 它的 README 写什么？

答不全，不建目录。

---

## 4. 引用纪律

### 4.1 允许的依赖边

```text
core      ← 无内部依赖
infra     ← 无内部依赖
ports     ← core
adapters  ← core, infra, ports
engine    ← core, infra, adapters
usecases  ← core, infra, adapters, engine
entry     ← usecases, engine, core, infra
web/routes    ← web/services
web/services  ← usecases, engine, core, infra, adapters
web/app.py    ← 全部（组合根）
scripts   ← adapters, engine, usecases, core, infra
tools     ← 全部
tests     ← 全部
```

`engine` 与 `usecases` 可以直接 import `adapters`。理由：本仓是模块化单体，不是纯六边形。
代码现状是 `app` 的 3 个文件共 14 处、`engine` 的 12 个文件都在 import adapters。强行加
`ports` 会造出只有一个实现的接口，违反 §4.4。

### 4.2 禁止的依赖边

1. `core` 不得 import 任何 `yiban.*`。
2. `infra` 不得 import 任何 `yiban.*`。
3. `adapters` 不得 import `engine`、`usecases`、`entry`、`web`。
4. `engine` 不得 import `usecases`、`entry`、`web`。
5. `usecases` 不得 import `entry`、`web`。
6. `web/routes` 不得 import `adapters`；路由只调用 `web/services`。
7. 任何运行时模块不得 import `scripts`。
8. `scripts` 不得被 `yiban`、`web`、`docker` 的运行时模块 import。

### 4.2.1 组合根例外

只有下列位置可以装配 `adapters`。

- `web/app.py`
- `yiban/entry/cli/__main__.py`
- `docker/scheduler.py`
- `scripts/**`

例外之外出现 `adapters` 导入，门必须点名 `文件:行`。

### 4.3 路由与服务的边界

`web` 分三层。

```text
routes  →  services  →  yiban.usecases / yiban.engine / yiban.adapters
```

- `routes`：解析请求、鉴权、组装响应。不查库。
- `services`：web 用例编排、呈现辅助。可以查库、调外部。
- `app.py`：组合根与中间件。

这条边界只约束路由。`services` 保持现状，不搬进 `yiban/`。理由：60 个测试直接 import
`web/services`，搬家成本高于收益。

### 4.4 端口准入

`yiban/ports/<name>.py` 只在满足一条时创建。

1. 有至少两个生产实现。
2. 有已提交的测试替身。

两条都不满足，调用方直接使用 `adapters`。端口不是预留位。
现存 `yiban/ports/` 是空壳：B1 不新增消费者，也不判红；空壳去留与端口门由 B3 拍板。

### 4.5 隐藏 import 禁令

以下写法一律禁止。

```python
sys.path.insert(0, os.path.join(root, "scripts"))
import child_env
```

```python
sys.path.insert(0, .../scripts)
from email_policy import review_email
```

理由：它绕过 import-linter 的模块名判据。门看到的是 `child_env`，不是 `scripts.child_env`。
B1 把这两个模块搬进 `yiban/` 后，这段禁令由门钉住。

门做三件事：

1. 扫 `yiban/`、`web/`、`docker/` 的 `sys.path.insert`，目标含 `scripts` 即红。
2. 扫裸 import 名，名字等于 `scripts/**/*.py` 的 stem 即红。
3. 报告带 `文件:行`。

### 4.6 兼容壳

兼容壳只放 `scripts/compat/`。每个壳文件头部必须有一行删除条件。

```text
删除条件：<条件或日期或工单关闭>
```

没有条件的壳，门判红。条件到期未删，门判红。

---

## 5. 分包规范

### 5.1 包与模块

1. 一个包只承担一个职责。
2. 包名与模块名不得重复。`yiban/workers/workers.py` 是已知例外，见 §6.2。
3. 包根不得放散件。`yiban/` 根只允许 `__init__.py`。现有 7 个散件登记在 §6.2。
4. 领域子包至少三个模块。不足三个，平铺成模块。
5. 入口包不受第 4 条约束。`entry/cli` 可以只有两个模块。

### 5.2 `__init__.py`

1. 只放 docstring 与显式再导出。
2. 不放逻辑。
3. 不放副作用。
4. 不做 I/O。

### 5.3 数据与读者

1. 单一读者的数据文件，放在读者模块旁边。
2. 多读者且跨层的，放顶层。
3. `config/registry.json` 是第 2 条的实例：加载器与门都读它。名字容易误解，用 README 纠正，不搬家。

### 5.4 每包 README

每个顶层目录必须有 `README.md`，不超过 30 行，固定四个小节。

```text
## 放什么
## 不放什么
## 入口
## 遗留与删除条件
```

B1 首批补齐 `config/`、`deploy/`、`docker/`、`web/services/` 四份 README 与
`web/static/vue/BUILT.md`。其余顶层目录的 README 由 B2 补齐；README 门见 §9.4。

### 5.5 基线棘轮

`scripts/gate/naming-baseline.tsv` 记录已知命名例外。基线只能缩小，不能增长。B1 建立，B2 起生效。

---

## 6. 命名规范

### 6.1 规则

| 对象 | 规则 | 例子 |
|---|---|---|
| Python 包目录 | `snake_case` | `statefiles/` |
| 非代码目录 | `kebab-case` | `deploy/prod/` |
| 模块 | `snake_case` | `sign_one.py` |
| 可执行程序 | `kebab-case` | `check-import-linter.py`、`dev-verify.sh` |
| 库 | `snake_case`；不得在 `scripts/` | `env_child.py` |
| 测试 | `test_<域>_<不变式>.py` | `test_claim_single_writer.py` |
| 兼容壳 | `scripts/compat/<原命令名>.py` | `compat/signin.py` |
| 禁止 | 版本号、工单号、日期、批次号进文件名 | `test_x_mf60.py` |

### 6.2 已知例外（基线）

| 路径 | 违反的规则 | 目标 | 时机 |
|---|---|---|---|
| `yiban/workers/workers.py` | 包名与模块名重复 | `workers/{pool,supervisor,fallback}.py` | B3 |
| `yiban/challenge.py` | 包根散件 | `adapters/protocol_yiban/challenge.py` | `[可]` 触及即搬 |
| `yiban/client.py` | 包根散件 | `usecases/client.py` | `[可]` 触及即搬 |
| `yiban/egress.py` | 包根散件 | `engine/egress.py` | `[可]` 触及即搬 |
| `yiban/geo.py` | 包根散件 | `core/geo.py` | `[可]` 先注入随机源 |
| `yiban/logging_ext.py` | 包根散件 | `infra/logging.py` | `[可]` 触及即搬 |
| `yiban/platform.py` | 包根散件 | `adapters/protocol_yiban/platform.py` | `[可]` 触及即搬 |
| `yiban/security.py` | 包根散件 | `engine/security.py` | `[可]` 触及即搬 |
| `scripts/deploy_gate.sh` | 可执行程序用了下划线 | `deploy-gate.sh` | `[可]` 有 10 个读者，收益不足 |

基线只许缩小。新增违规直接红。

---

## 7. `web` 边界

### 7.1 只改 6 个路由文件

现状：6 个路由直接 import adapters。B2 逐个改为调用 `web/services`。

| 路由文件 | 直接 import 的 adapters | 目标服务 |
|---|---|---|
| `web/routes/audit_api.py` | `persistence.audit_chain` | 新建 `services/audit_data.py` |
| `web/routes/accounts_api.py` | `persistence.db`、`persistence.accounts`、`notifier.mail` | `services/accounts_data.py` |
| `web/routes/signin_api.py` | `persistence.db` | `services/manual_sign.py` |
| `web/routes/data.py` | `config.env_io`、`persistence.db` | `services/env_io.py`、`services/logs.py` |
| `web/routes/users_api.py` | `persistence.db` | `services/accounts_data.py` |
| `web/routes/stream_api.py` | `persistence.run_events` | `services/run_stream.py` |

### 7.2 不搬 18 个 services

`web/services` 的 18 个模块保持原位。它们有 60 个测试直接 import。搬家不产生用户可见变化。

只做两件事：

1. 加 `web/services/README.md`，写清「web 应用层，可以碰 adapters」。
2. 把直接查库的路由改成调用 services。

---

## 8. 批次

### 8.1 三批

| 批次 | 内容 | 改行为 | 前置门 | 回滚 |
|---|---|---|---|---|
| B1 | 锚点门、命名门、四份 README（`config`、`deploy`、`web/services`、各顶层目录）、`child_env` 入 `infra`、`email_policy` 入 `core`、开发工具移入 `tools/` 并移出镜像 | 否 | 锚点门、命名门 | `git revert` |
| B2 | 6 个路由改调 services、隐藏 import 门、`scripts/compat/` | 否 | 隐藏 import 门、路由边界门 | `git revert` |
| B3 | `app/` 改 `usecases/`、`attempt/jobs.py` 收敛为 `usecases/verify_jobs.py`、`workers/workers.py` 拆三分、端口按需门 | 否 | 端口门 | `git revert` |

三批都是零行为变化。按 `AGENTS.md` §33–§36 减配：每片跑范围档，收口门与全量由主会话跑一次。

B1 搬 6 个开发工具时，同批改五处注释里的旧路径：`account_crypto.py`、`time_prefs.py`、
`migrations.py`、`purge_guard.py`、`executor_v3.py`。这些是注释引用，不是运行时 import，
但改路径不改注释会让下一个读者按旧坐标找不到文件。

### 8.2 锚点表

锚点门只检查状态为「现存」的路径。

| 路径 | 状态 |
|---|---|
| `yiban/core` | 现存 |
| `yiban/engine` | 现存 |
| `yiban/app` | 现存 |
| `yiban/workers` | 现存 |
| `yiban/adapters` | 现存 |
| `yiban/infra` | 现存 |
| `yiban/entry` | 现存 |
| `web/routes` | 现存 |
| `web/services` | 现存 |
| `web/templates` | 现存 |
| `web/static/vue` | 现存 |
| `web/deploy` | 现存 |
| `frontend/src` | 现存 |
| `frontend/e2e` | 现存 |
| `scripts` | 现存 |
| `tests` | 现存 |
| `deploy/prod` | 现存 |
| `docker` | 现存 |
| `config/registry.json` | 现存 |
| `docs/dev` | 现存 |
| `run.sh` | 现存 |
| `run_probe.sh` | 现存 |
| `docker-compose.yml` | 现存 |
| `yiban/ports` | 现存（空壳；B1 不新增消费者） |
| `yiban/infra/env_child.py` | 现存（B1） |
| `yiban/core/email_policy.py` | 现存（B1） |
| `tools` | 现存（B1） |
| `yiban/usecases` | 计划 |
| `yiban/usecases/verify_jobs.py` | 计划 |
| `yiban/workers/pool.py` | 计划 |
| `scripts/compat` | 计划 |

B1 收口时已更新本表（新增 `tools`、`yiban/infra/env_child.py`、`yiban/core/email_policy.py`、
`yiban/ports`）。B3 完成后删除 `yiban/app` 行，加入 `yiban/usecases` 行。

---

## 9. 门清单

### 9.1 现有门

| 门 | 看什么 | 位置 |
|---|---|---|
| 顶层结构门 | 顶层条目 ⊆ 白名单 | `tests/test_repo_structure_gate.py` |
| 分层门 | import 方向 | `.importlinter` + `scripts/check-import-linter.py` |
| 文档引用门 | 入库文本的 `docs/…md` 可解 | `tests/test_doc_reference_gate.py` |
| 入口引导门 | `scripts/*.py` 先引导 `sys.path` | `tests/test_deploy_entry_imports.py` |
| 前端可复现门 | 构建产物与源码同源 | `scripts/check-vue-build-reproducible.sh` |
| 约束注册门 | 在册约束逐条真跑 | `tests/test_constraint_registry_gate.py` |

### 9.2 待建门

| 门 | 看什么 | 批次 |
|---|---|---|
| 锚点门 | §8.2 的「现存」路径存在；B1 搬迁旧坐标清零 | B1（已落地） |
| 命名门 | §6.1 规则 + §6.2 基线只缩不涨 | B1（已落地） |
| 隐藏 import 门 | `sys.path` 与裸名导入 `scripts` | B2 |
| 路由边界门 | `web/routes` 不 import adapters | B2 |
| 端口门 | `ports/*.py` 有第二实现或测试替身 | B3 |

四道先行，端口门随 B3，共五道。

### 9.3 门的共同要求

1. 扫描面为空判红。空扫描不得当绿。
2. 每个门必须有突变验证。删一条真实路径或写一处违规，门必须点名。
3. 报告必须给 `文件:行`。
4. 门自身要独立审查。门放行的东西，就是以后没人守的东西。

### 9.4 暂缓的门

| 门 | 暂缓理由 | 何时建 |
|---|---|---|
| README 门 | B1 由人工补齐并核对 | B1 收口后 |
| 遗留棘轮门 | 还没有 `templates/legacy/` 目录 | 该目录出现时 |
| 壳条件门 | 兼容壳刚进 `scripts/compat/` | B2 收口后 |

暂缓不等于豁免。每条规则在门落地前只作评审依据。

---

## 10. 显式豁免

| 对象 | 豁免内容 | 理由 |
|---|---|---|
| `run.sh` | 留在仓库根 | cron 与 systemd 已依赖该路径 |
| `run_probe.sh` | 留在仓库根 | 探针 cron 已依赖该路径 |
| `docker-compose.yml` | 留在仓库根 | Docker 工具约定 |
| `scripts/dev-verify.sh` | 留在 `scripts/` 根 | `AGENTS.md` 与 CI 已引用 |
| `web/app.py` | 组合根 | 只有它可以 import adapters |
| `web/templates/` | 暂留 Jinja 渲染路径 | 旧页只减不增，不整批搬 |
| `web/services/` | 保留原名与 18 个模块 | 60 个测试直接 import |
| `web/static/vue/` | 保留原名 | 29 个文件提及；`BUILT.md` 说明产物身份 |
| `config/` | 保留原名 | 21 个文件提及；README 说明是名册 |
| `deploy/` `docker/` `web/deploy/` | 三处保持分开 | 76 个文件提及；三种部署形态 |
| `yiban/engine/` | 保留 | 51 个测试 import；是执行机制层，不是同义词 |
| `yiban/workers/workers.py` | 包名重复 | B3 拆分前豁免 |
| `yiban/entry/cli` | 两个模块的子包 | 入口包不受三模块阈值约束 |
| `yiban/adapters/protocol_fake` | 测试假上游留在生产包 | e2e 需要；门只查生产入口不引用 |
| `scripts/deploy_gate.sh` | 下划线 | 10 个读者，改名收益不足 |
| `yiban/infra/env_child.py` | §4.2 第 2 条：infra 不得 import `yiban.*` | 引用 `yiban.adapters.config.env_io`（路径键唯一口径）；B1 零行为搬迁，拆分属 B3 |
| `yiban/core/email_policy.py` | §3：core 不放 I/O | 读取同目录 `email_blocklist.txt`（mtime+size 缓存）；判定与数据同模块，拆分属 B3 |
| `yiban/infra/wire_dump.py` | §4.2 第 2 条：infra 不得 import `yiban.*` | B1 前既有：引用 `yiban.core.{masking,clock}`；触及时处置 |

豁免必须写在这里。豁免不写在注释里。

---

## 11. 非目标

1. 不改签到行为。
2. 不重命名 `yiban/`、`web/`、`config/`、`static/vue/`、`deploy/`、`web/services/`。
3. 不删 `yiban/engine/`。
4. 不新增 `ports/` 的消费者；现存空壳的去留由 B3 拍板。
5. 不按层拆 `tests/`。当前 327 个测试平铺，可搜索。
6. 不为目录好看强拆 `planner`、`schedule`、`token_bucket`。它们有 I/O，搬进 `core` 会破坏 §4.2。
7. 不搬 18 个 `web/services` 模块。
8. 不动协议实现与定位算法。

---

## 12. 新人的五分钟路径

1. 读 §2 目标树，认三条运行边界：引擎、web、前端。
2. 进目标目录，读该目录 README 的「放什么 / 不放什么」。
3. 读「入口」一行，打开入口文件。
4. 用 §4.1 判断依赖方向。
5. 找 web：`routes` → `services` → `yiban.usecases` / `yiban.engine`。
6. 找签到：`entry/cli` → `usecases/runner` → `engine/schedule` → `core`。

前四步不依赖聊天记录、工单或过程稿。

---

## 13. 已决与仍开放

### 13.1 已决

| 议题 | 决定 | 依据 |
|---|---|---|
| `yiban/app` 与 `yiban/engine` 是否合并 | 不合并。`engine` 是执行机制，`app` 改名 `usecases` | 51 对 28 个测试 import |
| `web/services` 是否搬进 `yiban/` | 不搬 | 60 个测试 import |
| `config/` 是否改名 | 不改 | 21 个文件提及 |
| `static/vue/` 是否改名 | 不改 | 29 个文件提及 |
| `deploy/` 是否三合一 | 不合并 | 76 个文件提及；三种形态 |
| `ports/` 空壳去留 | 保留现状；B1 不新增消费者，B3 拍板 | 空壳由 `fc870cd8` 建立，今天零引用 |
| `web/routes` 能否碰 adapters | 不能 | 6 个文件待改 |

### 13.2 仍开放

| 议题 | 当前选择 | 备选 |
|---|---|---|
| `web` 是否改名 `server` | 保留 `web` | 改 `server`，与 `frontend` 成对 |
| `tests` 是否按层拆 | 不拆 | 拆 `unit/` 与 `integration/` |
| `frontend` 是否并入 `web` | 不并入 | 并入 `web/frontend/` |
| 7 个包根散件何时清 | 触及即搬 | 单开一批清空 |

四项都不阻塞 B1。
