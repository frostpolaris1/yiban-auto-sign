# -*- coding: utf-8 -*-
"""Webhook 推送的账本层：模块级可变状态、状态目录/路径解析、账本与节流文件的
"锁内读-改-写"，以及每日预算记账（占用 / 退还 / 耗尽告知凭证）。

磁盘是唯一事实源：`locks.file_lock`（POSIX flock / Windows msvcrt）串行化 web（常驻）
与 signin（cron 新进程）的读-改-写，进程内账本锁只承担同进程互斥。
依赖方向：config ← 本层 ← transport。
"""
import json
import logging
import os
import threading
import time
from contextlib import contextmanager, suppress

from yiban.infra import locks

from . import config

logger = logging.getLogger("notify")

# 节流状态（升级为磁盘持久化后）：
# _throttle_ts / _throttle_lock 只承担「本进程快速路径」——本进程刚放行过的标题
# 在窗口内直接跳过（省磁盘 IO）；跨进程一致性由磁盘 notify-throttle.json +
# 文件锁保证（见 _throttle_due）。保持这两个符号名不变，既有测试的
# `_throttle_ts.clear()` 复位口径仍可用（但注意它只清内存，不删磁盘节流文件）。
_throttle_ts = {}
_throttle_lock = threading.Lock()

# 每日推送预算（进程内计数，重启清零；Server酱免费 5 条/天由服务端自身响应兜底，
# 本计数用于提前感知并停止，避免徒劳请求）。：拆成非紧急 / 紧急两本账，
# 各自按日归零、各自持锁——signin 与 web 两个进程各持一份额度沿用既有事实，本轮不合并。
# 账本用字符串标识（_LEDGER_IDS）而不是布尔：退还凭证要把"退到哪本账"写死在占用时刻，
# 布尔在传参链上一旦取反就是静默错位；字符串标识在凭证与跳过日志里都能直接读出来。
#
# 每本账的 notice 子字典是"耗尽告知"的进程内状态（本组件只暴露状态，发邮件由调用方
# （web）负责；标记按日重置，见 _roll_locked_inner），三个字段语义各自独立：
#   pending   待取走的告知（pop_exhaustion_notice 取走）
#   notified  当日已交付给调用方——不再重复挂标记，保证"每本账每日各一次"
#   warned    当日已记过 warning；额度被退还撤销时**不**撤销它，否则通道长期失败
#             （每次占满都退还重来）会把 warning 刷成日志风暴
#
# 为什么 notice 与 count 共用同一把账本锁：判定"是否虚警"要读 count、
# 撤回告知要写 pending，两步必须在同一个临界区内完成。原先 pending 自持一把全局锁，
# 判定与撤回之间留了一道缝——另一线程（web 是 Flask threaded=True，设置页/健康检查确有
# 并发发送）可以在这道缝里把额度重新占满并挂上真实 pending，再被这一次陈旧 discard 抹掉；
# 又因 warned 不撤销、已 notified 的账本不再重挂，当日恰无后续被拒尝试时"告知静默"就
# 复现了。收进同一把锁后，挂标记与撤标记天然互斥；且仍是"一把锁管一件事"，
# 全程不存在嵌套持锁或跨锁调用，无死锁面。
_general_daily = {
    "state": {"date": "", "count": 0},
    "notice": {"pending": False, "notified": False, "warned": False},
    "lock": threading.Lock(),
}
_urgent_daily = {
    "state": {"date": "", "count": 0},
    "notice": {"pending": False, "notified": False, "warned": False},
    "lock": threading.Lock(),
}
# 登录失败告警独立账本。登录失败是公网最高频的告警源，web 侧
# send_notification(ledger="login_fail") 让它单独记账（YIBAN_LOGINFAIL_DAILY_MAX，
# 默认 3，0=不限），不再与 general/urgent 两本账互挤——喷洒类攻击把本账打满后，
# 审计链异常等真紧急告警仍可达手机。
_loginfail_daily = {
    "state": {"date": "", "count": 0},
    "notice": {"pending": False, "notified": False, "warned": False},
    "lock": threading.Lock(),
}
_LEDGER_IDS = ("general", "urgent", "login_fail")
_LEDGERS = {"general": _general_daily, "urgent": _urgent_daily, "login_fail": _loginfail_daily}

