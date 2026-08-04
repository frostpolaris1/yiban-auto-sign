# 易班自动签到

![GitHub Actions](https://img.shields.io/github/actions/workflow/status/frostpolaris1/yiban-auto-sign/signin.yml?label=Actions&logo=github)
![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

> 易班（yiban）是很多高校学生用的校园社交平台，部分学校要求每天早晨进行「早操签到」打卡。
>
> 本项目通过 Python 脚本自动完成这个签到过程——配置一次后，每天到点自动帮你打卡，无需手动操作、无需保持电脑开机。

## ✨ 功能特点

- 🤖 **全自动签到**：每天定时执行，无需人工干预
- 📍 **智能定位**：在签到范围内生成随机定位点，模拟真实 GPS（缩放质心算法）
- 👥 **多账号支持**：一个仓库管理多个易班账号
- 🔔 **消息通知**：签到失败时推送通知（Server 酱 / Bark / 企业微信等）
- 🆓 **完全免费**：使用 GitHub Actions 免费额度，每月消耗约 60 分钟（远低于 2000 分钟配额）
- ⏰ **永久运行**：内置 `gh-workflow-keepalive`，自动破解 GitHub 60 天无活动禁用限制
- 🔄 **自动重试**：遇到 WAF 拦截时自动重试（指数退避）

## 📑 目录

- [🚀 快速开始（GitHub Actions）](#-快速开始github-actions)
- [⏰ 定时执行](#-定时执行)
- [🔧 配置说明](#-配置说明)
- [🖥️ 服务器部署（进阶）](#️-服务器部署进阶)
- [🧠 工作原理](#-工作原理)
- [❓ 常见问题](#-常见问题)
- [⚠️ 注意事项](#-注意事项)
- [🛠️ 本地调试](#️-本地调试)
- [📜 License](#-license)
- [🙏 致谢](#-致谢)
- [🤖 AI 生成说明](#-ai-生成说明)

---

## 🚀 快速开始（GitHub Actions）

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
| `YIBAN_PROXY` | 代理地址（绕过 WAF 风控，详见 [代理配置](#代理配置重要)） | **推荐配置** |
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

如果日志显示 `风险访问服务禁用` 或 `WAF 风控拦截`，说明 GitHub 的海外 IP 被易班风控了，需要配置代理——见下方 [代理配置](#代理配置重要)。

### 第 5 步：配置代理（重要）

<details>
<summary>📖 为什么需要代理？</summary>

易班平台有 WAF（Web 应用防火墙）风控，会拦截来自海外 IP 的请求。GitHub Actions 的服务器在海外，直接访问会被拦截，返回「风险访问服务禁用」错误。

**解决方案**：配置一个国内出口的代理，让请求看起来是从国内发出的。
</details>

**方式一：自建代理（推荐，免费）**

如果你有国内云服务器（阿里云/腾讯云等），可在服务器上部署 TinyProxy 作为代理。完整教程见 [PROXY_DEPLOY_GUIDE.md](PROXY_DEPLOY_GUIDE.md)。

部署完成后，在 GitHub Secrets 中添加：

```
Name:  YIBAN_PROXY
Value: http://你的服务器IP:8888
```

**方式二：复用已有的服务器代理**

如果已按 [服务器部署](#️-服务器部署进阶) 方案部署了 ECS + TinyProxy，直接用同一个代理地址填入 GitHub Secrets 即可。

> ⚠️ **安全提示**：公网开放的 HTTP 代理可能被第三方滥用。建议平时关闭 8888 端口，仅在 GitHub Actions 需要时临时开放；或改用带密码认证的代理（如 Squid + BasicAuth）。

---

## ⏰ 定时执行

### 默认执行时间

早操签到窗口为 **北京时间 06:30–07:50**，项目每天在该窗口内执行 **2 次**：

| Cron 表达式 | UTC 时间 | 北京时间 | 用途 |
|------------|---------|---------|------|
| `35 22 * * *` | 22:35（前一日） | 06:35 | 首次打卡 |
| `15 23 * * *` | 23:15（前一日） | 07:15 | 容错重试（若首次已成功，遇"已签到"按成功处理） |

> 两次触发均刻意设在窗口中前段，为 GitHub Actions 的触发延迟（5–30 分钟）留出余量，确保实际执行时仍在 06:30–07:50 窗口内（脚本会按服务端返回的签到时间校验）。

### 修改执行时间

编辑 [`.github/workflows/signin.yml`](.github/workflows/signin.yml) 中的 `cron` 字段：

```yaml
on:
  schedule:
    - cron: '35 22 * * *'  # 北京时间 06:35
    - cron: '15 23 * * *'  # 北京时间 07:15
```

**Cron 表达式格式**：`分 时 日 月 周`（UTC 时间，北京时间 = UTC + 8）

> ⚠️ 务必保证触发时间落在签到窗口（06:30–07:50）内并预留延迟余量，否则脚本会因"未在签到时间内"而失败。

> ⏱️ GitHub Actions 的 `schedule` 实际触发时间可能延迟 5–30 分钟（高峰期更久），请勿用于要求精确到分钟的场景。

---

## 🔧 配置说明

### 环境变量一览

| 变量名 | 说明 | 必填 |
|--------|------|------|
| `YIBAN_ACCOUNTS` | 易班账号，格式 `手机号:密码`，多账号用 `#` 分隔 | 二选一 |
| `YIBAN_PHONE` | 易班手机号（单账号） | 二选一 |
| `YIBAN_PASSWORD` | 易班密码（单账号） | 二选一 |
| `YIBAN_PROXY` | 代理地址，如 `http://host:port` 或 `socks5://host:port` | 推荐 |
| `YIBAN_PHONE_MODEL` | 设备型号（如 `Vivo-V2454A`），学校开启设备绑定时必填 | 视情况 |
| `YIBAN_PHONE_CODE` | 设备唯一识别码（64位十六进制字符串），学校开启设备绑定时必填 | 视情况 |
| `YIBAN_NOTIFY_URL` | 通知 webhook URL | 可选 |

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

### 代理配置（重要）

| 代理类型 | 格式 | 说明 |
|---------|------|------|
| HTTP 代理 | `http://host:port` | 推荐，如 TinyProxy |
| 带认证的 HTTP | `http://user:pass@host:port` | 如 Squid + BasicAuth |
| SOCKS5 代理 | `socks5://host:port` | 如 SS5 / Shadowsocks |

> 💡 代理必须是国内出口 IP，否则无法绕过易班 WAF 风控。自建代理教程见 [PROXY_DEPLOY_GUIDE.md](PROXY_DEPLOY_GUIDE.md)。

### 设备绑定（可选）

部分学校在校本化后台开启了「设备绑定」功能，签到时会校验设备型号和唯一识别码，不匹配则返回「请使用授权设备进行签到」错误。如果你的学校开启了此功能，需要配置以下两个环境变量：

| 变量名 | 说明 | 示例 |
|--------|------|------|
| `YIBAN_PHONE_MODEL` | 设备型号（品牌-型号，`:` 需替换为 `-`） | `Vivo-V2454A` |
| `YIBAN_PHONE_CODE` | 设备唯一识别码（64位十六进制字符串） | `6b3788d8...` |

**如何获取设备信息：**

1. 在易班 App 中打开校本化签到页面
2. 通过易班内置的 JS 桥接获取（需借助工具页面）：
   - `yiban.getUUID()` → 返回设备唯一识别码（即 `YIBAN_PHONE_CODE`）
   - `yiban.getDeviceInfo()` → 返回设备信息，其中 `deviceModel` 字段即 `YIBAN_PHONE_MODEL`

> 💡 如果签到时未报「请使用授权设备进行签到」错误，说明你的学校未开启设备绑定，无需配置这两个变量。

---

## 🖥️ 服务器部署（进阶）

<details>
<summary>🔧 展开查看服务器部署方案</summary>

如果你已有国内云服务器，或希望比 GitHub Actions 更稳定、可即时调试，可部署到自己的服务器上。两套方案可同时启用，互为冗余。

### 适用场景

- GitHub Actions 因 WAF 拦截无法稳定运行
- 希望随时手动触发签到或查看日志
- 已有国内云服务器资源

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

#### 3. 配置环境变量

```bash
cat > /opt/yiban-auto-sign/.env << 'EOF'
YIBAN_PHONE=你的手机号
YIBAN_PASSWORD=你的易班密码
YIBAN_PROXY=http://127.0.0.1:8888
EOF
```

> 💡 **关于代理**：阿里云 ECS 的 IP 段也可能被易班 WAF 拦截。建议在同台服务器上部署 TinyProxy，通过本机代理访问易班。TinyProxy 安装与配置见 [PROXY_DEPLOY_GUIDE.md](PROXY_DEPLOY_GUIDE.md)。

#### 4. 创建运行脚本

```bash
cat > /opt/yiban-auto-sign/run.sh << 'EOF'
#!/bin/bash
cd /opt/yiban-auto-sign
export $(cat .env | xargs)
/usr/bin/python3 scripts/signin.py >> /var/log/yiban/sign.log 2>&1
EOF
chmod +x /opt/yiban-auto-sign/run.sh
mkdir -p /var/log/yiban
```

#### 5. 配置 crontab 定时任务

```bash
crontab -e
```

添加以下内容（周一到周六 6:40 和 7:10 各执行一次，周日不签到）：

```cron
# 易班自动签到 - 周一到周六执行
# 6:40 第一次签到（主要）
40 6 * * 1-6 /opt/yiban-auto-sign/run.sh
# 7:10 第二次签到（备用，防止第一次失败）
10 7 * * 1-6 /opt/yiban-auto-sign/run.sh
```

#### 6. 手动测试

```bash
bash /opt/yiban-auto-sign/run.sh
tail -20 /var/log/yiban/sign.log
```

### 常用运维命令

```bash
# 查看签到日志
tail -50 /var/log/yiban/sign.log

# 手动触发签到
bash /opt/yiban-auto-sign/run.sh

# 查看 cron 服务状态
systemctl status cron

# 查看 crontab 配置
crontab -l

# 更新代码后重新部署
scp scripts/signin.py root@服务器IP:/opt/yiban-auto-sign/scripts/
```

### 方案对比

| 特性 | GitHub Actions | 自有服务器 |
|------|---------------|-----------|
| 成本 | 免费（2000 分钟/月） | 需服务器费用 |
| 稳定性 | 受 WAF 拦截影响 | 本机代理更稳定 |
| 调试 | 仅看日志 | 可即时调试 |
| 触发延迟 | 5-30 分钟 | 即时执行 |
| 维护 | 配置后免维护 | 需维护服务器 |

</details>

---

## 🧠 工作原理

<details>
<summary>🔬 展开查看技术实现细节</summary>

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

为保证定位落在有效签到范围内且不暴露固定位置，本项目使用与原 Android 客户端一致的 **缩放质心算法**：

1. 解析签到范围返回的多边形顶点 `Points`
2. 计算多边形质心 `(center_lng, center_lat)`
3. 将多边形顶点向质心收缩 0.7 倍，得到 `scaled_polygon`
4. 在质心附近的边界框内随机生成点（最多 100 次尝试）
5. 校验点是否同时在 `scaled_polygon` 和 `original_polygon` 内
6. 若 100 次均未命中，兜底返回质心

这样每次签到的定位点都不同，但都落在有效范围内，避免被识别为异常定位。

### 重试机制

脚本内置了带指数退避的重试机制，用于处理临时性网络问题或 WAF 拦截：

- **最大重试次数**：3 次
- **基础延迟**：5 秒
- **最大延迟**：60 秒
- **抖动**：每次延迟添加 0-30% 的随机抖动

当遇到以下情况时会自动重试：
- WAF 风控拦截（"风险访问服务禁用"）
- 网络连接超时
- 其他临时性错误

### WAF 拦截检测

易班的 WAF 风控会返回包含「风险访问服务禁用」等关键词的页面。脚本通过以下方式检测：

- **长度检查**：响应超过 2000 字符直接判定为非拦截（正常页面通常较长）
- **关键词匹配**：检测「风险访问」「风控」「访问服务禁用」「WAF」「拦截」
- **Unicode 解码**：WAF 返回 JSON 格式时中文会被 `\uXXXX` 转义，需先解码再匹配

### 60 天限制破解

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

## ❓ 常见问题

<details>
<summary><b>Q1：手动测试报错 "登录失败（账号或密码错误）"</b></summary>

- 确认 `YIBAN_ACCOUNTS` 格式为 `手机号:密码`，密码中不含 `#` 字符
- 如密码含特殊字符，改用 `YIBAN_PHONE` / `YIBAN_PASSWORD` 两个 Secret 分别配置
- 确认账号可在 [https://www.yiban.cn/](https://www.yiban.cn/) 正常登录
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
- **解决方案**：配置 `YIBAN_PROXY` 代理（国内出口），详见 [代理配置](#代理配置重要)
- 脚本会自动重试 3 次，如果仍然失败会标记为错误
</details>

<details>
<summary><b>Q5：报错 "遇到 ydclearance 反爬"</b></summary>

- 已在 `requirements.txt` 中包含 `js2py`，正常情况下不会触发此错误
- 若仍出现，可能是 GitHub Actions IP 被风控，请配置代理
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

---

## ⚠️ 注意事项

1. **本项目仅供学习研究使用**，完整免责声明见 [AI 生成说明](#-ai-生成说明)
2. **强烈建议仓库设为 Private**，避免账号密码被搜索引擎索引
3. 不要将账号密码直接写在代码中，必须使用 GitHub Secrets
4. 请勿频繁调用 API（默认每天 2 次足够），以免触发风控
5. 如账号开启了二次验证，可能需要额外处理
6. **推荐配置代理**：GitHub Actions 的海外 IP 可能被易班风控，配置国内代理可提高稳定性

---

## 🛠️ 本地调试

<details>
<summary>🔧 展开查看本地运行方法</summary>

如需在本地运行：

```bash
# 1. 克隆仓库
git clone https://github.com/<你的用户名>/yiban-auto-sign.git
cd yiban-auto-sign

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量（Windows PowerShell）
$env:YIBAN_ACCOUNTS="13800138000:your_password"

#    或 Linux/macOS
export YIBAN_ACCOUNTS="13800138000:your_password"

# 4. 运行
python scripts/signin.py
```

</details>

---

## 📊 资源消耗

| 项目 | 数值 |
|------|------|
| 每次执行耗时 | 约 10-30 秒 |
| 每日执行次数 | 2 次 |
| 每月消耗 Actions 分钟 | 约 2-5 分钟 |
| GitHub 免费额度 | 2000 分钟/月（公开仓库无限） |
| 费用 | **完全免费** |

---

## 📜 License

MIT License - 见 [LICENSE](LICENSE)

---

## 🙏 致谢

本项目参考了以下开源项目与资料：

- [AEtherside/skland-daily-attendance](https://github.com/AEtherside/skland-daily-attendance) - GitHub Actions 工作流结构与 keepalive 方案
- [Auto-Test](https://github.com/) - 易班登录流程（OAuth + RSA + ydclearance）
- [liskin/gh-workflow-keepalive](https://github.com/liskin/gh-workflow-keepalive) - 60 天限制破解方案
- 原项目 KillYiBan 模块 - nightAttendance 签到流程与多边形定位算法

---

## 🤖 AI 生成说明

### AI 编程生成声明

本项目的代码与文档由 AI 辅助生成，非人工逐行编写：

- **开发工具**：[TRAE IDE](https://www.trae.cn/)（基于 GLM-5.2 模型）
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
