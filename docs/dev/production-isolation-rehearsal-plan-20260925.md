# 生产机隔离演练预案（2026-09-25）

> **是什么**：release-gate §1「非生产形态演练（裸机 + 容器）」在**生产机本机**上安全执行的隔离演练预案（操作单级）
> **状态**：定稿（2026-09-25）
> **范围**：覆盖演练的隔离设计、裸机/容器两形态操作单、证据格式、中止与事故预案；**不覆盖**真实签到链路的生产取证（那是 server-web 条件②，另行采集），不覆盖测试机压测（`scripts/loadtest/README.md` 管辖）
> **读者**：在生产机上执行晋升演练的人（含第三方执行人）必读；仓库维护者选读；纯开发任务可略过
> **基线**：`repair/m3-batch0` 工作树（2026-09-25，Task 1–7 合入后）

**关键词**：生产机演练、隔离演练、非生产形态、netns、namespaces、mock 上游、release-gate、rehearsal、iptables、/etc/hosts、容器形态

## 速览（≤5 条结论）

- 引擎**没有**进程级 API base 覆盖（`yiban/fyiban/protocol.py:52-58` 全为模块常量），指向 mock 只能靠 DNS 级重定向——因此裸机首选方案是**专用网络命名空间**（`ip netns`），机器全局的 hosts/iptables 改写在生产机上**默认禁用**。
- netns 新建后**没有默认路由**：出站天然全灭，比 iptables REJECT 更强且零全局副作用；CA 信任、代理摘除、目录/锁/状态全部本来就是进程级/环境级机制。
- 容器形态用 `--internal` 网络 + `--add-host`（容器私有 hosts）+ **不发布任何宿主端口**，宿主生产端口与网络栈零接触。
- 所有机制都挡不住"钟点撞车"：演练一律安排在**北京时间 09:00–17:00**，硬底线 **06:20 前完全还原**（生产签到 cron 06:31/07:12、窗口 06:31–07:49，见 `deploy/prod/cron.d/yiban-sign`）。
- 演练轮**不是** release-gate §3 的「有效轮次」，证据写进当批文稿的"门禁证据"一节即可，不得混入 `main` 台账。

## 目录

1. 适用范围与效力
2. 隔离原则
3. 网络隔离设计
4. 裸机形态操作单（netns 路径）
5. 容器形态操作单
6. 机器全局机制的兜底路径（不推荐）
7. 证据格式
8. 中止与事故预案
9. 已知缺口与待办

---

## 1. 适用范围与效力

本预案回答一个问题：**测试机不再是长期资源后，server-web/main 晋升前的"非生产形态演练"（`docs/dev/release-gate.md` §1 表 `server-web` 行、§2④）如何在生产机上做，且与生产部署隔离到"生产不可能被演练伤到"。**

适用对象：`server-web` 候选提交的晋升前演练（裸机 + 容器两形态）。文中"生产侧"一律指生产部署事实：`/opt/yiban-auto-sign` 应用树、`/var/log/yiban` 状态/日志目录、`/var/lock/yiban` 锁、`yiban.db`、监听 `127.0.0.1:17892` 的 web 服务（`web/app.py:2482` 默认端口；`web/deploy/yiban-web.service` 模板）、`/etc/cron.d/yiban-*` 三张表（`deploy/prod/cron.d/`，含 root crontab 的备份轮）、yiban 用户的 crontab。这些对象演练中**只读**（取证/基线比对），**绝不写、绝不改、绝不重启**。

时间口径全部为北京时间（`yiban/clock.py` 是全项目唯一时间口径）。文中示例手机号一律 `138****0000` 形态；本预案不含真实域名解析结果、IP、凭据。

## 2. 隔离原则

**全维度隔离**——演练与生产在下列每一维都必须物理分开，任何一维"看起来共用无害"都不行（`yiban/infra/env_io.py:resolve_path` 的 docstring 已经写明了共用的下场：*"同一台机器上跑第二份部署时，两份会往同一个 /var/log/yiban 写状态文件、锁与磁盘外锚点，互相污染对方的取证基线"*）：

| 维度 | 生产 | 演练 | 隔离机制（代码依据） |
|------|------|------|----------------------|
| 目录树 | `/opt/yiban-auto-sign` | `/opt/yiban-rehearsal`（候选提交全新 clone） | `run.sh:8` `APP_DIR="${YIBAN_APP_DIR:-/opt/yiban-auto-sign}"`，演练 shell 里 export 覆盖 |
| 端口 | web `127.0.0.1:17892` | 演练 web **不发布宿主端口**，容器内检查用 `docker exec` | `docker/supervisord.conf:[program:web]` 绑容器内回环；宿主监听集不变（§4-S0/C0 快照对账） |
| STATE/日志 | `/var/log/yiban` | `/var/log/yiban-rehearsal` | `run.sh:59` `YIBAN_STATE_DIR`、`run.sh:84-85` `YIBAN_LOG_FILE`（按天名自动派生） |
| **锁** | `/var/lock/yiban/sign.lock` | `/var/lock/yiban-rehearsal` | `run.sh:162` `YIBAN_LOCK_DIR`——**漏掉这键，演练会持住生产的锁，把次日 06:31 签到直接弹成 `exit 0` 静默跳过**（`run.sh:177-180`），这是全预案最贵的一行 |
| DB | 生产 `yiban.db`（含真实账号） | 演练独立库（`seed_accounts.py` 造的假号库，默认落在 `$APP_DIR/yiban.db`） | `run.sh:192` `DB_FILE="${YIBAN_DB_FILE:-yiban.db}"`（cwd=APP_DIR）；引擎侧 `env_io.resolve_path`（进程环境→.env→默认） |
| 上游出口 | 真实易班 | 回环 mock（§3） | netns 无路由 / 容器 `--internal` 无 NAT |
| cron | `/etc/cron.d/yiban-{sign,probe,cleanup}` + root crontab | **一律不安装、不修改**（`deploy/prod/install.sh` 演练中禁跑向生产路径；确需验证安装器只许 `DESTDIR=/tmp/...` 暂存形态）；触发只走人工执行 `run.sh` | cron 表原件在 `deploy/prod/cron.d/`，安装与来源断言见 `install.sh` 头注释 |
| systemd | `yiban-web` 等生产服务 | **零接触**（不 start/stop/restart/edit，NRestarts 只读采集进基线） | `web/deploy/yiban-web.service` |
| .env / 密钥 | 生产 `.env`（只读都不需要） | 演练 `.env` 由 `scripts/loadtest/seed_accounts.py` 生成，含**自造** `YIBAN_ACCOUNTS_KEY`（"绝不复用生产密钥"，seed_accounts.py 头注释） | 禁止复制生产 `.env` 的任何键 |