# 每日预算从「进程内内存」升级为「磁盘账本」——原实现 web（常驻）与
# signin（每次 cron 新进程）各持一份独立计数，默认 5 条/天的上限实际可发 15 条
# （web 5 + 首签 5 + 补签 5），且进程重启即清零。Server酱免费版 5 条/天是第三方
# **全局**约束，进程级计数让系统侧"每日预算"承诺形同虚设。
# 现把两本账的 state 与 notice 持久化到 $YIBAN_STATE_DIR/notify-ledger.json：
# - 跨进程一致性：文件锁（fcntl.flock，POSIX）串行化 web 与 signin 的读-改-写；
#   Windows 无 fcntl 退化为进程内锁（开发机单进程，接受）；
# - 进程重启不丢：账本随文件存活，重启后从磁盘恢复当日计数；
# - 跨日归零：_roll_locked_inner 检测到磁盘账本日期 != 今日时清零并随临界区落盘。
# 文件布局：{"general": {"date","count","pending","notified","warned"},
#           "urgent": {...}}——与内存账本结构一一对应，每次读改写整体序列化。


def _state_dir():
    """状态目录解析唯一口径：YIBAN_STATE_DIR 环境变量优先，回退 .env 同键，
    最后回落 .env 所在目录（与 signin 的状态目录口径一致）——web（常驻，
    经 .env 读）与 signin（cron，经 run.sh 注入环境变量）两侧必须指向同一
    目录，否则跨进程共享额度/节流窗口的目标落空。"""
    state_dir = os.environ.get("YIBAN_STATE_DIR", "").strip()
    if not state_dir:
        state_dir = config._read_env_file().get("YIBAN_STATE_DIR", "").strip()
    if not state_dir:
        state_dir = os.path.dirname(config._env_path()) or "."
    return state_dir


def _ledger_path():
    """账本文件路径：$YIBAN_STATE_DIR/notify-ledger.json。"""
    return os.path.join(_state_dir(), "notify-ledger.json")


def _throttle_path():
    """节流状态文件路径：$YIBAN_STATE_DIR/notify-throttle.json。

    与账本同目录：web 与 signin 共享同一文件，跨进程节流窗口才成立。
    """
    return os.path.join(_state_dir(), "notify-throttle.json")


@contextmanager
def _state_file_lock(filename):
    """状态文件锁：经 `locks.file_lock` 统一（POSIX flock / Windows msvcrt）。

    此前 Windows 上退化为 no-op 且无告警，账本与节流的跨进程互斥失效（额度可被
    多进程超发）。锁由统一原语自行拼 ``<path>.lock``，不与状态文件本身的读写句柄混用。
    """
    lock_path = os.path.join(_state_dir(), filename)
    lock_dir = os.path.dirname(lock_path) or "."
    with suppress(OSError):
        os.makedirs(lock_dir, exist_ok=True)
    with locks.file_lock(lock_path):
        yield


@contextmanager
def _ledger_file_lock():
    """账本文件锁（`_state_file_lock` 的账本专用入口，锁文件与旧版一致）。"""
    with _state_file_lock("notify-ledger.json"):
        yield


