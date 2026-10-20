# 分支分级与发布门槛

> 用户 2026-09-16 两次收严，**以第二次为准**：分支按"验证深度"分级，
> `server-web` 必须"在非生产形态演练过、且在**生产机上跑通过一次真实签到链路**"；
> `main` 必须是"在**生产机上完全成功运行了三次以上**且不存在问题的版本"。
> 本文件是这条要求的可执行版本（命令 + 判据 + 台账 + 证据要求）。

## 1. 分支阶梯（推送权限随验证深度递减）

| 分支 | 含义 | 允许推送的条件（须全部满足） |
|------|------|------------------------------|
| `feature/<线>-<主题>` | 单条工作线的功能开发（前端、后端各一条） | 不得含真实凭据、部署与备案信息 |
| `develop` | 集成基线（原 `refactor/web-adminator` 改名而来） | 只收 `feature/*` 合并；四条通用门禁通过 |
| `server-web` | 部署分支（生产按它升级） | ①**发布门槛 = 本地 smoke（假上游 e2e + `--check-config`）+ 备份可恢复演练（`backup.sh --restore` 自检通过）**；netns/hosts/iptables 全隔离演练与容器全形态演练**按需使用**（改部署面/网络面时才跑），不再是固定门槛；②**在生产机上至少跑通一次真实签到链路** |
| **`main`** | **对外默认可见的"已验证"分支** | 在 `server-web` 两条之外，追加：**同一提交在生产机上完成 ≥3 个有效轮次、跨 ≥2 个自然日**，期间无失败、无异常、无人工干预、无回滚；并满足 §2 四条通用门禁 |

**`main` 天然落后于生产若干天，这是设计如此，不是"失同步"。**

## 2. 四条通用门禁（质量底线；`main` 要求全部通过）

### ① 安全测试通过

```bash
# 安全 / 鉴权 / 脱敏相关用例
# --dist loadfile 必带：同文件顺序依赖（MF-107）在纯 xdist 分片下会误红（与 ci.yml fast 轨逐字同口径）
python -m pytest tests/ -q -n 8 --dist loadfile -k "security or mask or audit or login or private or csrf or ratelimit"
# 全量（含上述）
python -m pytest tests/ -q -p no:randomly -n 8 --dist loadfile
# 时区专项子集（仅本子集双 TZ：改时钟/日期相关代码时才需要跑这条）
# 名字含 date/midnight/rollover/utc/tz/retention/saturday/weekend/cross_day 的
# 日期敏感用例（按天文件名、跨天/跨午夜、保留期、周末边界），TZ=UTC 下 ~10s；
# 与 .github/workflows/nightly.yml 的时区专项步逐字同词集。
TZ=UTC python -m pytest tests/ -q -p no:randomly -n 4 --dist loadfile -k "date or midnight or rollover or utc or tz or retention or saturday or weekend or cross_day"
# 脱敏自查：不得含真实域名/IP/邮箱/手机号/密钥、部署与备案信息
# 排除 tests/：测试桩里的示例号码（形如 13800138000）与回环地址不算泄漏
git diff <旧提交>..HEAD -- . ":(exclude)tests" | grep -E "^\+" \
  | grep -nE "<账号名前缀>|[0-9]{1,3}(\.[0-9]{1,3}){3}|1[3-9][0-9]{9}|\bssh \b"
```

判据：安全类用例 **0 失败**；脱敏自查 **0 命中**（命中即修，不得以"看起来是测试桩"放过）。

### ② 压力测试（按需；触碰规模/并发/调度/库写入/网络路径的改动才跑）

离线压测/容量基准工具族（压测造数、hosts/iptables 改写、netns 隔离、容量探针）已于
2026-09 从仓库移除，不再有可跑的演练工具链；容量结论改由部署者在本机自行量取后录入
`YIBAN_CAPACITY_MEASURED`（重启条件见归档的容量口径设计说明）。多执行体一致性仍是一条
可执行的门：

```bash
# 多执行体一致性（4 执行体 × N 账号：零重复登录、领取池均分）
python3 scripts/signin.py --workers 4
```

判据：并发一致性成立（每账号恰好一次登录）；无锁错误/OOM/超时退化。容量类实测值
**只作建议**，绝不写死成跨部署常量；结论要写进当批记忆点文稿（跑了什么、结果数字）。

### ③ 足够稳定