**红线（违反任一条 = 立即中止，见 §8）**：绝不读写生产的 DB、日志、状态文件、锁目录、`.env`、cron 表、systemd 服务；演练轮产生的网络请求**一条都不许出本机**；生产机上的机器全局改动（hosts/iptables/网络栈/端口监听）在推荐路径下必须为**零**。

## 3. 网络隔离设计

### 3.1 前提事实：引擎无法"进程级改地址"

- 上游端点全部是模块常量：`yiban/fyiban/protocol.py:52-58`（`OAUTH_REDIRECT_URI`、`API_AUTH_URL`、`OAUTH_CODE_HTML_URL`、`OAUTH_USERSURE_URL`、`IFRAME_INDEX_URL`、`SIGN_POSITION_URL`、`SIGN_IN_URL`），URL 不带端口 → mock 必须监听 **443**。
- `YIBAN_*` 键里没有 API base 覆盖；`YIBAN_BASE_PATH` 是 web 应用的反代前缀（`web/app.py:1531-1532`），与上游无关。
- 结论：**把引擎指向 mock 只能改"域名→IP 的解析结果"或"出口路径"**。以下逐一评估这两条路上的现有机制。

### 3.2 现有机制的机器全局副作用评估

| 机制 | 代码依据 | 粒度 | 机器全局副作用（在生产机上的爆炸半径） | 判定 |
|------|----------|------|------------------------------------------|------|
| `/etc/hosts` 改写（mock 工具链现行做法） | `scripts/loadtest/mock_env.py:apply_hosts`（默认 `--hosts-file /etc/hosts`，mock_env.py:376） | **机器全局** | 全机所有进程的解析被改：窗口内若生产轮触发→打回环 mock（真实签到被吞）；`*/10` 探针 cron（`deploy/prod/cron.d/yiban-probe`）与手工 web 触发随时可能吃到假失败并发告警邮件/webhook | 生产机**禁用**（兜底路径 §6 除外） |
| iptables 出站 443 REJECT | `mock_env.py:apply_iptables`（`-I OUTPUT 1/2`，前插放行回环 + REJECT 其余） | **机器全局** | OUTPUT 链全机共享：证书续期（acme 走 443）、pip/apt 的 https 源、任何进程的真实 443 出站全被掐——正是"可打断生产流量" | 生产机**禁用**（兜底路径 §6 除外） |
| 代理键（`YIBAN_PROXY` 等） | `yiban/egress.py:ENV_SINGLE=62`；`yiban/client.py:140-142`（`session.proxies`）；引擎 `requests.Session` 默认 `trust_env=True`（`isolation.py` 模块 docstring 记录此事实） | 进程/环境级 ✓ | 但 `mock_yiban.py` 是 HTTPS 源站、**不是 CONNECT/MITM 代理**，代理通道当前无 mock 可对接；且经代理出口会旁落零外联链（Task 4 正是为此把 `*PROXY*` 键从子进程环境**摘除**：`isolation.py:strip_proxy:48-54`、`scale_driver.py:base_env` 末尾） | 现状**不可用作指 mock 手段**（缺口 G2） |
| CA 信任注入 | `scale_driver.py:base_env`（`REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE`/`CURL_CA_BUNDLE`，scale_driver.py:236-238）；requests 在 `trust_env` 且 verify=True 时读取 | 进程级 ✓ | 无 | **采用** |
| 记账对平 / 零外联探测 | `isolation.py:assert_accounting:76-83`、`require_egress_probe:67-73`、`mock_env.py:verify_zero_egress:335-363`（缺 `--egress-probe-ip` 即判 FAIL） | 进程级判据 ✓ | 无（只是观测/断言） | **采用** |
| **网络命名空间 `ip netns`** | iproute2（`ip-netns(8)`：`ip netns exec` 会把 `/etc/netns/<name>/` 下的同名文件 over-mount 进 `/etc`）；`mock_env.py:376` 的 `--hosts-file` 参数允许把 hosts 改写**落进 netns 专属文件** | **命名空间级**（对外等价于进程级：改动的生效域只有显式 `ip netns exec` 的进程） | 全局仅留两个可枚举、可瞬删的痕：`/var/run/netns/<name>` 引用与 `/etc/netns/<name>/` 目录 | **裸机首选**（§4） |
| **Docker 容器** | 容器私有 `/etc/hosts`（`--add-host`）；`docker network create --internal`（该网无 NAT 网关→容器无法出公网）；不 `-p` 则宿主端口零变化；gunicorn 本就绑容器内回环（`docker/supervisord.conf:[program:web]`） | 容器级 | 无（宿主端口/路由/iptables 表都不动；dockerd 自管的那部分宿主 veth 只挂在该内部网） | **容器形态首选**（§5） |

### 3.3 选定设计：仅用"进程级/命名空间级"机制

**裸机主路径** = `ip netns` 专用命名空间 + netns 内 hosts 重定向 + **无默认路由** + 回环 mock + 进程级 CA 注入 + 环境摘代理 + 全目录/锁/状态覆键（§2 表）。零机器全局改动。

