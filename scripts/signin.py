#!/usr/bin/env python3
"""
易班自动签到脚本

功能：
1. 自动登录易班（支持多账号）
2. 自动获取签到任务范围
3. 在签到范围内生成随机定位点（模拟真实定位）
4. 自动提交签到
5. 支持消息通知（Server 酱、Bark、企业微信等）
6. 重试逻辑：遇到 WAF 拦截时自动重试（指数退避）
7. 随机延迟：启动时随机延时，避免并发触发风控

参考项目：
- 本地 KillYiBan 模块（nightAttendance 签到流程）
- Auto-Test 项目（登录流程）
"""

import os
import sys
import json
import random
import logging
import argparse
import traceback
import time
from dataclasses import dataclass
from hashlib import md5
from datetime import datetime
from re import compile
from base64 import b64encode
from urllib.parse import urlencode

import requests
from requests.utils import cookiejar_from_dict, dict_from_cookiejar
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

try:
    from js2py import eval_js
    HAS_JS2PY = True
except ImportError:
    HAS_JS2PY = False


# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
# 支持通过环境变量调整日志级别：DEBUG / INFO / WARNING / ERROR
LOG_LEVEL = os.environ.get('YIBAN_LOG_LEVEL', 'DEBUG').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.DEBUG),
    format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('yiban')


# 易班 iOS 客户端 UA（与 Auto-Test 保持一致）
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) '
                  'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/4.0 '
                  'Chrome/104.0.5112.97 Mobile Safari/537.36 yiban_iOS/5.0.12',
    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
    'X-Requested-With': 'com.yiban.app',
    'Origin': 'https://app.uyiban.com',
    'Referer': 'https://app.uyiban.com/',
    'Connection': 'close',
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
    phone_model: str = ''  # 设备型号（学校开启"设备绑定"时必填）
    phone_code: str = ''   # 设备唯一识别码（学校开启"设备绑定"时必填）

    @property
    def has_device_info(self):
        return bool(self.phone_model and self.phone_code)


# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------
# 重试配置
MAX_RETRIES = 3  # 最大重试次数
RETRY_BASE_DELAY = 5  # 基础延迟（秒）
RETRY_MAX_DELAY = 60  # 最大延迟（秒）

# 账号配置文件（默认当前目录，可用 YIBAN_ACCOUNTS_FILE 覆盖）
ACCOUNTS_FILE = os.environ.get('YIBAN_ACCOUNTS_FILE', 'accounts.json')

# WAF 风控关键词（用于判断是否被拦截）
WAF_KEYWORDS = ['风险访问', '风控', '访问服务禁用', 'WAF', '拦截']


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
        if ((yi > y) != (yj > y)) and \
                (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
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
        ((p[0] - center_lng) * 0.7 + center_lng,
         (p[1] - center_lat) * 0.7 + center_lat)
        for p in polygon_points
    ]

    for _ in range(100):
        lng = center_lng + (max_lng - min_lng) * 0.2 * (random.random() - 0.5)
        lat = center_lat + (max_lat - min_lat) * 0.2 * (random.random() - 0.5)
        if point_in_polygon(lng, lat, scaled_points) and \
                point_in_polygon(lng, lat, polygon_points):
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
    decoded = compile(r'\\u([0-9a-fA-F]{4})').sub(
        lambda m: chr(int(m.group(1), 16)), response_text
    )
    for keyword in WAF_KEYWORDS:
        if keyword in response_text or keyword in decoded:
            return True
    return False


def retry_with_backoff(func, *args, **kwargs):
    """带指数退避的重试装饰器。"""
    last_exception = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exception = e
            if attempt < MAX_RETRIES:
                delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                delay += random.uniform(0, delay * 0.3)  # 添加抖动
                logger.warning(f'第 {attempt + 1} 次尝试失败，{int(delay)} 秒后重试: {e}')
                time.sleep(delay)
            else:
                logger.error(f'已重试 {MAX_RETRIES} 次，放弃')
    raise last_exception


