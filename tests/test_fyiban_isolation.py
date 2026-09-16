# -*- coding: utf-8 -*-
"""第三方隔离层（`yiban/fyiban/`）的**保护存在性断言**与边界守卫。

背景（PROMPT 核心目标 1）：沿用上游 `onefeifan/fyiban`（AGPL-3.0）的定位算法与协议
特征必须圈在一处、可独立核对替换。抽层的风险是"抽着抽着改了行为"或"留下第二份实现"，
故本文件同时钉两类东西：

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
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import signin  # noqa: E402

from yiban.fyiban import algo as fyiban_algo  # noqa: E402
from yiban.fyiban import headers as fyiban_headers  # noqa: E402
from yiban.fyiban import waf as fyiban_waf  # noqa: E402

# 一个 1km 量级的近似方形多边形（经纬度，南京）
SQUARE = [(118.88, 31.92), (118.90, 31.92), (118.90, 31.94), (118.88, 31.94)]


class AlgoBehaviorTest(unittest.TestCase):
    def test_point_in_polygon_basic(self):
        inside = (118.89, 31.93)
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
        self.assertIn("X-Requested-With", fyiban_headers.HEADERS)
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
    def test_unrecognized_text_fails_loudly(self):
        """无法识别的挑战页必须抛错：静默返回空 cookie 会让登录少走一步且难排查。"""
        with self.assertRaises(RuntimeError):
            fyiban_waf.solve_ydclearance("<html>not a challenge</html>",
                                         allow_url=lambda u: True)

    def test_whitelist_policy_is_injected_not_inlined(self):
        """白名单由调用方注入：signin 的包装必须传它自己的 `_is_fyiban_url`。"""
        seen = {}

        def _fake(text, allow_url):
            seen["text"] = text
            seen["allow_url"] = allow_url
            return ("cookie", "https://f.yiban.cn/x")

        with mock.patch.object(fyiban_waf, "solve_ydclearance", _fake):
            out = signin.YibanClient._solve_ydclearance(object(), "CHALLENGE")
        self.assertEqual(seen["text"], "CHALLENGE")
        self.assertIs(seen["allow_url"], signin._is_fyiban_url)
        self.assertEqual(out, ("cookie", "https://f.yiban.cn/x"))

    def test_waf_module_has_no_security_policy_of_its_own(self):
        """第三方层不得自带 URL 白名单实现（安全策略属本项目，须注入）。

        判据是"有没有自己的域名校验"，不是"调没调 allow_url"——调用注入的策略正是契约。
        本项目的策略实现用 `urlsplit` 做精确主机比对，故这里以"不得出现该实现"为准。
        """
        src = io.open(os.path.join(BASE, "yiban", "fyiban", "waf.py"),
                      encoding="utf-8").read()
        self.assertNotIn("_is_fyiban_url", src)
        self.assertNotIn("urlsplit", src)
        self.assertNotIn("urlparse", src)
        # 注入契约在位：allow_url 参与判定且判定失败必须响亮失败
        self.assertRegex(src, r"if not allow_url\(target\):")
        self.assertRegex(src, r"raise RuntimeError\(\"ydclearance 跳转目标不在白名单")


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
        self.assertRegex(self._fyiban_src("waf.py"), r"(?m)^def solve_ydclearance\(")

    def test_license_and_provenance_travel_with_code(self):
        """AGPL §5 要求修改声明与许可随代码分发——两件必须在目录里。"""
        lic = self._fyiban_src("LICENSE")
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", lic)
        prov = self._fyiban_src("PROVENANCE.md")
        self.assertIn("onefeifan/fyiban", prov)
        self.assertIn("AGPL-3.0", prov)
        self.assertRegex(prov, r"\bd854182\b", "需记录核对的上游提交号")
        # 逐块对照表至少要覆盖三个实现文件
        for f in ("algo.py", "headers.py", "waf.py"):
            with self.subTest(file=f):
                self.assertIn(f, prov)

    def test_layer_does_not_import_business_modules(self):
        bad = []
        for name in ("algo.py", "headers.py", "waf.py", "__init__.py"):
            src = self._fyiban_src(name)
            for m in re.finditer(r"(?m)^\s*(?:import|from)\s+([\w.]+)", src):
                dotted = m.group(1)
                if dotted.split(".")[0] in ("db", "signin", "notify", "mailer", "web") or \
                        dotted.startswith(("yiban.store", "yiban.security", "yiban.client")):
                    bad.append(f"{name} → {dotted}")
        self.assertEqual(bad, [], "第三方层不得依赖业务层/安全层（策略靠注入）：" + "; ".join(bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
