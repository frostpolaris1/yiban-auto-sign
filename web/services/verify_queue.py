# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""账号在线校验的执行闸门、异步任务与失败落库。

**功能**
外呼校验的两条路径：同步带闸的 `run_verify_with_gate`（全局并发席位 + 每用户配额，
附异常类型 `VerifyGateBusy` / `VerifyQuotaExceeded`）与异步任务族的建任务
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

from web.services.accounts_data import ACCOUNT_STATUS_REJECTED
from yiban.attempt import jobs as attempt_jobs
from yiban.infra.env_io import parse_env_flag
from yiban.masking import mask_phone as _mask_phone
from yiban.store import db

# 与 web.app 同名的日志通道：校验队列的告警落回既有通道，便于运维沿用同一处过滤
logger = logging.getLogger("web")

#: 待办（pending + running）任务上限：每个任务占一个后台线程，而外呼席位远小于此，
#: 不设上界时并发提交会堆出大量等席位的线程。这是 503「校验繁忙」在异步模式下的
#: **可达来源**（席位满由后台排队消化、不拒绝用户；待办队列满才拒绝）。
VERIFY_JOBS_MAX_PENDING = attempt_jobs.MAX_PENDING


class VerifyGateBusy(Exception):
    """外呼校验的全局并发席位已满（**未消耗**每用户配额）。"""


class VerifyQuotaExceeded(Exception):
    """该会话用户的验证尝试配额用尽。"""


def run_verify_with_gate(clean, username, limits, seat, attempt_allowed, verify_clean):
    """执行一次外呼校验（全局并发闸包裹）。

    `limits` 是每用户配额表（`current_app.extensions` 登记，经 `web.routes.verify_limits()`
    取用；进程内字典，故显式传入）。

    顺序刻意如此：**先抢全局席位、再扣用户配额**——抢不到席位时立即抛
    VerifyGateBusy 且不消耗配额，否则我们自己的饱和会变成对用户的惩罚。
    席位在 `finally` 中释放，含配额拒绝与校验异常两条路径。

    返回 verify_err（None 表示验证通过）；并发满/配额尽时抛上述异常，
    由调用方翻译成 503 / 429。

    席位信号量、配额判定与只读验证都由调用方传入（`web.app` 的 `_verify_sem` /
    `_verify_attempt_allowed` / `_verify_account_clean`）：三者都是会被测试打桩的
    模块级名字，本模块另持绑定会让打桩静默失效（席位还必须与异步路径共用同一个）。
    """
    if not seat.acquire(blocking=False):
        raise VerifyGateBusy()
    try:
        if not attempt_allowed(limits, username):
            raise VerifyQuotaExceeded()
        return verify_clean(clean)
    finally:
        seat.release()


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
    `yiban/store/verify_jobs.py` 的超龄收口都以 `account_id is None` 为跳过条件
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


def verify_async_enabled(read_env, env_file):
    """在线校验异步开关：**默认关**，显式置 `YIBAN_VERIFY_ASYNC=1` 才启用。

    异步路径把外呼交给后台任务、请求线程不再被占用（长任务不拖垮整站），
    但**结果需要前端轮询 `/api/verify-jobs/<id>` 才能展示**——当前前端尚未实现该轮询，
    故默认走同步带闸路径：用户提交账号时当场得到校验结论（与界面现状一致）。
    前端就绪后把默认值改为开即可（两侧契约已在 `yiban/attempt/jobs.py` 与
    `/api/verify-jobs` 端点就位，测试也按显式开关覆盖了两条路径）。

    与 `YIBAN_ACCOUNT_VERIFY` 同口径读 `.env`（`os.environ` 优先，便于临时覆盖）——
    只读 os.environ 会让"只配 .env"的常规部署用不上这个开关。`.env` 路径与读取器由
    调用方传入（`web.app` 的 `ENV_FILE` / `read_env`）：本函数须用**本进程的**路径，
    它可被 `--env-file` 改写，两者也都是会被测试改写的模块级名字。
    """
    raw = os.environ.get("YIBAN_VERIFY_ASYNC")
    if raw is None:
        raw = read_env(env_file).get("YIBAN_VERIFY_ASYNC", "")
    return str(raw).strip().lower() in ("1", "true", "on", "yes")


def _account_verify_enabled(read_env, env_file):
    """注册/添加账号时是否做即时验证（YIBAN_ACCOUNT_VERIFY=1，任意管理员可开关）。

    `.env` 路径与读取器由调用方传入（`web.app` 的 `ENV_FILE` / `read_env`）：
    两者都是会被测试改写、也会随 `--config` 变化的模块级名字。

    真值口径单源在 `yiban.infra.env_io.parse_env_flag`（1/true/on/yes，大小写与两侧空白
    不敏感），与面板 `GET /api/settings` 的 account_verify 同口径。
    """
    env = read_env(env_file)
    return parse_env_flag(env.get("YIBAN_ACCOUNT_VERIFY", ""), default=False,
                          key="YIBAN_ACCOUNT_VERIFY", log=logger)
