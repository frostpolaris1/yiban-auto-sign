# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""账号数据族：读取、展示序列化、字段校验与账号审核态词表。

**功能**
账号与用户的读取 `load_accounts` / `load_accounts_raw` / `load_users`、出站展示序列化
`mask_account`（含邮箱脱敏 `_mask_email` 与归属展示名 `_owner_display_of`）、按手机号
定位 `find_account_index`、字段清洗与重提冲突文案 `validate_account` /
`_duplicate_phone_error` / `_owner_has_other_live`、按 idx 寻址的错位守卫 `_stale_idx_guard`、
注销冷却剩余 `_delete_grace_remaining`、口令策略 `_password_policy_error` /
`_admin_password_policy_error`、自选时间片的展示与预计时段 `_slot_to_label` / `_estimate_slot`，
以及只读验证的入参构造 `_as_signin_account` 与验证包装 `_verify_account_clean`。

**归属**
原 `web/app.py` 的模块级账号数据辅助，唯一真源在本模块；`web/app.py` 只保留名字面与
转发，把它自己持有、而本模块需要的模块级名字——签到窗口 `_sign_window`、`.env` 路径与
读取器、整数配置读取器、掐头去尾口径 `edge_config`、账号读入口 `load_accounts`——在调用
时刻现取后注入。账号审核态词表、口令策略常量、手机号正则与注销宽限期随本族搬入本模块，
`web/app.py` 再导出以免 `m.*` 名字面损失。

**复用**
`mask_account` 只经 `_mask_email` / `_owner_display_of` 出站脱敏，不各自实现一份；读取族
（`load_accounts` / `load_accounts_raw` / `load_users`）共用 `web.services.locks` 的
`_file_lock`，避免同进程内读到未提交事务的部分结果；口令策略与只读验证各只有一处判据
（`signin.verify_account`），注册、编辑与校验队列都经它取用。

