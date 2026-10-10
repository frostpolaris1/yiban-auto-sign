# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""账号数据族：读取、展示序列化、字段校验与账号审核态词表。

**功能**
账号与用户的读取 `load_accounts` / `load_accounts_raw` / `load_users`、出站展示序列化
`mask_account`（含邮箱脱敏 `_mask_email` 与归属展示名 `_owner_display_of`）、按手机号
定位 `find_account_index`、字段清洗与重提冲突文案 `validate_account` /
`_duplicate_phone_error` / `_owner_has_other_live`、设备识别码表单协议折算
`fold_phone_code`（`CLEAR_SENTINEL` 唯一真源）、按 idx 寻址的错位守卫 `_stale_idx_guard`、
注销冷却剩余 `_delete_grace_remaining`、口令策略 `_password_policy_error` /
`_admin_password_policy_error`、自选时间片的展示与预计时段 `_slot_to_label` / `_estimate_slot`，
以及只读验证的入参构造 `_as_signin_account` 与验证包装 `_verify_account_clean`。

**归属**
原 `web/app.py` 的模块级账号数据辅助，唯一真源在本模块；`web/app.py` 只保留名字面与
转发，把它自己持有、而本模块需要的模块级名字——有效窗口视图 `sign_window_bounds`、
`.env` 路径与读取器、账号读入口 `load_accounts`——在调用
时刻现取后注入。账号审核态词表、口令策略常量、手机号正则与注销宽限期随本族搬入本模块，
`web/app.py` 再导出以免 `m.*` 名字面损失。

**复用**
`mask_account` 只经 `_mask_email` / `_owner_display_of` 出站脱敏，不各自实现一份；读取族
（`load_accounts` / `load_accounts_raw` / `load_users`）共用 `web.services.locks` 的
`_file_lock`，避免同进程内读到未提交事务的部分结果；口令策略与只读验证各只有一处判据
（`signin.verify_account`），注册、编辑与校验队列都经它取用。

**通信**
本模块不反向导入 `web.app`（本仓测试以别名加载 `app.py`，普通 import 会再执行一份副本
模块）。有效窗口视图、`.env` 路径与读取器、账号读入口都作为显式参数接收：它们在
`web.app` 上是会被测试打桩或赋值改写的模块级名字（`_sign_window` / `edge_config` /
`read_env` / `load_accounts` 在既有测试里被打桩，`ENV_FILE` 被直接赋值；窗口打桩经
`sign_window_bounds` 现取后穿透到本模块），本模块另持一份绑定会让这些改写静默失效。
只读验证复用 `scripts/signin.py` 的 `signin.verify_account` 真源（登录 + 拉任务，
不提交签到），不自建第二套探针。
"""

import random
import re
from datetime import datetime, timedelta

import signin  # 探针/子进程模块（scripts/ 在 sys.path 上，由 web.app 的引导保证）

from web.services.locks import _file_lock
from yiban import clock
from yiban.engine import schedule as yb_schedule
from yiban.masking import mask_email, mask_email_local
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

# 账号编辑表单里"清除设备识别码"的哨兵值（收到 = 显式清空该字段）。唯一真源在本模块，
# `web/app.py` 经导入区再导出以保 m.CLEAR_SENTINEL 名字面；前端 account-form.js 内联
# 同一字面量（跨语言无法 import，靠本常量与折算函数单点在 Python 侧收敛语义）。
CLEAR_SENTINEL = "__clear__"

# 注销宽限期（天）：**必须**取 db.SOFT_DELETE_RETENTION_DAYS（账号保留期唯一事实源），
# 不要再写字面量。漂移不是假想：常量之外全仓还散着 48 处「7 天」字面量（含注释，其中
# 24 处落在 `web/templates/` 与 `web/static/js/`，另有 1 处是不相干的「每 7 天」排期选项；
# 复点：`grep -ro "7 天\|7天" web yiban --include=*.py --include=*.js --include=*.html`），
# 保留期一改，账号会被提前物理清除，而页面/邮件/接口提示仍按旧天数承诺——用户点
# "恢复"会看到成功、实际账号已经没了（静默数据丢失）。把这些处收敛到同一来源归 M3。
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
# 统一口径文案（前后端同句）：数量下限一律写成"…中的至少 + 本常量值 + 类别"那种形式。
# 少了限定词就会被读成恰好值，换用别的比较词又会被读成再多一档；这两种歧义写法由
# tests/test_rekey_key_source.py::PasswordPolicyParityB14Test 按字面量拦——它连注释一起扫，
# 所以此处只描述规则，不复述被禁写法。
# 大小写在文案里合并（精简），判定仍按四类各自计数，所以 _PASSWORD_CLASS_LABELS 保持
# 四元组：主管理员"至少三类"的消息必须把四类列全。
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

    `_file_lock` 与写操作同锁（RLock 可重入，写操作内调用无死锁），但只护住
    「取快照」这一步：解密是 CPU 密集且不碰连接，放到锁外——否则多路 web 线程的账号
    读写会被解密串行化。
    """
    def _snap():
        with _file_lock:
            return db.accounts_snapshot()

    return db.read_accounts(_snap)


