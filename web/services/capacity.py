# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""容量核计与触顶告警族：账号/用户配额判定、容量预估与每进程一次的通知。

**功能**
计入容量的账号数 `_capacity_account_count` 与不占容量的未过审账号数
`_capacity_audit_count`；按当前窗口与间隔预估可容纳账号数 `_capacity_estimate`；
账号配额判定 `_accounts_at_capacity`、注册暂停开关 `_registration_paused`、用户配额
判定 `_users_at_capacity`；同类型告警邮件节流 `_mail_alert_due` 与容量触顶通知
`_notify_capacity_once`（每进程每种资源一次）。

**归属**
原 `web/app.py` 的模块级容量与触顶告警辅助，唯一真源在本模块；`web/app.py` 只保留
名字面与转发，把它自己持有、而本模块需要的模块级名字——`.env` 路径 `ENV_FILE`、整数
配置读取器 `load_env_int`、两个缺省上限 `DEFAULT_MAX_ACCOUNTS` / `DEFAULT_MAX_USERS`、
窗口解析器 `_sign_window`、掐头去尾口径 `edge_config`、告警出口
`send_notification`——在调用时刻现取后注入。

**复用**
`_capacity_account_count` 与 `_capacity_audit_count` 互斥互补（两者之和 = 全部非删除
账号），判定口径唯一来源是 `yiban.store.accounts.signs_in`；`_capacity_estimate` 的公式
与引擎共用 `yiban.engine.schedule.capacity_accounts`，有效窗口取 `yiban.window.bounds`
（含"裁剪吃空 → 回退默认窗口"），不另写一套容量模型。`_accounts_at_capacity` 复用
`_capacity_account_count`，`_users_at_capacity` 与其同构（"超过上限才拒绝"语义）。