**通信**
本模块不反向导入 `web.app`（本仓测试以别名加载 `app.py`，普通 import 会再执行一份副本
模块）。签到窗口、`.env` 路径与读取器、掐头去尾口径、账号读入口都作为显式参数接收：
它们在 `web.app` 上是会被测试打桩或赋值改写的模块级名字（既有测试在 `web.app` 上打桩
`_sign_window` / `edge_config` / `read_env` / `load_accounts`，又直接赋值 `ENV_FILE`），
本模块另持一份绑定会让这些改写静默失效。只读验证复用 `scripts/signin.py` 的
`signin.verify_account` 真源（登录 + 拉任务，不提交签到），不自建第二套探针。
"""

import random
import re
from datetime import datetime, timedelta

import signin  # 探针/子进程模块（scripts/ 在 sys.path 上，由 web.app 的引导保证）

from web.services.locks import _file_lock
from yiban import clock
from yiban.masking import mask_phone as _mask_phone
from yiban.store import db

# ---------------------------------------------------------------------------
# 账号审核态词表（与签到状态码 STATUS_* 同名异义，故独立命名空间）
# ---------------------------------------------------------------------------
ACCOUNT_STATUS_PENDING = "pending"  # 待审核（不参与定时签到）
ACCOUNT_STATUS_ACTIVE = "active"  # 已生效（参与定时签到）
ACCOUNT_STATUS_REJECTED = "rejected"  # 已拒绝（附理由，用户可编辑重新提交）

# 手机号格式（易班登录账号为中国 11 位手机号；恶意字符可注入前端事件与日志）
PHONE_RE = re.compile(r"^1\d{10}$")

# 注销宽限期（天）：软删除冷却期，与账号软删除保留期对齐；与 db.purge_deleted_users
# 默认一致；已注销用户视图按此计算剩余天数。原实现另有硬编码的 7，与
# db.SOFT_DELETE_RETENTION_DAYS（账号保留期唯一事实源）及 db.purge_deleted_users 默认值
# 形成三份互不相干的"7"——运维按注释去调 SOFT_DELETE_RETENTION_DAYS 时，账号会被提前
# 物理清除而恢复宽限期仍按 7 天，用户点恢复会看到"成功"实际账号已消失（静默数据丢失）。
# 现统一取同一常量（**唯一事实源**，不要再写字面量），使两处口径无法各自漂移。
DELETE_GRACE_DAYS = db.SOFT_DELETE_RETENTION_DAYS

# ---------------------------------------------------------------------------
# 口令策略（普通用户与主管理员两档）
# ---------------------------------------------------------------------------
# 密码策略：至少 10 位且包含大写/小写/数字/符号中至少两类（只对新建/修改生效，存量密码不受影响）
PASSWORD_MIN_LEN = 10
# 口令"字符类别"单一事实源：四个类别正则按文案顺序排列，与 _PASSWORD_CLASS_LABELS
# 同序同数。判定语义是"命中类别数 >= _PASSWORD_MIN_CLASSES 即通过"——符号算一类，不额外要求含符号。
# 前端各页各内联一份同名同序的 PW_CLASS_PATTERNS（不跨文件共享脚本），由元测试从模板源码
# 提取后与本元组逐字比对，任一侧漂移即红。
_PASSWORD_CLASS_PATTERNS = (r"[A-Z]", r"[a-z]", r"\d", r"[^A-Za-z0-9]")
_PASSWORD_CLASS_LABELS = ("大写字母", "小写字母", "数字", "符号")
# 类别下限：文案里的中文"两"须与本常量一致（元测试同时钉住数值与措辞，防只改一处）
_PASSWORD_MIN_CLASSES = 2
# 统一口径文案（前后端同句）：旧写法一处把下限写成易被读成"三类起"的中文比较词、另一处
# 简写得像"数量恰好等于下限"。此后统一用"…中的至少两类"这一无歧义说法。
# 大小写合并显示（文案精简）；判定仍按上方四类（大写/小写/数字/符号各自独立），
# 故 _PASSWORD_CLASS_LABELS 保持四元组——管理员"至少三类"消息必须完整列举四类。
_PASSWORD_CLASS_HINT = "大小写字母、数字、符号中的至少两类"
_PASSWORD_POLICY_HINT = f"至少 {PASSWORD_MIN_LEN} 位，且包含{_PASSWORD_CLASS_HINT}"
# 主管理员（内置 .env 管理员）口令单独提档：至少 12 位且命中至少三类，与启动期的
# reject_default_admin_password 同一策略（普通用户维持原口径）。
ADMIN_PASSWORD_MIN_LEN = 12
ADMIN_PASSWORD_MIN_CLASSES = 3


# ---------------------------------------------------------------------------
# 账号 / 用户读取
# ---------------------------------------------------------------------------
def load_accounts():
    """全部账号（SQLite，password/phone_code 已解密为明文，按 sort_order 升序）。

    `_file_lock` 只护住「取快照」这一步：解密是 CPU 密集且不碰连接，放到锁外——
    否则 8 个 web 线程的账号读写仍会被解密串行化。
    `_file_lock` 与写操作同锁，避免读到同一连接上未提交事务的部分结果
    （RLock 可重入，写操作内调用无死锁）。
    """
    def _snap():
        with _file_lock:
            return db.accounts_snapshot()

    return db.read_accounts(_snap)


def load_accounts_raw():
    """全部账号原始行（不解密 password/phone_code），供仅需明文列的统计/归类路径。

    只 SELECT + 组行，无 AES-GCM 解密、无明文自愈回写：计数、取 owner 集合、
    容量三分类等只用得到 phone（本就是明文列，兼作 AAD）/status/user_paused/
    deleted/owner，调用方拿不到也无需明文凭据。
    _file_lock 与 load_accounts 同锁：同连接上未提交事务的部分结果不可见。
    """
    with _file_lock:
        return db.accounts_snapshot()


def load_users():
    """全部用户（SQLite）。"""
    with _file_lock:
        return db.load_users()


# ---------------------------------------------------------------------------
# 展示序列化
# ---------------------------------------------------------------------------
def _mask_email(e):
    """日志/列表脱敏：邮箱 → abc***@example.com（保留域名）；已脱敏或非邮箱原样返回（幂等）。"""
    e = str(e)
    if "*" in e:
        return e
    i = e.find("@")
    if i <= 0:
        return e
    return e[: min(3, i)] + "***" + e[i:]


def _owner_display_of(owner_email):
    """把账号归属邮箱映射为展示名（后台归属列用）：普通用户显示邮箱前缀（@ 前）。"""
    if owner_email in ("admin", ""):
        return "管理员"
    return owner_email.split("@")[0] if "@" in owner_email else owner_email


def mask_account(acc, index, masked=True):
    """账号展示序列化（列表默认脱敏手机号/归属邮箱，网络层不泄露完整 PII）。

    masked=False 时返回完整信息（仅详情接口使用，按需取完整号用于编辑/签到等操作）。
    密码始终不下发（has_password 布尔）；设备识别码始终不下发（has_phone_code 布尔）。
    """
    phone = acc.get("phone", "")
    owner = acc.get("owner", "admin")
    return {
        "index": index,
        "name": acc.get("name", ""),
        "phone": _mask_phone(phone) if masked else phone,
        "phone_model": acc.get("phone_model", ""),
        "has_password": bool(acc.get("password")),
        "has_phone_code": bool(acc.get("phone_code")),
        "display_name": acc.get("name") or f"账号{index + 1}",
        # 普通用户体系：owner=提交者邮箱（'admin'=管理员添加），status=待审核/已生效
        "owner": _mask_email(owner) if masked else owner,
        "owner_display": _owner_display_of(owner),
        "status": acc.get("status", ACCOUNT_STATUS_ACTIVE),
        "reject_reason": acc.get("reject_reason", ""),
        "user_paused": bool(acc.get("user_paused", False)),  # 用户自暂停（调度 v2）
        # 软删除：管理员删除后进入待删除状态（保留期内可恢复）
        "deleted": bool(acc.get("deleted")),
        "deleted_at": acc.get("deleted_at", ""),
    }


# ---------------------------------------------------------------------------
# 定位与字段校验
# ---------------------------------------------------------------------------
def find_account_index(accounts, phone):
    """按手机号查账号下标（手动签到用）。"""
    for i, acc in enumerate(accounts):
        if acc.get("phone") == phone:
            return i
    return None


def _duplicate_phone_error(accounts, phone, email):
    """重提手机号冲突的差异化文案。

    冲突行是本人刚删除的账号（deleted_by=本人）时给撤销指引；其余统一口径，
    不向调用方泄露该手机号的归属信息。返回 None 表示非本人待删除行冲突。
    """
    conflict = next((a for a in accounts if a.get("phone") == phone), None)
    if (
        conflict is not None
        and conflict.get("owner") == email
        and conflict.get("deleted")
        and conflict.get("deleted_by", "") == email
    ):
        return (f"该手机号对应你刚删除的账号，可先撤销删除；"
                f"或等 {DELETE_GRACE_DAYS} 天自动清除后再提交")
    return None


def _owner_has_other_live(accounts, acc):
    """归属用户（非 admin）名下是否已有其他未删除账号（每人限 1 个，恢复/添加时校验）。"""
    owner = acc.get("owner", "admin")
    if not owner or owner == "admin":
        return False
    return any(
        a.get("owner") == owner and not a.get("deleted") and a is not acc for a in accounts
    )


def validate_account(data, require_password):
    """校验账号字段。require_password=True 时密码必填；返回 (错误信息 or None, 清洗后的账号 dict)。"""
    name = str(data.get("name", "")).strip()
    if len(name) > 50:
        return "名称过长（最多 50 字）", None
    phone = str(data.get("phone", "")).strip()
    password = str(data.get("password", "")).strip()
    if not phone:
        return "手机号为必填项", None
    if not PHONE_RE.match(phone):
        return "手机号格式不正确（应为 1 开头的 11 位数字）", None
    if require_password and not password:
        return "密码为必填项", None
    phone_model = str(data.get("phone_model", "")).strip()
    if len(phone_model) > 50:
        return "设备型号过长（最多 50 字）", None
    phone_code = str(data.get("phone_code", "")).strip()
    if len(phone_code) > 128:
        return "设备识别码过长", None
    return None, {
        "name": name,
        "phone": phone,
        "password": password,
        "phone_model": phone_model,
        "phone_code": phone_code,
    }


def _stale_idx_guard(acc, data, *, fail_closed=False):
    """防错位校验：mutation 按 idx 寻址时，客户端携带的 phone 与服务端 idx 解析结果
    不一致 → 账号列表在视图快照后已漂移（并发删除/移动等），放行会静默操作错误对象。
    返回 True 表示错位，调用方应返回 409 引导刷新。未携带 phone 的请求（旧客户端/
    测试）默认保持兼容不校验。

    比对前双侧 _mask_phone 归一：/api/accounts 出站即脱敏（mask_account），
    浏览器回传的是 138****8000 形态；_mask_phone 幂等（含 * 原样返回），直连
    API 发全号的旧客户端/测试同样归一可比；伪造他人号码仍因不等被拦。

    `fail_closed=True` 给"改写凭据"这类写路径：拿不出任何可核对的标识就等于
    没人证明 idx 仍指向视图里那一行。放行的代价（静默改掉别人的易班凭据、还回
    200）远大于拒绝的代价（调用方刷新一次页面），故此时按错位处理。
    """
    phone = data.get("phone") if isinstance(data, dict) else None
    if phone is None:
        return fail_closed
    return _mask_phone(str(phone).strip()) != _mask_phone(str(acc.get("phone", "")))


def _delete_grace_remaining(deleted_at):
    """注销冷却剩余秒数：deleted_at + 宽限期 − now；非冷却中（无时间/已过期/解析失败）返回 0。

    登录即恢复与已注销用户视图共用此判定。
    兼容两种格式：主格式 strftime("%Y-%m-%d %H:%M:%S")（新写入），存量 ISO 格式自动回退。
    """
    if not deleted_at:
        return 0
    try:
        d = datetime.strptime(deleted_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            d = datetime.fromisoformat(str(deleted_at))
        except (ValueError, TypeError):
            return 0
    remain = (d + timedelta(days=DELETE_GRACE_DAYS)) - clock.now()
    return remain.total_seconds() if remain.total_seconds() > 0 else 0


# ---------------------------------------------------------------------------
# 口令策略
# ---------------------------------------------------------------------------
def _password_policy_error(password):
    """校验密码强度：至少 PASSWORD_MIN_LEN 位且命中 _PASSWORD_CLASS_PATTERNS 中
    _PASSWORD_MIN_CLASSES 个类别（符号算一类）。返回错误信息 or None。

    类别正则与文案都取自模块级常量（本函数不再内联字面量）：前端各页各有一份同序的
    PW_CLASS_PATTERNS 与 PW_POLICY_HINT，由元测试逐字比对防漂移。
    """
    if len(password) < PASSWORD_MIN_LEN:
        return f"密码{_PASSWORD_POLICY_HINT}"
    classes = sum(bool(re.search(pat, password)) for pat in _PASSWORD_CLASS_PATTERNS)
    if classes < _PASSWORD_MIN_CLASSES:
        return f"密码需包含{_PASSWORD_CLASS_HINT}"
    return None


def _admin_password_policy_error(password):
    """主管理员（内置 .env 管理员）口令策略：至少 12 位且命中至少三类。

    主管理员口令单独提档（普通用户维持原口径不变），与 reject_default_admin_password
    的启动 fail-closed 同一策略。
    """
    if len(password) < ADMIN_PASSWORD_MIN_LEN:
        return f"至少 {ADMIN_PASSWORD_MIN_LEN} 位，且包含大写字母、小写字母、数字、符号中的至少三类"
    classes = sum(bool(re.search(pat, password)) for pat in _PASSWORD_CLASS_PATTERNS)
    if classes < ADMIN_PASSWORD_MIN_CLASSES:
        return f"需包含{'、'.join(_PASSWORD_CLASS_LABELS)}中的至少三类"
    return None


# ---------------------------------------------------------------------------
# 自选时间片展示与预计时段
# ---------------------------------------------------------------------------
def _slot_to_label(slot_min, sign_window):
    """自选片相对窗口起点的分钟偏移 → "HH:MM"（偏移 0 = 窗口起点，与调度侧同号）。

    窗口解析器由调用方传入（`web.app` 的 `_sign_window`）：它是会被测试打桩的模块级名字。
    """
    if slot_min is None:
        return None
    sw = sign_window()
    base = sw[0][0] * 60 + sw[0][1]
    m = base + int(slot_min)
    return f"{m // 60:02d}:{m % 60:02d}"


def _estimate_slot(phone, load_accounts, read_env, env_file, load_env_int, sign_window,
                   edge_config):
    """预计签到时段（调度 v2）：
    顺序排序 = 可预期（线性填块区间 / 锚点中心 / 小人数确定性等分）；
    随机排序 = 每天重排，返回 None + 提示文案。
    返回 (estimated_str|None, note_str)。

    账号读入口、`.env` 路径与读取器、整数配置读取器、窗口解析器、掐头去尾口径都由
    调用方传入（`web.app` 的同名模块级名字）：它们会被测试打桩或赋值改写，
    本模块另持绑定会让这些改写静默失效。
    """
    env = read_env(env_file)
    mode = env.get("YIBAN_SIGN_MODE", "").strip().lower()
    order = env.get("YIBAN_SIGN_ORDER", "").strip().lower() or (
        "random" if mode == "random" else "sequence")
    dist = env.get("YIBAN_SIGN_DIST", "").strip().lower() or (
        "normal" if mode == "normal" else "uniform")
    if order != "sequence":
        return None, "随机模式每日重排，签到时间当天 06:31 后可见"
    accounts = load_accounts()
    # 与 build_schedule 一致：user_paused 账号不参与调度（零占位），预计时段按实际参与人计算
    live = [a for a in accounts if not a.get("user_paused")]
    idx = next((i for i, a in enumerate(live) if a.get("phone") == phone), None)
    if idx is None or not live:
        return None, ""
    sw = sign_window()
    front_min, back_min = edge_config()[0] / 60.0, edge_config()[1] / 60.0
    start_min = sw[0][0] * 60 + sw[0][1]
    end_min = sw[1][0] * 60 + sw[1][1]
    eff_lo = start_min + front_min
    eff_hi = end_min - back_min
    span = eff_hi - eff_lo

    def fmt(m):
        m = int(m)
        return f"{m // 60:02d}:{m % 60:02d}"

    if dist == "uniform":
        # 线性填块（与 signin._schedule_blocks 同口径：块从窗口起点步进 5、裁到有效窗口、
        # 被缓冲吃掉的无效块跳过；压缩模式等极端场景按末块估算）
        k = load_env_int(env_file, "YIBAN_BLOCK_CAP", 15)
        valid = []
        b = start_min
        while b < end_min:
            lo = max(b, eff_lo)
            hi = min(b + 5, eff_hi)
            if hi > lo:
                valid.append((lo, hi))
            b += 5
        if not valid:
            return None, ""
        # k<=0（`.env` 显式写 YIBAN_BLOCK_CAP=0）= 不限容量，与 my.py 拥挤度口径
        # （`cap > 0` 才算百分比与满员提示）一致：无块内人数上限，全员落在首块，
        # 不能拿 k 当除数（否则用户端自选片接口整个 500）。引擎侧配置校验把 0 判非法
        # 并回退默认块容量，故这里只是展示/估算口径，不改真实排片。
        bi = 0 if k <= 0 else min(idx // k, len(valid) - 1)
        lo, hi = valid[bi]
        return f"{fmt(lo)}~{fmt(hi)}", "（每日固定时段，块内时刻每天略有抖动）"
    # 顺序 × 正态：锚点 z 固定 → 预期中心（μ 中值 50%、σ 中值 20%）
    z = random.Random(str(phone)).gauss(0, 1)
    center = max(eff_lo, min(eff_hi, eff_lo + span * 0.5 + span * 0.20 * z))
    return f"约 {fmt(center)}", "（每日波动约 ±10 分钟）"


# ---------------------------------------------------------------------------
# 只读验证（登录 + 拉任务，不提交签到）
# ---------------------------------------------------------------------------
def _as_signin_account(fields):
    """账号字段 dict → `signin.Account`（只读探针 `verify_account` 收的是账号对象）。

    `id` 是库内账号行 id，会一路带到运行期复核（`account_still_signable`）：库里来的
    行必须带上，否则探针会跳过"账号已被删除/停用"的复核。注册时的待入库账号没有 id，
    取 0（该复核对无 id 的账号恒按有效处理）。
    """
    return signin.Account(
        phone=fields.get("phone", ""),
        password=fields.get("password", ""),
        phone_model=fields.get("phone_model", ""),
        phone_code=fields.get("phone_code", ""),
        account_id=int(fields.get("id") or 0),
    )


def _verify_account_clean(clean):
    """对清洗后的账号字段做只读验证（复用 signin.verify_account：登录+拉任务，不提交签到）。

    返回错误信息 or None（验证通过）。message 来自 signin（已脱敏），此处再转义换行防注入。
    """
    try:
        ok_v, msg_v = signin.verify_account(_as_signin_account(clean))
    except Exception as e:
        return f"账号验证异常：{str(e).replace(chr(10), ' ').replace(chr(13), ' ')}"
    if not ok_v:
        return f"账号验证未通过：{str(msg_v).replace(chr(10), ' ').replace(chr(13), ' ')}"
    return None
