# -*- coding: utf-8 -*-
"""邮件排版层 `yiban/mail/layout.py` 的专属测试。

发信与推送的测试只验到"正文里有没有某句话"；本文件验的是排版本身的四条纪律：

1. 主动断行按**显示宽度**（全角 2 列）在标点边界切，不把英文词与括号补充劈成两半；
2. 纯文本不依赖等宽列对齐（手机纯文本查看器多用比例字体），结构只靠缩进与项目符；
3. HTML 出口对**用户可控字段**一律转义——正文里会出现拒绝理由、用户名、公告文本；
4. 推送出口是短通道变体：裁剪条目与字段值，但**如实标注被裁掉多少**，不静默丢。

全程无网络、无 SMTP。
"""
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from yiban.mail import layout  # noqa: E402


class DisplayWidthTest(unittest.TestCase):
    """显示宽度：全角计 2、半角计 1、组合符号与控制字符计 0。"""

    def test_cjk_counts_two_latin_one(self):
        self.assertEqual(layout._dwidth("易班自动签到"), 12)
        self.assertEqual(layout._dwidth("yiban"), 5)
        self.assertEqual(layout._dwidth("签到ok"), 6)

    def test_combining_and_control_count_zero(self):
        self.assertEqual(layout._dwidth("a\u0301"), 1)   # a + 组合锐音符
        self.assertEqual(layout._dwidth("a\tb"), 2)


class FoldTest(unittest.TestCase):
    """主动断行：切在标点/空格之后，不切在意群中间。"""

    def test_breaks_after_chinese_comma_not_mid_word(self):
        text = "连续失败会被系统自动暂停账号；如账号本身正常，多为易班服务端临时不可用"
        out = layout._fold(text, width=30)
        self.assertLessEqual(max(layout._dwidth(ln) for ln in out), 30)
        for ln in out:
            self.assertNotIn("账号本", ln.replace("账号本身", "□"), "不得在词中间断开")

    def test_ascii_word_never_split_when_breakable_exists(self):
        text = "Server酱 推送额度已用尽 Server酱 推送额度已用尽"
        for ln in layout._fold(text, width=24):
            self.assertNotIn("Server", ln[:2] + ln[-2:], "长英文词应整体搬到下一行")
            self.assertNotIn("erve", ln.replace("Server", ""), "不得把 Server 切开")

    def test_parenthetical_moves_as_one_atom(self):
        """放得下时括号补充整体移动；放不下时只允许在空格/标点处断。"""
        text = "剩余有效窗口 320s（至 07:50），仅可容纳 5 个（单账号 45s + 账号间隔 8s）"
        wide = layout._fold(text, width=48)
        self.assertTrue(any("（单账号 45s + 账号间隔 8s）" in ln for ln in wide),
                        f"宽度够时括号补充应整体在一行内：{wide}")
        for ln in layout._fold(text, width=34):
            # 真正的不变量：断点只落在分隔符/空格之后，不切开任何词
            self.assertTrue(
                ln == ln.rstrip() and (not ln or ln[-1] in "，、；。）】》,;)]} ！？!?"
                or layout._dwidth(ln) <= 34),
                f"断点位置异常: {ln!r}")
            self.assertNotIn("账号间", ln.replace("账号间隔", "□"), "不得切开中文词")
            self.assertNotIn("45", ln[-1:], "数字与其单位不得分家")

    def test_unbreakable_long_token_hard_cut_by_width(self):
        long_hash = "3f9a1c2b4d5e6f708192a3b4c5d6e7f8"   # 32 个半角字符
        out = layout._fold(long_hash, width=16)
        self.assertEqual(out, ["3f9a1c2b4d5e6f70", "8192a3b4c5d6e7f8"])
        for ln in out:
            self.assertLessEqual(layout._dwidth(ln), 16)

    def test_explicit_newline_always_forced(self):
        self.assertEqual(layout._fold("a\n\nb", width=40), ["a", "", "b"])

    def test_hanging_indent_on_continuation(self):
        text = "原因：网络超时（第 3 次重试后仍无响应，已放弃本轮，请人工核查）"
        out = layout._fold(text, width=30, first="  1) ", cont="     ")
        self.assertTrue(out[0].startswith("  1) "))
        for ln in out[1:]:
            self.assertTrue(ln.startswith("     "), f"续行应悬挂缩进: {ln!r}")
        self.assertTrue(all(ln == ln.rstrip() for ln in out), "不得留行尾空白")


