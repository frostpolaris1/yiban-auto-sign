#!/bin/bash
# ============================================
# 安全加固 3：安装安全更新
# 目标：修复已知漏洞
# 风险等级：低（建议先查看更新内容）
# ============================================

set -e

echo "=== 安全加固 3：安装安全更新 ==="
echo ""

# 步骤 1：更新软件包列表
echo "[1/4] 更新软件包列表..."
apt-get update -qq
echo "  ✅ 已更新"

# 步骤 2：查看可升级的包
echo "[2/4] 可升级的包："
apt list --upgradable 2>/dev/null | grep -v "Listing..." | head -20
echo ""

# 步骤 3：执行升级
echo "[3/4] 执行升级..."
apt-get upgrade -y -qq 2>&1 | tail -5
echo "  ✅ 升级完成"

# 步骤 4：清理旧包
echo "[4/4] 清理..."
apt-get autoremove -y -qq 2>&1 | tail -3
apt-get autoclean -y -qq 2>&1 | tail -3
echo "  ✅ 清理完成"

echo ""
echo "=== 升级后状态 ==="
echo "  待更新: $(apt list --upgradable 2>/dev/null | grep -c 'upgradable') 个包"
echo ""
echo "✅ 安全更新完成"