# ---------------------------------------------------------------------------
# 账号配置加载
# ---------------------------------------------------------------------------
def _parse_account_dict(data):
    """将账号 JSON 对象解析为 Account，校验必填字段。"""
    phone = str(data.get('phone') or data.get('account') or '').strip()
    password = str(data.get('password') or data.get('pwd') or '').strip()
    if not phone or not password:
        raise ValueError(f'账号配置缺少必填字段: {data}')
    return Account(
        phone=phone,
        password=password,
        phone_model=str(data.get('phone_model') or '').strip(),
        phone_code=str(data.get('phone_code') or '').strip(),
    )


def _load_accounts_from_file():
    """从 accounts.json 文件加载（服务器端 TUI 配置工具生成）。"""
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE, encoding='utf-8') as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f'账号配置文件 {ACCOUNTS_FILE} 解析失败: {e}')
    if not isinstance(raw, list):
        raise RuntimeError(f'账号配置文件 {ACCOUNTS_FILE} 应为 JSON 数组')
    accounts = [_parse_account_dict(item) for item in raw]
    logger.info(f'已从 {ACCOUNTS_FILE} 加载 {len(accounts)} 个账号')
    return accounts


def _load_accounts_from_json_env():
    """从 YIBAN_ACCOUNTS_JSON 环境变量加载（JSON 数组字符串，供 CI 使用）。"""
    raw = os.environ.get('YIBAN_ACCOUNTS_JSON', '').strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f'YIBAN_ACCOUNTS_JSON 不是合法 JSON: {e}')
    if not isinstance(data, list):
        raise RuntimeError('YIBAN_ACCOUNTS_JSON 应为 JSON 数组')
    accounts = [_parse_account_dict(item) for item in data]
    logger.info(f'已从 YIBAN_ACCOUNTS_JSON 加载 {len(accounts)} 个账号')
    return accounts


def _load_accounts_from_legacy_env():
    """旧格式兼容：YIBAN_ACCOUNTS（phone:password#...）与 YIBAN_PHONE/YIBAN_PASSWORD。"""
    accounts = []
    accounts_str = os.environ.get('YIBAN_ACCOUNTS', '')
    for item in accounts_str.split('#'):
        item = item.strip()
        if not item:
            continue
        if ':' not in item:
            logger.error(f'账号配置格式错误（应为 phone:password）: {item}')
            continue
        phone, pwd = item.split(':', 1)
        accounts.append(Account(phone.strip(), pwd.strip()))
    if not accounts:
        phone = os.environ.get('YIBAN_PHONE', '').strip()
        pwd = os.environ.get('YIBAN_PASSWORD', '').strip()
        if phone and pwd:
            accounts.append(Account(phone, pwd))
    return accounts


def _apply_global_device_info(accounts):
    """账号未配置设备信息时，回退到全局环境变量（兼容旧配置方式）。"""
    model = os.environ.get('YIBAN_PHONE_MODEL', '').strip()
    code = os.environ.get('YIBAN_PHONE_CODE', '').strip()
    if not (model and code):
        return accounts
    for acc in accounts:
        if not acc.has_device_info:
            acc.phone_model = model
            acc.phone_code = code
    return accounts


def load_accounts():
    """按优先级加载账号配置：文件 > JSON 环境变量 > 旧格式环境变量。"""
    for loader in (_load_accounts_from_file,
                   _load_accounts_from_json_env,
                   _load_accounts_from_legacy_env):
        accounts = loader()
        if accounts:
            return _apply_global_device_info(accounts)
    return []


def parse_concurrency(value):
    """解析并发数：未设置/0/1/非法值均回退为顺序执行（1）。"""
    try:
        return max(1, int(str(value).strip()))
    except (TypeError, ValueError):
        logger.warning(f'YIBAN_CONCURRENCY 取值无效: {value!r}，已回退为顺序执行')
        return 1


def print_config_summary(accounts):
    """打印账号配置摘要（密码脱敏），不发任何网络请求。"""
    print('==== 账号配置检查 ====')
    for i, acc in enumerate(accounts, 1):
        if acc.has_device_info:
            device = f'设备: {acc.phone_model} / {acc.phone_code[:8]}...'
        else:
            device = '设备: 未配置（如学校开启设备绑定，签到将失败）'
        print(f'  {i}. {acc.phone} | 密码: {"*" * 8} | {device}')
    print(f'共 {len(accounts)} 个账号，配置检查通过。')


