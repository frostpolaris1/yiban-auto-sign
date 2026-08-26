# ============================================================
# 容器入口：准备数据卷后交给 supervisor 常驻（web + 签到调度）
# 本身以 root 运行仅做一次卷准备，随后 supervisord 按 yiban 用户拉起子进程
# （2026-08-27 审查加固 P2-11：业务进程不再以 root 跑）
# ============================================================
set -e
mkdir -p /data/logs /data/state

# 兜底创建 .env：应用首次启动会据此自动生成
#   YIBAN_SECRET_KEY（会话密钥）与账号加密密钥
# （若你已提前写好 ./data/.env，此处不会覆盖已有内容）
[ -f /data/.env ] || : > /data/.env

# 数据卷归属专用用户；宿主卷属主受限时给出醒目告警而非静默失败
if ! chown -R yiban:yiban /data 2>/dev/null; then
    echo "警告：/data 归属调整失败（宿主卷属主受限），业务子进程可能无法写入数据卷" >&2
fi

exec supervisord -c /etc/supervisord.conf
