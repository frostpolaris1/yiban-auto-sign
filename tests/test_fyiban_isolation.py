# -*- coding: utf-8 -*-
"""第三方隔离层（`yiban/fyiban/`）的**保护存在性断言**与边界守卫。

标签：K · 登录协议与第三方隔离
覆盖：算法行为（点在多边形内、水平边不除零、生成点必在界内、空与退化输入的兜底、缩放系数与上游一致）、请求头行为（两处版本号同值、键集合钉住、signin
   只同名转发）、WAF
   行为（无法识别的挑战页响亮失败、白名单由调用方注入、模块不自带安全策略）、结构守卫（实现只在隔离层、许可与来源声明随代码、本层不导入业务模块）、协议层边界（端点单点定义、不内联域名判定、不自带会话持久化、signin
   转发同一对象、客户端外观不重写协议步骤）。
对应实现：yiban/fyiban/（algo.py、headers.py、waf.py、protocol.py、PROVENANCE.md）、scripts/signin.py
   的同名转发、yiban/security.py（被注入的白名单）。
关键断言：抽层的风险是「抽着抽着改了行为」与「留下第二份实现」，两类都要钉：行为侧逐值对齐上游（改
   SCALE_FACTOR 等于换算法），结构侧断言实现只出现在 yiban/fyiban/ 且 signin
   拿到的是同一个对象（assertIs，不是等值）。安全策略（URL 白名单、WAF
   关键词）属本项目层，必须由调用方注入——第三方隔离层自带一份就等于把策略圈在许可范围内改不掉。AGPL
   要求修改声明与许可随代码分发，故 PROVENANCE
   与许可文件的存在性本身是被测行为。
依赖：读源码文本做结构断言 +
   直接调用隔离层函数；不发网络请求、不建库。整文件在本机执行，无 skip。

1. **行为**：算法在真实多边形上的判定结果、采样点必须落在多边形内、版本号两处一致、
   挑战解析对无法识别输入必须响亮失败（不静默返回错值）；
2. **结构**：实现只在 `yiban/fyiban/` 定义（`signin` 只做同名转发，是**同一个对象**）、
   许可与来源声明随代码在目录里、本层不得导入业务模块、不得内联安全策略。

上游对照与逐块差异见 `yiban/fyiban/PROVENANCE.md`。
"""
import io
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import signin  # noqa: E402

from yiban import security as fyiban_security  # noqa: E402
from yiban.fyiban import algo as fyiban_algo  # noqa: E402
from yiban.fyiban import headers as fyiban_headers  # noqa: E402
from yiban.fyiban import waf as fyiban_waf  # noqa: E402

# 一个 1km 量级的近似方形多边形（经纬度，南京）
SQUARE = [(118.88, 31.92), (118.90, 31.92), (118.90, 31.94), (118.88, 31.94)] # 经纬度的真实量级（约 1km）：退化判定按度算，随手写 0~1 会绕过它


class AlgoBehaviorTest(unittest.TestCase):
    def test_point_in_polygon_basic(self):
        inside = (118.89, 31.93) # 界内界外都取整分量，避免判差落在边界抖动上
        outside = (118.87, 31.93)
        self.assertTrue(fyiban_algo.point_in_polygon(*inside, SQUARE))
        self.assertFalse(fyiban_algo.point_in_polygon(*outside, SQUARE))

    def test_point_in_polygon_tolerates_horizontal_edge(self):
        """水平边（相邻两点纬度相同）不得除零——本实现给分母加 1e-12，上游没有。"""
        poly = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
        self.assertTrue(fyiban_algo.point_in_polygon(1.0, 0.0, poly))
        self.assertTrue(fyiban_algo.point_in_polygon(1.0, 1.0, poly))
        self.assertFalse(fyiban_algo.point_in_polygon(3.0, 1.0, poly))

    def test_generated_point_always_inside(self):
        for _ in range(30):
            pt = fyiban_algo.generate_position_in_polygon(SQUARE)
            self.assertIsNotNone(pt)
            self.assertTrue(
                fyiban_algo.point_in_polygon(pt[0], pt[1], SQUARE),
                f"生成的坐标落在签到范围外：{pt}",
            )

    def test_empty_polygon_returns_none(self):
        self.assertIsNone(fyiban_algo.generate_position_in_polygon([]))

    def test_degenerate_polygon_falls_back(self):
        """极小/退化多边形不能死循环，也不能抛异常（走质心 + 抖动兜底）。"""
        tiny = [(118.88, 31.92), (118.8800001, 31.92),
                (118.8800001, 31.9200001), (118.88, 31.9200001)]
        pt = fyiban_algo.generate_position_in_polygon(tiny)
        self.assertIsNotNone(pt)

    def test_scale_factor_matches_upstream(self):
        """缩放质心系数与上游一致（createScaledPolygon(..., 0.7)）：改它等于换算法。"""
        self.assertEqual(fyiban_algo.SCALE_FACTOR, 0.7)


