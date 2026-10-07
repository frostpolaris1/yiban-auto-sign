#!/usr/bin/env python
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
104 第一批·A 的配置名册 CI 门禁：名册自校验 + 默认值字面量清零 + 未登记键不得出现。

三条判据（工单 `yiban-auto-sign-2ula`）：
1. **名册自校验**：类型合法、域有序、缺省值在域内、必填字段齐全（复用加载器的
   `yiban.config_loader.validate_registry`——校验实现只有一份，门禁不自抄第二套）。
2. **已登记键的默认值字面量在代码内清零**：对名册里开了 `default_literal_gate` 的键，
   生产代码不得再出现两种回潮形状——
   （a）键名出现的同一行里又写死缺省值字面量（读取点回退）；
   （b）`default_symbols` 里登记的承载常量被重新赋成缺省值字面量。
3. **未在名册登记的键不得新增读取点**：生产代码里出现的配置键字面量必须在册。
   判据认三种写法——引号字面量 `"YIBAN_X"`、bash 展开 `${YIBAN_X:-…}` / `$YIBAN_X`、
   YAML/compose 键行 `- YIBAN_X=…`。裸标识符（与配置键同名前缀的 Python 模块常量）
   不是配置键引用，不入判据。
   命名以 `_` 结尾者视为**族前缀**（`"YIBAN_MAIL_"` + 动态拼名），不是键。

**为什么用 AST 之外还要文本扫描**：判据 2 要判断"同一行里既有键名又有缺省值字面量"，
AST 里键名是 `ast.Constant`、缺省值是另一处 `ast.Constant`，"同行"这层关系只在文本上
可见。判据 3 要覆盖 bash / compose / YAML 三种载体，AST 只看得了 Python。

**注释口径**：剥真注释（Python 用 `tokenize` 精确剥、sh/YAML/JS 按行剥），文档字符串与
普通字符串字面量**计入**——把键名抄进散文正是"改代码不改散文"的病（与
`scripts/check-shared-facts.sh` 同一立场）。

**已知弱化点（如实登记，别当"已完全覆盖"）**：
- 键名由变量拼出（`_PREFIX + "USER"`）时看不到，故 `YIBAN_MAIL_*` / `YIBAN_NOTIFY_*`
  两族只有族前缀进判据，成员键靠人工维护名册；
- sh/YAML 的行内注释只在 `#` 前有空白时剥，值里含 `#` 的写法可能把后半行当注释；
- 本脚本自身不在扫描面内（判据文本里必然出现键名形状的示例）；
- 判据 2 的第一条（键名与字面量同行）只认缺省值的**规范写法**：浮点缺省 1.0 不认
  裸整数写法 `1`。放它进来会造成假红——同行任何一处独立的 `1`（下标、`k=1`、
  文档串里的"1"）都会被算成回潮。裸整数写法只在第二条（常量名紧跟 `= 字面量`）
  里认，那里两者相邻、判据精确。代价：把读取点回退写成裸整数的形状抓不到，
  但那种写法的取值与规范写法相同（1 == 1.0），不构成静默改值。

**归属**
门禁/运维侧脚本（`scripts/`）。守卫测试：`tests/test_config_registry_gate.py`（含突变
验证：每一条放行断言都配一条同形状的污染断言）。CI 接线：`scripts/dev-verify.sh` 的
`run_ci`。接线若断，`tests/test_config_registry_gate.py::GateCiWiringTest` 变红。

**用法**
    scripts/check-config-registry.py [--root DIR] [--registry FILE] [--scan 路径 ...]
                                     [--min-keys N]
默认 `--root` = 本脚本所在仓库根；`--registry` = `<root>/config/registry.json`；
`--scan` 缺省扫生产树（见 `DEFAULT_SCAN`）。

