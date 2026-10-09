# 开发文档（项目地图）

本目录是**可入库**的开发文档：模块地图、依赖方向、接口契约、分支与发布纪律。

| 文件 | 内容 |
|------|------|
| `README.md`（本文件） | 模块树、依赖方向、多执行体现状与出口配置 |
| `api-executors.md` | 执行体与出口接口契约（前端用） |
| `cli.md` | CLI 契约（面向 agent/自动化） |
| `dev-verify.md` | **统一跑测入口** `scripts/dev-verify.sh` 的配方（本地 WSL 全量 + CI 子集同源） |
| `release-gate.md` | **推送 `main` 的四条门槛**（安全/压力/稳定/脱敏与演练） |
与设计/调研类文稿分开——这里的每条内容都按脱敏红线自查过：
不含真实域名/IP/邮箱/手机号/密钥与站点注册信息，示例一律用占位值。

## 目录树与职责（一句话版）

```
yiban/                        签到引擎与共享基础（可被 web / scripts / docker 复用）
├── clock.py                  时间口径唯一来源（北京时间；禁止各写 datetime.now()）
├── window.py                 签到窗口口径（bounds / from_env / full_sec / retry_hm）
├── status.py                 状态码词汇表（唯一事实源，含两张刻意不合并的映射表）
├── masking.py                脱敏与清洗（手机号 / 日志文本 / URL query）
├── security.py               WAF 判定 + URL 白名单 + 脱敏描述（注入协议层，不内联）
├── egress.py                 **出口（代理）分配**：single / worker / fallback 三角色
├── client.py                 客户端外观：凭据托管 / 会话缓存 / 代理 / 设备绑定 / 签到分支
├── cred_state.py             账密熔断状态文件
├── state_gc.py               按日状态文件的保留期策略与清理
├── logging_ext.py            按天 + flock 的日志 handler
├── platform.py               易班平台事实（端点/请求头/版本）+ 登录与签到编排
├── protocol/                 ★ 洁净室协议库（纯解析/构造；MIT，见其 LICENSE/SPEC.md）
├── geo.py                    多边形内随机定位点（偶奇射线法 + 拒绝采样 + 显式兜底）
├── challenge.py              风控挑战**检测**（只检测不求解）
├── infra/                    叶子工具：locks / env_io / env_lock / account_crypto
├── cli.py                    统一命令行入口（八个子命令；见 docs/dev/cli.md）
├── engine/                   签到引擎（按“执行一轮”切分）：runner / round / schedule
│                             / attempts / probe / alerts / state_io / accounts
│                             / workers / config_check / cli_support
│                             / db_maintenance / egress_admin
├── store/                    数据层：db（连接/迁移）+ accounts / verify_jobs / claims
├── notify/                   通知推送：config / ledger（额度与节流账本）/ transport
└── mail/                     邮件：config / transport

web/                          管理端（Flask 工厂 + 路由 + 模板 + 静态资源）
├── app.py                    create_app 工厂与全部路由（拆分进行中）
├── static/js/pages/*.js      每页一份脚本；共享组件在 core.js（YB.*）
├── static/css/app.css        设计令牌与组件样式
└── deploy/                   systemd / nginx / logrotate 部署模板

scripts/                      运维 CLI（含过渡期的兼容壳）
├── signin.py                 兼容壳 → yiban.engine.runner（旧命令行与退出码不变）
├── db.py                     兼容壳 → yiban.store.db（旧 `import db` 仍可用）
├── state_cleanup.py          状态文件清理 CLI
├── yiban-fallback.sh         兜底常驻执行体的 cron 薄包装（读 .env 判开关，关则静默退出）
├── generate_demo_data.py     演示数据生成（含清库，调用方须回显目标指纹）
└── ledger_check.py           台账对账（与 v20 补账同生，在役）

docker/                       容器：Dockerfile / entrypoint / supervisord / scheduler
```

## 依赖方向（单向，有守卫测试钉住）

```
web / scripts / docker  →  yiban.*  →  infra, platform, protocol, store（`yiban` 不得裸名导入 scripts/ 模块）
```

- `yiban/infra/`、`yiban/protocol/` 与 `yiban/geo.py` **不得导入业务模块**（`tests/test_infra_layer.py`、
  `tests/test_provenance_guard.py`）；
- `yiban/platform.py` 的安全策略必须由调用方**注入**（协议层不自算白名单与拦截判定）；
- `scripts/*.py` 直接运行时**必须先引导 `sys.path`**，且引导要早于任何 `yiban` 导入
  （`tests/test_deploy_entry_imports.py`）。

## 多执行体形态（当前实现状态）

