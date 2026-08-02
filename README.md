# 易班自动签到

基于 Python 的易班（yiban）自动签到项目，支持 **双方案互为冗余** 部署：云服务器（主力）+ GitHub Actions（备份），任一方案均可独立完成每日签到，配置一次即可稳定运行。

> 本项目是橙星（oranje-star）的延伸子项目，将原 Android 客户端中的易班签到逻辑移植为 Python 脚本。

## 🛡️ 双方案冗余架构

| 方案 | 定位 | 优势 | 适用场景 |
|------|------|------|---------|
| 云服务器部署（阿里云 ECS + TinyProxy） | **主力** | 即时执行、可调试、本机代理绕过 WAF | 已有国内云服务器资源 |
| GitHub Actions | **备份** | 免费、免维护、无需服务器 | 云服务器故障或维护时的兜底 |

> 💡 **推荐做法**：两套方案同时启用，签到时间错开（如云服务器 06:40、GitHub Actions 07:15），任一成功即可。详见下文部署章节。

---

## ✨ 功能特点

- 🤖 **全自动签到**：每天定时执行，无需人工干预
- 📍 **智能定位**：在签到范围内生成随机定位点，模拟真实 GPS（缩放质心算法）
- 👥 **多账号支持**：一个仓库管理多个易班账号
- 🔔 **消息通知**：签到失败时推送通知（Server 酱 / Bark / 企业微信等）
- 🆓 **完全免费**：使用 GitHub Actions 免费额度，每月消耗约 60 分钟（远低于 2000 分钟配额）
- ⏰ **永久运行**：内置 `gh-workflow-keepalive`，自动破解 GitHub 60 天无活动禁用限制
- 🔄 **自动重试**：遇到 WAF 拦截时自动重试（指数退避）
- 🎲 **随机延迟**：启动时随机延时，避免并发触发风控

---

## 📁 项目结构

```
yiban-auto-sign/
├── .github/
│   └── workflows/
│       └── signin.yml          # 签到工作流（含 keepalive）
├── scripts/
│   └── signin.py               # 核心签到脚本
├── .env.example                # 环境变量示例
├── .gitee-ci.yml               # Gitee Go 工作流配置
├── .gitignore
├── requirements.txt            # Python 依赖
├── run.sh                      # 服务器部署运行脚本
├── PROXY_DEPLOY_GUIDE.md       # TinyProxy 代理部署指南
├── LICENSE
└── README.md                   # 本文件（部署教程）
```

---

## 🚀 方案一：GitHub Actions 部署（备份方案，免费免维护）

### 第 1 步：Fork 或导入仓库

**方式一：Fork（推荐）**

如果你已将本项目推送到 GitHub，直接点击页面右上角的 **Fork** 按钮，将仓库复制到你的账号下。

**方式二：新建仓库并上传**

1. 在 GitHub 上点击右上角 `+` → `New repository`
2. 仓库名填 `yiban-auto-sign`（可自定义）
3. **可见性建议选 Private（私有）**，避免泄露账号信息
4. 点击 `Create repository`
5. 在本地将本项目代码推送到该仓库：

```bash
cd yiban-auto-sign
git init
git add .
git commit -m "feat: 初始化易班自动签到项目"
git branch -M main
git remote add origin https://github.com/<你的用户名>/yiban-auto-sign.git
git push -u origin main
```

### 第 2 步：配置 Secrets（关键）

进入你 fork 的仓库，依次点击 **`Settings`** → **`Secrets and variables`** → **`Actions`** → **`New repository secret`**，添加以下密钥：

| Secret 名称 | 说明 | 是否必填 |
|------------|------|---------|
| `YIBAN_ACCOUNTS` | 易班账号，格式 `手机号:密码`，多账号用 `#` 分隔 | 二选一必填 |
| `YIBAN_PHONE` | 易班手机号（单账号，向后兼容） | 二选一必填 |
| `YIBAN_PASSWORD` | 易班密码（单账号，向后兼容） | 二选一必填 |
| `YIBAN_NOTIFY_URL` | 通知 webhook URL（Server 酱 / Bark 等） | 可选 |
| `YIBAN_PROXY` | 代理地址（如 `http://user:pass@host:port` 或 `socks5://host:port`） | **推荐配置** |

**配置示例：**

**单账号（推荐用 YIBAN_ACCOUNTS）：**
```
Name:  YIBAN_ACCOUNTS
Value: 13800138000:your_password
```

**多账号：**
```
Name:  YIBAN_ACCOUNTS
Value: 13800138000:pwd1#13900139000:pwd2#13700137000:pwd3
```

**代理配置（推荐）：**
```
Name:  YIBAN_PROXY
Value: http://myproxy:password123@1.2.3.4:7890
```