def _load_ledger_file():
    """读取磁盘账本（缺文件按空 dict 处理）。

    损坏/解析失败**不静默重置**：记 warning 并把损坏文件归档改名留证
    （notify-ledger.json.corrupt-<ts>-<pid>）再按空账处理——否则"读不了就当
    全新一天满额"会超发，且另一本账的已记账数据被整体抹除时零日志、无从排障。
    """
    path = _ledger_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("账本顶层不是 JSON 对象")
        for ledger_id in _LEDGER_IDS:
            val = data.get(ledger_id)
            if val is not None and not isinstance(val, dict):
                raise ValueError(f"账本 {ledger_id} 条目结构损坏（{type(val).__name__}）")
        return data
    except FileNotFoundError:
        return {}  # 首次运行 / 未初始化：正常缺文件，不是损坏
    except (OSError, ValueError, TypeError) as e:
        _archive_corrupt_state_file(path)
        logger.warning("推送额度账本损坏或不可读，已归档并按空账处理: %s", e)
        return {}


def _archive_corrupt_state_file(path):
    """把损坏的状态文件改名留证（原路径随后会被重建为正常状态文件）。

    账本与节流表共用：损坏文件不静默丢弃，归档留证便于排障。
    """
    try:
        os.replace(path, f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}")
    except OSError as e:
        logger.warning("归档损坏状态文件失败（继续按空状态处理）: %s", e)


def _save_ledger_file(data):
    """原子写磁盘账本（tmp + os.replace），失败仅告警不影响发送主流程。"""
    try:
        os.makedirs(os.path.dirname(_ledger_path()) or ".", exist_ok=True)
        tmp = _ledger_path() + ".tmp" + str(os.getpid()) + "-" + str(threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _ledger_path())
    except OSError as e:
        logger.warning("写入推送额度账本失败（额度仍按内存计数）: %s", e)


# ---------------------------------------------------------------------------
# 节流状态持久化（跨进程共享节流窗口）
# 文件布局：{"<title>": 上次放行时间戳}；与每日预算账本同款「文件锁临界区内
# 读-改-写」保证 web 与 signin 不会各自持一份节流表。损坏/缺文件按空表处理
# （最坏多放行一条同类告警，不超发），但同样归档留证 + warning，不静默。
# ---------------------------------------------------------------------------

def _load_throttle_file():
    """读取磁盘节流表（缺文件按空 dict 处理）。

    损坏/解析失败**不静默重置**：归档改名留证 + warning 后按空表处理——
    与账本损坏的处理口径一致，避免"读不了就当全新"的静默失效无从排障。
    """
    path = _throttle_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("节流表顶层不是 JSON 对象")
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError) as e:
        _archive_corrupt_state_file(path)
        logger.warning("推送节流状态损坏或不可读，已归档并按空表处理: %s", e)
        return {}


def _save_throttle_file(data):
    """原子写磁盘节流表（tmp + os.replace），失败仅告警不影响发送主流程。"""
    try:
        os.makedirs(os.path.dirname(_throttle_path()) or ".", exist_ok=True)
        tmp = _throttle_path() + ".tmp" + str(os.getpid()) + "-" + str(threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _throttle_path())
    except OSError as e:
        logger.warning("写入推送节流状态失败（节流按内存态执行）: %s", e)


def _prune_throttle_entries(data, now, cooldown):
    """清理已过期的节流条目，防磁盘文件无限增长（标题可被动态拼接成天量）。

    条目在写盘时顺带清理：早于 ``now - cooldown`` 的条目对后续判定已无意义
    （窗口内必为 ``now - ts < cooldown``）。仅删过期条目，仍生效窗口不受影响。
    """
    expire_before = now - cooldown
    for title in [t for t, ts in data.items() if ts < expire_before]:
        del data[title]


