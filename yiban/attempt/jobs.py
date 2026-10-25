# -*- coding: utf-8 -*-
"""在线校验异步任务：排队、外呼、看门狗与收口。

调度核心的一环——请求线程只**扣配额并建任务**，外呼交给后台线程，因此可以等待全局席位
（请求线程不能等，那正是要消除的线程占用）。

收口路径共五条，都经 CAS 落终态，互不覆盖：
① 正常完成 → done / rejected（并置账号为 rejected）
② 等待同账号互斥超时 → 任务落终态（error 文案带「请稍后重试」），**不改账号状态**
③ 等待全局席位超时 → 任务落终态（error 文案带「请稍后重试」），**不改账号状态**
④ 计时器创建/启动失败或外呼实现抛异常 → 任务落终态，**不改账号状态**
⑤ 看门狗到点（`OUTBOUND_TIMEOUT`，只计外呼、不计任何排队）→ rejected；
   worker 之后回来时 CAS 已失配，不会把终态改写掉。

宿主（`web/app.py`）通过 `configure()` 注入外呼实现、席位闸与账号写回动作——这些都是
web 侧的进程内状态与业务规则，本模块不复制它们；用 lambda 延迟解析，故测试替换 `webapp`
上的实现后仍然生效。
"""
import logging
import threading
import time

from yiban.adapters.persistence import verify_jobs as store

logger = logging.getLogger("yiban.attempt.jobs")

OUTBOUND_TIMEOUT = 30   # 单次外呼预算（秒），超出按 rejected 收口（技术原因）
GATE_WAIT = 300         # 后台任务等待全局校验席位的最长秒数
MAX_PENDING = 32        # 待办队列上限（pending + running）

TERMINAL_STATUSES = store.TERMINAL_STATUSES

_hooks = {}


def configure(seat=None, verify_one=None, record_failure=None, mask_phone=None,
              reject_account=None, queue_full=None, acquire_account=None,
              release_account=None):
    """注入宿主侧依赖（延迟 lambda，见模块 docstring）。可重复调用（测试用）。

    `acquire_account` / `release_account` 是可选的同账号互斥钩子。给了 `acquire_account`
    时，它在看门狗起表**之前**执行：排队等另一个外呼结束不消耗外呼预算。钩子缺省时不
    加锁，既有测试与单进程调用方语义不变。两个钩子必须成对登记：只给一半会让账号权
    永久泄漏。
    """
    if (acquire_account is None) != (release_account is None):
        raise ValueError("acquire_account 与 release_account 必须成对登记")
    if seat is not None:
        _hooks["seat"] = seat
    if verify_one is not None:
        _hooks["verify_one"] = verify_one
    if acquire_account is not None:
        _hooks["acquire_account"] = acquire_account
    if release_account is not None:
        _hooks["release_account"] = release_account
    if record_failure is not None:
        _hooks["record_failure"] = record_failure
    if mask_phone is not None:
        _hooks["mask_phone"] = mask_phone
    if reject_account is not None:
        _hooks["reject_account"] = reject_account
    if queue_full is not None:
        _hooks["queue_full"] = queue_full


def start(clean, username, account_id, fails, limits=None):
    """建任务 + 起后台线程，返回 (job_id, created_at)；队列满返回 None。

    调用方在返回 None 时翻成 503（本模块不抛宿主的异常类型，避免反向依赖）。

    `limits` **不消费也不下传**（工单 ba-p10-06）：用户级配额只在宿主侧的同步路径
    （`web/services/verify_queue.py::run_verify_with_gate` 的 `attempt_allowed`）判定，
    后台线程从不按它扣额。保留形参只为兼容既有调用方（该调用点不在本批改动边界内）；
    线程参数里不再带它——旧实现把它一路透传进 `run` 却全程不读，让读者以为异步路径
    也持有限额判据。
    """
    del limits  # 宿主侧已判；后台线程不消费（见 docstring）
    if _hooks["queue_full"]():
        return None
    prev_status = str(clean.get("status") or store.VERIFY_JOB_PENDING)
    job_id, created_at = store.create(account_id, clean["phone"], username or "",
                                     prev_status=prev_status)
    t = threading.Thread(
        target=run,
        args=(job_id, clean, username, account_id, prev_status, fails),
        name=f"verify-job-{job_id}",
        daemon=True,
    )
    t.start()
    return job_id, created_at


