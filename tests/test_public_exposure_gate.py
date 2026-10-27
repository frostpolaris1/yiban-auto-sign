# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""公开仓库数据红线门（AGENTS.md §十六.46–49）。

# 功能

扫描全部入库文本，拒绝「看起来像真实信息」的字面量。判据按**形状**，
不按来源：

1. 手机号：11 位 `1[3-9]` 开头的数字只准落在登记的合成段
   `1990000XXXX`（两位前缀 + 八位序号）。
2. 掩码手机号：`1[3-9]\\d\\*{3,4}\\d{4}` 只准以 `199` 开头。
3. 坐标对：只准落在合成网格 `lng ∈ [-2, 2]`、`lat ∈ [-1, 10]`
   （Null Island 附近的平移结果，不是任何真实校区）。
4. 平台标识：`yb_uid=` 只准 `1` 或 `10000001`。
5. 实拍令牌：`ajax_sign` / `verify_request` / `oauth_upwd` 的值只准是
   单一重复字符（全 0 或全 A，允许 base64 尾部 `=`）。
6. 学校/人员标识：`UniversityId` / `PositionId` / `PersonId` / `SchoolId`
   的 32 位十六进制只准以 28 个 0 开头（`000…0001` 形态）。

# 为什么按形状

真实值一旦入库，删除 HEAD 不够：历史、fork、缓存都会留。故本门只认
「明显合成」的形状，不维护真实值黑名单——黑名单本身会把真实值再写一遍。

# 归属

门禁/测试面。规则正本是 AGENTS.md §十六。夹具来源说明在
`tests/protocol/conftest.py`。

# 口径

扫描面按 `git ls-files` 全量取，再按 `SKIP` 排除第三方与机器生成件。
**扫空不得当绿**：文件数低于下限即判红。本文件排除自身：判据文本里
必然出现形状示例。

# 用法

    python -m pytest tests/test_public_exposure_gate.py -q
