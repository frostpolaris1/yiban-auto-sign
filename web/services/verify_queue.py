# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""账号在线校验的执行闸门、异步任务与失败落库。

**功能**
外呼的两条路径：同步带闸的通用闸门 `run_gated_call`（全局并发席位 + 每用户配额，附异常
类型 `VerifyGateBusy` / `VerifyQuotaExceeded`）与它在账号验证上的绑定
`run_verify_with_gate`；异步任务族的建任务
`_start_verify_job`、待办上限 `_verify_queue_full`、超龄收口 `_reclaim_stale_verify_jobs`、
失败落库 `_reject_account`、编辑路径的后台探针 `start_edit_probe`（不绑定账号 id，
失败只留痕）；外加两个开关的解析 `verify_async_enabled` / `_account_verify_enabled`。

**归属**
原 `web/app.py` 的模块级校验队列辅助，唯一真源在本模块；`web/app.py` 只保留名字面与
转发，把它自己持有、而本模块需要的模块级名字——外呼席位信号量 `_verify_sem`、每用户配额
判定 `_verify_attempt_allowed`、只读验证 `_verify_account_clean`、`.env` 路径与读取器——
在调用时刻现取后注入。账号审核态词表（`ACCOUNT_STATUS_*`）与待办上限常量随账号域留在
`web/services/accounts_data.py`，本模块按名取用。

**复用**
全局席位与每用户配额共用一次调用序列（先抢席位、再扣配额，席位在 `finally` 释放）；
异步任务的核心（线程、终态、CAS 拒绝）复用 `yiban.attempt.jobs` 的真源，本模块只做
宿主侧编排，不另写第二套任务机；"置账号为已拒绝"与"收口超龄任务"共用同一个审核态常量。