> ⚠️ **密码中如包含 `:` 或 `#` 字符**：请改用单账号的 `YIBAN_PHONE` / `YIBAN_PASSWORD` 配置。

> 💡 **关于代理**：GitHub Actions 的海外 IP 可能被易班 WAF 风控拦截。如果遇到"风险访问服务禁用"错误，请配置 `YIBAN_PROXY` 代理（国内出口）后重试。

#### 复用方案二的 ECS 代理（推荐）

如果已按方案二部署了阿里云 ECS + TinyProxy，可直接把同一代理用于 GitHub Actions，无需另建代理：

1. 确认 ECS 安全组已放行 **8888** 端口（GitHub Actions 出口 IP 不固定，需允许所有来源）
2. 确认 `/etc/tinyproxy/tinyproxy.conf` 中 `Allow 127.0.0.1` 已注释掉（允许外网访问）
3. 在 GitHub 仓库 Secrets 中配置：

```
Name:  YIBAN_PROXY
Value: http://你的ECS公网IP:8888
```

例如 ECS 公网 IP 为 `120.26.23.83`，则填入 `http://120.26.23.83:8888`。

> ⚠️ **安全提示**：公网开放的 HTTP 代理可能被第三方滥用扫流量。强烈建议：
> - 通过 TinyProxy 的 `Allow` 指令限制来源 IP（但 GitHub Actions IP 动态，需放行较大范围）
> - 或改用带 BasicAuth 的代理（如 Squid），配置 `http://user:pass@IP:port`
> - 或仅在云服务器维护期间临时启用公网代理，平时关闭 8888 端口
>
> 详见 [PROXY_DEPLOY_GUIDE.md](PROXY_DEPLOY_GUIDE.md)。

### 第 3 步：启用 GitHub Actions

1. 进入仓库的 **`Actions`** 标签页
2. 如果看到提示，点击 **`I understand my workflows, go ahead and enable them`** 启用工作流
3. 左侧应能看到名为 **`Yiban Sign-in`** 的工作流

### 第 4 步：手动测试

部署完成后，先手动触发一次确认配置无误：

1. 进入 `Actions` 标签页
2. 左侧选择 **`Yiban Sign-in`** 工作流
3. 点击右侧的 **`Run workflow`** 按钮
4. 分支选 `main`，点击绿色的 **`Run workflow`** 确认执行
5. 等待约 30 秒后刷新页面，点击本次运行查看日志

**日志中出现以下内容即代表签到成功：**
```
[2026-08-01 06:35:01] [INFO] ==== 开始执行签到，共 1 个账号 ====
[2026-08-01 06:35:01] [INFO] 随机延迟 45 秒，避免触发风控...
[2026-08-01 06:35:46] [INFO] [13800138000] 登录成功
[2026-08-01 06:35:47] [INFO] [13800138000] 生成定位: (118.789,32.045) 地址: XX大学
[2026-08-01 06:35:48] [INFO] [13800138000] ✅ 签到成功
[2026-08-01 06:35:48] [INFO] ==== 签到汇总 ====
[2026-08-01 06:35:48] [INFO]   ✅ 13800138000: 签到成功
```

---

## ⏰ 定时说明与调整

### 默认执行时间

早操签到窗口为 **北京时间 06:30–07:50**，项目每天在该窗口内执行 **2 次**：

| Cron 表达式 | UTC 时间 | 北京时间 | 用途 |
|------------|---------|---------|------|
| `30 22 * * *` | 22:30（前一日） | 06:30 | 首次打卡 |
| `15 23 * * *` | 23:15（前一日） | 07:15 | 容错重试（若首次已成功，遇"已签到"按成功处理） |

> 两次触发均刻意设在窗口中前段，为 GitHub Actions 的触发延迟（5–30 分钟）留出余量，确保实际执行时仍在 06:30–07:50 窗口内（脚本会按服务端返回的签到时间校验）。

### 修改执行时间

编辑 [`.github/workflows/signin.yml`](.github/workflows/signin.yml) 中的 `cron` 字段：

```yaml
on:
  schedule:
    - cron: '30 22 * * *'  # 北京时间 06:30
    - cron: '15 23 * * *'  # 北京时间 07:15
```

**Cron 表达式格式**：`分 时 日 月 周`（UTC 时间，北京时间 = UTC + 8）

> ⚠️ 务必保证触发时间落在签到窗口（06:30–07:50）内并预留延迟余量，否则脚本会因"未在签到时间内"而失败。

> ⏱️ GitHub Actions 的 `schedule` 实际触发时间可能延迟 5–30 分钟（高峰期更久），请勿用于要求精确到分钟的场景。

