#!/bin/sh
# ============================================================
# 容器入口：准备数据卷后交给 supervisor 常驻（web + 签到调度）
# 本身以 root 运行仅做一次卷准备，随后 supervisord 按 yiban 用户拉起子进程
# （2026-08-27 审查加固 P2-11：业务进程不再以 root 跑）
# ============================================================
set -e
# /data 内状态/凭据/日志文件创建即 0600（完整手机号不入世界可读）
umask 077
# /backups 与 /data 同一处准备：它是 compose 顶层 volumes: 声明的命名卷挂点。
# Docker 首次创建命名卷时挂点是 root:root 0755，而写备份的是下面 supervisord 按
# user=yiban 降级后的 uid 10001 子进程——compose 的 CHOWN/DAC_OVERRIDE 只到本脚本
# （root 阶段）为止，能力随 setuid 丢失，救不了子进程。漏这一步 = 备份天天 EACCES。
mkdir -p /data/logs /data/state /backups

# 兜底创建 .env：应用首次启动会据此自动生成
#   YIBAN_SECRET_KEY（会话密钥）与账号加密密钥
# （若你已提前写好 ./data/.env，此处不会覆盖已有内容）
[ -f /data/.env ] || : > /data/.env

# 数据卷归属专用用户；宿主卷属主受限时给出醒目告警而非静默失败
if ! chown -R yiban:yiban /data 2>/dev/null; then
    echo "警告：/data 归属调整失败（宿主卷属主受限），业务子进程可能无法写入数据卷" >&2
fi
# 备份卷同理，且单独一条分支：/data 受宿主限制而失败时，不能连累 /backups 的准备被跳过
if ! chown -R yiban:yiban /backups 2>/dev/null; then
    echo "警告：/backups 归属调整失败（命名卷属主受限），每日 02:00 备份无法落盘" >&2
fi
# 0700：备份包是密文但含全部密钥的备份，组/其他一律不可读写（命名卷由 Docker 建，
# 默认 0755 世界可读；只 chmod 挂点本身，包内 0600 由 backup-docker.sh 的 umask 管）
if ! chmod 0700 /backups 2>/dev/null; then
    echo "警告：/backups 权限位未能收紧到 0700，备份目录可能被其他用户读取" >&2
fi

exec supervisord -c /etc/supervisord.conf
