# 目标架构：分层与依赖方向（契约）

> **性质**：设计契约，不是建议。它是分层重构（简化批）的任务书依据。
> **脱敏口径**：本文只含契约部分。不含内部工单号、缺陷计数、主机与账号信息，也不含审查过程记录——那些属内部工作文稿，仅本地留存。
> **仲裁**：**以代码为准**。本文与代码不符时以代码为准，并在该批交付时刷新本文。

## 1. 分层与依赖方向

```mermaid
flowchart TD
    subgraph ENTRY["入口层 · 只翻译参数 + 鉴权"]
        CLI["cli"]
        WEB["web 管理端<br/>口令门 · CSRF · XFF 信任 · 入口限速"]
        CRON["cron · scheduler"]
    end

    subgraph APP["应用层 · 用例编排 · 无算法 · 无 SQL"]
        ROUND["run_round 跑一轮"]
        SIGNONE["sign_one 签一个"]
        ING["ingest 结果入库"]
        REP["report 汇总与告警"]
        COORD["协调 · 派发与工作体池"]
    end

    subgraph CORE["领域核心 · 纯函数 · 无 I/O 无时钟无随机"]
        POL["policy 该不该签"]
        PLAN["plan 分片/抖动/密度"]
        RATE["rate 限速单点裁决"]
        CLS["classify 三分类"]
        IDEM["idempotency 幂等裁决"]
    end

    subgraph PORTS["端口 · 只声明接口"]
        PS["Store"]
        PP["Protocol"]
        PN["Notifier"]
    end

    subgraph ADAPT["适配器 · 唯一的 I/O 面"]
        GATE["持久化门关<br/>命令面 + 查询面<br/>SQL 后端 + 配置后端"]
        PYI["protocol_yiban"]
        PFAKE["protocol_fake"]
    end

    DB[("SQLite")]
    CFG[("'.env' + 'secrets.env'")]
    WORK["签名工作体<br/>子进程 / 常驻池"]

    ENTRY --> APP
    APP --> CORE
    APP --> PORTS
    COORD --> WORK
    WORK --> PYI
    ADAPT -.-> PORTS
    GATE --> DB
    GATE --> CFG
    WEB -.->|"SSE 读事件游标"| GATE
    WEB -.->|"手动签到 · 带鉴权的 ingress"| COORD

    style GATE fill:#ffe6cc,stroke:#d79b00
    style COORD fill:#ffe0e0,stroke:#cc0000
    style CORE fill:#e8f5e9
```

**依赖方向单向**：`entry → app → core`；`adapters` 实现 `ports`。web 只调 `app/`，不 import 持久化实现。

**三条层内红线**：

- `core`：无 I/O、无时钟、无随机——同一输入必得同一输出，可不用 mock 测。
- `app`：无 SQL；事务边界在这一层。
- `entry`：只翻译参数与鉴权，不含业务判据。

## 2. 层准入判据

| 层 | 准入判据 |
|---|---|
| `core` | 是纯函数吗；同一输入必得同一输出吗；能不用 mock 测吗 |
| `app` | 是「一次业务动作」吗；事务边界在这里吗 |
| `adapters` | 是唯一碰外部世界的地方吗 |
| `entry` | 只做参数翻译与鉴权吗 |

## 3. 目标目录树

```text
yiban/
├── entry/                 # 入口层：只翻译参数 + 鉴权
│   ├── cli/               #   七子命令（现 yiban/cli.py）
│   └── （web 管理端独立于本包；docker/scheduler 为容器接线）
├── app/                   # 应用层：用例编排，无算法无 SQL
│   ├── round.py           #   run_round 跑一轮（现 engine/round）
│   ├── sign_one.py        #   sign_one（现 executor_v3 主流程）
│   ├── ingest.py          #   结果入库（现 executor_v3 落库段）
│   ├── report.py          #   汇总与告警（现 alerts）
│   └── coord.py           #   派发与工作体池（现 workers）
├── core/                  # 领域核心：纯函数，无 I/O 无时钟无随机
│   ├── policy.py          #   该不该签（现 attempts/window 判定）
│   ├── plan.py            #   分片/抖动/密度（现 planner）
│   ├── rate.py            #   限速单点（现 token_bucket）
│   ├── classify.py        #   三分类（现 attempts 关键词表）
│   ├── idempotency.py     #   幂等裁决
│   ├── masking.py         #   脱敏（现 yiban/masking，纯函数）
│   └── security_policy.py #   WAF 词表与白名单裁决（现 yiban/security）
├── ports/                 # 端口：只声明接口
│   ├── store.py           #   持久化契约
│   ├── protocol.py        #   协议契约
│   └── notifier.py        #   通知契约
├── adapters/              # 适配器：唯一 I/O 面
│   ├── persistence/       #   门关：命令面 + 查询面
│   │   ├── migrations/    #     迁移账（含方言差异）
│   │   └── audit.py       #     哈希链审计
│   ├── config/            #   env_io + 配置名册加载器 + secrets
│   ├── protocol_yiban/    #   现 yiban/protocol
│   ├── protocol_fake/     #   测试假服务器
│   └── statefiles/        #   状态文件 I/O（现 state_io）
└── workers/               # 签名工作体（slim；进程模型由测量决定）
web/                       # HTTP 适配（独立包；routes 只翻译 + 鉴权）
frontend/                  # Vue（独立构建）
```

### 每模块的演进映射

