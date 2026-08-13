#!/usr/bin/env python3
"""
易班自动签到脚本

功能：
1. 自动登录易班（支持多账号，默认 KillYiBan 同款真实 App 特征登录）
2. 自动获取签到任务范围
3. 在签到范围内生成随机定位点（模拟真实定位）
4. 自动提交签到
5. 支持消息通知（Server 酱、Bark、企业微信等）
6. 重试逻辑：失败账号放队尾分散重试（风控类最多 2 次，其他最多 4 次）
7. 随机延迟：启动与账号间隔随机打散（YIBAN_START_DELAY_MAX / YIBAN_ACCOUNT_GAP_MAX）

参考项目：
- 本地 KillYiBan 模块（nightAttendance 签到流程与登录特征）
- Auto-Test 项目（旧登录流程，YIBAN_LEGACY_LOGIN=1 启用）
"""

import argparse
import json
import logging
import os
import random
import re
import secrets
import sys
import time
import traceback
from base64 import b64decode, b64encode
from dataclasses import dataclass
from datetime import datetime
from hashlib import md5
from urllib.parse import urlencode, urlsplit

import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA
from requests.utils import cookiejar_from_dict, dict_from_cookiejar

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
# 支持通过环境变量调整日志级别：DEBUG / INFO / WARNING / ERROR
LOG_LEVEL = os.environ.get("YIBAN_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.DEBUG),
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("yiban")


# 易班 iOS 客户端 UA（与 Auto-Test 保持一致）
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/4.0 "
    "Chrome/104.0.5112.97 Mobile Safari/537.36 yiban_iOS/5.0.12",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "com.yiban.app",
    "Origin": "https://app.uyiban.com",
    "Referer": "https://app.uyiban.com/",
    "Connection": "close",
}

# KillYiBan 同款请求头（默认登录方式）
# 注意：usersure 提交时会被显式覆盖为不带 Origin/Referer（见 login_killyiban 第 3 步，
# 实测带 Origin → e001 无效应用端编号），其余请求用此头
KILLYIBAN_HEADERS = {
    "User-Agent": "Yiban",
    "AppVersion": "5.1.2",
    "Origin": "https://c.uyiban.com",
    "Referer": "https://c.uyiban.com/",
    "Connection": "close",
}


# ---------------------------------------------------------------------------
# 账号数据模型
# ---------------------------------------------------------------------------
@dataclass
class Account:
    """单个易班账号配置。

    通过 TUI 配置工具或直接编辑 accounts.json 创建，
    一次输入一个账号的完整信息，无需用符号分隔。
    """

    phone: str
    password: str
    phone_model: str = ""  # 设备型号（学校开启"设备绑定"时必填）
    phone_code: str = ""  # 设备唯一识别码（学校开启"设备绑定"时必填）
    name: str = ""  # 可选：自定义名称（TUI 输入，未填写时显示为"账号N"）

    @property
    def has_device_info(self):
        return bool(self.phone_model and self.phone_code)


# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------
# 队列重试配置：每账号"1 次初始尝试 + 最多 3 次队列重试"（总尝试上限 4 次）
MAX_ATTEMPTS = 4
# 风控类失败（e003/无效应用端等）加重标记风险，最多重试 1 次（总尝试上限 2 次）
RISK_MAX_ATTEMPTS = 2
# 同一账号两次尝试之间的最短间隔（秒），避免紧邻重试被识别为连击
RETRY_MIN_INTERVAL = 60
# 网络/瞬时类失败重试间隔打散上限（秒），作为账号间随机延迟之外的补充
RETRY_GAP_MAX = 30

# 账号配置文件（默认当前目录，可用 YIBAN_ACCOUNTS_FILE 覆盖）
ACCOUNTS_FILE = os.environ.get("YIBAN_ACCOUNTS_FILE", "accounts.json")

# 随机延迟默认值（TUI 设置栏开启时采用；默认关闭=0）
# 打散"每天固定秒级执行"的脚本特征，作为 e003 登录特征修复之外的纵深防御
DEFAULT_START_DELAY_MAX = 60  # 启动后随机等待 0~60 秒
DEFAULT_ACCOUNT_GAP_MAX = 10  # 顺序模式账号间随机间隔 0~10 秒

# 签到模式：sequence（列表顺序，默认）/ random（列表随机打散）
# 由网页系统设置页写入 .env（YIBAN_SIGN_MODE），run.sh 加载后经环境变量传入
SIGN_MODE = os.environ.get("YIBAN_SIGN_MODE", "").strip().lower()

# WAF 风控关键词（用于判断是否被拦截）
WAF_KEYWORDS = ["风险访问", "风控", "访问服务禁用", "WAF", "拦截"]


# ---------------------------------------------------------------------------
# 定位生成：多边形内随机点
# ---------------------------------------------------------------------------
def point_in_polygon(x, y, polygon):
    """射线法判断点是否在多边形内。"""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def generate_position_in_polygon(polygon_points):
    """在多边形内生成随机点（缩放质心算法）。"""
    if not polygon_points:
        return None

    min_lng = min(p[0] for p in polygon_points)
    max_lng = max(p[0] for p in polygon_points)
    min_lat = min(p[1] for p in polygon_points)
    max_lat = max(p[1] for p in polygon_points)

    center_lng = sum(p[0] for p in polygon_points) / len(polygon_points)
    center_lat = sum(p[1] for p in polygon_points) / len(polygon_points)

    scaled_points = [
        ((p[0] - center_lng) * 0.7 + center_lng, (p[1] - center_lat) * 0.7 + center_lat)
        for p in polygon_points
    ]

    for _ in range(100):
        lng = center_lng + (max_lng - min_lng) * 0.2 * (random.random() - 0.5)
        lat = center_lat + (max_lat - min_lat) * 0.2 * (random.random() - 0.5)
        if point_in_polygon(lng, lat, scaled_points) and point_in_polygon(lng, lat, polygon_points):
            return (lng, lat)

    return (center_lng, center_lat)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def is_waf_blocked(response_text):
    """判断响应是否为 WAF 风控拦截。

     WAF 拦截页通常很短（< 2000 字符），而正常页面（如 OAuth 授权页、
     服务协议等）内容较长且可能包含"风控""拦截"等正常法律文本。
     因此仅在响应内容较短时才检测 WAF 关键词，避免误报。

     注意：易班 WAF 返回 JSON 格式时，中文会被 Unicode 转义
    （如 \\u98ce\\u9669 = "风险"），需先解码再匹配关键词。
    """
    if len(response_text) > 2000:
        return False
    # 解码 \uXXXX 形式的 Unicode 转义序列后一并检测
    decoded = re.compile(r"\\u([0-9a-fA-F]{4})").sub(
        lambda m: chr(int(m.group(1), 16)), response_text
    )
    return any(keyword in response_text or keyword in decoded for keyword in WAF_KEYWORDS)


