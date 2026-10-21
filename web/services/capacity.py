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
原 `web/app.py` 的模块级容量与触顶告警辅助，唯一真源在本模块。批 3-d 删除
`web/app.py` 的转发包装：路由与测试直取本模块。`.env` 路径 `ENV_FILE`、整数配置
读取器 `load_env_int`、宽松读取器 `read_env`、窗口解析器、掐头去尾口径与告警出口
`send_notification` 经 `configure(...)` 登记进 `_hooks`，按调用时刻解析。两个缺省上限
直接取自 `web/services/limits.py`。

**复用**
`_capacity_account_count` 与 `_capacity_audit_count` 互斥互补（两者之和 = 全部非删除
账号），判定口径唯一来源是 `yiban.adapters.persistence.accounts.signs_in`；`_capacity_estimate` 的公式
与引擎共用 `yiban.engine.schedule.capacity_of`（**显式固定 v2 公式**，即
`capacity_accounts` 那一支：双轨开关已随单池消失，展示/闸门口径保持批 4 的取值不变），
有效窗口取 `yiban.core.window.bounds`（含"裁剪吃空 → 回退默认窗口"），
不另写一套容量模型。`_accounts_at_capacity` 复用 `_capacity_account_count`，
`_users_at_capacity` 与其同构（"超过上限才拒绝"语义）。触发告警的发信不在这里直接
发出，而是经 `web/services/locks.py` 的 `run_after_file_lock` 登记——那是全局锁
"锁内不得有网络 I/O"这条纪律的唯一汇合点，本模块不另造一套延后机制。

