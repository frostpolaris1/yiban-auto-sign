> [← 返回 README](../../README.md)

## 运维

### 无浏览器部署（纯命令行 / 自动化）

只有 SSH、没有浏览器的机器也能把管理面走完——管理端本身就是一套 JSON 接口，网页只是它的前端。**除 `GET /api/scheduler/executors` 外，下面这些接口属于内部契约**（随版本可能变），自动化脚本请锁定版本并自测。

```bash
B="http://127.0.0.1:$(sed -n 's/^YIBAN_WEB_PORT=//p' deploy/web.env)" ; J=/tmp/yiban.jar
# 登录（会话存 cookie jar）
curl -s -c $J -X POST $B/api/login -H 'Content-Type: application/json' \
     -d '{"username":"你的管理员账号","password":"你的口令"}'
# 写操作要 CSRF 令牌：从 /api/me 取（必须带 -c 回写 cookie，否则令牌不在会话里）
CSRF=$(curl -s -b $J -c $J $B/api/me | python3 -c 'import sys,json;print(json.load(sys.stdin)["csrf_token"])')
# 加账号（管理员提交：默认待审核，审核通过才参与签到）
curl -s -b $J -X POST $B/api/accounts -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
     -d '{"phone":"13800138000","password":"该账号的易班密码","name":"备注名"}'
curl -s -b $J $B/api/accounts                      # 列表（手机号等已脱敏）
curl -s -b $J -X POST $B/api/accounts/0/review -H "X-CSRF-Token: $CSRF" \
     -H 'Content-Type: application/json' -d '{"action":"approve"}'
# 手动签到一次（只影响该账号）
curl -s -b $J -X POST $B/api/signin -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
     -d '{"phone":"13800138000"}'
# 系统设置：POST /api/settings（部分更新）；公告：PUT /api/announcement 写草稿、
# POST /api/announcement/publish（仅主管理员；口令按 YIBAN_PW_GATE 档位——缺省 risk 档
# 同出口的已登录会话可直接生效，换出口才要一次；full 档每次都要）发布或下线
```

绝大多数设置项也可以直接改 `.env` 里的键再重启 web（键名见「配置说明」表；页面上的改动本身就写在 `.env` 里）。危险操作（改他人账号凭据、执行体配置、破坏性设置等）在接口层按 `YIBAN_PW_GATE` 档位要求 `confirm_password` 字段（默认 `risk`：只有风控命中才要，纯脚本调用通常无需带；`full` 档每次都要）。非 `full` 档下的不可逆操作（物理清除 / 删用户 / 急停）另需请求体带 `confirm_delay_ack: true`，否则返回 `reason=delay_ack_required`（前端据此弹倒计时确认框）。

### 备份与恢复

宿主与容器两条备份/恢复路线的完整步骤、取证命令与威胁边界见 [`docs/dev/backup-restore.md`](docs/dev/backup-restore.md)。

### 状态文件清理

按天产生的文件（签到日志、按日结构化状态、用户失败提醒额度账本、调度快照等）由统一策略清理，**宿主与容器共用一套规则**——新增一类按日文件会被检查拦下，状态目录不会逐日累积：

```bash
bash scripts/yiban-cleanup.sh        # 薄包装，宿主 cron 调用（策略唯一在 yiban/adapters/statefiles/state_gc.py）
.venv/bin/python3 -m yiban.entry.cli state  # 默认 dry-run，列出将删除的文件；加 --yes 才动手
```

保留期用环境变量配：`YIBAN_RETENTION_DAYS`（日志与可回看的状态文件，默认 `365`）、`YIBAN_SNAPSHOT_RETENTION_DAYS`（只在近几天有意义的标记/账本，默认 `7`）。清理结果追加到 `<状态目录>/cleanup.log`（运维按它判断清理是否在跑）。

> ℹ️ 清理**绝不能**写签到日志：root 预创建当日签到日志（umask 077）会让 `run.sh` 全部重定向失败、签到静默不执行。

### 多执行体并行签到（可选）

<details>
<summary>🚀 什么时候需要、怎么开、怎么配出口</summary>

**先说结论**：几十到几百个账号**不需要**这一节——单执行体（默认）就够了。什么时候需要，用你自己的机器量：

```bash
.venv/bin/python3 -m yiban.entry.cli capacity   # 按当前窗口/间隔给容量与建议执行体数（不发请求）
```

**(可选) 不想上测试机？让网页现场量一次**：网页「系统设置 → 执行体」里有一个"实测"按钮（仅主管理员可见）：

- **它会用一个真实账号访问易班一次**（登录 + 拉一次签到任务，**不提交签到**，也不写当天的签到结果）；
- **默认用的是账号列表里第一个可签账号**——通常就是**某位真实用户**的账号（不是你自己），
  而且列表顺序稳定，所以每次默认都是同一个账号。要换一个，请用接口显式传 `phone`；