**通信**
本模块不反向导入 `web.app`（本仓测试以别名加载 `app.py`，普通 import 会再执行一份副本
模块）。`.env` 路径、读取器、缺省上限、窗口/裁剪口径与告警出口都作为显式参数接收：
它们在 `web.app` 上会被测试打桩或直接赋值改写（`_sign_window` / `edge_config` /
`load_env_int` / `ENV_FILE` / `send_notification` 都是既有打桩名），本模块另持一份绑定
会让这些改写静默失效。数据访问层 `db` 与引擎公式直接取真源（与 `web.app.db` 是同一
模块对象），不另立第二套读写。
"""

import logging
import threading
import time

from web.services.accounts_data import load_accounts_raw
from yiban import window as yb_window
from yiban.engine.schedule import capacity_accounts
from yiban.mail import layout as mail_layout
from yiban.store import db

# 与 web.app 同名的日志通道：本族的告警落回既有通道，便于运维沿用同一处过滤
logger = logging.getLogger("web")


def _capacity_account_count():
    """计入账号容量的账号数（= 会发起易班请求的账号，含 owner='admin' 裸账号）。

    口径（判据唯一来源 `yiban.store.accounts.signs_in`）：**非删除且审核态已通过**。
    审核态未通过（pending/rejected）的行永不签到（引擎加载与运行期复核都按同一条件
    过滤），把它们计入会让"永不签到的存量"长期占满名额——新账号在提交时被
    「账号数量已达上限」误拒，而总览三分类还把这类行显示成"正常"。
    user_paused 仍计入（用户主动暂停、一键可恢复；三分类已单独列出）。
    显示/配额/预估三处同一源；性能：只用明文列，走 load_accounts_raw
    免解密（原 load_accounts 对每行做 AES-GCM 解密并长持 _conn_lock）。
    """
    return sum(1 for a in load_accounts_raw() if db.account_signs_in(a))


def _capacity_audit_count():
    """未通过审核而不占容量的账号数（仅展示：总览/设置页的容量说明）。

    与 `_capacity_account_count` 互斥互补：两者之和 = 全部非删除账号。
    """
    return sum(1 for a in load_accounts_raw()
               if not a["deleted"] and not db.account_signs_in(a))


def _capacity_estimate(gap=0, *, sign_window, edge_config):
    """按当前签到窗口与账号间隔设置预估可容纳账号数（**配置属性**口径）。

    公式与引擎共用 `signin.capacity_accounts`，有效窗口取
    `yiban.window.from_env(...).full_sec()`（含"裁剪吃空 → 回退默认窗口"，故不会再
    出现"配置异常时容量显示 0"）。avg 取 YIBAN_AVG_ATTEMPT_SEC（缺省 3s）。

    **刻意用完整有效窗口、不扣已流逝时间**：本函数服务设置页展示与**保存闸门**
    （"按新设置预估容量 < 当前账号数则拒绝保存"），问的是"这套配置能容纳几个"。
    若按时段扣减，管理员在窗口末尾将永远无法保存设置。引擎侧预检问的是"今天还能
    签几个"，那里才用 `remaining_sec()`。

    窗口解析器与掐头去尾口径由调用方传入（`web.app` 的 `_sign_window` / `edge_config`）：
    两者在 `web.app` 上都是既有的打桩点，本模块另持绑定会让桩静默失效。
    """
    win = yb_window.bounds({
        "sign_start": sign_window()[0], "sign_end": sign_window()[1],
        "edge_front_sec": edge_config()[0], "edge_back_sec": edge_config()[1],
    })
    return capacity_accounts(win.full_sec(), gap)


def _accounts_at_capacity(extra_accounts=0, *, env_file, load_env_int, max_accounts_default):
    """账号配额判定：占用 = **会签到的账号数** + 本次将新增账号数，
    > 上限则 True（0 = 不限）。调小上限不删除存量账号，只限制新增。

    口径见 `_capacity_account_count`：未通过审核（pending/rejected）的行不计入
    ——它们永不发起易班请求，计入会把名额被"永不签到的存量"占满，新账号被误拒。
    审核通过是"让这一行开始产生负载"的动作，故 `api_account_review` 的 approve
    分支同样过这道门（否则名额只在提交时把关、审批时无门可越界）。
    extra_accounts：本次提交将新增（或转为参与签到）的账号数，添加/通过恰为 1 个。

    `.env` 路径、整数读取器与缺省上限由调用方传入（`web.app` 的 `ENV_FILE` /
    `load_env_int` / `DEFAULT_MAX_ACCOUNTS`）：三者都会被测试改写或打桩。
    """
    max_accounts = load_env_int(env_file, "YIBAN_MAX_ACCOUNTS", max_accounts_default)
    if max_accounts <= 0:
        return False
    return _capacity_account_count() + extra_accounts > max_accounts


def _registration_paused(env_file, load_env_int):
    """注册是否处于暂停状态。

    YIBAN_REGISTRATION_PAUSE=1 视为暂停；未配置/空 = 允许——既有部署升级后
    无此键，注册行为不变（用户裁决：默认允许，新部署才默认暂停）。新部署由
    ensure_secret_key 首次创建 .env 时写入 1，管理员完成初始配置后在设置页
    （危险区，仅主管理员）开启。与 YIBAN_GLOBAL_PAUSE 同款读写口径。

    提升为模块级：注册接口（web/routes/auth.py）与公开状态接口
    （api_registration_paused）共用，且函数体只读 .env、无工厂状态。
    `.env` 路径与读取器由调用方传入（两者都会被测试改写或打桩）。
    """
    return load_env_int(env_file, "YIBAN_REGISTRATION_PAUSE", 0) == 1


def _users_at_capacity(*, env_file, load_env_int, max_users_default):
    """用户配额判定（与 `_accounts_at_capacity` 同构的"超过上限才拒绝"语义，
    容量阈值语义统一）：再注册 1 人后全部未删除注册用户数
    > 上限 则 True（注册每次恰好新增 1 用户；0 = 不限）。
    users 口径 = 全部未删除注册用户（含空用户）。

    `.env` 路径、整数读取器与缺省上限由调用方传入（`web.app` 的 `ENV_FILE` /
    `load_env_int` / `DEFAULT_MAX_USERS`）：三者都会被测试改写或打桩。
    """
    max_users = load_env_int(env_file, "YIBAN_MAX_USERS", max_users_default)
    if max_users <= 0:
        return False
    return len(db.load_users()) + 1 > max_users


# 高危告警邮件节流（被盗号滥用面加固）：同类型告警邮件在窗口内只发一封，
# 防被盗管理员会话通过反复触发高危操作（批量删除等）耗尽 SMTP 发件额度；webhook
# 保持实时逐条推送，不受影响。窗口可调：YIBAN_MAIL_ALERT_COOLDOWN（秒，0=关闭，默认 300）。
DEFAULT_MAIL_ALERT_COOLDOWN = 300
_mail_alert_ts = {}
_mail_alert_lock = threading.Lock()


def _mail_alert_due(title, env_file, load_env_int):
    """同类型告警邮件节流判断：窗口内已发过返回 False（本次跳过邮件，仅走 webhook）。

    `.env` 路径与整数读取器由调用方传入（`web.app` 的 `ENV_FILE` / `load_env_int`）：
    两者都会被测试改写或打桩。
    """
    window = load_env_int(env_file, "YIBAN_MAIL_ALERT_COOLDOWN", DEFAULT_MAIL_ALERT_COOLDOWN)
    if window <= 0:
        return True  # 0 = 关闭节流
    now = time.time()
    with _mail_alert_lock:
        last = _mail_alert_ts.get(title, 0.0)
        if now - last < window:
            return False
        _mail_alert_ts[title] = now
        return True


# 容量告警去重（进程内）：首次触顶通知一次，之后静默拒绝（防通知风暴；重启后重置）
_capacity_alerts = {"users": False, "accounts": False}


def _notify_capacity_once(kind, limit, label, *, send_notification):
    """容量触顶通知（每进程每种资源只发一次）：管理员知情且不刷屏。

    告警出口由调用方传入（`web.app` 的 `send_notification`）：它是既有的打桩点，
    本模块另持绑定会让那些桩静默失效。
    """
    if _capacity_alerts.get(kind):
        return
    _capacity_alerts[kind] = True
    logger.warning("%s已达上限 %d，已拒绝新注册/添加", label, limit)
    send_notification(
        f"{label}已达上限",
        mail_layout.Mail(
            summary=f"{label}已达上限，新的注册/添加已被拒绝。",
            fields=[("当前上限", limit)],
            advice=["如需扩容请在 .env 调整 YIBAN_MAX_USERS / YIBAN_MAX_ACCOUNTS"],
            level="urgent",
        ),
        urgent=True,
    )