class HeadersBehaviorTest(unittest.TestCase):
    def test_app_version_is_consistent_across_headers(self):
        """两处请求头必须同值（服务端一致性校验），改一处必须改另一处。"""
        self.assertEqual(fyiban_headers.KILLYIBAN_HEADERS["AppVersion"],
                         fyiban_headers.YIBAN_APP_VERSION)
        self.assertIn(fyiban_headers.YIBAN_APP_VERSION,
                      fyiban_headers.HEADERS["User-Agent"])

    def test_header_keys_pinned(self):
        self.assertEqual(set(fyiban_headers.KILLYIBAN_HEADERS),
                         {"User-Agent", "AppVersion", "Origin", "Referer", "Connection"})
        self.assertIn("X-Requested-With", fyiban_headers.HEADERS) # 键名逐个钉住：上游改头等于换协议，不是「多用一个键无所谓」
        self.assertEqual(fyiban_headers.HEADERS["Origin"], "https://app.uyiban.com")
        self.assertEqual(fyiban_headers.KILLYIBAN_HEADERS["Origin"], "https://c.uyiban.com")

    def test_signin_reexports_same_objects(self):
        """signin 只是同名转发（同一对象）——不是第二份实现。"""
        self.assertIs(signin.YIBAN_APP_VERSION, fyiban_headers.YIBAN_APP_VERSION)
        self.assertIs(signin.HEADERS, fyiban_headers.HEADERS)
        self.assertIs(signin.KILLYIBAN_HEADERS, fyiban_headers.KILLYIBAN_HEADERS)
        self.assertIs(signin.point_in_polygon, fyiban_algo.point_in_polygon)
        self.assertIs(signin.generate_position_in_polygon,
                      fyiban_algo.generate_position_in_polygon)


class WafBehaviorTest(unittest.TestCase):
    def test_challenge_detection_delegates_to_vendored_library(self):
        """检测本体委托 vendored 库：已知挑战标记命中、正常 JSON 不误报。"""
        self.assertTrue(fyiban_waf.looks_like_challenge("<html>ydclearance</html>"))
        self.assertTrue(fyiban_waf.looks_like_challenge(
            "Set-Cookie: https_ydclearance=abc", "https_ydclearance=abc"))
        self.assertTrue(fyiban_waf.looks_like_challenge(
            '<script>window.onload=setTimeout("yy(1)",200);eval("qo=eval;qo(po);");</script>'))
        self.assertFalse(fyiban_waf.looks_like_challenge('{"code":0,"msg":""}'))

    def test_solver_is_gone_and_detection_hit_fails_loudly(self):
        """求解器已按既定裁决删除（生产全历史零触发）；检测命中由协议层响亮失败。"""
        self.assertFalse(hasattr(fyiban_waf, "solve_ydclearance"),
                         "生产零触发的挑战求解器必须删除，不得以第二份实现复活")
        self.assertIn("ydclearance", fyiban_waf.CHALLENGE_DETECTED_MESSAGE)
        self.assertTrue(fyiban_security.is_hard_fail_message(
            fyiban_waf.CHALLENGE_DETECTED_MESSAGE),
            "检测命中必须落不可重试硬失败档（同一挑战重发无益）")

    def test_waf_module_has_no_security_policy_of_its_own(self):
        """第三方层不得自带 URL 白名单或求解实现（安全策略属本项目，须注入）。

        判据是"有没有自己的域名校验/求解运算"，不是"调没调 allow_url"——求解器删除后
        `allow_url` 注入面随之消失，本模块只剩"检测"与委托。
        """
        src = io.open(os.path.join(BASE, "yiban", "fyiban", "waf.py"),
                      encoding="utf-8").read()
        self.assertNotIn("_is_fyiban_url", src)
        self.assertNotIn("urlsplit", src)
        self.assertNotIn("urlparse", src)
        self.assertNotIn("allow_url", src)
        # 检测本体委托 vendored 洁净室库（本层不再内联求解/挑战模板正则）
        self.assertIn("yiban._vendor.yiban_protocol", src)