**退出码**
    0  三条判据全过
    1  有违规（名册不合法、默认值字面量回潮、未登记键）
    2  门禁自身的环境/参数错误：名册缺失或不是 JSON、名册没有任何键、键数不足
       `--min-keys`、扫描面不存在或不贡献任何被扫文件。
       最后几条都是**防废门**：静默的 0 命中会被读成"合规"。
"""
import argparse
import io
import json
import os
import re
import sys
import tokenize

# 门禁要 import 被测实现（`yiban.config_loader` 的校验器）：按路径直跑时 sys.path[0]
# 是 scripts/，仓库根不在路径上——先补引导再 import（`tests/test_deploy_entry_imports.py`
# 对 runtime 目录里导入 yiban 的脚本立的是同一条规矩）。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from yiban import config_loader as CL  # noqa: E402  （引导块必须在 import 之前）

#: 本门不扫自己：判据文本里必然出现键名形状的示例。
SELF = os.path.join("scripts", "check-config-registry.py")
#: 缺省扫描面（生产树；`tests/` 不在内——测试夹具设的是既有键的夹具值，改不了生产行为）。
DEFAULT_SCAN = ("yiban", "web", "scripts", "docker", "deploy", "frontend",
                "run.sh", "run_probe.sh", "docker-compose.yml")
#: 被扫的文件扩展名。
SCAN_EXTS = (".py", ".sh", ".js", ".mjs", ".ts", ".vue", ".yml", ".yaml",
             ".html", ".conf", ".service", ".toml", ".example")
SKIP_DIRS = {"__pycache__", ".venv", ".pytest_cache", ".ruff_cache", ".git",
             "node_modules", "work"}
#: 配置键字面量的三种写法（见模块 docstring 判据 3）。前两种对 Python 用，
#: 第三种（YAML/compose/shell 的 `KEY=值` 键行）只对非 Python 文件用——
#: Python 里 `YIBAN_APP_VERSION = "5.2.3"` 这类**同名模块常量**不是配置键引用。
TOKEN_QUOTED = re.compile(
    r"""["'](YIBAN_[A-Z0-9_]+)["']"""          # 引号字面量
    r"""|\$\{?(YIBAN_[A-Z0-9_]+)""")           # bash 展开 `${YIBAN_X…` / `$YIBAN_X`
TOKEN_KEYLINE = re.compile(r"(?:^|[\s\-\[])(YIBAN_[A-Z0-9_]+)\s*[=:]")
#: 被扫文件里必须出现的最小文件数（防"扫描面塌了 ⇒ 零命中 ⇒ 全绿"）。
DEFAULT_MIN_FILES = 100
#: 非生产顶层目录（不参与配置键判据）。新增一枚目录要么进 `DEFAULT_SCAN`、
#: 要么进本表并写明理由——否则门禁把"新目录里的键"整片漏掉而依旧判绿。
BENIGN_TOP_DIRS = {
    ".git", ".github", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__",
    "build", "docs", "node_modules", "out", "tests", "work",
}


def _die(msg):
    """环境/参数错误：点名问题并退 2。与"有违规"（1）严格分开。"""
    print("check-config-registry: %s" % msg, file=sys.stderr)
    sys.exit(2)


def _read(path):
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as exc:
        _die("读不了这个文件: %s（%s）" % (path, exc))


def _strip_py_comments(text):
    """用 tokenize 精确剥 Python 注释（保留行号：注释位置填空串）。"""
    lines = text.split("\n")
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text          # 解析不了就整份保留：判据宁可更严，不许静默放宽
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            (row, col) = tok.start
            if 0 < row <= len(lines):
                lines[row - 1] = lines[row - 1][:col]
    return "\n".join(lines)