判据：全量测试 **0 失败**（跳过项必须写明原因，如"CI 无 node"）；已知缺陷无新增；
核心路径连续多轮运行无退化（状态文件与日志无异常增长、无重试风暴）。

### ④ 已脱敏 + 发布前 smoke + 备份可恢复演练

```bash
# 裸机 smoke（生产同版本 Python）
python3 scripts/signin.py --check-config        # 退出码 0，且手机号已脱敏
# 本地 e2e smoke（假易班服务端，零外联；与 CI 快车道同一条）
python -m pytest tests/test_login_e2e_mock.py -q -p no:randomly
# 备份可恢复演练：--restore 解包后自动跑 integrity_check 与 audit_verify 并带回结论
bash scripts/backup.sh --restore <最新备份包> <临时目标目录>   # 退出码 0
```

判据：两条 smoke 全绿；备份恢复演练退出码 **0**（backup.sh 的自检失败码：6=明文回退、
7=密文校验不过、8=轮转后当日归档失踪——任一非 0 都必须先修再谈发布）。
**照抄 compose 的 environment**，否则会出现"手动 run 缺 6 个键 → 打不开数据库"的假失败。

两种演练形态各有一键脚本（演练轮，证据只进本地 `work/`，不计入 §3 的有效轮次）：

- **裸机（宿主 / systemd 形态）**：`scripts/drill-baremetal.sh` —— B1 依赖装配、
  B2 `--check-config` 脱敏、B3 假上游 e2e、B4 备份、B5 `--restore` 恢复、B6 `run.sh`
  整轮真签到（三行判据）。在演练树根目录执行，需要 `.venv-bare`。
- **容器形态**：`scripts/drill-container.sh` —— supervisord → container_scheduler →
  signin 全链，含内部网隔离与 egress 探针。

容器形态与 netns/hosts/iptables 全隔离演练
为**按需使用**：只在改动部署面（入口脚本/镜像/cron 模板）或网络面时跑，不再是每次晋升的固定门槛；
镜像内容兜底仍由 `tests/test_docker_image_contents.py` 常跑承接。

## 3. "有效轮次"的唯一定义（不得放宽凑数）

一次 `run.sh` 完整执行，且**真实执行了签到**，日志同时具备三行：

1. `开始执行签到`（**带版本号**，见 §4）
2. `签到汇总` 中 `❌ 0 失败`
3. `run.sh 退出，退出码: 0`（`run.sh:184` 实打串为 `=== run.sh 退出，退出码: $rc ===`）

且当日 `sign-status-<日期>.txt` 为 `SUCCESS`。

- 「补签轮：无需补跑」这类**空转轮不算**；
- 同一自然日的重复轮次**不能**替代跨日证据（`main` 要求跨 ≥2 个自然日）；
- 生产每日两条 cron（首签 + 收尾/补签），干净的日子通常只产生**一个**有效轮次。

**计数清零**：生产在该提交上出现失败、异常（ERROR/Traceback）、人工干预或回滚，
该提交的轮次计数**从零重算**——"不存在问题的版本"要求连续性，不接受"三次里两次好"。

### 3.1 豁免条款（用户 2026-10-13 裁决）

用户原话：**「判据本身有错，豁免。」**

- **账号固有失败不计为失败**：失败原因＝平台要求的签到类型不是普通签到（拍照/扫码/手势等）、
  自动化无法执行，平台返回含 `非法签到` 字样。该类失败**不触发清零、不推翻退出码判据**。
  已知实例 `138****0001`（真实用户、未删除、未暂停）。同类实例另存本地过程稿，不随本仓分发；本判据不依赖该台账。
- **两条过渡期例外（同批记录，非阻断）**：①判据③（见工单 `l065`：日志无 5 秒级重复行，
  该判据只在该工单里定义，本仓无第二处定义）在 `ikik` 部署前不列为阻断；
  ②`sign-status-<日期>.txt` 在 `f7va` 部署前缺失不列为阻断，当日判据以日志
  `run.sh 退出，退出码: …` 收尾行为准。两条修复随 2026-10-17 部署生效。

## 4. 轮次与提交怎么对齐（否则台账无法自证）

生产日志本身不带版本号，故靠两条**可机读**的证据对齐：

1. **引擎在轮次开始时打印版本号**：`yiban/__init__.py` 的 `__version__` 是**唯一来源**，
   `web/__init__.py` 与 `web/app.py` 都引用它（不再各写一份字面量）；