- 有**全局冷却**（默认 10 分钟，`.env` 的 `YIBAN_MEASURE_COOLDOWN` 可调，填 `0` 关闭），没到点会告诉你还要等多少秒；
- **签到时段内点不了**（避免抢正在签到的执行体那份资源）；
- 量出来的数字**不会自动保存**：页面把结果填进输入框，你确认后再保存设置。

> ⚠ **它量到的是"窗口外的最小链路"**：登录 + 拉任务（5 次请求）。真实签到还要往下走
> **定位计算 + 提交签到**（6 次请求再加一段计算），而窗口内它又点不了——所以现场量的秒数
> **偏小、据此换算的容量偏乐观**。**正式定档请以部署者自行量取的完整链路基准为准**
> （自带的离线容量基准工具已于 2026-09 移除；量法与重启条件见 `docs/dev/scheduler-v3.md` §9），
> 两种数字**不要混着填**进设置页的实测值。

**它是怎么分工的**：启动 N 个执行体，它们**不预先分名单**，而是抢着从同一个"待办池"里领账号——谁空了谁领下一个；某个执行体中途挂了，它手上账号的"租约"到期后会被别人接手。所以**同一个账号永远只会被一个执行体登录**（设计红线：重复登录会触发易班风控）。

**怎么开**：

```bash
# .env 或网页「系统设置 → 执行体」配置执行体数量（默认 1；2~64 才会真的拉起多个子进程）
YIBAN_WORKERS=4
```

执行体由同一条命令拉起，**不需要改 cron**（`run.sh` 会读 `YIBAN_WORKERS`）：

```bash
bash /opt/yiban-auto-sign/run.sh          # cron 里仍是这一条
.venv/bin/python3 scripts/signin.py --workers 4   # 手动拉起一轮试跑（打印每个执行体的出口与结果）
```

**兜底常驻执行体**：它在签到时段内**反复扫描"还没签完"的账号**并随手接手（学校晚放号、窗口内刚通过审核的账号、被慢账号拖住的、失败待重试的），窗口结束自动退出。开它需要**两步**（只做第一步不会有任何进程被拉起来）：

```bash
# 第 1 步：打开开关（网页「系统设置 → 执行体」里也有这个开关）
YIBAN_FALLBACK_ENABLE=1

# 第 2 步：在宿主加一条 cron，放在签到窗口开始时（窗口 06:30 起则 06:05 挂上即可）
# 编辑 /etc/cron.d/yiban-sign 追加（用户要与 run.sh 同属主，通常是 yiban）：
5 6 * * * yiban /bin/bash /opt/yiban-auto-sign/scripts/yiban-fallback.sh
```

包装脚本 `scripts/yiban-fallback.sh` 自己读 `.env`（开关关掉时**静默退出**，不会每 5 分钟发一封 cron 邮件），并把日志并到当天的 `sign-YYYY-MM-DD.log`。它是常驻的，**不需要** timeout 包裹；与 `run.sh` 并存是安全的（各自持独立锁文件，分工交给领取池）。cron 模板与细节见该脚本头部注释。

> **cron 行上不要再写 `YIBAN_FALLBACK_ENABLE=1` 前缀（M27 口径变更，2026-10-01）**：非路径键统一为「**`.env` 优先，进程环境只补缺**」——`.env` 里写了 `YIBAN_FALLBACK_ENABLE=0` 时，cron 行上的 `=1` 不再能把它顶开（过去可以）。受影响的是"靠 cron 前缀临时绕过开关"的部署：**开关的真值源只有 `.env`**（网页设置页写的也是它）。想临时改开关就改 `.env`；确实需要在命令行临时覆盖的场景，`env -u` 去掉这个键即可让 `.env` 补进来。同步说明：`run.sh` 对非路径键同样 `.env` 优先；**四个路径键例外**（`YIBAN_DB_FILE` / `YIBAN_STATE_DIR` / `YIBAN_LOG_FILE`），cron/systemd 行上的同名值优先、`.env` 只补缺（工单 71z7，2026-10-20）。

> 上面说的是**服务器（裸机/systemd）部署**。容器部署**不需要**加这条 cron——容器调度器自己在**有效签到窗口内**按同一个 `YIBAN_FALLBACK_ENABLE` 拉起兜底执行体、窗口结束由进程自行退出（判据与口径同一份，见 `docs/dev/scheduler-v3.md` 与 `docs/dev/README.md`）。

**怎么确认在跑**（三条，任选）：