def _roll_locked_inner(ledger_id, disk, today):
    """跨日归零 + 内存对齐（调用方必须已持有该账本的 lock 与**文件锁**）。

    只对齐内存态到磁盘，**不落盘**——落盘由外层单次文件锁临界区统一完成，
    归零判定以**磁盘账本**为准（另一进程可能已把今日额度用掉），内存态先对齐
    磁盘再判断，避免双进程各自归零造成"第二份额度"。
    计数与耗尽告知标记同生同灭：换日时一起重置，才不会留下"昨日挂着的 pending
    在今日仍被 pop 出来"的错位（告知标记按日重置的既有语义）。
    """
    led = _LEDGERS[ledger_id]
    dled = disk.get(ledger_id)
    if not isinstance(dled, dict):
        dled = {}
    disk_date = str(dled.get("date", "") or "")
    if disk_date != today:
        # 磁盘无当日账本（首次/跨日）：归零内存（写回由外层统一落盘）
        led["state"]["date"] = today
        led["state"]["count"] = 0
        led["notice"].update({"pending": False, "notified": False, "warned": False})
        return
    # 磁盘已有当日账本：以磁盘为准刷新内存（本进程可能刚启动/另一进程刚扣过）
    led["state"]["date"] = disk_date
    led["state"]["count"] = int(dled.get("count", 0) or 0)
    led["notice"]["pending"] = bool(dled.get("pending", False))
    led["notice"]["notified"] = bool(dled.get("notified", False))
    led["notice"]["warned"] = bool(dled.get("warned", False))


def _ensure_ledger_structure(disk):
    """校验盘上账本结构完整（含两本账），缺失键补默认值（不覆盖已有值）。"""
    for ledger_id in _LEDGER_IDS:
        cur = disk.get(ledger_id)
        if not isinstance(cur, dict):
            cur = {}
            disk[ledger_id] = cur
        cur.setdefault("date", "")
        cur.setdefault("count", 0)
        cur.setdefault("pending", False)
        cur.setdefault("notified", False)
        cur.setdefault("warned", False)


def _merge_ledger_into_disk(disk, ledger_id, led):
    """把某本账内存态合并进磁盘 dict（调用方必须已持有文件锁）。

    仅更新本账本，其余键原样保留；写回前补齐两本账的缺失结构。本函数由
    `_with_ledger_locked`（单次锁临界区，读-改-写一致）使用，故直接覆盖本账本
    五个字段即可——锁内读到的盘值就是最新值，不存在陈旧内存回退问题。
    """
    _ensure_ledger_structure(disk)
    disk[ledger_id] = {
        "date": led["state"]["date"],
        "count": int(led["state"]["count"]),
        "pending": bool(led["notice"]["pending"]),
        "notified": bool(led["notice"]["notified"]),
        "warned": bool(led["notice"]["warned"]),
    }


def _with_ledger_locked(ledger_id, fn):
    """在**单次文件锁临界区**内完成「读盘 → 跨日对齐 → fn(led, disk) → 合并写回」。

    调用方必须已持有该账本的进程内 lock（获取顺序保持"进程内锁 → 文件锁"不变，
    避免与其它路径反向嵌套造成死锁）。磁盘是唯一事实源：读-改-写合并为一次文件锁
    持有，消除跨进程在两次文件锁之间被插入完整操作导致的丢失更新/额度超发。
    fn 在锁内执行，其返回值原样返回；fn 对 led 内存态的修改随本次写回原子落盘。
    仅当本账本实际发生变化时才写盘（跨日归零/占用/退还/取走），纯读路径零写盘。
    """
    led = _LEDGERS[ledger_id]
    with _ledger_file_lock():
        disk = _load_ledger_file()
        _roll_locked_inner(ledger_id, disk, _daily_today())
        result = fn(led, disk)
        before = disk.get(ledger_id)
        _merge_ledger_into_disk(disk, ledger_id, led)
        if before != disk.get(ledger_id):
            _save_ledger_file(disk)
        return result

# 跳过原因日志去重表：{原因: 上次记录时间}
_skip_logged = {}
_skip_log_lock = threading.Lock()


def _daily_today():
    """今日日期串（本地时区，与服务器日期一致）。"""
    return time.strftime("%Y-%m-%d")


def _ledger(ledger_id):
    """账本字典（{"state","notice","lock"}），ledger_id 取 general / urgent / login_fail。

    threading.Lock 不可重入：调用方持有返回值的 lock 时只能直接读写 state / notice
    （跨日与告知标记重置由 _with_ledger_locked 统一在文件锁内完成），不得再进入
    本模块任何取账本的函数。
    """
    return _LEDGERS[ledger_id]