**通信**
本模块不反向导入 `web.app`（本仓测试以别名加载 `app.py`，普通 import 会再执行一份副本
模块）。`.env` 路径、读取器、窗口/裁剪口径与告警出口经 `configure(...)` 登记，按调用
时刻解析：测试打桩 `_sign_window` / `edge_config` / `load_env_int` / `read_env` /
`send_notification` 与赋值 `ENV_FILE` 仍然生效。未登记即 `RuntimeError`（fail-closed）。
数据访问层 `db` 与引擎公式直接取真源（与 `web.app.db` 是同一模块对象），不另立第二套读写。
"""

import logging
import os
import threading
import time

from web.services.accounts_data import load_accounts_raw
from web.services.limits import DEFAULT_MAX_ACCOUNTS, DEFAULT_MAX_USERS
from web.services.locks import run_after_file_lock
from yiban.adapters.config.env_io import parse_env_flag
from yiban.adapters.notifier.mail import layout as mail_layout
from yiban.adapters.persistence import db
from yiban.core import window as yb_window
from yiban.engine.schedule import capacity_of

# 与 web.app 同名的日志通道：本族的告警落回既有通道，便于运维沿用同一处过滤
logger = logging.getLogger("web")

# ---------------------------------------------------------------------------
# 宿主注入登记点（批 3-d）
# ---------------------------------------------------------------------------
# 本模块原由 `web/app.py` 的转发包装在调用时刻现取宿主名字后注入。批 3-d 删除那层
# 包装，改由宿主在 import 末尾登记 lambda；本模块按调用时刻从 `_hooks` 取。
# 未登记即 fail-closed（抛 `RuntimeError`）。
_hooks = {}


def configure(env_file=None, load_env_int=None, read_env=None, sign_window=None,
              edge_config=None, send_notification=None):
    """注入宿主侧依赖（登记 lambda，见上方说明）。可重复调用（测试用）。

    只覆盖显式传入的键：未传的保持上一次登记。
    """
    if env_file is not None:
        _hooks["env_file"] = env_file
    if load_env_int is not None:
        _hooks["load_env_int"] = load_env_int
    if read_env is not None:
        _hooks["read_env"] = read_env
    if sign_window is not None:
        _hooks["sign_window"] = sign_window
    if edge_config is not None:
        _hooks["edge_config"] = edge_config
    if send_notification is not None:
        _hooks["send_notification"] = send_notification


def _host(key):
    """取宿主登记的依赖。未登记即 fail-closed。

    **`try` 只包住取键，不包住调用**：宿主 lambda 内部抛的 `KeyError` 是真实业务
    错误，必须原样传出。
    """
    try:
        fn = _hooks[key]
    except KeyError:
        raise RuntimeError(
            "web.services.capacity 尚未登记宿主依赖 %r：请确认 web/app.py 的 import "
            "末尾调用了 web.services.capacity.configure(...)" % (key,)
        ) from None
    return fn()


def _capacity_account_count():
    """计入账号容量的账号数（= 会发起易班请求的账号，含 owner='admin' 裸账号）。

    口径（判据唯一来源 `yiban.adapters.persistence.accounts.signs_in`）：**非删除且审核态已通过**；
    user_paused 仍计入（用户主动暂停、一键可恢复，总览三分类已单独列出）。
    「同源」成立在**判据**上，不成立在**计数表达式**上：设置页视图（
    `web/routes/settings_api.py` 的 `_cur_accounts = sum(... account_signs_in(a))`）另写了
    一份计数，为的是单请求只读一次明文列、不复本函数的解密开销；「预估」那路走的是引擎
    公式 `yiban.engine.schedule.capacity_of`，也不经本函数。故改本函数只改到调用它的
    显示与配额两处；要改**口径**得改 `account_signs_in`，那两处才会一起跟上。
    """
    # 审核未通过（pending/rejected）的行永不签到（引擎加载与运行期复核按同一条件过滤），
    # 计入会让"永不签到的存量"长期占满名额：新账号提交时被「账号数量已达上限」误拒，
    # 而总览三分类还把这类行显示成"正常"
    # 走 load_accounts_raw 而非 load_accounts：只用明文列，免逐行 AES-GCM 解密
    return sum(1 for a in load_accounts_raw() if db.account_signs_in(a))


def _capacity_audit_count():
    """未通过审核而不占容量的账号数（仅展示：总览/设置页的容量说明）。

    与 `_capacity_account_count` 互斥互补：两者之和 = 全部非删除账号。
    """
    return sum(1 for a in load_accounts_raw()
               if not a["deleted"] and not db.account_signs_in(a))


def _capacity_estimate(gap=0):
    """按当前签到窗口与账号间隔设置预估可容纳账号数（**配置属性**口径）。

    公式与引擎共用 `yiban.engine.schedule.capacity_of`；本展示/闸门口径**显式固定 v2
    公式**（`enabled=False`，逐值不变）：`capacity_of` 的缺省已随台账单池化变为 v3，
    但容量口径（批 4）要求展示与"预估 < 当前账号数就拒绝保存"的闸门逐值不动。
    有效窗口取
    `yiban.core.window.from_env(...).full_sec()`（含"裁剪吃空 → 回退默认窗口"，故不会再
    出现"配置异常时容量显示 0"）。avg 取 YIBAN_AVG_ATTEMPT_SEC（缺省 3s）。

    avg 的取值配置层是**生效配置层**（进程环境为底、`.env` 覆盖——与 run.sh 起引擎
    子进程前的 export 同一优先级）。只读进程环境会让 avg 与 gap 跨两层，按偏小的默认
    avg 高估容量（MF-93）。展示面与判定面共用本函数 ⇒ "同一份配置两侧必然同值"由结构保证。

    宿主注入口径见模块头「通信」（`_sign_window` / `edge_config` 都是既有打桩点）。
    """
    sign_window = _host("sign_window")
    edge_config = _host("edge_config")
    env = {**os.environ, **_host("read_env")(_host("env_file"))}
    win = yb_window.bounds({
        "sign_start": sign_window()[0], "sign_end": sign_window()[1],
        "edge_front_sec": edge_config()[0], "edge_back_sec": edge_config()[1],
    })
    # full_sec() 而不是 remaining_sec()：本函数服务设置页展示与"预估 < 当前账号数就
    # 拒绝保存"的闸门，问的是"这套配置能容纳几个"。按时段扣减的话，管理员在窗口末尾
    # 永远存不下设置。引擎侧预检问"今天还能签几个"，那里才用 remaining_sec()
    return capacity_of(win.full_sec(), gap=gap, enabled=False, env=env)


def _accounts_at_capacity(extra_accounts=0):
    """账号配额判定：占用 = **会签到的账号数** + 本次将新增账号数，
    > 上限则 True（0 = 不限）。调小上限不删除存量账号，只限制新增。

    计入口径见 `_capacity_account_count`（唯一判据，此处不重述）。
    extra_accounts：本次将新增（或转为参与签到）的账号数，添加/通过恰为 1 个。
    宿主注入口径见模块头「通信」（`ENV_FILE` / `load_env_int` / `DEFAULT_MAX_ACCOUNTS`）。
    """
    max_accounts = _host("load_env_int")(
        _host("env_file"), "YIBAN_MAX_ACCOUNTS", DEFAULT_MAX_ACCOUNTS)
    if max_accounts <= 0:
        return False  # 0 与负数都是"不限额"，不是"一个都不许加"
    # 严格 >：加完正好等于上限仍放行（额度是"不超过"）。审核通过（api_account_review 的
    # approve 分支）也过这道门——它就是"让这一行开始产生负载"的动作，只在提交时把关的话，
    # 审批环节成了越界口
    return _capacity_account_count() + extra_accounts > max_accounts


def _registration_paused():
    """注册是否处于暂停状态。

    默认允许注册（升级后 .env 无此键时行为不变）；新部署由 ensure_secret_key 首次创建
    .env 时写入 1，管理员完成初始配置后在设置页危险区（仅主管理员）开启。与
    YIBAN_GLOBAL_PAUSE 同款读写口径。注册接口（web/routes/auth.py）与
    api_registration_paused 共用这一份实现。参数注入口径见模块头「通信」。

    判定与引擎的开关真值**同一口径**（`yiban.adapters.config.env_io.parse_env_flag`：1/true/on/yes=开）。
    原实现 `load_env_int(...) == 1` 只认整数，`=true` 会让引擎真停注册、此门（及登录页
    探测端点）却判"开放"——与面板显示的同一处假安心（census P0-1）。
    """
    raw = _host("read_env")(_host("env_file")).get("YIBAN_REGISTRATION_PAUSE", "")
    return parse_env_flag(raw, default=False,
                          key="YIBAN_REGISTRATION_PAUSE", log=logger)


def _users_at_capacity():
    """用户配额判定（与 `_accounts_at_capacity` 同构的"超过上限才拒绝"语义，
    容量阈值语义统一）：再注册 1 人后全部未删除注册用户数
    > 上限 则 True（注册每次恰好新增 1 用户；0 = 不限）。
    users 口径 = 全部未删除注册用户（含空用户）。

    宿主注入口径见模块头「通信」（`ENV_FILE` / `load_env_int` / `DEFAULT_MAX_USERS`）。
    """
    max_users = _host("load_env_int")(_host("env_file"), "YIBAN_MAX_USERS", DEFAULT_MAX_USERS)
    if max_users <= 0:
        return False  # 0 与负数都是"不限额"
    return len(db.load_users()) + 1 > max_users  # +1 = 把本次要注册的这人算进去再比


# 高危告警邮件节流（被盗号滥用面加固）：同类型告警邮件在窗口内只发一封，
# 防被盗管理员会话通过反复触发高危操作（批量删除等）耗尽 SMTP 发件额度；webhook
# 保持实时逐条推送，不受影响。窗口可调：YIBAN_MAIL_ALERT_COOLDOWN（秒，0=关闭，默认 300）。
DEFAULT_MAIL_ALERT_COOLDOWN = 300
_mail_alert_ts = {}
_mail_alert_lock = threading.Lock()


def _mail_alert_due(title, env_file, load_env_int, level=None):
    """同类型告警邮件节流判断：窗口内已发过返回 False（本次跳过邮件，仅走 webhook）。

    节流键是 **`(title, level)` 二元组**而非标题原文：同一标题下不同级别
    （如注册用户非紧急改密 vs 内置主管理员 urgent 改密）的告警各自独立计时。此前只按
    标题去重，最低档注册用户即可反复触发非紧急同类告警，把管理员的紧急同类告警一并
    静音（跨权限告警压制）——`level` 这一维就是那条修复的落点。

    曾有过第三维 `target`（想按目标再拆一次），但 `send_notification` 的签名里压根没有
    目标参数、全仓唯一实参调用点也只传 `(title, urgent)`，该维恒为 None，属**声明了
    却从未生效**的死维度：键里带着它只会让人误以为"同标题不同目标已分开计时"。故删除
    ——真要按目标拆，第一步是让 `send_notification` 真能拿到目标，那样加维才有意义。
    `level` 缺省 None，单参调用退化为 `(title, None)`，与既有调用点/打桩点兼容。

    参数注入口径见模块头「通信」（`ENV_FILE` / `load_env_int`）。
    """
    window = load_env_int(env_file, "YIBAN_MAIL_ALERT_COOLDOWN", DEFAULT_MAIL_ALERT_COOLDOWN)
    if window <= 0:
        return True  # 0 = 关闭节流
    key = (title, level)
    now = time.time()
    with _mail_alert_lock:
        last = _mail_alert_ts.get(key, 0.0)
        if now - last < window:
            return False  # 窗口内已发过：本次只走 webhook，邮件额度留着
        _mail_alert_ts[key] = now  # 判定与计时在同一把锁内：分开写会让两路同时放行
        return True


# 容量告警去重（进程内）：首次触顶通知一次，之后静默拒绝（防通知风暴；重启后重置）
_capacity_alerts = {"users": False, "accounts": False}


def _notify_capacity_once(kind, limit, label):
    """容量触顶通知（每进程每种资源只发一次）：管理员知情且不刷屏。

    调用点持 `_file_lock`，故发信经 `run_after_file_lock` 登记、出锁后执行
    （工单 ba-p05-01：锁内不得有网络 I/O）。**只有发信被推迟**：去重旗仍在锁内
    落定，否则两个并发请求会各登记一封、出锁后双发。旗先立后发、失败不回滚的
    口径不变。未持锁时登记即刻执行，行为同今天。

    参数注入口径见模块头「通信」（`send_notification` 是既有打桩点）。
    """
    if _capacity_alerts.get(kind):
        return  # 此后同资源的拒绝一律静默
    # 旗立在发送之前，且发送失败不回滚：本进程对这个资源从此彻底安静（重启才重置）。
    # 也就是说"没收到容量告警"不等于"没触顶"——要看日志里的 warning 行。
    _capacity_alerts[kind] = True
    logger.warning("%s已达上限 %d，已拒绝新注册/添加", label, limit)
    run_after_file_lock(
        _host("send_notification"),
        f"{label}已达上限",
        mail_layout.Mail(
            summary=f"{label}已达上限，新的注册/添加已被拒绝。",
            fields=[("当前上限", limit)],
            advice=["如需扩容请在 .env 调整 YIBAN_MAX_USERS / YIBAN_MAX_ACCOUNTS"],
            level="urgent",
        ),
        urgent=True,
    )
