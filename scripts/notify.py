# -*- coding: utf-8 -*-
"""Webhook 推送组件（Server酱 / 自定义 URL）。

配置与仓库代码解耦（存 .env，gitignored；密钥字段 AES-GCM 加密）：
- YIBAN_NOTIFY_TYPE            : serverchan / custom / 空 = 不启用
- YIBAN_NOTIFY_SECRET_ENC      : 加密后的密文 JSON（serverchan=SendKey；custom=URL）
- YIBAN_NOTIFY_COOLDOWN        : 同类型告警节流秒数（默认 60，0=关闭）
- YIBAN_NOTIFY_URGENT_ONLY     : 1 = 仅推送重要（urgent）告警；0/空 = 全部推送
- YIBAN_NOTIFY_DAILY_MAX       : 每日推送条数上限，语义收窄为「非紧急（general）账」
                                  （0=不限；默认 5，匹配 Server酱 免费版 5 条/天；
                                  超限后当日不再推送，邮件通道不受影响）
- YIBAN_NOTIFY_URGENT_DAILY_MAX: 「紧急（urgent）账」每日上限（0=不限；默认 3）

兼容旧配置：未配置加密密文时回退明文 YIBAN_NOTIFY_URL（custom 语义），
旧部署迁移后无需手动改 .env。

防滥用（2026-08-29 被盗号滥用面加固）：
- 同类型告警节流：窗口内同标题只推一条（防盗号/异常反复触发刷爆 Server酱等
  第三方配额与管理员手机）；
- 每日预算硬上限（批次14 P2-1 拆两本账）：非紧急推满 YIBAN_NOTIFY_DAILY_MAX、
  紧急推满 YIBAN_NOTIFY_URGENT_DAILY_MAX 后各自停手（邮件仍全量送达）。旧口径
  两类共用一份额度，未认证攻击者用「登录失败告警」（urgent）约 5 分钟即可烧掉
  当日全部额度，之后审计链异常等真告警在手机端全灭；
- 失败退还：额度在发送成功后才最终扣减，HTTP 异常 / 服务端非零 code / 白名单拒发
  一律退回，不浪费真告警的额度。退还按"占用时发出的凭证"执行——凭证带着账本标识与
  占用当日，因此跨日（23:59:59 占用、次日才失败）不会退到次日账上，管理员中途改
  上限也不会出现幻影退还 / 漏退；
- 耗尽可见：某本账耗尽时在**消耗动作内部当场**记 warning 并挂一次性告知标记
  （pop_exhaustion_notice，返回哪些账本耗尽；紧急 / 非紧急每日各一次），不等下一次
  尝试被拒才补——否则"最后一条恰好打满、当日再无新告警"时告知永远发不出去，那正是
  本档要治的静默失效。节流命中 / 额度耗尽 / 非紧急被过滤这三类静默跳过各有一行 info
  日志（同原因在窗口内只记一次），运维能区分"配置没生效"与"本来就没额度"；
- 仅重要告警：YIBAN_NOTIFY_URGENT_ONLY 开启后，非紧急（如用户日常改密、签到
  结果类）通知不推手机，把预算留给真正威胁系统/账号安全的事件；
- 检查服务端响应：Server酱 code!=0 / 非 JSON 一律记日志（配额耗尽、限频可见），
  不再"发出即成功"地静默失败；
- 自定义 URL 走 SSRF 白名单（https + 非回环/内网），与 web/signin 既有口径一致。

设计原则：不配置 = 不启用；发送异常只记日志、绝不抛出（不拖累签到主流程）。
"""

import ipaddress
import json
import logging
import os
import threading
import time
from contextlib import contextmanager, nullcontext
from urllib.parse import urlparse

try:
    import fcntl
except ImportError:  # Windows 无 fcntl，跨进程锁退化为进程内
    fcntl = None

import requests

try:
    from . import account_crypto
except ImportError:  # 非包上下文（scripts/ 直接 import）
    import account_crypto