为什么 netns 比"hosts + iptables"更强：新建 netns 里只有 down 状态的 lo，没有任何路由——到真实易班的包连"往哪发"都不存在，零外联是**结构保证**而非规则匹配；iptables REJECT 反而是全局规则 + 本机例外，装错顺序就破功（Task 4 评审已发现过"链中既有 ACCEPT 抢在兜底前放行"的同类问题，`mock_env.py:apply_iptables:268-270` 注释与 progress 台账）。代价与边界要诚实：

- netns 只隔离**网络维**。CPU/内存/IO/时钟与生产共享——所以硬时间规则仍然保留（§3.4）；演练轮必须 `--workers 1`、小账号数（3~5 个），压测级规模一律回测试形态，不在生产机做。
- netns 内 lo 默认 DOWN，忘 `ip -n <ns> link set lo up` 会得到"连接失败"的假阴性，操作单已列入必查步（§4-S2 判据）。
- `/etc/netns` over-mount 行为依赖 iproute2 实现，操作单里用**双向核验**兜住（netns 内解析=回环、netns 外解析=真实，§4-S3 判据），不靠文档背书。

**容器主路径** = `--internal` 网络 + `--add-host` + 独立卷 + 不发布端口（§5）。

**若两把钥匙都没有**（无 root 用不了 iproute2 netns、也没装 docker）：走 §6 的机器全局兜底路径——时窗 + 自动还原护栏 + 硬错峰，属"最不坏"，不是推荐。

### 3.4 时间硬规则（三种路径通用）

1. 演练执行时段：**北京时间 09:00–17:00**（避开 03:00 清理 cron、备份轮与签到全链）。
2. 硬底线 **06:20**：此刻前所有演练资源（netns/容器/mock 进程/临时目录改动）必须已还原并通过 §4-S7/§5-C7/§8 的还原验证。
3. **06:31–07:49 生产签到窗口（cron 06:31 首签、07:12 兜底，`deploy/prod/cron.d/yiban-sign`；有效签收到 `YIBAN_SIGN_END` 默认 07:50 前，`run.sh:262`）内禁止任何演练动作**——含"只是看一眼"的 exec，防止手滑触发。
4. 演练轮自身的"签到窗口"是**合成窗口**：把 `YIBAN_SIGN_START`/`YIBAN_SIGN_END` 指到执行时刻附近（口径照抄 `scale_driver.py:run_once` 的 extra 块：合成窗口 + `YIBAN_START_DELAY_MAX=0` + 两端留白 `YIBAN_WINDOW_EDGE_{FRONT,BACK}_SEC=0`），与真实窗口的"06:30–07:50"（`yiban/window.py:4`）无关。

## 4. 裸机形态操作单（netns 路径）

> 前置：以 root 执行（netns/mock 绑 443 需要）；Python 解释器与生产同版本同依赖（`run.sh:195-199` 的口径：优先 `$APP_DIR/.venv/bin/python3`，否则 `/usr/bin/python3`）。生产若用 venv，演练就在演练树自建 `$R/.venv`（`python3 -m venv $R/.venv && $R/.venv/bin/pip install -r $R/requirements.lock`，只写演练目录，**绝不 pip 进系统环境**）；若用系统解释器，则演练复用之，缺依赖即中止。判据见 S1。
> 记号：`NS=yiban-rehearsal`，`R=/opt/yiban-rehearsal`，`B=$R/loadtest`，`S=/var/log/yiban-rehearsal`，`L=/var/lock/yiban-rehearsal`。

### S0 基线快照（只读，全部留存到 `$R/evidence/baseline/`）

```bash
date; ip netns list > baseline/netns.txt
md5sum /etc/hosts > baseline/hosts.md5
iptables -S OUTPUT > baseline/ipt4.txt; ip6tables -S OUTPUT > baseline/ipt6.txt
ss -ltnp > baseline/ports.txt                       # 确认 17892 在生产侧监听
systemctl show -p NRestarts yiban-web > baseline/svc.txt
ls -la /etc/cron.d/ > baseline/crond.txt; crontab -l > baseline/cron-root.txt
crontab -l -u yiban > baseline/cron-yiban.txt
ls -la /var/log/yiban > baseline/state-ls.txt      # 只看清单不读内容
git -C /opt/yiban-auto-sign rev-parse HEAD > baseline/prod-head.txt
```

**判据**：以上文件全部生成且非空。**回滚**：无（只读）。

### S1 演练目录树（候选提交）

```bash
umask 077; mkdir -p $R && git clone <仓库地址> $R
git -C $R checkout <候选提交sha>
# 与生产对齐解释器：生产用 venv 则自建演练 venv（见本节前置），生产用系统 python3 则跳过：
[ -e /opt/yiban-auto-sign/.venv ] && { python3 -m venv $R/.venv && $R/.venv/bin/pip install -r $R/requirements.lock; }
PYC=$([ -x $R/.venv/bin/python3 ] && echo $R/.venv/bin/python3 || echo /usr/bin/python3)
$PYC --version; $PYC -c "import requests, Crypto, flask" || { echo "缺依赖，中止"; exit 2; }
```

**判据**：`git -C $R rev-parse HEAD` == 候选 sha；`$PYC --version` 与生产实际解释器一致（记入证据）；依赖 import 成功。**回滚**：`rm -rf $R`。

### S2 建命名空间 + 只起 lo

```bash
ip netns add $NS
ip -n $NS link set lo up
ip netns exec $NS ip route                          # 判据用
```

**判据**：`ip netns list` 含 `$NS`；`ip -n $NS link` 显示 lo UP；**`ip -n $NS ip route` 输出为空**（无路由是隔离本体，有路由 = 有人加了，异常）。**回滚**：`ip netns del $NS`（先确认无进程驻留，见 §8 中止步）。

### S3 netns 专属 hosts + 证书（复用 mock_env，零全局写）

