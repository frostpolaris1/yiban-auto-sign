# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""设备登记按需补全的编排：冷却 → 外呼闸门 → 结果分类 → 审计。

**功能**
`refresh_account_device(m, acc, username, limits, fails, audit_action)`：对一个账号执行
一次"读服务端已登记设备并落库"。返回 `(http_status, payload)`，两条端点
（管理面 / 本人）共用本函数，HTTP 外壳留在各自视图里。

**归属**
`web/routes/accounts_api.py`（管理面 `POST /api/accounts/<int:idx>/device-refresh`）与
`web/routes/my_api.py`（本人 `POST /api/my-accounts/<int:idx>/device-refresh`）共用。
外呼实现是 `yiban.app.probe.refresh_registered_device`（经 `signin` 壳转发），本模块只做
编排，不重写登录或读设备。

**复用**
掩码走 `yiban.core.masking.mask_device_code`（设备识别码展示口径唯一一处）；失败冷却与
认证失败记账走账号验证**同一份**账（`_verify_fail_cooldown_remaining` /
`_record_verify_failure`）；外呼闸门走 `web.app.run_gated_call`（与账号验证同一个席位 +
每用户配额）。三条都经宿主模块 `m` 在**调用时刻**现取——它们都是会被测试打桩的模块级
名字，本模块另持绑定会让打桩静默失效。

**通信**
本模块不反向导入 `web.app`（测试以别名加载 `app.py`，普通 import 会再执行一份副本模块）：
宿主侧依赖一律作参数传入。审计动作名由调用方给（`account_device_refresh` /
`my_account_device_refresh`），本模块不猜调用方是谁。
"""
import time

import signin  # 探针/子进程模块（scripts/ 在 sys.path 上，由 web.app 的引导保证）

from web import security as _security
from web.services import verify_queue as _verify_queue
from web.services.accounts_data import _as_signin_account
from yiban.core.masking import mask_device_code as _mask_device_code

#: `probe.refresh_registered_device` 的 `reason` → HTTP 状态码（**单一判定表**）。
#: 200 的两条是"上游正常应答但没给完整设备对"（未登记 / 只登记了识别码）——那不是
#: 服务端错误，也不是客户端错误，如实回 `ok=false` 让前端引导用户去 App 内登记；
#: `persist_failed` 是**我们**的写库失败（500）；其余是上游读取失败（502）。
#: 表外的未知 reason 一律兜底 502（宁可当上游问题，也不静默回 200）。
_REASON_STATUS = {
    "account_inactive": 400,
    "not_registered": 200,
    "partial": 200,
    "persist_failed": 500,
    "login_failed": 502,
    "session_expired": 502,
    "read_failed": 502,
    "blocked": 502,
}
_REASON_STATUS_FALLBACK = 502


def _find_row(m, acc):
    """按库内 id 重读账号行（拿补全**之后**的落库值做展示）。读不到回原行。

    走 `m.db`（不是模块级 `db`）：宿主侧的 db 绑定是会被测试打桩的模块级名字，
    另持一份绑定会让打桩静默失效。
    """
    account_id = acc.get("id")
    for row in m.db.load_accounts():
        if row.get("id") == account_id:
            return row
    return acc


def refresh_account_device(m, acc, username, limits, fails, audit_action):
    """执行一次设备登记按需补全。返回 `(http_status, payload)`。

    顺序：认证失败冷却 → 外呼闸门（席位 + 配额）→ 真实登录读回 → 分类 → 审计。
    冷却先于闸门：被冷却拦下的请求**没有**发起真实登录，不该消耗配额（与账号添加同口径）。
    """
    phone = str(acc.get("phone", ""))
    actor = username or "?"
    # 冷却与账号验证共用同一份按手机号的认证失败账：同一账号反复登录（含读设备）会加速
    # 平台侧锁定，冷却账必须同源，否则一边拦、另一边放。
    if _security._verify_fail_cooldown_remaining(fails, phone, time.time()) > 0:
        return 429, {"ok": False, "reason": "cooldown",
                     "error": _verify_queue.VERIFY_FAIL_COOLDOWN_MSG}
    account = _as_signin_account(acc)
    try:
        result = _verify_queue.run_gated_call(
            lambda: signin.refresh_registered_device(account), username, limits)
    except _verify_queue.VerifyGateBusy:
        return 503, {"ok": False, "reason": "busy", "error": _verify_queue.VERIFY_BUSY_MSG}
    except _verify_queue.VerifyQuotaExceeded:
        return 429, {"ok": False, "reason": "quota",
                     "error": "账号验证尝试过于频繁，请稍后再试"}

    if result["ok"]:
        row = _find_row(m, acc)
        m.db.audit(actor, audit_action, m._mask_phone(phone), "设备登记已补全")
        return 200, {
            "ok": True,
            "msg": "已读取服务端登记设备并保存",
            "device": {
                "phone_model": result["phone_model"],
                # 掩码取自**落库后**的行：展示的就是账号行现在持有的值
                "phone_code_masked": _mask_device_code(row.get("phone_code", "")),
                "authorized": result["authorized"],
                "persisted": True,
            },
        }

    if result["reason"] == "login_failed":
        # 确定性认证失败（密码错误/锁定）与账号验证同一份记账：达阈值即冷却
        kind = m._record_verify_failure(fails, phone, result["message"], time.time())
        m.db.audit(actor, audit_action, m._mask_phone(phone),
                   f"设备登记补全未通过（{kind}）")
    else:
        # 环境类失败（网络/WAF/未登记/未授权）与"读到了但写库失败"分开留痕，
        # 便于运维区分"平台问题"与"我们写库问题"
        m.db.audit(actor, audit_action, m._mask_phone(phone),
                   f"设备登记补全未取到（{result['reason']}）")
    return (_REASON_STATUS.get(result["reason"], _REASON_STATUS_FALLBACK),
            {"ok": False, "reason": result["reason"], "error": result["message"]})