class IsolationStructureTest(unittest.TestCase):
    def _fyiban_src(self, name):
        with io.open(os.path.join(BASE, "yiban", "fyiban", name), encoding="utf-8") as f:
            return f.read()

    def test_implementations_live_only_in_isolation_layer(self):
        signin_src = io.open(os.path.join(BASE, "scripts", "signin.py"),
                             encoding="utf-8").read()
        for pattern in (r"(?m)^def point_in_polygon\(",
                        r"(?m)^def generate_position_in_polygon\(",
                        r'(?m)^YIBAN_APP_VERSION = "',
                        r"(?m)^HEADERS = \{",
                        r"(?m)^KILLYIBAN_HEADERS = \{"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, signin_src),
                                  "signin.py 又出现了一份实现（应为转发引用）")
        self.assertRegex(self._fyiban_src("algo.py"), r"(?m)^def point_in_polygon\(")
        self.assertRegex(self._fyiban_src("algo.py"), r"(?m)^def generate_position_in_polygon\(")
        self.assertRegex(self._fyiban_src("headers.py"), r"(?m)^YIBAN_APP_VERSION = \"")
        self.assertRegex(self._fyiban_src("waf.py"), r"(?m)^def looks_like_challenge\(")

    def test_license_and_provenance_travel_with_code(self):
        """AGPL §5 要求修改声明与许可随代码分发——两件必须在目录里。"""
        lic = self._fyiban_src("LICENSE")
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", lic)
        prov = self._fyiban_src("PROVENANCE.md")
        self.assertIn("onefeifan/fyiban", prov)
        self.assertIn("AGPL-3.0", prov)
        self.assertRegex(prov, r"\bd854182\b", "需记录核对的上游提交号")
        # 逐块对照表至少要覆盖四个实现文件
        for f in ("algo.py", "headers.py", "waf.py", "protocol.py"):
            with self.subTest(file=f):
                self.assertIn(f, prov)

    def test_vendored_protocol_library_is_located_and_imported(self):
        """洁净室协议库的定位：vendored 代码只在 `yiban/_vendor/`，隔离层只引用不复制。

        B' 换核后，登录/签到链的纯解析/构造由 `yiban/_vendor/yiban_protocol`（MIT）承担；
        本仓对该目录**只同步覆盖、不改**，故它的许可文本与来源说明必须随代码在位，
        且隔离层经 `from yiban._vendor.yiban_protocol import …` 引用而非重写实现。
        """
        vendor = os.path.join(BASE, "yiban", "_vendor")
        for rel in ("VENDORED.md", "yiban_protocol/__init__.py",
                    "yiban_protocol/LICENSE", "yiban_protocol/position.py"):
            with self.subTest(rel=rel):
                self.assertTrue(os.path.isfile(os.path.join(vendor, rel)),
                                f"vendored 库缺文件: {rel}")
        vendored_doc = io.open(os.path.join(vendor, "VENDORED.md"), encoding="utf-8").read()
        self.assertIn("c20ad68", vendored_doc, "需记录来源 commit")
        self.assertIn("MIT", vendored_doc)
        lic = io.open(os.path.join(vendor, "yiban_protocol", "LICENSE"), encoding="utf-8").read()
        self.assertIn("MIT License", lic)
        # 隔离层只引用 vendored 库，不把纯函数再抄回来
        for name in ("protocol.py", "waf.py"):
            with self.subTest(file=name):
                self.assertIn("yiban._vendor.yiban_protocol", self._fyiban_src(name))

    def test_layer_does_not_import_business_modules(self):
        bad = []
        for name in ("algo.py", "headers.py", "waf.py", "protocol.py", "__init__.py"):
            src = self._fyiban_src(name)
            for m in re.finditer(r"(?m)^\s*(?:import|from)\s+([\w.]+)", src):
                dotted = m.group(1)
                if dotted.split(".")[0] in ("db", "signin", "notify", "mailer", "web") or \
                        dotted.startswith(("yiban.store", "yiban.security", "yiban.client",
                                           "yiban.masking", "yiban.clock")):
                    bad.append(f"{name} → {dotted}")
        self.assertEqual(bad, [], "第三方层不得依赖业务层/安全层（策略靠注入）：" + "; ".join(bad))


