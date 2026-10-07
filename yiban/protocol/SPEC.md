# 协议库 SPEC v0.1 — 解析器契约

> 本文件是**唯一规格来源**。所有实现与测试以本文件为准；
> 规格本身来自第一手黑盒证据（真实流量旁路记录，2026-10-03，已脱敏）。
> 证据形态快照与用例组随本批迁入主仓 `tests/protocol/`（夹具为合成数据，
> **字段结构与真实响应逐一对应**；个人数据已替换为虚构值）。

---

## 0. 定位与边界

**本包只做两件事：解析（bytes/dict → 结构化数据）与构造（结构化参数 → 请求体/URL）。**

非目标（明确不做）：

- 网络请求、会话/cookie 管理、重试——调用方自己持会话；
- 验证码/风控**求解**——只提供 `looks_like_challenge` 检测；
- 主站独立登录页、App 原生协议（login-n1/yb-sign）——只覆盖**网页 OAuth + nightAttendance 链**；
- 异步 API——全部同步纯函数，无 I/O，无日志，无全局状态。

设计约束：核心**仅依赖标准库**（`dataclasses`/`re`/`json`/`urllib.parse`/`base64`/`math`）；
`crypto.py` 为可选附加件（`pycryptodome`），其余模块不得 import 它。
本包是主仓的**一等模块** `yiban/protocol/`，不再是外部库的逐字节副本。

## 1. 错误分类法（`errors.py`）

```python
class YibanProtocolError(Exception): ...   # 基类
class ParseError(YibanProtocolError): ...  # 输入不符合已知形态（结构性失败）
class SessionExpired(YibanProtocolError): ...  # 信封 code == 999（会话过期语义）
```

原则：**解析不 speculate**。缺字段/类型错且无默认值 → `ParseError`（带字段名与片段摘要，
摘要截断 ≤120 字符）；能容忍的按 §3 容错规则收敛；业务失败（见下）返回数据、不抛异常，
**只有** `999` 例外（抛 `SessionExpired`，因其语义是"会话失效，调用方必须重登"）。

## 2. 公共 API

### 2.1 `page.py` — 授权页解析

```python
@dataclass(frozen=True)
class AuthorizePage:
    page_use: str          # 页面一次性令牌，观测形态 40 位十六进制
    public_key_pem: str    # 登录密码加密公钥（RSA-1024，PEM 文本）

def parse_authorize_page(html: str) -> AuthorizePage
```

- `page_use`：提取 JS 变量 `var page_use = '<token>';`（单引号或双引号都要容）。
  值必须非空且全为字母数字；提取不到 → `ParseError("page_use")`。
  **该值就是后续 usersure 请求的 `ajax_sign` query 参数**（夹具 `usersure_url.txt` 同源可验）。
- `public_key_pem`：授权页内隐藏表单项 `<input ... id="key" ...>` 的 `value` 属性，
  内含**多行 PEM**（`-----BEGIN PUBLIC KEY-----` … `-----END PUBLIC KEY-----`，属性值内含换行）。
  提取后须归一化为标准单行折叠 PEM 文本（保留头尾行，中间 base64 行以 `\n` 连接，
  去掉首尾空白与 CRLF）。注意上游把 type 写成了 `type="test"`——**靠 `id="key"` 定位，
  不得依赖 type**。找不到 → `ParseError("public_key")`。
- 夹具：`authorize_page.html`（真实页面，59KB）。

### 2.2 `location.py` — verify_request 提取

```python
def extract_verify_request(location: str) -> str | None
```

- 输入是 302 响应的 `Location` 头全文，例如
  `https://c.uyiban.com/#/?verify_request=<512位hex>`。
- **令牌可能位于 query 末位（其后没有 `&`）**——正则必须宽容：
  `verify_request=([^&]+)`；取不到返回 `None`（**不抛异常**，由调用方决定语义）。
- 返回原始匹配文本（令牌本身是 hex，无需 URL 解码）。
- 夹具：`iframe_location.txt`。

### 2.3 `envelopes.py` — 响应信封

