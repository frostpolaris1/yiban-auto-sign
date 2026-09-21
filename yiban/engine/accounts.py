# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
账号装载：数据库 / JSON 环境变量 / 旧格式环境变量三种来源，统一去重与设备回退。

优先级（`load_accounts`）：数据库 > `YIBAN_ACCOUNTS_JSON` > 旧格式环境变量。装载是
引擎的第一道门，因此这里同时承担三件事：密文解密（依赖 `YIBAN_ACCOUNTS_KEY`）、
审核态过滤（pending/rejected/deleted 不参与签到）、重复手机号去重（同一账号被完整
登录两次会让重试预算错乱）。

**归属**
`yiban.engine` 的账号装载层（引擎入口的第一道门）；`runner` / `probe` / 多执行体子进程
都从这里取账号。

**复用**
`Account` 数据模型与 `load_accounts` 是唯一来源；设备回退与审核态过滤口径被
`config_check`、web 服务层与 rekey 工具复用。

**通信**
输入：`db`（accounts 表，密文经 `account_crypto` 解密）、`YIBAN_ACCOUNTS_JSON` /
旧格式环境变量。输出：`Account` 列表（含 owner / account_id 等运行期字段）。
调用谁：`db`、`account_crypto`、`config_check`。
谁调用：`runner`、`probe`、`workers` 的子进程。
前端调用点：`/api/accounts`（`web/static/js/pages/work_accounts.js`、
`web/static/js/components/account-ops.js`）与 `/api/my-accounts`（`web/static/js/components/my-accounts.js`）
的增删改由本模块在下一轮装载生效——优先级/去重/审核态口径变化会改变这些页面看到的
可签到集合。
跨模块一律走模块属性访问（如 `config_check._key_env_file()`）。
"""
import json
import logging
import os
from dataclasses import dataclass

from yiban.engine import config_check
from yiban.infra import account_crypto
from yiban.masking import mask_phone as _mask_phone
from yiban.masking import sanitize_text as _sanitize_text
from yiban.store import db

logger = logging.getLogger("yiban")


# ---------------------------------------------------------------------------
# 账号数据模型
# ---------------------------------------------------------------------------
@dataclass
class Account:
    """单个易班账号配置。

    通过 Web 管理后台添加（存于 SQLite 数据库），
    一次输入一个账号的完整信息，无需用符号分隔。
    """

    phone: str
    # str 不可变，无法原位清零；且重试队列要跨尝试复用，密码 str 本体只能随
    # accounts 列表生命周期存活（客户端侧可变副本的清零与局限见
    # YibanClient._wipe_credentials）
    password: str
    phone_model: str = ""  # 设备型号（学校开启"设备绑定"时必填）
    phone_code: str = ""  # 设备唯一识别码（学校开启"设备绑定"时必填）
    name: str = ""  # 自定义名称（未填写时显示为"账号N"）
    user_paused: bool = False  # 用户自暂停签到（调度 v2；db.load_accounts 透传）
    owner: str = ""  # 账号归属用户邮箱（B 线：签到失败时向 owner 发提醒邮件；JSON/legacy 来源为空）
    # 库内账号行 id（db 来源才有，JSON/环境变量来源为 0）：运行期复核账号是否仍有效用
    account_id: int = 0

    @property
    def has_device_info(self):
        return bool(self.phone_model and self.phone_code)


# ---------------------------------------------------------------------------
# 账号配置加载
# ---------------------------------------------------------------------------
def _parse_account_dict(data):
    """将账号 JSON 对象解析为 Account，校验必填字段。

    password/phone_code 支持 AES-GCM 密文对象（web 存储层加密落盘）：解密依赖
    同一密钥——环境变量 YIBAN_ACCOUNTS_KEY → .env 同键（YIBAN_ENV_FILE 可指定
    路径）；密钥缺失/解密失败抛明确错误，绝不静默使用错误数据。
    """
    phone = str(data.get("phone") or data.get("account") or "").strip()
    password = data.get("password") or data.get("pwd") or ""
    phone_code = data.get("phone_code") or ""
    if account_crypto.is_encrypted(password) or account_crypto.is_encrypted(phone_code):
        if not account_crypto.has_key(config_check._key_env_file()):
            raise RuntimeError(
                "账号已加密但未配置 YIBAN_ACCOUNTS_KEY（请在 .env 中配置或恢复密钥备份）"
            )
        key = account_crypto.load_key(config_check._key_env_file())
        if account_crypto.is_encrypted(password):
            try:
                password = account_crypto.decrypt_password(password, key, phone)
            except ValueError as e:
                raise RuntimeError(f"账号 {phone} 密码解密失败: {e}") from e
        if account_crypto.is_encrypted(phone_code):
            try:
                phone_code = account_crypto.decrypt_password(phone_code, key, phone)
            except ValueError as e:
                raise RuntimeError(f"账号 {phone} 设备识别码解密失败: {e}") from e
    password = str(password).strip()
    if not phone or not password:
        # 异常消息只带 phone（登录名，非机密），绝不包含 password 明文
        missing = "phone" if not phone else "password"
        raise ValueError(f"账号配置缺少必填字段: {missing} 为空（phone={phone or '<空>'}）")
    return Account(
        phone=phone,
        password=password,
        phone_model=str(data.get("phone_model") or "").strip(),
        phone_code=str(phone_code).strip(),
        name=str(data.get("name") or "").strip(),
        # 用户自暂停（调度 v2）：显式解析 "1"/"true"/"on"/"yes"，避免 "0"/"false" 被 bool() 误判
        user_paused=str(data.get("user_paused", False)).strip().lower() in ("1", "true", "on", "yes"),
        # 归属用户邮箱（B 线用户失败提醒用；JSON/legacy 环境变量来源无此字段）
        owner=str(data.get("owner") or "").strip(),
        # 库内账号行 id（运行期复核账号是否仍有效用；JSON/legacy 来源无此字段 → 0）
        account_id=int(data.get("id") or 0),
    )


def _load_accounts_from_file():
    """从数据库文件（yiban.db，SQLite）加载；web 后台写入，单行事务防并发覆盖。

    db 层返回已解密明文；此处只做审核状态过滤。
    """
    db.init_db(env_file=config_check._key_env_file(), cleanup=False)
    all_accounts = db.load_accounts()
    # 跳过待审核（status=pending：网页端普通用户提交、管理员尚未审核通过）、
    # 被拒绝（status=rejected：管理员审核不通过，不得签到）与待删除账号
    # （deleted：网页端软删除，保留期内可恢复，不参与签到）。
    # 注意 "pending"/"rejected" 是账号审核态（web 侧 ACCOUNT_STATUS_*），与签到状态码
    # STATUS_PENDING 等是两套语义，勿混用。旧数据可能没有 status 字段（等于通过审核），
    # 必须放行。
    active_raw = [
        item
        for item in all_accounts
        if item.get("status") != "pending"
        and item.get("status") != "rejected"
        and not item.get("deleted")
    ]
    accounts = [_parse_account_dict(item) for item in active_raw]
    logger.debug(f"已从数据库加载 {len(accounts)} 个账号")
    return accounts


def _load_accounts_from_json_env():
    """从 YIBAN_ACCOUNTS_JSON 环境变量加载（JSON 数组字符串，供 CI 使用）。"""
    raw = os.environ.get("YIBAN_ACCOUNTS_JSON", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"YIBAN_ACCOUNTS_JSON 不是合法 JSON: {e}") from e
    if not isinstance(data, list):
        raise RuntimeError("YIBAN_ACCOUNTS_JSON 应为 JSON 数组")
    accounts = [_parse_account_dict(item) for item in data]
    logger.info(f"已从 YIBAN_ACCOUNTS_JSON 加载 {len(accounts)} 个账号")
    return accounts


def _load_accounts_from_legacy_env():
    """旧格式兼容：YIBAN_ACCOUNTS（phone:password#...）与 YIBAN_PHONE/YIBAN_PASSWORD。

    此路径接受的是**明文**凭据环境变量：进库前会加密，但明文源仍留在 .env 与进程
    环境里（/proc/<pid>/environ 同 uid 可读）。保留兼容，但加载即告警，提示改用
    Web 管理台或 YIBAN_ACCOUNTS_JSON；告警内容不含任何凭据明文。
    """
    accounts = []
    accounts_str = os.environ.get("YIBAN_ACCOUNTS", "")
    if accounts_str or os.environ.get("YIBAN_PASSWORD", ""):
        logger.warning(
            "检测到旧格式明文账号配置（YIBAN_ACCOUNTS/YIBAN_PASSWORD）：凭据明文存在于 "
            "环境变量与进程环境中，建议改用 Web 管理台或 YIBAN_ACCOUNTS_JSON 管理账号"
        )
    for item in accounts_str.split("#"):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            # 清洗后落日志，防畸形片段换行/回车注入。片段缺 ":" 时常见是裸手机号
            # （漏输密码），纯数字片段按手机号脱敏后落盘。
            logger.error(
                f"账号配置格式错误（应为 phone:password）: "
                f"{_sanitize_text(_mask_phone(item) if item.isdigit() else item)}")
            continue
        phone, pwd = item.split(":", 1)
        accounts.append(Account(phone.strip(), pwd.strip()))
    if not accounts:
        phone = os.environ.get("YIBAN_PHONE", "").strip()
        pwd = os.environ.get("YIBAN_PASSWORD", "").strip()
        if phone and pwd:
            accounts.append(Account(phone, pwd))
    return accounts


def _apply_global_device_info(accounts):
    """账号未配置设备信息时，回退到全局环境变量（兼容旧配置方式）。"""
    model = os.environ.get("YIBAN_PHONE_MODEL", "").strip()
    code = os.environ.get("YIBAN_PHONE_CODE", "").strip()
    if not (model and code):
        return accounts
    for acc in accounts:
        if not acc.has_device_info:
            acc.phone_model = model
            acc.phone_code = code
    return accounts


def _dedupe_by_phone(accounts):
    """同一手机号重复出现时只保留第一条并告警（返回新列表）。

    调度、重试预算、汇总与状态文件全以手机号为键：重复项会让同一账号被完整登录
    两次，且两次尝试共享同一份重试计数（预算错乱）。库内模式由 accounts.phone 的
    唯一索引天然兜底，但 JSON / 环境变量配置模式此前没有任何校验。
    """
    seen, kept, dup = set(), [], []
    for acc in accounts:
        if acc.phone in seen:
            dup.append(acc.phone)
            continue
        seen.add(acc.phone)
        kept.append(acc)
    if dup:
        logger.warning(
            "账号配置存在重复手机号 %d 个（已按首次出现去重）：%s",
            len(dup), ", ".join(_mask_phone(p) for p in dup),
        )
    return kept


def load_accounts():
    """按优先级加载账号配置：文件 > JSON 环境变量 > 旧格式环境变量（按手机号去重）。"""
    for loader in (
        _load_accounts_from_file,
        _load_accounts_from_json_env,
        _load_accounts_from_legacy_env,
    ):
        accounts = loader()
        if accounts:
            return _dedupe_by_phone(_apply_global_device_info(accounts))
    return []
