# -*- coding: utf-8 -*-
"""**功能**
易班风控挑战页的**检测**（不求解）。

**归属**：本项目自研的"平台特征识别"层。挑战页特征取自**平台观察**（响应正文的挑战
脚本形态、`Set-Cookie` 下发的令牌名），不是任何第三方项目的代码；检测本体委托洁净室
协议库 `yiban.protocol.looks_like_challenge`（其弱信号 `captcha` 带 HTML 上下文门槛，
不误报正常页）。

**为什么不求解**：`https_ydclearance` 挑战的纯 Python 求解器在生产全历史零触发，已按
既定裁决删除——检测命中不再尝试求解，改由协议层**响亮失败**
（`CHALLENGE_DETECTED_MESSAGE`，落不可重试硬失败档并联动清会话缓存）。

**复用**：`looks_like_challenge` 被 `yiban/platform.py` 与 `yiban/security.py` 复用。

**通信**：输入：响应文本、`Set-Cookie`。输出：布尔检测结果。不执行任何远程 JS、不访问
网络。调用谁：`yiban.protocol`。谁调用：`yiban/platform.py`（登录握手遇挑战时）、
`yiban/security.py`（拦截判定）。
前端调用点：无直接调用点；WAF 拦截最终经 `yiban/security.py` 的文案进入签到日志与
`/api/my-logs`、`/api/admin/sign-events` 页面。
"""
from yiban.protocol import looks_like_challenge as _lib_looks_like_challenge

#: 挑战检测命中后的响亮失败文案（不求解；词元 "ydclearance" 令其落不可重试硬失败档并清会话）。
CHALLENGE_DETECTED_MESSAGE = (
    "ydclearance 挑战检测命中：本项目不求解 WAF 挑战（既定裁决），"
    "请配置 YIBAN_PROXY 代理后重试"
)


def looks_like_challenge(text, set_cookie=""):
    """判断响应是否触发易班风控挑战——只检测，不求解。

    判据 = 本项目观察到的特征（`Set-Cookie` 下发 `https_ydclearance`、挑战 JS 双特征）
    ∪ 协议库特征（`ydclearance`/`fengkongcloud` 令牌；`acw_sc`/`captcha` 弱信号须 HTML
    上下文）。

    误报（把正常页当挑战）不可接受，漏报可接受；所有已知正常夹具都必须返回 `False`。
    """
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    text = text or ""
    if "https_ydclearance" in (set_cookie or ""):
        return True
    if "window.onload=setTimeout" in text and 'eval("qo=eval;qo(po);")' in text:
        return True
    return bool(_lib_looks_like_challenge(text))
