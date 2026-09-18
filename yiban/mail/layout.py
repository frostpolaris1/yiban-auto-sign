# -*- coding: utf-8 -*-
"""邮件与推送正文的排版层：调用点只声明"有哪些内容"，"长什么样"集中在这里。

发送路径（`transport._send`）与推送路径（`yiban.notify`）本身只收字符串，本模块不判
配置、不碰网络、不改任何调用点的业务分支。一处声明、三条出口：

- `to_plain()`：纯文本，邮件的 fallback 部分与所有 SMTP 失败降级路径共用；
- `to_html()`：图形化客户端（手机邮件 App / QQ 邮箱 / Gmail）的正式呈现；
- `to_markdown()`：Server酱 `desp` 走 Markdown，纯文本的单换行会被并成一段，故单独出口。

两条排版纪律（都源自实测定论，改动前请先读）：

1. **主动断行，不靠被动折行**：邮件正文的行宽由 `_fold` 按显示宽度（全角计 2 列）在
   标点边界主动切断，超宽无断点的原子（长哈希、IP）再硬切。客户端被动折行会切在意群
   中间，读者一眼扫不完。
2. **不做等宽列对齐**：手机上的纯文本查看器多用比例字体，`标签↥↥↥值` 这种列对齐必然
   散架。字段一律 `· 标签：值`，续行只靠缩进表示从属，不依赖字符对齐。
"""
import html as _html
import re
import unicodedata

from yiban import clock

# 正文行宽（显示列，全角算 2）。72 列是保守值：主流客户端按 76~80 列折行，留出缩进
# 与引号后仍在安全区；再宽会让手机端的被动折行重新出现。
_PLAIN_WIDTH = 72

# 断点：在这些字符**之后**可断。刻意不含 ASCII 句点（IP `1.2.3.4`、`45.2s`、
# `YIBAN_SIGN_START` 都会被切碎），也不含冒号（时间戳与 `key: value` 同理）。
_BREAK_AFTER = "，、；。）】》〉,;)]}！？!?"

_LEVEL_LABELS = {"urgent": "紧急", "warn": "重要", "info": "通知"}

_WIDE = frozenset("WF")


def _dwidth(s):
    """字符串显示宽度：东亚宽/全角字符计 2 列，组合符号与控制字符计 0 列。"""
    w = 0
    for ch in str(s):
        cat = unicodedata.category(ch)
        if cat.startswith("M") or cat == "Cc":
            continue
        w += 2 if unicodedata.east_asian_width(ch) in _WIDE else 1
    return w


def _atoms(text):
    """切成最小不可断片段，每片连同其后紧跟的断点字符/空格一起。

    `超载（窗口至 07:50）仅可容纳` → `超载（` / `窗口至 ` / `07:50）` / `仅可容纳`，
    于是括号内的补充说明整体移动，不会被折行劈成两半。
    """
    out, cur = [], ""
    for ch in str(text):
        cur += ch
        if ch in _BREAK_AFTER or ch in " \t":
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out


def _cut_at(text, limit):
    """返回按显示宽度切到 `limit` 列时的字符下标（CJK 字符不可从中间切）。"""
    w, i = 0, 0
    for i, ch in enumerate(text):
        cw = _dwidth(ch)
        if w + cw > limit:
            return i
        w += cw
    return len(text)


def _fold(text, width=_PLAIN_WIDTH, first="", cont=""):
    """把可能含换行的文本折成若干行；显式换行始终生效，`first`/`cont` 提供缩进。

    每个源行单独处理：折出的首段用 `first`、续段一律用 `cont`，因此字段值的悬挂缩进
    不会串到下一个字段去。无断点又超宽的原子（长哈希、无空格长词）按显示宽度硬切。
    """
    limit = max(8, width - max(_dwidth(first), _dwidth(cont)))
    out = []
    for raw in str(text).split("\n"):
        raw = raw.rstrip()
        if not raw:
            out.append("")
            continue
        segs, cur = [], ""
        for atom in _atoms(raw):
            if cur and _dwidth(cur) + _dwidth(atom) > limit:
                segs.append(cur)
                cur = ""
            cur += atom if cur else atom.lstrip()
            while _dwidth(cur) > limit:
                cut = _cut_at(cur, limit)
                segs.append(cur[:cut])
                cur = cur[cut:].lstrip()
        if cur:
            segs.append(cur)
        for idx, seg in enumerate(segs):
            out.append((first if idx == 0 else cont) + seg)
    return [ln.rstrip() for ln in out]


