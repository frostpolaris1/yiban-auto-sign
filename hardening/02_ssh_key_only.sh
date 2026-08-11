#!/bin/bash
# ============================================
# 安全加固 2：SSH 禁用密码登录，仅允许密钥登录
# 目标：防止暴力破解 SSH 密码
# 风险等级：中（必须确保密钥已配置好！）
# ⚠️ 执行前必须确认：你的 SSH 密钥已经能正常登录！
# ============================================

set -e

echo "=== 安全加固 2：SSH 密钥认证 ==="
echo ""

# 步骤 1：检查当前登录方式
echo "[1/5] 检查当前 SSH 认证方式..."
echo "  当前 sshd_config 中的关键配置："
grep -E "^(PasswordAuthentication|PermitRootLogin|PubkeyAuthentication)" /etc/ssh/sshd_config || echo "  （使用默认值）"
echo ""

# 步骤 2：检查用户是否有 authorized_keys
echo "[2/5] 检查 SSH 密钥..."
if [ -f ~/.ssh/authorized_keys ]; then
    KEY_COUNT=$(wc -l < ~/.ssh/authorized_keys)
    echo "  ✅ 找到 authorized_keys，包含 $KEY_COUNT 个密钥"
else
    echo "  ❌ 未找到 ~/.ssh/authorized_keys"
    echo "  请先添加你的 SSH 公钥到此文件！"
    echo "  方法：在本地执行 ssh-copy-id root@你的服务器IP"
    exit 1
fi

# 步骤 3：备份原始配置
echo "[3/5] 备份 SSH 配置..."
cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak.$(date +%Y%m%d)
echo "  ✅ 已备份到 /etc/ssh/sshd_config.bak.*"

# 步骤 4：修改 SSH 配置（禁用密码，启用密钥）
echo "[4/5] 修改 SSH 配置..."
# 禁用密码认证
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
# 启用公钥认证
sed -i 's/^#*PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
# 禁用 root 密码登录（允许密钥登录 root）
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config

echo "  ✅ 配置已修改："
grep -E "^(PasswordAuthentication|PermitRootLogin|PubkeyAuthentication)" /etc/ssh/sshd_config
echo ""

# 步骤 5：重启 SSH 服务
echo "[5/5] 重启 SSH 服务..."
systemctl restart sshd
echo "  ✅ SSH 服务已重启"

echo ""
echo "=== 验证 ==="
echo "  ⚠️ 重要：不要关闭当前 SSH 连接！"
echo "  请新开一个终端窗口，测试以下命令能否登录："
echo "    ssh root@你的服务器IP"
echo ""
echo "  如果新窗口能登录，说明密钥认证成功。"
echo "  如果新窗口不能登录，请用当前窗口执行回滚："
echo "    cp /etc/ssh/sshd_config.bak.* /etc/ssh/sshd_config"
echo "    systemctl restart sshd"
echo ""
echo "✅ SSH 加固步骤完成（请新开终端验证）"