> ⚠ **S3 收敛顺序（终审 Important②，先读再做）**：`ip netns exec` 在 exec 启动的瞬间，把
> `/etc/netns/$NS/hosts` **当前 inode** over-mount 到 `/etc/hosts`（iproute2 语义）；而
> `mock_env.py:apply_hosts` 用 tmp + `os.replace`（rename）落盘，会换一个新 inode。若在**同一次
> exec** 内既写 hosts 又做 `verify_zero_egress`，自检里的 `getaddrinfo` 只看得见 exec 启动时
> 钉住的那个旧 inode（首轮 = 覆盖文件尚未存在 ⇒ 回落到宿主真实 `/etc/hosts`；后续轮 = rename
> 前的旧 inode），**永远看不到本轮刚写进去的标记块** ⇒ 自检必失败 ⇒ rc≠0 触发 `mock_env.py`
> 的 try/finally `--restore`（`488-497`）把标记块剥掉。反复重跑停在同一状态=确定性死胡同。
> 因此把「写 hosts」与「验证零外联」拆到**两个进程**：先无 netns、用 `apply_hosts` 一次落盘
> （不带自检、不带 finally，故不会被剥），再另起一次全新 `ip netns exec`——那次的 over-mount
> 钉住的正是已含标记块的 inode，自检的 `getaddrinfo` 当场可见 ⇒ 通过。

```bash
mkdir -p /etc/netns/$NS $B
env | grep -i proxy && { echo "环境含代理键，中止"; exit 2; }   # isolation.assert_no_proxy 同源要求
# (a) 预置（pre-seed）hosts：宿主 shell 里、任何 ip netns exec 之前。先拿宿主当前 hosts 作底
#     （netns 私阅副本，localhost 等条目不丢，绝不写回宿主），再直接调用 mock_env 的写函数
#     （幂等、无自检、无 finally），rename 一次把标记块落进源文件，令其 inode 立即携带映射。
cp /etc/hosts /etc/netns/$NS/hosts
R=$R NS=$NS B=$B python3 - <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.environ["R"], "scripts", "loadtest"))
from mock_env import apply_hosts, DEFAULT_DOMAINS
seed = os.path.join("/etc", "netns", os.environ["NS"], "hosts")
apply_hosts(seed, DEFAULT_DOMAINS, os.path.join(os.environ["B"], "hosts.orig"))
PY
# (b) 全新 ip netns exec 跑完整 mock_env：over-mount 钉住 (a) 已落盘的标记块 inode ⇒
#     进程内 getaddrinfo 见回环、自检通过；其内部 apply_hosts 幂等命中"已是目标内容，跳过"
#     （返回 False ⇒ hosts_changed=False ⇒ 绝不触发 finally --restore），不会剥掉 (a) 的预置；
#     证书仍由 ensure_certs 就地生成（幂等）。
ip netns exec $NS python3 $R/scripts/loadtest/mock_env.py \
  --base-dir $B --hosts-file /etc/netns/$NS/hosts \
  --no-iptables --i-understand-no-isolation \
  --egress-probe-ip 203.0.113.7
```

要点（都有代码依据，不猜）：`--hosts-file` 只写该路径（`mock_env.py:apply_hosts`），不碰宿主 `/etc/hosts`；(a) 的 `apply_hosts` 与 (b) 的 mock_env 内部 `apply_hosts` 是**同一函数**（`mock_env.py:183`，幂等 tmp+rename），(a) 预置后 (b) 命中"已是目标内容，跳过"分支（`mock_env.py:191-193`），故 (b) 不会二次改 inode、也不触发 `488` 的 finally；`--no-iptables` 在 netns 路径下是**升级而非降级**——netns 无路由，iptables 兜底本无必要（`apply_iptables` 若误跑，作用域也仅是 netns 的空表）；`--egress-probe-ip` 必填是 Task 4 的 fail-closed 门（`mock_env.py:404-415` 启动断言 + `verify_zero_egress:358-360`），203.0.113.7 为 RFC 5737 TEST-NET-3 合成靶（`capacity_probe.py:84-85` 同款），在 netns 内必然"不可达"= 判定通过。

**首轮预期行为（拆序后）**：(a) 只落盘一次 hosts（宿主私阅 + 标记块），不跑自检、不改 iptables、不生成证书——
无"自检失败被 finally 剥回"的窗口，源文件 inode 稳定携带标记块；(b) 是第一次 `ip netns exec`，
over-mount 即命中该 inode，自检当场通过、rc=0、证书就绪。**判据**：`mock_env.py` rc=0；双向核验——
`ip netns exec $NS grep -c 'yiban-loadtest-begin' /etc/hosts` ≥ 1（over-mount 生效）；
`grep -c 'yiban-loadtest-begin' /etc/hosts`（宿主）**= 0**；
`getent hosts api.uyiban.com`（宿主）仍解析真实地址；
`md5sum /etc/hosts` == S0 基线；`iptables -S OUTPUT` diff 基线 == 空。
**若 rc≠0 或双向核验不过**：先按本节顶部"收敛顺序"复查 (a)/(b) 是否拆到两个进程、(a) 是否早于任何
`ip netns exec`——**不要**改判为"要动机器全局机制"（那是 §6 的独立前提，见 G4）。
**回滚**：`ip netns exec $NS python3 .../mock_env.py --base-dir $B --hosts-file /etc/netns/$NS/hosts --restore`；`rm -rf /etc/netns/$NS`。

### S4 mock 上游启动 + 自检

```bash
ip netns exec $NS nohup python3 $R/scripts/loadtest/mock_yiban.py \
  --cert $B/ca/server.pem --key $B/ca/server.key \
  --log $B/logs/mock.jsonl --ready-file $B/mock.ready --delay-ms 200 &
until [ -s $B/mock.ready ]; do sleep 0.5; done
ip netns exec $NS curl --cacert $B/ca/ca.pem https://api.uyiban.com/__health   # 必须走域名，验 hosts+SNI 全链
ip netns exec $NS curl --cacert $B/ca/ca.pem https://api.uyiban.com/__stats    # total == 0
```