**通信**
本模块不反向导入 `web.app`（本仓测试以别名加载 `app.py`，普通 import 会再执行一份副本
模块）。席位、配额判定、只读验证与 `.env` 读取都作为显式参数接收：它们在 `web.app` 上
是会被测试打桩的模块级名字（既有测试在 `web.app` 上打桩 `_verify_account_clean` 与
`read_env`），本模块另持一份绑定会让这些打桩静默失效。异步任务真源按 `yiban.attempt.jobs`
取用（不是 `web.app` 上的别名，那个名字留给宿主模块的名字面）。
"""

import logging
import os
import threading
import time

from web.services.accounts_data import ACCOUNT_STATUS_REJECTED
from yiban.adapters.config.env_io import parse_env_flag
from yiban.adapters.persistence import db
from yiban.attempt import jobs as attempt_jobs
from yiban.core.masking import mask_phone as _mask_phone

# 与 web.app 同名的日志通道：校验队列的告警落回既有通道，便于运维沿用同一处过滤
logger = logging.getLogger("web")

#: 待办（pending + running）任务上限：每个任务占一个后台线程，而外呼席位远小于此，
#: 不设上界时并发提交会堆出大量等席位的线程。这是 503「校验繁忙」在异步模式下的
#: **可达来源**（席位满由后台排队消化、不拒绝用户；待办队列满才拒绝）。
VERIFY_JOBS_MAX_PENDING = attempt_jobs.MAX_PENDING

#: 异步模式待办队列已满时的 503 文案（`/api/verify-jobs` 与账号添加路径共用）。
VERIFY_BUSY_MSG = "校验繁忙，请稍后重试"
#: 账号验证认证失败冷却中的 429 文案。按「被锁定对象 = 易班账号 = 手机号」设冷却；
#: 仅限 web 验证路径，探针与 cron 签到不受影响。
VERIFY_FAIL_COOLDOWN_MSG = (
    "该手机号验证失败次数过多，已被临时限制验证，请约 10 分钟后再试；"
    "连续密码错误会导致易班账号被锁定，如密码有误请先在易班 APP 重置。"
)


# ---------------------------------------------------------------------------
# 宿主注入登记点（批 3-c S3）
# ---------------------------------------------------------------------------
# 本模块需要的宿主侧状态有三项，且都必须是**组合根持有、可被测试替换**的：
#   · 外呼席位信号量 `seat`——**必须与异步路径共用同一个**（`yiban.attempt.jobs`
#     经 `configure(seat=...)` 拿的是同一枚）；
#   · 每用户配额判定 `attempt_allowed` 与只读验证 `verify_clean`；
#   · `.env` 路径与读取器——路径可被 `--env-file` 与测试赋值改写，自持绑定会读到旧值。
# 沿用 `yiban/attempt/jobs.py` 已在用的形态：宿主在 import 末尾登记 lambda，本模块按
# 调用时刻取。未登记即 fail-closed（抛 `RuntimeError`，不静默回退默认值）——
# 静默回退会让"忘记登记"表现为"校验永远放行/永远关闭"，属最坏的假绿方向。
_hooks = {}


def configure(seat=None, attempt_allowed=None, verify_clean=None, env_file=None,
              read_env=None):
    """注入宿主侧依赖（登记 lambda，见上方说明）。可重复调用（测试用）。"""
    if seat is not None:
        _hooks["seat"] = seat
    if attempt_allowed is not None:
        _hooks["attempt_allowed"] = attempt_allowed
    if verify_clean is not None:
        _hooks["verify_clean"] = verify_clean
    if env_file is not None:
        _hooks["env_file"] = env_file
    if read_env is not None:
        _hooks["read_env"] = read_env


def _host(key):
    """取宿主登记的依赖。未登记即 fail-closed。

    **`try` 只包住取键，不包住调用**：宿主登记 lambda 内部抛的 `KeyError` 是真实
    业务错误，必须原样传出（旧写法会把它改写成"尚未登记宿主依赖"，抹掉故障位置）。
    """
    try:
        fn = _hooks[key]
    except KeyError:
        raise RuntimeError(
            "web.services.verify_queue 尚未登记宿主依赖 %r：请确认 web/app.py 的 import "
            "末尾调用了 web.services.verify_queue.configure(...)" % (key,)
        ) from None
    return fn()


class VerifyGateBusy(Exception):
    """外呼校验的并发闸已满（全局席位满，或同一账号已有一次外呼在飞）。"""


class VerifyQuotaExceeded(Exception):
    """该会话用户的验证尝试配额用尽。"""


#: 同一账号的 in-flight 外呼（工单 cgx6）。形状不是实例：**"同一账号不得被并发持有"
#: 是本仓已确立的不变式**（引擎侧靠单条 CAS 判据）；web 侧此前只有全局席位 + 每用户
#: 配额，两者都不阻止"同一手机号同时跑两次真实登录"——账号验证与设备补全各点一次
#: 即可重叠。故守卫放在**两条外呼共用的闸门**上，不逐点打补丁。
#:
#: 键取手机号（易班账号的唯一标识）；空键不守卫（没有可互斥的对象）。
#: gunicorn 单进程多线程形态下进程内集合即足够；跨进程（多 worker）形态不在本仓
#: 生产口径内（`-w 1 --threads 32` 是明文不变量）。
_in_flight_accounts = set()
_in_flight_cv = threading.Condition()


def _account_key(phone):
    return str(phone or "").strip()


def acquire_account(phone, timeout=None):
    """占用某账号的外呼权。返回 True=已占用，False=超时/已被占用。

    `timeout=0` 是非阻塞（同步请求路径用：同账号已在飞时立刻回"校验繁忙"，
    不把请求线程挂住）；`timeout=None` 无限等待（异步任务路径用：后台线程可以排队，
    但**绝不会与另一次外呼重叠**）。
    """
    key = _account_key(phone)
    if not key:
        return True
    with _in_flight_cv:
        if timeout is None:
            while key in _in_flight_accounts:
                _in_flight_cv.wait()
        else:
            deadline = time.monotonic() + max(0.0, timeout)
            while key in _in_flight_accounts:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    return False
                _in_flight_cv.wait(remain)
        _in_flight_accounts.add(key)
        return True


def release_account(phone):
    """释放某账号的外呼权（与 `acquire_account` 成对；空键为 no-op）。"""
    key = _account_key(phone)
    if not key:
        return
    with _in_flight_cv:
        _in_flight_accounts.discard(key)
        _in_flight_cv.notify_all()


def run_account_exclusive(phone, action, timeout=None):
    """在"同一账号互斥"下执行 `action()`；占用不到时抛 VerifyGateBusy。

    异步任务路径（`yiban.attempt.jobs` 经宿主登记的 `verify_one`）用它把
    "同账号两次真实登录"排成串行：后到者等待前一次结束再跑（`timeout=None`）。
    同步请求路径不经过这里——`run_gated_call` 直接做非阻塞占用。
    """
    if not acquire_account(phone, timeout=timeout):
        raise VerifyGateBusy()
    try:
        return action()
    finally:
        release_account(phone)


def run_gated_call(action, username, limits, subject=None):
    """在全局并发席位、账号互斥与每用户配额下执行一次**真实外呼**，返回结果。

    这是"web 进程代用户外呼"的**唯一闸门**：`run_verify_with_gate`（账号验证）与设备
    登记补全都经它——两条外呼同为一次真实登录，风控暴露面相同，闸门必须同源；各写一份
    迟早分叉（一边补了新闸、另一边没有）。

    `subject` 是被操作账号的手机号：非空时占用"同一账号互斥"权（工单 cgx6）。
    非阻塞占用——同账号已有外呼在飞时立即抛 VerifyGateBusy，不排队（请求线程等不起）。

    顺序：**先抢全局席位、再抢账号权、最后扣用户配额**——任何一步失败都不消耗
    用户配额（自己的饱和不该变成对用户的惩罚）。席位与账号权在 `finally` 中释放，
    含配额拒绝与 action 异常两条路径。

    席位信号量与配额判定按调用时刻取自宿主登记的 lambda（见 `configure`）：两者都是
    会被测试打桩的模块级名字，本模块另持绑定会让打桩静默失效（席位还必须与异步路径
    共用同一个）。
    """
    seat = _host("seat")
    attempt_allowed = _host("attempt_allowed")
    if not seat.acquire(blocking=False):
        raise VerifyGateBusy()
    try:
        if not acquire_account(subject, timeout=0):
            raise VerifyGateBusy()
        try:
            if not attempt_allowed(limits, username):
                raise VerifyQuotaExceeded()
            return action()
        finally:
            release_account(subject)
    finally:
        seat.release()


def run_verify_with_gate(clean, username, limits):
    """执行一次外呼校验（全局并发闸包裹）。本函数是 `run_gated_call` 在账号验证上的绑定。

    `limits` 是每用户配额表（`current_app.extensions` 登记，经 `web.routes.verify_limits()`
    取用；进程内字典，故显式传入）。

    返回 verify_err（None 表示验证通过）；并发满/同账号已在飞/配额尽时抛上述异常，
    由调用方翻译成 503 / 429。
    """
    return run_gated_call(
        lambda: _host("verify_clean")(clean), username, limits,
        subject=clean.get("phone"),
    )


def _reject_account(phone, reason, account_id, expect_status):
    """把校验未通过的账号置为 rejected（账号**留在库中**并承载技术原因）。

    `expect_status` 是任务建立时账号的状态（任务表里的 prev_status）。**只有账号
    仍处于该状态时才写**——异步校验在后台跑，期间管理员可能已点"审核通过"
    （pending → active），无条件写回会把管理员的决定静默回滚。人类决定优先。

    缺少任务上下文时**不写**：无从判断账号是否已被人工改动，宁可不改也不能覆盖
    人工决定（账号状态由管理员在审核列表里可见并处理）。

    **两种缺上下文分开报级**：`account_id is None` 是**设计内路径**——编辑探针任务
    刻意不绑账号 id（见 `start_edit_probe`），探针失败只留痕、绝不回写状态。这条
    路径按 INFO 报，措辞点明"设计内"；按 ERROR 报会让运维照缺陷口径追，而它每天
    都会正常出现。`account_id` 有值而 `prev_status` 空才是真缺陷（任务上下文丢失），
    仍按 ERROR 报。
    """
    if account_id is None or not expect_status:
        masked = _mask_phone(phone)
        if account_id is None:
            logger.info("编辑探针任务不绑账号 id（设计内），不改账号状态: %s", masked)
        else:
            logger.error("缺少任务上下文（account_id=%r / prev_status=%r），不改账号状态: %s",
                         account_id, expect_status, masked)
        return
    try:
        wrote = db.update_account_status_if(
            account_id, ACCOUNT_STATUS_REJECTED, expect_status,
            reason or "在线校验未通过",
        )
        if not wrote:
            logger.info(
                "账号 %s 状态已人工变更（非 %s），异步校验结果不覆盖人工决定",
                _mask_phone(phone), expect_status,
            )
    except Exception as e:
        logger.error("置账号为 rejected 失败（%s）: %s", _mask_phone(phone), e)


def _reclaim_stale_verify_jobs():
    """收口超龄校验任务（启动期与取消端点调用；入队路径见 `_verify_queue_full`）。

    进程在任务执行期间消失（重启/重部署/OOM）会让任务永久停在 running：既不去
    终态、又不可取消，还一直占用待办名额，累计到上限后所有新增账号的在线校验
    永久 503。收口与账号侧的 CAS 拒绝在同一事务内完成（见 store 的 reclaim_stale）。
    返回收口条数。实现只从真源取数（不依赖 `web.app` 名字面），故启动路径可用。
    """
    rows = db.reclaim_stale_verify_jobs(reject_status=ACCOUNT_STATUS_REJECTED)
    if rows:
        logger.warning("已收口 %d 条超龄校验任务（进程重启或线程异常终止）", len(rows))
    return len(rows)


def _verify_queue_full():
    """待办队列是否已满（判定前先收口超龄任务，否则卡死的任务会永久占满名额）。"""
    _reclaim_stale_verify_jobs()
    return db.count_active_verify_jobs() >= VERIFY_JOBS_MAX_PENDING


def _start_verify_job(clean, username, account_id, fails, limits):
    """建任务并起后台线程；待办队列满时抛 VerifyGateBusy（调用方翻成 503）。"""
    started = attempt_jobs.start(clean, username, account_id, fails, limits)
    if started is None:
        raise VerifyGateBusy()
    return started


def start_edit_probe(clean, username, fails, limits, start_job,
                     enabled, queue_full, attempt_allowed, cooldown_remaining):
    """编辑账号后对**已变更的易班凭据**做后台复核，返回 job_id 或 None。

    **用途**
    新增账号路径有真实登录探针。编辑路径此前既无探针也无本地口令策略，错误凭据被
    静默接受。本函数补上这条前提：口令或识别码实际变更时，后台复核新凭据。

    **语义**
    一律走异步任务，不退回同步带闸路径。同步会占住请求线程与外呼席位。编辑已落盘，
    不能回滚。探针失败**不改账号可用状态语义**：不自动禁用，不回审翻转。失败只留
    可查记录（任务终态、审计行、失败冷却）。

    **承重突变**
    任务**不绑定账号 id**（`start_job(..., None, ...)`）。这是"探针失败不改账号
    可用状态"的唯一机制：`yiban/attempt/jobs.py` 的失败回调与
    `yiban/adapters/persistence/verify_jobs.py` 的超龄收口都以 `account_id is None` 为跳过条件
    （见 `_reject_account` 与 `reclaim_stale`）。改传真实账号 id 后，校验失败会把
    账号翻成 rejected，违反本端点的验收。

    **跳过条件**
    以下任一成立时跳过探针，编辑仍算成功：账号校验开关未开、该手机号在失败冷却中、
    待办队列已满、每用户配额已用尽。以上都是 best-effort。

    **注入**
    `start_job` 等宿主侧能力由调用方传入（保持 `web.app.<名字>` 的打桩面）。
    """
    phone = clean.get("phone", "")
    if not enabled():
        return None
    if cooldown_remaining() > 0:
        logger.info("编辑探针跳过：手机号 %s 在验证失败冷却中", _mask_phone(phone))
        return None
    if queue_full():
        logger.warning("编辑探针跳过：校验任务队列已满")
        return None
    if not attempt_allowed():
        logger.info("编辑探针跳过：验证尝试配额已用尽")
        return None
    try:
        job_id, _ = start_job(clean, username, None, fails, limits)
    except VerifyGateBusy:
        # 建任务与扣配额之间队列被占满（竞态兜底）：编辑已成功，不因此报错。
        logger.warning("编辑探针跳过：校验任务队列已满（入队竞态）")
        return None
    return job_id


def _effective_switch(key):
    """两个验证开关**共用**的 overlay 读取器：`.env` 优先、进程环境只补缺（M27）。

    原实现两个相邻开关各读一层且优先级相反：`verify_async_enabled` 先读
    `os.environ`、缺键才读 `.env`（进程环境优先，与 M27 反了），
    `_account_verify_enabled` 只读 `.env`（`docker run -e` / systemd
    `Environment=` 形态下开关打不开）。两者还都自称"同口径"。
    这里按仓库口径统一：`.env` 有非空值就取它，否则回退进程环境。
    `.env` 路径与读取器按调用时刻取自宿主登记的 lambda（见 `configure`）。
    """
    file_env = _host("read_env")(_host("env_file"))
    value = file_env.get(key, "")
    if str(value or "").strip():
        return str(value)
    return os.environ.get(key, "")


def verify_async_enabled():
    """在线校验异步开关：**默认关**，显式置 `YIBAN_VERIFY_ASYNC=1` 才启用。

    异步路径把外呼交给后台任务、请求线程不再被占用（长任务不拖垮整站），
    但**结果需要前端轮询 `/api/verify-jobs/<id>` 才能展示**——当前前端尚未实现该轮询，
    故默认走同步带闸路径：用户提交账号时当场得到校验结论（与界面现状一致）。
    前端就绪后把默认值改为开即可（两侧契约已在 `yiban.attempt/jobs.py` 与
    `/api/verify-jobs` 端点就位，测试也按显式开关覆盖了两条路径）。

    与 `YIBAN_ACCOUNT_VERIFY` **同口径**（同一 overlay 读取器 `_effective_switch`）：
    `.env` 优先、进程环境补缺（M27）。
    """
    return _effective_switch("YIBAN_VERIFY_ASYNC").strip().lower() in (
        "1", "true", "on", "yes")


def _account_verify_enabled():
    """注册/添加账号时是否做即时验证（YIBAN_ACCOUNT_VERIFY=1，任意管理员可开关）。

    与 `verify_async_enabled` 共用 `_effective_switch`（`.env` 优先、进程环境补缺）。

    真值口径单源在 `yiban.adapters.config.env_io.parse_env_flag`（1/true/on/yes，大小写与两侧空白
    不敏感），与面板 `GET /api/settings` 的 account_verify 同口径。
    """
    return parse_env_flag(_effective_switch("YIBAN_ACCOUNT_VERIFY"), default=False,
                          key="YIBAN_ACCOUNT_VERIFY", log=logger)
