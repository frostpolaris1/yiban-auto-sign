# -*- coding: utf-8 -*-
"""测试侧取「邮件/推送正文」的统一入口。

`send_user` / `send_admin_alert` / `send_notification` 的正文入参已放宽为
`layout.Mail | str`——只有结构化正文才能同时渲染出纯文本与 HTML 两版。断言"正文里
有没有某句话"的用例不该关心这个区别，故一律经这里取回渲染后的文本再比对。

三个通道口径不同，测试要按被断言的那条通道取：
- `plain`：邮件的 `text/plain` 部分（也是 SMTP 失败降级时的兜底正文）；
- `markdown`：Server酱 / 自定义 webhook 收到的正文（短通道会裁剪字段与条目数）；
- `html`：邮件的 `text/html` 部分（结构化正文才有，普通字符串为 None）。
"""
from yiban.mail import layout


def render_body(payload, channel="plain"):
    """渲染正文。普通字符串原样返回，`layout.Mail` 按 `channel` 取对应出口。"""
    if isinstance(payload, layout.Mail):
        if channel == "plain":
            return payload.to_plain()
        if channel == "markdown":
            return payload.to_markdown()
        if channel == "html":
            return payload.to_html()
        raise ValueError(f"未知通道: {channel}")
    return str(payload)