class Mail:
    """一封通知邮件（或一条推送）的结构化内容。

    各区块按此顺序渲染：`title` → `summary` → `fields` → `items` → `notes` →
    `advice` → `groups` → `footer` → `time`。全为可选，缺的区块不产生空行。

    - `summary`：一句话结论，读者只看这一行也知道发生了什么；
    - `fields`：`[(标签, 值)]`，一项一行——同一行塞多个字段是本次要治的首要问题；
    - `items`：并列短条目（目标清单、逐账号明细）；
    - `notes`：成段的背景/成因说明，不做字段化；
    - `advice`：处置建议，渲染成独立的「建议」区块；
    - `groups`：`[(组标题, [条目])]`，供汇总邮件用；条目是 `str`（兼容既有的
      `"账号: x\n原因: y"` 写法）或 `[(标签, 值)]`（结构化）；
    - `footer`：免责与小字（"可在「我的账号」页关闭本提醒"）；
    - `level`：`urgent`/`warn`/`info`，只影响 HTML 的强调色与标记，纯文本不额外喊话；
    - `time`：时间戳；`None` 取当前北京时间，传 `""` 表示本封不放时间行。
    """

    __slots__ = ("advice", "fields", "footer", "groups", "items", "level",
                 "notes", "summary", "time", "title")

    def __init__(self, summary="", title=None, fields=None, items=None, notes=None,
                 advice=None, footer=None, groups=None, level="info", time=None):
        self.summary = summary
        self.title = title
        self.fields = list(fields or [])
        self.items = list(items or [])
        self.notes = list(notes or [])
        self.advice = list(advice or [])
        self.footer = [footer] if isinstance(footer, str) else list(footer or [])
        self.groups = list(groups or [])
        self.level = level if level in _LEVEL_LABELS else "info"
        self.time = clock.ts() if time is None else time

    # ---- 纯文本 ----
    def to_plain(self, width=_PLAIN_WIDTH):
        out = []

        def block(lines):
            if not lines:
                return
            if out and out[-1] != "":
                out.append("")
            out.extend(lines)

        if self.title:
            block(_fold(self.title, width))
        if self.summary:
            block(_fold(self.summary, width))
        if self.fields:
            block(_fields_plain(self.fields, width))
        if self.items:
            block(_items_plain(self.items, width))
        for note in self.notes:
            block(_fold(note, width))
        for gtitle, entries in self.groups:
            n = len(entries)
            head = f"【{gtitle}】" + (f"{n} 条" if n > 1 else "")
            if out and out[-1] != "":
                out.append("")
            out.append(head)
            for idx, entry in enumerate(entries, 1):
                out.append("")
                # 多条时编号：只靠空行分隔，两个账号的"账号/原因"会连成一片读不出边界
                number = idx if n > 1 else None
                if isinstance(entry, str):
                    mark = f"  {idx}) " if number else "  "
                    out.extend(_fold(entry, width, first=mark, cont=" " * _dwidth(mark)))
                else:
                    out.extend(_fields_plain(entry, width, indent="  ", bullet="", number=number))
        if self.advice:
            block(["建议：", *_items_plain(self.advice, width, bullet="·")])
        if self.time:
            block(_fold(f"时间：{self.time}", width, cont="        "))
        if self.footer:
            # RFC 3676 §5 签名界：Gmail / mutt 识别到独占一行的 "-- " 会把其后的内容
            # 折叠起来，页脚这类小字说明正好该受这个待遇，不占首屏。
            if out and out[-1] != "":
                out.append("")
            out.append("-- ")
            for f in self.footer:
                out.extend(_fold(f, width))
        # 去掉块与块之间多余的空行堆叠，首尾不留空行
        return "\n".join(_squeeze(out))

    # ---- 推送（Server酱 desp 走 Markdown）----
    def to_markdown(self):
        """推送正文：同一份内容的"短通道变体"。

        刻意不是 `to_plain()` 的换皮：手机上看不到邮件的分组层次，200 条明细塞进 `desp`
        等于没推。这里按 Zabbix 短信变体的做法裁剪字段与条目数，**超出量如实标注**而非
        静默丢弃——"另有 N 条"是读者判断要不要去开后台的依据。
        """
        parts = []
        if self.title:
            parts.append(f"#### {self.title}")
        if self.summary:
            summary = _clip(self.summary, 120)
            parts.append(f"**{summary}**" if self.level != "info" else summary)
        if self.fields:
            parts.append("\n".join(f"- **{k}**：{_clip(v, 80)}" for k, v in self.fields))
        if self.items:
            parts.append("\n".join(f"- {_clip(i, 80)}" for i in _clip_many(self.items, 8)))
        parts.extend(_clip(n, 300) for n in self.notes)
        for gtitle, entries in self.groups:
            lines = [f"【{gtitle}】" + (f"{len(entries)} 条" if len(entries) > 1 else "")]
            for entry in _clip_many(entries, 5):
                if isinstance(entry, str):
                    lines.append(f"- {_clip(entry.replace(chr(10), '／'), 100)}")
                else:
                    lines.append("- " + "／".join(f"{k} {_clip(v, 40)}" for k, v in entry))
            if len(entries) > 5:
                lines.append(f"…另有 {len(entries) - 5} 条，明细见邮件或后台日志")
            parts.append("\n".join(lines))
        if self.advice:
            parts.append("建议：\n" + "\n".join(f"- {_clip(a, 80)}" for a in _clip_many(self.advice, 4)))
        if self.footer:
            parts.append("\n".join(f"> {f}" for f in self.footer))
        if self.time:
            parts.append(f"`{self.time}`")
        return "\n\n".join(p for p in parts if str(p).strip())

    # ---- HTML ----
    def to_html(self):
        return to_html(self)