def _loginfail_daily_limit(envs=None):
    """登录失败告警独立账本（M8）的每日上限；0 = 不限。

    键 YIBAN_LOGINFAIL_DAILY_MAX 无 NOTIFY_ 前缀（独立命名），读取口径与其他
    notify env 键一致：环境变量优先、回退 .env、非法/负值回退默认。
    """
    value = os.environ.get(config.LOGINFAIL_DAILY_MAX_KEY, "").strip()
    if not value:
        if envs is None:
            envs = config._read_env_file()
        value = envs.get(config.LOGINFAIL_DAILY_MAX_KEY, "").strip()
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return config.DEFAULT_LOGINFAIL_DAILY_MAX


def _daily_limit(ledger_id, envs=None):
    """该本账的每日上限（0 = 不限）。

    urgent → YIBAN_NOTIFY_URGENT_DAILY_MAX；login_fail → YIBAN_LOGINFAIL_DAILY_MAX
    （M8 独立账本）；其余（general）→ YIBAN_NOTIFY_DAILY_MAX。
    """
    if ledger_id == "urgent":
        return config._env_int("URGENT_DAILY_MAX", config.DEFAULT_URGENT_DAILY_MAX, envs)
    if ledger_id == "login_fail":
        return _loginfail_daily_limit(envs)
    return config._env_int("DAILY_MAX", config.DEFAULT_DAILY_MAX, envs)


def _daily_remaining(ledger_id, limit=None):
    """该本账今日剩余可推送条数；无上限（对应键 <=0）返回 None。

    limit 可由调用方传入已解析好的上限，避免同一次调用里重复解析 .env（见 get_config）。
    """
    if limit is None:
        limit = _daily_limit(ledger_id)
    if limit <= 0:
        return None
    led = _ledger(ledger_id)
    with led["lock"]:
        # 单次文件锁临界区：读盘 → 跨日对齐 → 读剩余，纯读不写盘
        return _with_ledger_locked(
            ledger_id, lambda led_, disk_: max(0, limit - led_["state"]["count"]))


class BudgetTicket:
    """一次额度占用的凭证：由 _consume_daily_budget 发出，发送失败时交 _refund_daily_budget。

    凭证自己带着三个事实：
      allowed —— 本次是否放行；
      ledger  —— 占了哪本账，None 表示本次没占额度（不限额 / 已被拒 / force）；
      day     —— 占用当日的日期串，退还只在同一天内生效。
    为什么不用"全局登记表 + 凭证号"：登记表要在发送成功时也保留条目才防得住重复退还，
    于是每发出一条就永久多一条残留，日积月累顶到上限后反而开始漏退；而跨日清扫登记表
    又要引入第三把锁。凭证随 send() 的局部变量生灭，没有这类状态。
    凭证一次性：take() 取用一次即作废，同一笔占用退两次在这里被挡住；它只在单次 send()
    调用内由同一线程传递，故不做加锁。
    """

    # 顺序按 ruff RUF023 要求排（自然序），与 __init__ 的形参顺序无对应关系
    __slots__ = ("_used", "allowed", "day", "ledger")

    def __init__(self, allowed, ledger, day):
        self.allowed = allowed
        self.ledger = ledger
        self.day = day
        self._used = False

    def take(self):
        """取用凭证换取退还资格；返回 (账本标识, 占用当日) 或 None（没占额度 / 已退过）。"""
        if self._used or self.ledger is None:
            return None
        self._used = True
        return self.ledger, self.day