| 能力 | 状态 |
|------|------|
| 领取池与账号级租约（一个账号一天只被一个执行体做） | 已实现（唯一台账表 `sign_tasks`，v18；旧表 `sign_claims` 已冻结） |
| 并行执行体 | 已实现：`signin sign --workers N`（父进程监督 + 子进程领活） |
| 兜底常驻执行体 | 已实现：`signin sign --fallback`（窗口内反复扫"未了结"账号，时段结束退出） |
| 容器形态的兜底常驻 | 已实现：容器调度器在**有效签到窗口内**按开关自动拉起、窗口结束由进程自行退出（与宿主同一个 `YIBAN_FALLBACK_ENABLE`、同一把独立锁、同一份心跳） |
| 每个执行体独立出口 | 已实现：`yiban/egress.py` + 下列环境变量（留空=直连） |
| 每个执行体的存活四态（`running`/`finished`/`idle`/`stale`） | 已实现：并行执行体写固定名心跳文件，接口按心跳新鲜度判定（详见 `api-executors.md`） |
| 账号列表的"上一个业务日是谁签的" | 已实现：`GET /api/accounts` 的 `last_executor` |
| 现场实测单账号耗时 | 已实现：`POST /api/scheduler/executors/measure`（仅主管理员 + 全局冷却 + 窗口内拒绝；**会真实访问易班一次**） |
| 前端页面（执行体与出口配置） | 已实现（系统设置·执行体分区，A1~A4 已落地；契约见 `api-executors.md`） |

### 运行期状态文件（固定名，条数不随时间增长）

| 文件（在 `YIBAN_STATE_DIR`） | 写入方 | 读方 |
|------------------------------|--------|------|
| `worker-alive-<身份键>.json` | 并行执行体监督进程与 v3 执行体（开始 / 存活期刷新 / 正常退出各写一次，创建即 0600）。身份键由角色决定：`worker` 用槽位号（`worker-alive-0.json`），`single`/`fallback` 无槽位号故用角色名（`worker-alive-single.json` / `worker-alive-fallback.json`）——三者都解析出槽位 0，不带角色就会共用一个文件、互相污染四态 | `GET /api/scheduler/executors` 的存活四态 |
| `capacity-measure.json` | `POST /api/scheduler/executors/measure`（冷却占位 + 实测结果） | 同端点（跨进程限频） |
| `fallback-alive.json` | 兜底常驻执行体（每轮扫描刷新，退出时删除） | 告警抑制与 `fallback.alive` |

按日生成的文件（`sign-state-*` / `sched-run-*` / …）的保留期由 `yiban/state_gc.py` 统一管理；
上面这些**不是按日文件**，不参与清理（固定名 + 覆盖写，故无需清理策略）。

### 出口配置（部署者视角）

| 环境变量 | 作用 | 示例 |
|----------|------|------|
| `YIBAN_PROXY` | 单执行体 / 默认出口 | `http://host:3128` |
| `YIBAN_PROXY_LIST` | **每个并行执行体一个**，逗号分隔；空位=该执行体直连 | `http://a:1,,http://c:3` |
| `YIBAN_PROXY_FALLBACK` | 兜底常驻执行体的出口（未设则用 `YIBAN_PROXY`） | `http://fb:8080` |
| `YIBAN_WORKERS` | 并行执行体数（未设=1） | `4` |
| `YIBAN_FALLBACK_INTERVAL` | 兜底执行体扫描间隔秒（默认 60） | `60` |
| `YIBAN_FALLBACK_ENABLE` | 兜底常驻执行体开关（1/true/on/yes=开；未设=关）。进程由部署形态各自拉起：宿主形态要加一条 cron（模板见 `scripts/yiban-fallback.sh` 头注释），容器形态由容器调度器在窗口内自动拉起 | `1` |
| `YIBAN_CAPACITY_MEASURED` | 部署者实测的**单执行体容量**（账号/窗口），用于给出建议值 | `354` |
| `YIBAN_MEASURE_COOLDOWN` | 现场实测端点的**全局冷却秒数**（默认 600，`0`=关闭限频） | `600` |

规则细节（按序取用、不足循环、空位语义）与脱敏口径见 `yiban/egress.py` 的模块文档；
容量基准由部署者在自己的机器上量取后写入 `YIBAN_CAPACITY_MEASURED`（离线基准工具族已于
2026-09 移除），**建议值 = 实测 × 2/3，只是建议**。

## 数据层调用约定

### 校验/取证类读库必须显式 `init_db(cleanup=False, migrate=False)`（M36 · 定档保留）

`yiban.store.db.init_db(...)` 的缺省是 `cleanup=True, migrate=True`，而
`yiban/store/connection.py` 的 `get_conn()` 在单例连接不存在时会**隐式**调一次
`init_db()` 的**全套缺省**。这对业务进程是对的（启动时该做的清理与迁移就该做），
对**校验/取证类工具**是错的：`scripts/audit_verify.py`、哨兵、`db_export.py` 这类调用方
只是想**读**——

- `cleanup=True` 会在校验过程中物理删除超保留期的审计行/事件（**改动了被校验对象**，
  "检出删除"会变成"我自己删的"）；
- `migrate=True` 会回填审计链等结构（同样改动被校验对象）。

**约定（`yiban/store/db.py` 的 `init_db` docstring 已写明）**：只读取证类调用方在第一次
触库前**显式** `db.init_db(cleanup=False, migrate=False)`。仓库内的既有样例：
`scripts/backup_sentinel.py` 的 `_anchor_snapshot()`（M28 的锚点外发读链头）。

**为什么不改 `get_conn()` 的缺省**：它被所有 CLI / 工具 / REPL 调用方共用，改缺省会同时
改变它们"跑一条命令顺手做一次启动清理"的既有行为，属初始化契约裁决，不是本批能顺手改的。
**怎么判断一个调用方算不算"校验类"**：它是否会因为多删了几行/多写了几个字段而改变结论。
会，就显式传 `False`。
