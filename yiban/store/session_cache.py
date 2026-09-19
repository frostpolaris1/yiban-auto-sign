# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""会话 Cookie 缓存域：session_cache 表族的读写、失效判定与凭据加密。

**功能**
- 读/写/清除：`get_session_cache`（未命中、过期、解密失败一律返回 None）、
  `set_session_cache`（UPSERT，保留首次 created_at）、`clear_session_cache`；
- 失效判定：有效期两道判据（主判据 = 同一业务日，护栏 = TTL 小时数），命中即读时
  顺手清行；`_session_cache_ttl_hours` 读取并钳制 `YIBAN_SESSION_TTL_HOURS`；
- 凭据加密：`_session_cache_key` 经 HKDF-SHA256 从账号主密钥派生**专用**密钥，
  cookie 与 csrf 均 AES-GCM 密文落库（AAD=手机号）；`_session_cache_now` 是写入与
  判定共用的唯一时钟接缝。

**归属**
`yiban.store.db` 门面之后：定义点在本模块，门面把全部迁出名纳入模块级读写转发
（`db.get_session_cache()` 一类调用、`db._session_cache_now = 替身` 一类打桩与
`del db.get_session_cache` 都落到这里）；TTL 与 HKDF 常量以本模块为唯一定义点，门面按常量
再导出。留在 db.py 的跨域助手 `_clear_session_cache_by_phones`（改绑手机号/注销的停用
凭据缓存路径）仍直接 DELETE 本表——它与 `_cascade_phone_owned` 共用同一份"以 phone 为键的
表"清单，故不随本域迁出。建表在迁移域（`yiban/store/migrations.py`，v8），本模块只读写。

**复用**
`yiban.client`（登录后写缓存 / 命中即免登录）与 `yiban.engine.attempts`（会话失效类失败
联动清除）经 db 门面调用；`account_crypto` 提供加解密原语，`yiban.clock` 提供东八区时钟。