def _consume_budget_locked(led, ledger_id, limit):
    """锁内：尝试占一条该本账当日额度（调用方必须已持有账本 lock 与文件锁）。

    返回 (allowed, charged, first_warning)；first_warning 由调用方锁外落日志
    （logger 可能触发文件 I/O，不宜持锁执行）。
    """
    state = led["state"]
    if state["count"] >= limit:
        allowed, charged = False, None
    else:
        state["count"] += 1
        allowed, charged = True, ledger_id
    first_warning = False
    # 耗尽这件事必须在"本次消耗正好打满"或"本次直接被拒"时就记账，
    # 不能等下一次尝试被拒才补标记。修复轮2：挂标记就写在这把账本锁的临界区内，
    # 与 _refund 里"判定虚警 + 撤回"共用同一把锁，两者不可能交错。
    if state["count"] >= limit:  # 被拒（没占）与恰好占满此刻都等于"当日已耗尽"
        first_warning = _mark_exhausted_locked(ledger_id)
    return allowed, charged, first_warning


def _consume_daily_budget(ledger_id):
    """尝试占一条该本账当日额度，返回 BudgetTicket。

    - ticket.allowed=False：额度已耗尽，本次不发（邮件通道不受影响），且没占额度；
    - allowed=True 且 ledger=None：该本账不限额（上限 <=0），本来就没占额度；
    - ledger 非 None：确实占了一条，发送失败时凭它退还。
    """
    today = _daily_today()
    limit = _daily_limit(ledger_id)
    if limit <= 0:
        return BudgetTicket(True, None, today)  # 0 = 不限：不占额度，也就没有可退的东西
    led = _ledger(ledger_id)
    with led["lock"]:
        # 读盘-判定/修改-写回合并为单次文件锁临界区，
        # 占用即落盘——另一进程（web 常驻 vs signin cron）读到最新计数，
        # 不再各自持一份进程内额度，也不再在两次文件锁之间留出可被插入的窗口。
        allowed, charged, first_warning = _with_ledger_locked(
            ledger_id, lambda led_, disk_: _consume_budget_locked(led_, ledger_id, limit))
    if first_warning:  # 锁外落日志：logger 可能触发文件 I/O，不持锁执行
        _log_exhaustion_warning(ledger_id)
    return BudgetTicket(allowed, charged, today)


def _refund_budget_locked(led, ledger_id, limit_now):
    """锁内：按凭证退还一条该本账当日额度（调用方必须已持有账本 lock 与文件锁）。

    退完还有富余（或该账本已被改成不限额）→ 之前的"耗尽"是虚警，就地撤回
    （判定与撤回必须写在同一个临界区里，防陈旧撤回抹掉真实 pending）。
    """
    if led["state"]["count"] <= 0:
        return
    led["state"]["count"] -= 1
    if limit_now <= 0 or led["state"]["count"] < limit_now:
        _unmark_exhausted_locked(ledger_id)


def _refund_daily_budget(ticket):
    """发送失败退还已占额度（额度只在真发出去后才算花掉）。

    只认占用时发出的凭证，不再重读上限、不再猜"当初占没占"：
    - 凭证带着占用当日，跨日（23:59:59 占用、次日才失败）直接作废——
      次日账本已被归零，再 -1 就是凭空吞掉次日一条额度；
    - 发送途中管理员把上限从 N 改成 0（不限）或反向时，靠重读 limit
      判断会出现幻影退还 / 漏退，凭证已固化"确实占了"这一事实。
    """
    taken = ticket.take() if ticket is not None else None
    if taken is None:
        return  # 没占过额度（force / 不限额 / 被拒）或已退过，没什么可退
    ledger_id, day = taken
    today = _daily_today()
    if day != today:
        return  # 跨日作废（占用当日那次归零已经把它算清了）
    limit_now = _daily_limit(ledger_id)  # 锁外解析，别持锁做文件 I/O
    led = _ledger(ledger_id)
    with led["lock"]:
        # P1-1 修复：读盘-判定/修改-写回合并为单次文件锁临界区，退还即落盘
        _with_ledger_locked(
            ledger_id, lambda led_, disk_: _refund_budget_locked(led_, ledger_id, limit_now))


