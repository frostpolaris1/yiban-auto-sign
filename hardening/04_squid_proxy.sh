#!/bin/bash
# ============================================
# 安全加固 4：用 Squid + BasicAuth 替代 TinyProxy
# 目标：代理需要密码认证，防止公网滥用
# 风险等级：中（需要切换代理配置）
# ============================================

set -e

echo "=== 安全加固 4：部署 Squid 认证代理 ==="
echo ""

# 配置
SQUID_PORT=3128
SQUID_USER="yibanproxy"
# 生成随机密码
SQUID_PASS=$(openssl rand -base64 16 | tr -dc 'a-zA-Z0-9' | head -c 16)
PROXY_URL="http://${SQUID_USER}:${SQUID_PASS}@127.0.0.1:${SQUID_PORT}"

echo "⚠️  即将部署 Squid 代理，配置如下："
echo "  端口: ${SQUID_PORT}"
echo "  用户名: ${SQUID_USER}"
echo "  密码: ${SQUID_PASS}"
echo ""
echo "  请保存以下代理地址（用于 GitHub Actions 或服务器）："
echo "  ${PROXY_URL}"
echo ""
read -p "确认继续？(y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "已取消"
    exit 0
fi

# 步骤 1：安装 Squid 和 htpasswd 工具
echo "[1/7] 安装 Squid..."
apt-get install -y -qq squid apache2-utils > /dev/null 2>&1
echo "  ✅ Squid 已安装"

# 步骤 2：创建密码文件
echo "[2/7] 创建认证文件..."
htpasswd -cb /etc/squid/passwords "${SQUID_USER}" "${SQUID_PASS}" 2>/dev/null
chown proxy:proxy /etc/squid/passwords
chmod 600 /etc/squid/passwords
echo "  ✅ 密码文件已创建"

# 步骤 3：备份并写入 Squid 配置
echo "[3/7] 配置 Squid..."
cp /etc/squid/squid.conf /etc/squid/squid.conf.bak.$(date +%Y%m%d)

cat > /etc/squid/squid.conf << 'EOF'
# Squid 认证代理配置
# 仅允许本机访问
http_port 127.0.0.1:3128

# 认证配置
auth_param basic program /usr/lib/squid/basic_ncsa_auth /etc/squid/passwords
auth_param basic realm Squid Proxy
auth_param basic credentialsttl 2 hours
acl authenticated proxy_auth REQUIRED
http_access allow authenticated
http_access deny all

# 基本设置
cache deny all
access_log none
cache_log /dev/null
EOF
echo "  ✅ 配置已写入"

# 步骤 4：初始化 Squid
echo "[4/7] 初始化 Squid..."
squid -z 2>/dev/null || true
echo "  ✅ 初始化完成"

# 步骤 5：启动 Squid
echo "[5/7] 启动 Squid..."
systemctl enable squid > /dev/null 2>&1
systemctl restart squid
sleep 2
echo "  ✅ Squid 已启动"

# 步骤 6：测试代理
echo "[6/7] 测试代理..."
TEST_RESULT=$(curl -x "${PROXY_URL}" -s -o /dev/null -w "%{http_code}" --max-time 10 https://www.baidu.com)
if [ "$TEST_RESULT" = "200" ]; then
    echo "  ✅ 代理测试成功！HTTP ${TEST_RESULT}"
else
    echo "  ❌ 代理测试失败，HTTP ${TEST_RESULT}"
    exit 1
fi

# 步骤 7：更新防火墙
echo "[7/7] 更新防火墙..."
ufw allow 3128/tcp comment 'Squid Proxy' > /dev/null 2>&1
echo "  ✅ 防火墙已更新"

echo ""
echo "=== Squid 部署完成 ==="
echo ""
echo "  代理地址: ${PROXY_URL}"
echo ""
echo "  更新服务器 .env："
echo "  YIBAN_PROXY=${PROXY_URL}"
echo ""
echo "  更新 GitHub Actions Secrets："
echo "  YIBAN_PROXY=${PROXY_URL}"
echo ""
echo "  更新服务器 crontab 中的代理地址："
echo "  编辑 /opt/yiban-auto-sign/.env 并修改 YIBAN_PROXY"
echo ""
echo "  保留 TinyProxy 作为备用："
echo "  sudo systemctl stop tinyproxy"
echo "  sudo systemctl disable tinyproxy"
echo ""
echo "  ⚠️ 记得在所有使用代理的地方更新地址！"
