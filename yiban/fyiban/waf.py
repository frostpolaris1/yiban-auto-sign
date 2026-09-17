# -*- coding: utf-8 -*-
"""易盾 WAF（https_ydclearance）挑战解析：**衍生自 `onefeifan/fyiban`（AGPL-3.0）**。

上游在 `Core/BaseReq.kt` + 登录流程里处理该挑战（跟随跳转、取回 cookie）；本实现把它
落成纯 Python 的确定性解析（不执行远程代码）。**安全策略以 `allow_url` 注入**，见
`PROVENANCE.md` 与包文档的两条纪律。
"""
import re


def looks_like_challenge(text, set_cookie=""):
    """判断响应是否触发 ydclearance 反爬挑战。

    特征判定（不依赖响应长度）：
    - Set-Cookie 已下发 https_ydclearance（说明已过挑战）；
    - 或响应包含挑战 JS 特征（window.onload=setTimeout + eval("qo=eval;qo(po);")）。

    "是不是挑战页"是平台特征识别，属本层；"这个挑战能不能信"（跳转白名单）才是
    本项目策略，仍由 `solve_ydclearance(..., allow_url=...)` 注入。
    """
    if "https_ydclearance" in (set_cookie or ""):
        return True
    return "window.onload=setTimeout" in text and 'eval("qo=eval;qo(po);")' in text


def solve_ydclearance(text, allow_url):
    """纯 Python 解析易盾 WAF（https_ydclearance）挑战；不执行任何远程 JS。

    升级/替换依据见同目录 `PROVENANCE.md`。挑战模板固定（易盾 WAF v1）：
    `oo` 十六进制字节数组 + 三步固定变换（取反+旋转-常量、逆向差分、加常量+旋转），
    最后跳过 `qo % K` 的下标、逐字节异或挑战参数拼出 `po` 字符串（含 cookie 赋值与跳转
    路径）。各步数值常量随挑战变化，用正则从 JS 中提取后在 Python 中复刻运算；任何一步
    提取失败都抛明确错误，绝不 eval 远程代码。

    `allow_url` 是本项目注入的**安全策略**（跳转目标白名单）：第三方层不内联安全校验，
    调用方传什么口径就按什么口径判（生产传 `yiban.security` 的 f.yiban.cn 白名单）。
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
    target = path_m[0]
    if target.startswith("/"):
        target = "https://f.yiban.cn" + target
    if not allow_url(target):
        raise RuntimeError("ydclearance 跳转目标不在白名单")
    return cookie_m[0], target