def as_body(text):
    """发送层入口：返回 (纯文本, HTML 或 None)。

    普通字符串原样透传（既有调用方与测试的行为一字不变），只有 `Mail` 才生成 HTML 部分。
    """
    if isinstance(text, Mail):
        return text.to_plain(), text.to_html()
    return str(text), None


def as_text(text):
    """推送层入口：`Mail` 取 Markdown 出口，普通字符串原样透传。"""
    return text.to_markdown() if isinstance(text, Mail) else str(text)


def _squeeze(lines):
    """折叠连续空行至多一行，并去掉首尾空行。"""
    out = []
    for ln in lines:
        if ln == "" and (not out or out[-1] == ""):
            continue
        out.append(ln)
    while out and out[-1] == "":
        out.pop()
    return out


def _clip(value, max_width):
    """按显示宽度截断（短通道用）：宽度够则原样返回，否则补省略号。"""
    s = str(value)
    return s if _dwidth(s) <= max_width else s[:_cut_at(s, max_width)].rstrip() + "…"


def _clip_many(items, n):
    return list(items)[:n]


def _fields_plain(fields, width, indent="", bullet="·", number=None):
    """字段区：一项一行，续行悬挂缩进。

    `number` 只贴在**第一行**前（分组条目的 `1)` 编号用），其余字段缩进到编号之后的
    同一列——把编号当每行前缀会渲染成 `1) 账号` / `1) 原因`，读起来像两个账号。
    """
    pad = indent + (bullet + " " if bullet else "")
    first_pad = f"{indent}{number}) " if number is not None else pad
    body_pad = " " * _dwidth(first_pad) if number is not None else pad
    out = []
    for idx, (k, v) in enumerate(fields):
        prefix = first_pad if idx == 0 else body_pad
        cont = " " * _dwidth(prefix)
        out.extend(_fold(f"{k}：{v}", width, first=prefix, cont=cont) or [prefix])
    return out


def _items_plain(items, width, bullet="-"):
    pad = bullet + " "
    cont = " " * _dwidth(pad)
    out = []
    for it in items:
        out.extend(_fold(it, width, first=pad, cont=cont))
    return out


