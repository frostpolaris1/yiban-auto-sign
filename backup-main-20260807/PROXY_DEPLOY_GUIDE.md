# 自建代理服务器完整部署指南

你的核心需求：**让 CI/CD（GitHub Actions / Gitee Go）通过国内代理访问易班，绕过 WAF 风控**。

---

## 一、方案对比

| 方案 | 难度 | 费用 | 稳定性 | 推荐度 |
|------|------|------|--------|--------|
| A. 国内 VPS + TinyProxy | ⭐ | 30-50元/月 | ⭐⭐⭐⭐ | ✅ 推荐 |
| B. 国内 VPS + SS5 | ⭐⭐ | 30-50元/月 | ⭐⭐⭐ | 可选 |
| C. 国内 VPS + Shadowsocks | ⭐⭐ | 30-50元/月 | ⭐⭐⭐⭐ | ✅ 推荐 |
| D. 朋友闲置电脑 + frp | ⭐ | 免费 | ⭐⭐ | 备用 |

---

## 二、准备工作：购买国内 VPS

### 推荐配置

| 配置 | 最低要求 |
|------|---------|
| CPU | 1 核 |
| 内存 | 512MB-1GB |
| 带宽 | 5Mbps |
| 流量 | 10GB/月（够用） |
| 系统 | CentOS 7/8 或 Ubuntu 20.04/22.04 |
| 位置 | 中国大陆（上海/广州/深圳） |

### 推荐服务商

| 服务商 | 最低价格 | 特点 |
|---------|---------|------|
| **阿里云 ECS** | 约 40 元/月（新用户更低） | 稳定、靠谱 |
| **腾讯云轻量** | 约 35 元/月 | 新用户优惠大 |
| **华为云 ECS** | 约 40 元/月 | 企业级 |
| **Oracle Cloud** | **免费** | 海外 IP（不适合本场景） |

> ⚠️ **注意**：必须选择**中国大陆节点**，海外 IP 同样会被易班 WAF 拦截。

---

## 三、方案 A：VPS + TinyProxy（推荐）

### 步骤 1：连接 VPS

```bash
ssh root@你的VPS的IP
```

### 步骤 2：安装 TinyProxy

**CentOS/RHEL：**
```bash
# 安装 EPEL 仓库
yum install -y epel-release

# 安装 TinyProxy
yum install -y tinyproxy
```

**Ubuntu/Debian：**
```bash
apt update
apt install -y tinyproxy
```

### 步骤 3：配置 TinyProxy

编辑配置文件：
```bash
vi /etc/tinyproxy/tinyproxy.conf
```

修改以下关键参数：
```ini
# 监听端口（默认 8888）
Port 8888

# 允许访问的 IP（注释掉则允许所有 IP，公网不建议）
# Allow 127.0.0.1

# 不显示代理头（隐身模式）
DisableViaHeader Yes
```

### 步骤 4：启动服务

```bash
systemctl enable tinyproxy
systemctl start tinyproxy

# 检查状态
systemctl status tinyproxy
```

### 步骤 5：开放端口

在 VPS 控制面板中开放端口 **8888**（或你自定义的端口）。

**防火墙操作（CentOS）：**
```bash
firewall-cmd --permanent --add-port=8888/tcp
firewall-cmd --reload
```

### 步骤 6：获取代理地址

```bash
# 查看 VPS 的公网 IP
curl ifconfig.me
```

代理地址格式：
```
http://你的VPS的IP:8888
```

---

## 四、方案 B：VPS + SS5（SOCKS5 代理）

### 步骤 1：安装依赖

```bash
yum install -y gcc gcc-c++ automake make pam-devel openldap-devel cyrus-sasl-devel openssl-devel
```

### 步骤 2：下载编译 SS5

```bash
cd /tmp
wget https://sourceforge.net/projects/ss5/files/ss5/3.8.9-8/ss5-3.8.9-8.tar.gz
tar -zxvf ss5-3.8.9-8.tar.gz
cd ss5-3.8.9
./configure
make && make install
```

### 步骤 3：配置 SS5