#: 行注释起点（按扩展名分派，见 `_strip_comments`）。
_HASH_COMMENT = re.compile(r"(?:^|\s)#.*$")
_SLASH_COMMENT = re.compile(r"^\s*//.*$")
_BLOCK_COMMENT_LINE = re.compile(r"^\s*(?:\*|/\*)")
#: 用 `#` 作注释的语言（shell / YAML / conf / service / toml）。
_HASH_COMMENT_EXTS = (".sh", ".yml", ".yaml", ".conf", ".service", ".toml")
#: 用 `//` 与 `/* */` 作注释的语言。`.vue` / `.html` **不在内**：它们的 `#id`
#: 选择器不是注释，剥了会把整行判据吞掉（假绿）。
_SLASH_COMMENT_EXTS = (".js", ".mjs", ".ts")


def _strip_comments(text, ext):
    """按**扩展名**剥注释；不属于已知注释语法的扩展名原样返回。

    认错注释语法会把真命中的行整片吞掉，那是假绿。故只剥已知的（shell/YAML 的 `#`、
    JS 的 `//` 与 jsdoc 续行 `*`），其余一律不剥。**不许**用"行首 `*` 或 `#`"当注释
    判据：shell 的 `case` 分支写 `*)`、CSS 的 `#id` 选择器都长这样。
    """
    if ext in _SLASH_COMMENT_EXTS:
        out = []
        for line in text.split("\n"):
            if _BLOCK_COMMENT_LINE.match(line):
                out.append("")
                continue
            out.append(_SLASH_COMMENT.sub("", line))
        return "\n".join(out)
    if ext in _HASH_COMMENT_EXTS:
        return "\n".join(_HASH_COMMENT.sub("", line) for line in text.split("\n"))
    return text


def _guard_scan_surface(root, entries):
    """扫描面残缺守卫：有被扫扩展名的顶层目录必须进扫描面或 BENIGN_TOP_DIRS。

    少了这道守卫，"在仓库里新开一个生产目录"就能把配置键整片搬出判据，而门禁照旧判绿——
    静默的盲区正是本门要消灭的形状。栅栏是刻意的：加目录的人必须顺手说明它是不是生产面。
    """
    scanned = {e.split("/")[0] for e in entries}
    missing = []
    for name in sorted(os.listdir(root)):
        if name in scanned or name in BENIGN_TOP_DIRS:
            continue
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        for _cur, dnames, fnames in os.walk(path):
            dnames[:] = [d for d in dnames if d not in SKIP_DIRS]
            if any(f.endswith(SCAN_EXTS) for f in fnames):
                missing.append(name)
                break
    if missing:
        _die("这些顶层目录有被扫文件但不在扫描面内：%s——加进 --scan/DEFAULT_SCAN，"
             "或加进 BENIGN_TOP_DIRS 并写明它不是生产面" % "、".join(missing))


def _scan_files(root, entries):
    """扫描面下的被扫文件（相对路径，POSIX 分隔）。条目不存在 ⇒ 硬失败（拒判）。"""
    found = []
    for entry in entries:
        target = os.path.join(root, entry)
        if not os.path.exists(target):
            _die("扫描面 '%s' 在 --root=%s 下不存在——拒绝在塌掉的扫描面上判绿"
                 % (entry, root))
        if os.path.isfile(target):
            found.append(entry)
            continue
        for cur, dnames, fnames in os.walk(target):
            dnames[:] = [d for d in dnames if d not in SKIP_DIRS]
            for name in fnames:
                if name.endswith(SCAN_EXTS):
                    rel = os.path.relpath(os.path.join(cur, name), root)
                    found.append(rel.replace(os.sep, "/"))
    return sorted(set(found))


def _lines_of(root, rel):
    """被扫文件的**非注释文本**按行返回（1-based 取值时 +1）。"""
    text = _read(os.path.join(root, rel))
    ext = os.path.splitext(rel)[1].lower()
    text = _strip_py_comments(text) if ext == ".py" else _strip_comments(text, ext)
    return text.split("\n")


def _cached_lines(root, rel, cache):
    """按文件缓存"非注释文本行"：判据逐键遍历文件，不缓存会把每个文件读多次。"""
    if rel not in cache:
        cache[rel] = _lines_of(root, rel)
    return cache[rel]


