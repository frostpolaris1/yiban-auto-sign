# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
配置名册加载器：把 `config/registry.json` 的声明、`.env`、进程环境合成为强类型配置。

**这是内部件，不是使用者概念。**本模块不入 `yiban` 包的公开面（`yiban/__init__.py`
不导出它），也不给"使用者"直接调用；它有三个内部角色：

1. **名册缺省的唯一定义点**：取默认值只有本模块两个入口——代码里的默认常量用
   `default_required(key)`（缺省缺失即响亮失败），"本键可以没有缺省"的读取用
   `default_of(key)`。代码里不许再写默认值字面量（104 §5.3 硬规则一）。
2. **解析优先级的唯一实现**：进程环境 → `.env` → 名册缺省（F18 / 工单 71z7 裁决：
   进程环境优先、`.env` 补缺、名册缺省排最后）。写反即静默改行为，方向由
   `tests/test_config_loader.py::PriorityChainTest` 钉住。
3. **出声的校验器**：超域键告警但**不改行为**（不夹取、不回退缺省）；未知键告警，
   不静默忽略（104 §5.3 硬规则三）。

**本批（104 第一批·A）不做的事**：不切换任何现有读路径（八套 `.env` 解析收口归后续
批次）；不实现热重载（`reload` 字段仅声明，重载动作靠既有重启语义）；域（`domain`）
只用于校验与告警，消费点仍用代码里既有的边界常量（域消费点收口归后续批次）。

**解析语义（逐条，测试逐条钉）**
- 取值层序：进程环境 → `.env` → 名册 `default`；空串视为"未设"（与 `.env` 写侧
  "空值=删键"、`env_io.resolve_path` 的 `.strip() or default` 同口径）。
- 不可解析的写法（例如整数键写成了字母）：告警后按"未设"处理，继续往下一层。
- 值可解析但超出 `domain`：告警后**原样返回**，不夹取、不回退缺省。
- 名册没有的键：告警（点名键名），不进强类型对象。
- **派生键不许手填**（104 §5.3 硬规则二）：来源里出现派生键即告警并忽略手填值，
  取值一律为 `None`（值只由派生式给）。
- 无 `default` 的键（密钥类）取不到值时返回 `None`，不报错。

**归属**
`yiban.config_loader` 落在包根（`yiban/`），因为它同时被引擎、web 与运维脚本消费，
放进 `yiban/infra/` 会把"配置声明"与"基础设施"混为一类。它只依赖标准库与
`yiban.infra.env_io`（复用既有 `.env` 解析器，不另立第二套解析实现）。