def run(job_id, clean, username, account_id, prev_status, fails):
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

    # 同账号互斥与全局席位都是**排队**，不是外呼。两者都必须在看门狗起表之前完成，
    # 否则排队时间会被算进 `OUTBOUND_TIMEOUT`：一次外呼都没发生，账号却被判死。
    # 先取账号权、再取席位：等待同一账号时不应占着稀缺的全局席位。
    # 两个等待各有上界（`GATE_WAIT`）。超时只落任务终态（error 文案为「稍后重试」），
    # 不改账号状态——账号没有出错，只是此刻忙。
    phone = clean.get("phone")
    acquire_account = _hooks.get("acquire_account")
    release_account = _hooks.get("release_account")
    if acquire_account is not None and not acquire_account(phone, GATE_WAIT):
        store.finish(job_id, error="校验繁忙：等待同账号校验超时，请稍后重试")
        return
    # 席位句柄**在这一行取一次**，acquire/release 必须是同一个对象：宿主的
    # `seat` 是 BoundedSemaphore（超发会直接 `ValueError: Semaphore released too
    # many times`），而读两次 `_hooks["seat"]` 就等于假设"取席位与还席位之间注册
    # 不会被换掉"——全量测试里这条假设真的会破（上一个测试文件留下的校验线程还在
    # 飞，下一个文件调用 configure 重新注册了自己的信号量，旧线程的 release 就打到
    # 别人的信号量上）。取一次句柄后，释放永远回到它当初拿的那个对象。
    seat = _hooks["seat"]
    if not seat.acquire(timeout=GATE_WAIT):
        if release_account is not None:
            release_account(phone)
        store.finish(job_id, error="校验繁忙：等待全局校验席位超时，请稍后重试")
        return
    # 看门狗只计**外呼**预算，从两个等待都完成、真正要外呼的这一刻起表。
    # 席位与账号权从拿到起就由**一个** try/finally 兜底：`BaseException`
    # （KeyboardInterrupt / SystemExit）也走释放段。旧写法分成两个 try，且计时器
    # 那段只捕 `Exception`——BaseException 会越过释放段，两个资源永久泄漏
    # （dix A-2）。异常照原样上抛；只有 `Exception` 才转成业务终态。
    watchdog = None
    slot_released = False

    def _release_slot():
        # 释放一次且仅一次：计时器启动失败的 `Exception` 分支先释放再落终态，
        # 外层 finally 会再调一次；BoundedSemaphore 超发会直接 ValueError。
        nonlocal slot_released
        if slot_released:
            return
        slot_released = True
        if watchdog is not None:
            watchdog.cancel()
        seat.release()
        if release_account is not None:
            release_account(phone)

    try:
        try:
            watchdog = threading.Timer(OUTBOUND_TIMEOUT, _on_timeout)
            watchdog.daemon = True
            watchdog.start()
        except Exception:
            # 计时器没起来就不能进入外呼。先释放资源再落终态：`store.finish`
            # 自己也可能失败，资源不能等它。任务也要落终态，否则前端会显示
            # "进行中"直到 900 秒超龄收口。
            _release_slot()
            logger.exception("异步校验计时器启动失败（job=%s）", job_id)
            store.finish(job_id, error="校验启动失败：计时器未启动")
            return
        verify_err = _hooks["verify_one"](clean)
    except Exception as e:
        # 外呼实现抛异常是内部错误，不是账号问题：任务落终态，账号状态不动。
        logger.exception("异步校验外呼异常（job=%s）", job_id)
        store.finish(job_id, error=f"校验异常：{type(e).__name__}")
        return
    finally:
        _release_slot()

    if expired.is_set():
        return
    if verify_err:
        from yiban.adapters.persistence import db
        fail_kind = _hooks["record_failure"](fails, clean["phone"], verify_err, time.time())
        db.audit(username or "?", "account_verify_job_fail", mask(clean["phone"]),
                 f"异步校验未通过（{fail_kind}）")
        if store.finish(job_id, error=verify_err):
            _reject(verify_err)
    else:
        store.finish(job_id)
