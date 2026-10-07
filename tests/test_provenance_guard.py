# -*- coding: utf-8 -*-
"""来源边界守卫：上游隔离层已退役——**名与路径不得复活**，许可随库、实现在位。

标签：K · 登录协议与自研链路
覆盖：退役名的复活（生产树不得再出现 `fyiban` / `onefeifan` 字样）、`yiban/_vendor`
    路径与 import 串的复活、MIT 许可随洁净室协议库在位、核心实现（几何采样 /
    平台事实常量 / 协议端点）各自单点定义、`signin` 只做同名转发（同一对象）、
    客户端外观层不重写协议步骤、风控挑战检测归属与委托、协议层不自算安全裁决、
    自交与求解器不复活、根许可以及"无 AGPL 声明指向已退役代码"。
对应实现：yiban/geo.py、yiban/platform.py、yiban/challenge.py、yiban/protocol/、
    scripts/signin.py（同名转发）、yiban/security.py（被注入的白名单）。
关键断言：退役的隔离层（`yiban/fyiban/`）与 vendored 层（`yiban/_vendor/`）**都不能
    回来**——它们的存在本身就是本批要消除的"同步税 + 派生面"。判据是**文本级 grep**：
    生产树里任何一处 `fyiban` / `onefeifan` / `yiban._vendor` 字样都判红，因为
    "第二个定义点/第二份实现"最早就长在这样一处引用上。策略（URL 白名单、WAF
    判定、脱敏）属本项目层，必须由调用方注入——协议层自带一份就等于把策略圈进
    改不动的范围。
已知误报形状（**不许为此放宽本守卫**）：JSON 转义 `\ufeff` 紧接 `YIBAN_` 会拼出
    `fyiban` 这六个字母（`tests/test_env_line_model_unified.py` 里有实例，测试不在
    本守卫扫描面内）。生产树若出现同样形状，处理办法是改写那一处字面量（把 BOM 写成
    `\N{ZERO WIDTH NO-BREAK SPACE}`，或把常量名与转义拆到两行），**不是**把判据改成
    词边界匹配——放宽会漏掉 `fyiban_algo` 这类真实复活形态。
依赖：读源码文本做结构断言 + 直接调用模块函数；不发网络请求、不建库。无 skip。
"""
import io
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import signin  # noqa: E402

from yiban import challenge as yiban_challenge  # noqa: E402
from yiban import geo as yiban_geo  # noqa: E402
from yiban import platform as yiban_platform  # noqa: E402
from yiban import security as yiban_security  # noqa: E402

#: 生产/运行树：退役名的复活只在这些目录里判（tests/ 与 docs/ 不在内——它们记录
#: 历史与守卫本身，含这些字样是应当的）。
PRODUCTION_DIRS = ("yiban", "web", "scripts", "docker", "frontend", "config", "deploy")
#: 生产树根级配置文件（同为运行面，一并判）。
PRODUCTION_FILES = ("run.sh", "run_probe.sh", "docker-compose.yml", "pyproject.toml",
                    ".env.example", ".env.docker.example")
#: 退役的上游名（大小写不敏感）。
RETIRED_NAMES = ("fyiban", "onefeifan")
#: 不扫的目录名与二进制后缀（内容非文本，与"字样复活"无关）。
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", ".vite"}
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".ico", ".gif", ".webp", ".woff", ".woff2",
                 ".ttf", ".otf", ".gz", ".zip", ".tar", ".map", ".pyc", ".so")

# 一点真实量级的围栏（南京，约 1km）：退化判定按度算
SQUARE = [(118.88, 31.92), (118.90, 31.92), (118.90, 31.94), (118.88, 31.94)]


def _production_files():
    """产出生产树里的文本文件相对路径（相对仓库根，用 `/` 分隔）。"""
    for directory in PRODUCTION_DIRS:
        root = os.path.join(BASE, directory)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if name.lower().endswith(SKIP_SUFFIXES):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), BASE).replace(os.sep, "/")
                yield rel
    for name in PRODUCTION_FILES:
        if os.path.isfile(os.path.join(BASE, name)):
            yield name


