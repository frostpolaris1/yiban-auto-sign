# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""口令策略与哈希算法的单一定义点。

**为什么独立成模块**：普通用户口令、内置主管理员口令、CLI 首次初始化三条路径必须
使用同一套字符类别与下限。原先主管理员策略在 `web/services/accounts_data.py`，
而 CLI 不能 import Web 层；复制一份字面量会让"网页创建"与"CLI init"漂移。

本模块只放纯常量与纯函数，不 import Web、不读写文件。
"""

#: 普通用户口令下限。只对新建/修改生效，存量口令不受影响。
PASSWORD_MIN_LEN = 10

#: 口令"字符类别"单一事实源。四个正则按文案顺序排列，与 PASSWORD_CLASS_LABELS
#: 同序同数。判定语义是"命中类别数 >= 下限即通过"：符号算一类，不额外要求含符号。
PASSWORD_CLASS_PATTERNS = (r"[A-Z]", r"[a-z]", r"\d", r"[^A-Za-z0-9]")
PASSWORD_CLASS_LABELS = ("大写字母", "小写字母", "数字", "符号")
PASSWORD_MIN_CLASSES = 2

#: 统一口径文案：数量下限一律写成"…中的至少 + 常量值 + 类别"。
PASSWORD_CLASS_HINT = "大小写字母、数字、符号中的至少两类"
PASSWORD_POLICY_HINT = f"至少 {PASSWORD_MIN_LEN} 位，且包含{PASSWORD_CLASS_HINT}"

#: 内置主管理员口令单独提档：至少 12 位且命中至少三类。
ADMIN_PASSWORD_MIN_LEN = 12
ADMIN_PASSWORD_MIN_CLASSES = 3

#: 口令哈希算法（werkzeug scrypt，OWASP 推荐参数）。
SCRYPT_METHOD = "scrypt:65536:8:1"


def password_class_count(password):
    """口令命中的字符类别数（0-4）。"""
    import re

    return sum(bool(re.search(pat, password)) for pat in PASSWORD_CLASS_PATTERNS)


def admin_password_policy_error(password):
    """主管理员口令策略：通过返回 None，否则返回中文文案。"""
    if len(password) < ADMIN_PASSWORD_MIN_LEN:
        return (
            f"至少 {ADMIN_PASSWORD_MIN_LEN} 位，且包含"
            "大写字母、小写字母、数字、符号中的至少三类"
        )
    if password_class_count(password) < ADMIN_PASSWORD_MIN_CLASSES:
        return f"需包含{'、'.join(PASSWORD_CLASS_LABELS)}中的至少三类"
    return None