# ---------------------------------------------------------------------------
# HTML 出口
# ---------------------------------------------------------------------------
# 兼容口径逐条对应 caniemail 实测：`<style>` 只有约 78% 支持率（Gmail 部分支持、
# Outlook 桌面版不认），所以**关键样式一律内联到标签上**，下面这块只放"丢了也不影响
# 可读"的增强。三条针对具体客户端的坑值得写在这里：Outlook.com 会把 .ExternalClass
# 里的行高塌成 100%（中文行挤成一团）；Gmail App 用 #MessageViewBody 强改链接色与字号；
# `prefers-color-scheme` 支持率仅约 42%，故暗色只是加成，浅色基线必须自己站得住。
_STYLE = """
body{margin:0;padding:0;width:100%!important;-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%}
a{color:#0969da;text-decoration:none}
.mono{font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",Menlo,monospace}
@media all{.ExternalClass,.ExternalClass p,.ExternalClass span,.ExternalClass font,
  .ExternalClass td,.ExternalClass div{line-height:1.75}}
#MessageViewBody a{color:inherit;text-decoration:none;font-size:inherit}
@media only screen and (max-width:640px){.wrapper{padding:12px 6px!important}.card{padding:16px 14px!important}}
/* 窄屏把"标签 / 值"堆成两行：375px 实测下 nowrap 标签列占掉约三成宽度，值列被挤成
   窄条后断句切在意群中间。必须 !important——标签列的 white-space/padding 是内联属性，
   优先级高于类选择器。不支持 <style> 的客户端退回两列（那类客户端在桌面上，宽度够）。 */
@media only screen and (max-width:480px){
  .lbl{display:block!important;width:100%!important;padding:6px 0 0!important;white-space:normal!important}
  .val{display:block!important;width:100%!important;padding:0 0 6px!important}
}
@media (prefers-color-scheme: dark){
  .card{background:#16191d!important;border-color:#30363d!important}
  .card,.card *{color:#e6edf3!important}
  .muted{color:#9198a1!important}
  .rule{border-color:#30363d!important}
}
"""
# 中英文混排的字体栈必须把 CJK 字体列在拉丁系之后、`sans-serif` 之前；且 Windows 简中
# 环境里 Outlook 按本地化名匹配，只写 "Microsoft YaHei" 而缺 `"微软雅黑"` 会退回宋体。
_FONT = ('-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,'
         '"PingFang SC","Hiragino Sans GB","Microsoft YaHei","微软雅黑",'
         '"Noto Sans CJK SC","Source Han Sans SC","WenQuanYi Micro Hei",sans-serif')
_LEAD = "1.75"          # CJK 满字宽，行高要比拉丁文的 1.3~1.5 更松
_TEXT = "#1f2328"
_MUTED = "#57606a"
_RULE = "#d0d7de"
_BG = "#f6f8fa"
_CARD_BG = "#ffffff"
_ACCENT = {"urgent": "#cf222e", "warn": "#9a6700", "info": "#57606a"}


def _esc(value):
    """所有动态值出站前一律转义：字段里含用户可控内容（拒绝理由、用户名、公告文本）。"""
    return _html.escape(str(value), quote=True)


_MONO_RE = re.compile(
    r"^(\d{1,3}(?:\.\d{1,3}){3}|"       # IPv4
    r"[0-9a-f]{8,}…?|"                   # 十六进制哈希（可带省略号）
    r"[A-Z][A-Z0-9_]{4,})$",             # .env 键名
)


def _val(value):
    """值的 HTML：转义后把哈希/IP/环境变量名这类"要抄下来的串"换成等宽。"""
    s = str(value)
    e = _esc(s)
    return f'<span class="mono">{e}</span>' if _MONO_RE.match(s.strip()) else e


def _rows(fields):
    out = []
    for k, v in fields:
        out.append(
            f'<tr><td class="lbl" style="padding:3px 10px 3px 0;white-space:nowrap;'
            f'vertical-align:top;color:{_MUTED};font-size:13px;line-height:{_LEAD};'
            f'font-family:{_FONT}">{_esc(k)}：</td>'
            f'<td class="val" style="padding:3px 0;vertical-align:top;'
            f'word-break:break-word;font-size:14px;line-height:{_LEAD};'
            f'font-family:{_FONT}">{_val(v)}</td></tr>'
        )
    return "".join(out)


def _bullets(items):
    return "".join(
        f'<li style="margin:2px 0;word-break:break-word;line-height:{_LEAD}">{_val(i)}</li>'
        for i in items
    )


def _rule():
    return f'<div class="rule" style="border-top:1px solid {_RULE};margin:18px 0 10px"></div>'