| 现在的位置 | 目标层 | 处置 |
|---|---|---|
| `engine/planner.py` `schedule.py` `window.py` `token_bucket.py` `hrw.py` | `core/` | 移动（纯函数，改 import） |
| `engine/attempts.py` | `core/classify.py` + `core/policy.py` | 拆分移动 |
| `yiban/masking.py` `yiban/security.py` | `core/` | 移动 |
| `engine/round.py` `alerts.py` | `app/` | 移动 |
| `engine/runner.py` | `app/` | 移动 |
| `engine/probe.py` | `app/`（探针＝一类用例） | 移动 |
| `engine/workers.py` | `workers/` | 移动 |
| `engine/executor_v3.py` | `app/sign_one` + `app/coord` + `workers/` | 拆分（切面评估后进行） |
| `engine/state_io.py` | `adapters/statefiles/` | 移动（I/O 面） |
| `store/*`（除 migrations/audit） | `adapters/persistence/` | 移动 |
| `store/migrations.py` | `adapters/persistence/migrations/` | 移动 |
| `store/audit_chain.py` | `adapters/persistence/audit.py` | 移动 |
| `mail/` `notify/` | `adapters/notifier/` | 移动 |
| `yiban/infra/`（env_io 等） | `adapters/config/` | 移动（＋名册加载器） |
| `yiban/cli.py` | `entry/cli/` | 移动；收掉对存储层的直接引用 |
| `web/app.py` 再导出表 | 打桩面迁移完成后删除 | 拆解（与测试定型同批） |
| `web/routes/` `services/` | `entry`（HTTP）／`app`（用例） | 归层 |
| `docker/scheduler.py` | 容器入口 | 保持 |
| `frontend/` | 独立构建 | 保持 |

**不过关的候选保持不动**——不为目录好看强拆。

## 4. 运行时流程（目标）

```mermaid
flowchart TD
    T["触发：cron · web 手动 · 调度器"] --> LOCK{"运行锁 + 当日已收尾"}
    LOCK -->|"已收尾或已在跑"| X0["退出 0"]
    LOCK -->|可以跑| REC["重启检查：有 started_at 无结果的行"]
    REC -->|有| UNK["标 unknown_outcome + 告警<br/>不自动重试"]
    REC -->|无| SNAP["门关：一次事务读快照<br/>账号 · 点位 · 当日已有结果"]
    UNK --> SNAP
    SNAP --> PLAN["core 纯函数<br/>policy 三门口 + 分片 + 分层抖动 + 密度整形"]
    PLAN --> Q["待签队列 · 内存 · 不落库"]
    Q --> POOL["工作体池 K 个<br/>存活 = 进程句柄"]
    POOL --> RATE["core.rate 单点裁决"]
    RATE --> ATT["门关：记 attempt started"]
    ATT --> TRY["工作体：一次真实尝试<br/>凭据走 stdin · 无状态"]
    TRY --> RES["类型化结果"]
    RES --> CLS["core.classify 三分类"]
    CLS -->|终态| W1["门关：一个事务<br/>写结果 + 幂等核销"]
    CLS -->|可重试| W2["core.plan 退避重排<br/>回内存队列"]
    CLS -->|进程故障| W3["门关：写 attempt 事件（分账）"]
    W1 --> MORE{"队列空 且 窗口未尾"}
    W2 --> MORE
    W3 --> MORE
    MORE -->|还有| RATE
    MORE -->|没有| SUM["汇总 + 门关写收尾 + 通知"]
    SUM --> E1["退出码分族"]

    style Q fill:#fff3cd
```

**崩溃语义（必须钉死）**：

1. 每次真实尝试之前，先记一行「尝试已开始（带 `started_at`）」。
2. 协调者启动时检查「有 `started_at`、无结果」的行：标 `unknown_outcome`、响亮告警、**不自动重试**。
3. 判据：**漏签可接受，双登录不可接受**。在途集合只有 K 条（K ＝ 并发工作体数），崩溃影响面有界。

## 5. 持久化边界（目标）

```mermaid
flowchart LR
    subgraph DISK["落盘 · 只记已发生的事实"]
        direction TB
        D1["尝试事件<br/>账号 · 业务日 · 结果 · 耗时 · 幂等键"]
        D2["账号与点位"]
        D3["schema 版本与迁移账"]
    end
    subgraph MEM["内存 · 只放进行中"]
        direction TB
        M1["待签队列 · 并发槽位"]
        M2["限速器状态"]
    end
    DISK --> R["重启：重算待签集合<br/>在途行标 unknown_outcome"]
    MEM -.->|"崩溃即弃"| R
    R --> RUN["继续跑"]
    style DISK fill:#e1f5ff
    style MEM fill:#fff3cd
```

## 6. 配置与密钥分层（目标）

```mermaid
flowchart TD
    R["config/registry.json · 唯一定义点<br/>类型 · 域 · 默认 · 分组 · 敏感级别 · 热重载"]
    ENV[".env · 行为配置 KEY=value"]
    SEC["secrets.env · 密钥"]
    LOAD["加载器<br/>env 与文件合并 · 域校验"]
    R --> LOAD
    ENV --> LOAD
    SEC --> LOAD
```

三层职责：**名册**（`config/registry.json`）是默认值与合法域的唯一来源；**`.env`** 只放行为配置；**`secrets.env`** 只放密钥。迁移期密钥双写一个版本周期，加载器读 `secrets.env` 优先。

## 7. 施工判据

1. **拆分六判据**：独立领域／分别修改／可独立测／不同依赖／公共容器／牵连无关——**不过关保持不动**。
2. **执行前十问**：其中「能否更小改动」为可是即选小。
3. **单向依赖铁律**：`entry → app → core`；`adapters` 实现 `ports`；web 不 import 持久化实现。
4. **行为保持**：每步「读断言 → 移动 → 全量跑测 → diff 检查」；发现无关缺陷不顺手修。
5. **打桩面迁移与拆分同一批**：不许「先拆后改测试」；分层重构必须与测试定型同批。