- ① 日志：窗口内应有"兜底执行体启动"与每轮的"本轮处理 N 个账号"——`grep -E '\[fallback( |\])' /var/log/yiban/sign-$(date +%F).log`。2026-10-07 起执行体自己的日志行（每轮行、每账号行、起跑横幅）都带 `[角色 r轮次]` 前缀（兜底 `[fallback r3]`、并行执行体 `[worker-2]`、单执行体 `[single]`），按前缀筛人或筛轮即可；监督进程关于子进程的行不在内（那些行逐条点名槽位号）。空转轮的"本轮处理"行只在事实变化时才落 INFO，其余降 DEBUG（默认级别下不落盘），故窗口内该行的条数约等于"事实变化的次数"，不随轮数膨胀；
- ② 心跳文件 `<YIBAN_STATE_DIR>/fallback-alive.json`（默认 `/var/log/yiban/fallback-alive.json`；自定义状态目录时**跟着状态目录走**）：内容时间戳应在一两个扫描间隔内（进程被 `kill -9` 也能靠它识别）；
- ③ 网页「系统设置 → 执行体」的兜底状态：`running` 才是真在跑；`declared_not_running` = 开关开了但没进程（检查上面那条 cron 是否漏加）。

**执行体的类型/归属怎么看**：每个执行体在数据库里只留一个身份串（`sign_claims.owner`）：

| 类型 | 身份串形态 | 怎么来的 |
|------|------------|----------|
| 并行执行体 | `worker-{序号}@{主机名}` | `YIBAN_WORKERS=N` 拉起的第 i 个子进程 |
| 兜底常驻执行体 | `fallback@{主机名}` | `scripts/yiban-fallback.sh`（或手动 `--fallback`） |
| 单执行体 | `single@{主机名}` | 默认形态（没开上面两项时） |

**身份串是稳定的**：同一台机器上重启执行体**不会换名字**（不复含进程号），所以同一槽位的归属可以跨重启对上；主机后缀是为了**两台机器同时跑时不至于同名**（同名会让它们互相认领对方的账号，即重复登录）。

**网页/接口哪里看**：主管理员读 `GET /api/scheduler/executors`——每个并行执行体带 `role`/`label`（如 `并行执行体 #1`），`fallback` 带开关四态（`off` / `running` / `declared_not_running` / `running_not_declared`）与是否在窗口内，`activity` 给出**当天每个执行体各做了多少**（成功/失败/在飞）。接口字段与脱敏口径见仓库内 `docs/dev/api-executors.md`。

> **身份串不会出现在网页上**：它含主机名，属部署信息；接口只回**1-based 槽位号**（"第 1 个并行执行体"），需要追溯具体进程时请在服务器上直接查库。
>
> **历史数据为什么显示为「未标注（旧数据）」**：旧格式身份串（`exec-` / `fallback-` / `:workers:` 那批）已无法区分角色，接口照实回 `unknown` + 标签「未标注（旧数据）」——这不是 bug，是存量数据的事实；新记录都带各自的前缀，从此可判定。

