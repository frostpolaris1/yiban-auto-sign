> [← 返回 README](../../README.md)

## 配置说明

> **优先级口径（工单 71z7，2026-10-20 裁定）**：
> - **非路径键：`.env` 优先，进程环境只补缺**（M27）。`run.sh` / `run_probe.sh` /
>   `scripts/yiban-fallback.sh` / 容器调度器 / 引擎 `build_child_env` 一律按这一条判。
> - **四个路径键例外：进程环境优先，`.env` 只补缺**——
>   `YIBAN_DB_FILE` / `YIBAN_STATE_DIR` / `YIBAN_LOG_FILE`（与 `env_io.resolve_path`
>   同口径）。测试/CI 用进程环境重定向数据目录时，不被 `.env` 里的旧路径拉回。
>   指针 `YIBAN_ENV_FILE` 是**两段自举**：进程环境 → 基线 `.env`（默认 `data/.env`）
>   → 默认值；即基线 `.env` 可以指向另一份 `.env`（web 与容器调度器都认这条）。
>   三个 bash 入口（`run.sh` / `run_probe.sh` / `yiban-fallback.sh`）固定
>   `$APP_DIR/data/.env`，因为它们必须在 venv 损坏时也能定位。
> 于是：
> - **非路径键**：docker-compose 的 `environment:` 注入会被 `/data/.env` 的同名键覆盖；
>   cron / systemd 行上的 `YIBAN_XXX=v` 前缀同样会被 `.env` 覆盖。
> - **路径键**：compose / cron / systemd 的同名值**优先**，`.env` 只在没写该键时补缺。
> - `.env` 里**没有**的键才由进程环境补缺；想让非路径项走环境变量，正确做法是**别在 `.env` 里写它**。
> - 要临时改一项非路径配置，改 `.env`（网页设置页写的也是 `.env`），别指望命令行或 compose 覆盖——
>   口径分叉时会出现"页面显示关闭、排程却照跑"这种两处同时为真的状态，排查时没人说得清算哪个生效。
>
> **另一例外**：`YIBAN_ACCOUNTS_KEY` 的取钥判据在 `yiban/adapters/config/account_crypto.py`，
> 历来是**环境变量档优先**且与 `.env` 同键做分叉断言，不一致**拒绝启动**（fail-closed），
> 本次不改（改它会打穿 systemd EnvironmentFile 形态的现网换钥流程）。

<details>
<summary>⚙️ 展开：环境变量一览 / 账号间隔与容量 / 账号 JSON / 消息通知 / 代理 / 设备绑定 / 探针</summary>

### 环境变量一览

账号默认存 **SQLite（`yiban.db`）**，由网页后台写入（AES-GCM 加密）。下表只列用得上的键；**完整键位与逐键说明见仓库根 `.env.example`**（它本身就是配置模板）。