```bash
chmod a+x /etc/init.d/ss5
vi /etc/opt/ss5/ss5.conf
```

取消以下两行的注释：
```auth    0.0.0.0/0.0.0.0       -       -
permit    -       0.0.0.0/0.0.0.0/0.0.0.0/0.0.0.0       -       -       -
```

### 步骤 4：启动并设置开机自启

```bash
service ss5 start
chkconfig --add ss5
chkconfig ss5 on
```

### 步骤 5：获取代理地址

代理地址格式：
```
socks5://你的VPS的IP:1080
```

---

## 五、方案 C：VPS + Shadowsocks

### 步骤 1：安装 Shadowsocks

```bash
pip3 install shadowsocks
```

### 步骤 2：创建配置文件

```bash
vi /etc/shadowsocks.json
```

写入：
```json
{
    "server": "0.0.0.0",
    "server_port": 50001,
    "local_port": 1080,
    "password": "你的强密码",
    "timeout": 600,
    "method": "aes-256-cfb"
}
```

### 步骤 3：创建系统服务

```bash
vi /etc/systemd/system/shadowsocks.service
```

写入：
```ini
[Unit]
Description=Shadowsocks
[Service]
TimeoutStartSec=0
ExecStart=/usr/local/bin/ssserver -c /etc/shadowsocks.json
[Install]
WantedBy=multi-user.target
```

### 步骤 4：启动服务

```bash
systemctl enable shadowsocks
systemctl start shadowsocks
```

---

## 六、在 CI/CD 中使用

### 在 GitHub Actions 中设置

进入 GitHub 仓库 → `Settings` → `Secrets and variables` → `Actions` → `New repository secret`：

| 变量名 | 变量值 |
|--------|--------|
| `YIBAN_PROXY` | `http://VPS的IP:8888`（TinyProxy）|
| `YIBAN_PROXY` | `socks5://VPS的IP:1080`（SS5/Shadowsocks）|

### 在 Gitee Go 中设置

进入 Gitee 仓库 → 流水线编辑器 → 环境变量配置：

| 变量名 | 变量值 |
|--------|--------|
| `YIBAN_PROXY` | `http://VPS的IP:8888`（TinyProxy）|
| `YIBAN_PROXY` | `socks5://VPS的IP:1080`（SS5/Shadowsocks）|

### 在脚本中验证

脚本已内置代理支持，无需修改代码：
```python
# signin.py 中已有
proxy = os.environ.get('YIBAN_PROXY', '').strip()
if proxy:
    self.session.proxies = {'http': proxy, 'https': proxy}
```

---

## 七、常见问题

### Q1：VPS 的 IP 被易班 WAF 拦截怎么办？

**原因**：部分云厂商的 IP 段被网站风控。

**解决方案**：
- 更换 VPS 服务商（如阿里云换腾讯云）
- 更换 VPS 的 IP 地址（重新分配或购买弹性 IP）

### Q2：代理速度慢怎么办？

**原因**：VPS 带宽不足或跨运营商。

**解决方案**：
- 选择 BGP 多线 VPS
- 选择与你 VPS 同运营商的节点

### Q3：TinyProxy 和 SS5 哪个更好？

| 对比 | TinyProxy | SS5 |
|------|----------|-----|
| 协议 | HTTP/HTTPS | SOCKS5 |
| 难度 | 简单 | 中等 |
| 兼容性 | Python requests 原生支持 | PySocks 已安装 |
| 推荐 | ✅ | 可选 |

### Q4：免费的 VPS 有哪些？

**适合场景**：测试用

| 服务商 | 特点 |
|--------|------|
| Oracle Cloud 免费 tier | 海外 IP（不适合本场景）|
| 各厂商新用户试用 | 通常 1-3 个月 |

---

## 八、最终推荐

**推荐方案：阿里云/腾讯云 国内 VPS + TinyProxy**

理由：
1. 国内 IP 不被易班 WAF 拦截
2. TinyProxy 配置简单，5 分钟搞定
3. 费用低（约 30-50 元/月）
4. Python requests 原生支持 HTTP 代理

---

部署完成后，把代理地址告诉我，我帮你验证配置。
