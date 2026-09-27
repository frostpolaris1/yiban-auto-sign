# -*- coding: utf-8 -*-
"""MF-49 出口面（三）：对象 repr 与邮件正文两个"formatter 够不着"的文本出口。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`Account.__repr__`（异常消息 / traceback / `%s account` 直出的载体）、
    `yiban.mail.layout.Mail` 构造点的正文遮罩收口（邮件 to_plain/to_html 与推送
    三条出口共享同一构造，故一处收口覆盖三条）。
对应实现：`yiban/engine/accounts.py::Account.__repr__`、
    `yiban/mail/layout.py::Mail.__init__` / `_masked_tree`。
关键断言：脱敏口径复用单一原语——号码 `mask_phone`/`mask_phones_in_text`（与
    `MaskingFormatter` 同规则），邮箱 `_mask_addr`（与告警收件人 MF-51 同口径），
    不新造第二套；已遮文本**幂等**通过（二次遮不变形）。
依赖：纯进程内，无网络/无库；`Account` 直接构造，`Mail` 只走排版层。

假值规范：`13800001234 → 138****1234`；`student99@example.com → stu******@example.com`。
"""
import unittest
from unittest import mock

from yiban.engine import alerts
from yiban.engine.accounts import Account
from yiban.mail.layout import Mail

PHONE_RAW = "13800001234"
PHONE_MASKED = "138****1234"
PHONE_RAW_2 = "13900002345"
PHONE_MASKED_2 = "139****2345"
EMAIL_RAW = "student99@example.com"
EMAIL_MASKED = "stu******@example.com"
PASSWORD = "hunter2-under-key-a"
PHONE_CODE = "device-code-secret"


class AccountReprSurfaceTest(unittest.TestCase):
    """`repr(account)` 是异常消息/traceback/日志 `%s` 的载体：必须遮号+遮邮箱+不带凭据。"""

    def _acc(self):
        return Account(phone=PHONE_RAW, password=PASSWORD, phone_model="Pixel 7",
                       phone_code=PHONE_CODE, name="测试账号", owner=EMAIL_RAW,
                       account_id=42)

    def test_repr_has_no_credentials_or_raw_phone(self):
        r = repr(self._acc())
        self.assertNotIn(PHONE_RAW, r, f"repr 泄裸号: {r}")
        self.assertNotIn(PASSWORD, r, f"repr 泄口令: {r}")
        self.assertNotIn(PHONE_CODE, r, f"repr 泄设备识别码: {r}")
        self.assertNotIn(EMAIL_RAW, r, f"repr 泄归属邮箱: {r}")

    def test_repr_keeps_readable_identity_shape(self):
        """遮罩不得牺牲"定位是哪个账号"：名称/id/型号仍在，号与邮有遮罩形态。"""
        r = repr(self._acc())
        self.assertIn(PHONE_MASKED, r, f"repr 应带遮罩号: {r}")
        self.assertIn(EMAIL_MASKED, r, f"repr 应带遮罩邮箱: {r}")
        self.assertIn("测试账号", r)
        self.assertIn("42", r)

    def test_empty_account_repr_falsy_password(self):
        """空口令账号 repr 不报错，且口令位显示为空串（区别于已配置显示 ***）。"""
        acc = Account(phone=PHONE_RAW, password="")
        r = repr(acc)
        self.assertNotIn(PHONE_RAW, r)
        self.assertIn("phone='138****1234'", r)

    def test_sanitize_text_still_swallow_account_repr(self):
        """既有纵深不破：`sanitize_text` 对 `Account(...)` 形态整体吞成 `Account(***)`。

        新 repr 虽已自带遮罩，但 `sanitize_text` 的整对象吞除是"调用点忘了过 repr"
        时的兜底——这条钉住它对新 repr 仍生效，别在改 repr 时把它带崩。
        """
        from yiban.masking import sanitize_text
        out = sanitize_text(f"登录失败 {self._acc()} 请检查")
        self.assertIn("Account(***)", out)
        self.assertNotIn(PHONE_RAW, out)