**判据**：ready 文件含端口 443（`mock_yiban.py:--ready-file/--port` 默认 443、只绑 `127.0.0.1`/`::1`，mock_yiban.py:457-497 的 `create_servers`）；`__health` 200；`__stats.total`==0。证书 SAN 覆盖五个域名（`mock_env.py:_san_conf`），域名清单即 `mock_env.py:DEFAULT_DOMAINS`。**回滚**：`pkill -f "$R/scripts/loadtest/mock_yiban.py"`。

### S5 造号 + 演练 .env

```bash
ip netns exec $NS python3 $R/scripts/loadtest/seed_accounts.py --n 3 \
  --db $R/yiban.db --env $R/.env --state-dir $S --log-file $S/sign.log \
  --gap 2 --window-start <now+10min 的 HH:MM> --window-end <now+50min 的 HH:MM>
```

**判据**：rc=0；`$R/.env` 含自造 `YIBAN_ACCOUNTS_KEY` 与合成窗口键（seed_accounts.py 头注释：密钥自造、绝不用生产密钥；账号手机号为测试桩号码，本预案示例一律 `138****0000` 形态）。**回滚**：删 `$R/.env` 与 `$R/yiban.db`。

### S6 一整轮 run.sh（部署路径真跑）

```bash
ip netns exec $NS env \
  YIBAN_APP_DIR=$R YIBAN_STATE_DIR=$S YIBAN_LOG_FILE=$S/sign.log \
  YIBAN_LOCK_DIR=$L YIBAN_HOST_SECOND_ROUND=0 \
  YIBAN_NOTIFY_URL= YIBAN_PROBE_ENABLE=0 YIBAN_GLOBAL_PAUSE=0 \
  REQUESTS_CA_BUNDLE=$B/ca/ca.pem SSL_CERT_FILE=$B/ca/ca.pem CURL_CA_BUNDLE=$B/ca/ca.pem \
  bash $R/run.sh; rc=$?
```

键语义逐条对照 `run.sh`：APP_DIR（:8）、STATE（:59）、LOG（:84）、LOCK（:162，**不可漏**）、`YIBAN_HOST_SECOND_ROUND=0` 关进程内补签轮保证单轮（:219）、通知/探针全灭防告警外发（告警走 `yiban/engine/alerts.py` → webhook/邮件，演练环境必须哑火）、CA 三键与 `scale_driver.py:base_env` 同源、合成窗口来自 S5 的 `.env`。

**判据（五项全过才算演练裸机通过）**：

1. **rc**：`rc == 0`；
2. **sign-status**：`cat $S/sign-status-$(date +%F).txt` == `SUCCESS`（且经 `run.sh:_status_credible_success` 的库内事实交叉核对——它查的就是 `$R/yiban.db`，天然演练侧）；
3. **日志三行**（`$S/sign-$(date +%F).log`，语义即 release-gate §3 三要素）：
   - `开始执行签到（v<版本>）`——`yiban/engine/runner.py:370`（版本号来自 `yiban/__init__.py:__version__`，release-gate §4）；
   - `签到汇总（v<版本>）：✅ 3 成功，❌ 0 失败`——`runner.py:551/558`；
   - `=== run.sh 退出，退出码: 0 ===`——`run.sh:_on_exit:121`（现行字面量，release-gate §3 的"run.sh 执行完成，退出码: 0"按此现行格式采证，映射关系记入证据文稿）；
4. **记账对平**：`__stats.total` == `$B/logs/mock.jsonl` 行数，且 > 0（`isolation.py:assert_accounting` 语义；差值 = 有请求旁路出真实出口，红线事故）；mock JSONL 的 peer 只含 `127.0.0.1`/`::1`；
5. **零真实外联复核**：`ip netns exec $NS ip route` 仍为空 + `ip netns exec $NS python3 -c "import socket;s=socket.create_connection(('203.0.113.7',443),3)"` 必须抛错。

**回滚**：`timeout` 包整步或记秒表——超 15 分钟不收敛即按 §8 中止（生产机不是压测场）。

### S7 清理与还原验证

```bash
pkill -f "$R/scripts/loadtest/mock_yiban.py" || true
ip netns exec $NS python3 $R/scripts/loadtest/mock_env.py --base-dir $B --hosts-file /etc/netns/$NS/hosts --restore
ip netns del $NS; rm -rf /etc/netns/$NS
# 还原对账（与 S0 基线逐项 diff，全部只读）：
md5sum -c baseline/hosts.md5; diff <(iptables -S OUTPUT) baseline/ipt4.txt
diff <(ss -ltnp) baseline/ports.txt; systemctl show -p NRestarts yiban-web   # 与 baseline 相同
ip netns list | grep -c $NS || true                                          # 0
ls -la /var/log/yiban | diff - baseline/state-ls.txt || true                 # 仅允许生产自身的正常日增量
```

**判据**：除"生产自身正常增量"（cron 轮、web 日志）外全绿；`$R/evidence/` 按 §7 归档后整个 `$R`、`$S`、`$L` 删除。**回滚**：本步即回滚；若 `ip netns del` 报 busy，说明有驻留进程——按 §8 的进程清退步处理，不得留着过夜。

## 5. 容器形态操作单

### C0 基线快照：同 S0，另加 `docker ps -a > baseline/docker-ps.txt`、`docker network ls > baseline/docker-net.txt`。

### C1 镜像构建（部署路径真跑之一）

```bash
cd $R && docker build -t yiban-rehearsal:<short-sha> -f docker/Dockerfile . \
  ${PIP_INDEX_URL:+--build-arg PIP_INDEX_URL=$PIP_INDEX_URL}
docker run --rm yiban-rehearsal:<short-sha> ls /app/yiban/__init__.py /app/scripts/container_scheduler.py
```