def _read(rel):
    with io.open(os.path.join(BASE, rel), encoding="utf-8", errors="ignore") as f:
        return f.read()


def _source(rel):
    with io.open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


class RetiredLayerRevivalTest(unittest.TestCase):
    """退役层不得复活：名、路径、import 串三样都要判。"""

    def test_retired_upstream_names_do_not_revive(self):
        """生产树不得再出现退役名的字样（大小写不敏感）。

        判据是文本级 grep，不是"有没有功能"：一处 docstring 里的"来源"字样就能让
        读者以为隔离层还在，下一代改动会顺着它把旧实现搬回来。
        """
        hits = []
        for rel in _production_files():
            text = _read(rel).lower()
            for token in RETIRED_NAMES:
                if token in text:
                    hits.append(f"{rel}: {token}")
        self.assertEqual(hits, [],
                         "生产树出现退役的上游名（隔离层名与路径不得复活）：" + "; ".join(hits))

    def test_vendored_layer_path_does_not_revive(self):
        """`yiban/_vendor/` 与 `yiban._vendor` 引用都不得复活。

        洁净室协议库已提升为一等模块 `yiban/protocol/`，不再有"跟库 re-vendor"的
        同步税；任何一份 `_vendor` 路径都说明同步纪律又回来了。
        """
        self.assertFalse(os.path.exists(os.path.join(BASE, "yiban", "_vendor")),
                         "yiban/_vendor/ 目录不得复活")
        hits = []
        for rel in _production_files():
            text = _read(rel)
            for token in ("yiban._vendor", "yiban/_vendor"):
                if token in text:
                    hits.append(f"{rel}: {token}")
        self.assertEqual(hits, [], "生产树出现 vendored 路径引用：" + "; ".join(hits))

    def test_agpl_declaration_does_not_point_at_retired_code(self):
        """退役后不得留下指向已删除代码的 AGPL 声明（许可文本只在 `yiban/protocol/`）。"""
        self.assertFalse(os.path.exists(os.path.join(BASE, "yiban", "fyiban")),
                         "yiban/fyiban/ 隔离层必须整体退役")
        licences = []
        for dirpath, dirnames, filenames in os.walk(os.path.join(BASE, "yiban")):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if name in ("LICENSE", "PROVENANCE.md", "COPYING"):
                    licences.append(
                        os.path.relpath(os.path.join(dirpath, name), BASE).replace(os.sep, "/"))
        self.assertEqual(licences, ["yiban/protocol/LICENSE"],
                         "yiban/ 下只允许洁净室协议库的 MIT 许可文本：" + "; ".join(licences))

    def test_root_license_stays_agpl(self):
        """项目整体许可不变：根 `LICENSE` 仍是 AGPL-3.0 全文。"""
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", _source("LICENSE"))


class ProtocolLibraryTest(unittest.TestCase):
    """洁净室协议库：一等模块，MIT 许可随代码，隔离层只引用不复制。"""

    def test_library_is_located_with_its_license(self):
        for rel in ("yiban/protocol/__init__.py", "yiban/protocol/LICENSE",
                    "yiban/protocol/position.py", "yiban/protocol/forms.py",
                    "yiban/protocol/crypto.py", "yiban/protocol/envelopes.py"):
            with self.subTest(rel=rel):
                self.assertTrue(os.path.isfile(os.path.join(BASE, rel)), f"缺文件: {rel}")
        self.assertIn("MIT License", _source("yiban/protocol/LICENSE"))
        self.assertNotIn("AFFERO", _source("yiban/protocol/LICENSE"))

    def test_consumers_import_the_library_instead_of_reimplementing(self):
        """消费方一律 `from yiban.protocol import …`，不再内联页面正则或表单字段表。"""
        for rel in ("yiban/platform.py", "yiban/challenge.py"):
            with self.subTest(rel=rel):
                self.assertIn("yiban.protocol", _source(rel))


