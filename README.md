# 易班自动签到

![Python](https://img.shields.io/badge/Python-3.13+-blue?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)

## 项目介绍

> 易班（yiban）是部分高校使用的学生平台，一些学校要求每天早晨完成「早操签到」。
>
> 本项目配置一次后，每天到点自动完成签到：不需要手动操作，也不要求保持个人电脑开机。
>
> 部署方式：**国内云服务器**（推荐）、**Docker 容器**。

- 🤖 **全自动签到**：每天定时执行，无需人工干预；窗口内错峰排期（分片 + 账号间隔铺开），不是到点一起打
- 🔐 **真实 App 登录特征**：登录流程使用平台客户端的真实 App 请求特征（UA=Yiban + AppVersion + 随机 CSRF；字段值取自实拍），实测绕过易班风控 e003，新旧账号均稳定登录
- 🖥️ **网页管理后台**：管理员在任意设备（手机/平板/电脑）登录管理——账号增删改/排序/手动签到、审核用户提交的账号、用户管理与权限分级、批量操作、全局公告、签到日志与日历
- 🗄️ **SQLite 数据库存储**：账号与用户数据存于 SQLite——多人同时操作互不覆盖、手机号全局唯一；密码与设备识别码 AES-GCM 密文存储；数据库结构启动时自动迁移升级；批量操作整体回滚；关键管理操作自动审计留痕（HMAC 防篡改）
- 📍 **智能定位**：在签到围栏内生成随机定位点，模拟真实 GPS（外接矩形拒绝采样 + 偶奇射线法判定，围栏内精确均匀）
- 👥 **多账号支持**：一个仓库管理多个易班账号，顺序执行 + 队列重试（失败账号分散重试，普通≤3次/风控类≤2次；密码错误不重试）
- 🔔 **消息通知**：签到失败时推送通知（Server 酱 / Bark / 企业微信等）
- 🔄 **队列重试**：失败账号不立即重试，间隔分散重新安排，避免连击触发风控
- 🛑 **注册管控**：管理员可一键暂停新用户注册（全新部署默认暂停，完成初始配置后在设置页开启；暂停期间已注册用户登录不受影响，管理员仍可手动添加账号）

## 双线说明

> 本仓有两条线。旧线 = Python/Flask，生产在用 v0.6.5，本文描述它。
> 新线 = Go + React，正在 `test/go-p1-20261025` 分支实验，不在本分支。
> 新线可运行：`git switch test/go-p1-20261025`，读该分支的 `STRUCTURE.md`。
> 两线只共享 `docs/`。旧线只修缺陷，不加新功能。

## 文档索引

| 文档 | 内容 |
|---|---|
| [服务器部署（分步详解）](docs/guides/deploy-baremetal.md) | 环境准备 / 上传代码 / 账号与 .env / run.sh / crontab |
| [Docker 部署](docs/guides/deploy-docker.md) | 容器形态：Web + HTTPS + 定时签到 + 容器备份 |
| [配置说明](docs/guides/configuration.md) | 环境变量一览、账号间隔与容量、通知、代理、设备绑定、探针 |
| [网页管理后台](docs/guides/admin-console.md) | 启动方式、systemd 单元、Cookie 安全、部署形态、图标、合规文档 |
| [运维](docs/guides/operations.md) | 无浏览器部署、状态文件清理、多执行体并行签到、常用命令 |
| [运行测试](docs/guides/testing.md) | 已测试环境、跑测入口、未覆盖场景 |
| [第三方组件声明与开源致谢](docs/legal/third-party-notices.md) | 第三方组件、衍生来源分类、上游对照、致谢 |
| [备份与恢复](docs/dev/backup-restore.md) | 宿主与容器两条备份/恢复路线、取证命令、威胁边界 |
| [CLI 契约](docs/dev/cli.md) | 面向脚本/agent 的子命令、退出码、JSON 输出 |
| [故障排查与常见问题](docs/dev/troubleshooting.md) | Q1–Q10 |
| [原理与实现](docs/dev/implementation-notes.md) | 模块树、签到流程、定位算法、重试机制 |
| [开发文档索引](docs/dev/README.md) | 全部开发文档清单 |
| [测试文件清单](tests/README.md) | 测试目录说明与完整清单 |

## 部署方式选择

| 部署方式 | 适用场景 | 稳定性 | 成本 |
|---------|---------|--------|------|
| **云服务器**（推荐） | 有国内服务器；主力签到通道 | 稳定（国内出口不被 WAF 拦截） | 需服务器费用 |
| **Docker**（可选） | 已有 Docker 的服务器，一键容器化 | 稳定（Web / HTTPS / 定时签到开箱即用） | 需服务器费用 + Docker 环境 |
| **GitHub Actions**（已退役） | 无服务器；或作为冗余备份 | 已于 2026-09-25 退役，不再支持 | 免费 |

> **Docker 与「云服务器」方式二选一，不要同时启用**：两者都占用 80/443 端口（Docker 部署自带 nginx 反代），同时跑会端口冲突。

> ⛔ GitHub Actions 路径已退役（2026-09-25，MF-106）：`.github/workflows/signin.yml` 已从仓库删除，不再是受支持的部署路径。

**本项目的定位与规模**：这是**自托管工具（self-hosted）**，面向个人/小团体自建，资源需求很低（1 核 1G 小服务器可用）。

"能带多少账号"由签到窗口与账号间隔决定，不由一个固定数字决定——**用你自己的机器实测**，不要按估算拍板：

```bash
cd /opt/yiban-auto-sign
.venv/bin/python3 -m yiban.entry.cli capacity             # 按当前窗口/间隔给容量建议（不发请求）
```

量到「单执行体容量」后把它写进 `.env` 的 `YIBAN_CAPACITY_MEASURED`，`capacity` 与网页
「执行体」分区就会按它给建议值（实测 × 2/3）。

容量口径与建议值只在**提醒**层面存在，程序不设"每小时最多多少次"这类硬限制（不同部署者的机器与出口差异很大，写死阈值必然误伤）。详见 [多执行体并行签到（可选）](docs/guides/operations.md#多执行体并行签到可选)。

## 快速开始

服务器为 Ubuntu 22.04（已含 python3）；完整分步见 [服务器部署（分步详解）](docs/guides/deploy-baremetal.md)。

```bash
# 1. 环境（只需一次；sqlite3 供备份一致性快照，缺它时备份回退 cp 并跳过校验）
apt update && apt install -y python3-venv python3-pip sqlite3
pip3 config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

# 2. 拉取代码（Gitee 为国内拉取源；GitHub 是同一仓库的开发源）
git clone https://gitee.com/frostpolaris/yiban-auto-sign.git /opt/yiban-auto-sign

# 3. 依赖（装进项目自带虚拟环境 .venv；不要装进系统 Python）
cd /opt/yiban-auto-sign
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock

# 4. 配置账号：推荐用网页管理后台；无头环境用 .env 的 YIBAN_ACCOUNTS_JSON

# 5. 定时任务（每天 06:31 首签；同一脚本在进程内补签，07:12 再有一条 cron 兜底）
# ⚠ 已跑过 `sudo bash deploy/prod/install.sh` 的主机**跳过本节**：安装器已把
#    deploy/prod/cron.d/yiban-sign 落到 /etc/cron.d/。两处都写 = 每个排期两条
#    触发器，同一作业并发跑两轮（2026-10-25 备份事故的同形：gpg 互相覆写）。
crontab -e   # 仅在未跑安装器时追加：
# 31 6 * * * /opt/yiban-auto-sign/run.sh
# 12 7 * * * /opt/yiban-auto-sign/run.sh
# 无人值守/脚本化安装（未跑 install.sh 时；crontab -e 是交互式的）：
#   ( crontab -l 2>/dev/null; echo '31 6 * * * /opt/yiban-auto-sign/run.sh'; \
#     echo '12 7 * * * /opt/yiban-auto-sign/run.sh' ) | crontab -
# 跑过 install.sh 后删掉上面两行 root crontab（crontab -e 删除）：
# /etc/cron.d/yiban-sign 与 root crontab 各一条会让同一排期重复触发。
# 补签时刻须与 .env 的 YIBAN_SECOND_RUN_TIME（默认 07:12）一致。

# 6. 验证
.venv/bin/python3 scripts/signin.py --check-config   # 只读配置检查，不发任何请求
bash run.sh && tail -20 /var/log/yiban/sign-$(date +%F).log
```

## 配置速查

优先级口径：非路径键 **`.env` 优先、进程环境只补缺**；四个路径键（`YIBAN_DB_FILE` / `YIBAN_STATE_DIR` / `YIBAN_LOG_FILE` / `YIBAN_ENV_FILE`）进程环境优先。完整键位与逐键说明见 [配置说明](docs/guides/configuration.md) 与仓库根 `.env.example`。

| 变量名 | 说明 | 必填 |
|--------|------|------|
| `YIBAN_ACCOUNTS_JSON` | 账号 JSON 数组（推荐用于无头环境/CI，格式见[配置说明](docs/guides/configuration.md)） | 二选一 |
| `YIBAN_ACCOUNTS` / `YIBAN_PHONE`+`YIBAN_PASSWORD` | 旧格式（`手机号:密码`，多账号 `#` 分隔；单账号两键）向后兼容 | 二选一 |
| `YIBAN_ACCOUNT_GAP_MAX` | 账号间隔：相邻两次签到请求的最小间隔秒数，自动与手动签到均生效；默认 `10`，`0`=关闭 | 可选 |
| `YIBAN_SIGN_START` / `YIBAN_SIGN_END` | 签到窗口（`HH:MM`，默认 `06:30` / `07:50`） | 可选 |
| `YIBAN_SECOND_RUN_TIME` | 补签轮触发点，默认 `07:12`；**须与补签 cron 时刻一致**（见快速开始第 5 步） | 可选 |
| `YIBAN_SUNDAY_SIGN` / `YIBAN_SATURDAY_SIGN` | `1`=当天也执行；缺省/`0`=跳过（两个默认都跳过） | 可选 |
| `YIBAN_MAX_USERS` / `YIBAN_MAX_ACCOUNTS` | 容量上限（默认 `500` 用户 / `200` 账号；`0`=不限）。调小不删存量，只限制新增 | 可选 |
| `YIBAN_PROXY` | 代理地址（`http://host:port`、`socks5://host:port` 或带认证 `http://user:pass@host:port`） | 可选 |
| `YIBAN_PROXY_LIST` / `YIBAN_PROXY_FALLBACK` / `YIBAN_WORKERS` | 多执行体相关（单执行体部署**不需要**配置） | 可选 |
| `YIBAN_PHONE_MODEL` / `YIBAN_PHONE_CODE` | 设备型号与唯一识别码；账号未单独配置时全局回退 | 视情况 |
| `YIBAN_NOTIFY_TYPE` + `YIBAN_NOTIFY_SECRET_ENC` | 消息推送类型（`serverchan` / `custom`）与密钥密文；建议在网页「系统设置 → 通知通道」配置 | 可选 |
| `YIBAN_MAIL_ENABLE` / `YIBAN_MAIL_SMTP_HOST` / `YIBAN_MAIL_USER` / `YIBAN_MAIL_PASS` / `YIBAN_MAIL_ADMIN_TO` | 邮件通知（默认关闭）；发件列表推荐在网页配置 | 可选 |
| `YIBAN_TRUST_FORWARDED_HEADERS` | 转发头信任开关：**缺省不信任（fail-closed）**；只在请求来源是回环地址**且**本开关为开时采信 `X-Forwarded-For`。反代部署必须显式置 `1` | 可选 |
| `YIBAN_COOKIE_SECURE` | 会话 Cookie 的 Secure 位：经 nginx HTTPS 反代访问保持 `1`；直连 HTTP 调试必须删掉该行或设为 `0` | 可选 |
| `YIBAN_ADMIN_USER` / `YIBAN_ADMIN_PASSWORD_HASH` / `YIBAN_ADMIN_PW_VERSION` | 内置主管理员账号与 scrypt 哈希（由网页 `/setup` 引导或 `CLI init` 写入，不手填） | 自动 |
| `YIBAN_ADMIN_PASSWORD` | 旧版明文口令键：仅用于兼容既有部署；`/setup` 或 `CLI init` 成功后会被删除。新部署不要手填 | 旧部署可选 |
| `YIBAN_STATE_DIR` / `YIBAN_LOG_FILE` | 状态文件目录（默认 `/var/log/yiban`）与日志路径（按天分文件） | 可选 |
| `YIBAN_DB_FILE` / `YIBAN_ENV_FILE` | 数据库与 `.env` 路径（默认相对检出根：`data/yiban.db` / `data/.env`） | 可选 |
| `YIBAN_BASE_PATH` | Web 挂载前缀，仅在自动识别切错时兜底 | 可选 |

网页管理后台的启动、systemd 单元与部署形态见 [网页管理后台](docs/guides/admin-console.md)。创建内置管理员：

```bash
# 创建内置管理员（二选一）
#    ① 有浏览器：启动后首次打开站点，会自动进入 /setup 引导页；
#    ② 无浏览器（headless）：口令只走环境变量，不接受命令行参数。
YIBAN_ADMIN_PASSWORD='你的强口令' .venv/bin/python3 -m yiban.entry.cli init --username admin
#    ⚠️ 主管理员口令策略：至少 12 位，且包含大写字母 / 小写字母 / 数字 / 符号中的
#    至少三类。网页引导与 CLI init 使用同一套策略；写入的是 scrypt 哈希，明文不落盘。
```

管理接口节选（完整契约见 [docs/dev/cli.md](docs/dev/cli.md)）：

```bash
# 系统设置：POST /api/settings（部分更新）；公告：PUT /api/announcement 写草稿、
# POST /api/announcement/publish（仅主管理员；口令按 YIBAN_PW_GATE 档位——缺省 risk 档
# 同出口的已登录会话可直接生效，换出口才要一次；full 档每次都要）发布或下线
```

## 运维速查

```bash
# 查看今天签到日志（按天分文件）
tail -50 /var/log/yiban/sign-$(date +%F).log

# 清理过期数据（默认 dry-run；保留期见「状态文件清理」）
.venv/bin/python3 -m yiban.entry.cli state
bash scripts/yiban-cleanup.sh

# 手动触发签到
bash /opt/yiban-auto-sign/run.sh

# cron 与 Web 服务
systemctl status cron
crontab -l
sudo systemctl restart yiban-web.service   # 只改了 .env 也必须重启（配置在启动时读取）

# 更新代码（**整仓更新**）与依赖
cd /opt/yiban-auto-sign && git pull --ff-only
.venv/bin/pip install -r requirements.lock
sudo systemctl restart yiban-web.service
bash scripts/check-deps-lock.sh       # 依赖一致性门：报"venv 与 lock 一致"退 0
```

- 状态文件清理、多执行体并行签到、无浏览器部署（REST 接口）：[运维](docs/guides/operations.md)
- 备份与恢复（宿主与容器两条路线、取证命令、威胁边界）：[备份与恢复](docs/dev/backup-restore.md)
- 健康探针、告警报告发送时刻：见 [配置说明](docs/guides/configuration.md)

## 本地调试

<details>
<summary>🔧 展开：本地运行方法</summary>

```bash
# 1. 克隆仓库
git clone https://github.com/<你的用户名>/yiban-auto-sign.git
cd yiban-auto-sign

# 2. 安装依赖
pip install -r requirements.lock

# 3. 配置账号（推荐 JSON，一次输入一个账号完整信息）
export YIBAN_ACCOUNTS_JSON='[{"phone":"13800138000","password":"your_password"}]'
#   Windows PowerShell：$env:YIBAN_ACCOUNTS_JSON='[{"phone":"13800138000","password":"your_password"}]'
#   或旧格式：export YIBAN_ACCOUNTS="13800138000:your_password"

# 3.1 检查配置（不发任何网络请求，密码脱敏显示）
python scripts/signin.py --check-config

# 4. 运行
python scripts/signin.py
```

</details>

## 常见问题

故障排查与常见问题（Q1–Q10）见 [docs/dev/troubleshooting.md](docs/dev/troubleshooting.md)。

- 账号或密码错误（e003）：先别改密码，见 [Q1](docs/dev/troubleshooting.md#q1-账号或密码错误e003)
- Docker 与服务器部署同时启用会抢 80/443 端口：二选一
- 日志与备份排期重复触发：只保留 `/etc/cron.d/yiban-sign` 一处
- 学校开启设备绑定后签到失败：配置 `YIBAN_PHONE_MODEL` / `YIBAN_PHONE_CODE`（见 [配置说明](docs/guides/configuration.md)）

更多排查口径（WAF、探针、时区、备份）见 troubleshooting 全文。

## 注意事项

> **🔐 请自建部署，不要使用他人的公开实例**：本工具会保存易班账号密码（加密存储）、手机号、设备识别码等敏感数据。使用不明来源的公开实例 = 把易班账号和密码交给陌生人，对方可能解密查看甚至冒用签到。本项目为 AGPL 开源、自建成本很低，**请务必自行部署、自己掌控数据**；如发现有人运营公开实例收集账号，请提醒使用者注意风险。

1. **本项目仅供学习研究使用**，完整免责声明见 [AI 生成说明](#ai-生成说明)
2. **强烈建议仓库设为 Private**（Fork 场景），避免账号密码被搜索引擎索引
3. 账号密码只写在服务器本地 `.env` 或 GitHub Secrets，**不要写进代码、不要提交进仓库**
4. 请勿频繁调用 API（默认每天 2 次足够），以免触发风控
5. 如账号开启了二次验证，可能需要额外处理
6. **推荐国内服务器为唯一签到通道**：Actions 海外 IP 会被 WAF 拦截，其反复失败尝试可能连带触发账号风控（表现为 e003"密码错误"），进而影响服务器签到——有服务器时建议禁用 Actions 工作流（详见 [Q10](#q10-actions-会不会触发风控)）
7. **遇到"账号或密码错误"先别改密码**：默认登录方式已修复此问题（见 [Q1](#q1-账号或密码错误e003)）；若仍出现，先用手机 App 验证账号正常，再检查是否切回了旧流程（`YIBAN_LEGACY_LOGIN=1`）

## License

**GNU Affero General Public License v3.0（AGPL-3.0）** - 见 [LICENSE](LICENSE)

### 核心条款简述

1. **网络服务强制开源**：通过网络（网站 / API）向用户提供服务时，必须向服务使用者提供完整的源代码
2. **强制传染**：任何使用、修改或分发本项目的衍生作品，必须同样以 AGPL-3.0 协议开源
3. **署名要求**：必须保留原作者版权声明与许可声明

### 特别免责声明

- **滥用与盈利免责**：本项目按"原样"提供。任何使用本项目进行商业或非商业行为时，若因违反当地法律法规、滥用功能（包括但不限于网络攻击、诈骗等非法用途）而产生任何形式的刑事或民事纠纷，均与本项目作者无关，使用者需自行承担所有法律后果
- **无担保**：作者不保证本项目的适用性、稳定性或无错误（Bug）
- **损失免责**：因使用或无法使用本项目而导致的任何直接、间接、偶然或后果性损害（包括数据丢失、业务中断、利润损失等），作者不承担任何责任

第三方组件声明、衍生来源与开源致谢见 [docs/legal/third-party-notices.md](docs/legal/third-party-notices.md)。

## 相关开源项目推荐

易班签到生态中的其他开源方案（均含开源许可证，可放心参考）：

- [2117516450/yiban_signin](https://github.com/2117516450/yiban-signin)（易签，Unlicense）- 易班校本化早签/晚签打卡，多用户 + 多线程 + Server酱推送
- [Qs315490/YiBan_AutoSgin](https://github.com/Qs315490/YiBan_AutoSgin)（GPL-2.0）- 易班校本化晚点签到脚本（含活跃 fork：[Lumjiel/YiBan_AutoSgin](https://github.com/Lumjiel/YiBan_AutoSgin)）
- [OneFeiFan/FYIBAN](https://github.com/OneFeiFan/FYIBAN)（AGPL-3.0）- 易班 API 安卓库，校本化 OAuth 登录与签到（本项目**早期**实现的参考来源；2026-10 换核后仓内已不含其派生代码）

## AI 生成说明

<details>
<summary>🤖 AI 编程生成声明 / AI 安全免责 / 第三方适配免责</summary>

### AI 编程生成声明

本项目的代码与文档由 AI 辅助生成，非人工逐行编写：

- **AI 负责**：核心签到逻辑的编写与调试、GitHub Actions / Gitee Go 工作流配置、README 等文档、WAF 风控问题的排查与修复；
- **人类负责**：需求定义与流程设计、实际运行测试与参数调优、服务器部署与运维决策、最终代码审查与确认。

> ⚠️ AI 生成的代码可能存在未预见的问题、逻辑漏洞或与最新平台规则不符的情况。使用前请务必充分测试，并根据自己的环境调整后再投入使用。

### AI 安全免责声明

> **使用本项目即表示你已知晓并接受以下风险：**

1. **代码可靠性风险**：AI 生成的代码可能存在未发现的缺陷，可能导致签到失败、账号异常、数据丢失等问题；
2. **账号安全风险**：本项目需要你提供易班账号密码，存在凭证泄露的风险（即使使用 GitHub Secrets，也无法保证 100% 安全）；
3. **平台规则变化风险**：易班平台可能随时更新 API 接口、WAF 规则或用户协议，导致本项目失效或触发风控；
4. **AI 训练数据时效性**：AI 模型的训练数据存在时效性，可能不了解易班平台的最新规则变化；
5. **责任归属**：本项目仅供学习研究使用，使用者需自行承担一切后果。开发者（包括 AI 与人类协作者）不对任何直接或间接损失负责。

### 第三方适配免责声明

> **本项目与易班官方无任何关联，属于非官方第三方适配项目：**

1. **非官方性质**：本项目不是易班（yiban.cn / uyiban.com）官方产品，也未获得易班官方的授权、认可或支持；
2. **接口逆向**：本项目通过逆向分析易班客户端的 API 接口实现签到功能，可能违反易班用户协议或相关服务条款；
3. **合规性提示**：使用本项目可能违反你所在学校的相关规定，可能导致你的易班账号被风控、限制功能或封禁，也可能影响学校考勤数据的真实性，带来学业诚信问题；
4. **数据与隐私**：本项目会在服务器上处理你的账号密码，请务必在可信环境（如私有仓库、自有服务器）中部署；
5. **停止维护**：如易班官方提出要求，本项目可能随时停止维护或下架。

> 📌 **建议**：如条件允许，请优先使用易班官方客户端手动签到。本项目仅作为技术学习与研究的产物，不鼓励用于实际规避考勤。

</details>
