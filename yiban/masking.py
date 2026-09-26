# -*- coding: utf-8 -*-
"""对外输出脱敏：手机号、进入日志与通知的文本、URL。

三类数据一旦进入日志、通知或页面展示就不再受控（日志被转发、通知走 webhook、
页面被截图），故统一在这里收口：

- `sanitize_text`：服务端可控内容（异常消息、上游返回）落日志/通知前转义换行并
  抹掉可能内嵌的凭据字面量——防日志注入与凭据泄露；
- `sanitize_url`：URL 入日志前对 query 与 fragment 里的凭据类参数打码（OAuth code /
  CSRF / session 标识 / 隐式流放进 fragment 的令牌 / 未知高熵令牌）；
- `mask_phone`：11 位手机号 → `138****8000`；
- `mask_phones_in_text`：自由文本里**所有** 11 位手机号 → `138****8000`，供日志输出面
  与展示/导出层共用（同一口径，不另起第二套）。

**日志落盘面也脱敏**：日志文件会被转发、导出、截图，故输出面的 formatter 对最终
消息统一兜底脱敏（`yiban.logging_ext.MaskingFormatter`），不依赖各调用点自觉。

本模块是**按键名/值形态打码**的一层，不是"任何形态都遮得住"的一层：每个规则的
实际覆盖面与绕过面写在各自那行旁边，改口径要连 `tests/test_masking_ssrf_gaps.py`
与 `tests/test_masking_tokens.py` 一起看。

⚠ `mask_email` 不在这里：它与 signin 侧的邮箱脱敏公式不同，合并会改变用户可见输出，
须与前端展示口径一起改。
"""
import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

# URL query 敏感参数名片段（子串、不区分大小写匹配）：OAuth code/token、CSRF/session
# 标识、签名票据类——最终 URL 进诊断日志前值统一打码
_URL_SENSITIVE_KEY_PARTS = (
    "code", "token", "csrf", "session", "ticket", "sign",
    "auth", "key", "secret", "passwd", "password", "verify",
    "phone", "mobile", "tel",
)
# 大陆手机号形态（11 位、1[3-9] 开头）——参数名不敏感时也按值打码：
# 上游把手机号回显在 `u=`/`id=` 这类名字里时，24 位高熵阈值够不到 11 位。
_PHONE_VALUE_RE = re.compile(r"^1[3-9]\d{9}$")  # 只认连续 11 位数字：编码/分段书写都不命中

# 自由文本里的手机号：与 `_PHONE_VALUE_RE` 同字符口径（直接复用其 pattern，不另写
# 一套号码规则），只把首尾锚点换成"两侧不能是数字"——否则会把 12 位订单号之类
# 前 11 位截出来误伤；同时避免把坐标/时间戳里的数字段当号码。
_PHONE_IN_TEXT_RE = re.compile(r"(?<!\d)" + _PHONE_VALUE_RE.pattern.strip("^$") + r"(?!\d)")


# 凭据字面量的键名形态：允许 `refresh_token` / `session_id` / `id_token` /
# `JSESSIONID` / `x-csrf` / `api_key` 这类带前后缀的复合名——`\b` 在 `_`/`-` 处
# 不构成边界，原写法只认独立单词，实测 6 类复合名全部漏网。
# 刻意不含裸 `key`/`sid`：否则 `monkey`/`consider` 这类无关词会被误伤。
#
# 绕过面（如实记录，别把这条规则当成键名识别器）：它匹配的是**未经解码的字面文本**，
# 所以 `sanitize_text` 的输入里键名被百分号编码任意一个字符就绕开——`tok%65n=v`、
# `pass%77ord=v` 原样输出。`refresh%5Ftoken=v` 看着能中，是因为残段恰好仍以明文
# `token` 起头，不是规则认得它。同一条 URL 交给 `sanitize_url` 就不会漏，因为那里的
# 键名经 `parse_qsl` 解码后才参与匹配——两层的差别在解码，不在键名词表。
_CRED_KEY = r"(?:token|secret|passwd|password|pwd|cookie|session|csrf|authorization|api[-_]?key)"
# 值按"配对的同种引号串（含反斜杠转义）或裸值"取：`[^'"]*` 会在口令内含
# 另一种引号时截断，残留首引号之后的明文（repr 对含单引号的口令正好用双引号包裹）。
# 裸值取整段：空格/逗号/分号**不单独终止**——值截半是最典型的"截断残留"失效，
# 尾巴会作为正文继续外泄。终止判据是"下一段看起来是新的 `key=value` 对"
# （下一段在下个分隔符前含 `=`）：
# - 保住 cookie 形态 `k=v; k2=v2` 的逐对遮罩——`;` 后带 `=` 的段视为新对，
#   各自按键名判定，`path=/` 这类非凭据属性留在原地（放宽成"吃到行尾"会连它一起吞）；
# - 兜底层的取舍是宁过遮不漏：值后面的普通散文若不含 `=` 会被一并遮掉，
#   这是刻意的代价，日志可读性让位于泄漏面。
# 如实记录残余绕过面：值本身以"空格+含等号段"续接时（如 `token=a b==`）被判成
# 值结束，尾段 ` b==` 原样留存——它与"后文另一个 key= 对"在文本上不可区分，
# 按 cookie 逐对口径让位给后者。
_BARE_VALUE = r"[^\s,;]+(?:[ ,;]+(?![^\s,;]*=)[^\s,;]+)*"
_QUOTED_OR_BARE = r"(?:\"(?:[^\"\\\\]|\\\\.)*\"|'(?:[^'\\\\]|\\\\.)*'|" + _BARE_VALUE + r")"


