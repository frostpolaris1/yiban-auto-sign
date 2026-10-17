#!/usr/bin/env bash
# ============================================================
# handoff.sh —— 把宿主文件交给容器内业务用户 uid 10001
# ============================================================
# 用途：
#   README「Docker 部署 · 启用加密备份」第 ① 步的原命令是
#   `sudo chown 10001:10001 backup-passphrase && sudo chmod 600 backup-passphrase`。
#   部署者以普通用户操作，chown 需要提权。本函数执行同一件事，并用同一权限模型。
#
# 为什么非 root 要走 sudo：
#   容器内备份脚本以 uid 10001（yiban）读 /run/yiban-secrets/backup-passphrase。
#   bind mount 共享宿主 inode；宿主属主不对，容器内必然 EACCES。
#   非 root 宿主上裸 chown 一定失败（EPERM）。文件留在调用者名下（0600），
#   容器内 uid 10001 读不到。GitHub 托管 runner 以 runner 用户执行，带免密 sudo。
#
# 返回码：
#   0  属主已交给 uid 10001。
#   1  本机权限模型不允许，或 chown 自身失败。调用方必须显性 SKIP，不得当绿。
#
# 调用方：scripts/deploy_gate.sh 的 phase_C（C1b 之前）。
# ============================================================

handoff_to_uid10001() {
    # 恰好一个参数：多给会静默丢掉后面的路径，少给会空路径。
    [ "$#" -eq 1 ] || return 1
    local f="$1"
    if [ "$(id -u)" = "0" ]; then
        chown 10001:10001 "$f"
        return $?
    fi
    if command -v sudo >/dev/null 2>&1; then
        # -n：不交互。拿不到免密 sudo 就当场返回非零，不挂在密码提示上。
        sudo -n chown 10001:10001 "$f"
        return $?
    fi
    return 1
}
