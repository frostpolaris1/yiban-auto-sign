#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**功能**
按天状态文件清理入口（宿主 cron 调用；策略见 `yiban/state_gc.py`）。

宿主 `scripts/yiban-cleanup.sh` 只是本脚本的薄包装：策略与实现在 Python 侧唯一
（原先规则写在 bash 里，容器侧另写一份，新增一类按日文件没有机制提醒补规则）。

目录解析与 run.sh 同口径（顺序也一致）：
    YIBAN_STATE_DIR → 默认 /var/log/yiban；日志目录 = dirname(YIBAN_LOG_FILE) → 默认 state_dir
（旧脚本用 `YIBAN_DATA_DIR`，那个键全项目只此一处使用：改为同一套键后，把
YIBAN_STATE_DIR 指到别处的部署也能被正确清理，不会去清默认目录。）

**归属**
部署面脚本（`scripts/`），宿主 cron / 容器调度按文件路径调用；清理策略的**唯一实现**
在 `yiban/state_gc.py`，本脚本只做入口与退出码。

**复用**
`state_dir_from_env` / `log_dir_from_env` 是 `yiban.state_gc` 同名函数的保留绑定
（既有调用方无需改动）；清理函数与 `ARTIFACTS` 登记表复用 `yiban.state_gc`，不另写规则。

**通信**
输入：环境变量（`YIBAN_STATE_DIR` / `YIBAN_LOG_FILE` / 保留期两档）与可选 `argv`。
输出：被清理的文件、`<state_dir>/cleanup.log` 追加行；退出码 0 = 正常（无过期文件
也是 0），1 = 保留期配置非法或目录不可用（响亮失败，不静默退化——静默退化会让磁盘
慢慢涨满而没人发现）。
调用谁：`yiban.state_gc`（策略）、`yiban.clock`。
谁调用：宿主 `scripts/yiban-cleanup.sh`（cron）、容器调度器；`yiban/cli.py state`
子命令与之同源。
前端调用点：无直接调用点；容器调度器的清理结果与保留期设置经 `/api/settings`
（`web/static/js/components/settings-quota.js` 等设置页）暴露给运维。
"""
import datetime
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from yiban import state_gc  # noqa: E402

# 目录解析与 run.sh 同口径（顺序也一致）：YIBAN_STATE_DIR → 默认 /var/log/yiban；
# 日志目录 = dirname(YIBAN_LOG_FILE) → 默认 state_dir。实现在 `yiban/state_gc.py`
# ——CLI（`python -m yiban.cli state`）与本脚本共用同一份，免得出现"配置一样、
# 清理目录不同"；这里保留同名绑定，既有调用方（测试）无需改动。
state_dir_from_env = state_gc.state_dir_from_env
log_dir_from_env = state_gc.log_dir_from_env


def _append_log(log_path, message):
    """追加一行清理日志（0600：与状态目录其它文件同权限）。"""
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(message + "\n")
    except OSError:
        pass


def main(argv=None):
    os.umask(0o077)
    state_dir = state_dir_from_env()
    log_dir = log_dir_from_env(state_dir)
    log_path = os.path.join(state_dir, "cleanup.log")
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not os.path.isdir(state_dir):
        # 状态目录由 run.sh 首次签到时 mkdir -p 建立；此刻仍不存在，要么部署还没跑过
        # 签到，要么 YIBAN_STATE_DIR 配错了。两种都值得让 cron 报出来——**不在这里
        # 造目录**（清理脚本不该创建状态目录，且日志也没地方落）。
        print(f"状态目录不存在，跳过: {state_dir}", file=sys.stderr)
        return 1
    try:
        hold = state_gc.retention_days("log")
        snap = state_gc.retention_days("snapshot")
        removed, detail = state_gc.sweep(state_dir, log_dir)
    except ValueError as e:
        # 与旧脚本"无法计算保留截止日期，跳过"同向：响亮失败，不静默不清理
        _append_log(log_path, f"[{stamp}] 保留期配置非法，跳过清理: {e}")
        print(f"保留期配置非法，跳过清理: {e}", file=sys.stderr)
        return 1
    if state_gc.sweep_empty_cred_state(state_dir):
        removed += 1
        detail.append("cred-state.json（空内容）")
    if removed > 0:
        cutoff = (datetime.date.today() - datetime.timedelta(days=hold)).strftime("%Y-%m-%d")
        _append_log(
            log_path,
            f"[{stamp}] 已清理 {removed} 个过期文件"
            f"（日志/状态保留 {hold} 天，快照保留 {snap} 天，截止 {cutoff}）: "
            + "、".join(detail[:20])
            + ("…" if len(detail) > 20 else ""),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