def load_accounts_raw():
    """全部账号原始行（不解密 password/phone_code），供仅需明文列的统计/归类路径。

    只 SELECT + 组行：无 AES-GCM 解密、无明文自愈回写。计数、取 owner 集合、容量
    三分类只用明文列（phone 本就是明文、兼作 AAD）与 status/user_paused/deleted/owner，
    调用方拿不到也无需明文凭据。锁口径与 `load_accounts` 一致。
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
    """日志/列表脱敏：邮箱 → abc***@example.com（幂等）。

    真源在 `yiban.masking.mask_email`——展示面与审计 `actor` 列共用同一份口径，
    这里只是 web 侧的名字转发（前端 `maskEmail` 由对拍测试钉住同口径）。
    """
    return mask_email(e)


def _owner_display_of(owner_email):
    """账号归属展示名（后台归属列与用户下拉共用唯一口径，服务端算好再下发）。

    普通用户显示**遮罩后的**邮箱本地部（`mask_email_local`：号形态→`138****0000`，
    其余→前 3 字符 + `***`）。旧实现整段本地部外发——现网约一成账号的本地部
    **就是手机号**（MF-49 出口字段），且 `account-form.js` 的 `email.split("@")[0]`
    曾按同一规则在前端另算一份。收敛后拆分与遮罩只在这一处，前端一律消费
    服务端下发的结果字段，不得本地再拆。
    """
    if owner_email in ("admin", ""):
        return "管理员"
    local = owner_email.split("@")[0] if "@" in owner_email else owner_email
    return mask_email_local(local)


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


def fold_phone_code(clean, old_code=None):
    """把清洗字段里的设备识别码折算成**将写入库的最终值**，就地更新 clean，返回该值。

    表单协议（全部消费点共用同一折算，防各路由自行解读漂移）：
    - 空串 = 保持不变：回填 `old_code`（编辑表单不预填本字段，防误清空）。
    - `__clear__` 哨兵 = 显式清空：**折算成 ""** 留在字段里随 UPDATE 进 SET。
      哨兵是协议令牌而非用户数据，既不能原样落库，也不能在进 SET 前被摘掉——
      摘掉就是"用户点清除、看到已保存、库里值原封不动"的静默空操作。
    - 其余值原样保留。

    `old_code=None` 表示添加路径（没有旧值可保），空串原样留空。返回值供调用方
    与旧值比对，判定"本次是否改写了设备识别码"。
    """
    raw = clean.get("phone_code", "")
    if raw == CLEAR_SENTINEL:
        clean["phone_code"] = ""
    elif not raw and old_code is not None:
        clean["phone_code"] = old_code or ""
    return clean.get("phone_code", "")


def _stale_idx_guard(acc, data, *, fail_closed=False):
    """防错位校验：mutation 按 idx 寻址时，客户端携带的 phone 与服务端 idx 解析结果
    不一致 → 账号列表在视图快照后已漂移（并发删除/移动等），放行会静默操作错误对象。
    返回 True 表示错位，调用方应返回 409 引导刷新。

    **默认 `fail_closed=False`**（不携带 phone 就放行）只对**用户端**四个写口成立：
    `web/static/js/components/my-accounts.js` 的 pause/delete/restore 请求体确实不带
    phone，翻转默认值会造成生产用户删除/暂停/恢复 409 回归。**六个管理面写口一律显式
    传 `fail_closed=True`**（`web/routes/accounts_api.py` 的 update/delete/restore/
    purge/review/move），批量口另有"缺 phones 即 409"的对齐令牌闸——不可逆写口一律
    fail-closed，这条纪律就是为此。加新写口时**先看自己在哪一侧**，别照抄默认值。
    """
    phone = data.get("phone") if isinstance(data, dict) else None
    if phone is None:
        # 没带 phone 就等于没人证明"idx 还指向视图里那一行"：改写凭据这类写路径按错位
        # 处理。放行=静默改掉别人的易班凭据还回 200，代价远大于让用户刷新一次页面
        return fail_closed
    # 双侧先过 _mask_phone 再比：/api/accounts 出站即脱敏，浏览器回传的是 138****0000
    # 形态；_mask_phone 幂等（含 * 原样返回），所以直连 API 发全号的旧客户端同样可比。
    # 伪造别人的号码仍因不等被拦。
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
def _slot_to_label(slot_min, sign_window_bounds):
    """自选片相对**有效窗口起点**的分钟偏移 → "HH:MM"（偏移 0 = 窗口起点，与调度侧同号）。

    基准必须是有效窗口起点：片号是相对窗口起点的偏移，而裁剪把有效窗口吃空时
    `window.bounds` 回退默认窗口——直读原始窗口起点会让片卡（按有效窗口）显示 06:30、
    而偏好标签与保存提示（按原始窗口）说 07:00，同一页面出现两个钟点。

    参数注入口径见模块头「通信」（`sign_window_bounds` 是既有打桩点）。
    """
    if slot_min is None:
        return None  # 没选片就没有偏移：不出"07:00"这种假默认值
    win = sign_window_bounds()
    m = win.start_min + int(slot_min)
    return f"{m // 60:02d}:{m % 60:02d}"


def _estimate_slot(phone, load_accounts, read_env, env_file, sign_window_bounds):
    """预计签到时段（调度 v2）：
    顺序排序 = 可预期（线性填块区间 / 锚点中心 / 小人数确定性等分）；
    随机排序 = 每天重排，返回 None + 提示文案。
    返回 (estimated_str|None, note_str)。

    几何一律取自**有效窗口视图**（`window.bounds`，含裁剪吃空时的回退）：自己按原始
    窗口与原始裁剪拼 `eff_lo/eff_hi` 会在回退时得到空区间（span=0），预计时段静默变空。
    找不到可用片时仍返回 `(None, "")`（fail-closed，不回退成某个默认片）。

    块容量取引擎唯一源 `schedule.block_capacity`，env 用本函数已读的 `.env` 结果传入
    （MF-93：不能读 web 进程环境，那里没有 `.env` 的键）。

    参数注入口径见模块头「通信」（`load_accounts` / `read_env` / `ENV_FILE` /
    `sign_window_bounds` 都可被打桩或赋值改写）。
    """
    env = read_env(env_file)
    mode = env.get("YIBAN_SIGN_MODE", "").strip().lower()  # 旧的模式键，下面两个新键缺省时用它
    order = env.get("YIBAN_SIGN_ORDER", "").strip().lower() or (
        "random" if mode == "random" else "sequence")
    dist = env.get("YIBAN_SIGN_DIST", "").strip().lower() or (
        "normal" if mode == "normal" else "front")
    if order != "sequence":
        return None, "随机模式每日重排，签到时间当天 06:31 后可见"
    accounts = load_accounts()
    # 与引擎同源的三重过滤：软删 / 待审 / 已拒（工程装载器）+ 自暂停（build_schedule 开头），
    # 均不参与调度；旧数据缺 status 字段=视为已过审，必须放行。预计时段按实际参与人计算。
    live = [
        a for a in accounts
        if not a.get("deleted")
        and a.get("status") not in ("pending", "rejected")
        and not a.get("user_paused")
    ]
    idx = next((i for i, a in enumerate(live) if a.get("phone") == phone), None)
    if idx is None or not live:
        return None, ""
    win = sign_window_bounds()
    start_min, end_min = win.start_min, win.end_min
    eff_lo, eff_hi = win.lo_min, win.hi_min
    span = eff_hi - eff_lo

    def fmt(m):
        m = int(m)
        return f"{m // 60:02d}:{m % 60:02d}"

    if dist in ("uniform", "front"):
        # 线性填块（与 schedule._schedule_blocks 同口径：块从窗口起点步进 5、裁到有效窗口、
        # 被缓冲吃掉的无效块跳过；压缩模式等极端场景按末块估算）。`front` 只把落点收进窗口
        # 前段，与 `uniform` 同属确定性的非钟形一族，网页的"预计时段"取同一支。
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
        # 块容与引擎同源：`block_capacity` 内部对 YIBAN_BLOCK_CAP 做 [1,200] 夹取、
        # 非法/越界回退 15（含显式 0），压缩模式放大为 ceil(n/块数)。网页侧不再有
        # "k<=0=不限容量" 特判——该口径在引擎侧本就不存在，对齐后此分支不可达。
        k = yb_schedule.block_capacity(len(live), len(valid), env=env)
        bi = min(idx // k, len(valid) - 1)
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