**判据**：构建 rc=0（索引不可达用 `--build-arg PIP_INDEX_URL`，`docker/Dockerfile:ARG PIP_INDEX_URL`）；镜像含 `yiban/` 与容器调度件（`test_docker_image_contents.py` 的兜底内容人工等价核验）。**回滚**：`docker rmi`。

### C2 独立卷 + 演练配置

```bash
mkdir -p $R/data && cd $R
python3 scripts/loadtest/seed_accounts.py --n 3 --db $R/data/yiban.db \
  --env $R/data/.env --state-dir $R/data/state --log-file $R/data/logs/sign.log \
  --gap 2 --window-start <now+10min> --window-end <now+50min>
```

`.env` 放卷内（`YIBAN_ENV_FILE=/data/.env`），密钥自造。**判据**：卷目录里 `.env`/`yiban.db` 就位，文件 mode 0600/0700（umask 077）。**回滚**：删 `$R/data`。

### C3 内部网络 + 容器私有 hosts

```bash
docker network create --internal yiban-rehearsal-net
```

`--internal` = 该网无 NAT 出网关（容器无论如何到不了真实易班，容器形态的"结构保证"，对应裸机的"无路由"）。五个域名经 `--add-host` 写进**容器私有** `/etc/hosts`（`mock_env.py:DEFAULT_DOMAINS` 同源清单）：

```bash
ADDS=$(for d in oauth.yiban.cn f.yiban.cn api.uyiban.com c.uyiban.com app.uyiban.com; do echo -n "--add-host $d:127.0.0.1 "; done)
```

### C4 起容器（两进程 RUNNING 判据）

```bash
docker run -d --name yiban-rehearsal --network yiban-rehearsal-net \
  --add-host api.uyiban.com:127.0.0.1 --add-host oauth.yiban.cn:127.0.0.1 \
  --add-host f.yiban.cn:127.0.0.1 --add-host c.uyiban.com:127.0.0.1 \
  --add-host app.uyiban.com:127.0.0.1 \
  --security-opt no-new-privileges:true --cap-drop ALL \
  --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
  --cap-add SETGID --cap-add SETUID --cap-add KILL \
  --memory 512m --cpus 0.5 \
  -e TZ=Asia/Shanghai -e YIBAN_ENV_FILE=/data/.env -e YIBAN_DB_FILE=/data/yiban.db \
  -e YIBAN_ACCOUNTS_FILE=/data/accounts.json -e YIBAN_STATE_DIR=/data/state \
  -e YIBAN_LOG_FILE=/data/logs/sign.log -e YIBAN_COOKIE_SECURE=1 \
  -e YIBAN_GLOBAL_PAUSE=0 \
  -v $R/data:/data yiban-rehearsal:<short-sha>
```

environment **照抄 compose 全集**（`docker-compose.yml:services.yiban.environment`：TZ + 六个 `YIBAN_*` 键）——release-gate §2④ 点名的"照抄 compose 的 environment，否则缺 6 个键→打不开数据库"的假失败就靠这条防；cap/`no-new-privileges` 照抄 compose（审查 P2-11 的收敛集）。**不发布任何宿主端口**：gunicorn 绑容器内 `127.0.0.1:17892`（`docker/supervisord.conf:[program:web]`），宿主 17892 是生产 web 的，`-p` 既无必要也无可能穿透容器回环——端口重映射在本形态下的实现就是"宿主端口集零变化 + 全部检查走 `docker exec`"。资源钳（`--memory/--cpus`）保证与宿主 cron 共存时不抢生产。

**判据**：
- web RUNNING：`docker exec yiban-rehearsal python3 -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:17892/login',timeout=3).status)"` ∈ {200, 3xx}（同 compose healthcheck 形状）；
- sched RUNNING：`docker top yiban-rehearsal | grep -c container_scheduler` ≥ 1，且 `docker exec yiban-rehearsal tail -5 /data/logs/sched.log` 有 tick 留痕（`docker/scheduler.py` 分钟级闩锁，`docker/Dockerfile` 复制为 `scripts/container_scheduler.py`）；
- `docker inspect -f '{{.RestartCount}}' yiban-rehearsal` == 0；
- 宿主侧 `ss -ltnp` diff C0 基线 == 空。
**回滚**：`docker rm -f yiban-rehearsal; docker network rm yiban-rehearsal-net`。

### C5 容器内一整轮签到（可选加强项：容器形态也真跑签到链）

```bash
docker exec -it yiban-rehearsal bash             # 容器内 exec 为 root（entrypoint 降权不影响 exec）
# 证书生成复用 mock_env（容器内跑；hosts 写进卷内暂存文件即可，域名→127.0.0.1 已由 --add-host 就位；
# 容器无 NET_ADMIN，iptables 步骤显式跳过并知情确认）：
python3 scripts/loadtest/mock_env.py --base-dir /data/loadtest \
  --hosts-file /data/loadtest/hosts.scratch --no-iptables --i-understand-no-isolation \
  --egress-probe-ip 203.0.113.7
python3 scripts/loadtest/mock_yiban.py --cert /data/loadtest/ca/server.pem \
  --key /data/loadtest/ca/server.key --log /data/logs/mock.jsonl --ready-file /data/mock.ready &
REQUESTS_CA_BUNDLE=/data/loadtest/ca/ca.pem SSL_CERT_FILE=/data/loadtest/ca/ca.pem \
  bash run.sh 2>&1 | tail                        # 合成窗口/哑火键已在 /data/.env
```

判据同 S6 五项（日志在 `/data/logs/sign-<日期>.log`，status 在 `/data/state/`；容器 netns 内域名→127.0.0.1 由 `--add-host` 完成，443 回环绑定容器 root 直接可绑）。**与宿主 cron 共存安全**：容器网络 `--internal` 无出口、宿主 hosts/iptables 未被碰、锁与状态都在容器卷内——宿主 06:31 轮全程无感知；但 C4/C5 同样受 §3.4 时间硬规则约束（防的是资源抢占与人为误操作，不是网络串扰）。

