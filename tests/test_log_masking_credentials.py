# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""日志输出面的**凭据字面量**兜底：`MaskingFormatter` 从"只遮手机号"扩到遮凭据。

脱敏此前分两层：输出面兜底只认 11 位手机号，口令/token/cookie 靠各调用点自觉先过
`masking.sanitize_text` —— 没过它的调用点（含日志里内嵌的上游回显文本）不被救回。
本文件钉住扩面后的那层：整行成文之后，凭据形态的 `key=value` 一律归 `***`，与调用点
是否自觉解耦。

两个面**同一份键名词表、同一份实现，只在"值取到哪"上按输入域分叉**（刻意，见
`masking._mask_credential_literals`）：调用点面处理**上游文本**，值取到"下一个像是
键值对"处（防"截断残留"把凭据尾巴留成正文）；输出面处理**我们自己的成文行**，
值止于空白——成文行里的散文（如配置告警的解释句）不得被吞。分叉由本文件的
`ProseAfterCredentialKeySurvivesTest` 与 `CallSiteFaceUnchangedTest` 两侧同时钉住。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`MaskingFormatter` 对凭据字面量的遮蔽（裸/引号/JSON 键、authorization 方案名、
   cookie 多对、Account repr、异常栈文本、真实文件 handler 落盘）、幂等、与
   `masking.mask_credentials_in_text` 的接线同源、真实日志行的形状不变、已知过遮面
   登记、调用点面（`sanitize_text`）贪心取值不回退。
对应实现：`yiban/logging_ext.py` 的 `MaskingFormatter.format`、
   `yiban/masking.py` 的 `mask_credentials_in_text` / `_mask_credential_literals` /
   `sanitize_text`。
关键断言：**遮蔽发生在"最终成文之后"这一层**（覆写 `Formatter.format` 的返回串），
   且用的是与调用点面**同一个** `mask_credentials_in_text` 对象（换成自备的第二份
   规则即红）；反向控制同权重——真实日志行（含配置告警的解释句、坐标、时间戳、
   非凭据 `key=value`）逐条原样穿过，误伤会让日志失去排障价值。
它**不覆盖**：没挂 `MaskingFormatter` 的 handler（三入口各自的挂载由
   `tests/test_log_masking_entry_mounts.py` 钉住）；键名被百分号编码的写法
   （`tok%65n=v`，与 `sanitize_text` 同一登记面）；**含空格的裸凭据值**只遮到第一个
   空白前（输出面值止于空白，代价见上）；展示/导出面（`web/services/logs.py`）仍是
   号码口径，本批不改——那是读面，与写面不是同一条出口。
依赖：无网络、无 skip；固定 `rec.created` 钉住时间戳；真实文件 handler 用临时文件。
用法（项目根目录）：
    bash scripts/dev-verify.sh --target tests/test_log_masking_credentials.py
