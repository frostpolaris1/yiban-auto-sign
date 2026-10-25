#!/usr/bin/env python
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
104 第一批·A 的配置名册 CI 门禁：名册自校验 + 默认值字面量清零 + 未登记键不得出现
+ 模板缺省等值。

四条判据（工单 `yiban-auto-sign-2ula`；判据 4 是 S2「配置缺省单源」）：
1. **名册自校验**：类型合法、域有序、缺省值在域内、必填字段齐全（复用加载器的
   `yiban.adapters.config.loader.validate_registry`——校验实现只有一份，门禁不自抄第二套）。
2. **已登记键的默认值字面量在代码内清零**：对名册里开了 `default_literal_gate` 的键，
   生产代码不得再出现两种回潮形状——
   （a）键名出现的同一行里又写死缺省值字面量（读取点回退）；
   （b）`default_symbols` 里登记的承载常量被赋成字面量（不论该值等不等缺省）。
   模板载体（`TEMPLATE_CARRIERS`）**不参与**本条：模板按设计要写出示例值，
   那一条由判据 4 按"等值"判，不按"清零"判。
3. **未在名册登记的键不得新增读取点**：生产代码里出现的配置键字面量必须在册。
   判据认三种写法——引号字面量 `"YIBAN_X"`、bash 展开 `${YIBAN_X:-…}` / `$YIBAN_X`、
   YAML/compose 键行 `- YIBAN_X=…`。裸标识符（与配置键同名前缀的 Python 模块常量）
   不是配置键引用，不入判据。
   命名以 `_` 结尾者视为**族前缀**（`"YIBAN_MAIL_"` + 动态拼名），不是键。
4. **模板缺省等值**：名册是缺省的唯一定义点。`.env.example`、`.env.docker.example`、
   `README.md` 里的 `KEY=值` 是**用户可见的缺省声明**（复制模板即得到该值），故：
   - 每个名册键必须在**处置册**（`config/default-gate-register.json`）里恰好有一种
     **模板侧**处置：开门（`default_literal_gate: true`）或豁免
     （`default_literal_gate_exempt`，带 `class` 与 `reason`）；
   - 开门键在三份模板里的每个取值行都必须等于名册缺省（改任一侧必红）；
   - 开门键必须在模板里至少出现一次（门空转不算覆盖）；
   - 豁免类别只有三种：`no_default`（名册无缺省）、`no_template_value`（模板不含
     该键的取值行）、`example_diverges`（示例值刻意不同于缺省，理由必填）。
   处置册覆盖缺口（缺键/多余键）判 1；结构问题（字段形状、未知类别、缺理由、
   开门键却无缺省）与载体塌陷判 2。

**扫描面**
缺省扫生产树，另含两份 `.env.example` 与 `README.md`（判据 3 也看它们）。
判据 4 按 `TEMPLATE_CARRIERS` **固定读取**这三份载体，不受 `--scan` 影响——
否则把载体从 `--scan` 拿掉就能让判据 4 静默消失。

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
- 判据 4 只认模板里的 `KEY=值` 行。README 表格/散文里写"默认 `500`"这类**没有
  `KEY=` 前缀**的取值不在判据内；它靠评审，不靠本门。
- 判据 4 **不比较** `docker-compose.yml` 的注入值。compose 是部署形态声明，
  不是模板缺省：11 行 `YIBAN_*=` 注入里，4 个开门键是容器路径重映射
  （`YIBAN_ENV_FILE` / `YIBAN_DB_FILE` / `YIBAN_STATE_DIR` / `YIBAN_LOG_FILE`
  = `/data/…`），属容器形态刻意值，不入等值判据。
- 判据 4 只处置**模板侧**。代码侧仍由 registry 的 `default_literal_gate` 声明
  （本批 3 键）。46 键在代码/散文中仍有与键名同行的缺省字面量（判据 2a 形状；
  口径=本门文本扫描的同行命中数，非全部运行时读取点）；S2 不动代码取值，
  这些键的代码侧收口归后续批次，清单见 bd 单 `yiban-auto-sign-7yf`。