| 变量名 | 说明 | 必填 |
|--------|------|------|
| `YIBAN_ACCOUNTS_JSON` | 账号 JSON 数组（推荐用于无头环境/CI，格式见下） | 二选一 |
| `YIBAN_ACCOUNTS` / `YIBAN_PHONE`+`YIBAN_PASSWORD` | 旧格式（`手机号:密码`，多账号 `#` 分隔；单账号两键）向后兼容 | 二选一 |
| `YIBAN_ACCOUNT_GAP_MAX` | 账号间隔：相邻两次签到请求的最小间隔秒数，自动与手动签到均生效；默认 `10`，`0`=关闭 | 可选 |
| `YIBAN_SIGN_START` / `YIBAN_SIGN_END` | 签到窗口（`HH:MM`，默认 `06:30` / `07:50`） | 可选 |
| `YIBAN_WINDOW_EDGE_FRONT_SEC` / `_BACK_SEC` | 窗口首尾裁剪秒数（各默认 `60`，`0`~`300` 且 30 的倍数）；有效窗口 = 两端裁剪后的区间。旧键 `YIBAN_WINDOW_EDGE_SEC`（前后对称）仍兼容 | 可选 |
| `YIBAN_SIGN_ORDER` / `YIBAN_SIGN_DIST` | 排序 `sequence`（默认）/`random`；分布 `front`（默认，提前铺完）/`uniform`（均匀铺满整窗）/`normal`（钟形高峰） | 可选 |
| `YIBAN_BLOCK_CAP` | 错峰分块容量（每块最多人数，默认 `15`） | 可选 |
| `YIBAN_SECOND_RUN_TIME` | 补签轮触发点，默认 `07:12`；**须与补签 cron 时刻一致**（见服务器部署第 6 步） | 可选 |
| `YIBAN_SUNDAY_SIGN` / `YIBAN_SATURDAY_SIGN` | `1`=当天也执行；缺省/`0`=跳过（两个默认都跳过） | 可选 |
| `YIBAN_ALLOW_TIME_PREF` | 用户自选时间片总开关：`1`=开启（默认关） | 可选 |
| `YIBAN_MAX_USERS` / `YIBAN_MAX_ACCOUNTS` | 容量上限（默认 `500` 用户 / `200` 账号；`0`=不限）。调小不删存量，只限制新增；主管理员可在「系统设置 → 容量配额」直接设置 | 可选 |
| `YIBAN_BATCH_SIGN_COOLDOWN_SEC` | 手动签到全局冷却秒数（默认 `60`，`0`=关闭）：批量与单条共用；30 秒同账号防抖与此独立 | 可选 |
| `YIBAN_SIGNIN_RATE_WINDOW_SEC` / `YIBAN_SIGNIN_RATE_MAX` | 手动签到全局速率上限（默认 `600` 秒内最多 `10` 次，任一为 `0`=关闭）：冷却是两次触发之间的最小间隔，这是窗口内总次数；按会话用户名计数，只在冷却放行后计数，超限 429（文案「手动签到触发过于频繁」） | 可选 |
| `YIBAN_ADMIN_DELETE_MAX` / `YIBAN_ADMIN_DELETE_COOLDOWN_SEC` | 同管理员窗口内的**删除类**高危操作额度（默认 `20` 次 / `60` 秒，任一为 `0`=关闭）：账号软删/批量删除/彻底清除/清库清理，超限 429 | 可选 |
| `YIBAN_ADMIN_CREDS_MAX` / `YIBAN_ADMIN_CREDS_COOLDOWN_SEC` | 同管理员窗口内的**凭据改写类**高危操作额度（默认 `20` 次 / `60` 秒，任一为 `0`=关闭）：改写他人易班凭据/重置他人口令/换推送密钥，与删除类分开计数（批量重绑与批量清理互不撞 429） | 可选 |
| `YIBAN_LOGINFAIL_DAILY_MAX` | 登录失败告警独立推送日额度（默认 `3`，`0`=不限），与普通/紧急告警额度分账 | 可选 |
| `YIBAN_SLOW_SIGN_SEC` | 单次签到耗时告警阈值（秒，默认 `30`） | 可选 |
| `YIBAN_NOTIFY_TYPE` + `YIBAN_NOTIFY_SECRET_ENC` | 消息推送类型（`serverchan` / `custom`）与密钥密文；建议在网页「系统设置 → 通知通道」配置（自动加密落盘）。旧明文 `YIBAN_NOTIFY_URL` 仍兼容（按 `custom` 处理） | 可选 |
| `YIBAN_MAIL_ENABLE` / `YIBAN_MAIL_SMTP_HOST` / `_PORT` / `YIBAN_MAIL_USER` / `YIBAN_MAIL_PASS` / `YIBAN_MAIL_ADMIN_TO` | 邮件通知（默认关闭）；发件列表推荐在网页配置，`.env` 方式作为首个发件条目生效 | 可选 |
| `YIBAN_PROXY` | 代理地址（`http://host:port`、`socks5://host:port` 或带认证 `http://user:pass@host:port`） | 可选 |
| `YIBAN_PHONE_MODEL` / `YIBAN_PHONE_CODE` | 设备型号与唯一识别码；账号未单独配置时全局回退（见 [设备绑定](#设备绑定可选)） | 视情况 |
| `YIBAN_PROBE_ENABLE` / `YIBAN_PROBE_TIME` / `YIBAN_PROBE_INTERVAL_DAYS` / `YIBAN_ACCOUNT_VERIFY` / `YIBAN_VERIFY_ASYNC` | 健康探针与注册时校验（默认全关，见 [账号健康检查](#账号健康检查探针模式可选)） | 可选 |
| `YIBAN_HEALTH_REPORT_TIME` / `YIBAN_HEALTH_REPORT_WEEKDAY` | 告警通道健康报告的例行发送时刻（`HH:MM`）与星期（`0`=周一…`6`=周日）；都不配置 = 沿用「周一 + 清理线程唤醒即发」（见 [告警通道健康报告发送时刻](#告警通道健康报告发送时刻可选)） | 可选 |
| `YIBAN_LEGACY_LOGIN` | 设为 `1` 使用旧登录流程（伪造 iOS UA）；默认用真实 App 特征（推荐） | 可选 |
| `YIBAN_WORKERS` / `YIBAN_PROXY_LIST` / `YIBAN_PROXY_FALLBACK` / `YIBAN_FALLBACK_ENABLE` / `YIBAN_FALLBACK_INTERVAL` / `YIBAN_CAPACITY_MEASURED` | 多执行体相关（单执行体部署**不需要**配置），见 [多执行体并行签到](#多执行体并行签到可选) 与 [代理配置](#代理配置可选) | 可选 |
| `YIBAN_ADMIN_USER` / `YIBAN_ADMIN_PASSWORD_HASH` / `YIBAN_ADMIN_PW_VERSION` | 内置主管理员账号与 scrypt 哈希（由网页 `/setup` 引导或 `CLI init` 写入，不手填） | 自动 |
| `YIBAN_ADMIN_PASSWORD` | 旧版明文口令键：仅用于兼容既有部署；`/setup` 或 `CLI init` 成功后会被删除。新部署不要手填 | 旧部署可选 |
| `YIBAN_PW_GATE` | 危险操作（改他人凭据 / 物理清除 / 删用户 / 重置他人口令 / 改角色 / 改告警通道 / 破坏性设置 / 发公告 / 执行体写 / 急停）要口令的档位：`risk`（默认，仅换环境才要——本次出口 IP 与本会话已验证 IP 不一致，无记录视为未知不触发；验证通过即记住该出口）/ `full`（每个操作都当次要口令）/ `off`（永不要求）。非 `full` 档下不可逆操作还要求请求体带 `confirm_delay_ack`，改他人凭据 / 清空用户账号 / 执行体写三类在操作成功后补一封管理员告警（其余管理操作只留审计）；门禁失败告警与档位无关：任何实际要求了口令并失败的路径都发；非法值回退 `risk` | 可选 |
| `YIBAN_ACCOUNTS_KEY` / `YIBAN_AUDIT_KEY` / `YIBAN_TRACK_SALT` | 账号密文密钥 / 审计链 HMAC 密钥 / 访问统计盐：**按需自动生成**并写入 `.env`（分别在首次加解密账号、首次写入审计行、首次记录访问时），一般无需手填。注意：刚装好还没加过账号时，`​.env` 里可能只有管理员哈希与会话密钥——别把这一刻的 `.env` 当成「密钥齐全」归档 | 自动 |
| `YIBAN_STATE_DIR` / `YIBAN_LOG_FILE` | 状态文件目录（默认 `/var/log/yiban`）与日志路径（按天分文件） | 可选 |
| `YIBAN_DB_FILE` / `YIBAN_ENV_FILE` | 数据库与 `.env` 路径（默认相对检出根：`data/yiban.db` / `data/.env`） | 可选 |
| `YIBAN_BASE_PATH` | Web 挂载前缀，仅在自动识别切错时兜底（见 [部署形态](#部署形态)） | 可选 |

> 调度 v2 的其余内部参数（正态 μ/σ 范围、重试最小间隔等）见代码 `yiban/engine/schedule.py` 的 `_schedule_config()`，网页不展示的项一般无需调整。
>
> 「分布」默认是 `front`（提前铺完）：计划按安全速率铺进窗口前段，尾部留作重试与兜底。**想改回铺满整窗**：在 `.env` 写入 `YIBAN_SIGN_DIST=uniform`（或在「系统设置 → 签到调度」把分布选为「均匀分布」），保存后下次触发即生效。

### 账号间隔（防风控）与容量预估

账号间隔是相邻两次签到请求之间的最小间隔（秒），用于降低同一 IP 连续登录触发风控的概率，是 [Q1](#q1-账号或密码错误e003)（真实 App 登录特征）之外的纵深防御。**默认 10 秒**，`0`=关闭；自动签到与手动签到均生效。两种调整方式：

1. **网页「系统设置 → 签到调度」**（推荐）：改「账号间隔」后保存（需主管理员密码确认）；
2. **手动编辑 `.env`**：`YIBAN_ACCOUNT_GAP_MAX=10`，保存即可，下次触发自动生效。

**容量预估**（「数据总览 → 账号与用户容量」卡，按当前签到窗口与账号间隔计算）：

```
账号容量 = floor((有效窗口秒数 − 单账号耗时) ÷ (单账号耗时 + 账号间隔)) + 1
```

- **有效窗口** = 原始窗口 − 前后裁剪秒数（被裁掉的秒数不参与签到、不占容量）；单账号耗时默认按 **3 秒**估算，真实网络更慢时用 `YIBAN_AVG_ATTEMPT_SEC` 覆盖。
- **占用口径 = 会发起签到的账号数**（判据 `yiban.adapters.persistence.accounts.signs_in`）：非删除、且审核态已通过（`user_paused` 仍计入，未审核/已拒绝的账号永不签到，不计入）。
- **超容量仅警示**：容量卡标红提示，不阻断任何操作。
- **容量上限**是另一件事：`YIBAN_MAX_USERS`（全部未删除注册用户）管归属注册，`YIBAN_MAX_ACCOUNTS`（上述会签到的账号数）管账号存量；调小上限不删存量，只限制新增。

### 账号配置格式（JSON，兼容旧格式 / CI）

`YIBAN_ACCOUNTS_JSON` 是一个 JSON 数组，一个账号一次输入完整信息，**无需用符号分隔**（网页后台写入的数据库账号不依赖此格式）：

```json
[
  {"phone": "13800138000", "password": "你的密码", "phone_model": "Vivo-XXXX", "phone_code": "64位识别码"},
  {"phone": "13900139000", "password": "另一个密码"}
]
```

- `phone` / `password` 必填；`phone_model` / `phone_code` 可选（学校开启设备绑定时必填，每个账号可独立配置）；
- 同一手机号重复出现会按号码去重并告警；
- 检查配置（不发送任何请求）：`.venv/bin/python3 scripts/signin.py --check-config` 或 `.venv/bin/python3 -m yiban.entry.cli config`。

> ⚠️ 数据库里的 `password` / `phone_code` 是 **AES-GCM 密文对象**。解密密钥 `YIBAN_ACCOUNTS_KEY` 自动生成在 `.env`（chmod 600）：**密钥丢失 = 已加密账号密码不可恢复**，备份数据时必须连同 `.env` 一起备份（建议与数据分开放、分开打包）。生产环境可用 `/etc/yiban/accounts-key` 分盘存放（见 `web/deploy/yiban-web.service`）。

### 消息通知（可选）

签到失败会自动推送。渠道分两类（`YIBAN_NOTIFY_TYPE`），建议直接在网页「系统设置 → 通知通道」配置（密钥自动加密落盘，不回显）：

<details>
<summary>📲 serverchan（Server 酱）</summary>

1. 访问 [https://sct.ftqq.com/](https://sct.ftqq.com/) 注册并获取 SendKey；
2. 类型选 `serverchan`，密钥填 SendKey（`YIBAN_NOTIFY_URL` 的旧写法为 `https://sctapi.ftqq.com/YOUR_SENDKEY.send`）。
</details>

<details>
<summary>📲 custom（Bark / 企业微信群机器人等完整 webhook 地址）</summary>

- Bark：`https://api.day.app/YOUR_KEY/易班签到通知`
- 企业微信群机器人：群设置 → 群机器人 → 添加机器人，复制 webhook `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY`

类型选 `custom`，密钥填完整地址（旧写法直接填 `YIBAN_NOTIFY_URL`）。
</details>

**默认额度与节流**（网页可改，`0`=不限）：同类告警节流 60 秒；非紧急告警每日 5 条；紧急告警每日 3 条；登录失败告警单独记账（每日 3 条）——分账是为了避免暴力破解类告警烧完当日额度后，审计链异常这类真告警在手机端被静默吞掉。**「仅推送重要告警」默认开启**（`YIBAN_NOTIFY_URGENT_ONLY`）：只推安全与系统级告警，用户日常改密、签到结果类仅走邮件，把有限的推送日额度留给真故障。

**告警面**：管理操作（改设置 / 删用户 / 重置他人口令 / 改角色 / 软删 / 彻底删账号 / 通道变更与收件人摘除）**不再逐条外发**，只留审计行（「日志」页可查，链式防篡改）；签到失败、审核拒绝信、以及非 `full` 档下改他人凭据 / 清空用户账号 / 执行体写三类事后告警照发；单账号耗时与容量超载只进收尾汇总信正文（不即时推送），登录失败告警达 10 次与锁定同时发生。

**审计行里的「操作者」是遮罩，不是身份（M32）**：`actor` 列存的是邮箱遮罩（如 `z***@example.com`）或非邮箱标识（`admin` / `system`）。**遮罩是线索层、不是身份层**——同一域同前缀的多个用户/管理员（例如 `zhangsan@` 与 `zhangsan01@`）在遮罩后可能长得一样，**单看这一列不能断定是谁做的**。需要定性时配合同一行里的业务行（同事务写入的目标对象/手机号）与作用域标记（行内带请求作用域的记号，用来把一次操作里的一批行归到同一请求）一起看。这条口径是刻意保留的：改成不遮罩会让审计表本身变成一份可枚举的用户目录，而历史行与新行混排会让按操作者聚合彻底失效。

**「仅推送重要告警」开启时（默认）只走邮件的告警**（它们未标 `urgent`，邮件通道不受影响）：改密 / 注销 / 恢复三处的密码校验失败告警、注册用户自助改密的账号安全事件告警、新账号申请待审核告警、公告草稿变更告警。需要这些也推手机时，把「仅推送重要告警」关掉（`YIBAN_NOTIFY_URGENT_ONLY=0`）。

<details>
<summary>📧 邮箱通知（SMTP）——管理员告警 + 用户签到失败提醒</summary>

邮箱通知与 Webhook **并存**，各配各的：

- **管理员告警邮件**：签到失败、登录连续失败锁定、账号耗时与容量超载等，**签到轮彻底结束后合并成一封**「易班签到汇总」邮件（Webhook 对签到失败与通道降级仍即时逐条推送；耗时/容量超载只进汇总）；备份缺失、备份脚本漂移与同一作业重复触发另由每日 08:05 的哨兵发一封（见「运维 → 备份与恢复」）；
- **用户签到失败提醒**：普通用户自己账号签到最终失败时，发给其注册邮箱（成功不打扰，每账号每天最多 1 封，额度只按发送成功计）；用户可在「我的账号」自行关闭（默认开启）。

配置：网页「系统设置 → 通知通道 → 邮件通知 → 发件 SMTP 列表」可加多个发件邮箱按顺序主备切换（保存即时生效，无需改文件重启）。未在网页配置过列表时，`.env` 方式作为首个（主）发件条目生效：

```
YIBAN_MAIL_ENABLE=1                  # 1=开启邮箱通知（默认关闭）
YIBAN_MAIL_SMTP_HOST=smtp.qq.com     # 其它邮箱按服务商 SMTP 填写
YIBAN_MAIL_SMTP_PORT=465             # 465 走 SSL；其它端口自动 STARTTLS
YIBAN_MAIL_USER=你的发件邮箱@qq.com
YIBAN_MAIL_PASS=你的QQ邮箱授权码      # 敏感凭据：只填服务器本地 .env
YIBAN_MAIL_ADMIN_TO=管理员收件邮箱@qq.com  # 逗号分隔支持多个
```

> ⚠️ `YIBAN_MAIL_PASS` 是**授权码**而非邮箱登录密码；属敏感凭据，只写入服务器本地 `.env`（已被 `.gitignore` 排除）。不配置邮箱通知时，Webhook 通知不受任何影响。

> ❄️ 「保存即时生效」的前提（0.5.1 起的统一口径）：**非路径配置以 `.env` 优先，进程环境只补缺**——网页里保存的新值总会赢，环境变量里同名旧键被 `.env` 覆盖。四个路径键（`YIBAN_DB_FILE` / `YIBAN_STATE_DIR` / `YIBAN_LOG_FILE`）例外：进程环境优先。另有一个例外是 `YIBAN_ACCOUNTS_KEY`（账号密钥历来环境变量档优先，且与 `.env` 同键分叉时拒绝启动）。因此 systemd 部署把整份 `.env` 拷成 `EnvironmentFile`（历史装法）对非路径键虽不再压住新值，但仍属多余且易漂移——EnvironmentFile 按「网页管理后台」装法只放 `YIBAN_ACCOUNTS_KEY` 一行即可。

</details>

### 代理配置（可选）

| 变量名 | 作用 | 示例 |
|--------|------|------|
| `YIBAN_PROXY` | 单执行体（默认形态）走这个出口 | `http://user:pass@host:port` |
| `YIBAN_PROXY_LIST` | **并行执行体各用一个**（见 [多执行体并行签到](#多执行体并行签到可选)），逗号分隔 | `http://a:1,http://b:2,,http://d:4` |
| `YIBAN_PROXY_FALLBACK` | 兜底常驻执行体专用出口（不填则用 `YIBAN_PROXY`） | `http://fb:8080` |

支持 `http://`、`https://`；带认证写成 `http://user:pass@host:port`。

**留空就是走本机出口（直连）**：`YIBAN_PROXY_LIST` 里**空位表示"这个执行体直连"**；出口数少于执行体数时循环取用（3 个出口 + 5 个执行体 → 第 4、5 个复用第 1、2 个）。

⚠️ 三点提醒：

1. 代理地址里的账号密码**不会**回显到网页或日志（只显示 `http://主机:端口`）；
2. 网页里改出口配置后**下一轮定时任务/容器重启才生效**；
3. 仅在网络出口被风控或需要分散请求来源时按需配置；使用代理请遵守相关法律法规与平台条款。

### 设备绑定（可选）

部分学校在校本化后台开启了「设备绑定」，签到时会校验设备型号与唯一识别码，不匹配返回「请使用授权设备进行签到」。需要配置：

| 变量名 | 说明 |
|--------|------|
| `YIBAN_PHONE_MODEL` | 设备型号（App 上报的机型名，原样填写） |
| `YIBAN_PHONE_CODE` | 设备唯一识别码（由 App 生成并上报，原样填写） |

**如何获取**：在易班 App 的校本化签到页面，用工具页面读出 App 通过 JS 桥接暴露的设备信息（桥动作 `yiban_device` 的回包含 `appVersion`、`deviceModel` 等字段；App 侧另有 `getUUID()` / `getDeviceInfo()` 供页面调用）：回包中的机型名 → `YIBAN_PHONE_MODEL`；设备唯一识别码 → `YIBAN_PHONE_CODE`。

> ⚠️ **原样复制，不要手工构造或改动**：这两个值的长度与字符集由易班服务端解释，本项目不做假设。改动任何一个都会导致设备绑定校验失败。
>
> ⚠️ 不要把**第三方反欺诈 SDK**（例如易班内集成的数美 `SmAntiFraud`）的 `deviceUniqueId` 之类字段当成 `YIBAN_PHONE_CODE`——那不是易班设备绑定用的识别码，填错同样校验失败。
>
> 💡 如果签到时未报「请使用授权设备进行签到」，说明你的学校未开启设备绑定，无需配置这两个变量。

### 账号健康检查（探针模式，可选）

<details>
<summary>🩺 探针模式 + 注册时验证账号</summary>

非签到时段对全部账号做**只读健康检查**（登录 + 拉取签到任务，**不实际签到**），提前发现「图形验证墙 / 校本化失效 / 密码错误」等无法自愈的问题，避免次日签到失败才知道。

- **探针模式**：管理员在「系统设置 → 健康与探针」开启，设定**触发时间**与**触发频率**（每天 / 每 N 天 / 下一次计划时间单次执行）；到点后自动健康检查，异常账号邮件预警（管理员收汇总、用户收自己账号的预警），结果写入签到日志（来源标记为探针）。
- **注册时验证账号**：管理员开启后，用户 / 管理员提交账号时即时只读验证，失败**当场打回**并提示原因；同一手机号短时间内多次密码错误会被临时限制验证，防止反复试错导致易班账号被锁定。
- 需要一条**高频轮询**调度任务：systemd 部署在 `/etc/cron.d/` 增加 `*/10 * * * * yiban /opt/yiban-auto-sign/run_probe.sh`（脚本内自行判断是否到触发时间/频率，**实际执行时刻由 `YIBAN_PROBE_TIME` 决定**，改探针时间无需再动 cron；若 cron 写成低频固定时刻，会把探针钉死在 cron 时刻、设置页时间失效）。Docker 部署由容器调度器自动支持。
- 默认关闭：`YIBAN_PROBE_ENABLE=0`、`YIBAN_ACCOUNT_VERIFY=0`；在线校验默认**同步**（提交时当场返回结论），置 `YIBAN_VERIFY_ASYNC=1` 可改为异步（需界面支持）。不配置不影响现有签到。

</details>

### 告警通道健康报告发送时刻（可选）

<details>
<summary>🩺 固定每周报告的到达时间（时:分 + 星期几）</summary>

告警通道健康报告（例行每周一封；通道降级或当日推送额度耗尽时当天加发）由 web 进程的清理线程发出。**不配置时**，例行报告在**周一**、**服务启动那一刻的钟点**发出——节拍从服务启动起算，故重装或重启会把到达时刻一起搬走，深夜部署就在半夜收到。

- **配置入口**：「系统设置 → 健康与探针 → 固定健康报告的发送时刻」，或用 `.env` 直写 `YIBAN_HEALTH_REPORT_TIME=09:00`（`HH:MM`）与 `YIBAN_HEALTH_REPORT_WEEKDAY=0`（`0`=周一…`6`=周日）。
- **到达精度**：清理线程每 5 分钟唤醒一次，报告在配置时刻之后的**最近一次唤醒**发出，误差不超过 5 分钟。若设定时刻恰好落在跨零点的那一跳空隙里（例如设定 `23:58`、末跳 `23:56`），报告在零点后的第一跳补发（当周可能多出一封，**不会丢**）——"漏播比重复打扰严重"是本族既有的取舍。
- **时区口径**：时刻与星期一律按**北京时间（UTC+8）**判定——与签到窗口、按日账本同一时基，不是宿主系统本地时区。
- **生效时机**：保存后**下一轮唤醒即生效**（约 5 分钟内），不必重启服务。
- **不受影响的两件事**：① 通道降级或当日推送额度耗尽时**当天加发**，不看星期也不看时刻；②「确实送达才记已播」不变——没发出去就不记，当天稍后（重启后的下一轮）仍会重试。
- **只配星期、不配时刻**也是合法用法：那一天按**服务启动钟点**发（与完全不配置同一锚点，只是把那一天从周一挪到配置的星期）。
- 两枚键写坏（`99:99`、星期越界）会告警并**回落到"未配置"的既有行为**：报告照发，不会因为一处笔误而静默消失。

</details>

</details>
