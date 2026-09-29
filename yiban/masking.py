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
  与展示/导出层共用（同一口径，不另起第二套）；
- `mask_email`：邮箱 → `abc***@example.com`（本地部最多 3 字符 + 完整域名）——展示面
  与审计 `actor` 列共用的唯一口径（原 `accounts_data._mask_email` 的实现搬到这里做真源，
  前端 `maskEmail` 与它由对拍测试钉住同口径；口径本身逐字未变）。
  `mask_email_local`：邮箱本地部的安全展示形态（归属展示名用，号形态走 `mask_phone`）。

**日志落盘面也脱敏**：日志文件会被转发、导出、截图，故输出面的 formatter 对最终
消息统一兜底脱敏（`yiban.logging_ext.MaskingFormatter`），不依赖各调用点自觉。

本模块是**按键名/值形态打码**的一层，不是"任何形态都遮得住"的一层：每个规则的
实际覆盖面与绕过面写在各自那行旁边，改口径要连 `tests/test_masking_ssrf_gaps.py`
与 `tests/test_masking_tokens.py` 一起看。

`mask_email`（展示/审计面口径）与邮件收件人侧的 `_mask_addr`（MF-51 定案）是**两张不同
用途的公式**：前者"前缀+完整域名"保住可比对性（列表/actor 列要能认出同一人），后者逐项
打码服务告警正文。刻意不合并——合并会改变其中一面的用户可见输出。
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
# 尾巴会作为正文继续外泄。终止判据是"下一段看起来是新的 `key=value` 对"——
# "像新对"要求 `=` 之后**还跟着非 `=` 字符**（`next=ok`、`path=/`），只按"含 `=`"判会
# 把以等号收尾的段误判成新对：
# - `token=a b==` 的 `b==` 与 `Authorization: Basic ZGVmOg==` 的 base64 尾巴都是
#   "值自身的续段"而非键值对（MF-111 的"含等号段判定"轴）——旧判据在这里放走明文尾；
# - 保住 cookie 形态 `k=v; k2=v2` 的逐对遮罩——`;` 后带 `key=` 的段视为新对，
#   各自按键名判定，`path=/` 这类非凭据属性留在原地（放宽成"吃到行尾"会连它一起吞）；
# - 兜底层的取舍是宁过遮不漏：值后面的普通散文若不含新对形态会被一并遮掉，
#   这是刻意的代价，日志可读性让位于泄漏面。
_BARE_VALUE = r"[^\s,;]+(?:[ ,;]+(?![^\s,;]*=[^=\s,;])[^\s,;]+)*"
_QUOTED_OR_BARE = r"(?:\"(?:[^\"\\\\]|\\\\.)*\"|'(?:[^'\\\\]|\\\\.)*'|" + _BARE_VALUE + r")"
# 引号键形态（dict/JSON repr：`"access_token": "a b"`）：键名带引号时通用键规则接不上
# （`[:=]` 必须紧跟裸键名，中间隔着收尾引号），故单独一条。"不误伤 JSON 正文"判据：
# ①只有**整键**命中凭据名表（含前后缀复合名，与通用键规则同一张表）才处理——
#   `{"note": "token=…"}` 的 `"note"` 不触发键规则，只可能按通用键规则处理其内文；
# ②值优先按**配对同种引号串**取并原样保留引号（`"***"`），合法 JSON 打码后仍是合法
#   JSON，解析/还原能力不丢；引号不配对时退裸值整段吞（宁过遮不漏）；
# ③JSON 原子值（数字/`true`/`false`/`null`）不参与本规则——数字叶子不变量（裸号只以
#   字符串形态进 JSON payload，遮数字破坏类型契约）同样约束本规则的取值；
#   引号不配对的残段走裸值支整段吞，那是残破输入的过遮方向，安全侧。
_QKEYED_CRED = r"[a-z0-9_\-]*(?:" + _CRED_KEY + r"|phone_code)[a-z0-9_\-]*"
# 引号键规则的"值"：配对引号串（group 2 捕获引号种类供替换体回填）| 非 JSON 原子的裸值。
# 反引用编号前提：本串只嵌在"前面恰有一个捕获组（键名前缀）"的模式里使用。
_QKV_VALUE = (r"(?:(['\"])(?:\\.|(?!\2)[^\\])*\2"
              r"|(?!(?:true|false|null)\b|-?[0-9.])" + _BARE_VALUE + r")")


def _mask_quoted_key_value(m):
    """引号键值打码的替换体：配对的字符串值保住引号（JSON 仍可解析），其余整段归 `***`。"""
    quote = m.group(2)
    if quote:
        return m.group(1) + quote + "***" + quote
    return m.group(1) + "***"