def _key_tokens(line, *, python):
    """一行里的配置键字面量（以 `_` 结尾的族前缀不算键）。"""
    out = []
    patterns = (TOKEN_QUOTED,) if python else (TOKEN_QUOTED, TOKEN_KEYLINE)
    for pattern in patterns:
        for m in pattern.finditer(line):
            key = next((g for g in m.groups() if g), None)
            if key and not key.endswith("_"):
                out.append(key)
    return out


def _literal_candidates(default, *, wide=False):
    """缺省值的字面量候选写法。

    `wide=False`（默认）：只给**规范写法**。判据 2 的第一条是"键名与字面量同行"，
    这里放 `1.0` 的裸整数变体 `1` 会造成假红——行里任何一处独立的 `1`（下标、`k=1`、
    文档串里的"1"）都会被算成缺省值回潮。
    `wide=True`：再给浮点的裸整数写法。只给第二条判据（常量名紧跟 `= 字面量`）用，
    那里字面量与常量名相邻，判据精确。
    """
    if isinstance(default, bool):
        return []
    if isinstance(default, int):
        return [str(default)]
    if isinstance(default, float):
        cands = [repr(default)]
        if wide and float(default).is_integer():
            cands.append(str(int(default)))
        return cands
    if isinstance(default, str):
        text = json.dumps(default, ensure_ascii=False)
        cands = [text]
        if "'" not in default:
            cands.append("'%s'" % default)   # 单引号写法同样是"把缺省抄进代码"
        return cands
    return []


def _literal_alternation(cands):
    """缺省值字面量的正则片段（数字要词边界，避免 `100` 命中 `10`）。"""
    alts = []
    for cand in cands:
        if re.fullmatch(r"-?\d+(?:\.\d+)?", cand):
            alts.append(r"(?<![\d.])" + re.escape(cand) + r"(?![\d.])")
        else:
            alts.append(re.escape(cand))
    return "(?:" + "|".join(alts) + ")"


def _literal_re(cands):
    return re.compile(_literal_alternation(cands))


def _check_default_literals(root, files, registry, cache):
    """判据 2：已登记键的缺省值字面量在代码内清零。

    返回 `(违规条数, 开了门的键列表)`——清单给调用方打印口径用。
    """
    gated = [(k, v) for k, v in registry.items()
             if isinstance(v, dict) and v.get("default_literal_gate")]
    viol = 0
    for key, spec in sorted(gated):
        cands = _literal_candidates(spec.get("default"))
        if not cands:
            print("门禁自身: %s 开了默认值字面量门，但缺省值 %r 拿不到字面量候选"
                  % (key, spec.get("default")))
            viol += 1
            continue
        lit = _literal_re(cands)
        symbols = spec.get("default_symbols") or []
        # 常量回潮判据：常量名后**紧跟** `= 缺省值字面量`（同行即可，不必行首——
        # 函数签名里的 `bucket_rate=1.0` 就是这个形状）。此处用宽松候选集：
        # 字面量紧跟常量名，`bucket_rate=1` 也算回潮，不会误伤别处的独立 `1`。
        wide = _literal_alternation(_literal_candidates(spec.get("default"), wide=True))
        sym_re = re.compile("|".join(
            r"(?<![\w.])" + re.escape(s) + r"\s*=\s*" + wide
            for s in symbols)) if symbols else None
        for rel in files:
            for lineno, line in enumerate(_cached_lines(root, rel, cache), 1):
                if key in line and lit.search(line):
                    print("默认值字面量: %s 的键名与缺省值 %s 同行出现（%s:%d）——"
                          "缺省只许住在名册里" % (key, " / ".join(cands), rel, lineno))
                    viol += 1
                if sym_re is not None and sym_re.search(line):
                    sym = sym_re.search(line).group(0).split("=")[0].strip()
                    print("默认值常量: %s 的承载常量 %s 被赋成缺省值字面量"
                          "（%s:%d）——改成从名册取名册缺省" % (key, sym, rel, lineno))
                    viol += 1
    return viol, gated


