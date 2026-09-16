# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""配置检查：密钥文件定位、整数环境变量、账号配置脱敏打印、`--only` 过滤。

全是"读配置 / 把配置讲清楚"的纯本地逻辑（零网络请求），供入口与账号装载复用。
脱敏口径与 `yiban/masking.py` 一致：手机号必须打码——本模块的输出会落在 CI 日志、
终端记录与他人可读的会话里（运维常贴到群里排查）。
"""
import logging
import os

from yiban.masking import mask_phone as _mask_phone

logger = logging.getLogger("yiban")


def _key_env_file():
    """密钥来源 .env 路径：YIBAN_ENV_FILE 优先（与 web 子进程约定一致），回退默认 .env。

    2026-08-21 对抗性审查修复：此前 web 为保证自定义 .env 路径下子进程能解密，
    把 YIBAN_ACCOUNTS_KEY 明文注入子进程环境变量（同 uid 进程可读 /proc/<pid>/environ，
    密钥暴露面扩大）。现统一改为传递【路径】而非密钥本身，本函数即子进程侧的解析入口。
    """
    return os.environ.get("YIBAN_ENV_FILE", "").strip() or None


def parse_env_int(name, default):
    """读取非负整数环境变量：缺失/非法回退默认值，负值归零。"""
    try:
        return max(0, int(os.environ.get(name, "").strip()))
    except (TypeError, ValueError):
        return default


def print_config_summary(accounts):
    """打印账号配置摘要（手机号与密码脱敏），不发任何网络请求。

    手机号必须打码：本摘要会落在 CI 日志、终端记录与他人可读的会话里（运维常贴到
    群里排查），完整号码属个人信息。脱敏后仍可区分账号（138****8000）。设备识别码
    只报"已配置"、不打印任何前缀（防摘要泄露设备指纹），型号按原值展示便于排查。
    """
    print("==== 账号配置检查 ====")
    for i, acc in enumerate(accounts, 1):
        if acc.has_device_info:
            device = f"设备: {acc.phone_model} / 识别码已配置"
        else:
            device = "设备: 未配置（如学校开启设备绑定，签到将失败）"
        print(f"  {i}. {_mask_phone(acc.phone)} | 密码: {'*' * 8} | {device}")
    print(f"共 {len(accounts)} 个账号，配置检查通过。")


def _apply_only_filter(accounts, only_arg):
    """--only 过滤：只保留指定手机号，返回 (保留账号, 未命中号码列表)。

    对每个未命中号码落 warning 日志——此前仅"全部不命中"才报错，
    `--only "存在号,手滑号"` 时未命中号码被静默丢弃，用户误以为全部已处理。
    调用方负责"过滤后为空则报错退出"。
    """
    only_set = {p.strip() for p in only_arg.split(",") if p.strip()}
    filtered = [a for a in accounts if a.phone in only_set]
    missing = sorted(only_set - {a.phone for a in filtered})
    for phone in missing:
        # 裸号不带 [] 定界符，web 侧 _mask_log_phones（只认 [11 位号]）盖不住，
        # 落盘即脱敏——web 展示层/导出不得出现完整号（「本地日志保留完整号」
        # 的设计约定仅覆盖 [号] 形态行，见 _mask_phone 注释）
        logger.warning("--only 指定账号不在配置中: %s", _mask_phone(phone))
    return filtered, missing