logger = logging.getLogger("notify")

_PREFIX = "YIBAN_NOTIFY_"
SERVERCHAN_TURBO_HOST = "sctapi.ftqq.com"
DEFAULT_COOLDOWN = 60
DEFAULT_DAILY_MAX = 5
# 批次14 P2-1：紧急告警另开一本独立额度，保证噪声烧完非紧急额度后仍有手机通道
DEFAULT_URGENT_DAILY_MAX = 3
DEFAULT_URL_TIMEOUT = 10
MAX_TITLE_CHARS = 32
# 跳过原因日志的去重窗口（秒）：同一原因窗口内只记一行，避免被刷爆日志
SKIP_LOG_WINDOW = 60

_throttle_ts = {}
_throttle_lock = threading.Lock()

# 每日推送预算（进程内计数，重启清零；Server酱免费 5 条/天由服务端自身响应兜底，
# 本计数用于提前感知并停止，避免徒劳请求）。批次14 P2-1：拆成非紧急 / 紧急两本账，
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
# 为什么 notice 与 count 共用同一把账本锁（批次14 修复轮2）：判定"是否虚警"要读 count、
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
_LEDGER_IDS = ("general", "urgent")
_LEDGERS = {"general": _general_daily, "urgent": _urgent_daily}

# 批次15 P2-3：每日预算从「进程内内存」升级为「磁盘账本」——原实现 web（常驻）与
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


def _ledger_path():
    """账本文件路径：$YIBAN_STATE_DIR/notify-ledger.json。

    目录解析与 signin 的状态目录口径一致（YIBAN_STATE_DIR 环境变量优先，
    回退 .env 同键，最后回落 .env 所在目录）——web（常驻，经 .env 读）与
    signin（cron，经 run.sh 注入环境变量）两侧必须指向同一文件，否则
    双进程共享额度的目标落空。
    """
    state_dir = os.environ.get("YIBAN_STATE_DIR", "").strip()
    if not state_dir:
        state_dir = _read_env_file().get("YIBAN_STATE_DIR", "").strip()
    if not state_dir:
        state_dir = os.path.dirname(_env_path()) or "."
    return os.path.join(state_dir, "notify-ledger.json")