class ProtocolLayerTest(unittest.TestCase):
    """协议层（`protocol.py`）的边界：端点只在隔离层，安全策略只在注入点。"""

    def _src(self, *parts):
        with io.open(os.path.join(BASE, *parts), encoding="utf-8") as f:
            return f.read()

    def test_endpoints_defined_only_in_isolation_layer(self):
        """端点与客户端标识只能定义在协议层：signin / web 里再出现一份就是第二份实现。"""
        patterns = (
            r'"https://oauth\.yiban\.cn/code/usersure"',
            r'"https://oauth\.yiban\.cn/code/html"',
            r'"https://api\.uyiban\.com/base/c/auth/yiban"',
            r'"https://f\.yiban\.cn/iframe/index"',
            r'"https://api\.uyiban\.com/nightAttendance/student/index/signPosition"',
            r'"https://api\.uyiban\.com/nightAttendance/student/index/signIn"',
            r'"95626fa3080300ea"',
        )
        owners = [("scripts", "signin.py"), ("web", "app.py"),
                  ("yiban", "client.py"), ("yiban", "security.py")]
        for owner in owners:
            src = self._src(*owner)
            for pattern in patterns:
                with self.subTest(owner="/".join(owner), pattern=pattern):
                    self.assertIsNone(
                        re.search(pattern, src),
                        f"{'/'.join(owner)} 里出现了易班端点字面量（应引用 protocol 常量）")

        protocol_src = self._src("yiban", "fyiban", "protocol.py")
        self.assertRegex(protocol_src, r'(?m)^OAUTH_CLIENT_ID = "95626fa3080300ea"')
        self.assertRegex(protocol_src, r'(?m)^OAUTH_USERSURE_URL = "https://oauth\.yiban\.cn/code/usersure"')

    def test_security_policy_is_injected_not_inlined(self):
        """协议层不得自带 WAF 关键词或**域名判定**（那些属 yiban/security.py，须注入）。

        端点常量本身当然含 `yiban.cn`（那是平台事实），所以判据不是"出现域名"，
        而是"**算出裁决**"：不得有白名单函数、不得做主机后缀比对、不得内联拦截词。
        """
        src = self._src("yiban", "fyiban", "protocol.py")
        for forbidden in ("WAF_KEYWORDS", "风险访问", "访问服务禁用",
                          "def is_fyiban_url", "def is_yiban_trusted_url",
                          "urlsplit", "endswith(", "hostname =="):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, src,
                                 "协议层出现了安全判定（应由 policy 注入）")
        # 注入契约在位：白名单与 WAF 判定都经 policy 调用
        self.assertRegex(src, r"policy\.require_trusted\(")
        self.assertRegex(src, r"policy\.require_not_blocked\(")
        self.assertRegex(src, r"policy\.is_blocked\(")
        # 挑战：检测委托 vendored 库，命中即用常量响亮失败（不再求解、不再注入白名单）
        self.assertRegex(src, r"fyiban_waf\.looks_like_challenge\(")
        self.assertRegex(src, r"raise RuntimeError\(fyiban_waf\.CHALLENGE_DETECTED_MESSAGE\)")

    def test_protocol_layer_has_no_own_session_persistence(self):
        """会话缓存只能经 session_store 注入：协议层不得自己碰库或状态文件。"""
        src = self._src("yiban", "fyiban", "protocol.py")
        for forbidden in ("is_initialized", "get_session_cache", "YIBAN_STATE_DIR", "sqlite3"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, src)

    def test_signin_forwards_the_same_objects(self):
        """signin 只是转发（**同一对象**）：类、白名单、WAF 判定、账号复核都不是第二份。"""
        import signin

        from yiban import client as yiban_client
        from yiban.store import accounts

        self.assertIs(signin.YibanClient, yiban_client.YibanClient)
        self.assertIs(signin.is_waf_blocked, fyiban_security.is_waf_blocked)
        self.assertIs(signin._is_fyiban_url, fyiban_security.is_fyiban_url)
        self.assertIs(signin._is_yiban_trusted_url, fyiban_security.is_yiban_trusted_url)
        self.assertIs(signin.WAF_KEYWORDS, fyiban_security.WAF_KEYWORDS)
        self.assertIs(signin.account_still_signable, accounts.account_still_signable)

    def test_client_does_not_reimplement_protocol_steps(self):
        """客户端外观层只管组装：请求构造不得再回到 client.py 里手写。"""
        src = self._src("yiban", "client.py")
        for forbidden in ('"https://', "PKCS1_v1_5", "urlencode("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, src,
                                 "客户端外观层出现了协议细节（应走 yiban.fyiban.protocol）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