# ---------------------------------------------------------------------------
# 易班登录
# ---------------------------------------------------------------------------
class YibanClient:
    """易班客户端：封装登录与签到流程。"""

    def __init__(self, account):
        self.account = account
        self.password = account.password.encode('UTF-8')
        self.csrf = md5(str(datetime.now()).encode('UTF-8')).hexdigest()
        self.session = requests.Session()
        self.session.keep_alive = False
        self.session.headers = dict(HEADERS)
        # 代理配置：GitHub Actions 海外 IP 可能被易班 WAF 地域风控拦截
        proxy = os.environ.get('YIBAN_PROXY', '').strip()
        if proxy:
            self.session.proxies = {'http': proxy, 'https': proxy}
            logger.info(f'[{account.phone}] 已启用代理: {proxy}')
        else:
            logger.warning(f'[{account.phone}] 未配置 YIBAN_PROXY，如遇到 WAF 拦截请配置代理（国内出口）')
        # 设备信息：部分学校开启了"设备绑定"，签到时需校验设备型号和唯一识别码
        self.phone_model = account.phone_model
        self.phone_code = account.phone_code
        self.logged_in = False

    def login(self):
        """登录易班，成功返回 True，失败抛出异常。"""
        self.session.cookies = cookiejar_from_dict({'csrf_token': self.csrf})
        self.session.headers.update(
            Referer='https://c.uyiban.com/',
            Origin='https://c.uyiban.com',
        )

        # 1. 获取跳转 URL
        resp = self.session.get(
            'https://api.uyiban.com/base/c/auth/yiban',
            params={'CSRF': self.csrf},
            allow_redirects=False,
            timeout=15,
        )
        data = resp.json()
        if data.get('code') != 0:
            raise RuntimeError(f"获取登录入口失败: {data.get('msg')}")

        # 2. 跳转到 OAuth 页面，解析 RSA 公钥与 page_use
        resp = self.session.get(data['data']['Data'], allow_redirects=True, timeout=15)

        # 检查是否被 WAF 拦截
        if is_waf_blocked(resp.text):
            raise RuntimeError('请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理（国内出口）后重试')

        page_use_match = compile(r'page_use ?= ?[\'|\"]([a-zA-Z0-9-_]+)[\'|\"]').findall(resp.text)
        key_match = compile(r'id="key"\s+value="([^"]+)"').findall(resp.text)
        if not page_use_match or not key_match:
            body_preview = resp.text[:1500].replace('\n', '\\n')
            logger.error(f'[{self.account.phone}] OAuth 页解析失败诊断:')
            logger.error(f'  最终 URL: {resp.url}')
            logger.error(f'  状态码: {resp.status_code}')
            logger.error(f'  响应长度: {len(resp.text)}')
            logger.error(f'  响应前1500字符: {body_preview}')
            logger.error(f'  page_use 命中: {len(page_use_match)}, key 命中: {len(key_match)}')
            if is_waf_blocked(resp.text):
                logger.error('  检测到 WAF 风控拦截特征，通常是 GitHub Actions 海外 IP 被易班风控')
            logger.error('  若响应为 WAF 挑战页/拦截页，通常是 GitHub Actions 海外 IP 被易班风控，'
                         '请配置 YIBAN_PROXY 代理（国内出口）后重试。')
            raise RuntimeError('登录页面解析失败（page_use / RSA key 未找到），详见上方诊断日志')

        cipher = PKCS1_v1_5.new(RSA.importKey(key_match[0]))
        self.session.headers.update(
            Referer=resp.url,
            Origin='https://oauth.yiban.cn',
        )

        # 3. 提交账号密码
        resp = self.session.post(
            'https://oauth.yiban.cn/code/usersure',
            params={'ajax_sign': page_use_match[0]},
            data=urlencode({
                'oauth_uname': self.account,
                'oauth_upwd': b64encode(cipher.encrypt(self.password)),
                'client_id': '95626fa3080300ea',
                'redirect_uri': 'https://f.yiban.cn/iapp7463',
                'state': '',
                'scope': '1,2,3,4,',
                'display': 'html',
            }),
            allow_redirects=False,
            timeout=15,
        )
        result = resp.json()

        # 检查是否被 WAF 拦截
        if is_waf_blocked(resp.text):
            raise RuntimeError('请求被 WAF 风控拦截，请配置 YIBAN_PROXY 代理（国内出口）后重试')

        if 'reUrl' not in result:
            body_preview = resp.text[:1500].replace('\n', '\\n')
            logger.error(f'[{self.account.phone}] usersure 响应无 reUrl 字段，诊断:')
            logger.error(f'  状态码: {resp.status_code}')
            logger.error(f'  响应前1500字符: {body_preview}')
            raise RuntimeError(f'登录响应异常（无 reUrl）: {result}')
        if 'error' in result.get('reUrl', ''):
            raise RuntimeError(f'登录失败（账号或密码错误）: {self.account.phone}')

        # 4. 跳转回 f.yiban.cn，可能遇到 ydclearance 反爬
        self.session.headers.update(Referer='https://oauth.yiban.cn')
        resp = self.session.get(result['reUrl'], allow_redirects=False, timeout=15)

        if len(resp.text) > 10:  # 触发 ydclearance 反爬
            if not HAS_JS2PY:
                raise RuntimeError('遇到 ydclearance 反爬，需安装 js2py: pip install js2py')
            clearance = self._solve_ydclearance(resp.text)
            cookies = dict_from_cookiejar(self.session.cookies)
            cookies['https_ydclearance'] = clearance[0]
            self.session.cookies = cookiejar_from_dict(cookies)
            self.session.headers.update(Referer=resp.url, Origin='https://f.yiban.cn')
            resp = self.session.get(f'https://f.yiban.cn{clearance[1]}', allow_redirects=False, timeout=15)
            self.session.headers.update(Referer=resp.url)
        else:
            self.session.headers.update(Referer=resp.url, Origin='https://f.yiban.cn')

        # 5. 获取 verify_request
        resp = self.session.get(resp.headers['Location'], allow_redirects=False, timeout=15)
        verify_match = compile(r'verify_request=([^&]+)&?').findall(resp.headers.get('Location', ''))
        if not verify_match:
            raise RuntimeError('获取 verify_request 失败')
        verify_code = verify_match[0]

        # 6. 完成登录
        self.session.headers.update(
            Referer='https://c.uyiban.com/',
            Origin='https://c.uyiban.com',
        )
        self.session.get(
            'https://api.uyiban.com/base/c/auth/yiban',
            params={'verifyRequest': verify_code, 'CSRF': self.csrf},
            cookies={},
            allow_redirects=False,
            timeout=15,
        )

        cookies = dict_from_cookiejar(self.session.cookies)
        if 'csrf_token' not in cookies:
            raise RuntimeError('登录失败：未获取到 csrf_token')

        self.logged_in = True
        logger.info(f'[{self.account.phone}] 登录成功')

    def _solve_ydclearance(self, text):
        """解析 ydclearance 反爬 JS。"""
        result = compile(r'(function ([a-z]{2,})\(.+) ?</script>').findall(text)
        js_code = str(result[0][0])
        js_code = js_code.replace(r'eval("qo=eval;qo(po);");', r'return po;')
        js_code += '\n' + result[0][1] + '(' + \
            compile(r'window.onload=setTimeout\("' + result[0][1] + r'\(([0-9]+).+').findall(text)[0] + ');'
        evaluated = eval_js(js_code)
        return [
            compile(r'https?_ydclearance=([0-9a-zA-Z-_]+);?').findall(evaluated)[0],
            compile(r'window\.document\.location="(.+)"').findall(evaluated)[0],
        ]

    # ---- 签到 -------------------------------------------------------------
    def signin(self):
        """执行签到，返回 (success: bool, message: str, skip: bool)。

        skip=True 表示当前不在签到时间窗口内，不需要重试。
        """
        if not self.logged_in:
            self.login()

        # 1. 获取签到位置范围
        self.session.headers.update(Origin='https://app.uyiban.com', Referer='https://app.uyiban.com/')
        resp = self.session.get(
            'https://api.uyiban.com/nightAttendance/student/index/signPosition',
            params={'CSRF': self.csrf},
            allow_redirects=False,
            timeout=15,
        )
        data = resp.json()
        if data.get('code') != 0:
            return False, f"获取签到任务失败: {data.get('msg')}", False

        data_obj = data['data']
        msg = data_obj.get('Msg', '')
        if msg == '已签到':
            return True, '今日已签到（无需重复签到）', False
        if msg == '今日无需签到':
            return True, '今日无需签到（非签到日）', False

        position_list = data_obj.get('Position', [])
        if not position_list:
            return False, '未找到签到位置数据', False
        position = position_list[0]
        range_obj = data_obj.get('Range', {})

        # 2. 校验签到时间
        now_ts = int(datetime.now().timestamp())
        start_ts = int(range_obj.get('StartTime', 0))
        end_ts = int(range_obj.get('EndTime', 0))
        if start_ts and end_ts and not (start_ts <= now_ts <= end_ts):
            # 不在签到时间窗口内，标记为 skip（不需要重试）
            return False, f'未在签到时间内（{datetime.fromtimestamp(start_ts)} ~ {datetime.fromtimestamp(end_ts)}）', True

        # 3. 解析多边形点
        points_raw = position.get('Points', [])
        polygon = []
        for p in points_raw:
            parts = p.split(',')
            if len(parts) >= 2:
                polygon.append((float(parts[0]), float(parts[1])))

        if not polygon:
            return False, '签到范围点解析失败', False

        # 4. 在多边形内生成随机点
        lng, lat = generate_position_in_polygon(polygon)
        logger.info(f'[{self.account.phone}] 生成定位: ({lng},{lat}) 地址: {position.get("Address", "")}')

        # 5. 构建签到数据并提交
        sign_info = {
            'Reason': '',
            'AttachmentFileName': '',
            'LngLat': f'{lng},{lat}',
            'Address': position.get('Address', ''),
        }
        if not self.phone_model or not self.phone_code:
            logger.warning(f'[{self.account.phone}] 未配置设备信息（YIBAN_PHONE_MODEL/YIBAN_PHONE_CODE），'
                           '如学校开启了设备绑定，签到将失败')
        resp = self.session.post(
            'https://api.uyiban.com/nightAttendance/student/index/signIn',
            params={'CSRF': self.csrf},
            data={
                'Code': self.phone_code,
                'PhoneModel': self.phone_model,
                'SignInfo': json.dumps(sign_info, ensure_ascii=False),
                'OutState': '1.0',
            },
            allow_redirects=False,
            timeout=15,
        )
        result = resp.json()
        if result.get('code') == 0 and result.get('data'):
            return True, '签到成功', False
        err_msg = result.get('msg', '未知错误')
        if '授权设备' in err_msg:
            err_msg += '（请配置 YIBAN_PHONE_MODEL 和 YIBAN_PHONE_CODE 环境变量）'
        return False, f"签到失败: {err_msg}", False