def to_html(m):
    """结构化内容 → 独立完整的 HTML 文档字符串。

    `line-height` 与 `font-family` 在每个承载文字的 `td` 上重复一遍：Outlook 桌面版
    （Word 渲染引擎）不继承 `<body>` 的字体设置，只写在外层会整封退回宋体/Calibri。
    """
    accent = _ACCENT[m.level]
    body = []
    if m.title:
        body.append(
            f'<div style="font-size:17px;font-weight:600;margin:0 0 10px;line-height:{_LEAD}">'
            f'{_esc(m.title)}</div>')
    if m.summary:
        level_chip = ""
        if m.level != "info":
            level_chip = (
                f'<span style="display:inline-block;padding:1px 8px;border-radius:10px;'
                f'font-size:12px;background:{accent};color:#ffffff;margin-right:8px">'
                f'{_LEVEL_LABELS[m.level]}</span>')
        body.append(
            f'<div style="border-left:3px solid {accent};padding:2px 0 2px 12px;'
            f'margin:0 0 14px;font-size:15px;line-height:{_LEAD}">'
            f'{level_chip}{_val(m.summary)}</div>')
    if m.fields:
        body.append(
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:0 0 14px">{_rows(m.fields)}</table>')
    if m.items:
        body.append(
            f'<ul style="margin:0 0 14px;padding-left:20px;font-size:14px">{_bullets(m.items)}</ul>')
    for note in m.notes:
        body.append(
            f'<p style="margin:0 0 12px;font-size:14px;line-height:{_LEAD};'
            f'word-break:break-word">{_val(note)}</p>')
    for gtitle, entries in m.groups:
        inner = []
        multi = len(entries) > 1
        for entry in entries:
            if isinstance(entry, str):
                blk = (f'<div style="font-size:14px;line-height:{_LEAD};'
                       f'word-break:break-word">{_val(entry)}</div>')
            else:
                blk = ('<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
                       f'style="font-size:14px">{_rows(entry)}</table>')
            # 多条时给每条一道左边框：图形客户端里用边框标边界，比序号省视觉噪音
            wrap = (f'<div style="border-left:2px solid {_RULE};padding-left:10px;margin:0 0 8px">'
                    if multi else '<div style="margin:0 0 8px">')
            inner.append(wrap + blk + "</div>")
        badge = (f'<span style="color:{_MUTED};font-weight:400"> {len(entries)} 条</span>'
                 if multi else "")
        body.append(
            _rule()
            + f'<div style="font-size:14px;font-weight:600;margin:0 0 8px">{_esc(gtitle)}{badge}</div>'
            + "".join(inner))
    if m.advice:
        body.append(
            f'<div style="margin:0 0 4px;font-size:13px;color:{_MUTED}">建议</div>'
            f'<ul style="margin:0 0 14px;padding-left:20px;font-size:14px">{_bullets(m.advice)}</ul>')
    if m.footer:
        body.append(
            _rule()
            + f'<div class="muted" style="font-size:12px;line-height:{_LEAD};color:{_MUTED}">'
            + "<br>".join(_val(f) for f in m.footer) + "</div>")
    if m.time:
        body.append(
            f'<div class="muted" style="font-size:12px;color:{_MUTED};margin-top:6px">'
            f'时间 {_esc(m.time)}</div>')
    # 卡片宽度用 width:100% + max-width:600px（实测 430px 视口下写死 width:600px 会撑出
    # 横向滚动、正文右侧直接读不到）。Outlook 桌面版不认 max-width 会摊满窗口——那只是
    # 偏宽仍可读，比手机上内容被裁轻得多，故取这个方向。
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        # 不关掉的话 iOS 邮件会把时间戳认成日历事件、把脱敏手机号认成可拨号码
        '<meta name="format-detection" content="telephone=no,date=no,address=no,email=no,url=no">'
        f"<style>{_STYLE}</style></head>"
        f'<body style="margin:0;padding:0;background:{_BG};font-family:{_FONT};color:{_TEXT}">'
        f'<table role="presentation" class="wrapper" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;background:{_BG}"><tr><td align="center" '
        f'style="padding:20px 12px"><!--[if mso]&nbsp;<![endif]--></td></tr>'
        '<tr><td align="center">'
        f'<table role="presentation" class="card" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;max-width:600px;background:{_CARD_BG};border:1px solid {_RULE};'
        f'border-radius:8px;text-align:left"><tr><td '
        f'style="padding:22px 24px;font-family:{_FONT};font-size:14px;color:{_TEXT};'
        f'line-height:{_LEAD}">'
        + "".join(body) + "</td></tr></table></td></tr></table></body></html>"
    )
