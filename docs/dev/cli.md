# CLI 契约（面向 agent / 自动化）

本项目的命令行**以"被程序调用"为第一优先级**：部署脚本、容器调度器、CI、以及 AI agent
都会调用它；人类日常操作走网页与 README 教程，不必记这些细节。

> 分工（2026-09-16 用户裁决）：**CLI 面向 agent，README 面向人类**。
> 本文是 agent 侧的契约；人类教程在仓库根 `README.md`。

## 1. 形态（**M3 已实施**）

统一入口已落地，七个子命令与 `--json` 全部可用：

```
python3 -m yiban.cli <子命令> [选项]
  sign      一轮签到（可 --workers N / --only 手机号 / --fallback / --second-run-check）
  probe     只读健康检查
  config    配置检查（脱敏打印，不联网）
  capacity  容量基准与建议（默认只读建议；--measure 转发基准工具，需 root 隔离测试机）
  state     状态文件清理（默认 dry-run，--yes 才动手）
  db        数据库维护（--status / --integrity / --backup [路径]）
  version   版本与库版本
```

`scripts/signin.py`、`scripts/db.py`、`scripts/state_cleanup.py` 是**兼容壳**：旧调用方式
（`run.sh`、cron、容器调度器、本文档 §4 的写法）继续可用并与新入口同退出码。
`scripts/notify.py`、`scripts/mailer.py` 两个壳**已删除**，调用方直连 `yiban.notify` / `yiban.mail`。

模块落位：引擎在 `yiban/engine/`（runner / round / schedule / attempts / probe / alerts /
state_io / accounts / workers / config_check / cli_support），SQLite 层在 `yiban/store/db.py`。

## 2. 硬性约定（实施与评审都按这几条）

| # | 约定 | 理由 |
|---|------|------|
| 1 | **不交互**：任何子命令不得等待输入（不做 y/N 确认）；需要确认的事由调用方决定 | agent/CI 里没有 stdin |
| 2 | **stdout 放结果，stderr 放日志** | 调用方要能直接解析 stdout |
| 3 | **`--json` 输出机器可读结构**（单一 JSON 对象打在一行；字段名稳定、不复用） | 解析不用正则 |
| 4 | **退出码稳定且细分**（见 §3），错误信息在 stderr，成功也可以是"什么都没做" | 调用方按码分支 |
| 5 | **幂等**：同一命令重复执行不产生副作用累积（清理、探测、备份都如此） | agent 会重试 |
| 6 | **破坏性操作有 `--dry-run`**，且默认**不加 `--yes` 不动手** | 误删不可逆 |
| 7 | **配置只从 `.env` 与环境变量读**，不从命令行读敏感值（口令/密钥/代理凭据） | 命令行会进 shell 历史与进程列表 |
| 8 | **路径解析与 run.sh 同源**（`YIBAN_STATE_DIR` / `YIBAN_LOG_FILE` / `YIBAN_DB_FILE` / `YIBAN_ENV_FILE`） | 免得"配置一样、清理目录不同" |
| 9 | **不因未配置的可选能力失败**（如未配通知就跳过通知） | 可选能力不该阻塞主流程 |
| 10 | 人类可读输出**不进 stdout**（要给人看的汇总走 stderr/日志） | 保持 stdout 纯净 |

## 3. 退出码表（**只增不改**）

| 码 | 含义 | 备注 |
|----|------|------|
| 0 | 全部成功（或"无需执行"，例如补签轮判定为不需要） | |
| 1 | 存在真实失败（账号/网络/配置错误） | 调用方据此告警 |
| 2 | 全部跳过或存在窗口外未了结 | 宿主 `run.sh` 据此写 `SKIPPED` 并触发补签 |
| 3 | 队列忙（进程级锁被持有） | 手动签到被挡时返回 |
| 4 | schema 迁移完整性拒启（MF-40）：`user_version` 声称已过某迁移，但完成记录/核心产物缺失 ⇒ 拒绝启动，stderr/异常点名缺哪条 | 区别于配置错误(1)，调度方据此走人工/重试而非告警"零账号" |
| 10 | `--second-run-check` 专用：需要补跑第二轮 | 仅该子命令使用 |

`--workers N` 的汇总码取"最严重者"：`4 > 10 > 1 > 3 > 2 > 0`（4 = 任一执行体迁移拒启，整轮不可信，优先于补签判定）。

## 4. 命令速查

新入口（推荐给脚本/agent；`--json` 时 stdout 是**单行** JSON 对象）：

```bash
python3 -m yiban.cli version --json
python3 -m yiban.cli config --json            # 脱敏配置检查（不联网）
python3 -m yiban.cli db --status --json       # user_version / 表 / 账号数
python3 -m yiban.cli state                    # 默认 dry-run，只报告
python3 -m yiban.cli state --yes              # 真删（保留期见 .env）
python3 -m yiban.cli capacity --json          # 读实测值给建议
python3 -m yiban.cli capacity --measure --repo <repo> --users 5000   # 转发基准工具（需 root）
python3 -m yiban.cli sign --workers 4
python3 -m yiban.cli sign --fallback
python3 -m yiban.cli sign --second-run-check  # 退出码 10 = 需要补跑
```

等价的旧写法（兼容壳，退出码一致；`run.sh` / cron / 容器调度器用的就是这些）：

```bash
python3 scripts/signin.py --check-config      # = config
python3 scripts/signin.py --only <手机号>     # 只签指定账号（可逗号分隔）
python3 scripts/signin.py --probe             # = probe
python3 scripts/signin.py --workers 4         # 多执行体并行一轮（父进程监督）
python3 scripts/signin.py --fallback          # 兜底常驻：窗口内反复接手未了结账号
python3 scripts/state_cleanup.py              # = state --yes（宿主 cron 用，无参数）
bash run.sh                                   # 宿主入口：读 .env（含 YIBAN_WORKERS）后执行一轮
```

> `capacity --measure` 转发的是 `scripts/loadtest/capacity_probe.py`（自建假易班、零真实外联、
> 跑完自动还原），**只在隔离测试机上跑**；与 `--json` 互斥（转发工具的 stdout 自成一路）。

## 5. 相关文档

- 人类教程与常见问题：仓库根 `README.md`（含「多执行体并行签到」「代理配置」章节）；
- 执行体接口（前端用）：`docs/dev/api-executors.md`；
- 模块地图与依赖方向：`docs/dev/README.md`。
