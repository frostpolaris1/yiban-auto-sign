#!/usr/bin/env python3
"""
易班自动签到脚本

功能：
1. 自动登录易班（支持多账号）
2. 自动获取签到任务范围
3. 在签到范围内生成随机定位点（模拟真实定位）
4. 自动提交签到
5. 支持消息通知（Server 酱、Bark、企业微信等）

参考项目：
- 本地 KillYiBan 模块（nightAttendance 签到流程）
- Auto-Test 项目（登录流程）
"""

import os
import sys
import json
import random
import logging
import traceback
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
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
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
# 定位生成：多边形内随机点
# ---------------------------------------------------------------------------
def point_in_polygon(x, y, polygon):
    """射线法判断点是否在多边形内。

    Args:
        x: 经度
        y: 纬度
        polygon: [(lng, lat), ...] 多边形顶点

    Returns:
        bool
    """
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
    """在多边形内生成随机点。

    使用缩放质心算法（参考本地 KillYiBan 实现）：
    1. 计算多边形质心
    2. 将多边形向质心收缩 0.7 倍
    3. 在质心附近随机生成点，校验是否在缩放多边形内

    Args:
        polygon_points: [(lng, lat), ...] 多边形顶点列表

    Returns:
        (lng, lat) 生成的随机点
    """
    if not polygon_points:
        return None

    # 计算边界框
    min_lng = min(p[0] for p in polygon_points)
    max_lng = max(p[0] for p in polygon_points)
    min_lat = min(p[1] for p in polygon_points)
    max_lat = max(p[1] for p in polygon_points)

    # 计算质心
    center_lng = sum(p[0] for p in polygon_points) / len(polygon_points)
    center_lat = sum(p[1] for p in polygon_points) / len(polygon_points)

    # 缩放多边形（向质心收缩 0.7 倍）
    scaled_points = [
        ((p[0] - center_lng) * 0.7 + center_lng,
         (p[1] - center_lat) * 0.7 + center_lat)
        for p in polygon_points
    ]

    # 随机生成点（最多 100 次尝试）
    for _ in range(100):
        lng = center_lng + (max_lng - min_lng) * 0.2 * (random.random() - 0.5)
        lat = center_lat + (max_lat - min_lat) * 0.2 * (random.random() - 0.5)
        # 同时检查缩放多边形和原始多边形
        if point_in_polygon(lng, lat, scaled_points) and \
                point_in_polygon(lng, lat, polygon_points):
            return (lng, lat)

    # 兜底：返回质心
    return (center_lng, center_lat)


# ---------------------------------------------------------------------------
# 易班登录
# ---------------------------------------------------------------------------
class YibanClient:
    """易班客户端：封装登录与签到流程。"""

    def __init__(self, account, password):
        self.account = account
        self.password = password.encode('UTF-8')
        self.csrf = md5(str(datetime.now()).encode('UTF-8')).hexdigest()
        self.session = requests.Session()
        self.session.keep_alive = False
        self.session.headers = dict(HEADERS)
        # 可选代理：GitHub Actions 海外 IP 可能被易班 WAF/地域风控拦截，
        # 设置环境变量 YIBAN_PROXY（如 http://user:pass@host:port 或 socks5://host:port）
        # 即可让所有请求走代理（建议用国内出口）。
        proxy = os.environ.get('YIBAN_PROXY', '').strip()
        if proxy:
            self.session.proxies = {'http': proxy, 'https': proxy}
            logger.info(f'[{self.account}] 已启用代理: {proxy}')
        self.logged_in = False

    # ---- 登录 -------------------------------------------------------------
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
        page_use_match = compile(r'page_use ?= ?[\'|"]([a-zA-Z0-9-_]+)[\'|"]').findall(resp.text)
        key_match = compile(r'id="key" ?value="?([0-9a-zA-Z -_/=+\n]+[^"])"? ').findall(resp.text)
        if not page_use_match or not key_match:
            # 诊断：打印实际收到的响应，便于判断是 WAF 挑战页 / 地域拦截 / 空响应
            body_preview = resp.text[:1500].replace('\n', '\\n')
            logger.error(f'[{self.account}] OAuth 页解析失败诊断:')
            logger.error(f'  最终 URL: {resp.url}')
            logger.error(f'  状态码: {resp.status_code}')
            logger.error(f'  响应长度: {len(resp.text)}')
            logger.error(f'  响应前1500字符: {body_preview}')
            logger.error(f'  page_use 命中: {len(page_use_match)}, key 命中: {len(key_match)}')
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
        if 'error' in result.get('reUrl', ''):
            raise RuntimeError(f'登录失败（账号或密码错误）: {self.account}')

        # 4. 跳转回 f.yiban.cn，可能遇到 ydclearance 反爬
        self.session.headers.update(Referer='https://oauth.yiban.cn/')
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
        logger.info(f'[{self.account}] 登录成功')

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
            compile(r"window\.document\.location='(.+)'").findall(evaluated)[0],
        ]

    # ---- 签到 -------------------------------------------------------------
    def signin(self):
        """执行签到，返回 (success: bool, message: str)。"""
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
            return False, f"获取签到任务失败: {data.get('msg')}"

        data_obj = data['data']
        if data_obj.get('Msg') == '已签到':
            return True, '今日已签到（无需重复签到）'

        position_list = data_obj.get('Position', [])
        if not position_list:
            return False, '未找到签到位置数据'
        position = position_list[0]
        range_obj = data_obj.get('Range', {})

        # 2. 校验签到时间
        now_ts = int(datetime.now().timestamp())
        start_ts = int(range_obj.get('StartTime', 0))
        end_ts = int(range_obj.get('EndTime', 0))
        if start_ts and end_ts and not (start_ts <= now_ts <= end_ts):
            return False, f'未在签到时间内（{datetime.fromtimestamp(start_ts)} ~ {datetime.fromtimestamp(end_ts)}）'

        # 3. 解析多边形点
        points_raw = position.get('Points', [])
        polygon = []
        for p in points_raw:
            parts = p.split(',')
            if len(parts) >= 2:
                polygon.append((float(parts[0]), float(parts[1])))

        if not polygon:
            return False, '签到范围点解析失败'

        # 4. 在多边形内生成随机点
        lng, lat = generate_position_in_polygon(polygon)
        logger.info(f'[{self.account}] 生成定位: ({lng},{lat}) 地址: {position.get("Address", "")}')

        # 5. 构建签到数据并提交
        sign_info = {
            'Reason': '',
            'AttachmentFileName': '',
            'LngLat': f'{lng},{lat}',
            'Address': position.get('Address', ''),
        }
        resp = self.session.post(
            'https://api.uyiban.com/nightAttendance/student/index/signIn',
            params={'CSRF': self.csrf},
            data={
                'Code': '',
                'PhoneModel': '',
                'SignInfo': json.dumps(sign_info, ensure_ascii=False),
                'OutState': '1.0',
            },
            allow_redirects=False,
            timeout=15,
        )
        result = resp.json()
        if result.get('code') == 0 and result.get('data'):
            return True, '签到成功'
        return False, f"签到失败: {result.get('msg', '未知错误')}"


