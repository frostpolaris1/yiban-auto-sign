# SPDX-License-Identifier: AGPL-3.0-only
"""签到/探针子进程环境构造（run.sh、container_scheduler、web 共用口径）。

此前 web 手动签到子进程只继承 gunicorn 启动时的环境快照，
管理员事后在 .env 改的 YIBAN_PROXY / YIBAN_NOTIFY_URL / 登录方式等对手动签到
不生效（定时签到经 run.sh/scheduler 每次重读 .env，两条路径行为分叉）。
现统一为本模块：以进程环境为底座，.env 的 YIBAN_* 键覆盖注入。

`.env` 读不到时**响亮告警**（口径对齐 run.sh 的 MF-81④）：本模块存在的理由就是
"设置页写进 `.env` 的全局暂停/代理/窗口时间对子进程即时生效"，读不到 `.env` 恰好把
这个修复整个抹掉——若静默，结果是漏签或"该暂停没暂停"而无人知道。故两类读失败
（**路径不存在**与**存在但当前用户不可读**）各留一行 WARNING，点名路径并说明后果。
取值仍是"告警后继续"（返回继承环境，不抛、不中断签到）：理由与 run.sh 同——现网
可能以读不到 root-only `.env` 的身份跑 cron，直接退非 0 会天天红。
"""
import logging
import os

# 告警通道：与 `yiban.infra.env_io` 的 .env 解析告警同通道。调度器
# （docker/scheduler.py 的 `_setup_logging`）与 web 入口都把 root 挂上处理器并把
# "yiban" 放开到 INFO，故本模块的 WARNING 必然落到容器 stdout / web 按天日志面。
logger = logging.getLogger("yiban")

# 仅接受合法环境变量键名（与 run.sh 同口径）：防 .env 被手工写入含空格/特殊
# 字符的键后注入子进程环境
_KEY_RE = None


def _key_pattern():
    global _KEY_RE
    if _KEY_RE is None:
        import re
        _KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    return _KEY_RE


def parse_env_file(path):
    """逐行解析 .env：返回 {YIBAN_ 开头的合法键: 值}。

    与 run.sh 同口径：忽略空行/# 注释行，按首个 = 切分并 strip；
    非 YIBAN_ 前缀与非法键名一律丢弃（不向子进程注入无关变量）。
    `export KEY=v` 行同被丢弃（键以 "export" 起头，不是 YIBAN_ 前缀）——与
    `yiban.infra.env_io` / shell 入口的收敛口径一致："export 行不生效"
    （差异钉死见 env_io.parse_env_file 注释）。
    """
    # 告警频次口径（两类读失败同族，只准一条上界；本块同时管住下面两个 except 分支）：
    # 每次调用都喊，不设进程内闩、不去重。
    # 调用频率有界——容器常驻调度器每 PROBE_TRY_SECONDS 试一次探针、每
    # FALLBACK_TRY_SECONDS 查一次兜底（后者与引擎兜底常驻的扫描间隔
    # YIBAN_FALLBACK_INTERVAL 同量级），web 手动签到每请求一次。
    # 若改成"只喊一次"，退化状态可在其后数月的静默中持续。
    # 告警只多一行日志；漏报会漏签。
    # 两类事件的档位（哪类允许静默）见 run.sh 的「告警频次口径」段。
    pattern = _key_pattern()
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
                if not pattern.match(key):
                    continue
                out[key] = val.strip()
    except FileNotFoundError:
        # 调用点若本无 .env 属正常，但本模块的消费方（设置页写入的 .env）读不到即
        # 退化，故与"不可读"一样出声；两类分开报，与 run.sh MF-81④ 逐字同义。
        logger.warning(
            ".env 不存在，全部 YIBAN_* 配置回落进程环境快照，"
            "设置页改动不生效: %s", path)
    except OSError:
        # 与"不存在"分支分开报（消息不同）；频次与去重口径见函数开头的上界块。
        logger.warning(
            ".env 存在但当前用户不可读，全部 YIBAN_* 配置回落进程环境快照，"
            "设置页改动不生效: %s", path)
    return out


def build_child_env(env_file, base=None):
    """构造签到/探针子进程环境：进程环境为底座，.env 的 YIBAN_* 键覆盖注入。

    文件值优先于外部环境：这是 Web 设置页能生效的关键。`.env` 缺失/不可读时
    **不静默**：`parse_env_file` 按 run.sh MF-81④ 口径记一行 WARNING（两类读失败
    消息不同，点名路径与后果"全部 YIBAN_* 配置回落进程环境快照，设置页改动不生效"），
    本函数照常返回继承环境（不抛、不中断签到）。
    """
    env = dict(base) if base is not None else dict(os.environ)
    env.update(parse_env_file(env_file))
    return env
