# -*- coding: utf-8 -*-
"""在线校验异步任务：排队、外呼、看门狗与收口。

调度核心的一环——请求线程只**扣配额并建任务**，外呼交给后台线程，因此可以等待全局席位
（请求线程不能等，那正是要消除的线程占用）。

收口路径共三条，都经 CAS 落终态，互不覆盖：
① 正常完成 → done / rejected（并置账号为 rejected）
② 等待席位超时 → rejected
③ 看门狗到点（`OUTBOUND_TIMEOUT`）→ rejected；
   worker 之后回来时 CAS 已失配，不会把终态改写掉。

宿主（`web/app.py`）通过 `configure()` 注入外呼实现、席位闸与账号写回动作——这些都是
web 侧的进程内状态与业务规则，本模块不复制它们；用 lambda 延迟解析，故测试替换 `webapp`
上的实现后仍然生效。
"""
import logging
import threading
import time

from ..store import verify_jobs as store

logger = logging.getLogger("yiban.attempt.jobs")

OUTBOUND_TIMEOUT = 30   # 单次外呼预算（秒），超出按 rejected 收口（技术原因）
GATE_WAIT = 300         # 后台任务等待全局校验席位的最长秒数
MAX_PENDING = 32        # 待办队列上限（pending + running）

TERMINAL_STATUSES = store.TERMINAL_STATUSES

_hooks = {}


def configure(seat=None, verify_one=None, record_failure=None, mask_phone=None,
              reject_account=None, queue_full=None):
    """注入宿主侧依赖（延迟 lambda，见模块 docstring）。可重复调用（测试用）。"""
    if seat is not None:
        _hooks["seat"] = seat
    if verify_one is not None:
        _hooks["verify_one"] = verify_one
    if record_failure is not None:
        _hooks["record_failure"] = record_failure
    if mask_phone is not None:
        _hooks["mask_phone"] = mask_phone
    if reject_account is not None:
        _hooks["reject_account"] = reject_account
    if queue_full is not None:
        _hooks["queue_full"] = queue_full


def start(clean, username, account_id, fails, limits):
    """建任务 + 起后台线程，返回 (job_id, created_at)；队列满返回 None。

    调用方在返回 None 时翻成 503（本模块不抛宿主的异常类型，避免反向依赖）。
    """
    if _hooks["queue_full"]():
        return None
    prev_status = str(clean.get("status") or store.VERIFY_JOB_PENDING)
    job_id, created_at = store.create(account_id, clean["phone"], username or "",
                                     prev_status=prev_status)
    t = threading.Thread(
        target=run,
        args=(job_id, clean, username, account_id, prev_status, fails, limits),
        name=f"verify-job-{job_id}",
        daemon=True,
    )
    t.start()
    return job_id, created_at


def run(job_id, clean, username, account_id, prev_status, fails, limits):
    """执行一条任务（后台线程）。落终态后写账号的那一步同样带 CAS：
    `prev_status` 是任务建立时的账号状态，人工审批先于迟到的校验结果。"""
    if not store.claim(job_id):
        return  # 已被取消（或已被别的线程接走）

    expired = threading.Event()
    mask = _hooks["mask_phone"]

    def _reject(reason):
        _hooks["reject_account"](clean["phone"], reason, account_id, prev_status)

    def _on_timeout():
        expired.set()
        if store.finish(job_id, error="外呼超时：校验未在预算内完成"):
            _reject("外呼超时：校验未在预算内完成")

    watchdog = threading.Timer(OUTBOUND_TIMEOUT, _on_timeout)
    watchdog.daemon = True
    watchdog.start()
    try:
        if not _hooks["seat"].acquire(timeout=GATE_WAIT):
            if store.finish(job_id, error="校验繁忙：等待全局校验席位超时"):
                _reject("校验繁忙：等待全局校验席位超时")
            return
        try:
            if expired.is_set():
                return  # 看门狗已收口，省掉一次无谓外呼
            verify_err = _hooks["verify_one"](clean)
        finally:
            _hooks["seat"].release()
    finally:
        watchdog.cancel()

    if expired.is_set():
        return
    if verify_err:
        from yiban.store import db
        fail_kind = _hooks["record_failure"](fails, clean["phone"], verify_err, time.time())
        db.audit(username or "?", "account_verify_job_fail", mask(clean["phone"]),
                 f"异步校验未通过（{fail_kind}）")
        if store.finish(job_id, error=verify_err):
            _reject(verify_err)
    else:
        store.finish(job_id)