OWNERS = {
    r"(?m)^def point_in_polygon\(": {"yiban/geo.py"},
    r"(?m)^def generate_position_in_polygon\(": {"yiban/geo.py"},
    r'(?m)^YIBAN_APP_VERSION = "': {"yiban/platform.py"},
    r"(?m)^HEADERS = \{": {"yiban/platform.py"},
    r"(?m)^KILLYIBAN_HEADERS = \{": {"yiban/platform.py"},
    r"(?m)^def looks_like_challenge\(": {"yiban/challenge.py",
                                        "yiban/protocol/envelopes.py"},
    r"(?m)^CHALLENGE_DETECTED_MESSAGE = \(": {"yiban/challenge.py"},
}


class SingleOwnerTest(unittest.TestCase):
    """每个核心事实只许在**指定的文件**里定义（换个目录再抄一份就是第二份实现）。

    `looks_like_challenge` 有两个合法定义点，各司其职：协议库提供**检测本体**
    （`yiban/protocol/envelopes.py`），本项目的适配层在 `yiban/challenge.py` 追加
    本项目观察到的特征并转调库——第三处出现即判红。
    """


    def test_owners_define_their_facts(self):
        for pattern, owners in OWNERS.items():
            for rel in sorted(owners):
                with self.subTest(rel=rel, pattern=pattern):
                    self.assertRegex(_source(rel), pattern)

    def test_no_second_definition_anywhere_in_production(self):
        hits = []
        for rel in _production_files():
            if not rel.endswith(".py"):
                continue
            text = _read(rel)
            for pattern, owners in OWNERS.items():
                if re.search(pattern, text) and rel not in owners:
                    hits.append(f"{rel}: {pattern}")
        self.assertEqual(hits, [], "核心事实出现了第二个定义点：" + "; ".join(hits))

    def test_legacy_algorithm_helpers_do_not_revive(self):
        """旧实现的内部构件（剪耳剖分、三角形内缩、上游缩放系数）不得复活。"""
        gone = (r"(?m)^def _ear_clip_triangles\(", r"(?m)^def _sample_by_triangulation\(",
                r"(?m)^def _generate_by_scaled_centroid\(", r"(?m)^_TRI_SHRINK_RATIO = ",
                r"(?m)^SCALE_FACTOR = ")
        hits = []
        for rel in _production_files():
            if not rel.endswith(".py"):
                continue
            text = _read(rel)
            for pattern in gone:
                if re.search(pattern, text):
                    hits.append(f"{rel}: {pattern}")
        self.assertEqual(hits, [], "旧算法的构件复活了：" + "; ".join(hits))

    def test_signin_has_no_second_implementation(self):
        src = _source("scripts/signin.py")
        for pattern in (r"(?m)^def point_in_polygon\(",
                        r"(?m)^def generate_position_in_polygon\(",
                        r'(?m)^YIBAN_APP_VERSION = "',
                        r"(?m)^HEADERS = \{",
                        r"(?m)^KILLYIBAN_HEADERS = \{"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, src),
                                  "signin.py 又出现了一份实现（应为转发引用）")