def sanitize_text(text):
    """服务端可控内容进入错误消息/日志/通知前转义换行与回车，防止日志与通知注入。"""
    s = str(text).replace("\r", "\\r").replace("\n", "\\n")
    # 异常消息可能含 Account dataclass repr（带明文密码/令牌）：
    # 整体替换 Account(...) 对象（正则配对单引号串，跨过值内的 `)` 与 `(` 不截断），
    # 并兜底替换命名字段。
    s = re.sub(r"Account\((?:[^()']|'[^']*')*\)", "Account(***)", s)
    # authorization 专项必须**先于**下面的通用键规则：它把方案名 "Bearer" 一起
    # 吃掉并归一为 `authorization=***`；值按通用取值（配对引号或整段裸值），
    # 截断留尾与凭据键规则同罪。
    s = re.sub(r"(?i)\bauthorization\b\s*:?\s*(?:bearer\s+)?" + _QUOTED_OR_BARE,
               r"authorization=***", s)
    s = re.sub(rf"(?i)\b(password|phone_code)\s*[:=]\s*{_QUOTED_OR_BARE}", r"\1=***", s)
    # dict/repr 形态（vars()/json.dumps 调试输出）：键自身带引号，故以引号为界
    # 而不是要求 `\b`——空格与引号都是非词字符，那里不存在词边界。
    s = re.sub(rf"(?i)(['\"](?:password|phone_code)['\"]\s*[:=]\s*){_QUOTED_OR_BARE}",
               r"\1***", s)
    # 凭据字面量：意外落入文本的 token/cookie/session 等直接抹值，
    # 键名允许带前后缀（refresh_token / session_id / JSESSIONID / x-csrf / api_key）。
    # 值与 password 同用 `_QUOTED_OR_BARE`：`[^\s,;]+` 在引号/空格/逗号处截半，
    # `refresh_token="abc def"` 会留下 ` def"` 这种明文尾巴——本层是最后兜底，
    # 截半等于没遮。
    s = re.sub(
        rf"(?i)(?<![\w-])([a-z0-9_\-]*{_CRED_KEY}[a-z0-9_\-]*)\s*[:=]\s*{_QUOTED_OR_BARE}",
        r"\1=***",
        s,
    )
    return s


def mask_phone(phone):
    """11 位手机号 → 138****8000；已脱敏（含 `*`）或非 11 位原样返回（**幂等**）。

    幂等是有意的：同一串可能被判据链上多处脱敏（日志页展示层再脱敏一次），
    不幂等会把 `138****8000` 二次打码成 `138*****8000` 之类的畸形串。
    """
    p = str(phone)
    if "*" in p:
        return p
    return p[:3] + "****" + p[7:] if len(p) == 11 else p


def mask_phones_in_text(text):
    """把自由文本里**全部** 11 位手机号替换为 `138****8000`，其余原样（**幂等**）。

    日志输出面（`yiban.logging_ext.MaskingFormatter`）与展示/导出层（`_mask_log_phones`）
    共用本函数：脱敏必须只有一个号码口径，否则两套必然分叉（历史缺陷正是展示层只认
    `[11 位]` 方括号形态、中文逗号分隔的裸号漏过）。已遮形态（含 `*`）不匹配 11 位
    连续数字，故重复调用不再变形。
    """
    return _PHONE_IN_TEXT_RE.sub(lambda m: mask_phone(m.group(0)), str(text))