# ---------------------------------------------------------------------------
# 消息通知
# ---------------------------------------------------------------------------
def send_notification(title, content, url):
    """通过 Server 酱 / Bark / 企业微信等 webhook 发送通知。"""
    if not url:
        return
    try:
        if url.startswith('http'):
            requests.post(url, json={'title': title, 'content': content}, timeout=10)
            logger.info('通知发送成功')
    except Exception as e:
        logger.warning(f'通知发送失败: {e}')


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def process_account(account, notify_url=None):
    """处理单个账号签到，带重试逻辑。"""
    phone = account.phone
    try:
        client = YibanClient(account)

        # 使用重试逻辑执行登录和签到
        def do_signin():
            client.login()
            return client.signin()

        success, message, skip = retry_with_backoff(do_signin)

        status = '✅' if success else '❌'
        logger.info(f'[{phone}] {status} {message}')
        if not success and notify_url and not skip:
            send_notification('易班签到失败', f'账号: {phone}\n原因: {message}', notify_url)
        return success, message, skip
    except Exception as e:
        logger.error(f'[{phone}] ❌ 异常: {e}')
        logger.debug(traceback.format_exc())
        if notify_url:
            send_notification('易班签到异常', f'账号: {phone}\n异常: {e}', notify_url)
        return False, str(e), False


