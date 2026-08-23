# ============================================================
# 容器入口：准备数据卷后交给 supervisor 常驻（web + 签到调度）
# ============================================================
set -e
mkdir -p /data/logs /data/state

# 兜底创建 .env：应用首次启动会据此自动生成
#   YIBAN_SECRET_KEY（会话密钥）与账号加密密钥
# （若你已提前写好 ./data/.env，此处不会覆盖已有内容）
[ -f /data/.env ] || : > /data/.env

exec supervisord -c /etc/supervisord.conf