class PlainStructureTest(unittest.TestCase):
    """纯文本骨架：一项一行、分组编号、页脚走签名界、时间收在最后。"""

    def test_one_field_per_line(self):
        text = layout.Mail(
            summary="批量删除账号。",
            fields=[("操作者", "admin"), ("目标", "138****0001")],
            time="2026-09-18 07:12:03",
        ).to_plain()
        lines = [ln for ln in text.split("\n") if ln]
        self.assertIn("· 操作者：admin", lines)
        self.assertIn("· 目标：138****0001", lines)
        self.assertEqual(lines[-1], "时间：2026-09-18 07:12:03", "时间收在末尾")

    def test_group_entries_numbered_only_when_multiple(self):
        multi = layout.Mail(summary="s", groups=[("易班签到失败", [
            [("账号", "138****0001")], [("账号", "138****0002")]])], time="").to_plain()
        self.assertIn("【易班签到失败】2 条", multi)
        self.assertIn("  1) 账号：138****0001", multi)
        self.assertIn("  2) 账号：138****0002", multi)
        # 编号只贴首行：一条明细不得顶掉两个编号
        self.assertEqual(multi.count("1) "), 1)

        single = layout.Mail(summary="s", groups=[("容量超载", [
            [("当前账号", "12 个")]])], time="").to_plain()
        self.assertNotIn("1)", single, "单条不编号，避免多余噪音")
        self.assertIn("  当前账号：12 个", single)

    def test_legacy_string_entry_still_renders(self):
        """既有的 `"账号: x\\n原因: y"` 字符串条目必须还能渲染（收集器端口保持兼容）。"""
        text = layout.Mail(summary="s", groups=[("易班签到失败", [
            "账号: 138****0001\n原因: 密码错误"])], time="").to_plain()
        self.assertIn("账号: 138****0001", text)
        self.assertIn("原因: 密码错误", text)

    def test_footer_uses_rfc3676_signature_delimiter(self):
        text = layout.Mail(summary="s", time="2026-09-18 07:12:03",
                           footer="本邮件由系统自动发送。").to_plain()
        self.assertLess(text.index("时间："), text.index("-- "),
                        "页脚在时间之后，才会被客户端折叠进签名区")
        self.assertIn("\n-- \n本邮件由系统自动发送。", text)

    def test_no_blank_line_runs_longer_than_one(self):
        text = layout.Mail(summary="s", fields=[("a", "b")], advice=["c"],
                           items=["d"], footer=["e"], time="t").to_plain()
        self.assertNotIn("\n\n\n", text)
        self.assertFalse(text.startswith("\n") or text.endswith("\n"))