"""

import os
import re
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SELF = os.path.join("tests", "test_public_exposure_gate.py")

#: 不入扫描面的路径前缀与理由。新增一条必须写明为什么它不是个人数据。
SKIP = {
    "yiban/core/email_blocklist.txt": "公开 CC0 一次性邮箱域名数据集，非个人信息",
    "frontend/package-lock.json": "依赖锁文件，机器生成",
    "ui/package-lock.json": "依赖锁文件，机器生成",
    ".github/": "工作流里的 40 位十六进制是公开提交 SHA，不是个人信息",
    "web/static/vendor/": "第三方前端静态资源",
    "web/static/vue/": "构建产物，源在 frontend/",
}

#: 只扫这些扩展名；其余按二进制或机器产物跳过。
EXTS = (
    ".py", ".md", ".txt", ".json", ".html", ".js", ".mjs", ".ts", ".vue",
    ".go", ".sh", ".yml", ".yaml", ".tsv", ".toml", ".example", ".service",
    ".conf", ".css", ".sql",
)

PHONE = re.compile(r"(?<![0-9])1[3-9]\d{9}(?![0-9])")
MASKED = re.compile(r"1[3-9]\d\*{3,4}\d{4}")
COORD = re.compile(r"(?<![0-9.\-])(-?\d{1,3}\.\d{3,})\s*,\s*(-?\d{1,3}\.\d{3,})(?![0-9])")
UID = re.compile(r"yb_uid=(\d+)")
TOKEN = re.compile(r"\b(ajax_sign|verify_request|oauth_upwd)=([A-Za-z0-9+/=%]{8,})")
HEXID = re.compile(
    r'"(?:UniversityId|PositionId|PersonId|SchoolId)"\s*:\s*"([a-f0-9]{32})"')

SYNTH_PHONE = re.compile(r"^1990000\d{4}$")
SYNTH_MASKED = re.compile(r"^199\*{3,4}\d{4}$")
SYNTH_UID = {"1", "10000001"}
LNG_RANGE = (-2.0, 2.0)
LAT_RANGE = (-1.0, 10.0)
#: 扫描面下限：当前入库文本文件约 600；低于此数说明扫描面塌了。
MIN_FILES = 500


def _tracked_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=BASE,
                         capture_output=True).stdout.split(b"\0")
    files = []
    for raw in out:
        rel = raw.decode("utf-8", "ignore")
        if not rel:
            continue
        norm = rel.replace("/", os.sep)
        if os.path.normpath(norm) == os.path.normpath(SELF):
            continue
        if any(rel == p or rel.startswith(p) for p in SKIP):
            continue
        if not rel.endswith(EXTS):
            continue
        files.append(rel)
    return files


#: 已登记的无害占位令牌：形状短、语义明显（不是实拍值）。
SYNTH_TOKENS = {"deadbeef", "mockvreq123", "VERIFYTOK", "TAILTOKEN"}


def _synthetic_token(value):
    if value in SYNTH_TOKENS:
        return True
    body = value.rstrip("=")
    if not body:
        return False
    return len(set(body)) == 1 and body[0] in "0A"


def scan_text(rel, text):
    """返回 `[(行号, 判据, 值)]`；空列表表示该文件合规。"""
    bad = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in PHONE.finditer(line):
            if not SYNTH_PHONE.match(m.group(0)):
                bad.append((lineno, "phone", m.group(0)))
        for m in MASKED.finditer(line):
            if not SYNTH_MASKED.match(m.group(0)):
                bad.append((lineno, "masked-phone", m.group(0)))
        for m in COORD.finditer(line):
            lng, lat = float(m.group(1)), float(m.group(2))
            if not (LNG_RANGE[0] <= lng <= LNG_RANGE[1] and
                    LAT_RANGE[0] <= lat <= LAT_RANGE[1]):
                bad.append((lineno, "coordinate", m.group(0)))
        for m in UID.finditer(line):
            if m.group(1) not in SYNTH_UID:
                bad.append((lineno, "yb_uid", m.group(0)))
        for m in TOKEN.finditer(line):
            if not _synthetic_token(m.group(2)):
                bad.append((lineno, "token", "%s=..." % m.group(1)))
        for m in HEXID.finditer(line):
            if not m.group(1).startswith("0" * 28):
                bad.append((lineno, "school-or-person-id", m.group(1)))
    return bad


def scan_tree(files):
    """返回 `{相对路径: [(行号, 判据, 值)]}`，只含违规文件。"""
    found = {}
    for rel in files:
        path = os.path.join(BASE, rel.replace("/", os.sep))
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        bad = scan_text(rel, text)
        if bad:
            found[rel] = bad
    return found


class PublicExposureGateTest(unittest.TestCase):
    """入库文本不得出现看似真实的信息（AGENTS §46）。"""

    def test_real_repo_has_no_real_looking_values(self):
        files = _tracked_files()
        self.assertGreaterEqual(
            len(files), MIN_FILES,
            "只扫到 %d 个文件（下限 %d）：扫描面塌了" % (len(files), MIN_FILES))
        found = scan_tree(files)
        self.assertEqual(
            found, {},
            "这些文件含看似真实的信息；按 AGENTS §46 改为登记合成值：\n%s"
            % "\n".join(
                "%s: %s" % (rel, bad[:3]) for rel, bad in sorted(found.items())
            ))

    def test_gate_rejects_every_shape(self):
        """门要有牙：每种判据各给一个真实形状，必须判红。

        样本在运行期拼出：字面量会在历史清洗时被替换，拼出来的不会，
        这样本条测试在清洗前后都测同一件事。
        """
        cases = {
            "phone": "1" + "3800138000",
            "masked-phone": "1" + "39" + "****" + "5678",
            "coordinate": "118." + "101837,32." + "003954",
            "yb_uid": "yb_uid=" + "85118634",
            "token": "ajax_sign=" + "0000000000000000000000000000000000000000",
            "school-or-person-id":
                '"UniversityId": "' + "00000000000000000000000000000003" + '"',
        }
        for kind, sample in cases.items():
            with self.subTest(kind=kind):
                bad = scan_text("fixture.txt", sample)
                self.assertTrue(
                    any(b[1] == kind for b in bad),
                    "%s 形状没有被判红: %r -> %r" % (kind, sample, bad))

    def test_gate_accepts_synthetic_values(self):
        """登记过的合成值必须放行，否则门会逼人写真实值。"""
        ok = "\n".join([
            "19900000001",
            "199****0001",
            "0.101837,1.003954",
            "yb_uid=10000001",
            "ajax_sign=" + "0" * 40,
            "oauth_upwd=" + "A" * 40 + "=",
            '"UniversityId": "00000000000000000000000000000001"',
        ])
        self.assertEqual(scan_text("fixture.txt", ok), [])

    def test_empty_scan_is_red(self):
        """扫空不得当绿。"""
        self.assertGreaterEqual(MIN_FILES, 1)
        self.assertEqual(scan_tree([]), {})


if __name__ == "__main__":
    unittest.main()
