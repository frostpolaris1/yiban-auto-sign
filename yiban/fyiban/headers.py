# -*- coding: utf-8 -*-
"""易班客户端请求头与版本特征：**衍生自 `onefeifan/fyiban`（AGPL-3.0）**。

来源见同目录 `PROVENANCE.md`：上游 `Core/SchoolBased.kt::headers()`（Origin/User-Agent/
AppVersion 三项）与 `Core/BaseReq.kt` 的 cookie/重定向处理给出了这套特征的骨架；
UA 全文与本项目的默认登录方式（KILLYIBAN）为本地补充。

**两处头必须同值**：`KILLYIBAN_HEADERS.AppVersion` 与 usersure 提交里的版本字段
（服务端一致性校验），改一处必须改另一处（`tests/test_fyiban_isolation.py` 钉住）。
"""
# 易班 App 版本特征：两处请求头（KILLYIBAN_HEADERS / usersure 提交）必须同值，
# 不一致可能触发服务端一致性校验；旧流程 iOS UA 尾段同步引用。
# 2026-09-15 由 5.2.2 升至 5.2.3（依据：易班官方 5.2.3 安装包的清单版本号实测）
YIBAN_APP_VERSION = "5.2.3"

# 易班 iOS 客户端 UA（与 Auto-Test 保持一致）
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/4.0 "
    "Chrome/104.0.5112.97 Mobile Safari/537.36 yiban_iOS/" + YIBAN_APP_VERSION,
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "com.yiban.app",
    "Origin": "https://app.uyiban.com",
    "Referer": "https://app.uyiban.com/",
    "Connection": "close",
}

# KillYiBan 同款请求头（默认登录方式；与同作者的 FYIBAN 同源，KillYiBan 脱胎于 FYIBAN）
# 注意：usersure 提交时会被显式覆盖为不带 Origin/Referer（见 login_killyiban 第 3 步，
# 实测带 Origin → e001 无效应用端编号），其余请求用此头
KILLYIBAN_HEADERS = {
    "User-Agent": "Yiban",
    "AppVersion": YIBAN_APP_VERSION,
    "Origin": "https://c.uyiban.com",
    "Referer": "https://c.uyiban.com/",
    "Connection": "close",
}
