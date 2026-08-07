# 服务器安全加固脚本

本目录包含 4 个安全加固脚本，按顺序执行可显著提升服务器安全性。

## ⚠️ 重要提醒

1. **执行前备份**：脚本会自动备份关键配置，但建议先创建服务器快照
2. **逐个执行**：不要一次性运行所有脚本，每步验证后再继续
3. **保持连接**：SSH 相关操作时，不要关闭当前连接，用新窗口验证

## 📋 执行顺序

| 顺序 | 脚本 | 内容 | 风险 |
|------|------|------|------|
| 1 | `01_firewall.sh` | 启用 UFW 防火墙 | 低 |
| 2 | `02_ssh_key_only.sh` | SSH 禁用密码登录 | 中 |
| 3 | `03_security_updates.sh` | 安装安全更新 | 低 |
| 4 | `04_squid_proxy.sh` | Squid 认证代理替代 TinyProxy | 中 |

## 🚀 使用方法

### 方式一：逐个执行（推荐）

```bash
# 1. 上传脚本到服务器
scp hardening/*.sh root@120.26.23.83:/tmp/

# 2. SSH 登录服务器
ssh root@120.26.23.83

# 3. 逐个执行
chmod +x /tmp/*.sh
/tmp/01_firewall.sh    # 防火墙
/tmp/02_ssh_key_only.sh # SSH 密钥
/tmp/03_security_updates.sh # 安全更新
/tmp/04_squid_proxy.sh # Squid 代理
```

### 方式二：一键执行（仅限熟悉后）

```bash
scp hardening/*.sh root@120.26.23.83:/tmp/
ssh root@120.26.23.83 "chmod +x /tmp/*.sh && /tmp/01_firewall.sh && /tmp/02_ssh_key_only.sh && /tmp/03_security_updates.sh && /tmp/04_squid_proxy.sh"
```

## 🔄 回滚方法

如果某步出错，可以回滚：

```bash
# SSH 配置回滚
cp /etc/ssh/sshd_config.bak.* /etc/ssh/sshd_config
systemctl restart sshd

# 防火墙回滚
ufw --force reset
ufw default allow incoming
ufw disable

# Squid 卸载
systemctl stop squid
apt-get remove -y squid
systemctl start tinyproxy
```

## 📊 加固效果

完成所有步骤后：
- ✅ 防火墙仅开放必要端口
- ✅ SSH 仅允许密钥登录，防暴力破解
- ✅ 系统漏洞已修复
- ✅ 代理需要密码认证，防滥用
