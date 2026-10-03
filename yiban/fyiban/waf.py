# -*- coding: utf-8 -*-
"""**功能**
易班风控挑战页的**检测**（不求解）。

**裁决（B' 换核）**：`solve_ydclearance`（纯 Python 求解 `https_ydclearance` 挑战的
三段字节变换）为生产全历史零触发的死代码，已删除——检测命中不再尝试求解，改由协议层
响亮失败（`CHALLENGE_DETECTED_MESSAGE`，落不可重试硬失败档）。检测本体改为委托
vendored 洁净室库 `yiban._vendor.yiban_protocol.looks_like_challenge`，并在适配层
补齐本项目既有特征（`Set-Cookie` 携带 `https_ydclearance`、挑战 JS 双特征），
即"现实现特征集 ∪ 库特征集"——只加不减，不得弱化检测。

**归属**
`yiban/fyiban/` 第三方隔离层的 WAF 子模块。"是不是挑战页"属平台特征识别（本层）；
"这个挑战能不能信"（跳转白名单）属本项目策略，原先以白名单参数注入求解器，
求解器删除后该注入面随之消失。

**复用**
`looks_like_challenge` 被 `yiban/fyiban/protocol.py` 与 `yiban/security.py` 复用。

**通信**
输入：响应文本、`Set-Cookie`。输出：布尔检测结果。不执行任何远程 JS、不访问网络。
调用谁：vendored `yiban_protocol`。
谁调用：`yiban/fyiban/protocol.py`（登录握手遇挑战时）、`yiban/security.py`（拦截判定）。
前端调用点：无直接调用点；WAF 拦截最终经 `yiban/security.py` 的文案进入签到日志与
`/api/my-logs`、`/api/admin/sign-events` 页面。
"""
from yiban._vendor.yiban_protocol import looks_like_challenge as _lib_looks_like_challenge

#: 挑战检测命中后的响亮失败文案（不求解；词元 "ydclearance" 令其落不可重试硬失败档并清会话）。
CHALLENGE_DETECTED_MESSAGE = (
    "ydclearance 挑战检测命中：本项目不求解 WAF 挑战（既定裁决），"
    "请配置 YIBAN_PROXY 代理后重试"
)


def looks_like_challenge(text, set_cookie=""):
    """判断响应是否触发易班风控挑战——只检测，不求解。

    判据为"现实现特征集 ∪ 库特征集"的并集（只加不减）：
    - 本项目既有特征：`Set-Cookie` 已下发 `https_ydclearance`；或页面含挑战 JS 双特征
      （`window.onload=setTimeout` + `eval("qo=eval;qo(po);")`）；
    - 库特征：`ydclearance` / `fengkongcloud` / `captcha` 标记，或 `acw_sc` + HTML 标记。

    误报（把正常页当挑战）不可接受，漏报可接受；所有已知正常夹具都必须返回 `False`。
    """
    if "https_ydclearance" in (set_cookie or ""):
        return True
    if "window.onload=setTimeout" in text and 'eval("qo=eval;qo(po);")' in text:
        return True
    return bool(_lib_looks_like_challenge(text))