"""
import logging
import os
import sys
import tempfile
import unittest

from yiban import logging_ext, masking
from yiban.logging_ext import MaskingFormatter
from yiban.masking import (
    mask_credentials_in_text,
    mask_phones_in_text,
    sanitize_text,
)

FORMAT = "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

#: 真实日志行（逐条取自仓库调用点，含配置告警的解释句、坐标、时间戳、非凭据键值对）：
#: 它们必须**逐字穿过**输出面。误伤（值被吞、散文被吃）会让日志失去排障价值。
LEGIT_LINES = (
    "[138****0000] ⏳ 待重试（已 2 次）: 网络异常，稍后重试",
    "[worker-3] v3 执行体：6 条通道 / 64 个分片",
    "[fallback r3] 本轮处理 0 个账号（0 个已由他人负责，0 个已由他人领取，0 个无人接手），"
    "事件驱动等待（最多 60s）",
    "邮件通知部分收件人被拒（SMTP 条目 1/2 host=smtp.example.com，id=abc123）: 550 5.1.1",
    "账号密钥自证：env 档与 /srv/app/.env 文件档为同一把钥（kid=1f0a2b）",
    "多执行体：共 96 个账号待签，拉起 8 个执行体（槽位 [0, 1, 2, 3]）",
    "[fallback r1] 签到时段尚未开始（1500 秒后开始），60 秒后再看",
    "检测到 HTTPS（或可信反代的转发头），会话 Cookie 自动启用 Secure",
    "自定义通知地址未通过白名单校验，已拒发: host=smtp.example.com",
    "[138****0000] ⏹️ 用户已取消签到，跳过执行",
)

#: 凭据形态的样例：`(原串, 必须出现, 必须消失)`。键名形态覆盖复合名/引号键/方案名。
CRED_CASES = (
    ("登录回显 refresh_token=abc123def456 已忽略", "refresh_token=***", "abc123def456"),
    ("password: hunter2", "password=***", "hunter2"),
    ("Authorization: Bearer abc.def.ghi", "authorization=***", "abc.def.ghi"),
    ("Authorization: Basic ZGVmOg==", "authorization=***", "ZGVmOg"),
    ("Authorization: Digest d2c3f4a5b6", "authorization=***", "d2c3f4a5b6"),
    ("cookie: SID=abc123; path=/", "cookie=***", "abc123"),
    ("JSESSIONID=abc123xyz", "JSESSIONID=***", "abc123xyz"),
    ("api_key=AKIAIOSFODNN7EXAMPLE", "api_key=***", "AKIAIOSFODNN7EXAMPLE"),
    ("phone_code=123456", "phone_code=***", "123456"),
    ("上游返回 {\"access_token\": \"abc-def\"} 不可用", '"access_token": "***"', "abc-def"),
    ("Account(name='x', password='pw-1', cookie='c-1')", "Account(***)", "pw-1"),
    ("token=\"quoted secret\"", "token=***", "quoted secret"),
)


def _format(msg, exc_info=None, name="yiban"):
    """把一条日志记录交给 MaskingFormatter 格式化，返回最终输出串。"""
    rec = logging.LogRecord(name, logging.INFO, __file__, 1, msg, None, exc_info)
    rec.created = 1758500000.0  # 固定时间戳，避免挂钟影响断言
    return MaskingFormatter(FORMAT, datefmt=DATEFMT).format(rec)


class CredentialLiteralMaskedTest(unittest.TestCase):
    """输出面兜底：凭据形态的字面量在成文之后归 `***`。"""

    def test_credential_cases_masked(self):
        for raw, must_in, must_out in CRED_CASES:
            with self.subTest(raw=raw):
                out = _format(raw)
                self.assertIn(must_in, out, f"凭据未遮：{raw}")
                self.assertNotIn(must_out, out, f"凭据明文外泄：{raw}")

    def test_exception_text_credential_masked(self):
        """异常栈文本由 `Formatter.format` 追加，只有对最终串替换才兜得住。"""
        try:
            raise RuntimeError("上游回显 refresh_token=abc123def456 失败")
        except RuntimeError:
            out = _format("签到异常", exc_info=sys.exc_info())
        self.assertIn("refresh_token=***", out)
        self.assertNotIn("abc123def456", out)

    def test_phone_and_credential_masked_in_one_pass(self):
        """两族同一趟里都要生效：扩面不得挤掉原有的号码口径。"""
        out = _format("[18951982278] ⏹️ 用户已取消签到，跳过执行 cookie: SID=abc123")
        self.assertIn("189****2278", out)
        self.assertNotIn("18951982278", out)
        self.assertNotIn("abc123", out)

    def test_output_idempotent(self):
        once = _format("refresh_token=abc123def456 已忽略")
        twice = _format("refresh_token=*** 已忽略")
        self.assertIn("refresh_token=***", once)
        self.assertEqual(once.replace(" 已忽略", ""), twice.replace(" 已忽略", ""),
                         "已遮形态与首次输出必须同形（重复脱敏不得再变形）")

    def test_file_handler_output_has_no_credential(self):
        """经真实文件 handler（输出面）落盘后，文件里不得有凭据明文。"""
        fd, path = tempfile.mkstemp(prefix="yiban-credmask-", suffix=".log")
        os.close(fd)
        try:
            handler = logging.FileHandler(path, encoding="utf-8")
            handler.setFormatter(MaskingFormatter(FORMAT, datefmt=DATEFMT))
            lg = logging.getLogger("yiban.test.credmask")
            lg.addHandler(handler)
            lg.setLevel(logging.INFO)
            lg.propagate = False
            try:
                lg.info("登录失败 password: hunter2 refresh_token=abc123def456")
            finally:
                lg.removeHandler(handler)
                handler.close()
            with open(path, encoding="utf-8") as f:
                body = f.read()
            self.assertNotIn("hunter2", body)
            self.assertNotIn("abc123def456", body)
            self.assertIn("password=***", body)
        finally:
            os.remove(path)


class LegitLinesUnchangedTest(unittest.TestCase):
    """反向控制：真实日志行的正文逐字穿过（误伤与漏遮同权重）。"""

    def test_real_log_lines_pass_through_verbatim(self):
        for line in LEGIT_LINES:
            with self.subTest(line=line):
                self.assertIn(line, _format(line))


class ProseAfterCredentialKeySurvivesTest(unittest.TestCase):
    """输出面的取值口径：值止于空白，**值后面的散文不得被吞**。

    配置类告警把"哪个键、什么值、为什么非法、回退成什么"写在同一行里；若值取到行尾，
    整句解释会连同散文一起进 `***`，排障信息凭空消失。这是输出面与调用点面唯一的
    分叉点（调用点面对上游文本仍取贪心值，防"截断残留"）。
    """

    def test_config_warning_keeps_its_explanation(self):
        raw = "配置 YIBAN_SESSION_TTL_HOURS=12 非法，回退默认 168 小时"
        out = _format(raw)
        self.assertIn("YIBAN_SESSION_TTL_HOURS=***", out, "键值对本身仍要遮")
        self.assertIn("非法，回退默认 168 小时", out, "值后面的解释句被吞——排障信息丢失")

    def test_bool_config_warning_keeps_its_explanation(self):
        raw = "配置 YIBAN_NOTIFY_SECRET=true 非法（既非真值也非假值），安全件按缺省开处理"
        out = _format(raw)
        self.assertIn("YIBAN_NOTIFY_SECRET=***", out)
        self.assertIn("非法（既非真值也非假值），安全件按缺省开处理", out)

    def test_real_cookie_secure_warning_keeps_its_explanation(self):
        """真实告警（web/app.py 的 YIBAN_COOKIE_SECURE 未开启）——值后紧跟**全角括号、
        中间没有空格**：只按空白终止会把整句解释（`（.env 或环境变量）`）一起吞掉。"""
        raw = ("YIBAN_COOKIE_SECURE 未开启：当前监听地址 10.0.0.9 非回环，生产环境请设置 "
               "YIBAN_COOKIE_SECURE=1（.env 或环境变量），否则登录 Cookie 可能在 HTTPS 下被"
               "浏览器拒绝")
        out = _format(raw)
        self.assertIn("YIBAN_COOKIE_SECURE=***", out)
        self.assertIn("（.env 或环境变量）", out, "值后面的全角括号解释被吞——排障信息丢失")
        self.assertIn("否则登录 Cookie 可能在 HTTPS 下被浏览器拒绝", out)

    def test_value_starting_with_punctuation_still_masked(self):
        """首字符不受终止集约束：凭据值可能以标点开头（`token=(a-b)`）。
        首字符也设限时该行整条不匹配 ⇒ 明文放走，那是**漏**的方向，比过遮重。"""
        out = _format("token=(a-b) 与 refresh_token=[x]")
        self.assertNotIn("a-b", out)
        self.assertNotIn("[x]", out)


class KnownOverMaskTest(unittest.TestCase):
    """如实登记过遮面，不把它当成"无误伤"。

    `token_len` / `session_token_len` 这类**度量键**落进凭据键名表 ⇒ 它的数字值也归
    `***`。刻意不给纯数字豁免：`phone_code=123456` 是同一形态，数字豁免会把它整类
    放走。代价是这一行的长度数字不可读，收益是"宁愿多遮不漏"。
    """

    def test_metric_style_key_value_is_masked(self):
        out = _format("CSRF 校验失败: ip=1.2.3.4 path=/api/x token_len=128 "
                      "session_token_len=64")
        self.assertIn("token_len=***", out)
        self.assertNotIn("128", out)
        self.assertIn("ip=1.2.3.4", out, "非凭据键的值不得连带被遮")


class WiredToSharedPrimitiveTest(unittest.TestCase):
    """接线同源：输出面用的是 `masking` 的同一个原语，不是自备的第二份规则。"""

    def test_formatter_reuses_the_masking_primitive_object(self):
        self.assertIs(logging_ext.mask_credentials_in_text,
                      masking.mask_credentials_in_text,
                      "输出面必须复用 masking 的原语对象（自备第二份规则会与调用点面分叉）")

    def test_output_equals_composition_of_shared_primitives(self):
        line = "[18951982278] ⏹️ 跳过执行 cookie: SID=abc123"
        expected = mask_phones_in_text(mask_credentials_in_text(_format(line)))
        self.assertEqual(_format(line), expected)

    def test_both_faces_share_one_key_vocabulary(self):
        """键名词表只有一份：同一组键在两个面都要被遮。"""
        keys = ("password", "passwd", "pwd", "token", "secret", "cookie",
                "session", "csrf", "authorization", "api_key", "phone_code",
                "refresh_token", "JSESSIONID")
        for key in keys:
            with self.subTest(key=key):
                probe = f"{key}=abc123def456"
                self.assertNotIn("abc123def456", mask_credentials_in_text(probe),
                                 "输出面漏了该键")
                self.assertNotIn("abc123def456", sanitize_text(probe),
                                 "调用点面漏了该键（两面词表已分叉）")


class CallSiteFaceUnchangedTest(unittest.TestCase):
    """调用点面（上游文本）仍是**贪心取值**：防"截断残留"是那条面的既有取舍。"""

    def test_sanitize_text_absorbs_bare_tail(self):
        # 值里的空格不单独终止：`token=abc def` 整段归 `***`（截半等于没遮）
        self.assertEqual(sanitize_text("token=abc def"), "token=***")
        self.assertEqual(sanitize_text("token=\"abc def\""), "token=***")

    def test_sanitize_text_keeps_masking_the_same_shapes(self):
        self.assertEqual(sanitize_text("password: hunter2"), "password=***")
        self.assertEqual(sanitize_text("Authorization: Bearer abc.def"),
                         "authorization=***")
        self.assertEqual(sanitize_text("cookie: SID=abc123; path=/"),
                         "cookie=***; path=/")
        self.assertEqual(sanitize_text("phone_code=123456"), "phone_code=***")
        self.assertEqual(sanitize_text('{"access_token": "a b"}'),
                         '{"access_token": "***"}')
        self.assertEqual(sanitize_text("Account(name=xxxxx, password=yyy)"),
                         "Account(***)")

    def test_sanitize_text_still_escapes_newlines_and_masks_phones(self):
        self.assertEqual(sanitize_text("第一行\n第二行 13800138000"),
                         "第一行\\n第二行 138****8000")


if __name__ == "__main__":
    unittest.main(verbosity=2)