```python
@dataclass(frozen=True)
class Envelope:
    code: object      # 上游原始 code（int 或 str，见夹具）
    msg: str
    data: object
    raw: object       # 原始解析出的 JSON

def parse_api_envelope(body: str | bytes) -> Envelope
```

- 输入是 `api.uyiban.com` 系列端点的 JSON 响应体
  （形如 `{"code":0,"msg":"","data":...}`）。**上游会把 JSON 用
  `text/html` 的 Content-Type 下发——解析只看内容，不看 Content-Type。**
- JSON 解析失败 → `ParseError("envelope")`。
- `code == 999` → 抛 `SessionExpired(msg)`（观测语义：会话过期）。
- 其余原样返回（`code==0` 的"成功"判定由调用方做，本库不做成功学）。

```python
@dataclass(frozen=True)
class UsersureResult:
    ok: bool          # code == "s200"
    re_url: str | None
    code: str

def parse_usersure_response(body: str | bytes) -> UsersureResult
```

- 输入是 usersure 的响应体（观测形态 `{"code":"s200","reUrl":"https://f.yiban.cn/iapp7463"}`，
  同样可能带 `text/html` 头）。`ok = (code == "s200")`；失败码（如 e001 类）**作为数据返回**。
- 夹具：`usersure_response.json`。

```python
def looks_like_challenge(body: str | bytes) -> bool
```

- **只检测不求解**。输入疑似 WAF/风控挑战页时返回 True。
- 判据（尽力而为，无实拍样本，保持保守）：命中下列任一特征——
  `ydclearance`、`fengkongcloud`（大小写不敏感）；或响应体是 HTML 且包含
  `acw_sc` / `captcha` 类特征（弱信号必须带 HTML 上下文，避免把引用了
  captcha.js 的正常页误判成挑战）。
- **硬约束：对夹具中所有正常页面/JSON（含 59KB 授权页）必须返回 False**——
  误报（把正常页当挑战）不可接受，漏报（检测不出来）可接受。
- 保留响亮失败原则：调用方检测到 True 时应停止并人工介入，本库不提供任何绕过手段。

### 2.4 `identity.py` — 身份解析（`base/c/auth/yiban` 的 `data`）

```python
@dataclass(frozen=True)
class App:
    id: str; service_id: str; name: str
    url: str; auth_code: str

@dataclass(frozen=True)
class Identity:
    university_name: str
    university_id: str
    person_id: str
    person_name: str
    person_type: str      # 观测 "student"
    state: int | None      # 观测 1；缺失/不可收敛 → None（§3.1）
    container: str        # 观测 "StudentDefault"
    apps: list[App]

def parse_identity(data: dict) -> Identity
```

- 字段映射：`UniversityName/UniversityId/PersonId/PersonName/PersonType/State/Container/Apps`。
- **必填**：UniversityName、UniversityId、PersonId、PersonName、PersonType（缺一 → `ParseError`）。
- `State` 按数字收敛（可能以 str 下发，见 §3）；`Container` 缺失 → `"StudentDefault"`。
- `Apps[]` 每项映射 `Id/ServiceId/AppName/AppUrl/AuthCode`，缺 `AuthCode` → `""`；
  `Apps` 键缺失 → 空列表。夹具含 3 个 App，其中之一
  `AppUrl = "https://app.uyiban.com/nightattendance/student/"`、
  `AuthCode = "nightattendance.student.*"`。
- 夹具：`auth_response.json`（取其 `data` 字段）。

### 2.5 `position.py` — 签到点解析（`nightAttendance .../signPosition` 的 `data`）