### C6/C7 清理与还原验证

`docker rm -f` + `docker network rm` + `docker rmi`（证据归档后可留镜像供复跑，生产机过夜只允许留"无运行容器"态）；对账 = S7 全套 + `docker ps -a`/`docker network ls` diff C0 基线。**判据**：无 `yiban-rehearsal*` 残留容器/网络；宿主端口/防火墙/hosts 三连 diff 全空。

## 6. 机器全局机制的兜底路径（不推荐）

仅当 S2/S3 与 §5 都不可行（无 iproute2 netns 能力且无 docker）时使用 `mock_env.py` 原生形态（全局 hosts + 全局 443 REJECT）。必须同时满足全部四条护栏：

1. **硬错峰**：只在 09:00–17:00 执行，06:20 前必须已 `--restore` 并过 S7 对账；预计超时（>2h）不许开始。
2. **即时回滚守护**：开始改写前装定时器，任何异常/会话掉线都会自动还原——
   `nohup bash -c 'sleep 7200; sudo python3 $R/scripts/loadtest/mock_env.py --base-dir $B --restore' >/dev/null 2>&1 &`
   外加人手一行版：`sudo python3 $R/scripts/loadtest/mock_env.py --base-dir $B --restore`。
3. **生产告警预登记**：改写期间 `*/10` 探针（若 `YIBAN_PROBE_ENABLE=1`）与任何 443 出站会失败——这是**预期内的假告警**，值班人须先知晓，且"探针连续失败"本身列为本路径的中止触发器（§8）。
4. **残迹对账**：结束除 S7 外，另跑 `grep -c 'yiban-loadtest' /etc/hosts` == 0、`iptables -S OUTPUT | grep -c 'REJECT.*443'` == 基线值；Task 4 台账挂着一笔"半隔离残留"的幂等隐患（`mock_env.py:488` finally 门条件偏窄），故本路径**必须**以 --check 复核收尾：`sudo python3 $R/scripts/loadtest/mock_env.py --base-dir $B --check` 应全 FAIL（环境已还原的证据）。

## 7. 证据格式

演练证据**进当批记忆点文稿的"门禁证据"一节**（release-gate §7："每批在当批文稿里留一段门禁证据……没有这段证据，等同于没通过门禁"），本预案文档本身不存演练记录，只定格式。映射表：

| release-gate §1/§2 验收项 | 本预案对应证据 |
|---------------------------|----------------|
| §1 `server-web`①"非生产形态演练过且无问题（裸机+容器两形态，部署路径真跑）" | §4 S1–S7 与 §5 C1–C7 两张判据清单逐项 ✅ + 还原对账 diff 截图/文本 |
| §2④"裸机=生产同版本 Python" | S0/S1 的 `python3 --version` 与生产 `$APP_DIR` 实际解释器对照行 |
| §2④"容器两进程 RUNNING" | C4 三判据输出（urlopen 状态码、container_scheduler 进程行、RestartCount） |
| §2④"照抄 compose environment" | C4 命令原文（六键） |
| §3 轮次三要素（演练格式采证） | S6/C5 三行日志摘录（脱敏后） |
| §2② 压测判据 | **不在本预案范围**——生产机不跑压测规模（§3.3），容量实测仍按 release-gate §2② 在隔离形态做 |

当批文稿中演练小节模板（每条一栏，全脱敏）：

```
## 晋升演练（生产机隔离形态，预案：docs/dev/production-isolation-rehearsal-plan-20260925.md）
- 候选提交 <sha> / 版本 v<X.Y.Z>（yiban/__init__.py）
- 形态：裸机 netns / 容器 --internal；机制要点：<netns 无路由 | --internal 无 NAT>
- 时刻：开始 YYYY-MM-DD HH:MM —— 还原确认 HH:MM（北京时间，均在 09:00–17:00 段）
- 判据：rc / sign-status / 日志三行 / 记账对平 / 零外联探测 —— 逐项 ✅（摘录在附录）
- 还原对账：hosts/iptables/端口/cron/systemd NRestarts/netns/docker ps 七项 diff == 基线 ✅
- 异常与处置：<无 | 发生什么、按 §8 哪条中止、生产影响面复核结论>
```

原始产物（mock.jsonl、日志、快照）**不入 git**，留在 `$R/evidence/` 归档打包外存后随 `$R` 删除；文稿里只贴关键行摘录（手机号一律 `138****0000` 形态，对齐 release-gate §5 的"就地打码"要求）。**轮次台账警示**：演练轮不计入 §3 有效轮次，也不写入 §4 台账表（它要求"真实执行了签到"的**生产**轮），混记 = 污染 `main` 的计数证据。

## 8. 中止与事故预案

### 8.1 立即中止触发器（任一命中即停，不讨论"先跑完这轮"）

| # | 征兆 | 检测方式 |
|---|------|----------|
| A1 | 时间逼近硬底线（06:20 未还原 / 已进入 06:31–07:49） | 人肉看钟；建议 `at`/`systemd-run --on-active` 装自动还原定时器（§6-2 同款，推荐路径同样值得装） |
| A2 | 宿主全局面出现本预案未授权的改动：`/etc/hosts` md5 ≠ 基线、`iptables -S OUTPUT` diff 基线、`ss -ltnp` 多了监听 | S0 基线文件随手 `diff`（§4-S3/S7 命令即巡检命令） |
| A3 | **有请求绕过 mock**：`__stats` 记账 < 引擎应发数；或零外联探测 connect 成功；或 netns 内 `ip route` 非空 | S6 判据 4/5——命中即事故级（意味着真实易班可能收到了演练请求） |
| A4 | 生产侧出现演练痕迹：`/var/log/yiban` 目录清单与基线比对出**非生产语义**的增量、`yiban-web` NRestarts 变化、cron 表内容变化 | `ls -la` diff 基线（不读文件内容） |
| A5 | 演练进程失控（CPU 饱和、杀不掉、mock 无响应仍重试） | `top`/`docker stats`；生产机不是压测场，超时 15 分钟不收敛即 A5 |
| A6 | 兜底路径（§6）期间：生产探针发假告警 / 备份轮 / 证书续期失败 | 值班告警渠道 |