# ---------------------------------------------------------------------------
# 重试分级：判断一次失败是否值得重试、重试上限
# ---------------------------------------------------------------------------
# 风控/凭据类失败特征：重试不仅无用，还可能加重账号标记
RISK_FAIL_KEYWORDS = [
    "账号或密码错误",
    "e003",
    "无效的应用端",
    "e001",
    "origin invalid",
    "登录失败",
    "登录响应异常",
    "OAuth 页解析失败",
    # WAF 风控拦截：重试只会浪费请求并加重 IP/账号标记（与 WAF_KEYWORDS 对应）
    "风险访问",
    "风控",
    "访问服务禁用",
    "WAF",
    "拦截",
]
# 网络/瞬时类失败特征：值得重试
TRANSIENT_FAIL_KEYWORDS = ["Connection", "连接", "超时", "timeout", "Max retries", "Read timed"]


def classify_failure(message):
    """对失败信息分级，返回 (max_attempts, retryable)。

    - 风控/凭据类：最多重试 1 次（RISK_MAX_ATTEMPTS），避免加重账号标记
    - 其他失败（网络/未知）：最多重试 MAX_ATTEMPTS 次
    - 网络类之外明确不可重试（配置错误等）：retryable=False
    """
    for kw in RISK_FAIL_KEYWORDS:
        if kw in message:
            return RISK_MAX_ATTEMPTS, True
    return MAX_ATTEMPTS, True


def random_delay(max_seconds, label):
    """随机等待 0~max_seconds 秒（打散固定执行规律，max_seconds<=0 时不等待）。"""
    if max_seconds <= 0:
        return
    wait = random.uniform(0, max_seconds)
    logger.info(f"{label}: 随机延迟 {int(wait)} 秒（上限 {max_seconds} 秒）")
    time.sleep(wait)


def _sanitize_text(text):
    """服务端可控内容进入错误消息/日志/通知前转义换行与回车，防止日志与通知注入。"""
    return str(text).replace("\r", "\\r").replace("\n", "\\n")


# ---------------------------------------------------------------------------
# 账号配置加载
# ---------------------------------------------------------------------------
def _parse_account_dict(data):
    """将账号 JSON 对象解析为 Account，校验必填字段。"""
    phone = str(data.get("phone") or data.get("account") or "").strip()
    password = str(data.get("password") or data.get("pwd") or "").strip()
    if not phone or not password:
        # 异常消息只带 phone（登录名，非机密），绝不包含 password 明文
        missing = "phone" if not phone else "password"
        raise ValueError(f"账号配置缺少必填字段: {missing} 为空（phone={phone or '<空>'}）")
    return Account(
        phone=phone,
        password=password,
        phone_model=str(data.get("phone_model") or "").strip(),
        phone_code=str(data.get("phone_code") or "").strip(),
        name=str(data.get("name") or "").strip(),
    )


def _load_accounts_from_file():
    """从 accounts.json 文件加载（服务器端 TUI 配置工具生成）。"""
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"账号配置文件 {ACCOUNTS_FILE} 解析失败: {e}") from e
    if not isinstance(raw, list):
        raise RuntimeError(f"账号配置文件 {ACCOUNTS_FILE} 应为 JSON 数组")
    # 跳过待审核账号（status=pending：网页端普通用户提交、管理员尚未审核通过）、
    # 被拒绝账号（status=rejected：管理员审核不通过，不得签到）与待删除账号
    # （deleted：网页端软删除，保留期内可恢复，不参与签到）。
    # 注意：旧数据可能没有 status 字段（等于通过审核），必须放行。
    active_raw = [
        item
        for item in raw
        if item.get("status") != "pending"
        and item.get("status") != "rejected"
        and not item.get("deleted")
    ]
    accounts = [_parse_account_dict(item) for item in active_raw]
    logger.info(f"已从 {ACCOUNTS_FILE} 加载 {len(accounts)} 个账号")
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
    """旧格式兼容：YIBAN_ACCOUNTS（phone:password#...）与 YIBAN_PHONE/YIBAN_PASSWORD。"""
    accounts = []
    accounts_str = os.environ.get("YIBAN_ACCOUNTS", "")
    for item in accounts_str.split("#"):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            logger.error(f"账号配置格式错误（应为 phone:password）: {item}")
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


def load_accounts():
    """按优先级加载账号配置：文件 > JSON 环境变量 > 旧格式环境变量。"""
    for loader in (
        _load_accounts_from_file,
        _load_accounts_from_json_env,
        _load_accounts_from_legacy_env,
    ):
        accounts = loader()
        if accounts:
            return _apply_global_device_info(accounts)
    return []


