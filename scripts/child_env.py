# SPDX-License-Identifier: AGPL-3.0-only
"""签到/探针子进程环境构造（run.sh、container_scheduler、web 共用口径）。

批次7 P2-10：此前 web 手动签到子进程只继承 gunicorn 启动时的环境快照，
管理员事后在 .env 改的 YIBAN_PROXY / YIBAN_NOTIFY_URL / 登录方式等对手动签到
不生效（定时签到经 run.sh/scheduler 每次重读 .env，两条路径行为分叉）。
现统一为本模块：以进程环境为底座，.env 的 YIBAN_* 键覆盖注入。
"""
import os

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
    """
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
    except OSError:
        pass
    return out


def build_child_env(env_file, base=None):
    """构造签到/探针子进程环境：进程环境为底座，.env 的 YIBAN_* 键覆盖注入。

    文件值优先于外部环境：这是 Web 设置页能生效的关键。.env 缺失/损坏时
    安静退化为纯继承。
    """
    env = dict(base) if base is not None else dict(os.environ)
    env.update(parse_env_file(env_file))
    return env
