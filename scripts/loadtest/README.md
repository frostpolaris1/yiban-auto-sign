# 压测 / 容量自动化（仅限测试机）

一套把「单进程资源占用 → 多少进程耗尽其资源 → 支持多少账号」跑清楚的工具。
**只允许在隔离的测试机上运行**：mock 通过自签证书 + `/etc/hosts` 把易班域名指向
本机回环，并用 iptables 兜底拒绝其余 443 出站，保证**零真实外联**。

> 安全红线
> 1. 绝不把本套工具指向真实易班生产环境；运行前先跑通 `mock_env.py --check` 自检。
> 2. 只能改测试机的 `/etc/hosts` / iptables / 自签证书目录；**结束后必须
>    `mock_env.py --restore`**（进程与监听一并停掉）。
> 3. 工具与 README 内不写任何真实凭据；运行产物（`results/`、证书、hosts 备份）
>    只留在测试机，不入库。

## 快速开始（三步）

假设测试机已 clone 本仓库到 `/opt/repo`，用 root 运行：

```bash
# 1) 一键环境：自签 CA/证书 + hosts 双栈改写 + 443 兜底 REJECT + 自检
python3 scripts/loadtest/mock_env.py --base-dir /opt/yiban-loadtest

# 2) 启动假易班（回环 127.0.0.1 / ::1 的 443；证书由上一步生成）
python3 scripts/loadtest/mock_yiban.py \
  --cert /opt/yiban-loadtest/ca/server.pem \
  --key  /opt/yiban-loadtest/ca/server.key \
  --log  /opt/yiban-loadtest/logs/mock.jsonl &

# 3) 造号 + 跑一轮（示例：60 账号 / 拟真 300ms / 窗口 420s）
python3 scripts/loadtest/seed_accounts.py --n 60 \
  --db  /opt/yiban-loadtest/data/yiban.db \
  --env /opt/yiban-loadtest/test.env --gap 10

python3 scripts/loadtest/scale_driver.py \
  --repo /opt/repo --env /opt/yiban-loadtest/test.env \
  --db   /opt/yiban-loadtest/data/yiban.db \
  --n 60 --label net300 --config-name net300 \
  --mock-config /opt/yiban-loadtest/mock_config.json --delay-ms 300 \
  --ca   /opt/yiban-loadtest/ca/ca.pem \
  --mock-log /opt/yiban-loadtest/logs/mock.jsonl \
  --window-sec 420 --outdir /opt/yiban-loadtest/results
```

结束后：

```bash
python3 scripts/loadtest/mock_env.py --base-dir /opt/yiban-loadtest --restore
python3 scripts/loadtest/mock_env.py --base-dir /opt/yiban-loadtest --check  # 自检应全 FAIL
```

周期 `S = 单账号耗时 t + 间隔 gap`；80 分钟生产窗口（有效窗口 4680s）容量 ≈ `4680 / S`。

## 参数表

### mock_yiban.py（假易班）
| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | IPv4 回环监听地址（默认仅回环） |
| `--port` | `443` | 监听端口 |
| `--ipv6-host` / `--no-ipv6` | `::1` / 关 | 是否同时监听 IPv6 回环 |
| `--cert` / `--key` | 空 | 服务器证书/私钥；未提供须显式 `--no-tls` |
| `--pubkey-file` | 内置测试公钥 | 登录页 `input#key` 内嵌的 RSA 公钥 PEM |
| `--delay-ms` | `0` | 每请求固定人工延迟 |
| `--tail-delay-ms` / `--tail-every` | `0` / `0` | 每 N 个请求追加一次尾延迟 |
| `--fail-rate` / `--fail-stage` | `0` / `none` | 失败注入概率与注入点（四类故障注入旋钮 `login`/`signIn`/`waf`/`nonjson`，另有 `signPosition` 与假成功档 `login-shallow`；**默认 none=全关，不开零变化**） |
| `--config` | 空 | 热读 JSON 配置（运行中切换档位，字段同上；场景声明形态的注入旋钮同样可经此热切；档位名须精确小写，热读通道不做大小写归一） |
| `--keep-alive` | 关 | 启用 HTTP keep-alive；默认关（每请求新连接，压测更稳、不触发偶发重试） |
| `--log` | 空 | 逐请求 JSONL 落盘路径 |
| `--ready-file` | 空 | 启动后写入实际端口，供驱动等待就绪 |