**归属**
门禁/运维侧脚本（`scripts/`）。处置册：`config/default-gate-register.json`
（每个名册键恰好一种处置）。守卫测试：`tests/test_config_registry_gate.py`（含突变
验证：每一条放行断言都配一条同形状的污染断言）。CI 接线：`scripts/dev-verify.sh` 的
`run_ci`。接线若断，`tests/test_config_registry_gate.py::GateCiWiringTest` 变红。

**用法**
    scripts/check-config-registry.py [--root DIR] [--registry FILE] [--register FILE]
                                     [--scan 路径 ...] [--min-keys N]
                                     [--min-files N] [--min-template-gated N]
默认 `--root` = 本脚本所在仓库根；`--registry` = `<root>/config/registry.json`；
`--register` = `<root>/config/default-gate-register.json`；
`--scan` 缺省扫生产树（见 `DEFAULT_SCAN`）。

**退出码**
    0  四条判据全过
    1  有违规（名册不合法、默认值字面量回潮、未登记键、处置册缺键/多余键、
       模板缺省漂移、开门键在模板里缺位）
    2  门禁自身的环境/参数错误：名册或处置册缺失/不是 JSON、名册没有任何键、
       键数不足 `--min-keys`、处置册字段形状不合法、开门键数不足
       `--min-template-gated`、模板载体缺失或没有任何取值行、扫描面不存在或
       不贡献任何被扫文件。
       最后几条都是**防废门**：静默的 0 命中会被读成"合规"。
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tokenize

# 门禁要 import 被测实现（`yiban.adapters.config.loader` 的校验器）：按路径直跑时 sys.path[0]
# 是 scripts/，仓库根不在路径上——先补引导再 import（`tests/test_deploy_entry_imports.py`
# 对 runtime 目录里导入 yiban 的脚本立的是同一条规矩）。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from yiban.adapters.config import loader as CL  # noqa: E402  （引导块必须在 import 之前）

#: 本门不扫自己：判据文本里必然出现键名形状的示例。
SELF = os.path.join("scripts", "check-config-registry.py")
#: 缺省扫描面（生产树；`tests/` 不在内——测试夹具设的是既有键的夹具值，改不了生产行为）。
#: 两份 `.env.example` 与 `README.md` 是模板载体：判据 3 也看它们（键必须在册）。
DEFAULT_SCAN = ("yiban", "web", "scripts", "docker", "deploy", "frontend",
                "run.sh", "run_probe.sh", "docker-compose.yml",
                ".env.example", ".env.docker.example", "README.md")
#: 模板载体（判据 4 的固定读取面）：用户复制即得到这些值，故它们是缺省声明。
TEMPLATE_CARRIERS = (".env.example", ".env.docker.example", "README.md")
#: 逐键处置册的相对路径。每个名册键必须恰好一条：开门或豁免。
REGISTER_RELATIVE = os.path.join("config", "default-gate-register.json")
#: 豁免类别词表（判据 4）。新增类别必须同批改本表与测试。
EXEMPT_CLASSES = ("no_default", "no_template_value", "example_diverges")
#: 开门键数下限：防把处置册整体改成豁免来废掉判据 4。
#: 实测 65 键（ref=origin/develop 56482b75 的模板取值行与名册缺省等值者）。
DEFAULT_MIN_TEMPLATE_GATED = 65
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
#: 额外豁免的顶层目录。扫描面残缺守卫先按 **git 跟踪集**排除无被跟踪文件的目录
#: （见 `_guard_scan_surface`），故被忽略/未入库的本地工具目录不必在此登记。
#: 本表只收**被跟踪但不属生产面**的目录（如 docs/、tests/）——否则门禁会把它们的
#: 键按生产面判红。新增被跟踪的顶层目录要么进 `DEFAULT_SCAN`、要么进本表并写明理由。
BENIGN_TOP_DIRS = {
    ".git", ".github", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__",
    "build", "docs", "node_modules", "out", "tests", "work",
    # 开发期构建与代码生成（target-tree §3 职责表）：不进镜像、不被运行期 import，
    # 写的是演示环境变量，不是生产配置面。B1（2026-10-25）从 scripts/ 移入。
    "tools",
}