2. **部署记录**：部署时刻 + `git rev-parse HEAD`（见各批次文稿的部署章节）。

台账（写进当批记忆点文稿）：

| 提交 | 版本 | 部署时刻 | 有效轮次（日期） | 期间异常 | 达标 |
|------|------|----------|------------------|----------|------|
| `<sha>` | `vX.Y.Z` | `YYYY-MM-DD HH:MM` | `YYYY-MM-DD`, … | 无 / 有（→ 计数清零） | 是/否 |

## 5. 证据采集命令（全部只读）

生产机命令一律**只读**（`git rev-parse` / `cat` / `grep`），别名与地址不入库：

```bash
ssh <生产别名> 'cd <部署目录> && git rev-parse --short HEAD'
ssh <生产别名> 'date; systemctl show -p NRestarts <服务名>'
ssh <生产别名> 'for f in <状态目录>/sign-status-*.txt; do echo "$f: $(cat $f)"; done'
ssh <生产别名> 'grep -aE "开始执行签到|签到汇总|run.sh 退出" <状态目录>/sign-<日期>.log \
  | sed -E "s/\[[0-9]{11}\]/[号码]/g"'
ssh <生产别名> 'grep -acE "ERROR|Traceback" <状态目录>/sign-<日期>.log'
# cron 链路前提预检（2026-10-03 事故：M07 收紧后存量锁目录属主不符，两轮启动即败；
# 部署验活只看了 web 面。锁目录/状态目录必须以【运行用户】视角检查，root 视角永远通过）：
ssh <生产别名> 'runuser -u <运行用户> -- bash -c "d=<锁目录>; [ -O \$d ] && [ \$(stat -c %a \$d) = 700 ] && echo 锁路径预检 OK || echo 锁路径预检失败（属主或权限不符，参考 run.sh M07）"'
```

> 采集到终端时**务必就地打码**（上面的 `sed` 即为此）：生产日志按账号打印手机号，
> 原样回显会把这些号码带进对话与文稿。

## 6. 推送流程（照抄）

```bash
# 1) 开发分支完成并自测
git push origin develop                 # 集成基线现名 develop（原 refactor/web-adminator）
# 2) 四条通用门禁（§2④ = smoke + 备份可恢复演练；重演练按需）→ 推 server-web
git push origin server-web
# 3) 授权后部署到生产；攒够 ≥3 个有效轮次并把台账写进当批文稿
# 4) 达标的**同一提交**才允许进 main
git branch -f main <已达标提交> && git push origin main && git push gitee main
```

> ⚠ **条件（2026-10-13 补，实测踩到）**：`git branch -f` 只在 `main` 能被该提交**整段覆盖**
> （即 `main` 无独有提交）时正确。若 `main` 已带提交（例：CI/chore 类，其提交不在 `server-web` 线上），
> 强移会**删掉这些改动**——实测会删掉默认分支的 `nightly.yml` 与 `e2e-nightly.yml`，作废 M24
> "排程只在默认分支生效"的修复。此时改用 **merge**：`git merge --no-ff <已达标提交>`，再推双远端；
> 合并后业务树须与 `<已达标提交>` **逐字节相同**（`git diff <合并tip> <已达标提交>` 只许有 CI 文件的差）。

**未达标的唯一正确动作**：留在开发分支（或 `server-web`）继续修。
**禁止**为了让 `main` 看起来同步而先推后补；**禁止**把没在生产跑过的提交推给 `main`。

## 7. 与记忆点的关系

每批在当批文稿里留一段"门禁证据"：四条通用门禁各自的命令与结果、非生产演练形态、
**以及有效轮次台账**。没有这段证据，等同于没通过门禁。

## 8. README 裸机步骤的步级覆盖名册（工单 c9uz）

README「服务器部署（分步详解）」的 7 个编号步骤（折叠标题写作 6 组：环境准备／上传代码／
账号与 .env／run.sh／crontab／手动测试），逐步骤的**载体**与**守卫**如下。本名册回答普查
§八-4 的"零覆盖"：哪几步有仓库载体与门、哪几步只有文字。