@contextmanager
def _ledger_file_lock():
    """账本文件锁：POSIX 用 fcntl.flock 跨进程互斥；Windows 退化为无操作。

    锁文件单独使用 ``<path>.lock``（与 signin._state_file_lock 同款约定），
    不与账本文件本身的读写句柄混用。
    """
    if fcntl is None:
        with nullcontext():
            yield
        return
    lock_path = _ledger_path() + ".lock"
    lock_dir = os.path.dirname(lock_path) or "."
    try:
        os.makedirs(lock_dir, exist_ok=True)
    except OSError:
        pass
    with open(lock_path, "a+", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


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
        _archive_corrupt_ledger(path)
        logger.warning("推送额度账本损坏或不可读，已归档并按空账处理: %s", e)
        return {}


def _archive_corrupt_ledger(path):
    """把损坏的账本文件改名留证（原路径随后会被重建为正常账本）。"""
    try:
        os.replace(path, f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}")
    except OSError as e:
        logger.warning("归档损坏账本文件失败（继续按空账处理）: %s", e)


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


def _sync_ledger_to_disk(ledger_id):
    """把某本账内存态**合并**写回磁盘（调用方已持有账本 lock；内部拿文件锁）。

    合并式：读盘上现存数据 → 仅更新本账本；pending/notified/warned 用"或"语义
    （本进程为 True 时写 True，为 False 时不覆盖盘上已有的 True）——防止本进程
    内存态陈旧时整块覆盖，把另一进程已写下的"已交付告知/已记警告"标记回退掉
    （告知邮件重复或漏发的根因）。count/date 是数值与日期，直接覆盖即可。
    """
    led = _LEDGERS[ledger_id]
    with _ledger_file_lock():
        disk = _load_ledger_file()
        _ensure_ledger_structure(disk)
        cur = disk.get(ledger_id, {})
        disk[ledger_id] = {
            "date": led["state"]["date"],
            "count": int(led["state"]["count"]),
            "pending": bool(led["notice"]["pending"]) or bool(cur.get("pending", False)),
            "notified": bool(led["notice"]["notified"]) or bool(cur.get("notified", False)),
            "warned": bool(led["notice"]["warned"]) or bool(cur.get("warned", False)),
        }
        _save_ledger_file(disk)


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


def _env_path():
    """本模块解析 .env 的唯一口径：YIBAN_ENV_FILE 优先（去空白），否则当前目录 .env。"""
    return os.environ.get("YIBAN_ENV_FILE", "").strip() or ".env"


def _read_env_file():
    """读取 .env（utf-8-sig 兼容 BOM），供读配置用。"""
    path = _env_path()
    result = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    result[key.strip()] = value.strip()
    except OSError:
        pass
    return result


def _env_str(key, envs=None):
    """环境变量优先，回退 .env（与 web/signin 惯例一致）。

    envs：调用方本轮已解析好的 .env 快照（_read_env_file() 的返回值）。不传则本函数
    自己读文件——一次调用读一遍全文件，get_config 里 6 个键就是 6 次磁盘 + 6 次解析
    （批次14 修复轮⑤），故允许把同一轮的解析结果传进来复用。
    """
    value = os.environ.get(_PREFIX + key, "").strip()
    if value:
        return value
    if envs is None:
        envs = _read_env_file()
    return envs.get(_PREFIX + key, "").strip()


def _env_int(key, default, envs=None):
    try:
        return max(0, int(_env_str(key, envs)))
    except (TypeError, ValueError):
        return default


def _mask_secret(secret):
    """密钥打码：保留前 3 位，其余星号。空返回空。"""
    if not secret:
        return ""
    if len(secret) <= 6:
        return secret[:2] + "**"
    return secret[:3] + "*" * max(4, len(secret) - 3)


def _host_of(url):
    """脱敏 URL 描述：仅 scheme://host[:port]，不含 userinfo/路径/查询（token 不外泄）。

    与 signin._notify_url_desc 同口径，供日志使用。
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        if parts.hostname:
            desc = f"{parts.scheme}://{parts.hostname}"
            if parts.port:
                desc += f":{parts.port}"
            return desc
    except ValueError:
        pass
    return "<无法解析>"


def is_safe_url(url):
    """自定义通知地址 SSRF 白名单：https + 非回环/内网/链路本地/未指定。

    与 web/app.py _is_safe_notify_url、signin.py send_notification 同口径，
    防 http 明文外泄与 SSRF 跳板。域名目标放行（DNS rebinding 由超时兜底）。
    """
    try:
        o = urlparse(url)
    except ValueError:
        return False
    if o.scheme != "https" or not o.hostname:
        return False
    host = o.hostname.strip().lower()
    if host == "localhost":
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # 域名：非 IP 字面量，放行
    return not (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified)


# ---------------------------------------------------------------------------
# 配置读取（加密存储）
# ---------------------------------------------------------------------------

def get_secret(envs=None):
    """返回当前加密配置解出的明文密钥（serverchan=SendKey；custom=URL）。

    未配置 / 密文损坏 / 密钥不匹配均返回空（并记日志），绝不抛异常。
    envs：调用方本轮已解析的 .env 快照，省略则自行读取（见 _env_str）。
    """
    enc = _env_str("SECRET_ENC", envs)
    if not enc:
        return _env_str("URL", envs) or ""  # 兼容旧明文 YIBAN_NOTIFY_URL
    try:
        entry = json.loads(enc)
    except ValueError:
        logger.warning("YIBAN_NOTIFY_SECRET_ENC 解析失败，消息推送不可用")
        return ""
    try:
        # 批次14：必须显式传路径。load_key() 不带参数会回落到 cwd/.env，而本模块
        # 的密文是按 YIBAN_ENV_FILE 读的——容器里 cwd=/app、真实配置在 /data/.env，
        # 于是"cwd 下没有 .env"→ 就地生成一把游离新密钥并写盘（P2-5 同源），
        # 结果是用错钥解密 → 推送通道静默死亡，还额外在镜像工作目录留下密钥文件。
        return account_crypto.decrypt_text(entry, account_crypto.load_key(_env_path()))
    except (ValueError, OSError) as e:
        # OSError：密钥文件存在但读不到（权限/占用），按"解不出"处理而非炸主流程
        logger.warning("消息推送密钥解密失败: %s", e)
        return ""


def get_config():
    """配置概览（脱敏），供设置页/日志展示。"""
    # 本轮所有键共用一份 .env 解析结果：下面 6 个字段各读一遍文件是 6 次磁盘 + 6 次
    # 全文件解析（批次14 修复轮⑤），设置页轮询时这笔开销并不便宜
    envs = _read_env_file()
    ntype = _env_str("TYPE", envs).strip().lower()
    secret = get_secret(envs)
    if not ntype and secret:
        ntype = "custom"  # 兼容旧明文 YIBAN_NOTIFY_URL
    enabled = bool(ntype and secret)
    # 上限各解析一次，紧接着复用给 daily_max / daily_remaining（原两者各自再读一遍文件）
    general_max = _daily_limit("general", envs)
    urgent_max = _daily_limit("urgent", envs)
    return {
        "ok": True,
        "enabled": enabled,
        "type": ntype if enabled else "",
        "secret_masked": _mask_secret(secret) if enabled else "",
        "configured": bool(ntype or secret),
        "cooldown": _env_int("COOLDOWN", DEFAULT_COOLDOWN, envs),
        "urgent_only": bool(_env_int("URGENT_ONLY", 0, envs)),
        # 批次14 P2-1：daily_* 两字段语义收窄为「非紧急账」（字段名不变，前端与既有
        # 调用方无需改），紧急账并列暴露为 urgent_daily_*
        "daily_max": general_max,
        "daily_remaining": _daily_remaining("general", general_max),
        "urgent_daily_max": urgent_max,
        "urgent_daily_remaining": _daily_remaining("urgent", urgent_max),
    }


# ---------------------------------------------------------------------------
# 节流与发送
# ---------------------------------------------------------------------------

def _throttle_due(title):
    """同类型告警节流：窗口内已发过返回 False（本次跳过）。0 = 关闭。"""
    cooldown = _env_int("COOLDOWN", DEFAULT_COOLDOWN)
    if cooldown <= 0:
        return True
    now = time.time()
    with _throttle_lock:
        last = _throttle_ts.get(title, 0.0)
        if now - last < cooldown:
            return False
        _throttle_ts[title] = now
        return True


def _daily_today():
    """今日日期串（本地时区，与服务器日期一致）。"""
    return time.strftime("%Y-%m-%d")


def _ledger(ledger_id):
    """账本字典（{"state","notice","lock"}），ledger_id 取 general / urgent。

    threading.Lock 不可重入：调用方持有返回值的 lock 时只能直接读写 state / notice
    （跨日与告知标记重置由 _with_ledger_locked 统一在文件锁内完成），不得再进入
    本模块任何取账本的函数。
    """
    return _LEDGERS[ledger_id]


def _daily_limit(ledger_id, envs=None):
    """该本账的每日上限（0 = 不限）；紧急账用 URGENT_DAILY_MAX，非紧急用 DAILY_MAX。"""
    if ledger_id == "urgent":
        return _env_int("URGENT_DAILY_MAX", DEFAULT_URGENT_DAILY_MAX, envs)
    return _env_int("DAILY_MAX", DEFAULT_DAILY_MAX, envs)


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

    凭证自己带着三个事实（批次14 修复轮②③）：
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
    # 批次14 修复轮①：耗尽这件事必须在"本次消耗正好打满"或"本次直接被拒"时就记账，
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
        # 批次15 P2-3 → P1-1 修复：读盘-判定/修改-写回合并为单次文件锁临界区，
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
    （批次14 修复轮2：判定与撤回必须写在同一个临界区里，防陈旧撤回抹掉真实 pending）。
    """
    if led["state"]["count"] <= 0:
        return
    led["state"]["count"] -= 1
    if limit_now <= 0 or led["state"]["count"] < limit_now:
        _unmark_exhausted_locked(ledger_id)


def _refund_daily_budget(ticket):
    """发送失败退还已占额度（批次14 P2-1：额度只在真发出去后才算花掉）。

    只认占用时发出的凭证，不再重读上限、不再猜"当初占没占"：
    - 批次14 修复轮②：凭证带着占用当日，跨日（23:59:59 占用、次日才失败）直接作废——
      次日账本已被归零，再 -1 就是凭空吞掉次日一条额度；
    - 批次14 修复轮③：发送途中管理员把上限从 N 改成 0（不限）或反向时，靠重读 limit
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
    logger.warning(
        "今日%s消息推送额度已用尽（YIBAN_NOTIFY_%s），当日同类告警不再推手机，请查邮件",
        "紧急" if ledger_id == "urgent" else "非紧急",
        "URGENT_DAILY_MAX" if ledger_id == "urgent" else "DAILY_MAX",
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
    # 批次14 修复轮2 把标记改入账本锁后，"两本账同时挂标记"这一瞬间不再被一把全局锁
    # 覆盖，但每本账自己的"挂 / 撤 / 取走"仍是原子的——每本账每日各一封的语义不变。
    for ledger_id in _LEDGER_IDS:
        led = _ledger(ledger_id)
        with led["lock"]:
            # P1-1 修复：取走即落盘，且在单次文件锁临界区内完成——已交付标记
            # 跨进程一致，防双进程各发一封耗尽告知邮件。
            if _with_ledger_locked(ledger_id, _pop_notice_locked):
                kinds.append(ledger_id)
    return kinds


def _log_skip(reason, msg, *args):
    """跳过发送的告知日志：同一原因在一个窗口内只记一行（可运维定位，不刷屏）。"""
    now = time.time()
    with _skip_log_lock:
        last = _skip_logged.get(reason, 0.0)
        if now - last < SKIP_LOG_WINDOW:
            return
        _skip_logged[reason] = now
    logger.info(msg, *args)


def _send_serverchan(sendkey, title, content):
    """Server酱 Turbo：POST https://sctapi.ftqq.com/{key}.send，title+desp。

    title 必填、最长 32 字符、不含换行；desp 为 Markdown 正文。成功返回 code==0。
    """
    url = f"https://{SERVERCHAN_TURBO_HOST}/{sendkey}.send"
    t = title.replace("\r", " ").replace("\n", " ").strip()
    if len(t) > MAX_TITLE_CHARS:
        t = t[:MAX_TITLE_CHARS]
    try:
        r = requests.post(
            url, data={"title": t, "desp": content or ""},
            timeout=DEFAULT_URL_TIMEOUT, allow_redirects=False,
        )
    except Exception as e:
        # 组件绝不抛异常（web/signin 调用方不兜底）；只记类型名（异常文本可能含 token）
        logger.warning("Server酱推送失败（%s）: %s", type(e).__name__, t)
        return False
    try:
        result = r.json()
    except ValueError:
        logger.warning("Server酱返回非 JSON（HTTP %s），视为失败: %s", r.status_code, t)
        return False
    if result.get("code") == 0:
        logger.info("消息推送已发送（serverchan）: %s", t)
        return True
    # 配额耗尽 / 限频 / 密钥错误等：返回非零 code，记日志可见（不重复刷）
    logger.warning(
        "Server酱推送被拒绝（code=%s, message=%s）: %s",
        result.get("code"), result.get("message", ""), t,
    )
    return False


def _send_custom(url, title, content):
    """自定义 webhook：POST JSON {title, content}（保持既有兼容格式）。"""
    try:
        r = requests.post(
            url, json={"title": title, "content": content},
            timeout=DEFAULT_URL_TIMEOUT, allow_redirects=False,
        )
    except Exception as e:
        # 组件绝不抛异常；只记类型名与脱敏 host（异常文本可能含 URL/token）
        logger.warning("通知推送失败（%s）: %s", type(e).__name__, _host_of(url))
        return False
    if r.status_code < 400:
        logger.info("消息推送已发送（custom）: %s", title)
        return True
    logger.warning("通知推送失败（状态码 %s）: %s", r.status_code, _host_of(url))
    return False


def send(title, content, force=False, urgent=False):
    """发送一条 webhook 通知（serverchan / custom）。返回是否成功发送。

    未配置 / 不启用 / 非紧急（仅重要告警开启时）/ 每日预算耗尽 / 节流命中 /
    发送失败均返回 False（静默，不拖累主流程，但会在日志留一行可定位的原因）。
    force=True 跳过节流与每日预算（供"测试推送"用）。
    urgent=True 标记重要告警：
    - YIBAN_NOTIFY_URGENT_ONLY 开启后仅此类会推送（邮件通道不受影响）；
    - 额度走紧急账（YIBAN_NOTIFY_URGENT_DAILY_MAX），与非紧急账互不挤占（批次14 P2-1）；
    - 只有真正发送成功才扣额度，失败（含 HTTP 异常、服务端非零 code、白名单拒发）凭
      占用时拿到的退还凭证退回。
    """
    # 同一逻辑段内复用一份 .env 快照：TYPE / SECRET_ENC / URGENT_ONLY 三个键共用，
    # 避免对同一文件重复解析。注意这不是"整次 send 只解析一次"——节流窗口
    # （_throttle_due）与额度上限（_consume_daily_budget / _daily_limit）仍各自按需解析，
    # 它们要读的是发送当刻的最新配置，把快照传下去反而会读到陈旧上限。
    envs = _read_env_file()
    ntype = _env_str("TYPE", envs).strip().lower()
    secret = get_secret(envs)
    if not ntype:
        if not secret:
            return False
        ntype = "custom"  # 兼容旧明文 YIBAN_NOTIFY_URL（未配 TYPE 但有 URL 时按 custom 发送）
    if not secret:
        return False
    ledger_id = "urgent" if urgent else "general"
    ticket = None  # None = 本次没占额度（force 路径），退还动作对它就是空操作
    if not force:
        if _env_int("URGENT_ONLY", 0, envs) and not urgent:
            _log_skip("urgent_only", "非紧急告警未推手机（YIBAN_NOTIFY_URGENT_ONLY=1）: %s", title)
            return False  # 仅重要告警：非紧急跳过（不消耗每日预算）
        if not _throttle_due(title):
            _log_skip("throttle", "推送节流命中（YIBAN_NOTIFY_COOLDOWN 窗口内同类已推）: %s", title)
            return False
        ticket = _consume_daily_budget(ledger_id)
        if not ticket.allowed:
            _log_skip("budget_exhausted_" + ledger_id,
                      "今日推送额度（%s 账）已用尽，本次不推手机: %s", ledger_id, title)
            return False
    if ntype == "serverchan":
        sent = _send_serverchan(secret, title, content)
    elif ntype == "custom":
        if not is_safe_url(secret):
            logger.warning("自定义通知地址未通过白名单校验，已拒发: host=%s", _host_of(secret))
            sent = False
        else:
            sent = _send_custom(secret, title, content)
    else:
        logger.warning("未知通知类型: %s", ntype)
        sent = False
    if not sent:
        _refund_daily_budget(ticket)  # 没送到就不该花额度（批次14 P2-1）
    return sent


def send_test():
    """发送一条测试消息（跳过节流，供设置页"测试推送"）。"""
    return send(
        "消息推送测试",
        "这是一条来自易班自动签到系统的测试消息，收到即表示消息推送配置正常。",
        force=True,
    )
