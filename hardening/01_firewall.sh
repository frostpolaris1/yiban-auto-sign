#!/bin/bash
# ============================================
# 安全加固 1：启用 UFW 防火墙
# 目标：仅开放 SSH(22) 和 TinyProxy(8888)，阻止其他所有入站
# 风险等级：低（确保 22 先开放，不会锁死自己）
# ============================================

set -e

echo "=== 安全加固 1：启用 UFW 防火墙 ==="
echo ""

# 步骤 1：检查 UFW 是否安装
echo "[1/6] 检查 UFW..."
if ! command -v ufw &> /dev/null; then
    apt-get install -y ufw > /dev/null 2>&1
    echo "  ✅ UFW 已安装"
else
    echo "  ✅ UFW 已存在"
fi

# 步骤 2：重置为默认状态（拒绝所有入站，允许所有出站）
echo "[2/6] 重置防火墙规则..."
ufw --force reset > /dev/null 2>&1
echo "  ✅ 已重置"

# 步骤 3：默认策略：拒绝所有入站，允许所有出站
echo "[3/6] 设置默认策略..."
ufw default deny incoming > /dev/null 2>&1
ufw default allow outgoing > /dev/null 2>&1
echo "  ✅ 默认：拒绝入站，允许出站"

# 步骤 4：开放 SSH（端口 22）—— 必须先开放，否则锁死自己！
echo "[4/6] 开放 SSH (端口 22)..."
ufw allow 22/tcp comment 'SSH' > /dev/null 2>&1
echo "  ✅ SSH 已开放"

# 步骤 5：开放 TinyProxy（端口 8888）
echo "[5/6] 开放 TinyProxy (端口 8888)..."
ufw allow 8888/tcp comment 'TinyProxy' > /dev/null 2>&1
echo "  ✅ TinyProxy 已开放"

# 步骤 6：启用防火墙
echo "[6/6] 启用防火墙..."
echo "y" | ufw enable > /dev/null 2>&1
echo "  ✅ 防火墙已启用"

# 验证
echo ""
echo "=== 防火墙状态 ==="
ufw status verbose
echo ""
echo "✅ 防火墙加固完成！"
echo "   开放端口: 22(SSH), 8888(TinyProxy)"
echo "   默认策略: 拒绝所有入站，允许所有出站"