覆盖接口：`GET /code/html`、`POST /code/usersure`、`GET /iframe/index`、
`GET /base/c/auth/yiban`（登录链 4 步）、`GET .../iapp7463`（旧流程落地）、
`GET .../signPosition`、`POST .../signIn`（签到 2 步 + 探针入口），
以及 `GET /__stats`、`GET /__health`。

#### 假上游故障注入旋钮（`--fail-stage`，默认全关）

各类注入以**场景声明**形态落地（CLI `--fail-stage`，或经 `--config` 热读 JSON
运行中切换，无需重启；两者都只由 mock 进程消费，引擎无感知，可从 run.sh 全链
入口穿透）。旋钮值语义：

| 旋钮 | 注入点 | 形态 |
|---|---|---|
| `login` | `POST /code/usersure` | 账密错形态（`code != s200`），登录端点报失败 |
| `signIn` | `POST .../signIn` | 非登录阶段的签到提交失败（业务码失败，不抛异常） |
| `signPosition` | `GET .../signPosition` | 拉任务失败（历史档位） |
| `waf` | `GET /iapp7463` | ydclearance **挑战页**（形态对照 `yiban/fyiban/waf.py` 的 `looks_like_challenge` 真实输入：`window.onload=setTimeout`+`eval("qo=eval;qo(po);")` 双特征 + `Set-Cookie: https_ydclearance`），喂风控识别支路 |
| `nonjson` | JSON 期望端点（`POST /code/usersure`、`POST .../signIn`、`GET /base/c/auth/yiban`、`GET .../signPosition`） | `200` + >2000 字符非 JSON 拦截 HTML——现网 `Expecting value:` 的形态（长页过 `is_waf_blocked` 长度界后 `.json()` 抛） |
| `login-shallow` | `GET /base/c/auth/yiban`（仅带 `verifyRequest` 的完成认证步） | **假成功**：`code==0` 但签发回执 `data` 载荷缺失——只判 `code` 的旧登录门会误认成功并写会话缓存，带回执判据的客户端必须拒绝；旧流程入口步（不带 `verifyRequest`）不受影响 |

配合 `--fail-rate 1.0` 即确定性注入；`none`（默认）不改变任何响应。记账侧
`injected` 计数与 JSONL 逐条 `injected` 标记同步（`--log`），每请求可审计。

### mock_env.py（环境）
| 参数 | 默认 | 说明 |
|---|---|---|
| `--base-dir` | `/opt/yiban-loadtest` | 证书/备份落盘目录 |
| `--hosts-file` | `/etc/hosts` | hosts 路径 |
| `--domains` | 易班所需域名 | 需映射到回环的域名（逗号分隔） |
| `--restore` | 关 | 还原 hosts + iptables（幂等） |
| `--no-ipv6` | 关 | 跳过 IPv6 处理 |
| `--no-iptables` | 关 | 跳过 443 出站兜底（**隔离降级**，须配 `--i-understand-no-isolation`） |
| `--i-understand-no-isolation` | 关 | 显式知情并接受 `--no-iptables` 的隔离降级，否则拒绝执行 |
| `--force` | 关 | 强制重签证书 |
| `--dry-run` | 关 | 只打印将执行的操作，不改系统 |
| `--check` | 关 | 只做自检（解析是否全为回环 + 兜底规则是否存在） |
| `--egress-probe-ip` | 空 | **搭建路径必填**：主动探测该 IP:443 应被拒绝（缺省即拒绝启动） |

