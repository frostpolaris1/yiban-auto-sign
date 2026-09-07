# SPDX-License-Identifier: AGPL-3.0-only
"""单一 .env 解析实现（mailer / notify / account_crypto / db 共用）。

各历史副本的解析口径完全一致，差异仅在错误策略，由 strict 参数表达：
- 宽松（默认）：任何 OSError（含文件不存在）→ 返回空 dict，调用方走"未配置"分支；
- 严格（strict=True）：文件不存在 → 空 dict；文件存在但读取失败（权限/占用等）
  → 直接抛出 OSError，由调用方记日志并失败。

严格模式的理由（勿简化掉）：密钥/审计盐的自动生成路径若把"读失败"误判为"未配置"，
会静默生成新钥覆盖旧钥，致存量密文与审计链永久不可解——宁可启动失败也不生成替代
密钥（2026-08-27 对抗性审查结论）。

解析口径：utf-8-sig 兼容 BOM（Windows 记事本等工具保存常见，否则首个键名带
\\ufeff 前缀导致读不到）；忽略空行与 # 注释行；按首个 = 切分，键值两侧 strip；
无 = 的行跳过。

child_env.parse_env_file 不在此收敛：它有额外语义（仅接受 YIBAN_ 前缀且键名
合法的行，供子进程环境注入，防 .env 被写入特殊键后污染子进程），保持独立实现。
"""
import os


def parse_env_file(path, *, strict=False):
    """解析 .env 全部键值，返回 dict。strict 语义见模块 docstring。"""
    result = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    result[key.strip()] = value.strip()
    except FileNotFoundError:
        pass  # 文件确实不存在 → 空配置（调用方据此走"未配置"分支）
    except OSError:
        if strict:
            raise
        return {}
    return result


def env_path(default=".env"):
    """解析 .env 路径的统一口径：环境变量 YIBAN_ENV_FILE 优先（去空白），回退默认值。

    与 web/signin 子进程约定一致（web/app.py 写入 YIBAN_ENV_FILE 传给子进程）。
    """
    return os.environ.get("YIBAN_ENV_FILE", "").strip() or default