def sanitize_url(url):
    """URL 入日志前对 query 与 fragment 敏感参数脱敏。

    诊断日志需要的是 scheme/host/path 与"带了哪些参数"，不是参数值：可能携带
    凭据的（OAuth code、CSRF、session 标识等）一律替换为 ***；≥24 位连续
    URL-safe 字符的高熵值无论参数名一律打码，兜底未知令牌参数名（阈值取 24：
    真实 code/token 通常远长于此，避免误伤 client_id 这类恰好 16 位的公开标识）。
    fragment 与 query 同一套判定：跟随重定向时 requests 会把 Location 头的
    fragment 传播进最终 `resp.url`，隐式流把令牌放在 `#access_token=…`，
    只查 query 会整段放行。解析失败返回占位符，绝不抛异常影响主流程。
    """
    raw = str(url)
    try:
        parts = urlsplit(raw)
        # query 分隔符只认 `&`：`;token=…` 会被当成上一个参数的**值**收下来，
        # 随后因不含敏感参数名而原样回显（fragment 同理）。
        pairs = parse_qsl(parts.query, keep_blank_values=True)  # 键名在此解码，故编码键名绕不开本层
        # fragment 里大量形态是不透明路由（`#/route`），空值段会被 parse_qsl 收
        # 成键再回填成 `#/route=` 造成改写；空白值不含凭据，跳过即可（宁遮不漏
        # 不在此处让渡——被跳过的只有**空值**，非空值一律进判定）。
        frag_pairs = parse_qsl(parts.fragment, keep_blank_values=False)
    except ValueError:
        return "<url 解析失败已省略>"
    if not pairs and not frag_pairs:
        return raw

    def _masked(key, value):
        k = key.lower()
        if any(part in k for part in _URL_SENSITIVE_KEY_PARTS):
            return f"{key}=***"
        if len(value) >= 24 and re.fullmatch(r"[A-Za-z0-9_\-]+", value):
            return f"{key}=***"  # 高熵兜底是纯形式判定：值里含 % / + . 的长令牌不命中
        if _PHONE_VALUE_RE.match(value):
            # 按值兜底：保留 mask_phone 同口径的前 3 后 4，仍可区分是哪个号
            return f"{key}={value[:3]}****{value[7:]}"
        return f"{key}={value}"  # 参数名不在片段表 + 值不够"高熵" = 原样回显，这是常态不是异常

    # 注意输出是**解码后**的 query/fragment（`%2F` 变回 `/`、`;` 分隔段的值里带回了
    # 原文），只能拿去写日志；回填成请求会改变实际发出去的内容。
    # 一侧无参数对时保留原样（opaque fragment 不因另一侧的改写而被连带重排）。
    query = "&".join(_masked(k, v) for k, v in pairs) if pairs else parts.query
    fragment = ("&".join(_masked(k, v) for k, v in frag_pairs)
                if frag_pairs else parts.fragment)
    return urlunsplit(parts._replace(query=query, fragment=fragment))

#: URL 里的 userinfo（`scheme://user:pass@host`）——代理串按契约允许带凭据，
#: 而 `sanitize_url` 只管 query/fragment 参数，**不碰 userinfo**，故单独一个口径。
_URL_USERINFO_RE = re.compile(r"(?<=://)[^/?#\s]*@")


def mask_url_userinfo(text):
    """把 URL 里的 `user:pass@` 抹成 `***@`，其余原样——供两处共用：

    1. **必须回显用户输入的场合**（如"代理地址格式不正确: <你填的值>"）：回显能帮用户
       定位问题，但凭据绝不能进错误文案 / DOM / 日志；
    2. **异常消息落日志**（requests 的异常文本会内嵌完整 URL，含代理 userinfo）。

    无 userinfo 时原样返回；无 scheme 的裸写法（`user:pass@host:port`）也一并抹掉。
    """
    raw = str(text)
    out = _URL_USERINFO_RE.sub("***@", raw)
    if "://" not in out:
        out = re.sub(r"^[^@\s/]*@", "***@", out)
    return out