**复用**
`parse_env_file` / `env_path`（`.env` 的行模型与指针口径）、`ENV_TRUTHY_LITERALS` /
`ENV_FALSY_LITERALS`（开关键真值表，不再抄第二份）。谁调用：本批的生产消费点只有
`yiban/engine/token_bucket.py`、`yiban/engine/schedule.py`、`yiban/engine/runner.py`、
`yiban/cli.py`、`web/app.py`，**一律取 `default_required(...)`**——代码里的默认常量
必须响亮失败，不许把缺省缺失静默变成 `None`。`default_of(...)` **不做生产默认常量的
取值入口**：它只给"本键可以没有缺省"的读取口径用（含测试与门禁）。读路径全量切换
归后续批次。
"""
import json
import logging
import os
import re

from yiban.infra import env_io

logger = logging.getLogger("yiban.config_loader")

#: 名册类型词汇表（104 §5.6 的 `rate`/`unlimited` 等推导族类型归"推导机制上线"批次）。
TYPES = ("int", "seconds", "float", "bool", "string", "path", "enum", "hhmm", "json")
#: 网页编辑档位：仅主管理员 / 管理员即可 / 普通用户（与既有口令门档位同一批键）。
TIERS = ("master", "admin", "user")
#: 生效方式声明（§5.4）：热重载 / 重启 / 下一轮。本批只声明，不改重启语义。
RELOADS = ("hot", "restart", "next-round")
#: 取值来源标注（`Config.source` 的返回值）。
SOURCES = ("process_env", "dotenv", "registry_default", "unset")
#: 名册内保留键（不以 YIBAN_ 开头，避免与配置键混淆）。
META_KEY = "_meta"
_META = META_KEY
_KEY_RE = re.compile(r"^YIBAN_[A-Z0-9_]+$")
_HHMM_RE = re.compile(r"^\d{1,2}:\d{1,2}$")
#: 必填字段（缺一即名册残件）。
_REQUIRED = ("group", "doc", "sensitive", "reload", "tier")
#: §5.4：路径类与代理类默认不入网页白名单（改路径等于任意文件写、改代理等于出口劫持）。
_WEB_FORBIDDEN_SUFFIXES = ("_DIR", "_FILE", "_PATH", "_PROXY")
#: 名册文件相对包根的路径（104 §5.3 指定 `config/registry.json`）。
REGISTRY_RELATIVE_PATH = os.path.join("config", "registry.json")
REGISTRY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), REGISTRY_RELATIVE_PATH)


class RegistryError(ValueError):
    """名册不合法（缺字段、类型未知、域无序、缺省越域……）——启动期响亮失败。"""


def registry_path():
    """名册文件的绝对路径（`config/registry.json`）。"""
    return REGISTRY_PATH


def _warn(msg, *args):
    logger.warning(msg, *args)


def _require(cond, msg):
    if not cond:
        raise RegistryError(msg)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_hhmm(value):
    """是不是合法时刻：**与 `yiban.window.parse_hhmm` 同口径**（该函数是 HH:MM 的既有
    唯一实现）。两处口径不一致会让"引擎收、名册不收"这类分叉悄悄长出来。

    接受 `H:MM` 与 `HH:MM`；小时 0~23、分钟 0~59；其余（`99:99`、`abc`、空）不算。
    """
    if _HHMM_RE.match(str(value).strip()) is None:
        return False
    hh, mm = str(value).strip().split(":")
    return 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59


def _check_domain(key, spec, type_name):
    """域的形态检查（形态 + 有序 + 与类型相配）；返回 None（无域）或归一后的域。

    **域的形态必须与类型相配**：只有数值型（`int`/`seconds`/`float`）与 `enum` 能声明域。
    其余类型声明域一律判不合法——数值域配字符串缺省会在"缺省值在域内"的比较处抛
    `TypeError`，而本函数的契约是"不合法即抛 `RegistryError`"。
    """
    domain = spec.get("domain")
    if domain is None:
        return None
    if type_name == "enum":
        # 允许空串作取值：`YIBAN_NOTIFY_TYPE` 的"空=关闭"就是域内合法值。
        _require(isinstance(domain, list) and domain and
                 all(isinstance(x, str) for x in domain),
                 "名册 %s 的域必须是枚举值列表（非空字符串数组）" % key)
        return list(domain)
    _require(type_name in ("int", "seconds", "float"),
             "名册 %s 的类型 %s 不接受域声明（只有数值型与 enum 能声明域）"
             % (key, type_name))
    _require(isinstance(domain, list) and len(domain) == 2 and
             all(_is_number(x) for x in domain),
             "名册 %s 的域必须是 [下界, 上界] 两个数字" % key)
    lo, hi = domain
    _require(lo <= hi, "名册 %s 的域无序（%s > %s）" % (key, lo, hi))
    return [lo, hi]


def _check_default(key, spec, type_name, domain):
    """缺省值的存在性、类型、域内三项检查。"""
    if "derived" in spec:
        _require("default" not in spec and "domain" not in spec and "type" not in spec,
                 "名册 %s 是派生键：不许同时手填类型、缺省值或域" % key)
        return None
    _require("default" in spec, "名册 %s 既非派生键也没有缺省值" % key)
    default = spec["default"]
    if default is None:
        return None           # 显式声明"本键无缺省"（密钥类、路径类）；缺字段才是残件
    if type_name in ("int", "seconds"):
        _require(isinstance(default, int) and not isinstance(default, bool),
                 "名册 %s 的缺省值必须是整数：%r" % (key, default))
    elif type_name == "float":
        _require(_is_number(default), "名册 %s 的缺省值必须是数字：%r" % (key, default))
    elif type_name == "bool":
        _require(isinstance(default, bool), "名册 %s 的缺省值必须是布尔：%r" % (key, default))
    elif type_name in ("string", "path", "enum", "hhmm"):
        _require(isinstance(default, str), "名册 %s 的缺省值必须是字符串：%r" % (key, default))
        if type_name == "hhmm":
            _require(_is_hhmm(default), "名册 %s 的缺省值不是 HH:MM：%r" % (key, default))
    elif type_name == "json":
        # `json` 键的缺省必须是可序列化的 JSON 值（dict/list/标量），不许是任意对象。
        _require(isinstance(default, (dict, list, str, int, float, bool)),
                 "名册 %s 的缺省值不是 JSON 值（只接受对象/数组/标量）：%r" % (key, default))
    if domain is not None:
        if type_name == "enum":
            _require(default in domain,
                     "名册 %s 的缺省值 %r 不在域 %r 内" % (key, default, domain))
        else:
            _require(domain[0] <= default <= domain[1],
                     "名册 %s 的缺省值 %r 不在域 [%s, %s] 内"
                     % (key, default, domain[0], domain[1]))
    return default


def validate_registry(data):
    """校验名册结构：类型合法、域有序、缺省值在域内、必填字段齐全。不合法即抛。

    加载器与 CI 门禁（`scripts/check-config-registry.py`）共用本函数——两处各写一套
    校验必然漂移，而"名册是唯一定义点"正是本设计的立身之本。
    """
    _require(isinstance(data, dict), "名册顶层必须是对象")
    keys = [k for k in data if k != _META]
    _require(keys, "名册没有任何配置键——静默通过即废门")
    if _META in data:
        meta = data[_META]
        _require(isinstance(meta, dict), "名册 %s 必须是对象" % _META)
    for key, spec in data.items():
        if key == _META:
            continue
        _require(_KEY_RE.match(key) is not None,
                 "名册键名不合法（须匹配 ^YIBAN_[A-Z0-9_]+$）：%r" % key)
        _require(isinstance(spec, dict), "名册 %s 的条目必须是对象" % key)
        if "derived" in spec:
            # 派生键：只声明派生式，不许同时声明类型/域/缺省（§5.3 硬规则二）
            _require(isinstance(spec["derived"], str) and spec["derived"].strip(),
                     "名册 %s 的派生式必须是非空字符串" % key)
            _check_default(key, spec, None, None)
            type_name = None
        else:
            type_name = spec.get("type")
            _require(type_name in TYPES,
                     "名册 %s 的类型 %r 不在词汇表 %r 内" % (key, type_name, list(TYPES)))
        for field in _REQUIRED:
            _require(field in spec, "名册 %s 缺字段 %s" % (key, field))
        group = spec["group"]
        _require(isinstance(group, list) and group and
                 all(isinstance(g, str) and g for g in group),
                 "名册 %s 的分组必须是非空字符串数组" % key)
        _require(isinstance(spec["doc"], str) and spec["doc"].strip(),
                 "名册 %s 的 doc 必须是非空字符串" % key)
        _require(isinstance(spec["sensitive"], bool),
                 "名册 %s 的 sensitive 必须是布尔" % key)
        _require(spec["tier"] in TIERS,
                 "名册 %s 的 tier %r 不在 %r 内" % (key, spec["tier"], list(TIERS)))
        _require(spec["reload"] in RELOADS,
                 "名册 %s 的 reload %r 不在 %r 内" % (key, spec["reload"], list(RELOADS)))
        if type_name is not None:
            domain = _check_domain(key, spec, type_name)
            _check_default(key, spec, type_name, domain)
        if "default_literal_gate" in spec:
            _require(spec["default_literal_gate"] is True,
                     "名册 %s 的 default_literal_gate 只允许 true（不设即关门）" % key)
            _require("default" in spec,
                     "名册 %s 开了默认值字面量门却没有缺省值，判据无处可判" % key)
            symbols = spec.get("default_symbols")
            _require(isinstance(symbols, list) and symbols and
                     all(isinstance(s, str) and s for s in symbols),
                     "名册 %s 开了默认值字面量门就必须登记承载缺省的常量名"
                     % key)
        if key.endswith(_WEB_FORBIDDEN_SUFFIXES):
            _require(spec.get("web_editable") is False,
                     "名册 %s 是路径/代理类键，不得入网页白名单（§5.4）" % key)
        _require(isinstance(spec.get("web_editable"), bool),
                 "名册 %s 的 web_editable 必须是布尔" % key)
    return data


def load_registry(path=None):
    """读名册并校验，返回 `{键: 条目}`（含 `_meta`）。读不了/坏 JSON 即抛 `RegistryError`。

    刻意不做进程内缓存：名册是常量件，重复读的代价远小于"改了名册进程内看不见"这类
    假象；调用方需要一致快照时自己取一次传下去（`resolve` / `load` 的 `registry=`）。
    """
    target = path or REGISTRY_PATH
    try:
        with open(target, encoding="utf-8-sig") as f:
            data = json.load(f)
    except OSError as exc:
        raise RegistryError("名册读不了：%s（%s）" % (target, exc)) from exc
    except ValueError as exc:
        raise RegistryError("名册不是合法 JSON：%s（%s）" % (target, exc)) from exc
    return validate_registry(data)


def registry_keys(registry=None):
    """名册里的配置键（排序后元组）。"""
    data = registry if registry is not None else load_registry()
    return tuple(sorted(k for k in data if k != _META))


def spec_of(key, registry=None):
    """单键的名册条目；未登记返回 None。"""
    data = registry if registry is not None else load_registry()
    spec = data.get(key)
    return spec if isinstance(spec, dict) else None


def default_of(key, registry=None):
    """**名册缺省的唯一入口**：返回该键的缺省值（未登记或派生键/无缺省 → None）。

    消费点用本函数取代默认值字面量——代码里再写一份默认值就是第二个定义点。
    代码里的**默认常量**改用 `default_required`：缺省缺失属名册残件，必须响亮失败，
    不能把 None 当默认值传下去。
    """
    spec = spec_of(key, registry=registry)
    if not spec:
        return None
    return spec.get("default")


def default_required(key, registry=None):
    """取**必需品**缺省：名册缺该键、该键是派生键、或缺省为 null 即抛 `RegistryError`。

    给代码里的默认常量用（`常量 = default_required(...)`）：名册缺失或残件时在
    import 阶段响亮失败，而不是把 None 当默认值用下去。
    """
    spec = spec_of(key, registry=registry)
    if spec is None:
        raise RegistryError("名册没有登记 %s，取不到缺省值" % key)
    if "derived" in spec:
        raise RegistryError("名册 %s 是派生键，取不到缺省值" % key)
    if spec.get("default") is None:
        raise RegistryError("名册 %s 没有缺省值（default 为 null）" % key)
    return spec["default"]


def derived_of(key, registry=None):
    """派生键的派生式（非派生键 → None）。手填派生键的值即回到两个定义点。"""
    spec = spec_of(key, registry=registry)
    if not spec:
        return None
    return spec.get("derived")


def domain_of(key, registry=None):
    """该键的域（声明了才返回；`enum` 返回取值列表，数值型返回 `[下界, 上界]`）。"""
    spec = spec_of(key, registry=registry)
    if not spec:
        return None
    return spec.get("domain")


def reload_of(key, registry=None):
    """该键声明的生效方式（`hot` / `restart` / `next-round`）。本批只声明，不实现重载。"""
    spec = spec_of(key, registry=registry)
    if not spec:
        return None
    return spec.get("reload")


def _coerce(key, type_name, raw):
    """按类型换算一个原始文本。返回 `(ok, value, 原因)`；ok=False 表示不可解析。"""
    text = str(raw).strip()
    if type_name in ("int", "seconds"):
        try:
            return True, int(text), ""
        except ValueError:
            return False, None, "不是整数"
    if type_name == "float":
        try:
            return True, float(text), ""
        except ValueError:
            return False, None, "不是数字"
    if type_name == "bool":
        low = text.lower()
        if low in env_io.ENV_TRUTHY_LITERALS:
            return True, True, ""
        if low in env_io.ENV_FALSY_LITERALS:
            return True, False, ""
        return False, None, "不是可辨认的开关写法"
    if type_name == "json":
        try:
            return True, json.loads(text), ""
        except ValueError:
            return False, None, "不是合法 JSON"
    if type_name == "hhmm" and not _is_hhmm(text):
        return False, None, "不是 HH:MM 时刻"
    return True, text, ""


def _in_domain(type_name, domain, value):
    """值是否在域内；没声明域一律算在域内。

    只有 `enum`（取值列表）与数值型（区间）能声明域——域形态与类型的相配由
    `_check_domain` 在名册自校验里把关，故此处不必为其它类型留分支。
    """
    if domain is None:
        return True
    if type_name == "enum":
        return value in domain
    return domain[0] <= value <= domain[1]


def value_sources(env=None, env_file=None):
    """取值层序的**一次快照**（F18）：进程环境 → `.env`。名册缺省由调用方垫底。

    一次 `load` / `resolve` 只解析 `.env` 一次：逐键重解析会让 138 键的加载读 138 遍
    文件，也让"同一次加载里 `.env` 被改了"变成半新半旧的值。快照口径同时钉住
    "已产出的 `Config` 不跟随后续改动"（`reload` 本批只声明）。
    """
    return (("process_env", os.environ if env is None else env),
            ("dotenv", env_io.parse_env_file(
                env_io.env_path() if env_file is None else env_file)))


def _pick(key, spec, sources):
    """三层取值：返回 `(来源, 值)`；来源 `unset` 表示三层都没有。"""
    if "derived" in spec:
        # 派生键不许手填（§5.3 硬规则二）：来源里出现该键即告警，值一律由派生式给。
        for source, values in sources:
            if str(values.get(key) or "").strip():
                _warn("配置 %s 是派生键，不许手填（来源 %s）；本次忽略手填值，"
                      "值只由派生式给出：%s", key, source, spec["derived"])
        return "unset", None
    type_name = spec["type"]
    domain = spec.get("domain")
    for source, values in sources:
        raw = values.get(key)
        if raw is None or not str(raw).strip():
            continue          # 未设或空值：与 `.env` 写侧"空值=删键"同口径
        ok, value, why = _coerce(key, type_name, raw)
        if not ok:
            _warn("配置 %s=%r 无法解析（%s，来源 %s）；本次按未设处理，继续取下一层",
                  key, raw, why, source)
            continue
        if not _in_domain(type_name, domain, value):
            _warn("配置 %s=%r 超出名册声明的域 %s（来源 %s）；照原值返回，不夹取也不回退缺省",
                  key, raw, domain, source)
        return source, value
    default = spec.get("default")
    if default is not None:
        return "registry_default", default
    return "unset", None      # 显式声明无缺省 ⇒ 三层都没有就是"未设"，不炸


def _unknown_keys(registry, sources):
    """来源里出现、名册里没有的 `YIBAN_*` 键（未知键告警不静默）。"""
    known = set(registry_keys(registry))
    seen = []
    for _source, values in sources:
        for key in values:
            if key not in known and key.startswith("YIBAN_") and key not in seen:
                seen.append(key)
    return seen


class Config:
    """加载结果：强类型取值 + 来源标注。**内部件对象**，不是面向使用者的配置门面。

    `values` 是 `{键: 已换算的值}`（未取到值的键也在，值为 None）；`source(key)` 回答
    该键的值来自哪一层（见 `SOURCES`）。
    """

    def __init__(self, values, sources):
        self.values = dict(values)
        self._sources = dict(sources)

    def __getitem__(self, key):
        return self.values.get(key)

    def __contains__(self, key):
        return key in self.values

    def __iter__(self):
        return iter(self.values)

    def keys(self):
        return tuple(self.values)

    def get(self, key, default=None):
        value = self.values.get(key)
        return default if value is None else value

    def source(self, key):
        """取值来源：`process_env` / `dotenv` / `registry_default` / `unset`。"""
        return self._sources.get(key, "unset")

    def as_dict(self):
        return dict(self.values)

    def __repr__(self):
        return "Config(%d 键)" % len(self.values)


def load(keys=None, env=None, env_file=None, registry=None):
    """加载（全部键或指定键）为 `Config`。

    `keys=None` = 名册里的全部键。`env` / `env_file` / `registry` 供测试注入；
    缺省读进程环境、`env_path()` 指向的 `.env`、`config/registry.json`。
    未知键（来源里有、名册里没有）逐键告警——不静默忽略。
    """
    data = registry if registry is not None else load_registry()
    sources = value_sources(env, env_file)
    if keys is None:
        keys = registry_keys(data)
    for key in _unknown_keys(data, sources):
        _warn("配置 %s 未在名册登记：已忽略该键（未知键不静默，请登记后再用）", key)
    values, out_sources = {}, {}
    for key in keys:
        spec = spec_of(key, registry=data)
        if spec is None:
            _warn("配置 %s 未在名册登记：已忽略该键（未知键不静默，请登记后再用）", key)
            continue
        source, value = _pick(key, spec, sources)
        values[key] = value
        out_sources[key] = source
    return Config(values, out_sources)


def resolve(key, env=None, env_file=None, registry=None):
    """单键解析：进程环境 → `.env` → 名册缺省；未登记的键告警并返回 None。"""
    data = registry if registry is not None else load_registry()
    spec = spec_of(key, registry=data)
    if spec is None:
        _warn("配置 %s 未在名册登记：无法取值（未知键不静默，请先登记）", key)
        return None
    _source, value = _pick(key, spec, value_sources(env, env_file))
    return value