def parse_env_int(name, default):
    """读取非负整数环境变量：缺失/非法回退默认值，负值归零。"""
    try:
        return max(0, int(os.environ.get(name, "").strip()))
    except (TypeError, ValueError):
        return default


def print_config_summary(accounts):
    """打印账号配置摘要（密码脱敏），不发任何网络请求。"""
    print("==== 账号配置检查 ====")
    for i, acc in enumerate(accounts, 1):
        if acc.has_device_info:
            device = f"设备: {acc.phone_model} / {acc.phone_code[:8]}..."
        else:
            device = "设备: 未配置（如学校开启设备绑定，签到将失败）"
        print(f"  {i}. {acc.phone} | 密码: {'*' * 8} | {device}")
    print(f"共 {len(accounts)} 个账号，配置检查通过。")


# ---------------------------------------------------------------------------
# 易班登录
# ---------------------------------------------------------------------------
class YibanClient:
    """易班客户端：封装登录与签到流程。"""

    def __init__(self, account):
        self.account = account
        self.password = account.password.encode("UTF-8")
        # 登录方式：默认 KillYiBan 同款流程（真实 App 特征，实测绕过 e003）；
        # 旧流程（Auto-Test 继承的 iOS 伪造 UA）仅在 YIBAN_LEGACY_LOGIN=1 时启用（GitHub Actions 等场景备选）
        self.use_killyiban = os.environ.get("YIBAN_LEGACY_LOGIN", "") != "1"
        if self.use_killyiban:
            self.csrf = secrets.token_hex(16)  # SecureRandom 真随机
            logger.debug(
                f"[{account.phone}] 登录方式: 标准 App 特征（UA=Yiban/AppVersion=5.1.2/SecureRandom CSRF）"
            )
        else:
            self.csrf = md5(str(datetime.now()).encode("UTF-8")).hexdigest()
            logger.debug(f"[{account.phone}] 登录方式: 旧流程（iOS 伪造 UA，YIBAN_LEGACY_LOGIN=1）")
        self.session = requests.Session()
        self.session.headers = dict(KILLYIBAN_HEADERS if self.use_killyiban else HEADERS)
        # 代理配置：GitHub Actions 海外 IP 可能被易班 WAF 地域风控拦截
        proxy = os.environ.get("YIBAN_PROXY", "").strip()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
            # 日志只记录 scheme://host:port，绝不落 userinfo（账号密码）明文
            proxy_parsed = urlsplit(proxy)
            if proxy_parsed.hostname:
                proxy_desc = f"{proxy_parsed.scheme}://{proxy_parsed.hostname}"
                if proxy_parsed.port:
                    proxy_desc += f":{proxy_parsed.port}"
            else:
                proxy_desc = "<无法解析>"
            logger.info(f"[{account.phone}] 已启用代理: {proxy_desc}")
        else:
            logger.warning(
                f"[{account.phone}] 未配置 YIBAN_PROXY，如遇到 WAF 拦截请配置代理（国内出口）"
            )
        # 设备信息：部分学校开启了"设备绑定"，签到时需校验设备型号和唯一识别码
        self.phone_model = account.phone_model
        self.phone_code = account.phone_code
        self.logged_in = False

    def _rsa_encrypt(self, cipher):
        """RSA-1024 + PKCS1_v1_5 加密密码，超长时给出明确报错（而非底层 ValueError 裸抛）。"""
        if len(self.password) > 117:
            raise ValueError(
                "密码过长: RSA-1024 公钥单次最多加密 117 字节（约 39 个中文字符），"
                "当前密码无法加密提交，请缩短密码或联系管理员处理"
            )
        return b64encode(cipher.encrypt(self.password))

    def login(self):
        """登录易班，成功返回 True，失败抛出异常。"""
        self.session.cookies = cookiejar_from_dict({"csrf_token": self.csrf})
        self.session.headers.update(
            Referer="https://c.uyiban.com/",
            Origin="https://c.uyiban.com",
        )

        # 1. 获取跳转 URL
        resp = self.session.get(
            "https://api.uyiban.com/base/c/auth/yiban",
            params={"CSRF": self.csrf},
            allow_redirects=False,
            timeout=15,
        )
        if is_waf_blocked(resp.text):
            raise RuntimeError("请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理（国内出口）后重试")
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取登录入口失败: {_sanitize_text(data.get('msg'))}")

        # 2. 跳转到 OAuth 页面，解析 RSA 公钥与 page_use
        resp = self.session.get(data["data"]["Data"], allow_redirects=True, timeout=15)

        # 检查是否被 WAF 拦截
        if is_waf_blocked(resp.text):
            raise RuntimeError("请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理（国内出口）后重试")

        page_use_match = re.compile(r"page_use ?= ?[\'|\"]([a-zA-Z0-9-_]+)[\'|\"]").findall(
            resp.text
        )
        key_match = re.compile(r'id="key"\s+value="([^"]+)"').findall(resp.text)
        if not page_use_match or not key_match:
            # 只落响应摘要（前 300 字符），避免整页 HTML/敏感内容进日志
            body_preview = resp.text[:300].replace("\n", "\\n")
            logger.error(f"[{self.account.phone}] OAuth 页解析失败诊断:")
            logger.error(f"  最终 URL: {resp.url}")
            logger.error(f"  状态码: {resp.status_code}")
            logger.error(f"  响应长度: {len(resp.text)}")
            logger.error(f"  响应前300字符: {body_preview}")
            logger.error(f"  page_use 命中: {len(page_use_match)}, key 命中: {len(key_match)}")
            if is_waf_blocked(resp.text):
                logger.error("  检测到 WAF 风控拦截特征，通常是 GitHub Actions 海外 IP 被易班风控")
            logger.error(
                "  若响应为 WAF 挑战页/拦截页，通常是 GitHub Actions 海外 IP 被易班风控，"
                "请配置 YIBAN_PROXY 代理（国内出口）后重试。"
            )
            raise RuntimeError("登录页面解析失败（page_use / RSA key 未找到），详见上方诊断日志")

        cipher = PKCS1_v1_5.new(RSA.importKey(key_match[0]))
        self.session.headers.update(
            Referer=resp.url,
            Origin="https://oauth.yiban.cn",
        )

        # 3. 提交账号密码
        resp = self.session.post(
            "https://oauth.yiban.cn/code/usersure",
            params={"ajax_sign": page_use_match[0]},
            data=urlencode(
                {
                    "oauth_uname": self.account,
                    "oauth_upwd": self._rsa_encrypt(cipher),
                    "client_id": "95626fa3080300ea",
                    "redirect_uri": "https://f.yiban.cn/iapp7463",
                    "state": "",
                    "scope": "1,2,3,4,",
                    "display": "html",
                }
            ),
            allow_redirects=False,
            timeout=15,
        )
        # 先检测 WAF 拦截再做 JSON 解析（拦截页是 HTML，直接 json() 会抛解析异常）
        if is_waf_blocked(resp.text):
            raise RuntimeError("请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理（国内出口）后重试")
        result = resp.json()

        if "reUrl" not in result:
            body_preview = resp.text[:300].replace("\n", "\\n")
            logger.error(f"[{self.account.phone}] usersure 响应无 reUrl 字段，诊断:")
            logger.error(f"  状态码: {resp.status_code}")
            logger.error(f"  响应前300字符: {body_preview}")
            raise RuntimeError(f"登录响应异常（无 reUrl）: {_sanitize_text(result)}")
        if "error" in result.get("reUrl", ""):
            raise RuntimeError(f"登录失败（账号或密码错误）: {self.account.phone}")

        # 4. 跳转回 f.yiban.cn，可能遇到 ydclearance 反爬
        self.session.headers.update(Referer="https://oauth.yiban.cn")
        resp = self.session.get(result["reUrl"], allow_redirects=False, timeout=15)

        if self._is_ydclearance_challenge(resp):
            # 纯 Python 解析挑战（不执行任何远程 JS），得出 cookie 与跳转路径
            clearance = self._solve_ydclearance(resp.text)
            cookies = dict_from_cookiejar(self.session.cookies)
            cookies["https_ydclearance"] = clearance[0]
            self.session.cookies = cookiejar_from_dict(cookies)
            self.session.headers.update(Referer=resp.url, Origin="https://f.yiban.cn")
            target = clearance[1]
            if not target.startswith("http"):
                target = "https://f.yiban.cn" + target
            resp = self.session.get(target, allow_redirects=False, timeout=15)
            self.session.headers.update(Referer=resp.url)
        else:
            self.session.headers.update(Referer=resp.url, Origin="https://f.yiban.cn")

        # 5. 获取 verify_request
        location = resp.headers.get("Location", "")
        if not location:
            raise RuntimeError(
                f"获取 verify_request 失败: 上一步响应缺少 Location 头"
                f"（状态码 {resp.status_code}，响应长度 {len(resp.text)}）"
            )
        resp = self.session.get(location, allow_redirects=False, timeout=15)
        verify_match = re.compile(r"verify_request=([^&]+)&?").findall(
            resp.headers.get("Location", "")
        )
        if not verify_match:
            raise RuntimeError(
                f"获取 verify_request 失败: 重定向响应缺少 verify_request 参数"
                f"（状态码 {resp.status_code}，响应长度 {len(resp.text)}）"
            )
        verify_code = verify_match[0]

        # 6. 完成登录
        self.session.headers.update(
            Referer="https://c.uyiban.com/",
            Origin="https://c.uyiban.com",
        )
        self.session.get(
            "https://api.uyiban.com/base/c/auth/yiban",
            params={"verifyRequest": verify_code, "CSRF": self.csrf},
            cookies={},
            allow_redirects=False,
            timeout=15,
        )

        cookies = dict_from_cookiejar(self.session.cookies)
        if "csrf_token" not in cookies:
            raise RuntimeError("登录失败：未获取到 csrf_token")

        self.logged_in = True
        logger.info(f"[{self.account.phone}] 登录成功")

    # ---- KillYiBan 同款登录（默认登录方式，e003 修复）----
    def login_killyiban(self):
        """完全复刻反编译 KillYiBan (p101w2/b.java) 的登录流程。

        差异点（vs 原 login）：
        - 入口直接打 oauth.yiban.cn/code/html（不先打 api.uyiban.com）
        - usersure 请求不带 Referer/Origin（原 App 传空 headers）
        - scope 传空、display 传 "authorize"（原 App 实值）
        - 成功标志判断 code == "s200"（原 App 判断方式）
        - CSRF 为 SecureRandom 真随机
        - 页面解析用 jsoup 等价正则（key 去掉 BEGIN/END 后按 X509 解码）
        """
        phone = self.account.phone
        # 设置 csrf_token cookie（服务器用其校验 CSRF 参数，缺失会报 CSRF invalid）
        self.session.cookies = cookiejar_from_dict({"csrf_token": self.csrf})
        # session 头已是 KILLYIBAN_HEADERS，此处无需再改

        # 1. 打开 OAuth 登录页（client_id/redirect_uri 参数，无 CSRF）
        #    注意：不跟随重定向，直接取登录页 HTML（diag 实测 allow_redirects=False 返回 200 登录页）
        resp = self.session.get(
            "https://oauth.yiban.cn/code/html",
            params={"client_id": "95626fa3080300ea", "redirect_uri": "https://f.yiban.cn/iapp7463"},
            allow_redirects=False,
            timeout=15,
        )
        # 若直接返回 iapp7463 说明已登录（正常流程是停留在登录页）
        if "iapp7463" in (resp.headers.get("Location", "")):
            logger.info(f"[{phone}] 登录: 已登录状态（无需提交）")
            self.logged_in = True
            return

        # 2. 解析 RSA 公钥与 page_use（jsoup input#key 等价正则）
        key_match = re.compile(r'<input[^>]*id="key"[^>]*value="([^"]+)"').findall(resp.text)
        page_use_match = re.compile(r"var page_use = '([^']+)'").findall(resp.text)
        if not key_match or not page_use_match:
            logger.error(
                f"[{phone}] 登录 OAuth 页解析失败（key={len(key_match)}, page_use={len(page_use_match)}）"
            )
            raise RuntimeError("登录: OAuth 页解析失败")
        # key 去掉 PEM 头尾后按 X509 解码
        key_b64 = re.compile(r"\s+").sub(
            "",
            key_match[0]
            .replace("-----BEGIN PUBLIC KEY-----", "")
            .replace("-----END PUBLIC KEY-----", ""),
        )
        cipher = PKCS1_v1_5.new(RSA.import_key(b64decode(key_b64)))

        # 3. 提交账号密码（实测：usersure 必须不带 Origin/Referer 才返回 s200；
        #    带 Origin → e001"无效的应用端编号"；scope 空 + display=authorize 与 App 一致）
        resp = self.session.post(
            "https://oauth.yiban.cn/code/usersure",
            params={"ajax_sign": page_use_match[0]},
            headers={
                "User-Agent": "Yiban",
                "AppVersion": "5.1.2",
                "Origin": None,
                "Referer": None,
                "X-Requested-With": None,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            data=urlencode(
                {
                    "oauth_uname": phone,
                    "oauth_upwd": self._rsa_encrypt(cipher),
                    "client_id": "95626fa3080300ea",
                    "redirect_uri": "https://f.yiban.cn/iapp7463",
                    "state": "",
                    "scope": "",
                    "display": "authorize",
                }
            ),
            allow_redirects=False,
            timeout=15,
        )
        # 先检测 WAF 拦截再做 JSON 解析（拦截页是 HTML，直接 json() 会抛解析异常）
        if is_waf_blocked(resp.text):
            raise RuntimeError("请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理（国内出口）后重试")
        result = resp.json()
        # App 用 code == "s200" 判断成功
        if result.get("code") != "s200":
            raise RuntimeError(f"登录失败: {_sanitize_text(result.get('msgCN', result))}")

        # 4. 打开 iframe/index 获取 Location → verify_request（默认三个头）
        resp = self.session.get(
            "https://f.yiban.cn/iframe/index",
            params={"act": "iapp7463"},
            allow_redirects=False,
            timeout=15,
        )
        location = resp.headers.get("Location", "")
        verify_match = re.compile(r"verify_request=(.*?)&").findall(location)
        if not verify_match:
            # Location 的 query 中含 verify_request 令牌，错误消息只留 host/path
            loc_desc = ""
            try:
                parts = urlsplit(location)
                if parts.scheme:
                    loc_desc = f"{parts.scheme}://{parts.netloc}{parts.path}"
                else:
                    loc_desc = parts.path
            except ValueError:
                loc_desc = "<无法解析>"
            raise RuntimeError(f"无法提取 verify_request（Location={loc_desc}）")

        # 5. 完成认证（默认三个头 + 跟随重定向，最终返回 JSON）
        resp = self.session.get(
            "https://api.uyiban.com/base/c/auth/yiban",
            params={"verifyRequest": verify_match[0], "CSRF": self.csrf},
            allow_redirects=True,
            timeout=15,
        )
        if is_waf_blocked(resp.text):
            raise RuntimeError("请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理（国内出口）后重试")
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"最终认证失败: {_sanitize_text(data.get('msg'))}")
        self.logged_in = True
        logger.info(f"[{phone}] 登录成功")

    def _is_ydclearance_challenge(self, resp):
        """判断响应是否触发 ydclearance 反爬挑战。

        特征判定（不依赖响应长度）：
        - Set-Cookie 已下发 https_ydclearance（说明已过挑战）；
        - 或响应包含挑战 JS 特征（window.onload=setTimeout + eval("qo=eval;qo(po);")）。
        """
        if "https_ydclearance" in resp.headers.get("Set-Cookie", ""):
            return True
        return "window.onload=setTimeout" in resp.text and 'eval("qo=eval;qo(po);")' in resp.text

    def _solve_ydclearance(self, text):
        """纯 Python 解析易盾 WAF（https_ydclearance）挑战，不执行任何远程 JS。

        挑战模板固定（易盾 WAF v1，f.yiban.cn 与 kuaidaili/89ip 同款）：
        `oo` 十六进制字节数组 + 三步固定变换（取反+旋转-常量、逆向差分、
        加常量+旋转），最后跳过 `qo % K` 的下标、逐字节异或挑战参数拼出
        `po` 字符串（含 cookie 赋值与跳转路径）。各步数值常量随挑战变化，
        用正则从 JS 中提取后在 Python 中复刻运算；任何一步提取失败都抛
        明确错误，绝不 eval 远程代码。
        """
        fn_m = re.compile(r"(function ([a-z]{2,})\(.+) ?</script>").findall(text)
        if not fn_m:
            raise RuntimeError("ydclearance 挑战解析失败: 未找到挑战函数")
        js_code = fn_m[0][0]
        if 'eval("qo=eval;qo(po);")' not in js_code:
            raise RuntimeError("ydclearance 挑战解析失败: 模板特征缺失（eval qo/po 未找到）")

        # 挑战参数：window.onload=setTimeout("<fn>(<arg>)", 200)
        arg_m = re.compile(r'window\.onload=setTimeout\("' + fn_m[0][1] + r"\(([0-9]+).+").findall(
            text
        )
        if not arg_m:
            raise RuntimeError("ydclearance 挑战解析失败: 未找到挑战参数")
        arg = int(arg_m[0])

        # oo 字节数组
        arr_m = re.compile(r"oo = (\[[0-9a-fA-Fx,\s]+?\])").findall(js_code)
        if not arr_m:
            raise RuntimeError("ydclearance 挑战解析失败: 未找到 oo 数组")
        oo = [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]+)", arr_m[0])]
        if len(oo) < 4:
            raise RuntimeError("ydclearance 挑战解析失败: oo 数组过短")

        # 变换 A（尾部到头部）：取反 → 旋转 → 减常量
        ta = re.search(
            r'"qo=(\d+); do\{oo\[qo\]=\(-oo\[qo\]\)&0xff;(.+?)\} while\(--qo>=2\);',
            js_code,
        )
        if not ta:
            raise RuntimeError("ydclearance 挑战解析失败: 变换 A 未找到")
        n_a = int(ta.group(1))
        ta_num = re.search(r">>(\d+)", ta.group(2))
        ta_shift_l = re.search(r"<<(\d+)", ta.group(2))
        ta_sub = re.search(r"-(\d+)\)&0xff", ta.group(2))
        if not (ta_num and ta_shift_l and ta_sub):
            raise RuntimeError("ydclearance 挑战解析失败: 变换 A 常量未找到")
        for i in range(n_a, 1, -1):
            oo[i] = (-oo[i]) & 0xFF
            oo[i] = (
                ((oo[i] >> int(ta_num.group(1))) | ((oo[i] << int(ta_shift_l.group(1))) & 0xFF))
                - int(ta_sub.group(1))
            ) & 0xFF

        # 变换 B（尾部到头部）：逆向差分
        tb = re.search(r"qo = (\d+); do \{ oo\[qo\] = \(oo\[qo\] - oo\[qo - 1\]\)", js_code)
        if not tb:
            raise RuntimeError("ydclearance 挑战解析失败: 变换 B 未找到")
        for i in range(int(tb.group(1)), 2, -1):
            oo[i] = (oo[i] - oo[i - 1]) & 0xFF

        # 变换 C（头部到尾部）：加常量 → 旋转
        tc = re.search(r"if \(qo > (\d+)\) break; oo\[qo\] = (.+?); qo\+\+", js_code)
        if not tc:
            raise RuntimeError("ydclearance 挑战解析失败: 变换 C 未找到")
        tc_num = re.search(r"\+ (\d+)\) & 0xff\) \+ (\d+)\) & 0xff\) << (\d+)", tc.group(2))
        tc_shift_r = re.search(r">> (\d+)\)", tc.group(2))
        if not (tc_num and tc_shift_r):
            raise RuntimeError("ydclearance 挑战解析失败: 变换 C 常量未找到")
        n_c = int(tc.group(1))
        tc_add1, tc_add2, tc_shift_l = (int(x) for x in tc_num.groups())
        for i in range(1, n_c + 1):
            v = (oo[i] + tc_add1) & 0xFF
            v = (v + tc_add2) & 0xFF
            oo[i] = ((v << tc_shift_l) & 0xFF) | (v >> int(tc_shift_r.group(1)))

        # 拼 po：跳过 qo % K 的下标，逐字节异或挑战参数
        tk = re.search(r"if \(qo % (\d+)\) po \+= String\.fromCharCode\(oo\[qo\] \^ [A-Za-z_]+\)", js_code)
        if not tk:
            raise RuntimeError("ydclearance 挑战解析失败: po 拼接逻辑未找到")
        k = int(tk.group(1))
        po = "".join(chr(oo[i] ^ arg) for i in range(1, n_c + 1) if i % k)

        cookie_m = re.compile(r"https?_ydclearance=([0-9a-zA-Z-_]+);?").findall(po)
        path_m = re.compile(r'window\.document\.location="(.+)"').findall(po)
        if not cookie_m or not path_m:
            raise RuntimeError("ydclearance 挑战解析失败: 解码结果中未提取到 cookie/跳转路径")
        return cookie_m[0], path_m[0]

    # ---- 签到 -------------------------------------------------------------
    def signin(self):
        """执行签到，返回 (success: bool, message: str, skip: bool)。

        skip=True 表示当前不在签到时间窗口内，不需要重试。
        """
        if not self.logged_in:
            # 与 attempt_signin 保持一致：按配置选择登录流程（防止直接调 signin() 时走错）
            if self.use_killyiban:
                self.login_killyiban()
            else:
                self.login()

        # 1. 获取签到位置范围
        if not self.use_killyiban:
            self.session.headers.update(
                Origin="https://app.uyiban.com", Referer="https://app.uyiban.com/"
            )
        resp = self.session.get(
            "https://api.uyiban.com/nightAttendance/student/index/signPosition",
            params={"CSRF": self.csrf},
            allow_redirects=False,
            timeout=15,
        )
        if is_waf_blocked(resp.text):
            return False, "请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理（国内出口）后重试", False
        data = resp.json()
        if data.get("code") != 0:
            return False, f"获取签到任务失败: {_sanitize_text(data.get('msg'))}", False

        data_obj = data["data"]
        msg = data_obj.get("Msg", "")
        if "已签到" in msg:
            return True, "今日已签到（无需重复签到）", False
        if "今日无需签到" in msg:
            return True, "今日无需签到（非签到日）", True

        position_list = data_obj.get("Position", [])
        if not position_list:
            return False, "未找到签到位置数据", False
        position = position_list[0]
        range_obj = data_obj.get("Range", {})

        # 2. 校验签到时间
        now_ts = int(datetime.now().timestamp())
        start_ts = int(range_obj.get("StartTime", 0))
        end_ts = int(range_obj.get("EndTime", 0))
        if not start_ts or not end_ts:
            # 签到时间窗口缺失（Range 为空），视为 skip，不直接提交
            return False, "签到时间窗口缺失（无 Range），已跳过", True
        if not (start_ts <= now_ts <= end_ts):
            # 不在签到时间窗口内，标记为 skip（不需要重试）
            return (
                False,
                f"未在签到时间内（{datetime.fromtimestamp(start_ts)} ~ {datetime.fromtimestamp(end_ts)}）",
                True,
            )

        # 3. 解析多边形点
        points_raw = position.get("Points", [])
        polygon = []
        for p in points_raw:
            parts = p.split(",")
            if len(parts) >= 2:
                polygon.append((float(parts[0]), float(parts[1])))

        if not polygon:
            return False, "签到范围点解析失败", False

        # 4. 在多边形内生成随机点
        lng, lat = generate_position_in_polygon(polygon)
        logger.info(
            f"[{self.account.phone}] 生成定位: ({lng},{lat}) 地址: {position.get('Address', '')}"
        )

        # 5. 构建签到数据并提交
        sign_info = {
            "Reason": "",
            "AttachmentFileName": "",
            "LngLat": f"{lng},{lat}",
            "Address": position.get("Address", ""),
        }
        if not self.phone_model or not self.phone_code:
            logger.warning(
                f"[{self.account.phone}] 未配置设备信息（YIBAN_PHONE_MODEL/YIBAN_PHONE_CODE），"
                "如学校开启了设备绑定，签到将失败"
            )
        resp = self.session.post(
            "https://api.uyiban.com/nightAttendance/student/index/signIn",
            params={"CSRF": self.csrf},
            data={
                "Code": self.phone_code,
                "PhoneModel": self.phone_model,
                "SignInfo": json.dumps(sign_info, ensure_ascii=False),
                # KillYiBan 用 MINI_VERSION="1"，原脚本用 "1.0"
                "OutState": "1" if self.use_killyiban else "1.0",
            },
            allow_redirects=False,
            timeout=15,
        )
        if is_waf_blocked(resp.text):
            return False, "请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理（国内出口）后重试", False
        result = resp.json()
        if result.get("code") == 0 and result.get("data"):
            return True, "签到成功", False
        err_msg = _sanitize_text(result.get("msg", "未知错误"))
        if "授权设备" in err_msg:
            err_msg += "（请配置 YIBAN_PHONE_MODEL 和 YIBAN_PHONE_CODE 环境变量）"
        return False, f"签到失败: {err_msg}", False