**出口（代理）怎么分**：每个执行体可以有**自己的出口**，也可以留空走本机出口（配置方法见 [代理配置](#代理配置可选)）。典型填法：有 3 个代理出口、要开 4 个执行体 →

```bash
YIBAN_WORKERS=4
YIBAN_PROXY_LIST=http://a:1,http://b:2,http://c:3      # 第 4 个执行体复用第 1 个出口
YIBAN_PROXY_FALLBACK=http://fb:8080                    # 兜底执行体单独一个出口
```

**怎么看它跑得怎么样**：

- **日志**：每轮结束打印一行汇总，例如 `签到汇总（vX.Y.Z）：✅ 10 成功，❌ 0 失败，⇄ 30 由其他执行体负责`（`⇄` = 由别的执行体领走了，不是失败）；
- **接口**：`GET /api/scheduler/executors` 返回每个执行体的出口与角色标签、兜底四态、当天各执行体计数（`activity`），以及 `measured` / `recommendation`（只有部署者实测并写入 `YIBAN_CAPACITY_MEASURED` 才有值，没实测就是 `null`，不编数字）；
- **进度**：签到分工记录表（`sign_claims`）里一行就是一个账号当天的"了结"情况（保留 14 天）。

**想量一量这台机器能带多少账号**：在本机按完整登录+签到链路量出「单执行体容量」，
写进 `.env` 的 `YIBAN_CAPACITY_MEASURED`，再跑一次 `.venv/bin/python3 -m yiban.entry.cli capacity`
即可得到「建议每执行体账号数（实测 × 2/3）/ 需要几个执行体」。**换机器、换网络都要重新
量**——这是建议值，不是程序上限。（自带的离线容量基准工具已于 2026-09 移除；实测值由
部署者自行量取后录入，重启条件见归档的容量口径设计说明。）

实测的"需要 N 个执行体"与引擎的执行体数口径（`schedule.executor_count`）**不是同一个
数**：实测按"每个进程各自一份限速桶"量取，N 个"执行体"= N 份桶；引擎（调度 v3）的 K 夹在
`[1, 出口数]` 内，是**目标出口口径**——同一出口的多个进程按**出口预算均分**该出口的速率
（每进程子桶速率 = 出口级 λ ÷ 同出口执行体数），**运行期合计 ≤ λ**，加进程不放大该出口的
实际速率（各进程首条仍即时放行，属可接受的小突发）。所以"5000 人要 20–22 个执行体"实际
意味着**声明 20–22 个出口**（`YIBAN_PROXY_LIST` 一行一个）。没声明出口清单的单出口部署里
K≡1 是设计语义，不是故障；要提量，先加出口、再加执行体。

> **虚分片数已定档 64**：它是"谁领哪批待办"的划分单位，**代码常量**（`yiban/core/hrw.py`
> 的 `v_for`，2026-09 定档），**不是配置项**——按需改代码比多一个没人用的键更合本项目的取舍。
> 该重新分档的信号：账号数到 500 以上且并行执行体 ≥4，或执行体之间领取明显不均（有的长期空转、有的积压）。
> 改法、选档公式与改档后要同步重算的测试见该函数 docstring；裁撤与重启条件见 `docs/dev/scheduler-v3.md` §9。

</details>

### 常用运维命令

```bash
# 查看今天签到日志（按天分文件）
tail -50 /var/log/yiban/sign-$(date +%F).log

# 清理过期数据（保留期见「状态文件清理」）
scripts/yiban-cleanup.sh
.venv/bin/python3 -m yiban.entry.cli state --json   # 机器可读的清理结果（默认 dry-run）

# 手动触发签到
bash /opt/yiban-auto-sign/run.sh

# cron 与 Web 服务
systemctl status cron
crontab -l
sudo systemctl restart yiban-web.service   # 只改了 .env 也必须重启（配置在启动时读取）

# 更新代码（**整仓更新**：代码已拆到 yiban/ 多个模块，单文件覆盖会漏文件）
cd /opt/yiban-auto-sign
git pull --ff-only          # 首次用 git clone 部署才有 .git；压缩包部署请重新上传覆盖

# 更新依赖（只更新代码不同步依赖，安全修复就上不了生产）
.venv/bin/pip install -r requirements.lock
sudo systemctl restart yiban-web.service   # 依赖变更后重启常驻服务
# 判据（机器可跑）：bash scripts/check-deps-lock.sh
#   报"venv 与 lock 一致"退 0；任一所锁包版本不符或缺失则退 1 并点名。
# 手工跑上面那条 pip 时，报 "Requirement already satisfied" 即已与 lock 一致。
# 更省事的等价走法（推荐）：`sudo bash deploy/prod/install.sh` 一条命令做完三件事——
#   落位执行件、把旧布局的运行数据迁入 `data/`、再把 venv 同步到 lock 并跑上面那道门。
#   见「运行数据在检出内的 `data/` 子目录」的升级小节。
```

> ✅ **依赖同步已落在裸机流程里**（2026-10-13 补齐，工单 j06f）：2026-10-12 实测到缺口——
> 流程此前只落位执行件与 cron，**不含任何依赖同步**；venv 一旦建好就不再更新，
> lock 后续的安全修复（`urllib3` / `Werkzeug` 的 CVE 修复）上不了生产。
> 现在 `sudo bash deploy/prod/install.sh` 会做这一步，口径三条：
> ① 以 `requirements.lock` 为参调用 `<检出>/.venv` 的 pip（**同一个** venv，与 run.sh、
> systemd 单元同源），不新建环境；
> ② 同步**之前**先落回滚点 `<检出>/deps-snapshot-<时间戳>.txt`（`pip freeze` 输出）；
> 回滚 ＝ **安装器当场打印的那条命令**：venv 有 `bin/pip` 时是
> `<检出>/.venv/bin/pip install -r <该快照>`，缺 `bin/pip` 时是
> `<检出>/.venv/bin/python3 -m pip install -r <该快照>`。别照抄某一支的写法；
> ③ 同步**之后**跑一致性门 `scripts/check-deps-lock.sh`——门红即安装器非 0 退出并打印
> 回滚命令，"装过"不等于"装对了"。
> 只用 crontab 手工运维（不跑安装器）时，仍按上面两条命令手工同步，并手工跑门。

> 面向脚本/agent 的统一入口是 `.venv/bin/python3 -m yiban.entry.cli <子命令>`（`sign` / `probe` / `config` / `capacity` / `state` / `egress` / `db` / `version`，支持 `--json`、非交互、稳定退出码）；`scripts/signin.py`、`scripts/db.py`、`scripts/state_cleanup.py` 是部署面的兼容壳，行为同源。完整契约见 [`docs/dev/cli.md`](docs/dev/cli.md)——本文只给人类用法，不重复契约细节。
