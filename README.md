# 易班自动签到

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)

## 项目介绍

> 易班（yiban）是很多高校学生用的校园社交平台，部分学校要求每天早晨进行「早操签到」打卡。
>
> 本项目通过 Python 脚本自动完成这个签到过程——配置一次后，每天到点自动帮你打卡，无需手动操作、无需保持电脑开机。
>
> 支持 **国内云服务器** 与 **GitHub Actions** 两种部署方式；提供 **网页管理后台** 与 **终端面板（TUI）** 两种管理界面。


- 🤖 **全自动签到**：每天定时执行，无需人工干预
- 🔐 **真实 App 登录特征**：登录流程参考 [OneFeiFan/fyiban](https://github.com/OneFeiFan/FYIBAN) 的真实 App 请求特征（UA=Yiban + AppVersion + SecureRandom CSRF），实测绕过易班风控 e003，新旧账号均稳定登录
- 🖥️ **网页管理后台**：管理员在任意设备（手机/平板/电脑）登录管理——账号 CRUD/排序/手动签到、审核用户提交的账号、用户管理与权限分级、批量操作、全局公告、签到日志与日历
- 🗄️ **SQLite 数据库存储**：账号与用户数据存于 SQLite（0.17+）——多人同时操作不互相覆盖、手机号全局唯一有保障；关键管理操作自动审计留痕
- 📍 **智能定位**：在签到范围内生成随机定位点，模拟真实 GPS（缩放质心算法）
- 👥 **多账号支持**：一个仓库管理多个易班账号，顺序执行 + 队列重试（失败账号放队尾分散重试，风控类≤2次/其他≤4次）
- 🔔 **消息通知**：签到失败时推送通知（Server 酱 / Bark / 企业微信等）
- 🆓 **完全免费**：使用 GitHub Actions 免费额度，每月消耗约 60 分钟（远低于 2000 分钟配额）
- ⏰ **自动续期**：内置 `gh-workflow-keepalive`，定时工作流自动续期，避免被 GitHub 60 天无活动禁用
- 🔄 **队列重试**：失败账号不立即重试，放回队尾分散重试，避免连击触发风控

## 目录

- [项目介绍](#项目介绍)
- [部署方式选择](#部署方式选择)
- [服务器部署教程](#服务器部署教程)
- [网页管理系统](#网页管理系统)
- [GitHub Actions 使用教程（备选）](#github-actions-使用教程备选)
- [配置说明](#配置说明)
- [数据存储与备份](#数据存储与备份)
- [本地调试](#本地调试)
- [原理与实现](#原理与实现)
- [常见问题](#常见问题)
- [注意事项](#注意事项)
- [测试范围与适配说明](#测试范围与适配说明)
- [License](#license)
- [致谢](#致谢)
- [相关开源项目推荐](#相关开源项目推荐)
- [AI 生成说明](#ai-生成说明)

## 部署方式选择

本项目支持两种部署方式，**推荐使用国内云服务器作为主力签到通道**：

| 部署方式 | 适用场景 | 稳定性 | 成本 |
|---------|---------|--------|------|
| **云服务器**（推荐） | 有国内服务器；主力签到通道 | 稳定（国内出口不被 WAF 拦截） | 需服务器费用 |
| **GitHub Actions**（备选） | 无服务器；或作为冗余备份 | 受 WAF 拦截影响，不稳定 | 免费 |

> ⚠️ GitHub Actions 的海外 IP 可能被易班 WAF 风控拦截，且反复失败可能触发账号风控。**有云服务器时强烈建议使用服务器部署**。

### 适用规模（建议先读）

本项目是**自托管工具（self-hosted）**，面向个人/小团体自建使用，典型部署画像：

- 单站点使用人数 **100 人以内**、管理员 **10 人以内**
- 大概率所有账号使用**相同签到窗口**（06:30–07:50，窗口内错峰由随机延迟打散）

该规模下当前架构（cron + 单脚本顺序执行 + 队列重试）完全够用，资源需求很低（1 核 1G 小服务器即可）。若预期单站点账号超千级、或需要"不同账号不同签到时间/一天多时段"等能力，可联系维护者咨询架构演进方案。


- GitHub Actions 因 WAF 拦截无法稳定运行
- 希望随时手动触发签到或查看日志
- 已有国内云服务器资源



| 特性 | GitHub Actions | 自有服务器 |
|------|---------------|-----------|
| 成本 | 免费（2000 分钟/月） | 需服务器费用 |
| 稳定性 | 受 WAF 拦截影响 | 本机代理更稳定 |
| 调试 | 仅看日志 | 可即时调试 |
| 触发延迟 | 5-30 分钟 | 即时执行 |
| 维护 | 配置后免维护 | 需维护服务器 |

</details>

---


## 服务器部署教程

### ⚡ 快速部署（3 分钟）

```bash
# 1. 服务器环境（Ubuntu 22.04 已含 python3）——只需一次
apt update && apt install -y python3-pip
pip3 config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

# 2. 拉取代码（服务器为主力签到，推荐国内网络直拉 Gitee 或上传压缩包）
git clone https://gitee.com/frostpolaris/yiban-auto-sign.git /opt/yiban-auto-sign
cd /opt/yiban-auto-sign && pip3 install -r requirements.txt

# 3. 配置账号（TUI 面板：名称/手机号/密码/设备识别码，一个账号一次输完）
# 安装 yiban 命令（SSH 后输入 yiban 直接打开面板）
cat > /usr/local/bin/yiban << 'EOF'
#!/bin/bash
cd /opt/yiban-auto-sign
exec python3 -m tui "$@"
EOF
chmod +x /usr/local/bin/yiban
yiban        #   A 添加 → 填写 → S 保存 → Q 退出；设置区可调随机延迟开关

# 4. 配置定时任务（每天 6:31 + 7:10 两次；周日是否执行由「周日签到」开关控制）
crontab -e   # 追加：
# 31 6 * * * /opt/yiban-auto-sign/run.sh
# 10 7 * * * /opt/yiban-auto-sign/run.sh
# 说明：cron 需每天执行，signin.py 内部按「周日签到」开关（网页系统设置，.env 的 YIBAN_SUNDAY_SIGN=1）决定周日是否跳过；未开启时周日自动跳过（SKIPPED）

# 5. 验证
python3 scripts/signin.py --check-config   # 配置检查（不发请求）
bash run.sh && tail -20 /var/log/yiban/sign-$(date +%F).log
```


### 部署步骤

#### 1. 服务器环境准备（Ubuntu 22.04）

```bash
apt update
apt install -y python3 python3-pip
```

#### 2. 上传项目代码

在本地打包（排除 `.git` 和缓存）：

```powershell
cd yiban-auto-sign
tar -czf yiban.tar.gz --exclude='.git' --exclude='__pycache__' .
scp yiban.tar.gz root@你的服务器IP:/opt/
```

在服务器解压并安装依赖：

```bash
mkdir -p /opt/yiban-auto-sign
cd /opt/yiban-auto-sign
tar -xzf /opt/yiban.tar.gz
rm /opt/yiban.tar.gz
pip3 config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
pip3 install -r requirements.txt
```

#### 3. 配置账号（推荐：TUI 配置工具）

SSH 登录服务器后运行表单式配置工具，**一个账号的所有信息（名称、手机号、密码、设备型号、设备识别码）一次输入完成**，密码输入时自动掩码：

```bash
yiban            # 推荐：全局命令（已安装到 /usr/local/bin/yiban）
# 或 python3 -m tui
```

**面板布局**：
- **左侧**：账号列表——序号（决定顺序打卡顺序）、状态图标（⏳ 准备签到 / ✅ 今日成功 / ❌ 最终失败 / 🔄 重试中 / ➖ 跳过）、名称、手机号、设备型号
- **右上**：签到日志（最近记录，自动刷新）
- **右下**：设置区——随机延迟开关（启动延迟/账号间隔，各含秒数可调）、连通性检测、服务器时间与签到状态，均写入 `.env`

**界面快捷键**：

| 按键 | 功能 |
|------|------|
| `A` | 添加账号（名称 / 手机号 / 密码 / 设备型号 / 设备识别码） |
| `E` / `D` | 编辑 / 删除选中账号（↑↓ 选择） |
| `[` / `]` | 上移 / 下移选中账号（调整阅读与顺序打卡顺序） |
| `M` | 手动签到选中账号（后台子进程执行，日志同步刷新） |
| `S` | 保存（账号 → SQLite 数据库 `yiban.db`，随机延迟 → `.env`） |
| `Q` | 退出 |

保存后 `signin.py` 每次执行会自动从数据库读取账号（0.17+ 数据存于 SQLite；`accounts.json` 仅为旧版本迁移来源）。也可以不启动 TUI，直接用网页管理后台添加账号。

> 💡 **手动验证配置**（不发送任何网络请求）：
> ```bash
> python3 scripts/signin.py --check-config
> ```

#### 4. 配置环境变量

```bash
cat > /opt/yiban-auto-sign/.env << 'EOF'
YIBAN_PROXY=http://127.0.0.1:8888
# YIBAN_START_DELAY_MAX=60   # 随机延迟：启动后 0~60 秒随机（默认关，见"随机延迟"小节）
# YIBAN_ACCOUNT_GAP_MAX=10   # 随机延迟：账号间 0~10 秒随机（默认关）
EOF
```

账号已通过 TUI / 网页写入数据库（`yiban.db`），`.env` 只需配置代理等公共选项（单账号也可继续用 `YIBAN_PHONE` / `YIBAN_PASSWORD`，向后兼容）。

#### 5. 运行脚本（仓库已自带，无需手写）

仓库根目录的 `run.sh` 已包含完整运行逻辑：单实例锁（防止 07:10 备用签到与 06:31 进行中的进程并发触发风控）、状态文件防重复（当天成功后自动跳过）、总超时保护、按天日志（`/var/log/yiban/sign-YYYY-MM-DD.log`）。直接使用即可：

```bash
chmod +x /opt/yiban-auto-sign/run.sh
mkdir -p /var/log/yiban
# 如确需自定义，请基于仓库版本修改（勿用下面这段简化版覆盖：缺少并发/防重复/超时保护）
# 简化版仅供理解核心调用：
#   export $(cat .env | xargs) && /usr/bin/python3 scripts/signin.py >> /var/log/yiban/sign-$(date +%F).log 2>&1
```

#### 6. 配置 crontab 定时任务

```bash
crontab -e
```

添加以下内容（每天 6:31 和 7:10 各执行一次；周日是否签到由网页「系统设置 → 周日签到」开关控制，未开启时周日自动跳过）：

```cron
# 易班自动签到 - 每天执行（周日由 YIBAN_SUNDAY_SIGN 开关控制，默认跳过）
# 6:31 第一次签到（主要，落在签到窗口 06:30 起点后）
31 6 * * * /opt/yiban-auto-sign/run.sh
# 7:10 第二次签到（备用，防止第一次失败）
10 7 * * * /opt/yiban-auto-sign/run.sh
```

> 部分学校周日也有签到任务：在网页「系统设置」开启「周日签到」后，周日也会尝试签到；若学校周日无需签到，将显示为已签到。

#### 7. 手动测试

```bash
bash /opt/yiban-auto-sign/run.sh
tail -20 /var/log/yiban/sign-$(date +%F).log
```


### 常用运维命令

```bash
# 查看今天签到日志（日志按天分文件：每天一个 sign-YYYY-MM-DD.log）
tail -50 /var/log/yiban/sign-$(date +%F).log

# 查看某天历史日志（示例：8 月 15 日）
cat /var/log/yiban/sign-2026-08-15.log

# 清理过期数据（按天日志/状态文件默认保留 365 天、调度快照 7 天；可配 cron 每天执行）
scripts/yiban-cleanup.sh

# 手动触发签到
bash /opt/yiban-auto-sign/run.sh

# 查看 cron 服务状态
systemctl status cron

# 查看 crontab 配置
crontab -l

# 更新代码后重新部署
scp scripts/signin.py root@服务器IP:/opt/yiban-auto-sign/scripts/
```


## 网页管理系统


除 SSH 打开 TUI 外，还提供浏览器管理界面（手机/平板/电脑任意设备访问）：

```bash
# 1. 安装依赖
pip3 install flask

# 2. .env 配置管理员账号（否则无法登录后台）
echo -e "YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD=你的密码" >> .env

# 3. 启动（默认端口 17892，--port 可改）
python3 -m web
# 生产建议用 systemd + gunicorn 常驻（禁止 werkzeug dev server 公网直连）：
#   systemd 单元模板：web/deploy/yiban-web.service
#   （部署细节见下方「🖥️ 服务器部署（进阶）」章节）
```

浏览器访问 `http://服务器IP:17892`：

- **管理员**：登录后可管理全部账号（添加/编辑/删除/排序/手动签到）、审核普通用户提交的账号、查看签到日志与状态、设置随机延迟、连通性检测、修改管理员账密；支持暗色主题
- **普通用户**：邮箱注册后提交自己的易班账号（名称+手机号+密码+设备信息），管理员审核通过后参与每日自动签到；可查看自己账号的签到状态与最近记录；每个用户限提交一个账号
- **安全**：登录失败限速（5 次锁定 5 分钟）+ 连续失败 webhook 告警（`YIBAN_NOTIFY_URL`）、CSRF 防护、密码哈希存储（scrypt）、HttpOnly/SameSite 会话、密码明文永不下发前端

> ⚠️ 无固定域名时建议在云服务商安全组仅放行常用 IP，并定期修改管理员密码。


### 自定义 Web 图标

网页顶部/侧边栏的品牌图标**优先读取 `web/static/vendor/logo.png`**：文件存在即显示你的图标；文件不存在时自动回退为占位符（内联 SVG 蓝色圆角块 +「签」字，无版权资源）。

**开源仓库不含 logo.png**（版权来源不明，避免公开分发风险），因此：

- **自己部署**：直接把你的图标文件放入 `web/static/vendor/logo.png`（约 64×64 以上即可），**无需改任何代码**，刷新页面即生效
- **其他部署者**：自行准备一个同名图标文件放入该位置即可；不放则显示占位符，不影响任何功能
- **想换内联 SVG**：改模板中回退占位 `<svg>` 的内容即可

代码位置：
- 管理端（管理员页面）：`web/templates/index.html` 品牌区（侧边栏顶部，搜 `brand-fallback`）
- 用户端：`web/templates/user.html` 顶栏（搜 `user-brand-fallback`）

> 请确保你放入的图标有使用权（仓库不附带任何品牌图标资源）。


### 使用指南


部署完成后，浏览器访问 `http://服务器IP:17892`（或经 nginx 反代的 HTTPS 域名）：

**普通用户**（详细版见 [docs/USER_GUIDE.md](docs/USER_GUIDE.md)）：
1. 邮箱注册 → 登录
2. 「提交我的易班账号」：填写名称、易班手机号、密码、设备信息（学校开启设备绑定时必填）
3. 等待管理员审核；审核不通过会显示拒绝理由，修改后可重新提交
4. 查看「签到情况」与「签到日历」（✅ 成功 / ❌ 失败 / ➖ 周日休息）

**管理员**（.env 配置的账号登录）：
- **账号管理**：添加/编辑（手机号唯一、密码留空=不变、识别码可一键清除）/软删除（7 天可恢复）/彻底删除/上移下移排序（决定签到顺序）/手动签到（30 秒防抖）/批量操作（审核、删除、恢复）
- **审核**：普通用户提交的账号 → 通过/拒绝（附理由）
- **用户管理**：查看注册用户、设为/取消管理员（仅主管理员）、重置密码、清空账号、完全删除；权限分级（主管理员 / 注册管理员 / 普通用户）
- **设置**：随机延迟开关、签到模式（列表顺序/列表随机）、全局公告、修改管理员账密
- **日志与状态**：签到日志实时刷新（手机号打码）、可按日期查看任意一天的历史日志（0.19.5+，日志按天分文件）、今日各账号状态图标、连通性检测、服务器时间

> 🔒 安全设计：登录失败限速锁定（5 次/5 分钟）、CSRF 防护、密码 scrypt 哈希、会话 HttpOnly/SameSite、列表手机号/邮箱脱敏、密码明文永不下发前端。

---


## GitHub Actions 使用教程（备选）


> ⚠️ **注意**：GitHub Actions 的服务器位于海外，可能被易班 WAF 风控拦截（返回「风险访问服务禁用」），且海外 IP 反复失败可能触发账号风控。**有云服务器时强烈建议改用 [服务器部署](#️-服务器部署进阶)**；以下 Actions 方案仅作免服务器场景的备选。

无需服务器，Fork 仓库 + 配置账号即可，5 分钟搞定。

### 第 1 步：Fork 仓库

点击本仓库右上角的 **Fork** 按钮，将项目复制到你的 GitHub 账号下。

> 💡 建议在 Fork 页面取消勾选「Copy the main branch only」以获取完整历史，不过只勾选主分支也能用。

### 第 2 步：配置账号密码（必填）

进入你 Fork 后的仓库，依次点击 **`Settings`** → **`Secrets and variables`** → **`Actions`** → **`New repository secret`**，添加：

| Secret 名称 | 说明 | 是否必填 |
|------------|------|---------|
| `YIBAN_ACCOUNTS` | 易班账号，格式 `手机号:密码`，多账号用 `#` 分隔 | 二选一必填 |
| `YIBAN_PHONE` | 易班手机号（单账号，向后兼容） | 二选一必填 |
| `YIBAN_PASSWORD` | 易班密码（单账号，向后兼容） | 二选一必填 |
| `YIBAN_PROXY` | 可选：HTTP 代理地址（配置后签到请求经代理发出） | 可选 |
| `YIBAN_PHONE_MODEL` | 设备型号（学校开启设备绑定时必填，详见 [设备绑定](#设备绑定可选)） | 视情况必填 |
| `YIBAN_PHONE_CODE` | 设备唯一识别码（学校开启设备绑定时必填，详见 [设备绑定](#设备绑定可选)） | 视情况必填 |
| `YIBAN_NOTIFY_URL` | 通知 webhook URL（详见 [消息通知](#消息通知可选)） | 可选 |

**单账号示例（推荐）：**

```
Name:  YIBAN_ACCOUNTS
Value: 13800138000:your_password
```

**多账号示例：**

```
Name:  YIBAN_ACCOUNTS
Value: 13800138000:pwd1#13900139000:pwd2#13700137000:pwd3
```

> ⚠️ **密码中如包含 `:` 或 `#` 字符**：请改用 `YIBAN_PHONE` / `YIBAN_PASSWORD` 两个 Secret 分别配置。

### 第 3 步：启用 GitHub Actions

1. 进入仓库的 **`Actions`** 标签页
2. 如果看到提示，点击 **`I understand my workflows, go ahead and enable them`** 启用工作流
3. 左侧应能看到名为 **`Yiban Sign-in`** 的工作流

### 第 4 步：手动测试

1. 在 `Actions` 页面左侧选择 **`Yiban Sign-in`** 工作流
2. 点击右侧 **`Run workflow`** → 分支选 `main` → 点绿色按钮确认
3. 等待约 30 秒后刷新页面，点击本次运行查看日志

**日志中出现以下内容即代表签到成功：**

```
[2026-08-01 06:35:01] [INFO] ==== 开始执行签到，共 1 个账号 ====
[2026-08-01 06:35:01] [INFO] [13800138000] 登录成功
[2026-08-01 06:35:02] [INFO] [13800138000] 生成定位: (118.789,32.045) 地址: XX大学
[2026-08-01 06:35:03] [INFO] [13800138000] ✅ 签到成功
[2026-08-01 06:35:03] [INFO] ==== 签到汇总 ====
[2026-08-01 06:35:03] [INFO]   ✅ 13800138000: 签到成功
```

如果遇到 WAF 风控拦截（「风险访问服务禁用」），说明该网络出口被易班风控，建议改用服务器部署方案。


### 定时执行

### 默认执行时间

早操签到窗口为 **北京时间 06:30–07:50**，GitHub Actions 每天在该窗口内执行 **1 次**：

| Cron 表达式 | UTC 时间 | 北京时间 | 实际预计执行 | 用途 |
|------------|---------|---------|------------|------|
| `45 21 * * *` | 21:45（前一日） | 05:45 | 约 06:40–07:45 | 签到（云服务器为主力，此为备用） |

> GitHub Actions 的 `schedule` 延迟分布约 55–120 分钟（26 个样本，中位数约 80 分钟）。设为 05:45 触发，实际执行约 06:40–07:45，安全落在签到窗口内；延迟 <45 分钟才空跑（历史 0%），延迟 >125 分钟才超时（历史 0%）。
>
> 云服务器（主力）在 06:31 和 07:10 各执行一次（07:10 仅在 06:31 失败时执行），GitHub Actions 作为备用方案。

### 修改执行时间

编辑 [`.github/workflows/signin.yml`](.github/workflows/signin.yml) 中的 `cron` 字段：

```yaml
on:
  schedule:
    - cron: '45 21 * * *'  # 北京时间 05:45
```

**Cron 表达式格式**：`分 时 日 月 周`（UTC 时间，北京时间 = UTC + 8）

> ⚠️ 务必保证触发时间 + 预估延迟（55–120 分钟）落在签到窗口（06:30–07:50）内，否则脚本会因"未在签到时间内"而失败。

> ⏱️ GitHub Actions 的 `schedule` 实际触发时间可能延迟 55–120 分钟，请勿用于要求精确到分钟的场景。

---


### 资源消耗

| 项目 | 数值 |
|------|------|
| 每次执行耗时 | 约 10-30 秒 |
| 每日执行次数 | 2 次 |
| 每月消耗 Actions 分钟 | 约 2-5 分钟 |
| GitHub 免费额度 | 2000 分钟/月（公开仓库无限） |
| 费用 | **完全免费** |

---


## 配置说明


### 环境变量一览

账号数据存于 **SQLite 数据库（`yiban.db`）**，由网页管理后台 / TUI 写入（AES-GCM 加密存储）。`YIBAN_ACCOUNTS_JSON`、`YIBAN_ACCOUNTS`、`YIBAN_PHONE`+`YIBAN_PASSWORD` 为旧格式 / CI 场景的向后兼容加载方式。

| 变量名 | 说明 | 必填 |
|--------|------|------|
| `YIBAN_ACCOUNTS_JSON` | 账号 JSON 数组（推荐），每个账号一次输入完整信息，格式见下方 | 二选一 |
| `YIBAN_ACCOUNTS` | 旧格式 `手机号:密码`，多账号用 `#` 分隔（向后兼容） | 二选一 |
| `YIBAN_PHONE` | 易班手机号（单账号，向后兼容） | 二选一 |
| `YIBAN_PASSWORD` | 易班密码（单账号，向后兼容） | 二选一 |
| `YIBAN_ACCOUNTS_FILE` | 旧 JSON 账号文件路径（仅 JSON→SQLite 迁移时使用；0.17+ 数据在 `yiban.db`） | 可选 |
| `YIBAN_START_DELAY_MAX` | 启动后随机延迟上限秒数：默认 `0`（关闭）；开启后脚本启动随机等待 0~N 秒再开始首个签到，打散"每天固定秒级执行"的脚本特征 | 可选 |
| `YIBAN_ACCOUNT_GAP_MAX` | 账号间随机间隔上限秒数：默认 `0`（关闭）；开启后账号间随机停顿 0~N 秒 | 可选 |
| `YIBAN_LEGACY_LOGIN` | 设为 `1` 时使用旧登录流程（伪造 iOS UA）；默认使用 fyiban 同款真实 App 特征（推荐，见 [Q1](#q1报错-账号或密码错误e003但密码明明是对的)） | 可选 |
| `YIBAN_PROXY` | 代理地址，如 `http://host:port` 或 `socks5://host:port` | 可选 |
| `YIBAN_PHONE_MODEL` | 设备型号（如 `Vivo-XXXX`），账号未配置设备信息时全局回退 | 视情况 |
| `YIBAN_PHONE_CODE` | 设备唯一识别码（64位十六进制字符串），账号未配置设备信息时全局回退 | 视情况 |
| `YIBAN_NOTIFY_URL` | 通知 webhook URL | 可选 |
| `YIBAN_SIGN_START` / `YIBAN_SIGN_END` | 签到窗口（`HH:MM`，默认 `06:30` / `07:50`；网页「系统设置」修改后写入） | 可选 |
| `YIBAN_SUNDAY_SIGN` | 周日签到开关：`1`=周日也执行，`0`/缺省=周日跳过（网页「系统设置」开关） | 可选 |
| `YIBAN_SIGN_ORDER` | 调度排序：`sequence`（列表顺序，默认）/ `random`（每天打乱） | 可选 |
| `YIBAN_SIGN_DIST` | 调度分布：`uniform`（均匀，默认）/ `normal`（钟形高峰） | 可选 |
| `YIBAN_WINDOW_EDGE_SEC` | 签到窗口首尾缓冲秒数（默认 `60`；防止踩点签到） | 可选 |
| `YIBAN_BLOCK_CAP` | 错峰调度块容量（每块最多人数，默认 `15`；超出向后顺延/压缩模式） | 可选 |
| `YIBAN_ALLOW_TIME_PREF` | 用户自选时间片总开关：`1`=开启（网页开放自选，默认关） | 可选 |
| `YIBAN_MAX_USERS` / `YIBAN_MAX_ACCOUNTS` | 容量上限（默认 `200` 用户 / `500` 账号；`0`=不限） | 可选 |

> 调度 v2 其余内部参数（正态 μ/σ 范围、重试最小间隔、容量预检耗时等）见代码 `scripts/signin.py` 的 `_schedule_config()`，网页「系统设置」不展示的项一般无需调整。

### 随机延迟（防风控）与容量预估

随机延迟用于打散"每天固定秒级执行"的脚本特征，作为 [e003 修复](#q1报错-账号或密码错误e003但密码明明是对的)（真实 App 登录特征）之外的纵深防御。**默认关闭**，两种方式开启：

1. **TUI 设置栏**（推荐）：`yiban` → 设置区 → 点「启动延迟 / 账号间隔」开关（开启自动填默认秒数，可改）→ `S` 保存
2. **手动编辑 `.env`**（服务器 `/opt/yiban-auto-sign/.env`）：

```bash
# 启动后随机等待 0~60 秒再开始首个签到（删除该行 = 关闭）
YIBAN_START_DELAY_MAX=60
# 账号间随机间隔 0~10 秒（删除该行 = 关闭）
YIBAN_ACCOUNT_GAP_MAX=10
```

改完保存即可，cron 下次触发自动生效，无需重启任何服务。

> ⚠️ 两个变量独立生效：可只开启动延迟、只开账号间隔，或全关。

**容量预估**（最坏情况：所有随机都取最大值，窗口 06:30~07:50、cron 06:31 触发）：

| 单账号耗时 | 每账号占位 | 窗口内可容纳 |
|-----------|-----------|-------------|
| 4 秒（实测） | 14 秒（含 10 秒间隔） | **~296 个账号** |
| 5 秒（保守） | 15 秒 | **~276 个账号** |

公式：`可容纳账号数 ≈ (窗口剩余秒数 − 启动延迟 − 单账号耗时) ÷ (单账号耗时 + 间隔) + 1`。对当前 1~2 个账号，60s 启动延迟 + 10s 间隔的实际影响可忽略（最晚 06:42 全部完成）。

### 账号配置格式（JSON，兼容旧格式 / CI）

`YIBAN_ACCOUNTS_JSON` 使用如下 JSON 数组格式，一个账号一次输入完整信息（手机号、密码、设备型号、设备识别码），**无需用符号分隔**（网页后台/TUI 写入的数据库账号不依赖此格式）：

```json
[
  {"phone": "13800138000", "password": "你的密码", "phone_model": "Vivo-XXXX", "phone_code": "64位识别码"},
  {"phone": "13900139000", "password": "另一个密码"}
]
```

- `phone` / `password` 必填；`phone_model` / `phone_code` 可选（学校开启"设备绑定"时必填，每个账号可独立配置）
- 服务器端可用 TUI 工具自动生成此文件：`python3 -m tui`
- 检查配置（不发送任何请求）：`python scripts/signin.py --check-config`

> ⚠️ 通过网页/TUI 保存的 `accounts.json`，其中 `password` / `phone_code` 为 **AES-GCM 密文对象**（非明文，`.gitignore` 已排除该文件）。解密密钥 `YIBAN_ACCOUNTS_KEY` 自动生成在 `.env`（chmod 600）：**密钥丢失 = 已加密账号密码不可恢复**，备份数据时必须连同 `.env` 一起备份（建议与数据分开放、分开打包）。

### 消息通知（可选）

签到失败时会自动推送通知。支持以下渠道：

<details>
<summary>📲 Server 酱（微信推送）</summary>

1. 访问 [https://sct.ftqq.com/](https://sct.ftqq.com/) 注册并获取 SendKey
2. 将 URL 填入 `YIBAN_NOTIFY_URL`：
   ```
   https://sctapi.ftqq.com/YOUR_SENDKEY.send
   ```
</details>

<details>
<summary>📲 Bark（iOS 推送）</summary>

```
https://api.day.app/YOUR_KEY/易班签到通知
```
</details>

<details>
<summary>📲 企业微信群机器人</summary>

1. 企业微信群 → 群设置 → 群机器人 → 添加机器人
2. 复制 webhook URL 填入 `YIBAN_NOTIFY_URL`：
   ```
   https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY
   ```
</details>

### 代理配置（可选）

| 代理类型 | 格式 | 说明 |
|---------|------|------|
| HTTP 代理 | `http://host:port` | 可选 |
| 带认证的 HTTP | `http://user:pass@host:port` | 可选 |
| SOCKS5 代理 | `socks5://host:port` | 可选 |

> 💡 仅在网络出口被风控时按需配置；使用代理访问服务请遵守相关法律法规与平台条款。

### 设备绑定（可选）

部分学校在校本化后台开启了「设备绑定」功能，签到时会校验设备型号和唯一识别码，不匹配则返回「请使用授权设备进行签到」错误。如果你的学校开启了此功能，需要配置以下两个环境变量：

| 变量名 | 说明 | 示例 |
|--------|------|------|
| `YIBAN_PHONE_MODEL` | 设备型号（品牌-型号，`:` 需替换为 `-`） | `Vivo-XXXX` |
| `YIBAN_PHONE_CODE` | 设备唯一识别码（64位十六进制字符串） | `xxxxxxxx...` |

**如何获取设备信息：**

1. 在易班 App 中打开校本化签到页面
2. 通过易班内置的 JS 桥接获取（需借助工具页面）：
   - `yiban.getUUID()` → 返回设备唯一识别码（即 `YIBAN_PHONE_CODE`）
   - `yiban.getDeviceInfo()` → 返回设备信息，其中 `deviceModel` 字段即 `YIBAN_PHONE_MODEL`

> 💡 如果签到时未报「请使用授权设备进行签到」错误，说明你的学校未开启设备绑定，无需配置这两个变量。

---


## 数据存储与备份


从 v0.17 起，账号与用户数据存储于 **SQLite 数据库**（`yiban.db`，WAL 模式），替代早期的 JSON 文件：

| 内容 | 位置 | 说明 |
|---|---|---|
| 易班账号（含密码/识别码，AES-GCM 密文） | `yiban.db` 的 `accounts` 表 | 手机号全局唯一；软删除保留 7 天可恢复 |
| 网站用户（邮箱/密码哈希/角色） | `yiban.db` 的 `users` 表 | scrypt 哈希存储 |
| 管理操作审计 | `yiban.db` 的 `audit_logs` 表 | 账号增删改/审核/批量/用户管理自动留痕，保留 180 天 |
| 每日签到状态 | `/var/log/yiban/sign-daily-*.json` | 日历展示用，不入库 |

**审计查询**（服务器上）：
```bash
sqlite3 /opt/yiban-auto-sign/yiban.db "SELECT ts, username, action, target FROM audit_logs ORDER BY id DESC LIMIT 20"
```

**备份**：`scripts/backup.sh`（每日 cron 02:00）——`sqlite3 .backup` 一致性快照 + 加密密钥 + 异机加密副本 + 30 天保留；恢复演练 `bash backup.sh --restore <包> <目录>`。

**回滚逃生门**：`python3 scripts/db_export.py --out /tmp/export` 可将数据库导出回 JSON 格式（降级/迁移用）。

> ⚠️ 加密密钥（`.env` 的 `YIBAN_ACCOUNTS_KEY`）与数据分开备份——密钥丢失 = 已加密账号密码不可恢复。

---


## 本地调试


<details>
<summary>🔧 展开查看本地运行方法</summary>

如需在本地运行：

```bash
# 1. 克隆仓库
git clone https://github.com/<你的用户名>/yiban-auto-sign.git
cd yiban-auto-sign

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置账号（推荐 JSON，一次输入一个账号完整信息）
$env:YIBAN_ACCOUNTS_JSON='[{"phone":"13800138000","password":"your_password"}]'

#    或旧格式（多个账号用 # 分隔，向后兼容）
$env:YIBAN_ACCOUNTS="13800138000:your_password"

#    或 Linux/macOS
export YIBAN_ACCOUNTS_JSON='[{"phone":"13800138000","password":"your_password"}]'

# 3.1 检查配置（不发送任何网络请求，密码脱敏显示）
python scripts/signin.py --check-config

# 4. 运行
python scripts/signin.py
```

</details>

---


## 原理与实现


<details>
<summary>🔬 展开查看技术实现细节</summary>

### 项目架构

```
web/app.py  Flask 管理后台（账号管理/审核/用户管理/日历/手动签到）
   │
   ├── scripts/db.py            SQLite 数据层（账号/用户/审计日志）
   ├── scripts/account_crypto.py AES-GCM 加密（密码/设备识别码）
   └── scripts/signin.py        签到引擎
            │
            ├── OAuth 登录（RSA 加密）→ 获取签到任务 → 多边形随机定位 → 提交
            ├── 触发方式：服务器 cron（run.sh）/ GitHub Actions
            └── 通知：Server酱 / Bark / 企业微信 webhook
```

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

为保证定位落在有效签到范围内且不暴露固定位置，本项目使用与 [OneFeiFan/FYIBAN](https://github.com/OneFeiFan/FYIBAN)（AGPL-3.0）一致的 **缩放质心算法**（射线法校验，感谢原作者开源）：

1. 解析签到范围返回的多边形顶点 `Points`
2. 计算多边形质心 `(center_lng, center_lat)`
3. 将多边形顶点向质心收缩 0.7 倍，得到 `scaled_polygon`
4. 在质心附近的边界框内随机生成点（最多 5000 次尝试）
5. 校验点是否同时在 `scaled_polygon` 和 `original_polygon` 内
6. 若 5000 次均未命中，兜底返回质心

这样每次签到的定位点都不同，但都落在有效范围内，避免被识别为异常定位。

### 重试机制

脚本内置队列重试模式，用于处理临时性网络问题或 WAF 拦截：失败账号放回队尾分散重试，同账号两次尝试间隔不小于 60 秒，单账号最多 4 次尝试（1 次初始 + 3 次重试；风控/凭据类失败最多 2 次），每次重试附加 0~30 秒随机延迟。

当遇到以下情况时会自动重试：
- WAF 风控拦截（"风险访问服务禁用"）
- 网络连接超时
- 其他临时性错误

### WAF 拦截检测

易班的 WAF 风控会返回包含「风险访问服务禁用」等关键词的页面。脚本通过以下方式检测：

- **长度检查**：响应超过 2000 字符直接判定为非拦截（正常页面通常较长）
- **关键词匹配**：检测「风险访问」「风控」「访问服务禁用」「WAF」「拦截」
- **Unicode 解码**：WAF 返回 JSON 格式时中文会被 `\uXXXX` 转义，需先解码再匹配

### 定时工作流自动续期

GitHub 官方政策：**仓库连续 60 天无活动，定时工作流会被自动禁用**。

本项目在工作流中集成了 [`liskin/gh-workflow-keepalive@v1`](https://github.com/liskin/gh-workflow-keepalive)：

```yaml
workflow-keepalive:
  if: github.event_name == 'schedule'
  runs-on: ubuntu-latest
  permissions:
    actions: write
  steps:
    - uses: liskin/gh-workflow-keepalive@v1
```

**原理**：每次定时触发签到时，keepalive job 会通过 GitHub API 检查并重新启用被禁用的工作流，重置 60 天计时器。无需产生额外的 commit，不污染提交历史。

</details>

---


## 常见问题


<details>
<summary><b>Q1：报错 "账号或密码错误"（e003），但密码明明是对的</b></summary>

> ✅ **已修复**：默认登录方式改为参考 fyiban 的真实 App 请求特征，详见下方"根因"。

**根因（2026-08-08 排查确认）**：旧登录流程沿用开源项目 Auto-Test 的请求特征（伪造 iPhone UA + `X-Requested-With: com.yiban.app` + 可预测 CSRF + 非 App 参数组合），被易班风控识别为**非官方客户端**，对登录接口统一返回 `e003 账号或密码错误` 伪装拒绝。它与 IP、账号、密码、设备信息均无关——实测：手机流量 IP + 新账号同样 e003，而同一网络下手机 App 正常。

**修复方式**：登录改为 fyiban 同款流程（UA=`Yiban` + `AppVersion: 5.1.2` + SecureRandom 真随机 CSRF + `scope` 空 + `display=authorize` + usersure 不带 Origin 头），新旧账号均恢复正常。旧流程保留，可用 `YIBAN_LEGACY_LOGIN=1` 切回（如 GitHub Actions 等特殊场景）。

**排查顺序（老版本或自定义改回旧流程时参考）**：

1. **用手机易班 App 登录一次**——能正常登录则说明账号和密码都没问题
2. **对照实验**：临时用 `.env` 旧格式（`YIBAN_PHONE`+`YIBAN_PASSWORD`）再跑一次——新旧方式同时报错，即可排除配置问题
3. **确认触发源**：检查同一账号当天是否被多个 IP 尝试过（如 GitHub Actions 海外 IP 定时签到失败重试）
4. **等待冷却**：风控冷却通常几小时到 24 小时，**期间不要反复重试**（会延长冷却）

> 💡 **预防**：避免在同一账号上叠加多路定时签到（如 GitHub Actions + 服务器同时跑）。推荐以国内服务器为唯一签到通道。
</details>

<details>
<summary><b>Q2：报错 "获取签到任务失败"</b></summary>

- 你的学校可能未开启晚间考勤（nightAttendance）任务
- 该接口仅适用于开启「晚间考勤 / 晚签到」功能的学校
- 如需打卡的是「每日打卡」（officeTask），需修改脚本中的 API 路径
</details>

<details>
<summary><b>Q3：报错 "未在签到时间内"</b></summary>

- 当前时间不在管理员设置的签到时间窗口内
- GitHub Actions 的触发延迟（5–30 分钟）可能导致实际执行时超出窗口
- 可调整 `.github/workflows/signin.yml` 中的 `cron` 时间，或等待下一次定时触发
- 此错误**不会**导致 GitHub Actions 标记为失败（退出码仍为 0）
</details>

<details>
<summary><b>Q4：报错 "风险访问服务禁用" / WAF 风控拦截</b></summary>

- **原因**：GitHub Actions 的海外 IP 被易班 WAF 风控拦截
- **解决方案**：改用 [服务器部署](#️-服务器部署进阶)（国内网络出口）；如确需使用 Actions，可配置 `YIBAN_PROXY` 代理（见 [代理配置](#代理配置可选)）
- 脚本会自动重试 3 次，如果仍然失败会标记为错误
- **注意**：海外 IP 的反复失败尝试可能让易班把**账号**标记为可疑，连带影响服务器签到（表现为 e003，见 [Q1](#q1报错-账号或密码错误e003但密码明明是对的)）。如果已有国内服务器签到，**建议在 GitHub Actions 页面禁用该工作流**，避免双路签到触发风控
</details>

<details>
<summary><b>Q5：报错 "遇到 ydclearance 反爬"</b></summary>

- 已在 `requirements.txt` 中包含 `js2py`，正常情况下不会触发此错误
- 若仍出现，可能是 GitHub Actions 的 IP 被风控，请改用服务器部署方案
</details>

<details>
<summary><b>Q6：报错 "请使用授权设备进行签到"</b></summary>

- **原因**：学校在校本化后台开启了「设备绑定」功能，签到时会校验设备型号和唯一识别码
- **解决方案**：配置 `YIBAN_PHONE_MODEL` 和 `YIBAN_PHONE_CODE` 两个环境变量（GitHub Actions 中为 Secrets）
- 获取方式详见 [设备绑定](#设备绑定可选) 章节
</details>

<details>
<summary><b>Q7：签到成功但 GitHub Actions 显示失败</b></summary>

- 检查日志中是否有 "❌" 标记的账号
- 多账号场景下，只要有一个**真正的失败**（非"未在签到时间内"），整体退出码就是 1
- "未在签到时间内"不会导致失败（这是正常行为）
</details>

<details>
<summary><b>Q8：定时任务不执行 / 突然停止</b></summary>

- 进入 `Actions` 页面确认工作流是否被禁用（被禁用会有醒目提示）
- 点击 `Enable workflow` 重新启用
- keepalive 会在下次定时触发时自动处理，但首次需手动启用
</details>

<details>
<summary><b>Q9：如何查看签到历史</b></summary>

- 进入仓库 `Actions` 标签页
- 左侧选择 `Yiban Sign-in`
- 可看到所有历史运行记录，点击进入可查看详细日志
</details>

<details>
<summary><b>Q10：GitHub Actions 会触发风控吗？要不要停掉？</b></summary>

**会**。Actions 使用 GitHub 海外 IP，每次定时尝试登录都会被易班 WAF 拦截（报"风险访问服务禁用"）；更麻烦的是，**反复的失败尝试可能让易班把账号标记为可疑，连带影响国内服务器签到**（服务器随后出现 e003 伪装"密码错误"，见 [Q1](#q1报错-账号或密码错误e003但密码明明是对的)）。

**建议**：

- 已有国内服务器签到 → **在 Actions 页面禁用工作流**（Actions → Yiban Sign-in → ⋯ → Disable workflow），让服务器成为唯一签到通道，最稳
- 没有服务器、必须用 Actions → 可配置 `YIBAN_PROXY` 代理（见 [代理配置](#代理配置可选)），并避免与其他签到通道叠加同一账号
- 恢复 Actions：同一位置 `Enable workflow`
</details>

---


## 注意事项


> **🔐 请自建服务器，不要使用他人的公开实例**：本工具会保存易班账号密码（加密存储）、手机号、设备识别码等敏感数据。使用不明来源的公开实例 = 把易班账号和密码交给陌生人，对方可能解密查看甚至冒用签到。本项目为 AGPL 开源、自建成本很低（见上方部署教程），**请务必自行部署、自己掌控数据**；如发现有人运营公开实例收集账号，请提醒使用者注意风险。

1. **本项目仅供学习研究使用**，完整免责声明见 [AI 生成说明](#-ai-生成说明)
2. **强烈建议仓库设为 Private**，避免账号密码被搜索引擎索引
3. 不要将账号密码直接写在代码中，必须使用 GitHub Secrets
4. 请勿频繁调用 API（默认每天 2 次足够），以免触发风控
5. 如账号开启了二次验证，可能需要额外处理
6. **推荐国内服务器为唯一签到通道**：GitHub Actions 海外 IP 会被 WAF 拦截，其反复失败尝试可能连带触发账号风控（表现为 e003"密码错误"），进而影响服务器签到——有服务器时建议在 Actions 页面禁用工作流（详见 [Q10](#q10github-actions-会触发风控吗要不要停掉)）
7. **遇到"账号或密码错误"先别改密码**：默认登录方式已修复此问题（见 [Q1](#q1报错-账号或密码错误e003但密码明明是对的)）；若仍出现，先用手机 App 验证账号正常，再检查是否切回了旧流程（`YIBAN_LEGACY_LOGIN=1`）

---


## 测试范围与适配说明

**已测试环境**：Ubuntu 22.04 服务器部署、Python 3.10+。

**未覆盖场景**（请自行验证）：

- **Docker / Windows 部署**：未测试（仅 Ubuntu 验证）
- **消息通知**（Server酱 / Bark / 企业微信）：代码已实现，但本项目未在生产环境实测通知链路，建议配置后自行验证
- **多学校适配**：易班校本化签到因学校而异（接口与任务类型可能不同）。本项目**仅在南京工程学院（NJIT）实测**，其他学校可能需要适配
- **多校混合账号**：未测试同一实例下多校账号混合签到

如你在其他学校使用成功或有适配需求，欢迎提交 Issue 或 PR。

## License


**GNU Affero General Public License v3.0（AGPL-3.0）** - 见 [LICENSE](LICENSE)

### 核心条款简述

1. **网络服务强制开源**：通过网络（网站 / API）向用户提供服务时，必须向服务使用者提供完整的源代码
2. **强制传染**：任何使用、修改或分发本项目的衍生作品，必须同样以 AGPL-3.0 协议开源
3. **署名要求**：必须保留原作者版权声明与许可声明

### 第三方组件声明（2026-08-15 补）

本项目分发包含以下第三方开源组件（按各自许可保留版权声明）：

| 组件 | 用途 | 许可证 |
|------|------|--------|
| [Textual](https://github.com/Textualize/textual) | TUI 终端面板（`tui/`） | MIT — Copyright (c) 2022 Textualize Inc. |
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

### 特别免责声明

- **滥用与盈利免责**：本项目按"原样"提供。任何使用本项目进行商业或非商业行为时，若因违反当地法律法规、滥用功能（包括但不限于网络攻击、诈骗等非法用途）而产生任何形式的刑事或民事纠纷，均与本项目作者无关，使用者需自行承担所有法律后果
- **无担保**：作者不保证本项目的适用性、稳定性或无错误（Bug）
- **损失免责**：因使用或无法使用本项目而导致的任何直接、间接、偶然或后果性损害（包括数据丢失、业务中断、利润损失等），作者不承担任何责任

---


## 致谢


本项目参考了以下开源项目与资料：

- [AEtherside/skland-daily-attendance](https://github.com/AEtherside/skland-daily-attendance) - GitHub Actions 工作流结构与 keepalive 方案
- Auto-Test - 易班登录流程（OAuth + RSA + ydclearance，已弃用并被本项目新登录特征取代）
- [liskin/gh-workflow-keepalive](https://github.com/liskin/gh-workflow-keepalive) - 定时工作流自动续期（避免 60 天无活动被禁用）
- [OneFeiFan/FYIBAN](https://github.com/OneFeiFan/FYIBAN)（AGPL-3.0）- 多边形内随机定位点算法（缩放质心、射线法验证）、易班登录特征与 nightAttendance 签到流程
- [Textualize/textual](https://github.com/Textualize/textual)（MIT）- TUI 终端面板框架（`tui/` 组件）

---


## 相关开源项目推荐


易班签到生态中的其他开源方案（均含开源许可证，可放心参考）：

- [2117516450/yiban_signin](https://github.com/2117516450/yiban-signin)（易签，Unlicense）- 易班校本化早签/晚签打卡，多用户 + 多线程 + Server酱推送
- [Qs315490/YiBan_AutoSgin](https://github.com/Qs315490/YiBan_AutoSgin)（GPL-2.0）- 易班校本化晚点签到脚本（含活跃 fork：[Lumjiel/YiBan_AutoSgin](https://github.com/Lumjiel/YiBan_AutoSgin)）
- [OneFeiFan/FYIBAN](https://github.com/OneFeiFan/FYIBAN)（AGPL-3.0）- 易班 API 安卓库，校本化 OAuth 登录与签到（本项目定位算法与登录特征参考来源）

---


## AI 生成说明


### AI 编程生成声明

本项目的代码与文档由 AI 辅助生成，非人工逐行编写：

- **开发工具**：
  - [ZCode](https://zcode.z.ai/)（基于 deepseek-v4-flash-0731 模型）
  - [TRAE](https://www.trae.cn/)（基于 GLM-5.2 模型）
  - [deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)（基于 deepseek-v4-pro-0813 模型）
- **AI 负责的工作**：
  - 核心签到脚本 `scripts/signin.py` 的编写与调试
  - GitHub Actions / Gitee Go 工作流配置
  - README 等文档的撰写与优化
  - WAF 风控拦截问题的排查与修复
- **人类负责的工作**：
  - 需求定义与流程设计
  - 实际运行测试与参数调优
  - 服务器部署与运维决策
  - 最终代码审查与确认

> ⚠️ AI 生成的代码可能存在未预见的问题、逻辑漏洞或与最新平台规则不符的情况。使用前请务必充分测试，并根据自己的环境调整后再投入使用。

### AI 安全免责声明

> **使用本项目即表示你已知晓并接受以下风险：**

1. **代码可靠性风险**：AI 生成的代码可能存在未发现的缺陷，可能导致签到失败、账号异常、数据丢失等问题
2. **账号安全风险**：本项目需要你提供易班账号密码，存在凭证泄露的风险（即使使用 GitHub Secrets，也无法保证 100% 安全）
3. **平台规则变化风险**：易班平台可能随时更新 API 接口、WAF 规则或用户协议，导致本项目失效或触发风控
4. **AI 训练数据时效性**：AI 模型的训练数据存在时效性，可能不了解易班平台的最新规则变化
5. **责任归属**：本项目仅供学习研究使用，使用者需自行承担一切后果。开发者（包括 AI 与人类协作者）不对任何直接或间接损失负责

### 第三方适配免责声明

> **本项目与易班官方无任何关联，属于非官方第三方适配项目：**

1. **非官方性质**：本项目不是易班（yiban.cn / uyiban.com）官方产品，也未获得易班官方的授权、认可或支持
2. **接口逆向**：本项目通过逆向分析易班客户端的 API 接口实现签到功能，可能违反易班用户协议或相关服务条款
3. **合规性提示**：
   - 使用本项目可能违反你所在学校的相关规定
   - 可能导致你的易班账号被风控、限制功能或封禁
   - 可能影响学校考勤数据的真实性，带来学业诚信问题
4. **数据与隐私**：本项目会在服务器上处理你的账号密码，请务必在可信环境（如私有仓库、自有服务器）中部署
5. **停止维护**：如易班官方提出要求，本项目可能随时停止维护或下架

> 📌 **建议**：如条件允许，请优先使用易班官方客户端手动签到。本项目仅作为技术学习与研究的产物，不鼓励用于实际规避考勤。

