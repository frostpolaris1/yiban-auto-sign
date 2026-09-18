# 易班自动签到

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)

## 项目介绍

> 易班（yiban）是部分高校使用的学生平台，一些学校要求每天早晨完成「早操签到」。
>
> 本项目配置一次后，每天到点自动完成签到：不需要手动操作，也不要求保持个人电脑开机。
>
> 部署方式：**国内云服务器**（推荐）、**Docker 容器**、**GitHub Actions**（备选）；自带网页管理后台，手机/平板/电脑均可访问。

- 🤖 **全自动签到**：每天定时执行，无需人工干预；窗口内错峰排期（分片 + 账号间隔铺开），不是到点一起打
- 🔐 **真实 App 登录特征**：登录流程复刻 [OneFeiFan/FYIBAN](https://github.com/OneFeiFan/FYIBAN) 的真实 App 请求特征（UA=Yiban + AppVersion + 随机 CSRF），实测绕过易班风控 e003，新旧账号均稳定登录
- 🖥️ **网页管理后台**：管理员在任意设备（手机/平板/电脑）登录管理——账号增删改/排序/手动签到、审核用户提交的账号、用户管理与权限分级、批量操作、全局公告、签到日志与日历
- 🗄️ **SQLite 数据库存储**：账号与用户数据存于 SQLite——多人同时操作互不覆盖、手机号全局唯一；密码与设备识别码 AES-GCM 密文存储；数据库结构启动时自动迁移升级；批量操作整体回滚；关键管理操作自动审计留痕（HMAC 防篡改）
- 📍 **智能定位**：在签到范围内生成随机定位点，模拟真实 GPS（缩放质心算法）
- 👥 **多账号支持**：一个仓库管理多个易班账号，顺序执行 + 队列重试（失败账号分散重试，普通≤3次/风控类≤2次；密码错误不重试）
- 🔔 **消息通知**：签到失败时推送通知（Server 酱 / Bark / 企业微信等）
- 🆓 **完全免费**：使用 GitHub Actions 免费额度，每月消耗仅几分钟（远低于 2000 分钟配额）
- ⏰ **自动续期**：内置 `gh-workflow-keepalive`，定时工作流自动续期，避免被 GitHub 60 天无活动禁用
- 🔄 **队列重试**：失败账号不立即重试，间隔分散重新安排，避免连击触发风控
- 🛑 **注册管控**：管理员可一键暂停新用户注册（全新部署默认暂停，完成初始配置后在设置页开启；暂停期间已注册用户登录不受影响，管理员仍可手动添加账号）

## 目录

- [项目介绍](#项目介绍)
- [部署方式选择](#部署方式选择)
- [快速开始](#快速开始)
- [服务器部署（分步详解）](#服务器部署分步详解)
- [Docker 部署（可选）](#docker-部署可选)
- [GitHub Actions（备选）](#github-actions备选)
- [配置说明](#配置说明)
- [网页管理后台](#网页管理后台)
- [运维](#运维)
  - [备份与恢复](#备份与恢复)
  - [状态文件清理](#状态文件清理)
  - [多执行体并行签到（可选）](#多执行体并行签到可选)
  - [常用运维命令](#常用运维命令)
- [本地调试](#本地调试)
- [原理与实现](#原理与实现)
- [常见问题](#常见问题)
- [注意事项](#注意事项)
- [测试范围与适配说明](#测试范围与适配说明)
- [License](#license)
- [开源致谢 / Acknowledgements](#开源致谢--acknowledgements)
- [相关开源项目推荐](#相关开源项目推荐)
- [AI 生成说明](#ai-生成说明)

## 部署方式选择

| 部署方式 | 适用场景 | 稳定性 | 成本 |
|---------|---------|--------|------|
| **云服务器**（推荐） | 有国内服务器；主力签到通道 | 稳定（国内出口不被 WAF 拦截） | 需服务器费用 |
| **Docker**（可选） | 已有 Docker 的服务器，一键容器化 | 稳定（Web / HTTPS / 定时签到开箱即用） | 需服务器费用 + Docker 环境 |
| **GitHub Actions**（备选） | 无服务器；或作为冗余备份 | 受 WAF 拦截影响，不稳定 | 免费 |

> **Docker 与「云服务器」方式二选一，不要同时启用**：两者都占用 80/443 端口（Docker 部署自带 nginx 反代），同时跑会端口冲突。

> ⚠️ GitHub Actions 的海外 IP 可能被易班 WAF 风控拦截，且反复失败可能触发账号风控。**有云服务器时强烈建议使用服务器部署**。

**本项目的定位与规模**：这是**自托管工具（self-hosted）**，面向个人/小团体自建，资源需求很低（1 核 1G 小服务器可用）。

"能带多少账号"由签到窗口与账号间隔决定，不由一个固定数字决定——**用你自己的机器实测**，不要按估算拍板：

```bash
cd /opt/yiban-auto-sign
python3 -m yiban.cli capacity                    # 按当前窗口/间隔给容量建议（不发请求）
sudo python3 scripts/loadtest/capacity_probe.py --repo /opt/yiban-auto-sign --users 5000
```

容量口径与建议值只在**提醒**层面存在，程序不设"每小时最多多少次"这类硬限制（不同部署者的机器与出口差异很大，写死阈值必然误伤）。详见 [多执行体并行签到（可选）](#多执行体并行签到可选)。

## 快速开始

服务器为 Ubuntu 22.04（已含 python3）：

```bash
# 1. 环境（只需一次）
apt update && apt install -y python3-pip
pip3 config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

# 2. 拉取代码（服务器为主力签到，推荐国内网络直拉 Gitee 或上传压缩包）
git clone https://gitee.com/frostpolaris/yiban-auto-sign.git /opt/yiban-auto-sign
cd /opt/yiban-auto-sign && pip3 install -r requirements.lock
# 裸机部署安装精确锁定的 requirements.lock（与 CI/Docker 镜像同源），
# 保证实际部署的依赖版本 = 安全审计覆盖的版本；requirements.txt 仅作下限声明。

# 3. 配置账号：推荐用网页管理后台（见「网页管理后台」）；
#    无头环境/CI 可用 .env 的 YIBAN_ACCOUNTS_JSON（见「配置说明」）

# 4. 定时任务（每天 06:31 首签；同一条脚本在进程内补签，07:12 再有一条 cron 兜底）
crontab -e   # 追加：
# 31 6 * * * /opt/yiban-auto-sign/run.sh
# 12 7 * * * /opt/yiban-auto-sign/run.sh
# 补签时刻须与 .env 的 YIBAN_SECOND_RUN_TIME（默认 07:12）一致；
# 周六/周日是否执行由网页「系统设置 → 签到调度」的「周六签到 / 周日签到」开关决定，
# 两个默认都关（未开启时当天自动跳过）。

# 5. 验证
python3 scripts/signin.py --check-config   # 只读配置检查，不发任何请求
bash run.sh && tail -20 /var/log/yiban/sign-$(date +%F).log
```

## 服务器部署（分步详解）

<details>
<summary>📋 展开：环境准备 / 上传代码 / 账号与 .env / run.sh / crontab / 手动测试</summary>

#### 1. 服务器环境准备（Ubuntu 22.04）

```bash
apt update
apt install -y python3 python3-pip
```

#### 2. 上传项目代码

在本地打包（排除 `.git` 与缓存），再上传解压。**必须整仓更新**：代码已拆到 `yiban/` 多个模块，单文件覆盖会漏文件。

```bash
cd yiban-auto-sign
tar -czf yiban.tar.gz --exclude='.git' --exclude='__pycache__' .
scp yiban.tar.gz root@你的服务器IP:/opt/

# 服务器侧
mkdir -p /opt/yiban-auto-sign && cd /opt/yiban-auto-sign
tar -xzf /opt/yiban.tar.gz && rm /opt/yiban.tar.gz
pip3 config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
pip3 install -r requirements.lock
```

#### 3. 账号数据

推荐用网页后台添加（存 `yiban.db`，AES-GCM 加密）。无头环境 / CI 可用环境变量直接注入：

```bash
YIBAN_ACCOUNTS_JSON='[{"phone":"13800138000","password":"你的密码","phone_model":"Vivo-XXXX","phone_code":"64位识别码"}]'
```

单账号仍可用 `YIBAN_PHONE` / `YIBAN_PASSWORD`（向后兼容）。`accounts.json` 只是旧版本迁移来源（迁移后自动改名 `.bak`）。

#### 4. 配置环境变量（`.env`）

```bash
cat > /opt/yiban-auto-sign/.env << 'EOF'
YIBAN_PROXY=http://127.0.0.1:8888
# YIBAN_ACCOUNT_GAP_MAX=10   # 账号间隔：相邻请求最小间隔秒数（默认 10）
EOF
```

`.env` 只需放公共选项；账号已在数据库里。改完 `.env` 必须重启 Web 服务（配置在启动时读取），定时签到下次触发自动生效。完整键位见 [配置说明](#配置说明) 与仓库根 `.env.example`。

#### 5. run.sh（仓库自带，不要手写替代）

`run.sh` 已包含：单实例锁（防止两次 cron 并发触发风控）、当日已触发标记（第二次触发自动按补签轮运行）、进程内补签轮、总超时、按天日志（`<状态目录>/sign-YYYY-MM-DD.log`）。

```bash
chmod +x /opt/yiban-auto-sign/run.sh
mkdir -p /var/log/yiban
```

> ⚠️ 不要用"导出 .env 再跑 signin.py"的简化版覆盖它：那会丢掉锁、防重复与超时保护，两次 cron 并发登录同一批账号会触发易班风控。

#### 6. crontab

```cron
# 易班自动签到：06:31 首签 + 07:12 兜底（兜底仅覆盖"06:31 进程被宿主杀死"的场景）
31 6 * * * /opt/yiban-auto-sign/run.sh
12 7 * * * /opt/yiban-auto-sign/run.sh
```

补签时刻须与 `.env` 的 `YIBAN_SECOND_RUN_TIME`（默认 `07:12`）一致：`run.sh` 在首轮结束后、持锁的同一进程内等到该时刻再判定是否补跑；签到侧也按它判断"是否还有下一轮兜底"（告警抑制）。只改 cron 不改该键会出现提前告警或真异常漏报。

周六、周日默认都不签到：在「系统设置 → 签到调度」打开「周六签到 / 周日签到」后才会尝试（学校该日确实无任务时显示为无需签到）。

#### 7. 手动测试

```bash
bash /opt/yiban-auto-sign/run.sh
tail -20 /var/log/yiban/sign-$(date +%F).log
```

</details>

## Docker 部署（可选）

> 适合已有 Docker 环境、想"一键起 Web + HTTPS + 定时签到"的服务器。**与服务器部署二选一**（都占用 80/443，不要同时跑）。

### 1. 前置条件

- 已安装 Docker 与 Compose（验证：`docker --version`、`docker compose version`；Ubuntu 可参考 `curl -fsSL https://get.docker.com | sh`）
- **x86_64** 架构（镜像暂仅构建 x86_64）
- 资源建议 1 核 1G，端口 **80/443** 空闲

### 2. 一键部署

```bash
# 1. 拉取代码
git clone https://gitee.com/frostpolaris/yiban-auto-sign.git && cd yiban-auto-sign

# 2. 数据目录与配置（数据全部落在 ./data，证书放 ./certs）
mkdir -p data certs
cp .env.docker.example data/.env
vim data/.env      # 务必填写 YIBAN_ADMIN_USER / YIBAN_ADMIN_PASSWORD

# 3. 生成 HTTPS 证书（自签，用于快速体验；生产请替换为受信证书）
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout certs/key.pem -out certs/fullchain.pem \
  -subj "/CN=你的域名或IP"

# 4. 启动（首次会构建镜像）
docker compose up -d --build

# 5. 验证
docker compose ps && docker compose logs -f yiban
```

浏览器打开 `https://你的域名或IP`（自签证书首访需点「继续访问」信任），用 `data/.env` 里的管理员账号登录后添加易班账号。

> **国内主机构建镜像**：容器内直连 `pypi.org` 通常不可达，构建会在安装依赖时以 `No matching distribution found` 失败。指定国内索引即可（仅构建期生效，不写进镜像配置）：
> ```bash
> docker compose build --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
> ```
> 或 `docker build --build-arg PIP_INDEX_URL=... -f docker/Dockerfile .`。

> **生产 HTTPS**：用受信证书替换 `certs/fullchain.pem` 与 `certs/key.pem`，然后 `docker compose restart yiban-nginx`；申请免费证书可参考 `web/deploy/nginx.conf.example` 的提示（acme.sh / Let's Encrypt）。

### 3. 日常运维

```bash
docker compose down                  # 停止
docker compose up -d                 # 启动
docker compose logs -f yiban         # 应用/签到日志
docker compose logs -f yiban-nginx   # 反代日志

git pull && docker compose up -d --build   # 更新代码后重建
```

### 4. 与宿主部署的差异（容器内已自动处理）

- **定时签到**：不依赖宿主 cron，由容器内 `supervisor` 常驻的 `docker/scheduler.py` 承担（首签 + 补签 + 每日清理）。
- **时区**：容器固定 `Asia/Shanghai`；同时窗口与日期判定本身按北京时间计算（`yiban/clock.py`），宿主是 UTC 也不会算错。
- **安全模型**：nginx 通过 `network_mode: service` 与应用共享网络栈，应用只见回环流量；`X-Forwarded-For` 语义不变（限速/登录锁定按真实 IP 生效）。
- **自定义 Web 图标**：取消 `docker-compose.yml` 中 `yiban` 服务里那行被注释的挂载（宿主 `./logo.png` → 容器 `web/static/vendor/logo.png`），把图标放到仓库根 `logo.png`。

### 5. 目录/文件说明

| 文件 | 作用 |
|---|---|
| `docker/Dockerfile` | 应用镜像（x86_64） |
| `docker-compose.yml` | 编排 `yiban`（应用）+ `yiban-nginx`（反代）双容器 |
| `docker/nginx.conf` | 容器内 HTTPS 反代配置 |
| `docker/supervisord.conf` | 容器内进程管理（Web + 调度） |
| `docker/scheduler.py` | 容器内签到调度 |
| `docker/entrypoint.sh` | 容器入口：准备数据卷后拉起 supervisor |
| `.env.docker.example` | Docker 部署配置模板 |
| `.dockerignore` | 阻止账号/密钥/数据库/日志进镜像 |

<details>
<summary>💾 Docker 备份与恢复（加密备份 / 手动 tar / 恢复校验）</summary>

数据（SQLite / 账号密文 / 加密密钥 / 日志）全部位于宿主 `./data`，**备份该目录即可**：

```bash
# 推荐：加密备份（口令经环境变量传入，磁盘不留明文；RETAIN_DAYS 自动轮转，默认 30 天）
YIBAN_BACKUP_PASSPHRASE='你的备份口令' bash docker/backup-docker.sh

# 也可手动裸 tar（明文落盘，请自行妥善保管）
tar czf yiban-backup-$(date +%F).tar.gz data/
```

恢复（校验与解包一体；口令经环境变量注入，不出现在命令行/ps/shell history；解包前做路径穿越、符号链接、设备节点三重校验）：

```bash
YIBAN_BACKUP_PASSPHRASE='你的口令' bash docker/backup-docker.sh --restore backups/yiban-data-2026-08-29.tar.gz.gpg ./restore-test
```

> ⚠️ 与 systemd 部署一致：加密密钥（`data/.env`）与备份口令要与数据**分开存放备份**——密钥丢失 = 已加密账号不可恢复。
>
> ⚠️ 威胁边界：口令与数据**同机**存放（root crontab/.env）时，加密只能防「备份介质单独失窃」——SSH/root 失陷即口令与全部备份（含异机副本）同时易手。更高强度口径：用 systemd 部署 `scripts/backup.sh` 的 `BACKUP_GPG_RECIPIENT` 公钥模式（服务器只存公钥），或把口令/私钥保存在异机、仅在备份时注入。

</details>

<details>
<summary>🔐 安全运维：主管理员权限追回 / 账号凭据密钥轮换 / 时钟守卫冻结恢复</summary>

**核心机制**：主管理员会话有效性绑定 `.env` 的 `YIBAN_ADMIN_PW_VERSION`（整数）；递增即全部旧主管理员会话立即失效（无需重启，下一次请求生效）。v0.26.0 起，通过 SSH 重写 `YIBAN_ADMIN_PASSWORD` 后重启，系统检测到"明文与现存哈希不一致"会**自动递增** PW_VERSION。

**场景 A：密码被攻击者改掉/泄露**

1. SSH 登录服务器，编辑 `.env`（Docker：`/data/.env`；systemd：`/opt/yiban-auto-sign/.env`）：
   - **删除/清空 `YIBAN_ADMIN_PASSWORD_HASH` 行**（不删则旧哈希仍优先生效，等于没改）；
   - 写入新密码（至少 12 位，且包含大写字母、小写字母、数字、符号中的至少三类，弱口令会拒绝启动）：`YIBAN_ADMIN_PASSWORD=新强密码`；
   - `YIBAN_ADMIN_PW_VERSION` 若已存在则 +1（不存在则忽略，重启迁移会自动处理）；
2. 重启服务：`systemctl restart yiban-web` 或 `docker compose restart yiban`（启动迁移会把新明文转 scrypt 哈希并清空明文，同时递增 PW_VERSION）；
3. 用新密码登录，清理失控的注册管理员（改回普通用户/删除）；
4. （可选，全端强制下线）更换 `YIBAN_SECRET_KEY` 为新随机串——所有用户需重新登录。账号密文密钥（`YIBAN_ACCOUNTS_KEY`）与审计链密钥（`YIBAN_AUDIT_KEY`）相互独立，不受影响。

**场景 B：仅会话 cookie 被盗（密码未失守）**：只做第 1 步的 PW_VERSION+1（实时生效）；如需全端下线再做第 4 步。

**场景 C：`YIBAN_ACCOUNTS_KEY` 疑似泄露（账号凭据密钥轮换）**

SSH 失陷时攻击者可读 `.env` 中的 `YIBAN_ACCOUNTS_KEY`，离线解密全部易班账号密码。轮换**必须在停服窗口执行**（Docker：`docker compose stop yiban`——web/scheduler 是该容器内 supervisord 子进程，`stop web scheduler` 这类服务名不存在；裸机：`systemctl stop yiban-web`。工具自身也会扫描进程并拒绝在存活的 web/signin/scheduler 旁执行）：

1. 一步完成解密→重加密→自校验→更新 `.env`：
   `python3 scripts/rekey_accounts.py --generate`（或 `--new-key <64位hex>` / `--new-key-file <文件>`；可用 `--db`/`--env` 指定路径；`--force` 跳过存活进程探活）。新钥会先落 0600 暂存文件 `<env>.rekey-staging` 作崩溃恢复之用，完成后自动删除；
2. 重启全部进程（web/signin/scheduler）；若 shell 或容器环境变量里仍设有旧 `YIBAN_ACCOUNTS_KEY`，同步更新——环境变量优先级高于 `.env`；
3. 事后取证：`python3 scripts/audit_verify.py --db data/yiban.db` 校验审计链（轮换动作本身也留痕）。注意旧密钥应视为已泄露——若攻击者曾拷贝数据库文件，历史密文仍需按泄露处理（通知受影响用户改易班密码）。

崩溃恢复（注意"改回旧钥即可恢复"只对**提交前**的中断成立）：

- 重加密事务提交**前**中断：库未变更，`.env` 旧钥仍有效，直接重跑本工具；
- 重加密事务提交**后**、写 `.env` 前中断：库内已是新钥密文而 `.env` 仍是旧钥——新钥就在暂存文件 `<env>.rekey-staging`（0600），写回 `.env` 的 `YIBAN_ACCOUNTS_KEY` 即恢复；或重跑 `python3 scripts/rekey_accounts.py --env-only --new-key-file <暂存文件>` 补完（`--env-only` 会先用新钥抽样试解一行库内密文，密钥不对即拒绝写 `.env`）。

**事后取证**：`python3 scripts/audit_verify.py --db data/yiban.db --env .env --anchor /var/log/yiban/audit-anchor.log`
一次跑完三件校验——哈希链自洽（防改行）、库外锚点比对（防删尾/删前缀/整表清空/截断或改写锚点文件）、审计写入欠账。
退出码 0=健康、1=检出异常、2=无法定论（缺密钥/库不存在/锚点不可读）。批量操作审计含脱敏目标清单，登录成功留有匿名化 IP
审计（登录失败阈值/越权 403/密钥轮换/数据导出同样留痕）。

> 诚实边界：以上判据都在**同一台机器**上。拿到 root 者可改 `.env` 里的审计密钥并重启服务，让链在新密钥下重签自洽——
> 合法的重链只会发生在"任何锚点存在之前"（即升级那一次），锚点之后再出现重链就判异常。但要真正排除，靠的是
> **离开本机的两份留痕**：每日日报邮件里的链头哈希与记录数、以及异机备份副本（`REMOTE_BACKUP`，其中已含审计锚点文件）。
> 怀疑失陷时先取这两处比对，再决定是否按密钥泄露处理。

**时钟守卫冻结恢复**：系统时间前进超 72h / 回拨超 1h（合法长停机、时钟维修后都会触发）时，全部物理清理会被守卫冻结并邮件告警。核实系统时间已正确后运行 `python3 scripts/clock_guard_reset.py --confirm` 重置（不带 `--confirm` 仅查看状态；刻意不自动恢复——防"拨快一次、下轮洗白"）。

> 若 `.env` 不可写：启动迁移失败后主管理员登录会被 fail-closed 拒绝（明文比对已停用），修复文件属主/权限后重启即自动补齐哈希。

> 注：Windows 无 `fcntl`，签到运行锁/状态文件锁/env 锁退化为进程内互斥——Windows 仅建议单进程开发调试，生产请用 Linux/容器。

</details>

<details>
<summary>👥 管理员权限矩阵（内置主管理员 / 注册管理员 / 普通用户）</summary>

| 能力 | 内置主管理员（.env） | 注册管理员 | 普通用户 |
|---|---|---|---|
| 账号管理（审核/编辑/删除/彻底删除/批量） | ✓ | ✓ | 仅本人（my-*） |
| 手动签到 / 批量签到 | ✓ | ✓ | ✗ |
| 重置/删除普通用户 | ✓ | ✓ | ✗ |
| 设为/取消管理员（单个操作 + 二次确认）、重置/删除**其他管理员** | ✓ | ✗ | ✗ |
| 物理清除已注销用户（剥夺 7 天反悔权，不可逆） | ✓ | ✗ | ✗ |
| 调度设置（排序/分布/掐头去尾/账号间隔/窗口/自选开关/全局暂停） | ✓ | ✗ | ✗ |
| 周六/周日开关、公告、注册开关、探针开关 | ✓ | ✓ | ✗ |
| SMTP 邮箱配置 | ✓ | ✗ | ✗ |
| `.env` 的 PW_VERSION / SECRET_KEY / 容量上限 / 审计密钥 | 仅 SSH | 不可达 | 不可达 |

</details>

## GitHub Actions（备选）

<details>
<summary>🐙 展开：Fork / Secrets / 启用工作流 / 手动测试 / 定时与延迟 / 资源消耗</summary>

> ⚠️ GitHub Actions 的服务器在海外，可能被易班 WAF 风控拦截（返回「风险访问服务禁用」），且海外 IP 反复失败可能触发账号风控。**有云服务器时请改用 [服务器部署](#服务器部署分步详解)**；以下仅作免服务器场景的备选。

### 第 1 步：Fork 仓库

点击仓库右上角 **Fork**。建议取消勾选「Copy the main branch only」以获取完整历史（只勾主分支也能用）。

### 第 2 步：配置账号（必填）

进入 Fork 后的仓库：**`Settings`** → **`Secrets and variables`** → **`Actions`** → **`New repository secret`**。

| Secret 名称 | 说明 | 是否必填 |
|------------|------|---------|
| `YIBAN_ACCOUNTS` | 易班账号，格式 `手机号:密码`，多账号用 `#` 分隔 | 二选一必填 |
| `YIBAN_PHONE` | 易班手机号（单账号，向后兼容） | 二选一必填 |
| `YIBAN_PASSWORD` | 易班密码（单账号，向后兼容） | 二选一必填 |
| `YIBAN_PROXY` | 可选：HTTP 代理地址 | 可选 |
| `YIBAN_PHONE_MODEL` | 设备型号（学校开启设备绑定时必填，见 [设备绑定](#设备绑定可选)） | 视情况必填 |
| `YIBAN_PHONE_CODE` | 设备唯一识别码（同上） | 视情况必填 |
| `YIBAN_NOTIFY_URL` | 通知 webhook URL（见 [消息通知](#消息通知可选)） | 可选 |

```
YIBAN_ACCOUNTS = 13800138000:your_password
# 多账号：13800138000:pwd1#13900139000:pwd2
```

> ⚠️ 密码中如含 `:` 或 `#`，请改用 `YIBAN_PHONE` / `YIBAN_PASSWORD` 两个 Secret 分别配置。

### 第 3 步：启用工作流

`Actions` 标签页 → 如有提示点 **`I understand my workflows, go ahead and enable them`** → 左侧应出现工作流 **`Yiban Sign-in`**。

### 第 4 步：手动测试

`Actions` → 左侧选 **`Yiban Sign-in`** → 右侧 **`Run workflow`** → 分支选 `main` → 确认。运行结束后点进本次运行查看日志。

日志中应出现（版本号会不同）：

```
[2026-08-01 06:35:01] [INFO] ==== 开始执行签到（vX.Y.Z），共 1 个账号，队列重试模式 ====
[2026-08-01 06:35:01] [INFO] [13800138000] 登录成功
[2026-08-01 06:35:02] [INFO] [13800138000] 生成定位: (118.789,32.045) 地址: XX大学
[2026-08-01 06:35:03] [INFO] [13800138000] 签到成功
[2026-08-01 06:35:03] [INFO] ==== 签到汇总（vX.Y.Z）：✅ 1 成功，❌ 0 失败 ====
```

若遇到 WAF 风控拦截（「风险访问服务禁用」），说明该网络出口被易班风控，建议改用服务器部署。

### 定时与延迟

早操签到窗口为**北京时间 06:30–07:50**，Actions 每天在该窗口内执行 **1 次**：

| Cron 表达式 | UTC 时间 | 北京时间 | 实际预计执行 | 用途 |
|------------|---------|---------|------------|------|
| `45 21 * * *` | 21:45（前一日） | 05:45 | 约 06:40–07:45 | 签到（云服务器为主力，此为备用） |

> Actions 的 `schedule` 延迟分布约 55–120 分钟（26 个样本，中位数约 80 分钟）。设为 05:45 触发，实际执行约 06:40–07:45，落在签到窗口内；延迟 <45 分钟才空跑、>125 分钟才超时（历史均为 0%）。
>
> 修改触发时间：编辑 [`.github/workflows/signin.yml`](.github/workflows/signin.yml) 的 `cron` 字段（格式 `分 时 日 月 周`，UTC；北京时间 = UTC + 8）。**务必保证触发时间 + 预估延迟仍落在签到窗口内**，否则会因"未在签到时间内"而空跑。
>
> **时区**：runner 是 UTC 主机，但签到窗口与"今天"的判定在程序内一律按北京时间计算（`yiban/clock.py`），工作流也额外注入了 `TZ: Asia/Shanghai`——无需你另行配置。表中的"UTC 时间"只描述 `cron` 字段如何被 GitHub 解释。

### 资源消耗

| 项目 | 数值 |
|------|------|
| 每日执行次数 | 1 次 |
| GitHub 免费额度 | 2000 分钟/月（公开仓库不限） |
| 每次执行耗时 / 每月消耗 | 在 Actions 运行详情页看实际耗时；账号越多越慢 |

</details>

## 配置说明

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
| `YIBAN_SIGN_ORDER` / `YIBAN_SIGN_DIST` | 排序 `sequence`（默认）/`random`；分布 `uniform`（默认）/`normal` | 可选 |
| `YIBAN_BLOCK_CAP` | 错峰分块容量（每块最多人数，默认 `15`） | 可选 |
| `YIBAN_SECOND_RUN_TIME` | 补签轮触发点，默认 `07:12`；**须与补签 cron 时刻一致**（见服务器部署第 6 步） | 可选 |
| `YIBAN_SUNDAY_SIGN` / `YIBAN_SATURDAY_SIGN` | `1`=当天也执行；缺省/`0`=跳过（两个默认都跳过） | 可选 |
| `YIBAN_ALLOW_TIME_PREF` | 用户自选时间片总开关：`1`=开启（默认关） | 可选 |
| `YIBAN_MAX_USERS` / `YIBAN_MAX_ACCOUNTS` | 容量上限（默认 `500` 用户 / `200` 账号；`0`=不限）。调小不删存量，只限制新增；主管理员可在「系统设置 → 容量配额」直接设置 | 可选 |
| `YIBAN_BATCH_SIGN_COOLDOWN_SEC` | 手动签到全局冷却秒数（默认 `1800`，`0`=关闭）：批量与单条共用；60 秒同账号防抖与此独立 | 可选 |
| `YIBAN_LOGINFAIL_DAILY_MAX` | 登录失败告警独立推送日额度（默认 `3`，`0`=不限），与普通/紧急告警额度分账 | 可选 |
| `YIBAN_SLOW_SIGN_SEC` | 单次签到耗时告警阈值（秒，默认 `30`） | 可选 |
| `YIBAN_NOTIFY_TYPE` + `YIBAN_NOTIFY_SECRET_ENC` | 消息推送类型（`serverchan` / `custom`）与密钥密文；建议在网页「系统设置 → 通知通道」配置（自动加密落盘）。旧明文 `YIBAN_NOTIFY_URL` 仍兼容（按 `custom` 处理） | 可选 |
| `YIBAN_MAIL_ENABLE` / `YIBAN_MAIL_SMTP_HOST` / `_PORT` / `YIBAN_MAIL_USER` / `YIBAN_MAIL_PASS` / `YIBAN_MAIL_ADMIN_TO` | 邮件通知（默认关闭）；发件列表推荐在网页配置，`.env` 方式作为首个发件条目生效 | 可选 |
| `YIBAN_PROXY` | 代理地址（`http://host:port`、`socks5://host:port` 或带认证 `http://user:pass@host:port`） | 可选 |
| `YIBAN_PHONE_MODEL` / `YIBAN_PHONE_CODE` | 设备型号与唯一识别码；账号未单独配置时全局回退（见 [设备绑定](#设备绑定可选)） | 视情况 |
| `YIBAN_PROBE_ENABLE` / `YIBAN_PROBE_TIME` / `YIBAN_PROBE_INTERVAL_DAYS` / `YIBAN_ACCOUNT_VERIFY` / `YIBAN_VERIFY_ASYNC` | 健康探针与注册时校验（默认全关，见 [账号健康检查](#账号健康检查探针模式可选)） | 可选 |
| `YIBAN_LEGACY_LOGIN` | 设为 `1` 使用旧登录流程（伪造 iOS UA）；默认用真实 App 特征（推荐） | 可选 |
| `YIBAN_WORKERS` / `YIBAN_PROXY_LIST` / `YIBAN_PROXY_FALLBACK` / `YIBAN_FALLBACK_ENABLE` / `YIBAN_FALLBACK_INTERVAL` / `YIBAN_CAPACITY_MEASURED` | 多执行体相关（单执行体部署**不需要**配置），见 [多执行体并行签到](#多执行体并行签到可选) 与 [代理配置](#代理配置可选) | 可选 |
| `YIBAN_ADMIN_USER` / `YIBAN_ADMIN_PASSWORD` | 内置主管理员账号（口令策略：至少 12 位且含四类字符中的至少三类） | 必填（Web） |
| `YIBAN_ACCOUNTS_KEY` / `YIBAN_AUDIT_KEY` / `YIBAN_TRACK_SALT` | 账号密文密钥 / 审计链 HMAC 密钥 / 访问统计盐：**首次启动自动生成**并写入 `.env`，一般无需手填 | 自动 |
| `YIBAN_STATE_DIR` / `YIBAN_LOG_FILE` | 状态文件目录（默认 `/var/log/yiban`）与日志路径（按天分文件） | 可选 |
| `YIBAN_DB_FILE` / `YIBAN_ENV_FILE` | 数据库与 `.env` 路径（默认相对路径） | 可选 |
| `YIBAN_BASE_PATH` | Web 挂载前缀，仅在自动识别切错时兜底（见 [部署形态](#部署形态)） | 可选 |

> 调度 v2 的其余内部参数（正态 μ/σ 范围、重试最小间隔等）见代码 `yiban/engine/schedule.py` 的 `_schedule_config()`，网页不展示的项一般无需调整。

### 账号间隔（防风控）与容量预估

账号间隔是相邻两次签到请求之间的最小间隔（秒），用于降低同一 IP 连续登录触发风控的概率，是 [Q1](#q1-账号或密码错误e003)（真实 App 登录特征）之外的纵深防御。**默认 10 秒**，`0`=关闭；自动签到与手动签到均生效。两种调整方式：

1. **网页「系统设置 → 签到调度」**（推荐）：改「账号间隔」后保存（需主管理员密码确认）；
2. **手动编辑 `.env`**：`YIBAN_ACCOUNT_GAP_MAX=10`，保存即可，下次触发自动生效。

**容量预估**（「数据总览 → 账号与用户容量」卡，按当前签到窗口与账号间隔计算）：

```
账号容量 = floor((有效窗口秒数 − 单账号耗时) ÷ (单账号耗时 + 账号间隔)) + 1
```

- **有效窗口** = 原始窗口 − 前后裁剪秒数（被裁掉的秒数不参与签到、不占容量）；单账号耗时默认按 **3 秒**估算，真实网络更慢时用 `YIBAN_AVG_ATTEMPT_SEC` 覆盖。
- **占用口径 = 会发起签到的账号数**（判据 `yiban.store.accounts.signs_in`）：非删除、且审核态已通过（`user_paused` 仍计入，未审核/已拒绝的账号永不签到，不计入）。
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
- 检查配置（不发送任何请求）：`python3 scripts/signin.py --check-config` 或 `python3 -m yiban.cli config`。

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

**默认额度与节流**（网页可改，`0`=不限）：同类告警节流 60 秒；非紧急告警每日 5 条；紧急告警每日 3 条；登录失败告警单独记账（每日 3 条）——分账是为了避免暴力破解类告警烧完当日额度后，审计链异常这类真告警在手机端被静默吞掉。

<details>
<summary>📧 邮箱通知（SMTP）——管理员告警 + 用户签到失败提醒</summary>

邮箱通知与 Webhook **并存**，各配各的：

- **管理员告警邮件**：签到失败、耗时超标、容量超载、登录连续失败锁定等，**签到轮彻底结束后合并成一封**「易班签到汇总」邮件（Webhook 仍即时逐条推送）；
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

</details>

## 网页管理后台

```bash
# 1. 安装依赖
pip3 install -r requirements.lock

# 2. .env 配置管理员账号（否则无法登录后台）
cat >> .env << 'EOF'
YIBAN_ADMIN_USER=admin
YIBAN_ADMIN_PASSWORD=你的密码
EOF
#    ⚠️ 主管理员口令策略：至少 12 位，且包含大写字母 / 小写字母 / 数字 / 符号中的
#    至少三类；常见弱口令或不满足策略会拒绝启动（fail-closed）。首次登录后建议在
#    「我的账号 → 修改主管理员密码」重设：修改会写入哈希并清空 .env 明文、递增
#    PW_VERSION（旧会话全部失效），并触发安全告警。

# 3. 启动（默认只监听回环 127.0.0.1:17892，--host/--port 可改）
python3 -m web
# 生产用 systemd + gunicorn 常驻（禁止 werkzeug dev server 公网直连）：
#   systemd 单元模板 web/deploy/yiban-web.service，nginx 示例 web/deploy/nginx.conf.example
```

浏览器访问 `https://你的域名`（经 nginx 反代）或 `http://127.0.0.1:17892`（本机调试）。**默认不监听全网卡**：确需直连局域网请显式 `python3 -m web --host 0.0.0.0`（明文 HTTP 无防护，自担风险）。

管理员侧导航：**数据总览 / 签到日志 / 账号管理 / 用户管理 / 系统设置**（个人域为 **我的日历 / 我的账号**）。普通用户走邮箱注册，提交自己的易班账号（名称 + 手机号 + 密码 + 设备信息），管理员审核通过后参与每日自动签到；每人限一个账号，可自助注销（两次确认 + 密码验证，7 天内可登录撤销）。

安全设计：登录失败限速（5 次锁定 5 分钟）+ 连续失败 webhook 告警、CSRF 防护、密码 scrypt 哈希、会话 HttpOnly/SameSite、列表手机号/邮箱脱敏、密码明文永不下发前端。

> ⚠️ 无固定域名时建议在云服务商安全组仅放行常用 IP，并定期修改管理员密码。

<details>
<summary>🌐 部署形态：域名根 / 独立子域 / 主站子路径</summary>

### 部署形态

Web 应用**自动适配挂载前缀**，同一份代码可部署在三种位置，无需改代码：

| 部署形态 | 访问地址示例 |
|---|---|
| 域名根 | `https://yiban.example.com/` |
| 独立子域 | `https://sub.example.com/` |
| 主站子路径 | `https://example.com/tools/yiban-auto-sign/` |

**部署契约（务必遵守）**：反向代理把完整 URI【原样透传】给后端——nginx 的 `proxy_pass` 后**不要**加 `/`（加 `/` 会剥掉前缀，自动识别失效）。应用会从请求路径自动识别挂载前缀，登录后的跳转、静态资源、API 请求都会带上正确前缀；不依赖 nginx/Caddy/Apache 的特定配置，直连 17892 也能用。

- 子路径首页请**带尾斜杠访问**；不带尾斜杠的裸路径按 404 处理（避免误伤根路径部署）。
- 若挂载前缀本身包含 `/api`、`/static` 或页面名等会与应用路由撞车的段（极少见），自动识别可能切错，请在 `.env` 显式设置 `YIBAN_BASE_PATH=/你的/前缀` 兜底。
- 静态资源由应用自带 `/static` 提供（含 30 天缓存 + `?v=` 版本号），**无需**为子路径单独配静态代理；追求性能可另配边缘直发（见 `web/deploy/nginx.conf.example`）。
- 代码位置：中间件 `BasePathMiddleware`（`web/app.py`）；前端 `BASE` 变量（三个外壳模板的 `<head>`，经 `partials/theme_boot.html` 注入）。

</details>

<details>
<summary>🎨 自定义 Web 图标（logo / favicon）</summary>

品牌图标**优先读取 `web/static/vendor/logo.png`**：文件存在即显示；不存在时自动回退为占位符（内联 SVG 蓝色圆角块 +「签」字，无版权资源）。**开源仓库不含 logo.png**（版权来源不明），自己部署时把图标文件放到该位置即可，无需改代码。

浏览器标签页图标：把 `favicon.png` 放入同一目录，机制相同（不存在则用浏览器默认图标）。建议 32×32 左右的 PNG；页面以约 1 小时短缓存引用，换图后无需改代码。

> 请确保你放入的图标有使用权（仓库不附带任何品牌图标资源）。

</details>

<details>
<summary>📜 合规文档接入（隐私政策 / 用户协议）</summary>

网页的《隐私政策》《用户协议》**读取仓库根目录的 `PRIVACY_POLICY.md` 与 `USER_AGREEMENT.md`**：文件存在即显示内容，缺失或留空时回退为"该文档尚未发布，请联系运营者"占位提示。

开源仓库内这两份文档是**只含说明注释的空模板**（涉及每个部署者真实运营者信息的内容不适合在公开仓库写死），自己部署时直接编辑这两个文件即可，无需改代码。站内展示位置：登录页注册勾选弹窗、`/terms`、`/privacy`；代码位置：`web/app.py` 的 `_DOC_FILES` / `_read_doc_html()` / `_render_md()`。

</details>

## 运维

### 备份与恢复

`scripts/backup.sh`（建议 cron 每日 02:00）做四件事：`sqlite3 .backup` 一致性快照 → **快照必须过 `PRAGMA integrity_check` 才算这次备份成功**（源库本身损坏时归档照留但非 0 退出；`.backup` 失败回退 `cp` 且校验不过时不落该归档——"看着有备份"比没备份更危险）→ 本地归档**默认加密**（明文落盘需显式 `BACKUP_PLAINTEXT=1`）→ 本地与异机各保留 30 天（`.sha256` 侧车一起轮转）→ 可选异机加密副本（`REMOTE_BACKUP`）。

包内内容除 `yiban.db`、`.env`、密钥文件外，还含**当日闸门标记与账本**（`sched-run-*`/`sched-slot-*`/`sched-snapshot-*`/`notify-ledger.json`/`notify-throttle.json`/`cred-state.json`）与**审计链外部锚点** `audit-anchor.log`：缺前者恢复当天会重签或漏签、告警日额度被重置；缺后者恢复出来的库就再也验不了"删尾/删前缀/整表清空"。`--restore` 解包后会自动跑 `integrity_check` 与 `audit_verify.py`（链 + 锚点 + 欠账）并带回结论，同时提示"先停服再覆盖"与"必须删除残留 `-wal`/`-shm`"。

```bash
# 备份（安装到 /usr/local/sbin 后用 root crontab 调用；--require-encrypt 不可省：
# 不带时一旦加密配置失效，cron 会静默产出含全部密钥与口令哈希的明文归档）
sudo /usr/local/sbin/yiban-backup.sh --require-encrypt
# 每日取证校验（锚点判据不能只挂在 web 的每日线程上——web 没起来就永远没人查）
30 2 * * * cd /opt/yiban-auto-sign && python3 scripts/audit_verify.py --db yiban.db --env .env >> /var/log/yiban/audit-verify.log 2>&1
# 恢复演练 / 真实恢复（支持 .tar.gz / .gpg / .age）
bash scripts/backup.sh --restore <备份包> <目标目录>
```

> ⚠️ **异机副本默认未启用**：`REMOTE_BACKUP` 不配置时备份仅存本机——root 失陷时攻击者可一并清掉 `/var/backups` 下的备份（备份随主机同灭）。恢复口令存于 `/etc/yiban/backup-passphrase`（0600，仅 root 可读），**该口令文件必须另行离机保存一份**（密码管理器/离线介质），否则主机损毁 = 备份与口令同灭、密文不可恢复。需要异地容灾时配置 `REMOTE_BACKUP`（见脚本头部说明）。

> ⚠️ 加密密钥（`.env` 的 `YIBAN_ACCOUNTS_KEY`）与数据分开备份——密钥丢失 = 已加密账号密码不可恢复。
>
> ℹ️ 数据库结构由启动时自动迁移（老数据平滑升级）；升级前仍建议先跑一次备份。

**取证与排障工具**：

```bash
python3 scripts/audit_verify.py --db /opt/yiban-auto-sign/yiban.db --env .env --anchor /var/log/yiban/audit-anchor.log
    # 链自洽 + 库外锚点比对 + 审计写入欠账三件一起跑；0=健康 1=检出删除/篡改 2=无法定论
python3 scripts/list_duplicate_owners.py                            # 列出"同一用户多个未删除账号"（人工清理后自动恢复一人一号约束）
python3 scripts/db_export.py --out /tmp/export                      # 导出回 JSON（降级/迁移用）
```

### 状态文件清理

按天产生的文件（签到日志、按日结构化状态、用户失败提醒额度账本、调度快照等）由统一策略清理，**宿主与容器共用一套规则**——新增一类按日文件会被检查拦下，状态目录不会逐日累积：

```bash
bash scripts/yiban-cleanup.sh        # 薄包装，宿主 cron 调用（策略唯一在 yiban/state_gc.py）
python3 -m yiban.cli state           # 默认 dry-run，列出将删除的文件；加 --yes 才动手
```

保留期用环境变量配：`YIBAN_RETENTION_DAYS`（日志与可回看的状态文件，默认 `365`）、`YIBAN_SNAPSHOT_RETENTION_DAYS`（只在近几天有意义的标记/账本，默认 `7`）。清理结果追加到 `<状态目录>/cleanup.log`（运维按它判断清理是否在跑）。

> ℹ️ 清理**绝不能**写签到日志：root 预创建当日签到日志（umask 077）会让 `run.sh` 全部重定向失败、签到静默不执行。

### 多执行体并行签到（可选）

<details>
<summary>🚀 什么时候需要、怎么开、怎么配出口</summary>

**先说结论**：几十到几百个账号**不需要**这一节——单执行体（默认）就够了。什么时候需要，用你自己的机器量：

```bash
python3 -m yiban.cli capacity     # 按当前窗口/间隔给容量与建议执行体数（不发请求）
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
> **偏小、据此换算的容量偏乐观**。**正式定档请用测试机上的基准**（假易班跑完整链路、延迟可控），
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
python3 scripts/signin.py --workers 4     # 手动拉起一轮试跑（打印每个执行体的出口与结果）
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

> 上面说的是**服务器（裸机/systemd）部署**。容器部署目前**还没有**对应的常驻方式（容器调度器未改动）——容器用户暂时只能手工在容器内跑 `python -m yiban.cli sign --fallback`。

**怎么确认在跑**（三条，任选）：

- ① 日志：窗口内应有"兜底执行体启动"与每轮的"本轮处理 N 个账号"——`grep 兜底 /var/log/yiban/sign-$(date +%F).log`；
- ② 心跳文件 `/var/log/yiban/fallback-alive.json`：内容时间戳应在一两个扫描间隔内（进程被 `kill -9` 也能靠它识别）；
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

**想量一量这台机器能带多少账号**（隔离测试机上）：

```bash
sudo python3 scripts/loadtest/capacity_probe.py --repo /opt/yiban-auto-sign --users 5000
```

它会自建假易班（不连真实易班，跑完自动还原环境），打印实测的「单账号周期 / 单执行体容量 / 建议每执行体账号数（实测 × 2/3）/ 需要几个执行体 / 本机实测可同时跑几个执行体」。**换机器、换网络都要重新量**——这是建议值，不是程序上限。

</details>

### 常用运维命令

```bash
# 查看今天签到日志（按天分文件）
tail -50 /var/log/yiban/sign-$(date +%F).log

# 清理过期数据（保留期见「状态文件清理」）
scripts/yiban-cleanup.sh
python3 -m yiban.cli state --json          # 机器可读的清理结果（默认 dry-run）

# 手动触发签到
bash /opt/yiban-auto-sign/run.sh

# cron 与 Web 服务
systemctl status cron
crontab -l
sudo systemctl restart yiban-web.service   # 只改了 .env 也必须重启（配置在启动时读取）

# 更新代码（**整仓更新**：代码已拆到 yiban/ 多个模块，单文件覆盖会漏文件）
cd /opt/yiban-auto-sign
git pull --ff-only          # 首次用 git clone 部署才有 .git；压缩包部署请重新上传覆盖
```

> 面向脚本/agent 的统一入口是 `python3 -m yiban.cli <子命令>`（`sign` / `probe` / `config` / `capacity` / `state` / `db` / `version`，支持 `--json`、非交互、稳定退出码）；`scripts/signin.py`、`scripts/db.py`、`scripts/state_cleanup.py` 是部署面的兼容壳，行为同源。完整契约见 [`docs/dev/cli.md`](docs/dev/cli.md)——本文只给人类用法，不重复契约细节。

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

## 原理与实现

<details>
<summary>🔬 展开：模块树 / 签到流程 / 定位算法 / 重试机制 / WAF 检测 / 续期</summary>

### 项目架构

```
web/            Flask 管理后台（账号管理/审核/用户管理/日历/手动签到）
   │
   ├── scripts/signin.py        兼容壳 → yiban/engine/（签到引擎）
   ├── scripts/db.py            兼容壳 → yiban/store/db.py（SQLite 连接与迁移）
   └── yiban/                   共享包（web 与签到引擎共用）
            ├── cli.py           统一命令行入口（`python -m yiban.cli <子命令>`）
            ├── clock.py         业务时间唯一入口（北京时间）
            ├── window.py        签到窗口唯一事实源（排计划/判关闭/算容量同源）
            ├── status.py        签到状态词汇表
            ├── masking.py       脱敏（手机号/日志文本/URL）
            ├── security.py      风控拦截判定 + 跳转白名单
            ├── egress.py        出口分配与执行体身份串（单/并行/兜底三类角色的唯一口径）
            ├── client.py        易班客户端外观（凭据/会话缓存/代理/设备绑定）
            ├── state_gc.py      按日状态文件的保留期策略与清理
            ├── logging_ext.py   日志落盘（跨进程互斥 + 按天滚动）
            ├── fyiban/          ★ 第三方隔离层（易班协议与定位算法，来源见其 PROVENANCE.md）
            ├── infra/           叶子工具：文件锁 / .env 读写 / 凭据加密
            ├── engine/          签到引擎（按"执行一轮"切分）：runner 编排 / round 队列重试
            │                     / schedule 排期与容量 / attempts 单账号尝试 / probe 探针
            │                     / alerts 告警与邮件 / state_io 状态文件 / accounts 装载
            │                     / workers 多执行体
            ├── store/           表级数据访问（db.py 连接与迁移、claims 签到分工记录表）
            ├── notify/          通知推送（配置 / 额度账本 / 发送）
            ├── mail/            告警邮件（配置 / 发送）
            └── attempt/jobs.py  在线校验异步任务（排队/看门狗/收口）
```

> 依赖方向单向：`web` / `scripts` → `yiban`（`yiban` 不反向依赖调用方）。
> 单文件规模目标 600 行，超出目标者须在 `tests/test_module_size_gate.py` 写明工程理由。
> 命令行有两条等价通道：人类按本文的命令（`bash run.sh`、`python3 scripts/signin.py ...`）照旧可用；统一入口与机器可读输出见 [`docs/dev/cli.md`](docs/dev/cli.md)。

### 签到流程

```
登录易班 (OAuth + RSA 加密)
    ↓
获取签到任务范围 (nightAttendance/signPosition)
    ↓
解析签到多边形 Points
    ↓
在多边形内生成随机定位点（缩放质心算法）
    ↓
提交签到 (nightAttendance/signIn)
```

### 定位生成算法

使用与 [OneFeiFan/FYIBAN](https://github.com/OneFeiFan/FYIBAN)（AGPL-3.0）一致的**缩放质心算法**（射线法校验，感谢原作者开源）：

1. 解析签到范围返回的多边形顶点 `Points`；
2. 计算多边形质心 `(center_lng, center_lat)`；
3. 将多边形顶点向质心收缩 0.7 倍，得到 `scaled_polygon`；
4. 在质心附近的边界框内随机生成点（最多 5000 次尝试）；
5. 校验点是否同时在 `scaled_polygon` 和 `original_polygon` 内；
6. 若 5000 次均未命中，兜底返回质心。

每次签到的定位点都不同，但都落在有效范围内，避免被识别为异常定位。

### 重试机制

失败账号放回队尾分散重试，同账号两次尝试间隔不小于 60 秒；每次重试附加 0~30 秒随机延迟。尝试次数按失败类型分档：

| 失败类型 | 最多尝试 |
|---|---|
| 普通（网络/超时/临时错误） | 3 次（1 初始 + 2 重试） |
| 风控类（WAF 拦截） | 2 次（1 初始 + 1 重试） |
| 密码错误 / 易班账号锁定 | 1 次（不重试——确定性失败，重复尝试只会加速易班侧锁定账号） |
| 会话陈旧（登录状态被服务端作废） | 2 次（1 初始 + 1 重试，并清除缓存重新登录） |
| 易班未返回签到点位 | 1 次（不重试，属任务未配置/当日已关闭，与账号无关） |

### WAF 拦截检测

易班 WAF 风控会返回包含「风险访问服务禁用」等关键词的页面。检测方式：

- **长度检查**：响应超过 2000 字符直接判定为非拦截（正常页面通常较长）；
- **关键词匹配**：检测「风险访问」「风控」「访问服务禁用」「WAF」「拦截」；
- **Unicode 解码**：WAF 返回 JSON 时中文会被 `\uXXXX` 转义，先解码再匹配。

### 定时工作流自动续期

GitHub 官方政策：**仓库连续 60 天无活动，定时工作流会被自动禁用**。工作流里集成了 [`liskin/gh-workflow-keepalive@v1`](https://github.com/liskin/gh-workflow-keepalive)：每次定时触发签到时，keepalive job 通过 GitHub API 检查并重新启用被禁用的工作流，重置 60 天计时器——无需额外 commit，不污染提交历史。

</details>

## 常见问题

### Q1 账号或密码错误（e003）

> ✅ **已修复**：默认登录方式已改为参考 fyiban 的真实 App 请求特征。

**根因**：旧登录流程沿用开源项目 Auto-Test 的请求特征（伪造 iPhone UA + `X-Requested-With: com.yiban.app` + 可预测 CSRF），被易班风控识别为**非官方客户端**，对登录接口统一返回 `e003 账号或密码错误` 伪装拒绝。它与 IP、账号、密码、设备信息均无关——实测手机流量 IP + 新账号同样 e003，而同一网络下手机 App 正常。

**修复方式**：登录改为 fyiban 同款流程（UA=`Yiban` + `AppVersion` + 真随机 CSRF + `scope` 空 + `display=authorize` + usersure 不带 Origin 头），新旧账号均恢复正常。旧流程保留，可用 `YIBAN_LEGACY_LOGIN=1` 切回。版本号现值见 `yiban/fyiban/headers.py` 的 `YIBAN_APP_VERSION`。

**排查顺序（老版本或自行改回旧流程时参考）**：

1. **用手机易班 App 登录一次**——能正常登录说明账号和密码都没问题；
2. **对照实验**：临时用 `.env` 旧格式（`YIBAN_PHONE`+`YIBAN_PASSWORD`）再跑一次——新旧方式同时报错即可排除配置问题；
3. **确认触发源**：检查同一账号当天是否被多个 IP 尝试过（如 Actions 海外 IP 定时签到失败重试）；
4. **等待冷却**：风控冷却通常几小时到 24 小时，**期间不要反复重试**（会延长冷却）。

> 💡 **预防**：避免同一账号叠加多路定时签到（如 Actions + 服务器同时跑）。推荐以国内服务器为唯一签到通道。


### Q2 获取签到任务失败

- 你的学校可能未开启晚间考勤（`nightAttendance`）任务，该接口仅适用于开启「晚间考勤 / 晚签到」的学校；
- 如需打卡的是「每日打卡」（`officeTask`），需修改脚本中的 API 路径。


### Q3 未在签到时间内

- 当前时间不在管理员设置的签到窗口内；
- Actions 的触发延迟（实测约 55–120 分钟）可能导致实际执行时超出窗口，可调整 `.github/workflows/signin.yml` 的 `cron`，或等下一次触发；
- 此错误**不会**让 Actions 标记为失败（退出码仍为 0）。


### Q4 风险访问服务禁用 / WAF 拦截

- **原因**：GitHub Actions 的海外 IP 被易班 WAF 拦截；
- **解决**：改用 [服务器部署](#服务器部署分步详解)（国内出口）；如确需 Actions，可配置 `YIBAN_PROXY`（见 [代理配置](#代理配置可选)）；
- 脚本按失败类型自动重试（见 [重试机制](#重试机制)），仍失败则标记为错误；
- ⚠️ 海外 IP 的反复失败可能让易班把**账号**标记为可疑，连带影响服务器签到（表现为 e003，见 [Q1](#q1-账号或密码错误e003)）。已有国内服务器时，**建议在 Actions 页面禁用该工作流**。


### Q5 ydclearance 反爬

- 此前依赖 `js2py` 在本机执行网页 JS 破解该反爬，但 js2py 存在无修复版本的沙箱逃逸漏洞 CVE-2024-28397，已移除——默认登录流程改为真实 App 请求特征，正常情况下不会走到该反爬页面；
- 若仍出现，可能是出口 IP 被风控，请改用服务器部署。


### Q6 请使用授权设备进行签到

- **原因**：学校在校本化后台开启了「设备绑定」，签到时会校验设备型号与唯一识别码；
- **解决**：配置 `YIBAN_PHONE_MODEL` 与 `YIBAN_PHONE_CODE`（Actions 中为 Secrets），获取方式见 [设备绑定](#设备绑定可选)。


### Q7 签到成功但 Actions 显示失败

- 检查日志中是否有 `❌` 标记的账号：多账号下只要有一个**真正的失败**（非"未在签到时间内"），整体退出码就是 1；
- "未在签到时间内"不会导致失败（属正常行为）。


### Q8 定时任务不执行 / 突然停止

- 进入 `Actions` 页面确认工作流是否被禁用（被禁用会有醒目提示）；
- 点击 `Enable workflow` 重新启用；keepalive 会在下次定时触发时自动处理，但首次需手动启用。


### Q9 查看签到历史

- Actions：进仓库 `Actions` 标签页 → 左侧 `Yiban Sign-in` → 历史运行记录与详细日志；
- 服务器部署：网页「签到日志」页（按日期查看/检索/导出），或直接看 `/var/log/yiban/sign-YYYY-MM-DD.log`。


### Q10 Actions 会不会触发风控

**会**。Actions 使用 GitHub 海外 IP，每次定时尝试登录都会被易班 WAF 拦截（报"风险访问服务禁用"）；更麻烦的是**反复失败可能让易班把账号标记为可疑，连带影响国内服务器签到**（服务器随后出现 e003 伪装"密码错误"，见 [Q1](#q1-账号或密码错误e003)）。

**建议**：

- 已有国内服务器签到 → **在 Actions 页面禁用工作流**（Actions → Yiban Sign-in → ⋯ → Disable workflow），让服务器成为唯一签到通道；
- 没有服务器、必须用 Actions → 配置 `YIBAN_PROXY`（见 [代理配置](#代理配置可选)），并避免与其他签到通道叠加同一账号；
- 恢复 Actions：同一位置 `Enable workflow`。


## 注意事项

> **🔐 请自建部署，不要使用他人的公开实例**：本工具会保存易班账号密码（加密存储）、手机号、设备识别码等敏感数据。使用不明来源的公开实例 = 把易班账号和密码交给陌生人，对方可能解密查看甚至冒用签到。本项目为 AGPL 开源、自建成本很低，**请务必自行部署、自己掌控数据**；如发现有人运营公开实例收集账号，请提醒使用者注意风险。

1. **本项目仅供学习研究使用**，完整免责声明见 [AI 生成说明](#ai-生成说明)
2. **强烈建议仓库设为 Private**（Fork 场景），避免账号密码被搜索引擎索引
3. 账号密码只写在服务器本地 `.env` 或 GitHub Secrets，**不要写进代码、不要提交进仓库**
4. 请勿频繁调用 API（默认每天 2 次足够），以免触发风控
5. 如账号开启了二次验证，可能需要额外处理
6. **推荐国内服务器为唯一签到通道**：Actions 海外 IP 会被 WAF 拦截，其反复失败尝试可能连带触发账号风控（表现为 e003"密码错误"），进而影响服务器签到——有服务器时建议禁用 Actions 工作流（详见 [Q10](#q10-actions-会不会触发风控)）
7. **遇到"账号或密码错误"先别改密码**：默认登录方式已修复此问题（见 [Q1](#q1-账号或密码错误e003)）；若仍出现，先用手机 App 验证账号正常，再检查是否切回了旧流程（`YIBAN_LEGACY_LOGIN=1`）

## 测试范围与适配说明

<details>
<summary>🧪 已测试环境 / 运行测试 / 未覆盖场景</summary>

**已测试环境**：Ubuntu 22.04 服务器部署、Python 3.10+。

### 运行测试

```bash
python -m pytest tests/ -q                 # 全量（串行）
python -m pytest tests/ -q -n auto         # 并发（需 pytest-xdist）

# 按功能域运行（文件前缀 = 功能域）
python -m pytest tests/test_notify_*.py    # 消息推送与告警
python -m pytest tests/test_web_*.py       # 网页管理后台
python -m pytest tests/test_scheduler*.py  # 调度器
python -m pytest tests/test_signin*.py     # 签到核心
python -m pytest tests/test_db_*.py        # 数据库与迁移

python -m pytest tests/test_smoke.py -v    # 单个文件
```

测试文件按**功能域**命名（`test_<功能>.py`），便于按需运行与定位；完整清单见 [`tests/README.md`](tests/README.md)。`testpaths = ["tests"]` 由 `pyproject.toml` 配置，`scripts/` 下的独立压力测试不参与默认收集。

**未覆盖场景**（请自行验证）：

- **Docker / Windows 部署**：未测试（仅 Ubuntu 验证）
- **消息通知**（Server 酱 / Bark / 企业微信）：代码已实现并测试，但未在生产环境实测通知链路，建议配置后自行验证
- **多学校适配**：易班校本化签到因学校而异（接口与任务类型可能不同）。本项目**仅在南京工程学院（NJIT）实测**，其他学校可能需要适配
- **多校混合账号**：未测试同一实例下多校账号混合签到

如你在其他学校使用成功或有适配需求，欢迎提交 Issue 或 PR。

</details>

## License

**GNU Affero General Public License v3.0（AGPL-3.0）** - 见 [LICENSE](LICENSE)

### 核心条款简述

1. **网络服务强制开源**：通过网络（网站 / API）向用户提供服务时，必须向服务使用者提供完整的源代码
2. **强制传染**：任何使用、修改或分发本项目的衍生作品，必须同样以 AGPL-3.0 协议开源
3. **署名要求**：必须保留原作者版权声明与许可声明

<details>
<summary>📦 第三方组件声明与衍生来源</summary>

### 第三方组件声明

本项目分发包含以下第三方开源组件（按各自许可保留版权声明）：

| 组件 | 用途 | 许可证 |
|------|------|--------|
| Flask / Werkzeug | Web 框架 | BSD-3-Clause |
| requests / urllib3 | HTTP 客户端 | Apache-2.0 / MIT |
| PySocks | SOCKS 代理 | MIT |
| pycryptodome | AES-GCM 加密 | Public Domain（作者声明）+ BSD-2-Clause 条款 |

各组件版权声明与完整许可文本见其官方仓库 LICENSE 文件。本项目仅按各自许可条款使用，未修改上述组件源码。

### 衍生来源

本项目直接参考 [OneFeiFan/FYIBAN](https://github.com/OneFeiFan/FYIBAN)（AGPL-3.0）实现：

- 多边形内随机定位点算法（缩放质心 + 射线法验证）
- 易班登录特征与 nightAttendance 签到流程

> 披露：OneFeiFan/FYIBAN 在其 README 中声明参考了 [Qs315490/fyiban](https://github.com/Qs315490/fyiban)（无许可证，上游 Sricor/yiban 已删库）。本项目未直接使用上述无许可证项目的代码，直接参考对象为 FYIBAN（AGPL-3.0），并按 AGPL-3.0 条款发布。

**改了什么（AGPL-3.0 §5(a) 要求的修改声明）**：复用部分已由 Kotlin 重写为 Python 并做了如下修改——定位采样由正态分布改为密码学安全随机的均匀分布并加质心抖动兜底，射线法补零除保护，登录侧新增会话缓存探活、URL 白名单、风控页面识别与日志脱敏；**其余部分（调度错峰、重试预算与失败分级、账密熔断、通知告警、账号与数据库、Web 管理后台、多执行体并行）为本项目原创**，上游无对应实现。逐项对照见[开源致谢](#开源致谢--acknowledgements)的「衍生来源」小节。

</details>

### 特别免责声明

- **滥用与盈利免责**：本项目按"原样"提供。任何使用本项目进行商业或非商业行为时，若因违反当地法律法规、滥用功能（包括但不限于网络攻击、诈骗等非法用途）而产生任何形式的刑事或民事纠纷，均与本项目作者无关，使用者需自行承担所有法律后果
- **无担保**：作者不保证本项目的适用性、稳定性或无错误（Bug）
- **损失免责**：因使用或无法使用本项目而导致的任何直接、间接、偶然或后果性损害（包括数据丢失、业务中断、利润损失等），作者不承担任何责任

## 开源致谢 / Acknowledgements

本项目的签到引擎与网页管理后台建立在一批优秀开源组件之上。以下按「名称 / 用途 / 许可证」列出实际使用的第三方组件；完整版权声明与许可文本见各组件官方仓库 LICENSE，前端资源的来源、版本与哈希另见 [`web/static/vendor/MANIFEST.md`](web/static/vendor/MANIFEST.md)。

### 网页前端（自托管于 `web/static/vendor/`，不引用任何外网 CDN）

| 组件 | 用途 | 许可证 |
|------|------|--------|
| [Adminator](https://github.com/puikinsh/Adminator-admin-dashboard) 4.3.0 | 全站设计系统：设计令牌（含暗色）、外壳布局、组件样式 | MIT |
| [Chart.js](https://www.npmjs.com/package/chart.js) 4.5.1 | 数据总览页折线 / 柱状 / 环形图表（按需注册的裁剪构建） | MIT |
| [Lucide](https://github.com/lucide-icons/lucide) 1.45.0 | 全站线性图标（70 个 symbol 子集；部分图标源自 Feather） | ISC（Feather 部分 MIT） |
| [Inter](https://github.com/rsms/inter) / [Noto Sans SC](https://fonts.google.com/noto/specimen/Noto+Sans+SC) / [JetBrains Mono](https://www.jetbrains.com/lp/mono/) | 自托管界面字体：拉丁 / 中文 / 等宽 | SIL OFL-1.1 |
| md-render.js | 更新日志的迷你 Markdown 渲染（本项目自研、零依赖，非第三方组件） | 随本项目 AGPL-3.0 |

### 后端与运行依赖（Python）

| 组件 | 用途 | 许可证 |
|------|------|--------|
| [Flask](https://flask.palletsprojects.com/) / [Werkzeug](https://werkzeug.palletsprojects.com/) / [Jinja2](https://jinja.palletsprojects.com/) / [click](https://click.palletsprojects.com/) / [itsdangerous](https://itsdangerous.palletsprojects.com/) / [MarkupSafe](https://markupsafe.palletsprojects.com/) / [blinker](https://github.com/pallets-eco/blinker) | Web 框架及其生态（路由 / WSGI / 模板 / CLI / 会话签名 / 转义） | BSD-3-Clause（blinker 为 MIT） |
| [gunicorn](https://gunicorn.org/) | 生产常驻 WSGI 服务 | MIT |
| [requests](https://requests.readthedocs.io/) | HTTP 客户端 | Apache-2.0 |
| [urllib3](https://urllib3.readthedocs.io/) / [charset-normalizer](https://github.com/Ousret/charset_normalizer) | HTTP 底层与字符集探测 | MIT |
| [certifi](https://github.com/certifi/python-certifi) | CA 根证书包 | MPL-2.0 |
| [idna](https://github.com/kjd/idna) | 国际化域名编码 | BSD-3-Clause |
| [PySocks](https://github.com/Anorov/PySocks) | SOCKS 代理（可选，`YIBAN_PROXY`） | BSD |
| [pycryptodome](https://www.pycryptodome.org/) | AES-GCM 账号凭据加密 | Public Domain + BSD-2-Clause |
| [colorama](https://github.com/tartley/colorama) | Windows 终端颜色（传递依赖） | BSD-3-Clause |

> 精确锁定版本见 [`requirements.lock`](requirements.lock)。

### 衍生来源（2026-09-15 逐项核对）

上游 [OneFeiFan/FYIBAN](https://github.com/OneFeiFan/FYIBAN) 是一个 **Kotlin/Android 库**（AGPL-3.0，约 670 行，作者 OneFeiFan）。本项目**没有复制其代码**（语言不同），而是按其算法与协议在 Python 中重写易班客户端。逐项对照如下（"改写"= 本地已按自己的实现重做）：

| 能力 | 上游实现 | 本项目 | 判定 |
|------|---------|--------|------|
| App 请求指纹（UA `Yiban` / AppVersion / Origin） | `Core/SchoolBased.kt` | `yiban/fyiban/headers.py` | 源自上游（版本值已更新） |
| CSRF 随机令牌 | `Core/SchoolBased.kt` | `yiban/fyiban/protocol.py` | 源自上游（改为每次实例重生成） |
| 校本化 OAuth 五步登录（`oauth.yiban.cn/code/html` → `code/usersure` → iframe → `verify_request` → `base/c/auth/yiban`）与全部请求常量 | `Core/SchoolBasedAuth.kt` | `yiban/fyiban/protocol.py` + `yiban/client.py` | 源自上游，本地改写（新增会话缓存分支、URL 白名单、风控识别、脱敏） |
| 密码 RSA/PKCS1v1.5 加密 | `Core/SchoolBasedAuth.kt` | `yiban/fyiban/protocol.py` | 源自上游（补长度守卫） |
| 登录成功判据 `code == "s200"` | `Core/SchoolBasedAuth.kt` | `yiban/fyiban/protocol.py` | 源自上游 |
| `nightAttendance` 的 `signPosition` / `signIn` 请求构造 | `Core/TaskFeedback.kt` | `yiban/fyiban/protocol.py` | 源自上游，本地改写（多任务遍历、Range 缺失、状态机化） |
| 缩放质心 + 射线法定位点算法 | `tool/Point.kt` | `yiban/fyiban/algo.py` | 源自上游，本地改写（见下） |
| **定位采样分布** | Box-Muller 正态分布（可能取到范围外的点） | 密码学安全随机的**均匀分布** + 质心抖动兜底 | 本地改写 |
| **调度与错峰**（时间窗分块、锚点/σ、重试落点） | 无 | `yiban/engine/schedule.py` + `yiban/engine/round.py` | 本地原创 |
| **重试预算与失败分级、账密熔断、健康探针** | 无（上游仅 HTTP 层 `retryOnConnectionFailure`） | `yiban/engine/attempts.py` + `yiban/engine/probe.py` | 本地原创 |
| **通知告警**（webhook / 管理员汇总邮件 / 用户失败提醒） | 无 | `yiban/notify/`、`yiban/mail/`、`yiban/engine/alerts.py` | 本地原创 |
| **账号存储、会话缓存、审计、Web 管理后台、多执行体并行** | 无（示例里凭据硬编码，单账号） | `yiban/`、`web/`、`scripts/db.py` | 本地原创 |

上游仓库内没有任何调度、通知、Web 或数据库代码（可自行核对：其全库无 Python 文件，且除 `retryOnConnectionFailure` 外无定时/重试实现）。

**许可与署名**：上游与本项目同为 **AGPL-3.0**（同一版本），本项目按 §5(a) 保留许可声明并在本节声明修改内容、按 §5(c) 以 AGPL-3.0 授权下游。上游未在文件头或 LICENSE 中填写具体版权行，故署名只能标注项目名与作者身份（OneFeiFan）；若上游后续补充版权声明，本项目亦应同步补入。

### 参考项目与资料

- [AEtherside/skland-daily-attendance](https://github.com/AEtherside/skland-daily-attendance) - GitHub Actions 工作流结构与 keepalive 方案
- Auto-Test - 易班登录流程（OAuth + RSA + ydclearance，已弃用并被本项目新登录特征取代）
- [liskin/gh-workflow-keepalive](https://github.com/liskin/gh-workflow-keepalive) - 定时工作流自动续期（避免 60 天无活动被禁用）

特别感谢 [Lumjiel](https://github.com/Lumjiel) 对本项目的指导。

## 相关开源项目推荐

易班签到生态中的其他开源方案（均含开源许可证，可放心参考）：

- [2117516450/yiban_signin](https://github.com/2117516450/yiban-signin)（易签，Unlicense）- 易班校本化早签/晚签打卡，多用户 + 多线程 + Server酱推送
- [Qs315490/YiBan_AutoSgin](https://github.com/Qs315490/YiBan_AutoSgin)（GPL-2.0）- 易班校本化晚点签到脚本（含活跃 fork：[Lumjiel/YiBan_AutoSgin](https://github.com/Lumjiel/YiBan_AutoSgin)）
- [OneFeiFan/FYIBAN](https://github.com/OneFeiFan/FYIBAN)（AGPL-3.0）- 易班 API 安卓库，校本化 OAuth 登录与签到（本项目定位算法与登录特征参考来源）

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