# ---------------------------------------------------------------------------
# 消息通知
# ---------------------------------------------------------------------------
def send_notification(title, content, url):
    """通过 Server 酱 / Bark / 企业微信等 webhook 发送通知。"""
    if not url:
        return
    try:
        if url.startswith("http"):
            requests.post(url, json={"title": title, "content": content}, timeout=10)
            logger.info("通知发送成功")
    except Exception as e:
        logger.warning(f"通知发送失败: {e}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def attempt_signin(account, notify_url=None):
    """单次签到尝试（登录 + 签到），不重试。

    返回 (success, message, skip)：
    - success: 是否成功（含"已签到""非签到日"）
    - message: 结果说明
    - skip: True 表示窗口外等无需重试的情况
    """
    phone = account.phone
    try:
        client = YibanClient(account)
        if client.use_killyiban:
            client.login_killyiban()
        else:
            client.login()
        return client.signin()
    except Exception as e:
        logger.error(f"[{phone}] ❌ 尝试失败: {e}")
        logger.debug(traceback.format_exc())
        # 逐次失败不通知（避免通知风暴），仅最终放弃时由 run_queue_retry 通知一次
        return False, str(e), False


def run_queue_retry(accounts, notify_url, start_delay_max, gap_max):
    """轮询队列 + 分散重试执行全部账号签到。

    流程：启动随机延迟 → 按签到模式（列表顺序 / 列表随机）确定执行顺序逐个尝试
    （账号间随机间隔）；失败的账号不立即重试，放入队尾等待下一轮；
    每账号总尝试次数受 classify_failure 分级控制（风控类最多 2 次，其他最多 4 次）；
    同一账号两次尝试间隔不小于 RETRY_MIN_INTERVAL 秒，避免连击。

    返回结果字典 {手机号: (success, message, skip)}。
    """
    random_delay(start_delay_max, "启动延迟")
    queue = list(accounts)
    if SIGN_MODE == "random":
        # 列表随机模式：每次运行打乱顺序（打破"固定顺序+固定时刻"的脚本指纹）
        random.shuffle(queue)
        logger.info(f"签到模式: 列表随机（顺序已打散，共 {len(queue)} 个账号）")
    else:
        logger.info(f"签到模式: 列表顺序（共 {len(queue)} 个账号）")
    attempts = {acc.phone: 0 for acc in accounts}
    results = {}
    first_round = True

    while queue:
        acc = queue.pop(0)
        phone = acc.phone
        # 首轮（第一个账号）不等待，后续每个账号（含重试回队）先打散账号间间隔；
        # 标记在 pop 之后无条件置 False（原实现只在失败分支置 False，
        # 导致全成功路径账号间隔打散失效）
        if not first_round:
            random_delay(gap_max, f"账号 {phone} 间隔")
        first_round = False
        attempts[phone] += 1
        logger.info(f"[{phone}] 🔄 第 {attempts[phone]} 次尝试")

        success, message, skip = attempt_signin(acc, notify_url)

        if success:
            results[phone] = (True, message, skip)
            logger.info(f"[{phone}] ✅ {message}")
            continue

        # 失败：跳过类不重试；其余按分级放回队尾
        if skip:
            results[phone] = (False, message, True)
            logger.info(f"[{phone}] ➖ {message}（不重试）")
            continue

        max_attempts, _ = classify_failure(message)
        if attempts[phone] >= max_attempts:
            results[phone] = (False, message, False)
            logger.error(f"[{phone}] ❌ 已尝试 {attempts[phone]} 次，放弃: {message}")
            if notify_url:
                send_notification(
                    "易班签到失败", f"账号: {phone}\n原因: {_sanitize_text(message)}", notify_url
                )
            continue

        # 放回队尾：先固定补足最短间隔（可为 0），再打散一段随机延迟，
        # 总等待 = remaining + uniform(0, RETRY_GAP_MAX)，避免总间隔可能为 0
        remaining = max(0, RETRY_MIN_INTERVAL - gap_max)
        time.sleep(remaining)
        random_delay(RETRY_GAP_MAX, f"账号 {phone} 重试前等待")
        queue.append(acc)
        logger.warning(f"[{phone}] ⏳ 待重试（已 {attempts[phone]} 次，上限 {max_attempts} 次）")

    return results


def main():
    """主函数：加载账号配置并执行签到。

    支持：
    - 配置文件 accounts.json / YIBAN_ACCOUNTS_JSON（推荐，一次输入一个账号完整信息）
    - 旧格式 YIBAN_ACCOUNTS 或 YIBAN_PHONE/YIBAN_PASSWORD（向后兼容）
    - 队列重试：账号顺序执行，失败账号放队尾分散重试（分级上限）
    - 随机延迟：YIBAN_START_DELAY_MAX（启动）/ YIBAN_ACCOUNT_GAP_MAX（账号间隔）
    - --only 指定手机号（逗号分隔），仅供 TUI 手动签到单个账号
    - --check-config 仅检查配置，不发任何网络请求
    """
    parser = argparse.ArgumentParser(description="易班自动签到")
    parser.add_argument(
        "--check-config", action="store_true", help="仅检查账号配置（脱敏打印），不发起任何网络请求"
    )
    parser.add_argument(
        "--only", default="", help="仅签到指定手机号（逗号分隔，用于 TUI 手动签到）"
    )
    args = parser.parse_args()

    notify_url = os.environ.get("YIBAN_NOTIFY_URL", "")

    # 加载账号配置（文件 > JSON 环境变量 > 旧格式，详见 load_accounts）
    try:
        accounts = load_accounts()
    except RuntimeError as e:
        logger.error(f"配置加载失败: {e}")
        sys.exit(1)

    if not accounts:
        logger.error("未配置任何账号，请通过以下任一方式配置：")
        logger.error("  1. accounts.json 文件（推荐，用 TUI 配置工具生成）")
        logger.error("  2. YIBAN_ACCOUNTS_JSON 环境变量（JSON 数组）")
        logger.error("  3. YIBAN_ACCOUNTS 环境变量（旧格式 phone:password#phone2:password2）")
        logger.error("  4. YIBAN_PHONE / YIBAN_PASSWORD 环境变量（单账号）")
        sys.exit(1)

    # --only 过滤：只保留指定手机号（TUI 手动签到单个账号）
    if args.only:
        only_set = {p.strip() for p in args.only.split(",") if p.strip()}
        accounts = [a for a in accounts if a.phone in only_set]
        if not accounts:
            logger.error(f"--only 指定账号不在配置中: {args.only}")
            sys.exit(1)

    # 仅检查配置模式：不发任何网络请求，用于部署验证
    if args.check_config:
        print_config_summary(accounts)
        sys.exit(0)

    # 随机延迟（TUI 设置栏可开关；默认关闭，不影响现有行为）
    start_delay_max = parse_env_int("YIBAN_START_DELAY_MAX", 0)
    gap_max = parse_env_int("YIBAN_ACCOUNT_GAP_MAX", 0)

    logger.info(f"==== 开始执行签到，共 {len(accounts)} 个账号，队列重试模式 ====")
    # 状态文件以"尝试开始时刻"的日期命名（防跨午夜执行写错当天）
    attempt_date = datetime.now().strftime("%Y-%m-%d")
    results = run_queue_retry(accounts, notify_url, start_delay_max, gap_max)

    # 汇总（按 accounts 原始顺序展示）
    logger.info("==== 签到汇总 ====")
    has_real_failure = False
    has_executed = False
    for acc in accounts:
        success, msg, skip = results.get(acc.phone, (False, "未执行", False))
        status = "✅" if success else "❌"
        logger.info(f"  {status} {acc.phone}: {msg}")
        if not success and not skip:
            has_real_failure = True
        if not skip:
            has_executed = True

    # 写按日状态文件（供网页日历组件读取；skip=未在签到时间/非签到日跳过则不写，当天留空）
    state_dir = os.environ.get("YIBAN_STATE_DIR", "/var/log/yiban")
    try:
        os.makedirs(state_dir, exist_ok=True)
        daily_path = os.path.join(state_dir, f"sign-daily-{attempt_date}.json")
        daily = {}
        if os.path.exists(daily_path):
            with open(daily_path, encoding="utf-8") as f:
                daily = json.load(f)
        for acc in accounts:
            success, msg, skip = results.get(acc.phone, (False, "未执行", False))
            if not skip:
                daily[acc.phone] = "✅" if success else "❌"
        with open(daily_path, "w", encoding="utf-8") as f:
            json.dump(daily, f, ensure_ascii=False)
    except OSError as e:
        logger.warning("写入按日状态文件失败: %s", e)

    # 退出码（run.sh 依据退出码写状态文件）：
    # 0 - 全部成功（有实际签到执行；含"已签到""窗口外部分跳过但至少执行过"）
    # 1 - 有真正的失败（登录失败、签到失败等）
    # 2 - 全部 skip（无实际执行：全部未在签到时间内/非签到日/窗口缺失），
    #     由 run.sh 写 SKIPPED 而非 SUCCESS，避免备份等下游任务被吞
    if has_real_failure:
        sys.exit(1)
    if not has_executed:
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