```python
@dataclass(frozen=True)
class Position:
    id: str
    type: str                 # 观测 "campus"
    title: str
    address: str
    lnglat: tuple[float, float]        # 由 "lng,lat" 拆分
    range_m: int | None                # 精确点半径（米）
    points: list[tuple[float, float]]  # 校区边界多边形顶点
    map_type: int | None
    address_name: str | None
    campus: str | None
    building_id: str | None
    create_time: str | None            # 数字/字符串双态，统一 str() 保真

@dataclass(frozen=True)
class TimeWindow:
    start_time: int            # epoch 秒；实拍恰为当日签到窗开始（如 06:30+08）
    end_time: int              # epoch 秒；实拍恰为当日签到窗结束（如 07:50+08）
    sign_day: int | None
    relat_type: int | None
    relat_time_type: int | None

@dataclass(frozen=True)
class SignPositionConfig:
    state: int | None
    msg: str
    acs_state: str | None      # 观测 "off"（保持原始字符串语义）
    out_state: str | None      # 观测 "on"
    remark: str | None         # 上游对范围外签到的人工说明
    file_url: str | None
    type_: str | None          # 观测 "campus"
    is_need_photo: int | None  # 数值枚举**原样保留**（观测值 2；语义未定，勿臆断为布尔）
    attachment_file_name: str | None
    time_window: TimeWindow | None  # 顶层 Range 实为【签到时间窗】对象（见 §3.7）
    positions: list[Position]  # **可多于一个**（夹具有 2 个候选点）

def parse_sign_position(data: dict) -> SignPositionConfig
```

- `Position` 数组缺失 → `ParseError("Position")`；空数组 → `positions=[]` 合法。
- 每项必填 `Id/Type/Title/Address/LngLat`；`LngLat` 是 `"lng,lat"`（容忍两侧空白），
  解析失败 → `ParseError("LngLat")`；`Points[]` 同构，缺省 → `[]`。
- **数值字段按 §3 收敛**：`Range/MapType` 观测中既有数字也有字符串形态。
- `BuildingId` 观测到字符串 `"None"`——**按字面量处理为 None**（字符串 "None"/"" → None）。
- `IsNeedPhoto`：数值枚举**原样保留**（观测值 2；缺失 → None），不做布尔收敛。
- 夹具：`sign_position.json`。

### 2.6 `forms.py` — 请求体构造

```python
def build_usersure_form(
    phone: str, encrypted_password: str, client_id: str, redirect_uri: str,
    *, display: str = "authorize", scope: str | None = None,
) -> str
```

- 产出 `application/x-www-form-urlencoded` 字符串。观测字段与顺序：
  `oauth_uname`（明文手机号）、`oauth_upwd`（密文，RSA-1024 PKCS1v15 → base64，
  观测 172 字符）、`client_id`、`redirect_uri`、`display`
  （`scope` 仅在显式传入时追加在末尾，观测流程未携带）。
- 编码：`urllib.parse.urlencode`（即 `quote_plus`：空格→`+`，`+/=`→`%2B%2F%3D`）。
- 测试与夹具 `usersure_form.txt` **按字段对逐一比对**（值已合成化）。

```python
def build_sign_in_body(
    *, lnglat: tuple[float, float], address: str, out_state: int = 1,
    reason: str = "", attachment_file_name: str = "",
    code: str = "", phone_model: str = "",
) -> str
```

- 产出 `Code=&PhoneModel=&SignInfo=<json>&OutState=<int>` 形态。
- **`SignInfo` 的 JSON 序列化必须与观测逐字节同构**：
  `json.dumps(obj, separators=(", ", ": "), ensure_ascii=False)`，
  键序固定 `Reason, AttachmentFileName, LngLat, Address`；
  `LngLat` 值为 `f"{lng},{lat}"`，**浮点用 `str(float)` 全精度短表示**
  （合成等精度示例 `118.10389401234568,32.0078387654321`）。
- 整体经 `urlencode`（`quote_plus`）：JSON 的 `{}`/`"`/空格 → `%7B%22…`/`+`。
- 与夹具 `sign_in_body_urlencoded.txt` **整串相等**（该夹具即按此规则从
  `sign_in_body.txt` 编码而来——测试以两者互证）。

### 2.7 `crypto.py` — 密码加密（可选附加件，需 `pip install pycryptodome`）

```python
def encrypt_password(password: str, public_key_pem: str) -> str
```

- RSA **PKCS#1 v1.5** 加密 → base64 字符串。观测形态：RSA-1024、密文 128 字节、
  base64 后 172 字符（**末尾单个 `=`**；128 = 3×42+2）。