def _check_unregistered(root, files, registry, cache):
    """判据 3：生产代码里的配置键字面量必须在册。返回违规条数。"""
    known = set(CL.registry_keys(registry))
    viol = 0
    for rel in files:
        for lineno, line in enumerate(_cached_lines(root, rel, cache), 1):
            for key in _key_tokens(line, python=rel.endswith(".py")):
                if key not in known:
                    print("未登记: %s 出现在生产代码里（%s:%d）——名册是配置键的"
                          "唯一定义点，先登记再用" % (key, rel, lineno))
                    viol += 1
    return viol


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="scripts/check-config-registry.py",
        description="配置名册门禁：名册自校验 + 缺省值字面量清零 + 未登记键零出现")
    ap.add_argument("--root", default=_REPO_ROOT)
    ap.add_argument("--registry", default=None)
    ap.add_argument("--scan", action="append", default=None,
                    help="扫描面条目（可重复）；缺省扫生产树")
    ap.add_argument("--min-keys", type=int, default=50)
    ap.add_argument("--min-files", type=int, default=DEFAULT_MIN_FILES)
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    registry_path = args.registry or os.path.join(root, CL.REGISTRY_RELATIVE_PATH)
    scans = tuple(args.scan) if args.scan else DEFAULT_SCAN

    if not os.path.isdir(root):
        _die("根目录不存在: %s" % root)
    if not os.path.isfile(registry_path):
        _die("名册不存在: %s（门禁必须自包含，CI runner 上没有仓外名册）" % registry_path)

    # ——读名册：读不了/不是 JSON/没有任何键 = 门禁自身的环境错误（2，防废门）
    try:
        with io.open(registry_path, encoding="utf-8-sig") as f:
            raw = json.load(f)
    except ValueError as exc:
        _die("名册不是合法 JSON: %s（%s）" % (registry_path, exc))
    if not isinstance(raw, dict):
        _die("名册顶层必须是对象: %s" % registry_path)
    keys = [k for k in raw if k != CL.META_KEY]
    if not keys:
        _die("名册没有任何配置键: %s——静默的 0 命中会被读成合规" % registry_path)
    if len(keys) < args.min_keys:
        _die("名册只有 %d 键（< --min-keys %d）——名册被删成空壳即拒判"
             % (len(keys), args.min_keys))

    _guard_scan_surface(root, scans)
    files = _scan_files(root, scans)
    files = [f for f in files if f != SELF]
    if len(files) < args.min_files:
        _die("只扫到 %d 个文件（< --min-files %d）——拒绝零命中即通过"
             % (len(files), args.min_files))

    print("名册: %s（%d 键）" % (registry_path, len(keys)))
    print("扫描面: %s（%d 个文件）" % (" ".join(scans), len(files)))

    viol = 0
    # ——判据 1：名册自校验（复用加载器的校验器，门禁不自抄第二套）
    try:
        CL.validate_registry(raw)
    except CL.RegistryError as exc:
        print("名册不合法: %s" % exc)
        viol += 1

    # ——判据 2、3
    cache = {}
    lit_viol, gated = _check_default_literals(root, files, raw, cache)
    print("默认值字面量门: %d 键（%s）"
          % (len(gated), "、".join(k for k, _ in gated) or "无"))
    viol += lit_viol
    viol += _check_unregistered(root, files, raw, cache)

    if viol:
        print("config-registry 判红: %d 处违规" % viol)
        sys.exit(1)
    print("config-registry ok: %d 键、%d 键盘默认值字面量、扫描 %d 文件，全部合规"
          % (len(keys), len(gated), len(files)))
    sys.exit(0)


if __name__ == "__main__":
    main()