### 8.2 中止动作（按顺序，每步幂等）

```bash
# 1) 掐演练进程（先进程后命名空间：ip netns del 不会杀驻留进程，留着会成"无主 netns 进程"。
#    模式带 $R/ 前缀，绝不匹配到 /opt/yiban-auto-sign 下的生产进程）
pkill -f "$R/scripts/loadtest/mock_yiban.py"; pkill -f "$R/run.sh"; pkill -f "$R/scripts/signin.py"
docker rm -f yiban-rehearsal 2>/dev/null || true
# 2) 拆隔离面
ip netns del $NS; rm -rf /etc/netns/$NS; docker network rm yiban-rehearsal-net 2>/dev/null || true
# 3) 若用过全局机制
python3 $R/scripts/loadtest/mock_env.py --base-dir $B --restore   # 仅 §6 路径适用
# 4) 全套还原对账（S7/§6-4 命令）
```

### 8.3 中止后的生产未受影响验证（只读，全项留档为"事故复核"证据）

1. 下一个生产 cron 轮照常且成功：次日查 `/var/log/yiban/sign-status-<日期>.txt == SUCCESS`、三行日志、`grep -acE "ERROR|Traceback"` 与日常水位一致（命令照抄 release-gate §5，含打码 sed）；
2. 生产 web 无重启无掉监听：`systemctl show -p NRestarts yiban-web` == 基线；`ss -ltnp | grep 17892` 不变；
3. 网络面回原状：hosts md5 / iptables / netns / docker 四项 diff 基线 == 空；
4. 生产 DB 未被演练写：`/opt/yiban-auto-sign` 目录 mtime 面正常（只 stat 不读），备份轮 sha 链正常；
5. 若 A3 命中（疑似真实外联）：追加"真实上游影响评估"——演练库账号为合成假号，最坏情形是真实易班侧的失败登录尝试；记录时刻与出口 IP 供处置，并按项目风控口径核 `yiban/cred_state.py` 熔断/冷却是否被真实触发波及（生产账号与演练账号不同库不同凭据，正常无交集）。
6. 复盘写入当批文稿"异常与处置"栏（§7 模板末行）；演练轮计数若发生在生产同提交观察期内，**按 release-gate §3"计数清零"从严自问**：演练不触碰生产轮即不清零，存疑时按清零报，宁保守。

## 9. 已知缺口与待办

- **G1（本预案第一依据缺口）**：引擎无进程级 API base 覆盖（`yiban/fyiban/protocol.py:52-58` 常量直连、URL 固定 443），导致裸机演练必须借道 netns。**建议**（不是本批范围）：协议层加一个受 `YIBAN_UPSTREAM_*` 环境变量驱动的 host 映射注入点（同 `egress.py` 的"单口径"风格），落地后裸机路径可去掉 hosts 机制，仅留 CA env + 无代理。
- **G2**：代理通道（`client.py:140-142`）是现成的进程级出口改写，但 `mock_yiban.py` 无 CONNECT/MITM 形态，代理指 mock 这条路今天走不通；若 G1 落地则此路可废弃。
- **G3**：引擎 `requests.Session` 的 `trust_env=True` 无法按进程关闭（`isolation.py:loadtest_session` 的 `trust_env=False` 只管 loadtest 自建会话，docstring 自述"不触碰 yiban/ 引擎"）。本预案以"演练 shell 起手 `env | grep -i proxy` 必须为空"（S3）+ `isolation.strip_proxy` 同源纪律兜住；长期建议加 `YIBAN_TRUST_ENV=0` 显式键。
- **G4**：`ip netns exec` 对 `/etc/netns/<ns>/hosts` 的 over-mount 依赖 iproute2 行为，不同发行版/版本未逐一实测——操作单已用"双向核验"（S3 判据）把这一点从假设降级为每轮必查项。核验不过时**第一反应是复查 S3 的收敛顺序**（(a) 预写是否落实在任何 `ip netns exec` 之前、"写"与"验证"是否拆成了两个进程、`/etc/netns/$NS/hosts` 磁盘内容当下是否含标记块）——"自检永不过、标记块被 finally 剥掉"的表象与"over-mount 不生效"几乎不可分辨，直接跳 §6 会把**顺序误调用**成机器全局改写（生产机 hosts/iptables）的许可，正是本预案要防的误诊陷阱；§6 只属于它自己的前提（netns 与 docker 双双不可用），确有证据表明目标机 iproute2 无按文件 over-mount 语义时才进入 §6，并复盘记入当批"异常与处置"。
- **G5**：容器形态 `supervisord.conf` 无 `[unix_http_server]`/ctl 配置，`supervisorctl status` 不可用——C4 判据因此用 `docker top` + 回环探测替代；若日后要 supervisorctl 化，属容器基建，另立任务。
- **G6**：`mock_env.py:488` 的 finally 还原门条件偏窄（Task 4 台账已 defer："hosts_changed and …" 应含 `ipt_touched`），仅在 §6 兜底路径有残留风险，S7/§6-4 的 `--check` 复核是它的运行时补丁；代码修复随批次 1+。
- **G7**：本预案验证的是"部署路径 + 零真实外联"，**不**产生 release-gate §1 条件②（生产机真实签到一轮）与 §3 有效轮次——晋升 `server-web` 仍需演练之后单独完成真实轮取证，两份证据不可互替。
