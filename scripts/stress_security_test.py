# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""本地压力/安全/混合流量测试脚本（仅本地 demo 服务器，不发版不部署）。

用法：
    python3 scripts/stress_security_test.py [--url http://127.0.0.1:17892]

覆盖：
- 并发 GET 压力
- SQL 注入尝试
- XSS 输入存储（配合前端转义审查）
- CSRF 缺失令牌
- 登录限速
- 超大请求体
- 非法 JSON
- 恶意 + 正常混合流量
"""
import argparse
import concurrent.futures
import os
import sys
import time

import requests


def _post_json(session, url, path, data, headers=None):
    return session.post(
        url + path,
        json=data,
        headers=headers or {},
        timeout=10,
    )


def admin_session(base_url, username, password):
    s = requests.Session()
    r = s.post(base_url + "/api/login", json={"username": username, "password": password}, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"管理员登录失败: {r.status_code} {r.text[:200]}")
    me = s.get(base_url + "/api/me", timeout=10)
    csrf = me.json()["csrf_token"]
    return s, {"X-CSRF-Token": csrf}


def test_concurrent_get(base_url, concurrency=10, total=40):
    def get_root(_):
        try:
            r = requests.get(base_url + "/", allow_redirects=False, timeout=10)
            return r.status_code in (200, 302)
        except Exception:
            return False

    start = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        results = list(ex.map(get_root, range(total)))
    ok = sum(results)
    elapsed = time.time() - start
    print(f"[并发GET] 请求={total} 并发={concurrency} 成功={ok} 耗时={elapsed:.2f}s")
    return ok == total


def test_sql_injection(base_url):
    ok = True
    # 登录接口 SQLi
    r = requests.post(base_url + "/api/login", json={
        "username": "' OR 1=1 --",
        "password": "x",
    }, timeout=10)
    if r.status_code not in (400, 401, 429):
        print(f"[SQLi] 登录接口异常状态: {r.status_code}")
        ok = False
    # 添加账号接口 SQLi（需要管理员）
    try:
        s, headers = admin_session(base_url, "admin", "TestPass1234!")
        r = s.post(base_url + "/api/accounts", json={
            "name": "x' OR '1'='1",
            "phone": "13800138000' OR '1'='1",
            "password": "p1",
        }, headers=headers, timeout=10)
        if r.status_code not in (400, 403):
            print(f"[SQLi] 添加账号接口异常状态: {r.status_code}")
            ok = False
    except Exception as e:
        print(f"[SQLi] 管理员会话异常: {e}")
        ok = False
    print("[SQLi] 通过" if ok else "[SQLi] 失败")
    return ok


def test_xss_storage(base_url):
    # 以管理员添加一个带脚本的账号名，验证 API 能存储且不报错；
    # 实际展示是否转义由前端模板负责（Jinja 默认自动转义）。
    try:
        s, headers = admin_session(base_url, "admin", "TestPass1234!")
        accounts = s.get(base_url + "/api/accounts", headers=headers, timeout=10).json()["accounts"]
        target = next(a for a in accounts if not a["deleted"])
        idx = target["index"]
        detail = s.get(f"{base_url}/api/accounts/{idx}/detail", headers=headers, timeout=10).json()["account"]
        r = s.put(f"{base_url}/api/accounts/{idx}", json={
            "name": "<script>alert(1)</script>",
            "phone": detail["phone"],
        }, headers=headers, timeout=10)
        if r.status_code != 200:
            print(f"[XSS] 更新账号失败: {r.status_code} {r.text[:200]}")
            return False
        accounts = s.get(base_url + "/api/accounts", headers=headers, timeout=10).json()["accounts"]
        found = any(a["index"] == idx and "<script>" in a["name"] for a in accounts)
        print(f"[XSS] 存储原样返回={found}（前端渲染需依赖模板转义）")
        return found
    except Exception as e:
        print(f"[XSS] 异常: {e}")
        return False


def test_csrf(base_url):
    try:
        s, _ = admin_session(base_url, "admin", "TestPass1234!")
    except Exception as e:
        print(f"[CSRF] 管理员会话异常: {e}")
        return False
    r = s.post(base_url + "/api/accounts", json={
        "name": "x", "phone": "13800138888", "password": "p1",
    }, timeout=10)
    ok = r.status_code == 403
    print(f"[CSRF] 无令牌状态={r.status_code} 通过={ok}")
    return ok


def test_rate_limit(base_url):
    statuses = []
    for _ in range(20):
        r = requests.post(base_url + "/api/login", json={
            "username": "attacker@test.local",
            "password": "wrong",
        }, timeout=10)
        statuses.append(r.status_code)
    ok = 429 in statuses
    print(f"[限速] 20次失败登录状态码={statuses} 出现429={ok}")
    return ok


def test_large_payload(base_url):
    big = "x" * (70 * 1024)
    try:
        r = requests.post(base_url + "/api/login", data=big, headers={
            "Content-Type": "application/json",
        }, timeout=10)
    except requests.exceptions.RequestException as e:
        print(f"[大包] 请求异常（可能被连接断开）: {e}")
        return True
    ok = r.status_code == 413
    print(f"[大包] 状态={r.status_code} 通过={ok}")
    return ok


def test_invalid_json(base_url):
    try:
        r = requests.post(base_url + "/api/login", data="{bad json", headers={
            "Content-Type": "application/json",
        }, timeout=10)
    except requests.exceptions.RequestException as e:
        print(f"[非法JSON] 请求异常: {e}")
        return False
    ok = r.status_code == 400
    print(f"[非法JSON] 状态={r.status_code} 通过={ok}")
    return ok


def test_mixed_traffic(base_url, concurrency=10):
    # 等待全局限速窗口重置，避免前面测试累计触发 429
    time.sleep(11)

    def normal(_):
        try:
            r = requests.get(base_url + "/", allow_redirects=False, timeout=10)
            return r.status_code in (200, 302)
        except Exception:
            return False

    def malicious(i):
        try:
            if i % 3 == 0:
                return requests.post(base_url + "/api/login", json={
                    "username": "' OR 1=1 --", "password": "x",
                }, timeout=10).status_code in (400, 401, 429)
            if i % 3 == 1:
                return requests.post(base_url + "/api/login", data="x" * 70000,
                                     headers={"Content-Type": "application/json"},
                                     timeout=10).status_code == 413
            return requests.post(base_url + "/api/login", json={
                "username": "attacker@test.local", "password": "wrong",
            }, timeout=10).status_code in (400, 401, 429)
        except Exception:
            return False

    tasks = [("normal", i) for i in range(20)] + [("malicious", i) for i in range(20)]
    normal_ok = 0
    malicious_ok = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {}
        for kind, i in tasks:
            futures[ex.submit(normal if kind == "normal" else malicious, i)] = kind
        for fut in concurrent.futures.as_completed(futures):
            kind = futures[fut]
            try:
                result = fut.result()
            except Exception:
                result = False
            if kind == "normal":
                if result:
                    normal_ok += 1
            else:
                if result:
                    malicious_ok += 1
    print(f"[混合] 正常成功={normal_ok}/20 恶意被正确拒绝={malicious_ok}/20")
    return normal_ok >= 18 and malicious_ok >= 18


def main():
    parser = argparse.ArgumentParser(description="本地压力/安全/混合流量测试")
    parser.add_argument("--url", default="http://127.0.0.1:17892", help="本地服务器地址")
    args = parser.parse_args()

    # 批次7 P4-15：本脚本包含改写真实数据与触发限速的破坏性用例——
    # 仅允许对回环/本机地址执行，防止误对生产打靶
    from urllib.parse import urlparse as _urlparse
    _host = (_urlparse(args.url if "://" in args.url else "http://" + args.url).hostname or "")
    import ipaddress as _ipa
    try:
        _loopback = _ipa.ip_address(_host).is_loopback
    except ValueError:
        _loopback = _host in ("localhost",)
    if not _loopback and os.environ.get("STRESS_TEST_ALLOW_REMOTE") != "1":
        print(f"拒绝执行：目标 {_host} 非回环地址。确属隔离环境请设 STRESS_TEST_ALLOW_REMOTE=1")
        sys.exit(2)

    tests = [
        ("并发GET", lambda: test_concurrent_get(args.url)),
        ("SQL注入", lambda: test_sql_injection(args.url)),
        ("XSS存储", lambda: test_xss_storage(args.url)),
        ("CSRF", lambda: test_csrf(args.url)),
        ("超大请求体", lambda: test_large_payload(args.url)),
        ("非法JSON", lambda: test_invalid_json(args.url)),
        ("登录限速", lambda: test_rate_limit(args.url)),
        ("混合流量", lambda: test_mixed_traffic(args.url)),
    ]

    failed = []
    for name, fn in tests:
        print(f"\n=== {name} ===")
        try:
            ok = fn()
        except Exception as e:
            print(f"异常: {e}")
            ok = False
        if not ok:
            failed.append(name)

    print("\n===== 汇总 =====")
    if failed:
        print(f"失败项: {failed}")
        sys.exit(1)
    print("全部通过 [OK]")


if __name__ == "__main__":
    main()