class BehaviorWiringTest(unittest.TestCase):
    def test_geometry_sampling_stays_inside(self):
        """几何采样入口仍必须交出界内点（调用方直接解包，无 None 保护）。"""
        for _ in range(30):
            point = yiban_geo.generate_position_in_polygon(SQUARE)
            self.assertIsNotNone(point)
            self.assertTrue(yiban_geo.point_in_polygon(point[0], point[1], SQUARE),
                            f"生成的坐标落在签到范围外：{point}")

    def test_app_version_is_consistent_across_headers(self):
        """两处请求头必须同值（服务端一致性校验），改一处必须改另一处。"""
        self.assertEqual(yiban_platform.KILLYIBAN_HEADERS["AppVersion"],
                         yiban_platform.YIBAN_APP_VERSION)
        self.assertIn(yiban_platform.YIBAN_APP_VERSION,
                      yiban_platform.HEADERS["User-Agent"])

    def test_header_keys_pinned(self):
        self.assertEqual(set(yiban_platform.KILLYIBAN_HEADERS),
                         {"User-Agent", "AppVersion", "Origin", "Referer", "Connection"})
        self.assertIn("X-Requested-With", yiban_platform.HEADERS)
        self.assertEqual(yiban_platform.HEADERS["Origin"], "https://app.uyiban.com")
        self.assertEqual(yiban_platform.KILLYIBAN_HEADERS["Origin"], "https://c.uyiban.com")
        self.assertEqual(yiban_platform.APP_SIGN_HEADERS["Origin"], "https://app.uyiban.com")

    def test_signin_reexports_same_objects(self):
        """signin 只是同名转发（**同一对象**）——不是第二份实现。"""
        self.assertIs(signin.YIBAN_APP_VERSION, yiban_platform.YIBAN_APP_VERSION)
        self.assertIs(signin.HEADERS, yiban_platform.HEADERS)
        self.assertIs(signin.KILLYIBAN_HEADERS, yiban_platform.KILLYIBAN_HEADERS)
        self.assertIs(signin.point_in_polygon, yiban_geo.point_in_polygon)
        self.assertIs(signin.generate_position_in_polygon,
                      yiban_geo.generate_position_in_polygon)


class ChallengeDetectionTest(unittest.TestCase):
    def test_detection_delegates_to_library(self):
        """检测本体委托洁净室库：已知挑战标记命中、正常 JSON 不误报。"""
        self.assertTrue(yiban_challenge.looks_like_challenge("<html>ydclearance</html>"))
        self.assertTrue(yiban_challenge.looks_like_challenge(
            "Set-Cookie: https_ydclearance=abc", "https_ydclearance=abc"))
        self.assertTrue(yiban_challenge.looks_like_challenge(
            '<script>window.onload=setTimeout("yy(1)",200);eval("qo=eval;qo(po);");</script>'))
        self.assertFalse(yiban_challenge.looks_like_challenge('{"code":0,"msg":""}'))

    def test_solver_is_gone_and_detection_hit_fails_loudly(self):
        """求解器已按既定裁决删除（生产全历史零触发）；检测命中由协议层响亮失败。"""
        self.assertFalse(hasattr(yiban_challenge, "solve_ydclearance"),
                         "生产零触发的挑战求解器必须删除，不得以第二份实现复活")
        self.assertIn("ydclearance", yiban_challenge.CHALLENGE_DETECTED_MESSAGE)
        self.assertTrue(yiban_security.is_hard_fail_message(
            yiban_challenge.CHALLENGE_DETECTED_MESSAGE),
            "检测命中必须落不可重试硬失败档（同一挑战重发无益）")

    def test_module_has_no_security_policy_of_its_own(self):
        """本模块不得自带 URL 白名单或求解实现（安全策略属本项目，须注入）。"""
        src = _source("yiban/challenge.py")
        for forbidden in ("_is_fyiban_url", "is_yiban_trusted_url", "urlsplit", "urlparse",
                          "allow_url"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, src)
        self.assertIn("yiban.protocol", src)


