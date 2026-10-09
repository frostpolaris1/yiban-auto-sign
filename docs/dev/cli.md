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
  capacity  容量基准与建议（只读展示与换算建议；实测值由部署者自行量取后录入设置页）
  state     状态文件清理（默认 dry-run，--yes 才动手）
  db        数据库维护（--status / --integrity / --backup [路径] / --restore [路径]）
  version   版本与库版本
```

`scripts/signin.py`、`scripts/db.py`、`scripts/state_cleanup.py` 是**兼容壳**：旧调用方式
（`run.sh`、cron、容器调度器、本文档 §4 的写法）继续可用并与新入口同退出码。
`scripts/notify.py`、`scripts/mailer.py` 两个壳**已删除**，调用方直连 `yiban.notify` / `yiban.mail`。

模块落位：引擎在 `yiban/engine/`（runner / round / schedule / attempts / probe / alerts /
state_io / accounts / workers / config_check / cli_support / db_maintenance），SQLite 层在
`yiban/store/db.py`。入口一律 `python3 -m yiban.cli`（以文件路径直接跑既不支持也不需要）；
开发机在 WSL 下用 `~/.venv-yiban-wsl/bin/python -m yiban.cli`（cwd 为仓库根）。

## 1.1 路径相关环境变量（四个，与 run.sh 同源）

| 变量 | 作用 | 默认 |
|------|------|------|
| `YIBAN_ENV_FILE` | `.env` 文件路径（加密密钥 / 审计密钥来源） | `.env`（当前目录） |
| `YIBAN_DB_FILE` | SQLite 库文件路径 | `yiban.db` |
| `YIBAN_STATE_DIR` | 状态目录（状态文件 / 锁 / 库外锚点） | `/var/log/yiban` |
| `YIBAN_LOG_FILE` | 日志文件路径（按天文件按它的目录落） | `<YIBAN_STATE_DIR>/sign.log` |

**相对路径一律按当前工作目录解析**（`run.sh` 先 `cd` 到部署目录再 export，故"相对路径"
在两条路径下都应指向同一处；在别处直接调用 CLI 时请用绝对路径或先 `cd`）。解析优先级：
进程环境 → `.env` → 默认值。四个变量名在根 `--help`、`config --help`、`db --help` 的
文本里都列明。

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
| 4 | schema 迁移完整性拒启（MF-40）：`user_version` 声称已过某迁移，但完成记录/登记产物缺失 ⇒ 拒绝启动，stderr/异常点名缺哪条 | 区别于配置错误(1)，调度方据此走人工/重试而非告警"零账号" |
| 10 | `--second-run-check` 专用：需要补跑第二轮 | 仅该子命令使用 |

`--workers N` 的汇总码取"最严重者"：`4 > 10 > 1 > 3 > 2 > 0`（4 = 任一执行体迁移拒启，整轮不可信，优先于补签判定）。

`probe` 沿用同一套码值分族（不新增码）：真跑通过 = 0；真跑有失败（存在未通过健康检查的
账号）= 1；**这一轮没做检查**（探针开关关 / 未到触发时间与频率 / 一键暂停或周末门）= 2；
**撞锁**（运行锁被持有或不可用）= 3。此前三种结局一律 0，外部监控看不出探针坏了。

### 3.1 `--json` 失败对象与 `error_kind`（机读分族）

凡带 `--json` 的调用，**任何**退出路径（含用法错误、未知子命令、多余参数、互斥开关、
引擎 argparse 拒绝）都在 stdout 打出**恰一行** JSON 对象；成功对象与失败对象都带
`exit_code`（与进程返回码一致）。失败对象（`ok=false`）额外带机读分族字段 `error_kind`
（取值只增不改、不复用旧名）：

| `error_kind` | 何时出现 | 典型退出码 |
|---|---|---|
| `ok` | 成功 | 0 |
| `failure` | 存在真实失败（账号/网络/配置） | 1 |
| `skipped` | 全部跳过 / 窗口外未了结（含 `probe` 这一轮没做检查） | 2 |
| `locked` | 队列忙（运行锁被持有或不可用） | 3 |
| `schema_migration` | schema 迁移完整性拒启 | 4 |
| `second_run_check` | 需要补跑第二轮 | 10 |
| `usage` | 用法错误（未知子命令、被 argparse 拦下的互斥开关） | 2 |
| `usage_no_command` | 未给子命令 | 2 |
| `usage_extra_args` | 维护子命令收到多余参数 | 2 |
| `usage_conflict` | 互斥开关同时给出（`--yes --dry-run`） | 2 |
| `usage_engine` | sign/probe 透传的引擎 argparse 拒绝 | 2 |
| `config_error` | 配置错误（配置加载失败 / 零账号 / 保留期非法） | 1 |
| `runtime_error` | 运行期失败（库不可读或不存在、备份失败、目录不可用、审计不可写） | 1 |
| `confirmation_required` | 删除类入口未回显目标指纹 | 2 |

六种非法参数此前 stdout 逐字节相同（皆零字节）；现按 `command`、具体 `errors` 文本与
`error_kind` 三者可区分。`rc=2` 的码值**不变**（`run.sh` / 兼容壳依赖它），分族只表达
在错误对象里。

## 4. 命令速查

新入口（推荐给脚本/agent；`--json` 时 stdout 是**单行** JSON 对象）：

```bash
python3 -m yiban.cli version --json
python3 -m yiban.cli config --json            # 脱敏配置检查（不联网）
python3 -m yiban.cli db --status --json       # user_version / 表 / 账号数
python3 -m yiban.cli db --backup /tmp/copy.db --yes          # 写一致性副本（目标不存在）
python3 -m yiban.cli db --backup /tmp/copy.db --yes --force  # 目标已存在：必须显式 --force
python3 -m yiban.cli db --restore /tmp/copy.db --json        # 默认 dry-run：只报告计划 + 目标指纹
python3 -m yiban.cli db --restore /tmp/copy.db --yes --fingerprint <指纹>  # 覆盖当前库
python3 -m yiban.cli state                    # 默认 dry-run，只报告
python3 -m yiban.cli state --yes              # 真删（保留期见 .env）
python3 -m yiban.cli capacity --json          # 读实测值给建议
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