def run_concurrent(accounts, notify_url, max_workers):
    """并发执行多账号签到（每个账号使用独立会话，互不干扰）。

    返回结果列表，顺序与传入的 accounts 一致，便于汇总展示。
    """
    from concurrent.futures import ThreadPoolExecutor

    results = [None] * len(accounts)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(process_account, acc, notify_url): idx
            for idx, acc in enumerate(accounts)
        }
        from concurrent.futures import as_completed
        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                phone = accounts[idx].phone
                logger.error(f'[{phone}] ❌ 并发执行异常: {e}')
                results[idx] = (False, str(e), False)
    return results


def main():
    """主函数：加载账号配置并执行签到。

    支持：
    - 配置文件 accounts.json / YIBAN_ACCOUNTS_JSON（推荐，一次输入一个账号完整信息）
    - 旧格式 YIBAN_ACCOUNTS 或 YIBAN_PHONE/YIBAN_PASSWORD（向后兼容）
    - YIBAN_CONCURRENCY 控制并发（默认顺序执行）
    - --check-config 仅检查配置，不发任何网络请求
    """
    parser = argparse.ArgumentParser(description='易班自动签到')
    parser.add_argument('--check-config', action='store_true',
                        help='仅检查账号配置（脱敏打印），不发起任何网络请求')
    args = parser.parse_args()

    notify_url = os.environ.get('YIBAN_NOTIFY_URL', '')

    # 加载账号配置（文件 > JSON 环境变量 > 旧格式，详见 load_accounts）
    try:
        accounts = load_accounts()
    except RuntimeError as e:
        logger.error(f'配置加载失败: {e}')
        sys.exit(1)

    if not accounts:
        logger.error('未配置任何账号，请通过以下任一方式配置：')
        logger.error('  1. accounts.json 文件（推荐，用 TUI 配置工具生成）')
        logger.error('  2. YIBAN_ACCOUNTS_JSON 环境变量（JSON 数组）')
        logger.error('  3. YIBAN_ACCOUNTS 环境变量（旧格式 phone:password#phone2:password2）')
        logger.error('  4. YIBAN_PHONE / YIBAN_PASSWORD 环境变量（单账号）')
        sys.exit(1)

    # 仅检查配置模式：不发任何网络请求，用于部署验证
    if args.check_config:
        print_config_summary(accounts)
        sys.exit(0)

    # 并发配置：YIBAN_CONCURRENCY 默认 1（顺序执行），>1 时并发执行
    concurrency = parse_concurrency(os.environ.get('YIBAN_CONCURRENCY', '1'))
    mode = f'并发（{concurrency} 线程）' if concurrency > 1 else '顺序'
    logger.info(f'==== 开始执行签到，共 {len(accounts)} 个账号，执行模式: {mode} ====')

    if concurrency > 1:
        results = run_concurrent(accounts, notify_url, concurrency)
    else:
        results = [process_account(acc, notify_url) for acc in accounts]

    # 汇总（results 顺序与 accounts 一致）
    logger.info('==== 签到汇总 ====')
    has_real_failure = False
    for acc, (success, msg, skip) in zip(accounts, results):
        status = '✅' if success else '❌'
        logger.info(f'  {status} {acc.phone}: {msg}')
        if not success and not skip:
            has_real_failure = True

    # 退出码：
    # 0 - 全部成功，或仅因"未在签到时间内"跳过（不是真正的失败）
    # 1 - 有真正的失败（登录失败、签到失败等）
    sys.exit(0 if not has_real_failure else 1)


if __name__ == '__main__':
    main()