class ProtocolLayerTest(unittest.TestCase):
    """协议编排层（`yiban/platform.py`）的边界：端点单点定义，安全策略只在注入点。"""

    def test_endpoints_defined_only_in_platform_layer(self):
        """端点与客户端标识只能定义在协议层：别处再出现一份就是第二份实现。"""
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
            src = _source("/".join(owner))
            for pattern in patterns:
                with self.subTest(owner="/".join(owner), pattern=pattern):
                    self.assertIsNone(
                        re.search(pattern, src),
                        f"{'/'.join(owner)} 里出现了易班端点字面量（应引用 protocol 常量）")
        protocol_src = _source("yiban/platform.py")
        self.assertRegex(protocol_src, r'(?m)^OAUTH_CLIENT_ID = "95626fa3080300ea"')
        self.assertRegex(protocol_src,
                         r'(?m)^OAUTH_USERSURE_URL = "https://oauth\.yiban\.cn/code/usersure"')

    def test_security_policy_is_injected_not_inlined(self):
        """协议层不得自带 WAF 关键词或**域名判定**（那些属 `yiban/security.py`，须注入）。

        端点常量本身当然含 `yiban.cn`（那是平台事实），所以判据不是"出现域名"，
        而是"**算出裁决**"：不得有白名单函数、不得做主机后缀比对、不得内联拦截词。
        """
        src = _source("yiban/platform.py")
        for forbidden in ("WAF_KEYWORDS", "风险访问", "访问服务禁用",
                          "def is_strict_yiban_url", "def is_yiban_trusted_url",
                          "urlsplit", "endswith(", "hostname =="):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, src, "协议层出现了安全判定（应由 policy 注入）")
        self.assertRegex(src, r"policy\.require_trusted\(")
        self.assertRegex(src, r"policy\.require_not_blocked\(")
        self.assertRegex(src, r"policy\.is_blocked\(")
        # 挑战：检测委托洁净室库，命中即用常量响亮失败（不再求解、不再注入白名单）
        self.assertRegex(src, r"challenge\.looks_like_challenge\(")
        self.assertRegex(src, r"raise RuntimeError\(challenge\.CHALLENGE_DETECTED_MESSAGE\)")

    def test_protocol_layer_has_no_own_session_persistence(self):
        """会话缓存只能经 session_store 注入：协议层不得自己碰库或状态文件。"""
        src = _source("yiban/platform.py")
        for forbidden in ("is_initialized", "get_session_cache", "YIBAN_STATE_DIR", "sqlite3"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, src)

    def test_platform_layer_does_not_import_business_modules(self):
        """协议层不得依赖业务层/安全层（策略靠注入）。"""
        bad = []
        for rel in ("yiban/platform.py", "yiban/challenge.py", "yiban/geo.py"):
            src = _source(rel)
            for m in re.finditer(r"(?m)^\s*(?:import|from)\s+([\w.]+)", src):
                dotted = m.group(1)
                if dotted.split(".")[0] in ("db", "signin", "notify", "mailer", "web") or \
                        dotted.startswith(("yiban.store", "yiban.security", "yiban.client",
                                           "yiban.masking", "yiban.clock")):
                    bad.append(f"{rel} → {dotted}")
        self.assertEqual(bad, [], "协议层不得依赖业务层/安全层（策略靠注入）：" + "; ".join(bad))

    def test_signin_forwards_the_same_objects(self):
        """signin 只是转发（**同一对象**）：类、白名单、WAF 判定、账号复核都不是第二份。"""
        from yiban import client as yiban_client
        from yiban.store import accounts

        self.assertIs(signin.YibanClient, yiban_client.YibanClient)
        self.assertIs(signin.is_waf_blocked, yiban_security.is_waf_blocked)
        self.assertIs(signin._is_strict_yiban_url, yiban_security.is_strict_yiban_url)
        self.assertIs(signin._is_yiban_trusted_url, yiban_security.is_yiban_trusted_url)
        self.assertIs(signin.WAF_KEYWORDS, yiban_security.WAF_KEYWORDS)
        self.assertIs(signin.account_still_signable, accounts.account_still_signable)

    def test_client_does_not_reimplement_protocol_steps(self):
        """客户端外观层只管组装：请求构造不得再回到 client.py 里手写。"""
        src = _source("yiban/client.py")
        for forbidden in ('"https://', "PKCS1_v1_5", "urlencode("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, src,
                                 "客户端外观层出现了协议细节（应走 yiban.platform）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