**通信**
连接、进程内锁与写事务入口（`_conn_lock` / `get_conn` / `_begin_immediate`）一律经
`_facade()` 按属性取——必须按属性取而非模块级 from-import，`mock.patch.object(db,
"get_conn" / "_conn_lock" / "_session_cache_now", …)` 一类打桩才会在函数体里生效。密钥来源
路径 `_connection._env_file` 直接读连接模块（`db._env_file = path` 的写入由 db 门面转发
落到那里）。
"""
import contextlib
import datetime
import json
import logging
import os

from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF

from yiban import clock
from yiban.infra import account_crypto
from yiban.store import connection as _connection

logger = logging.getLogger("yiban.store.session_cache")


def _facade():
    """db 门面（函数内延迟导入，避免与 `yiban.store.db` 形成导入环）。

    打桩可见性见模块说明：必须按**属性**取而不是模块级 from-import，`db.<名字> = 替身`
    才会在函数体里生效。
    """
    from yiban.store import db
    return db


# ---------------------------------------------------------------------------
# 会话 Cookie 缓存（v8，docs/research-lumjiel-core-sign-20260822.md §七）
# ---------------------------------------------------------------------------
# 会话有效期两道判据（公测复盘后重定）：
# ① 主判据 = 同一业务日：签到是"一天一签"的业务，缓存只在当天内复用（同日重试 /
#    首轮到兜底补签 / 当天手动重试）。隔夜缓存收益最小、风险最大——生产当天 3 个
#    账号正是复用了前一晚 20:35 写入、被服务端作废的会话。
# ② 护栏 = TTL 小时数：同一天内的附加上限，默认由 12 降到 6（同日窗口最长 80 分钟，
#    6 小时已极宽）。旧口径"调大 TTL 以支持跨天复用"已被 ① 取代：再调大也解锁不了
#    跨天复用，只能放宽同日内的时长。
# 服务端会话真实有效期未知，取值原则不变：宁多登一次，不可拿过期凭据撞风控。
SESSION_CACHE_TTL_HOURS_DEFAULT = 6
# TTL 钳制边界：上限 72h（再大会把远过期凭据反复送去撞风控/已改密账号），
# 下限 1h（低于 1h 缓存命中形同虚设）。越界不静默接受，回默认并告警。
SESSION_CACHE_TTL_HOURS_MIN = 1.0
SESSION_CACHE_TTL_HOURS_MAX = 72.0

# 密钥分离：session_cache 加密密钥不再直接复用账号凭据主密钥，
# 改由 YIBAN_ACCOUNTS_KEY 经 HKDF-SHA256 派生（info/salt 固定常量）。
# 主密钥泄露面的缩小之外，更防一类跨用途事故：cookie 缓存密文与账号密码密文
# 同钥同体系，任一用途的密文/明文对（如已知自己密码的账号）可为攻击者提供
# 验证主密钥的样本；派生隔离后互不可推。HKDF 每次现算（约 3 次 HMAC，开销
# 微秒级），不缓存派生结果——主密钥轮换后立即生效。
# 派生密钥变更后旧缓存解密必然失败 → 读侧按未命中清行，用户重登即可重建
# （会话缓存本就是可再生优化数据，此失效路径可接受）。
SESSION_CACHE_HKDF_INFO = b"yiban-session-cache-v1"


def _session_cache_now():
    """会话缓存统一时钟（东八区裸时间，与库内 %Y-%m-%d %H:%M:%S 串同制）。

    写入与过期判定必须用同一个钟：宿主为 UTC 时若写入取北京时间、判定取本地时间，
    updated_at 会凭空"领先"8 小时，TTL 判据永远不会命中。东八区口径统一由
    `yiban.clock` 提供，本域不得另起固定 +8 时区。
    """
    return clock.now()


def _session_cache_key():
    """session_cache 专用加密密钥（HKDF-SHA256 派生，与账号凭据加密密钥隔离）。"""
    return HKDF(
        account_crypto.load_key(_connection._env_file),
        32,
        salt=SESSION_CACHE_HKDF_INFO,
        hashmod=SHA256,
        context=SESSION_CACHE_HKDF_INFO,
    )


def _session_cache_ttl_hours():
    """读 TTL 小时数（YIBAN_SESSION_TTL_HOURS，默认 6）：缺失/非法/非正回退默认。

    配置越界（<1h 或 >72h）同样回退默认并告警——此前可配 8760h 之类
    超大值，把早已失效的会话凭据跨季反复复用，等于放大风控与撞库面。
    """
    raw = os.environ.get("YIBAN_SESSION_TTL_HOURS", "").strip()
    if not raw:
        return SESSION_CACHE_TTL_HOURS_DEFAULT
    try:
        v = float(raw)
    except ValueError:
        logger.warning("配置 YIBAN_SESSION_TTL_HOURS=%r 非法，回退默认 %s 小时", raw, SESSION_CACHE_TTL_HOURS_DEFAULT)
        return SESSION_CACHE_TTL_HOURS_DEFAULT
    if v <= 0:
        logger.warning("配置 YIBAN_SESSION_TTL_HOURS=%s 非正，回退默认 %s 小时", raw, SESSION_CACHE_TTL_HOURS_DEFAULT)
        return SESSION_CACHE_TTL_HOURS_DEFAULT
    if not (SESSION_CACHE_TTL_HOURS_MIN <= v <= SESSION_CACHE_TTL_HOURS_MAX):
        logger.warning(
            "配置 YIBAN_SESSION_TTL_HOURS=%s 超出钳制范围 [%s, %s]，回退默认 %s 小时",
            raw, SESSION_CACHE_TTL_HOURS_MIN, SESSION_CACHE_TTL_HOURS_MAX,
            SESSION_CACHE_TTL_HOURS_DEFAULT,
        )
        return SESSION_CACHE_TTL_HOURS_DEFAULT
    return v


def get_session_cache(phone):
    """读取会话缓存：返回 {"cookies": <明文 JSON 串>, "csrf": str, 时间戳}，未命中返回 None。

    超过 TTL 的行读时顺手清除（updated_at 为定宽 %Y-%m-%d %H:%M:%S，字符串比较
    等价时间序，与库内其他 ts 比较口径一致）。缓存是可再生的优化数据，解密失败
    （换密钥/密文损坏）按未命中处理并清行，不阻断签到重新登录。
    csrf 列同为准密文（AES-GCM 对象）。读侧遇到旧版明文行（非密文对象）
    一律视为失效——csrf 是免登录复用的关键凭据，明文残留行不可信，整行清除重登。
    """
    db = _facade()
    with db._conn_lock:
        conn = db.get_conn()
        row = conn.execute(
            "SELECT cookies_ct, csrf, created_at, updated_at FROM session_cache WHERE phone=?",
            (phone,),
        ).fetchone()
        if row is None:
            return None
        now = _session_cache_now()
        ttl_hours = _session_cache_ttl_hours()
        cutoff = (now - datetime.timedelta(hours=ttl_hours)).strftime("%Y-%m-%d %H:%M:%S")
        today = now.strftime("%Y-%m-%d")
        # 两道判据取更严者，且跨日判定排在前面：否则"昨晚 20:35 写的缓存今早 06:31
        # 复用"会被报成"超出 TTL"，把业务日语义问题误读成秒数配置问题。
        if row["updated_at"][:10] != today:
            reason = f"跨业务日（缓存日 {row['updated_at'][:10]} ≠ 今日 {today}）"
        elif row["updated_at"] <= cutoff:
            reason = f"同日内超出 TTL {ttl_hours} 小时"
        else:
            reason = None
        if reason:
            try:
                db._begin_immediate(conn)
                conn.execute("DELETE FROM session_cache WHERE phone=?", (phone,))
                conn.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    conn.rollback()
                logger.warning("清理过期会话缓存失败: %s", phone)
            logger.info("会话缓存作废（%s）: %s，本次真实登录", reason, phone)
            return None
        try:
            obj = json.loads(row["cookies_ct"])
            key = _session_cache_key()  # HKDF 派生密钥，与账号凭据密钥隔离
            cookies = account_crypto.decrypt_password(obj, key, phone)
            # csrf 解密（旧明文行在此抛错 → 整行按未命中清除）
            csrf_obj = json.loads(row["csrf"])
            if not account_crypto.is_encrypted(csrf_obj):
                raise ValueError("csrf 字段不是密文对象（旧版明文行）")
            csrf = account_crypto.decrypt_password(csrf_obj, key, phone)
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            logger.warning("会话缓存解密失败（按未命中清除重登）: %s: %s", phone, e)
            with contextlib.suppress(Exception):
                conn.execute("DELETE FROM session_cache WHERE phone=?", (phone,))
                conn.commit()
            return None
        return {
            "cookies": cookies,
            "csrf": csrf,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


def set_session_cache(phone, cookie_json, csrf):
    """写入/更新会话缓存（UPSERT）。cookie_json 为明文 cookie jar JSON 串，
    落库前 AES-GCM 加密（AAD=phone，复用 account_crypto，与密码字段同体系）；
    csrf 与 cookies 同等加密保护（csrf 也是免登录复用的认证凭据，
    明文落库使库文件泄露即可直接伪造请求）；更新时保留首次 created_at，
    只刷新 updated_at。

    BEGIN IMMEDIATE：跨进程写锁（signin 与 web 并存时串行化写路径）。
    """
    db = _facade()
    now = _session_cache_now().strftime("%Y-%m-%d %H:%M:%S")
    key = _session_cache_key()  # HKDF 派生密钥，与账号凭据密钥隔离
    cookies_ct = json.dumps(
        account_crypto.encrypt_password(str(cookie_json), key, phone)
    )
    csrf_ct = json.dumps(account_crypto.encrypt_password(str(csrf), key, phone))
    conn = db.get_conn()
    with db._conn_lock:
        try:
            db._begin_immediate(conn)
            conn.execute(
                "INSERT INTO session_cache (phone, cookies_ct, csrf, created_at, updated_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(phone) DO UPDATE SET cookies_ct=excluded.cookies_ct, "
                "csrf=excluded.csrf, updated_at=excluded.updated_at",
                (phone, cookies_ct, csrf_ct, now, now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def clear_session_cache(phone):
    """清除单账号会话缓存（会话失效/风控类失败联动；行不存在时无操作）。"""
    db = _facade()
    conn = db.get_conn()
    with db._conn_lock:
        try:
            db._begin_immediate(conn)
            conn.execute("DELETE FROM session_cache WHERE phone=?", (phone,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