幂等性：重复 setup 不重复改 hosts/iptables；重复 `--restore` 无副作用。
宿主警示：WSL 会由系统生成器**运行期自动重写 /etc/hosts 并清空 iptables**（实测）——
全局形态在此类宿主上不持久，长链演练用 netns 裸机形态（见批 0 演练预案 §4）或
分步快跑，setup 后先 `--check` 再起链。

证书扩展（E1）：`ensure_certs` 生成的自签 CA 带 `basicConstraints(critical,CA:TRUE)`
与 `keyUsage(critical,keyCertSign,cRLSign)`——Python ≥3.14 默认开 `VERIFY_X509_STRICT`，
缺扩展会在 TLS 握手层整轮全灭且 **mock 记账为 0**（失败安静，极易误诊为隔离问题）。
幂等跳过分支会探测存量 CA 扩展，缺则自动重签；也可手工 `--force`。演练预案里的手工
openssl 重签路径与本代码路径并存、口径一致。版本下限：`openssl req -addext` 需
openssl ≥ 1.1.1，更老的发行版会在 CA 生成处直接报错退出（不会产出无扩展 CA 后静默失败）。

启动即断言（fail-closed，见 `isolation.py`）：四个入口（`mock_env`/`scale_driver`/
`concurrency_probe`/`capacity_probe`）在解析参数前即要求**进程环境无任何 `*PROXY*` 键**
（有则非零退出、原因打到 stderr），子进程环境构造处也会主动摘除代理键；`mock_env` 搭建
另要求 `--egress-probe-ip` 非空（不再静默 `[SKIP]`）。出站兜底的 443 REJECT 与回环 ACCEPT
一样用 `-I` 前插到链首（ACCEPT 占 1、REJECT 占 2），不再 `-A` 追加链尾被既有放行规则旁路。
`capacity_probe` 收尾会比对「mock 侧记账条数 == 驱动读到的 JSONL 条数」，不等则本轮不出结论。

### seed_accounts.py（造号）
| 参数 | 默认 | 说明 |
|---|---|---|
| `--n` | 必填 | 账号数（手机号 `131…` 连续递增） |
| `--db` / `--env` | 必填 | 测试库 / 测试 .env |
| `--gap` | `10` | 账号间隔（写入 `YIBAN_ACCOUNT_GAP_MAX`） |
| `--window-start` / `--window-end` | `06:30` / `07:50` | 签到窗口 |
| `--order` / `--dist` | `sequence` / `uniform` | 调度顺序/分布 |
| `--avg-attempt-sec` | `2` | 容量公式用的平均耗时 |
| `--no-wipe` | 关 | 追加造数（默认先清空账号表） |

### scale_driver.py（单进程驱动）
| 参数 | 默认 | 说明 |
|---|---|---|
| `--repo` / `--env` / `--db` | 必填 | 被测仓库 / 测试 .env / 测试库 |
| `--n` | 必填 | 本轮账号数 |
| `--label` / `--config-name` | `run` / `custom` | 结果文件标签 / 配置档名（CSV 去重键） |
| `--gap` | `10` | 账号间隔 |
| `--window-sec` | `0` | `>0` 时按该秒数生成今天窗口；`0` 沿用 .env |
| `--mock-config` | 空 | mock 热读配置路径；传入则按 `--delay-ms/--tail-*/--fail-*` 写入 |
| `--delay-ms` / `--tail-delay-ms` / `--tail-every` | `0` | 写入 mock 的延迟档 |
| `--fail-rate` / `--fail-stage` | `0` / `none` | 写入 mock 的失败注入档 |
| `--ca` / `--mock-log` | 必填 | mock CA / JSONL 路径（解析周期与并发） |
| `--outdir` | `.` | 结果目录 |
| `--timeout` | `3600` | 单轮超时 |