> `capacity` 只消费 `YIBAN_CAPACITY_MEASURED`（部署者自行量取后录入），自己不联网、
> 不转发任何工具。原先的 `capacity --measure` 转发开关随离线容量基准工具族于 2026-09
> 一并移除，传入即按"无法识别的参数"退 2。

`db --backup` 的目标已存在时**不得静默覆盖**上一份副本：不带 `--force` 的 `--yes` 直接
拒绝（退出码 1、零写入），dry-run 只报告"需 `--force`"。这与 §2 第 5 条"幂等"的边界是：
同一命令重复执行不会**累积**副作用（备份仍是同一路径的一份副本），但必须由调用方显式
确认是否要顶掉旧副本，不允许"再跑一次就把上一份悄悄换掉"。`--json` 里 `overwrite_allowed`
给出该判定结果。

`db --restore [路径]` 从 `db --backup` 写出的 SQLite 副本恢复，是**破坏性操作**，默认
dry-run、只报告计划。真正的恢复需要 `--yes --fingerprint <指纹>`（指纹由当前库与备份副本
的内容共同派生，先跑 dry-run 拿）。`--yes` 前还必须满足三条守卫，任一不满足即拒绝且
**零写入**：① 备份可读、`integrity_check` ok、是本项目结构（有 `accounts` 表）；② 备份里的
账号密文能用当前密钥解开（`load_accounts_readonly` 与运行期同一份 AES-GCM 口径）；③ 覆盖前
自动留一份 `<库文件>.pre-restore-<时间戳>` 副本（写不出副本就拒绝恢复）。恢复成功后再校验
结果（integrity ok 且 `user_version` 与备份一致），失败时 `--json` 的 `pre_restore_copy`
给出可回退的副本路径。该子命令不碰 `scripts/backup.sh` 的归档（`.tar.gz/.gpg`）——那套是
`backup.sh --restore` 的职责，本命令只认 `db --backup` 产出的裸 SQLite 副本。
`--json` 的 `user_version` 字段只在 `--status` 模式出现；`--restore` 模式报的是**备份的**
schema 版本 `backup_user_version`（恢复后的库版本已由 `--integrity` 独立回读校验）。

## 5. 相关文档

- 人类教程与常见问题：仓库根 `README.md`（含「多执行体并行签到」「代理配置」章节）；
- 执行体接口（前端用）：`docs/dev/api-executors.md`；
- 模块地图与依赖方向：`docs/dev/README.md`。