# ---------------------------------------------------------------------------
# 消息通知
# ---------------------------------------------------------------------------
def send_notification(title, content, url):
    """通过 Server 酱 / Bark / 企业微信等 webhook 发送通知。

    支持 Statocysts 格式 URL（与 skland-daily-attendance 一致）。
    """
    if not url:
        return
    try:
        # 简化处理：直接 POST JSON
        if url.startswith('http'):
            requests.post(url, json={'title': title, 'content': content}, timeout=10)
            logger.info('通知发送成功')
    except Exception as e:
        logger.warning(f'通知发送失败: {e}')


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def process_account(account, password, notify_url=None):
    """处理单个账号签到。"""
    try:
        client = YibanClient(account, password)
        client.login()
        success, message = client.signin()
        status = '✅' if success else '❌'
        logger.info(f'[{account}] {status} {message}')
        if not success and notify_url:
            send_notification('易班签到失败', f'账号: {account}\n原因: {message}', notify_url)
        return success, message
    except Exception as e:
        logger.error(f'[{account}] ❌ 异常: {e}')
        logger.debug(traceback.format_exc())
        if notify_url:
            send_notification('易班签到异常', f'账号: {account}\n异常: {e}', notify_url)
        return False, str(e)


def main():
    """主函数：支持多账号（账号间用 # 分隔，账号与密码用 : 分隔）。"""
    # 兼容两种配置方式：
    #   1. YIBAN_ACCOUNTS="phone1:pwd1#phone2:pwd2"
    #   2. YIBAN_PHONE / YIBAN_PASSWORD（单账号，向后兼容）
    accounts_str = os.environ.get('YIBAN_ACCOUNTS', '')
    notify_url = os.environ.get('YIBAN_NOTIFY_URL', '')

    accounts = []
    if accounts_str:
        for item in accounts_str.split('#'):
            item = item.strip()
            if not item:
                continue
            if ':' not in item:
                logger.error(f'账号配置格式错误（应为 phone:password）: {item}')
                continue
            phone, pwd = item.split(':', 1)
            accounts.append((phone.strip(), pwd.strip()))
    else:
        phone = os.environ.get('YIBAN_PHONE')
        pwd = os.environ.get('YIBAN_PASSWORD')
        if phone and pwd:
            accounts.append((phone, pwd))

    if not accounts:
        logger.error('未配置任何账号，请设置 YIBAN_ACCOUNTS 或 YIBAN_PHONE/YIBAN_PASSWORD 环境变量')
        sys.exit(1)

    logger.info(f'==== 开始执行签到，共 {len(accounts)} 个账号 ====')
    results = []
    for phone, pwd in accounts:
        success, msg = process_account(phone, pwd, notify_url)
        results.append((phone, success, msg))

    # 汇总
    logger.info('==== 签到汇总 ====')
    all_success = True
    for phone, success, msg in results:
        status = '✅' if success else '❌'
        logger.info(f'  {status} {phone}: {msg}')
        if not success:
            all_success = False

    sys.exit(0 if all_success else 1)


if __name__ == '__main__':
    main()