class MailBodyMaskingTest(unittest.TestCase):
    """`Mail` 是邮件与推送正文共同的生成点：字符串叶子在构造时统一过号遮罩。"""

    def test_plain_masks_all_string_leaves(self):
        m = Mail(
            summary=f"您的易班账号 {PHONE_RAW} 今日签到失败。",
            fields=[("账号", PHONE_RAW), ("耗时", "2.1s"), ("备注", f"号 {PHONE_RAW_2} 重试")],
            items=[f"目标 {PHONE_RAW_2}"],
            notes=[f"详情见 {PHONE_RAW}"],
            advice=[f"请联系 {PHONE_RAW_2}"],
            footer=f"回拨 {PHONE_RAW}",
            groups=[("易班签到失败",
                     [f"账号: {PHONE_RAW}", [("原因", f"号码 {PHONE_RAW_2} 异常")]])],
            level="warn",
        )
        text = m.to_plain()
        self.assertNotIn(PHONE_RAW, text, f"正文泄裸号: {text}")
        self.assertNotIn(PHONE_RAW_2, text, f"正文泄裸号: {text}")
        self.assertIn(PHONE_MASKED, text)
        self.assertIn(PHONE_MASKED_2, text)

    def test_html_masks_all_string_leaves(self):
        m = Mail(summary=f"账号 {PHONE_RAW} 失败",
                 fields=[("原因", f"登录 {PHONE_RAW} 被风控")], level="urgent")
        html = m.to_html()
        self.assertNotIn(PHONE_RAW, html)
        self.assertIn(PHONE_MASKED, html)

    def test_markdown_masks_all_string_leaves(self):
        """推送（Server酱 desp 走 Markdown）与邮件同一构造 ⇒ 同一收口。"""
        m = Mail(summary=f"账号 {PHONE_RAW} 失败",
                 fields=[("原因", f"登录 {PHONE_RAW} 被风控")], level="urgent")
        md = m.to_markdown()
        self.assertNotIn(PHONE_RAW, md)
        self.assertIn(PHONE_MASKED, md)

    def test_masking_is_idempotent_for_already_masked_text(self):
        """调用点已 `_mask_phone` 过的正文再经构造层不变形（幂等）。"""
        m = Mail(summary=f"账号 {PHONE_MASKED} 失败", fields=[("号", PHONE_MASKED)])
        text = m.to_plain()
        self.assertIn(PHONE_MASKED, text)
        self.assertNotIn("138****1234****", text, "二次遮罩会把形态打歪")

    def test_non_str_leaves_untouched(self):
        """只动字符串叶子：数字/布尔/None 原样（不产生非法渲染）。"""
        m = Mail(summary="容量超载", fields=[("账号数", 12), ("阈值", None), ("紧急", True)])
        text = m.to_plain()
        self.assertIn("12", text)

    def test_email_domain_untouched_by_phone_layer(self):
        """构造层只管号码——邮箱仍归调用点/收件人层（_mask_addr），这里不越位。"""
        m = Mail(summary="验证码已发送", fields=[("邮箱", EMAIL_RAW)])
        text = m.to_plain()
        # 号遮罩不动邮箱：邮箱脱敏是另一层职责（见 mail 收件人 + web _mask_email）
        self.assertIn(EMAIL_RAW, text)


class PushBodySharesConstructionTest(unittest.TestCase):
    """推送（webhook）出口与邮件共用 `Mail` 构造：一条含裸号的告警，两路都只剩遮罩。"""

    def test_notify_admin_entry_push_content_masked(self):
        entry = [("账号", PHONE_RAW), ("原因", f"登录 {PHONE_RAW} 被风控")]
        captured = []

        def _grab_push(title, content, url=None, **kw):
            captured.append(content)

        with mock.patch.object(alerts, "_collect_admin_mail") as collect, \
             mock.patch.object(alerts, "send_notification", _grab_push), \
             mock.patch.object(alerts.notify, "is_configured", return_value=True):
            alerts.notify_admin_entry("易班签到失败", entry, "https://push.example/t",
                                      push=True)
        # A 线收集口与推送口拿到的都是遮罩后的内容
        collect.assert_called_once()
        self.assertEqual(len(captured), 1, "推送口应收到同一份已遮罩构造")
        body = captured[0].to_markdown() + captured[0].to_plain()
        self.assertNotIn(PHONE_RAW, body)
        self.assertIn(PHONE_MASKED, body)


if __name__ == "__main__":
    unittest.main()