### concurrency_probe.py（并发探测）
| 参数 | 默认 | 说明 |
|---|---|---|
| `--repo` / `--env` / `--db` / `--ca` | 必填 | 同驱动 |
| `--k-list` | `1,2,4,8,12,16,20,24` | K 阶梯 |
| `--per-proc` | `8` | 每个进程处理的账号数（各进程等量，便于比较） |
| `--gap` | `0` | 账号间隔；`0` = 压满 CPU 口径 |
| `--mock-log` | 空 | 传入则统计运行窗口内的 mock 最大并发 |
| `--mock-pid` | `0` | mock 进程 PID；用于单独统计 mock 自身 CPU（把服务端开销从整机 CPU 里剥离） |
| `--mem-reserve-mb` | `180` | 可用内存低于该值判定内存饱和并停止升档 |
| `--db-microbench` | 关 | 每档前跑 scratch 库写并发微基准 |
| `--interval` / `--timeout` | `0.3` / `600` | 采样间隔 / 单档超时 |

## 结果文件格式

统一落 `<outdir>/`：

- `run-<label>-n<N>.json`（驱动）：字段名稳定，跨版本可直接对比；键的权威清单是
  `scale_driver.py` 里 `main()` 组装的那个 result 字典。关键字段：
  - `cycle_stats`：`{n, avg, min, p50, p95, max}`，单位秒；周期 = 相邻两次
    `GET /code/html` 起点差（= t + gap）。
  - `request_time_stats`：同形状；样本是"单账号 6 次请求耗时之和"（纯网络 t）。
  - `max_inflight_in_run` / `inflight_ge2_records`：运行窗口内 mock 侧观测的最大并发
    与并发 ≥ 2 的记录数（单进程应恒为 1，是多 worker 串行/并行的直接证据）。
  - `status_counts` / `state_entries` / `completed` / `success` / `failed` / `skipped`：结果口径。
  - `requests_run` / `requests_per_acct`：请求总数与每账号请求数。
  - `cpu_s` / `cpu_pct_1core` / `peak_rss_kb` / `peak_rss_mb` / `threads_max` / `db_delta_kb`。
  - `window` / `window_eff_s` / `finish_window` / `rc` / `created_at`。
  - `cap80_implied = int(4680 / cycle_min)`：换算到 80 分钟生产窗口的容量。
- `concurrency-<label>.json`（探测）：**与驱动不是同一套字段**，上面那批键里只有
  `status_counts` / `completed` / `success` / `failed` 在此出现；`cycle_stats`、
  `request_time_stats`、`max_inflight_in_run`、`cpu_s`、`cap80_implied` 本脚本一个都不产出。
  顶层是 `{label, per_proc, gap, ncpu, mem_total_mb, mem_reserve_mb, rows, verdict, created_at}`，
  每档实测放在 `rows[]`：`K` / `per_proc` / `accounts_total` / `wall_s` / `machine_cpu_pct` /
  `engine_cpu_s` / `engine_cpu_onecore_pct` / `engine_cpu_machine_pct` / `mock_cpu_s` /
  `mock_cpu_onecore_pct` / `peak_total_rss_mb` / `median_proc_rss_mb` / `min_available_mb` /
  `db_delta_kb` / `lock_errors` / `lock_hits` / `oom_killed` / `mem_abort` / `proc_wall_avg_s` /
  `proc_wall_max_s` / `per_acct_wall_s` / `degradation_x` / `throughput_acct_per_h` /
  `max_inflight` / `mock_records` / `rcs`。
- `results.csv`（驱动，固定追加到 `<outdir>/results.csv`）/ `concurrency-<label>.csv`（探测）：
  列名固定，见 `scale_driver.CSV_FIELDS` / `concurrency_probe.CSV_FIELDS`，分别按
  `(label, config)` 与 `K` 定位。**这两份不是上面 JSON 的原样扁平化**：驱动的
  `cycle_stats` 在 CSV 里叫 `cycle_avg_s` / `cycle_p50_s` / `cycle_p95_s` / `cycle_min_s` /
  `cycle_max_s`，`request_time_stats` 只留下 `t_avg_s` / `t_p95_s` 两列；探测侧
  `engine_cpu_s` / `mock_cpu_s` / `mock_records` / `lock_hits` / `rcs` / `mem_abort` 只进 JSON、
  不进 CSV，而 `db_write_p50_ms` / `db_write_p95_ms` / `db_write_max_ms` / `db_write_errors`
  仅在该档开了 `--db-microbench` 时有值（关档写 None）。