---

## 🔔 消息通知配置（可选）

签到失败时会自动推送通知。支持多种通知渠道：

### Server 酱（微信推送）

1. 访问 [https://sct.ftqq.com/](https://sct.ftqq.com/) 注册并获取 SendKey
2. 将 URL 填入 `YIBAN_NOTIFY_URL`：
   ```
   https://sctapi.ftqq.com/YOUR_SENDKEY.send
   ```

### Bark（iOS 推送）

```
https://api.day.app/YOUR_KEY/易班签到通知
```

### 企业微信群机器人

1. 企业微信群 → 群设置 → 群机器人 → 添加机器人
2. 复制 webhook URL 填入 `YIBAN_NOTIFY_URL`：
   ```
   https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY
   ```

---

## 🧠 工作原理

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

### 随机延迟

脚本启动时会随机延迟 0-120 秒，避免多个账号同时请求触发风控。

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

---

## 🖥️ 方案二：自有服务器部署（主力方案，更稳定）

如果希望更稳定、可即时调试，或作为 GitHub Actions 的兜底，可部署到国内服务器（如阿里云 ECS）。

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

---

## ❓ 常见问题

### Q1：手动测试报错 "登录失败（账号或密码错误）"

- 确认 `YIBAN_ACCOUNTS` 格式为 `手机号:密码`，密码中不含 `#` 字符
- 如密码含特殊字符，改用 `YIBAN_PHONE` / `YIBAN_PASSWORD` 两个 Secret 分别配置
- 确认账号可在 [https://www.yiban.cn/](https://www.yiban.cn/) 正常登录

### Q2：报错 "获取签到任务失败"

- 你的学校可能未开启晚间考勤（nightAttendance）任务
- 该接口仅适用于开启「晚间考勤 / 晚签到」功能的学校
- 如需打卡的是「每日打卡」（officeTask），需修改脚本中的 API 路径

### Q3：报错 "未在签到时间内"

- 当前时间不在管理员设置的签到时间窗口内
- 脚本启动时会随机延时 0-120 秒（`random_delay()`），若仍触发此错误，可能是学校签到窗口有调整
- 可调整 `scripts/signin.py` 中 `RANDOM_DELAY_MAX` 常量，或等待下一次定时触发
- 此错误**不会**导致 GitHub Actions 标记为失败（退出码仍为 0）

### Q4：报错 "风险访问服务禁用" / WAF 风控拦截

- **原因**：GitHub Actions 的海外 IP 被易班 WAF 风控拦截
- **解决方案**：配置 `YIBAN_PROXY` 代理（国内出口）
- 脚本会自动重试 3 次，如果仍然失败会标记为错误

### Q5：报错 "遇到 ydclearance 反爬"

- 已在 `requirements.txt` 中包含 `js2py`，正常情况下不会触发此错误
- 若仍出现，可能是 GitHub Actions IP 被风控，请配置代理

### Q6：签到成功但 GitHub Actions 显示失败

- 检查日志中是否有 "❌" 标记的账号
- 多账号场景下，只要有一个**真正的失败**（非"未在签到时间内"），整体退出码就是 1
- "未在签到时间内"不会导致失败（这是正常行为）

### Q7：定时任务不执行 / 突然停止

- 进入 `Actions` 页面确认工作流是否被禁用（被禁用会有醒目提示）
- 点击 `Enable workflow` 重新启用
- keepalive 会在下次定时触发时自动处理，但首次需手动启用

### Q8：如何查看签到历史

- 进入仓库 `Actions` 标签页
- 左侧选择 `Yiban Sign-in`
- 可看到所有历史运行记录，点击进入可查看详细日志

---

## 📊 资源消耗

| 项目 | 数值 |
|------|------|
| 每次执行耗时 | 约 1–5 分钟（含随机延时） |
| 每日执行次数 | 2 次 |
| 每月消耗 Actions 分钟 | 约 60–120 分钟 |
| GitHub 免费额度 | 2000 分钟/月（公开仓库无限） |
| 费用 | **完全免费** |

---

## ⚠️ 注意事项

1. **本项目仅供学习研究使用**，请遵守易班用户协议，由此产生的任何后果由使用者自负
2. **强烈建议仓库设为 Private**，避免账号密码被搜索引擎索引
3. 不要将账号密码直接写在代码中，必须使用 GitHub Secrets
4. 请勿频繁调用 API（默认每天 2 次足够），以免触发风控
5. 如账号开启了二次验证，可能需要额外处理
6. **推荐配置代理**：GitHub Actions 的海外 IP 可能被易班风控，配置国内代理可提高稳定性

---

## 🛠️ 本地调试

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