def _git_tracked_top_dirs(root):
    """这个检出里含被跟踪文件的顶层目录集合（= 生产面）。

    生产目录**必被跟踪入库**（部署只带跟踪文件），故"含被跟踪文件"是生产面的
    准确判据。被 git 忽略的本地工具目录（.beads / .qoder / .tmp / .zcode 一类）
    一个文件都没入库 ⇒ 不是生产面，不该触发扫描面残缺守卫。

    为什么不用 `git check-ignore`：忽略规则可以只写在 `.git/info/exclude`（`.beads`
    即此形），而 `scripts/dev-verify.sh` 的副本用**源索引**重建 `.git`，副本的
    info/exclude 是空的——按忽略规则判会在副本里把 `.beads` 误判成生产面。跟踪集
    来自索引本身，对任何检出形态（工作树 / dev-verify 副本 / 直跑）都一致。

    查询**整个索引**而不限定路径：限定到候选目录时，"stdout 为空"既可能是空索引，
    也可能是"这几个候选恰好都没被跟踪"（健康仓里大量存在）。两者必须分开判——前者
    无法判定生产面，后者是真的都不是生产面。

    返回 set（被跟踪的顶层目录名）。**返回 None** 表示无法判定生产面——git 缺失 /
    root 不是可用检出 / **索引为空**。调用方此时不得跳过任何目录，退回"每个顶层目录
    都算候选"的旧行为：空索引下"没有任何目录是生产面"会把新生产目录放绿，那是
    fail-open，方向错在危险的一边。

    **已知窗口（刻意代价，别当漏口）**：新开但**尚未 `git add`** 的顶层生产目录在本地
    不判生产面（无被跟踪文件）。它一入库（提交/CI/部署）即红。取舍理由：生产面由
    "入库"定义——未入库件不进提交、不进镜像，放行它不会把键漏出交付面；反之若把
    未入库目录也算生产面，被忽略/未入库的本地工具目录又会把每次全量拉成伪红（i8kk）。
    要覆盖 stage 前的新目录，靠的是提交前审查，不是这道门。

    **解码口径**：`git ls-files` 输出按**原始字节**取（不设 `text=True`），再用
    `os.fsdecode` 解——与 `os.listdir` 取候选目录名用的是同一套（文件系统编码 +
    surrogateescape）。理由有二：①名里含非法 UTF-8 字节时不会抛 `UnicodeDecodeError`
    （曾经该异常冒到顶层，门以 traceback 崩成 exit 1＝"有违规"，方向错）；②候选名与
    跟踪名按同一种解码比较，非 ASCII 名不会因两套编码而错配。
    """
    if shutil.which("git") is None:
        return None
    try:
        r = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                           capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout:
        # 非零 = 不是可用检出；空 stdout = 索引为空。两者都按"无法判定"处置。
        return None
    return {os.fsdecode(name).split("/", 1)[0]
            for name in r.stdout.split(b"\0") if name}


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

    生产面的判据是 **git 跟踪集**（见 `_git_tracked_top_dirs`）：无被跟踪文件的顶层
    目录不参与判据。理由是生产目录必被跟踪入库，而被忽略/未入库的本地工具目录
    （`.beads` / `.qoder` / `.tmp` / `.zcode` 一类）永非生产面——按目录名逐个维护
    BENIGN_TOP_DIRS 会随每台机器的工具变化而漂移，本机每次全量都伪红。无法判定生产面
    （问不到 git / 索引为空）时不跳过任何目录（旧行为），fail-closed。

    **刻意代价**：新开且**尚未 `git add`** 的顶层生产目录在本地不判生产面，入库（提交
    /CI/部署）后即红。理由与边界见 `_git_tracked_top_dirs` 的"已知窗口"一段。
    """
    scanned = {e.split("/")[0] for e in entries}
    candidates = []
    for name in sorted(os.listdir(root)):
        if name in scanned or name in BENIGN_TOP_DIRS:
            continue
        if not os.path.isdir(os.path.join(root, name)):
            continue
        candidates.append(name)
    if not candidates:
        return
    tracked = _git_tracked_top_dirs(root)
    missing = []
    for name in candidates:
        if tracked is not None and name not in tracked:
            continue        # 无被跟踪文件 ⇒ 不是生产面（本地工具目录 / 未入库件）
        path = os.path.join(root, name)
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
        # 常量回潮判据：常量名后**紧跟** `= 字面量`（同行即可，不必行首——
        # 函数签名里的 `bucket_rate=1.0` 就是这个形状）。认**任何**字面量而不止
        # "等于缺省"那一种：承载常量被赋 7（缺省是 5）同样是第二份定义点，
        # 且它不会撞上"改缺省值"这条判据——只认等值会让静默改值从两个方向漏掉。
        sym_re = re.compile("|".join(
            r"(?<![\w.])" + re.escape(s) +
            r"\s*=\s*(?:-?\d+(?:\.\d+)?|True|False|\"[^\"]*\"|'[^']*')"
            for s in symbols)) if symbols else None
        for rel in files:
            for lineno, line in enumerate(_cached_lines(root, rel, cache), 1):
                if key in line and lit.search(line):
                    print("默认值字面量: %s 的键名与缺省值 %s 同行出现（%s:%d）——"
                          "缺省只许住在名册里" % (key, " / ".join(cands), rel, lineno))
                    viol += 1
                if sym_re is not None and sym_re.search(line):
                    sym = sym_re.search(line).group(0).split("=")[0].strip()
                    print("默认值常量: %s 的承载常量 %s 被赋成字面量"
                          "（%s:%d）——改成从名册取缺省" % (key, sym, rel, lineno))
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


def _read_register(path):
    """读处置册。读不了/坏 JSON/顶层不是对象 ⇒ 拒判（2）。

    "读不了"包括缺失文件、路径是目录、权限拒绝：它们都是环境错误（2），
    不是"有违规"（1）。不先 `isfile` 预判，`io.open` 的 OSError 统一收口。
    """
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            raw = json.load(f)
    except OSError as exc:
        _die("处置册读不了: %s（%s）——每个名册键必须逐键处置（开门或豁免+理由）"
             % (path, exc))
    except ValueError as exc:
        _die("处置册不是合法 JSON: %s（%s）" % (path, exc))
    if not isinstance(raw, dict):
        _die("处置册顶层必须是对象: %s" % path)
    return raw


def _validate_register(register, registry, occurrences):
    """判据 4 的前置：处置册必须覆盖每个名册键，每键恰好一种合法处置。

    另外钉住豁免类别与事实一致（三条零成本前提）：
    - `no_default` ⇒ 名册缺省为 None；
    - `no_template_value` ⇒ 三份载体零取值行；
    - `example_diverges` ⇒ 至少一行取值不等于缺省。
    豁免写错类别会让判据静默失去覆盖，故按违规（1）判红。

    返回违规条数（退出码 1）。字段形状/未知类别/缺理由/开门却无缺省是
    **门禁自身的输入错误**（2）——它们让判据无法成立，不是"内容违规"。
    """
    known = set(CL.registry_keys(registry))
    entries = {k: v for k, v in register.items() if k != CL.META_KEY}
    viol = 0
    missing = sorted(known - set(entries))
    stale = sorted(set(entries) - known)
    if missing:
        print("处置册缺键: %s——名册的 %d 键必须逐键处置"
              % ("、".join(missing), len(known)))
        viol += 1
    if stale:
        print("处置册多余键: %s——名册里没有这个键" % "、".join(stale))
        viol += 1
    for key in sorted(set(entries) & known):
        entry = entries[key]
        spec = registry.get(key)
        if not isinstance(spec, dict):
            _die("名册 %s 的条目不是对象，判据 4 无法判" % key)
        if not isinstance(entry, dict):
            _die("处置册 %s 的值必须是对象" % key)
        has_gate = "default_literal_gate" in entry
        has_exempt = "default_literal_gate_exempt" in entry
        if has_gate == has_exempt:
            _die("处置册 %s 必须恰好给一种处置（default_literal_gate 或 "
                 "default_literal_gate_exempt）" % key)
        if has_gate:
            if entry["default_literal_gate"] is not True:
                _die("处置册 %s 的 default_literal_gate 只允许 true" % key)
            if spec.get("default") is None:
                _die("处置册 %s 开了模板缺省门，但名册没有缺省，判据无处可判" % key)
            continue
        exempt = entry["default_literal_gate_exempt"]
        if not isinstance(exempt, dict):
            _die("处置册 %s 的豁免必须是对象（class + reason）" % key)
        klass = exempt.get("class")
        if klass not in EXEMPT_CLASSES:
            _die("处置册 %s 的豁免 class %r 不在 %r 内"
                 % (key, klass, list(EXEMPT_CLASSES)))
        reason = exempt.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            _die("处置册 %s 的豁免缺 reason——豁免必须有理由" % key)
        # ——豁免类别必须与事实一致（写错类别＝静默失去覆盖）
        default = spec.get("default")
        rows = occurrences.get(key, [])
        if klass == "no_default":
            if default is not None:
                print("处置册错配: %s 标了 no_default，但名册缺省是 %r——"
                      "豁免类别必须与名册一致" % (key, default))
                viol += 1
        elif klass == "no_template_value":
            if rows:
                print("处置册错配: %s 标了 no_template_value，但模板有 %d 行取值"
                      "（%s）" % (key, len(rows),
                                  "、".join("%s:%d" % (c, ln) for c, ln, _ in rows)))
                viol += 1
        else:                                   # example_diverges
            allowed = _default_renderings(default)
            if allowed is None:
                print("处置册错配: %s 标了 example_diverges，但名册没有缺省——"
                      "应标 no_default" % key)
                viol += 1
            elif not allowed:
                # 非标量缺省拿不到单行写法：与开门分支同一口径，判据无处可判 ⇒ 2。
                _die("处置册 %s 标了 example_diverges，但缺省 %r 拿不到单行写法——"
                     "判据无处可判" % (key, default))
            elif not any(value not in allowed for _, _, value in rows):
                print("处置册错配: %s 标了 example_diverges，但模板取值全部等于缺省"
                      "——要么改成开门，要么写明真正的差异" % key)
                viol += 1
    return viol


#: 模板里的取值行：`KEY=值`，行首可带注释号（模板默认把示例写成注释态）。
_TEMPLATE_LINE = re.compile(r"^\s*#?\s*(YIBAN_[A-Z0-9_]+)\s*=\s*(.*)$")


def _strip_template_comment(text):
    """去掉模板值后的行内注释：`#` 前有空白、且不在引号内才算注释。"""
    quote = None
    for idx, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (idx == 0 or text[idx - 1].isspace()):
            return text[:idx]
    return text


def _parse_template_line(line):
    """→ `(键, 值)` 或 None。值去引号、去空白；空值保留为空串（空串也是缺省）。"""
    m = _TEMPLATE_LINE.match(line.rstrip("\r\n"))
    if not m:
        return None
    value = _strip_template_comment(m.group(2)).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return m.group(1), value


def _default_renderings(default):
    """缺省值在模板里的合法写法集合。

    `None` = 名册无缺省（无可断言）。非标量（dict/list 等）返回**空集**：
    这样的缺省拿不到单行写法，任何取值都不算等值。调用方据此拒判。
    """
    if default is None:
        return None
    if isinstance(default, bool):
        return {"1", "true"} if default else {"0", "false"}
    if isinstance(default, int):
        return {str(default)}
    if isinstance(default, float):
        out = {repr(default)}
        if float(default).is_integer():
            out.add(str(int(default)))
        return out
    if isinstance(default, str):
        return {default}
    return set()


def _read_template_occurrences(root):
    """读三份模板载体 → `{键: [(载体, 行号, 值)]}`。

    载体缺失/没有任何取值行 ⇒ 拒判（2）：判据 4 看的面塌了，静默放绿比红更危险。
    """
    occurrences = {}
    for carrier in TEMPLATE_CARRIERS:
        path = os.path.join(root, carrier)
        if not os.path.isfile(path):
            _die("模板载体不存在: %s——判据 4 不许在塌掉的面上判绿" % carrier)
        count = 0
        for lineno, line in enumerate(_read(path).split("\n"), 1):
            parsed = _parse_template_line(line)
            if parsed is None:
                continue
            count += 1
            key, value = parsed
            occurrences.setdefault(key, []).append((carrier, lineno, value))
        if count == 0:
            _die("模板载体 %s 没有任何 KEY=值 行——载体塌陷即拒判" % carrier)
    return occurrences


def _check_template_defaults(registry, register, min_gated, occurrences):
    """判据 4：模板载体里的缺省声明必须等于名册缺省。返回违规条数。"""
    gated = sorted(k for k, v in register.items()
                   if isinstance(v, dict) and v.get("default_literal_gate") is True)
    if len(gated) < min_gated:
        _die("处置册只开了 %d 键模板缺省门（< --min-template-gated %d）——"
             "把处置册整体改成豁免不能算合规" % (len(gated), min_gated))
    viol = 0
    for key in gated:
        rows = occurrences.get(key, [])
        if not rows:
            print("模板缺省缺位: %s 开了门但三份模板都没有它的取值行——"
                  "门空转不算覆盖" % key)
            viol += 1
            continue
        default = registry[key].get("default")
        allowed = _default_renderings(default)
        if not allowed:
            _die("处置册 %s 开了模板缺省门，但缺省 %r 拿不到单行写法——判据无处可判"
                 % (key, default))
        for carrier, lineno, value in rows:
            if value not in allowed:
                print("模板缺省漂移: %s 在 %s:%d 写的是 %r，名册缺省是 %r——"
                      "模板与名册必须同值" % (key, carrier, lineno, value, default))
                viol += 1
    return viol


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="scripts/check-config-registry.py",
        description="配置名册门禁：名册自校验 + 缺省值字面量清零 + 未登记键零出现"
                    " + 模板缺省等值")
    ap.add_argument("--root", default=_REPO_ROOT)
    ap.add_argument("--registry", default=None)
    ap.add_argument("--register", default=None,
                    help="逐键处置册；缺省 <root>/config/default-gate-register.json")
    ap.add_argument("--scan", action="append", default=None,
                    help="扫描面条目（可重复）；缺省扫生产树")
    ap.add_argument("--min-keys", type=int, default=50)
    ap.add_argument("--min-files", type=int, default=DEFAULT_MIN_FILES)
    ap.add_argument("--min-template-gated", type=int,
                    default=DEFAULT_MIN_TEMPLATE_GATED,
                    help="模板缺省门的最少开门键数（防处置册被整体改成豁免）")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    registry_path = args.registry or os.path.join(root, CL.REGISTRY_RELATIVE_PATH)
    register_path = args.register or os.path.join(root, REGISTER_RELATIVE)
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

    register = _read_register(register_path)

    _guard_scan_surface(root, scans)
    files = _scan_files(root, scans)
    files = [f for f in files if f != SELF]
    if len(files) < args.min_files:
        _die("只扫到 %d 个文件（< --min-files %d）——拒绝零命中即通过"
             % (len(files), args.min_files))

    print("名册: %s（%d 键）" % (registry_path, len(keys)))
    print("处置册: %s" % register_path)
    print("扫描面: %s（%d 个文件）" % (" ".join(scans), len(files)))

    viol = 0
    # ——判据 1：名册自校验（复用加载器的校验器，门禁不自抄第二套）
    try:
        CL.validate_registry(raw)
    except CL.RegistryError as exc:
        print("名册不合法: %s" % exc)
        viol += 1

    # ——判据 4 的前置：读模板载体（缺失/塌陷即拒判），再核逐键处置
    occurrences = _read_template_occurrences(root)
    viol += _validate_register(register, raw, occurrences)

    # ——判据 2、3（代码面）。模板载体不参与判据 2：模板按设计写示例值，
    # 那由判据 4 按"等值"判，不按"清零"判。
    cache = {}
    code_files = [f for f in files if f not in TEMPLATE_CARRIERS]
    lit_viol, gated = _check_default_literals(root, code_files, raw, cache)
    print("默认值字面量门: %d 键（%s）"
          % (len(gated), "、".join(k for k, _ in gated) or "无"))
    viol += lit_viol

    # ——判据 4：模板缺省等值
    template_viol = _check_template_defaults(raw, register,
                                             args.min_template_gated, occurrences)
    template_gated = sum(1 for v in register.values()
                         if isinstance(v, dict) and v.get("default_literal_gate") is True)
    print("模板缺省字面量门: %d 键" % template_gated)
    viol += template_viol

    viol += _check_unregistered(root, files, raw, cache)

    if viol:
        print("config-registry 判红: %d 处违规" % viol)
        sys.exit(1)
    print("config-registry ok: %d 键、%d 键盘默认值字面量、%d 键模板缺省、"
          "扫描 %d 文件，全部合规"
          % (len(keys), len(gated), template_gated, len(files)))
    sys.exit(0)


if __name__ == "__main__":
    main()
