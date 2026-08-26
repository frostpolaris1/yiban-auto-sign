#!/usr/bin/env python3
"""容器内签到调度：复刻宿主 cron 的语义。

- 06:31 首签 / 07:10 补签（当天已 SUCCESS 则跳过）
- 23:55 探针入口（signin.py --probe 内部自判触发时间/频率）
- 每日 03:00 清理 /data/logs 下 365 天前的按天日志

数据/配置路径由 compose 注入的 YIBAN_* 环境变量决定；同时把 YIBAN_ENV_FILE
指向的 .env（Web 设置页写入）解析后注入子进程环境——否则 Web 后台改的
探针/周日/暂停等开关对 signin 子进程不可见（2026-08-27 对抗性审查 P1-1：
原实现只继承 compose 环境变量，Docker 部署下这些设置静默失效）。

tick 采用「分钟级到点闩锁」而非「秒==0 命中」：调度循环被签到子进程阻塞、
错过整分第 0 秒时，进入目标分钟后仍会补触发一次（同日去重防重复），
不再整天丢失（P2-10）。
"""
import os
import subprocess
import time
from datetime import datetime, timedelta

STATEDIR = os.environ.get("YIBAN_STATE_DIR", "/data/state")
LOGDIR = os.path.dirname(os.environ.get("YIBAN_LOG_FILE", "/data/logs/sign.log"))
ENV_FILE = os.environ.get("YIBAN_ENV_FILE", "/data/.env")


def _parse_env_file(path):
    """逐行解析 .env：返回 {YIBAN_ 开头的合法键: 值}。

    与 run.sh 同口径：忽略空行/# 注释行，按首个 = 切分并 strip；
    非 YIBAN_ 前缀与非法键名一律丢弃（不向子进程注入无关变量）。
    """
    out = {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if not key.startswith("YIBAN_"):
                    continue
                out[key] = val.strip()
    except OSError:
        pass
    return out


def build_child_env(env_file=ENV_FILE, base=None):
    """构造签到/探针子进程环境：compose 环境为底座，.env 的 YIBAN_* 键覆盖注入。

    仅白名单键（YIBAN_ 前缀）可覆盖；.env 缺失/损坏时安静退化为纯继承，
    行为等同旧版。文件值优先于外部环境：这是 Web 设置页能生效的关键。
    """
    env = dict(base) if base is not None else dict(os.environ)
    env.update(_parse_env_file(env_file))
    return env


def _state_file():
    return os.path.join(STATEDIR, f"sign-status-{datetime.now():%Y-%m-%d}.txt")


def _signed_today():
    """对齐 run.sh 语义：当天状态文件为 SUCCESS 即视为已签到，跳过。"""
    try:
        with open(_state_file(), encoding="utf-8") as fh:
            return fh.read().strip() == "SUCCESS"
    except OSError:
        return False


def _cleanup_logs():
    """删除 365 天前的按天日志（sign-YYYY-MM-DD.log），对齐 scripts/yiban-cleanup.sh。"""
    cutoff = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    if not os.path.isdir(LOGDIR):
        return
    for name in os.listdir(LOGDIR):
        if name.startswith("sign-") and name.endswith(".log"):
            key = name[len("sign-"):-len(".log")]
            if key < cutoff:
                try:
                    os.remove(os.path.join(LOGDIR, name))
                except OSError:
                    pass


# 首签 / 补签 时间点（分钟级）；探针入口时刻：signin.py --probe 内部
# 自行判断是否到触发时间/频率（含 once 单次）。三者都用「已进入该分钟且
# 当天未执行过」的闩锁语义，见 main_loop。
FIRST, SECOND = (6, 31), (7, 10)
PROBE_AT = (23, 55)


def main_loop(sleep_seconds=1):
    """调度主循环。闩锁按 (任务, 当日) 记账；进程重启视为新一天可再执行。

    SECOND 不受 FIRST 影响：若首签子进程一直占用到越过 07:10，循环恢复后
    hm>=SECOND 仍会补一次（signin 内状态文件已防重），不再全天丢失补签。
    """
    done_sign_first = None   # date | None
    done_sign_second = None
    done_probe = None
    last_clean = None
    child_env = build_child_env()
    while True:
        now = datetime.now()
        today = now.date()
        hm = (now.hour, now.minute)
        if hm >= FIRST and done_sign_first != today and not _signed_today():
            # 与 run.sh 唯一实质差异：容器内无需 flock/宿主绝对路径，状态文件已防重
            subprocess.run(["python3", "scripts/signin.py"], cwd="/app", env=child_env)
            done_sign_first = today
        if hm >= SECOND and done_sign_second != today and not _signed_today():
            subprocess.run(["python3", "scripts/signin.py"], cwd="/app", env=child_env)
            done_sign_second = today
        if hm >= PROBE_AT and done_probe != today:
            # 探针：只读健康检查（未到配置触发时间/频率时 signin.py 内零请求退出）
            subprocess.run(["python3", "scripts/signin.py", "--probe"], cwd="/app", env=child_env)
            done_probe = today
        if now.hour >= 3 and last_clean != today:
            _cleanup_logs()
            last_clean = today
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main_loop()