- 产出里**没有**下面这几项，它们是测量缺口、不是文档漏写：驱动不把写进 mock 的
  `--delay-ms` / `--fail-rate` 档位回显到 JSON 或 CSV（只落在 `--mock-config` 那个文件里）；
  探测侧没有 `requests_per_acct`；探测的每账号耗时只有 avg / max，没有账号级 p50 / p95。
- 每个 K 的运行目录 `krun-<label>-k<K>/`：各进程独立 `state_p<i>/` 与
  `sign_p<i>.log`（失败现场）。

探测 JSON 另含 `verdict`：

```json
{"cpu_sat_k": null, "mem_sat_k": 20, "db_sat_k": null, "first_bottleneck": [20, "内存"]}
```

判定口径（`classify_bottlenecks`）：
- CPU：整机 CPU ≥ 90%（2 vCPU 即约 1.8 核）或引擎单核 CPU ≥ 180%；
- 内存：峰值总 RSS + 基线占用后 `MemAvailable < --mem-reserve-mb`，或子进程被 OOM(-9)；
- DB 写：日志出现 `database is locked/busy` 类错误，或 scratch 写 p95 ≥ 500ms。

经验：真实网络延迟（每请求数百毫秒）下，引擎 CPU 主要花在等待上，**内存通常先于 CPU 饱和**；
只有把延迟压到几十毫秒、请求背靠背时 CPU 才会先撞顶。DB 写入（WAL 单写者 + busy_timeout）
在几十个 worker 量级一般最后才成为瓶颈，表现为写事务排队 p95 上升而非报错。

## 换算到其他规格机器

工具只做「实测 + 线性外推」，换算遵循：

1. **单进程常量**（与机器无关的部分）：`t`（网络耗时，由 mock 延迟与真实链路决定）、
   `cpu_s_per_acct`（每账号 CPU 秒，实测 `cpu_s / 完成账号数`）、`rss_mb_per_proc`。
2. **CPU 上限**：`K_cpu ≈ vCPU × 0.9 × 单进程 wall / cpu_s_per_acct`；
   等价于 `K_cpu ≈ 0.9 × vCPU / (cpu_s_per_acct / wall)`。
3. **内存上限**：`K_mem ≈ 可用内存_MB / rss_mb_per_proc`（预留系统与 mock 占用）。
4. **实际可并发 worker 数** `K = min(K_cpu, K_mem)`，再取 `×2/3` 保守。
5. **单 worker 容量**（窗口 W 秒、间隔 gap）：
   `C1 = (W − t) / (t + gap) + 1`（与 `web/app.py:_capacity_estimate` 同形，
   注意其 `avg` 默认 8s 会系统性低估，压测应以实测 `t` 回填）。
6. **总可支持账号** `≈ K × C1`，保守上限 `≈ K × C1 × 2/3`。

把第 2、3 步里的 `vCPU` / `可用内存` 换成目标机器实测值即可；`t` 与
`rss_mb_per_proc` 若目标机语言/版本相同可沿用，否则在目标机重跑
`scale_driver.py --n 30` 重新标定。

## 自测

```bash
python -m pytest tests/test_loadtest_tools.py -q     # 秒级，21 passed
```

端到端（真实 signin 进程 + TLS + hosts，仅测试机 root）默认跳过；
设置 `YIBAN_LOADTEST_E2E=1` 及 `YIBAN_LOADTEST_{REPO,ENV,DB,CA,MOCK_LOG}` 后执行。