- `public_key_pem` 即 `parse_authorize_page` 的产出。
- 无 pycryptodome 时 import 本模块 → `ImportError`（带安装指引文案）。
- 测试：用夹具页面的公钥加密任意密码，断言密文 base64 长度 == 172 且能解回
  （测试内自带 RSA-1024 测试私钥做 round-trip；**不得**把任何真实密文写进仓库）。

## 3. 容错规则（全部来自实拍差异）

1. **数值字段双态**：`Range/MapType/State/CreateTime` 等上游可能发数字
   也可能发字符串（`"Range": 70` 与 `"Range": "110"` 同库并存）。统一收敛：
   `int/float` 直取；`str` 去空白后 `int()/float()`；失败或缺失 → `None`
   （必填字段除外）。
2. **字符串 "None"**：`BuildingId` 观测到字面量 `"None"`——空串/`"None"` → `None`。
3. **JSON 藏在 HTML 头下**：信封解析不看 Content-Type。
4. **query 末位令牌**：`verify_request` 后无 `&`，正则不得要求后随分隔符。
5. **PEM 藏在属性里**：`value="-----BEGIN…\n…"` 属性值含真实换行，提取需 DOTALL。
6. **多候选签到点**：`Position` 是数组，不得只取 [0]。
7. **顶层 `Range` 是对象不是数值**：`{"StartTime","EndTime","SignDay",
   "RelatType","RelatTimeType"}`，epoch 秒签到时间窗（实拍恰为当日
   06:30–07:50+08）——解析为 `TimeWindow`，数值收敛规则**不适用**于它。

## 4. 包结构

```
yiban/protocol/
  __init__.py     # 仅 re-export 公共 API 与 __version__ = "0.1.0"
  errors.py  page.py  location.py  envelopes.py
  identity.py  position.py  forms.py  crypto.py
  LICENSE         # MIT 许可全文（随包分发）
  SPEC.md         # 本文件
tests/protocol/
  conftest.py     # 夹具读取辅助（read_fixture / read_fixture_json）
  fixtures/       # 证据夹具（合成数据，勿改动）
  test_*.py       # 本文件各用例组的回归
```

## 5. 契约回归清单（最低集合）

> 夹具与用例组在 `tests/protocol/`（`conftest.py` 提供 `read_fixture` /
> `read_fixture_json`）；下表逐条对应那里的用例。
> `身份` 与 `usersure` 两组解析**当前没有生产消费者**——它们的用例是库资产保全。

| 用例组 | 断言 |
|---|---|
| 授权页 | `page_use` == `usersure_url.txt` 中的 `ajax_sign`；公钥可被 crypto 附加件导入（RSA-1024）；缺 `page_use` 的残页 → `ParseError` |
| verify_request | 夹具 Location → 512 位 hex；无参数的 URL → `None`；令牌在中间带 `&` 也能取 |
| usersure | `s200` → ok+re_url；`{"code":"e001"}` → ok=False；非 JSON → `ParseError` |
| 信封 | `code:0` → 原样；`code:999` → `SessionExpired`；截断 JSON → `ParseError` |
| 身份 | 夹具 → 逐字段相等；删掉 `PersonName` → `ParseError`；`Apps` 缺失 → 空列表 |
| 签到点 | 夹具 → **2** 个 Position；字符串 `"110"` 收敛为 int；`"None"` → None；points 全为 tuple |
| 表单构造 | `build_usersure_form` 与夹具字段对逐一相等；`build_sign_in_body` 与 urlencoded 夹具**整串相等** |
| 挑战检测 | 全部正常夹具 → False；含 `ydclearance`/`acw_sc` 的合成页 → True |
| 加密 | 附加件 round-trip（测试自带测试用私钥）；无依赖时 import 报错文案可读 |

## 6. 验收口径

- `pytest` 全绿；无网络访问（测试不触网）；
- `grep -r` 确认仓库不含任何真实个人数据（姓名/手机号/真实 ID/真实坐标）；
- 核心模块 `import` 零第三方依赖（`crypto.py` 除外）；
- 公共 API 与本文件签名逐一一致。

---
*SPEC 由第一手证据整理；上游行为变化时应以新的实拍证据修订本文件，而不是靠猜。*