def sanitize_text(text):
    """服务端可控内容进入错误消息/日志/通知前转义换行、遮裸号、抹凭据字面量。

    手机号按 `mask_phones_in_text`（与 `MaskingFormatter` 同一份号码口径，MF-49 的
    统一原语）收口在这里：上游异常消息/返回体里回显的裸号（如账号标识）随文本进
    告警与日志，此前只靠调用点自觉——出口面兜底脱敏与展示层必须共用一条规则，
    本函数补上最后一格，而不是在调用点再抄一份。
    """
    s = str(text).replace("\r", "\\r").replace("\n", "\\n")
    # 异常消息可能含 Account dataclass repr（带明文密码/令牌）：
    # 整体替换 Account(...) 对象——引号串按**两种引号各自配对**跨过值内的 `)` 与 `(`
    # 不截断（旧版只配单引号：name 含撇号时 repr 改用双引号包裹，吞除在双引号段前
    # 失效、整段明文外泄——MF-111 登记的两轴之一，本轴随键形态轴一并收口），
    # 并兜底替换命名字段。
    s = re.sub(r'''Account\((?:"[^"]*"|'[^']*'|[^()])*\)''', "Account(***)", s)
    # dict/JSON **引号键形态**（vars()/json.dumps 调试输出）：键自身带引号，通用键规则
    # 的 `[:=]` 接不上，故以引号为界单独一条。覆盖**整张凭据名表**（MF-111 键形态轴：
    # 旧版只认 password/phone_code，`{"access_token": "a b"}` 原样穿过）；
    # "不误伤 JSON 正文"判据见 `_QKV_VALUE` 上方注释。必须排在 authorization 专项**之前**：
    # 那条的冒号可选（`:?`）会先从 `"authorization": "…"` 的键名后半咬进来，把配对的
    # 引号串咬错位、产出撕坏 JSON 的残串。
    s = re.sub(rf"(?i)(['\"]{_QKEYED_CRED}['\"]\s*[:=]\s*){_QKV_VALUE}",
               _mask_quoted_key_value, s)
    # authorization 专项必须**先于**下面的通用键规则：它把方案名 "Bearer" 一起
    # 吃掉并归一为 `authorization=***`；值按通用取值（配对引号或整段裸值），
    # 截断留尾与凭据键规则同罪。冒号可选（`:?`）使它能在引号键（`"authorization":`）
    # 的收尾引号处下口、把配对引号咬错位——引号键形态已由上面的专属规则先行接管，
    # 这里显式让位：键名右侧紧跟引号时不再匹配。
    s = re.sub(r"(?i)\bauthorization\b(?!['\"]\s*[:=])\s*:?\s*(?:bearer\s+)?" + _QUOTED_OR_BARE,
               r"authorization=***", s)
    s = re.sub(rf"(?i)\b(password|phone_code)\s*[:=]\s*{_QUOTED_OR_BARE}", r"\1=***", s)
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
    # 裸号收口（文档字符串所述）：放在凭据规则**之后**——键值对形态先归值，再按
    # 值形态扫剩余号码；`mask_phones_in_text` 幂等，已遮形态不会二次变形。
    return mask_phones_in_text(s)


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


def mask_email(e):
    """邮箱 → `abc***@example.com`（本地部最多 3 字符 + 完整域名）。

    已含 `*`（幂等）或非邮箱（无 `@` / `@` 在首位，如裸用户名 `admin`）原样返回。
    这是**展示面与审计 `actor` 列共用的唯一口径**：`web.services.accounts_data._mask_email`
    只是对它的转发，前端 `core.js` 的 `maskEmail` 是同口径的第二份**实现**（两份由
    `tests/test_web_mask_email_parity.py` 真跑对拍钉死，改一边不改另一边即红）。
    本地部只留前 3 字符：现网实测约一成账号的本地部**就是手机号**（MF-49 出口字段），
    `138***` 不足以定位号码，也刻意不给后 4 位——归属列不是号码检索入口。
    """
    s = str(e)
    if "*" in s:
        return s
    i = s.find("@")
    if i <= 0:
        return s
    return s[: min(3, i)] + "***" + s[i:]


def mask_email_local(e):
    """邮箱**本地部**（`@` 前段）的展示安全形态：号码形态 → `mask_phone`（`138****0000`，
    仍可辨是哪个号），否则前 3 字符 + `***`（与 `mask_email` 的本地部保留量同界）。

    专供归属展示名（`accounts_data._owner_display_of`）：旧实现整段本地部外发
    （现网 9/96 即完整手机号），且前端 `account-form.js` 的 `email.split("@")[0]` 长出
    了同规则的第二份定义。收敛后规则只在此一处，前端一律消费服务端下发的结果字段。
    幂等：已含 `*` 原样返回。
    """
    s = str(e)
    if "*" in s:
        return s
    if not s:
        return s
    if _PHONE_VALUE_RE.match(s):
        return mask_phone(s)
    return s[:3] + "***"


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