class HtmlSafetyTest(unittest.TestCase):
    """HTML 出口：动态值一律转义；结构常量不受影响。"""

    def test_user_controlled_values_escaped(self):
        evil = '</td></tr><script>alert("x")</script>'
        html = layout.Mail(
            summary=evil,
            fields=[("审核理由", evil)],
            items=[evil],
            notes=[evil],
            advice=[evil],
            footer=[evil],
            groups=[(evil, [[("账号", evil)], evil])],
            time=evil,
        ).to_html()
        self.assertNotIn("<script>", html, "脚本标签必须被转义掉")
        self.assertNotIn("</td></tr><script", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn('alert("x")', html, "引号也须转义，不得逃出属性/文本")

    def test_title_and_group_headers_escaped(self):
        html = layout.Mail(summary="s", title="<b>粗</b>",
                           groups=[("<i>x</i>", ["y"])], time="").to_html()
        self.assertIn("&lt;b&gt;粗&lt;/b&gt;", html)
        self.assertIn("&lt;i&gt;x&lt;/i&gt;", html)
        self.assertNotIn("<i>x</i>", html)

    def test_plain_part_carries_values_verbatim(self):
        """纯文本部件不转义也不包标签：`<b>` 在 text/plain 里是字面量、本就无害，
        照 HTML 那样转义反而会让用户读到 `&lt;b&gt;` 这种字面量。"""
        text = layout.Mail(summary="<b>x</b>", fields=[("k", "<i>v</i>")],
                           time="").to_plain()
        self.assertIn("<b>x</b>", text)
        self.assertIn("· k：<i>v</i>", text)
        self.assertNotIn("&lt;", text)

    def test_monospace_applied_to_hashes_and_env_keys(self):
        html = layout.Mail(summary="s", time="", fields=[
            ("链头", "3f9a1c2b4d5e6f708192a3b4c5d6e7f8"),
            ("来源 IP", "10.1.2.3"),
            ("配置键", "YIBAN_MAIL_ENABLE"),
        ]).to_html()
        self.assertEqual(html.count('class="mono"'), 3)
        # 普通英文词不得被套成等宽（曾因 re.I 误判 admin 这类词）
        plain = layout.Mail(summary="s", time="", fields=[("操作者", "admin")]).to_html()
        self.assertNotIn('class="mono"', plain)

    def test_card_scales_down_for_mobile(self):
        """实测缺陷回归：卡片写死 width:600px 会在手机上撑出横向溢出。"""
        html = layout.Mail(summary="s", time="").to_html()
        self.assertIn("width:100%;max-width:600px", html)
        self.assertNotIn("width:600px;max-width:600px", html)

    def test_ios_data_detection_disabled(self):
        """iOS 邮件会把时间戳认成日历事件、把脱敏手机号认成可拨号码。"""
        html = layout.Mail(summary="s", time="").to_html()
        self.assertIn('name="format-detection"', html)
        self.assertIn("telephone=no", html)


class MarkdownPushTest(unittest.TestCase):
    """推送是短通道变体：裁剪但如实标注。"""

    def test_long_values_clipped(self):
        md = layout.Mail(summary="s", fields=[("原因", "x" * 300)], time="").to_markdown()
        self.assertIn("…", md)
        self.assertLess(layout._dwidth(md), 200)

    def test_many_entries_clipped_with_count(self):
        rows = [[("账号", f"138****{i:04d}")] for i in range(9)]
        md = layout.Mail(summary="s", groups=[("易班签到失败", rows)], time="").to_markdown()
        self.assertIn("【易班签到失败】9 条", md, "总数仍要说全")
        self.assertIn("另有 4 条", md, "裁掉多少要如实标注")
        self.assertEqual(md.count("- 账号"), 5, "只渲染前 5 条")

    def test_urgent_summary_bolded(self):
        md = layout.Mail(summary="炸了", level="urgent", time="").to_markdown()
        self.assertTrue(md.startswith("**炸了**"))
        info = layout.Mail(summary="正常", time="").to_markdown()
        self.assertTrue(info.startswith("正常"))


class ChannelEntryPointsTest(unittest.TestCase):
    """`as_body` / `as_text` 是发送层与推送层唯一的接入口。"""

    def test_plain_string_passthrough_unchanged(self):
        """普通字符串必须逐字透传且不出 HTML——既有调用方与测试的行为一字不变。"""
        self.assertEqual(layout.as_body("老式正文\n第二行"), ("老式正文\n第二行", None))
        self.assertEqual(layout.as_text("老式正文"), "老式正文")

    def test_mail_yields_both_parts(self):
        m = layout.Mail(summary="标题句", fields=[("k", "v")], time="t")
        plain, html = layout.as_body(m)
        self.assertEqual(plain, m.to_plain())
        self.assertIn("<!DOCTYPE html>", html)
        self.assertEqual(layout.as_text(m), m.to_markdown())

    def test_default_timestamp_is_beijing_time(self):
        """默认时间戳走 clock.ts()（北京时间），宿主时区为 UTC 时也不能写成 UTC 时刻。"""
        from yiban import clock
        got = layout.Mail(summary="s").time
        self.assertRegex(got, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertLess(abs((clock.now() - __import__("datetime").datetime.strptime(
            got, "%Y-%m-%d %H:%M:%S")).total_seconds()), 60)

    def test_empty_time_suppresses_line(self):
        self.assertNotIn("时间：", layout.Mail(summary="s", time="").to_plain())

    def test_level_falls_back_to_info(self):
        self.assertEqual(layout.Mail(summary="s", level="bogus").level, "info")


if __name__ == "__main__":
    unittest.main(verbosity=2)