| 步 | 内容 | 仓库载体 | 守卫（门／测试） |
|----|------|----------|------------------|
| 1 | 服务器环境准备（`apt install python3 python3-venv python3-pip`；时区） | **无**（宿主操作，不落仓库） | **无**（依赖 `docker/Dockerfile` 的镜像面另有其门，但裸机 apt 面无） |
| 2 | 上传项目代码（打包／解压／`python3 -m venv .venv`／`pip install -r requirements.lock`） | `deploy/prod/install.sh` 的**依赖同步步**（j06f：pip freeze 快照 → 装 lock → 跑门） | `scripts/check-deps-lock.sh`；`tests/test_deploy_deps_sync.py`；install.sh 的 venv 触点收进 M01 门 |
| 3 | 账号数据（网页后台／`YIBAN_ACCOUNTS_JSON` 注入） | `yiban/store/accounts.py` 等 | 既有账号面测试（不在本名册的发布判据面内） |
| 4 | 配置 `.env`（落 `data/.env`） | `deploy/prod/install.sh` 建 `data/`（**唯一建立点**，属服务账号 0700） | `tests/test_runtime_data_layout.py`；`web/deploy/yiban-web.service` 的 `ReadWritePaths` 与 `User=` 同值用例 |
| 5 | `run.sh`（`chmod +x`、`mkdir -p /var/log/yiban`、锁/状态目录 0700） | `run.sh`；锁目录**权威件** `deploy/prod/tmpfiles.d/yiban.conf` | `run.sh` 的 M07 状态/锁目录门；tmpfiles 件随 `deploy/prod/manifest.tsv` 逐件 sha256 对账 |
| 6 | crontab（`31 6`／`12 7` 两行 sign） | `deploy/prod/cron.d/yiban-sign`（另有 cleanup／probe／audit／backup-sentinel 四表） | `scripts/check-cron-provenance.sh`（cron 路径必须能回指仓库原件）；manifest 逐件对账 |
| 7 | 手动测试（`bash run.sh`） | **无**（操作步骤） | **无**（需真实平台；非生产环境用 `tests/test_login_e2e_mock.py` 的假上游 smoke 代替） |

**结论两条**：

- **有载体且有守卫的步**：2、4、5、6。它们由 `deploy/prod/install.sh` ＋ `manifest.tsv` ＋
  `check-cron-provenance.sh` ＋ 各步自己的用例共同守；干净容器内实测 9 件逐件 sha256 对账一致、
  M01 门通过、依赖同步步执行且一致性门绿。**证据落点＝工单 `c9uz` 的 2026-10-16 评论**（含
  drill 的完整 S1–S8 输出与 sha256）；本仓不复制那段输出（它会漂）。
- **无载体的步**：1、3、7。步 1（apt／时区）与步 7（手动测试）是宿主操作，不落仓库；
  步 3（账号）由既有账号面测试覆盖，不属本发布判据面。这三步的"零覆盖"是**如实登记**，
  不作为"已达标"的证据。

### 8.1 发布判据的守卫与其已知边界（工单 c9uz）

- **守卫**：`.github/workflows/deploy-gate.yml` 跑 `bash scripts/deploy_gate.sh`（README 默认
  索引形态）；`tests/test_deploy_gate_wiring.py` 钉住该接线（有 workflow 真调用闸门、缺省索引
  为空、跑 `dc build`＋`dc up -d`、`bash -n`）。闸门坏了 ⇒ 该 job 红。
- **L-2（登记，不称已达标）**：该守卫**不命中** `dev-verify.sh --ci` 的关键词子集（实测 0 条）
  ⇒ **删掉 `deploy-gate.yml` 的 PR 可以过 PR CI 绿**，只由 dev-verify 全量与 nightly 兜
  （nightly 经 `ref: develop` 间接在轨）。按 §2 的可接受性：守卫仍在自动轨上，但"删除接线"
  这一形态在 PR 时刻不红。要收掉需把该用例接进 `--ci` 子集（改 `dev-verify.sh` 的 `run_ci`
  与其冻结清单，属另一笔）。
- **L-4（登记，不改）**：`wait_healthy` 在 `restart: unless-stopped` 且容器长期 `starting` 时
  要烧满 `GATE_TIMEOUT`（默认 600s）才红——响亮但慢。属已知行为，本批不改（改它属另一笔）。

**已知未覆盖（登记，不称已达标）**：README 步 2 的"换目录部署要同步改 run.sh／systemd 单元／
crontab"（工单 6wjd）——`web/deploy/yiban-web.service` 与 `deploy/prod/cron.d/*` 硬写
`/opt/yiban-auto-sign`，换目录需手改多处，与 §1「零手动调整」相冲。6wjd 为独立设计批，
未并入本批。