def _mark_exhausted_locked(ledger_id):
    """某本账当日耗尽：挂上待取走的告知标记（调用方必须已持有该账本的 lock）。

    返回"当日是否首次 warning"，由调用方在锁外落日志：标记必须与额度计数同处一个
    临界区（修复轮2），而 logger 可能触发文件 I/O，不宜持锁执行。
    """
    notice = _LEDGERS[ledger_id]["notice"]
    # 已交付过（notified）就不再重挂：告知邮件每日每本账各一封
    if not notice["notified"]:
        notice["pending"] = True
    first_warning = not notice["warned"]
    if first_warning:
        notice["warned"] = True
    return first_warning


def _log_exhaustion_warning(ledger_id):
    """额度耗尽的那一行 warning（每天每本账只记一次，避免被反复触发的告警刷屏）。"""
    if ledger_id == "urgent":
        label, env_key = "紧急", "YIBAN_NOTIFY_URGENT_DAILY_MAX"
    elif ledger_id == "login_fail":
        label, env_key = "登录失败告警", config.LOGINFAIL_DAILY_MAX_KEY
    else:
        label, env_key = "非紧急", "YIBAN_NOTIFY_DAILY_MAX"
    logger.warning(
        "今日%s消息推送额度已用尽（%s），当日该类告警不再推手机，请查邮件",
        label, env_key,
    )


def _unmark_exhausted_locked(ledger_id):
    """额度因失败退还而回到未耗尽：撤回还没被取走的告知（调用方必须已持有该账本 lock）。

    只撤 pending：notified（调用方已取走、邮件已发出）无法撤销，warned 也不撤销，
    以免通道长期失败时"占满→退还→再占满"把 warning 刷成日志风暴。
    """
    _LEDGERS[ledger_id]["notice"]["pending"] = False


def budget_exhausted_today(urgent=None):
    """该本账今日额度是否已用尽；urgent=None 表示"任一账用尽"。无上限恒 False。"""
    if urgent is None:
        return budget_exhausted_today(True) or budget_exhausted_today(False)
    remaining = _daily_remaining("urgent" if urgent else "general")
    return remaining is not None and remaining == 0


def _pop_notice_locked(led, disk=None):
    """锁内：取走本账待告知标记并置已交付（调用方必须已持有账本 lock 与文件锁）。

    disk 由 _with_ledger_locked 传入（统一 fn(led, disk) 签名），此处无需使用。
    """
    notice = led["notice"]
    if notice["pending"]:
        notice["pending"] = False
        notice["notified"] = True
        return True
    return False


def pop_exhaustion_notice():
    """取走"当日有额度耗尽待告知"的标记，返回耗尽的账本标识列表，如 ["general", "urgent"]。

    语义是**每本账每日各一次**（不是每天总共一次）：非紧急与紧急各自挂标记，一次 pop
    把当次所有待告知的账本一并取走并置假。因此调用方应当**一次调用拿到整个列表**、
    在告知邮件里逐项写清"紧急额度已用尽 / 非紧急额度已用尽"，不要反复调用到空为止
    （那样两本账同日会发出两封信）。无待告知时返回空列表（falsy），
    `if notify.pop_exhaustion_notice():` 这类旧写法依旧成立。
    本组件不反向依赖邮件通道，只暴露状态。跨日未取走的标记不补发（按日重置）。
    """
    kinds = []
    # 逐本账各取一次：一次只持一把账本锁（绝不两把同持，也不在锁内套别的锁）。
    # 把标记改入账本锁后，"两本账同时挂标记"这一瞬间不再被一把全局锁
    # 覆盖，但每本账自己的"挂 / 撤 / 取走"仍是原子的——每本账每日各一封的语义不变。
    for ledger_id in _LEDGER_IDS:
        led = _ledger(ledger_id)
        with led["lock"]:
            # P1-1 修复：取走即落盘，且在单次文件锁临界区内完成——已交付标记
            # 跨进程一致，防双进程各发一封耗尽告知邮件。
            if _with_ledger_locked(ledger_id, _pop_notice_locked):
                kinds.append(ledger_id)
    return kinds
