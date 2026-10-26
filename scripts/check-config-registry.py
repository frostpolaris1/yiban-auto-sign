#!/usr/bin/env python
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
104 第一批·A 的配置名册 CI 门禁：名册自校验 + 默认值字面量清零 + 未登记键不得出现
+ 模板缺省等值 + 代码侧字面量逐键处置。

六条判据（工单 `yiban-auto-sign-2ula`；判据 4 是 S2「配置缺省单源」，
判据 5、6 是工单 `yiban-auto-sign-7yf` 的代码侧收口与载体差异登记）：
1. **名册自校验**：类型合法、域有序、缺省值在域内、必填字段齐全（复用加载器的
   `yiban.adapters.config.loader.validate_registry`——校验实现只有一份，门禁不自抄第二套）。
2. **已登记键的默认值字面量在代码内清零**：对名册里开了 `default_literal_gate` 的键，
   生产代码不得再出现四种回潮形状——
   （a）键名出现的同一行里又写死缺省值字面量（读取点回退）；
   （b）`default_symbols` 里登记的承载常量被赋成字面量（不论该值等不等缺省）；
   （c）HH:MM 键的承载时刻形状 `(H, M)` / `((H, M))` / `time(H, M)` 换算后必须
   等于名册缺省（等值断言，漂移即红；改载体形状即失去保护）；
   （d）shell 的 `${KEY:-值}` / `${KEY:=值}`（含嵌套 `${INNER:-X}` 的内层静态值）
   必须等于名册缺省。本臂覆盖**所有有缺省的键**，不只开门键；`$(…)`/`$VAR` 等
   静态判不了的跳过必须登记（见判据 5 的 `_meta.shell_exempt_defaults`）。
   顶层的非缺省操作符 `${KEY:?…}` / `${KEY:+…}` 不写缺省值，不判；同样的操作符
   出现在嵌套内层时取值到运行期才定，按动态值处理并强制登记。
   承载常量必须**显式声明**：无常量写 `default_symbols: []`，缺字段/null 由 loader 拒判。
   读不了名册的跨层常量是例外：`env_io.DEFAULT_STATE_DIR` 进不了 `default_symbols`
   （loader 在模块级导入 `env_io`，反向 import 拿到半初始化模块），由
   `tests/test_config_registry_gate.py::StateDirFallbackEquivalenceTest` 等值钉住
   常量与解析器的形参缺省。
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
5. **代码侧字面量逐键处置**：判据 2 只盖开了门的键。没开门的键若在代码/散文里仍有
   "键名与缺省字面量同行"的命中，必须在**代码侧处置册**
   （`config/code-literal-register.json`）里逐键登记豁免：`class`（一个类别或类别
   列表）∈ `CODE_EXEMPT_CLASSES`、非空 `reason`、以及 `files`（**每个文件的命中行数**）。
   类别与载体形状必须一致：类别列表里只要有机械类别（shell/container_form/harness/
   prose），每个命中文件必须命中至少一个；`prose` 在场即核命中行是否落在 docstring；
   `container_form` 只认 compose 与 `docker/` 下的非 `.py` 部署件；非机械类别不投票。
   另：shell 臂的跳过登记在 `_meta.shell_exempt_defaults`（file + key + reason），
   未登记即红、登记陈旧也红。
   台账按文件行数比对：新增一处裸缺省（行数变多）、删掉一处（行数变少＝陈旧豁免）、
   未登记命中，三者都判 1。开门键不许同时登记代码侧豁免（判 2）——开门要的是清零。
   这一条的判据形状与判据 2 相同（同一套文本扫描），差别只在"命中后怎么处置"。
6. **载体示例值差异逐键登记**：名册缺省为 null 的键，三份模板里的 `KEY=值` 只能是
   示例值。多份载体的示例值**刻意不一致**（宿主/容器形态、演示内容、占位文案）时，
   必须在处置册里给该键加 `carrier_example`（非空 `reason`）。取值不一致却没登记、
   或登记了却已改成一致（陈旧），都判 1。有缺省的键不许用 `carrier_example`
   （模板必须等于缺省，没有"刻意差"这条出路），判 2。

**扫描面**
缺省扫生产树，另含两份 `.env.example` 与 `README.md`（判据 3 也看它们）。
判据 4/6 按 `TEMPLATE_CARRIERS` **固定读取**这三份载体，不受 `--scan` 影响——
否则把载体从 `--scan` 拿掉就能让判据 4 静默消失。

**为什么用 AST 之外还要文本扫描**：判据 2/5 要判断"同一行里既有键名又有缺省值字面量"，
AST 里键名是 `ast.Constant`、缺省值是另一处 `ast.Constant`，"同行"这层关系只在文本上
可见。判据 3 要覆盖 bash / compose / YAML 三种载体，AST 只看得了 Python。

**注释口径**：剥真注释（Python 用 `tokenize` 精确剥、sh/YAML/JS 按行剥），文档字符串与
普通字符串字面量**计入**——把键名抄进散文正是"改代码不改散文"的病（与
`scripts/check-shared-facts.sh` 同一立场）。

**口径**
「代码侧缺省单一定义点」只覆盖**名册键**。`yiban/core/window.py` 的
`DEFAULT_EDGE_SEC` 属 core 内防御性回退：该层不读 `.env`，也不许 import
`yiban.adapters.config.loader`（loader 导入本模块，反向成环）。它不读名册，
由 `tests/test_config_registry_gate.py::CoreFallbackEquivalenceTest` 等值钉住。
本门判绿**不等于**"全仓缺省已单源"。

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
- 空串缺省只认"独立的 `""` / `''`"与三引号写的空串（六个连续引号）：三引号
  定界符里的两连引号不算一次回退。该收窄只去假红（散文提到键名本是说明，不是
  定义点），不放宽真回退——`env.get("KEY", "")` 照旧命中。
- 判据 2 的 shell 臂覆盖静态值（含嵌套内层）。`$(…)` / 裸 `$VAR` 静态判不了：
  跳过必须登记在代码侧处置册的 `_meta.shell_exempt_defaults`，未登记即红——
  "跳过"是显性代价，门不把它当绿。
- `default_symbols` 只保证**已声明**的常量被赋字面量时判红。常量与键之间没有机械
  映射，**漏声明的承载常量**本门看不见（第四轮实测 `_DEFAULT_SIGN_START` 改
  `(6, 31)` 仍 rc=0，本轮已补登记）。新增承载常量必须人工补登记。
- 判据 2 的时刻形状臂只认 `(H, M)` / `((H, M))` / `time(H, M)`。改载体形状
  （`(H, M, S)`、字符串元组、字典）即失去保护，diff 里必须写明。
- 判据 5 的类别事实核对：机械类别（shell/container_form/harness/prose）参与文件
  路径投票；`false_positive` / `write_value` / `other_semantics` 无法静态判定，
  只受"文件必须命中至少一个机械类别"这一条约束（类别列表全为非机械时不做路径核）。
- 判据 2/5 的"同行"判据对**真假命中不加区分**：`+ 1`、`> 0`、`lo=0` 这类与缺省
  无关的字面量同样算命中。它们在代码侧处置册里按 `false_positive` 登记豁免。
- 判据 5 的类别事实核对只覆盖可机械判定的四类（shell/container_form/harness/
  prose）；`false_positive` / `write_value` / `other_semantics` 靠评审。
- 判据 6 只比较**文本取值**，不判断差异是否有意义：占位文案不同（"your@qq.com"
  与"你的发件邮箱@qq.com"）也算差异，逐键登记理由即可；注释是否存在靠评审。
- 判据 4 只认模板里的 `KEY=值` 行。README 表格/散文里写"默认 `500`"这类**没有
  `KEY=` 前缀**的取值不在判据内；它靠评审，不靠本门。
- 判据 4 **不比较** `docker-compose.yml` 的注入值。compose 是部署形态声明，
  不是模板缺省：11 行 `YIBAN_*=` 注入里，4 个开门键是容器路径重映射
  （`YIBAN_ENV_FILE` / `YIBAN_DB_FILE` / `YIBAN_STATE_DIR` / `YIBAN_LOG_FILE`
  = `/data/…`），属容器形态刻意值，不入等值判据。
- 判据 5 的台账按文件行数记，不按行号：行号会随无关编辑漂移。代价是同一文件里
  "删一处、加一处"净变化为 0 时抓不到（判据 2 的开门键不受此限）。

**归属**
门禁/运维侧脚本（`scripts/`）。处置册：`config/default-gate-register.json`
（每个名册键恰好一种模板侧处置；可选 `carrier_example`）。代码侧处置册：
`config/code-literal-register.json`（只登记豁免，开门在名册里声明）。守卫测试：
`tests/test_config_registry_gate.py`（含突变验证：每一条放行断言都配一条同形状的
污染断言）。CI 接线：`scripts/dev-verify.sh` 的 `run_ci`。接线若断，
`tests/test_config_registry_gate.py::GateCiWiringTest` 变红。

**用法**
    scripts/check-config-registry.py [--root DIR] [--registry FILE] [--register FILE]
                                     [--code-register FILE] [--scan 路径 ...]
                                     [--min-keys N] [--min-files N]
                                     [--min-template-gated N]
默认 `--root` = 本脚本所在仓库根；`--registry` = `<root>/config/registry.json`；
`--register` = `<root>/config/default-gate-register.json`；
`--code-register` = `<root>/config/code-literal-register.json`；
`--scan` 缺省扫生产树（见 `DEFAULT_SCAN`）。

**退出码**
    0  全部判据通过
    1  有违规（名册不合法、默认值字面量回潮、未登记键、处置册缺键/多余键、
       模板缺省漂移、开门键在模板里缺位、代码侧字面量未处置/陈旧豁免/行数漂移、
       载体差异未登记/登记陈旧）
    2  门禁自身的环境/参数错误：名册或任一处置册缺失/不是 JSON、名册没有任何键、
       键数不足 `--min-keys`、处置册字段形状不合法、开门键数不足
       `--min-template-gated`、模板载体缺失或没有任何取值行、扫描面不存在或
       不贡献任何被扫文件。
       最后几条都是**防废门**：静默的 0 命中会被读成"合规"。
"""
import argparse
import ast
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
#: 代码侧豁免册的相对路径。只登记判据 2 没开门的键；开门在名册里声明。
CODE_REGISTER_RELATIVE = os.path.join("config", "code-literal-register.json")
#: 豁免类别词表（判据 4）。新增类别必须同批改本表与测试。
EXEMPT_CLASSES = ("no_default", "no_template_value", "example_diverges")
#: 代码侧豁免类别词表（判据 5）。每类都回答"为什么这处不算缺省定义点"。
CODE_EXEMPT_CLASSES = (
    "prose",            # docstring/注释里的说明文字：不能读名册
    "false_positive",   # 同行字面量与缺省无关（`+ 1`、`> 0`、`lo=0`、下标）
    "write_value",      # 写初值/清空值，不是读取回退
    "harness",          # 演练/端到端夹具显式注入的测试值
    "container_form",   # compose 等部署形态注入（读不了 JSON）
    "shell",            # bash 回退：读不了 JSON 名册
    "other_semantics",  # 与名册同值/同名但语义不同的常量
)
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
    """缺省值字面量的正则片段（数字要词边界，避免 `100` 命中 `10`）。

    空串是特例：`""` 只在**独立**出现时才算一次空串字面量。三引号文档串的定界符
    （三个连续引号）里必然有两连引号，按裸 `""` 匹配会把"散文提到键名"判成回潮——
    那会把整族空串缺省键逼出开门面。收窄只去假红：`get("KEY", "")` 仍命中。
    """
    alts = []
    for cand in cands:
        if cand == '""':
            # 独立的 `""`，以及用三引号写的空串（`""""""`，六个连续引号）。
            alts.append(r'(?<!")""(?!")')
            alts.append(r'"{6}')
        elif cand == "''":
            alts.append(r"(?<!')''(?!')")
            alts.append(r"'{6}")
        elif re.fullmatch(r"-?\d+(?:\.\d+)?", cand):
            alts.append(r"(?<![\d.])" + re.escape(cand) + r"(?![\d.])")
        else:
            alts.append(re.escape(cand))
    return "(?:" + "|".join(alts) + ")"


def _literal_re(cands):
    return re.compile(_literal_alternation(cands))


def _collect_code_literal_hits(root, files, registry, cache):
    """→ `{键: [(文件, 行号), …]}`：代码侧"键名与缺省字面量同行"的命中。

    判据 2（开门键清零）与判据 5（未开门键的处置）共用这一份扫描——两处各扫一遍
    会让"同一命中"有两种口径。`registry` 条目缺省为 null（或非标量拿不到写法）时
    不参与：没有缺省字面量，就无从断言"回退到缺省"。
    """
    hits = {}
    for key, spec in registry.items():
        if key == CL.META_KEY or not isinstance(spec, dict):
            continue
        cands = _literal_candidates(spec.get("default"))
        if not cands:
            continue
        lit = _literal_re(cands)
        for rel in files:
            for lineno, line in enumerate(_cached_lines(root, rel, cache), 1):
                if key in line and lit.search(line):
                    hits.setdefault(key, []).append((rel, lineno))
    return hits


def _check_default_literals(root, files, registry, cache, hits):
    """判据 2：已开门键的缺省值字面量在代码内清零。

    三种回潮形状：（a）键名与缺省字面量同行；（b）承载常量被赋字面量；
    （c）HH:MM 键的承载是时刻形状时，换算后必须等于名册缺省。
    （c）是**等值断言**：`DEFAULT_END = (7, 50)` 允许，换成 `(7, 51)` 即红。
    时刻形状只认三种：`(H, M)`、`((H, M))`、`time(H, M)`（含 `datetime.time`、
    尾部多余逗号）。**改载体形状即失去保护**（如 `(7, 51, 0)` 或字符串元组）。
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
        for rel, lineno in hits.get(key, []):
            print("默认值字面量: %s 的键名与缺省值 %s 同行出现（%s:%d）——"
                  "缺省只许住在名册里" % (key, " / ".join(cands), rel, lineno))
            viol += 1
        # 空列表 = 显式声明"本键没有承载常量"（形态由 loader 强制，见 validate_registry）。
        symbols = spec.get("default_symbols")
        if not symbols:
            continue
        # 常量回潮判据：常量名后**紧跟** `= 字面量`（同行即可，不必行首——
        # 函数签名里的 `bucket_rate=1.0` 就是这个形状）。认**任何**字面量而不止
        # "等于缺省"那一种：承载常量被赋 7（缺省是 5）同样是第二份定义点，
        # 且它不会撞上"改缺省值"这条判据——只认等值会让静默改值从两个方向漏掉。
        sym_re = re.compile("|".join(
            r"(?<![\w.])" + re.escape(s) +
            r"\s*=\s*(?:-?\d+(?:\.\d+)?|True|False|\"[^\"]*\"|'[^']*')"
            for s in symbols))
        tuple_res = ([re.compile(
            r"(?<![\w.])" + re.escape(s) + pat) for s in symbols for pat in (
                # 双层括号（含尾部多余逗号）
                r"\s*=\s*\(\s*\(\s*(\d{1,2})\s*,\s*(\d{1,2})\s*,?\s*\)\s*\)",
                # 普通元组（含尾部多余逗号）
                r"\s*=\s*\(\s*(\d{1,2})\s*,\s*(\d{1,2})\s*,?\s*\)",
                # datetime.time(H, M) / time(H, M)
                r"\s*=\s*(?:(?:datetime\.)?time)\(\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)",
            )] if spec.get("type") == "hhmm" else [])
        allowed = _default_renderings(spec.get("default"))
        for rel in files:
            for lineno, line in enumerate(_cached_lines(root, rel, cache), 1):
                m = sym_re.search(line)
                if m is not None:
                    sym = m.group(0).split("=")[0].strip()
                    print("默认值常量: %s 的承载常量 %s 被赋成字面量"
                          "（%s:%d）——改成从名册取缺省" % (key, sym, rel, lineno))
                    viol += 1
                    continue
                t = None
                for pattern in tuple_res:
                    t = pattern.search(line)
                    if t is not None:
                        break
                if t is None:
                    continue
                rendered = "%02d:%02d" % (int(t.group(1)), int(t.group(2)))
                if not allowed or rendered not in allowed:
                    print("元组缺省漂移: %s 的承载元组 %s 换算成 %r，名册缺省是 %r"
                          "（%s:%d）——改元组或改名册，一侧必改"
                          % (key, t.group(0).split("=")[0].strip(), rendered,
                             spec.get("default"), rel, lineno))
                    viol += 1
    return viol, gated


#: shell 的缺省回退写法：`${KEY:-…}` / `${KEY:=…}`（嵌套按花括号配对切）。
_SHELL_VAR_START = re.compile(r"\$\{(YIBAN_[A-Z0-9_]+)")
_SHELL_OP = re.compile(r"(:[-=+?]|[-=+?])")
_SHELL_INNER_VAR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: 不是缺省的操作符：`:+` / `+` 是"已设置才替换"，`:?` / `?` 是"未设即报错"的守卫。
#: 顶层的二者都不写缺省值，判据必须跳过（审查员 7yf 低-1：`${KEY:?msg}` 曾被误报漂移）。
#: 嵌套内层的二者让外层取值到运行期才定：按 dynamic 处理，必须登记（第四轮低-2）。
_SHELL_NOT_DEFAULT_OPS = (":+", "+", ":?", "?")
_SHELL_EXEMPT_FIELD = "shell_exempt_defaults"


def _shell_defaults_in_line(line):
    """→ `[(键, 操作符, 值原文)]`：`${KEY:-…}` / `${KEY:=…}`，嵌套按花括号配对。

    只收花括号形态（`$KEY` 没有缺省语法）。操作符原样返回，由调用方跳过 `:+`。
    """
    out = []
    i = 0
    while True:
        start = line.find("${", i)
        if start < 0:
            return out
        m = _SHELL_VAR_START.match(line, start)
        if m is None:
            i = start + 2
            continue
        pos = m.end()
        op = _SHELL_OP.match(line, pos)
        if op is None:
            i = pos
            continue
        depth = 1
        k = op.end()
        while k < len(line) and depth:
            if line.startswith("${", k):
                depth += 1
                k += 2
                continue
            if line[k] == "}":
                depth -= 1
            k += 1
        if depth:
            i = pos
            continue
        out.append((m.group(1), op.group(1), line[op.end():k - 1]))
        i = k


def _shell_static_value(text):
    """→ `(字面量, 状态)`；状态 ∈ {"static", "dynamic"}。

    嵌套 `${INNER:-X}` 递归取 X（审查员 7yf 中-2：`${KEY:-${INNER:-/tmp/x}}`
    内层静态值必须比对）。`$(…)` / 裸 `$VAR` / 无缺省操作符的嵌套 → dynamic。
    **嵌套**里的非缺省操作符（`_SHELL_NOT_DEFAULT_OPS`：`:+` 已设置才替换、
    `:?` 未设即报错）→ dynamic：内层取值到运行期才定，外层整体判不了，必须登记
    （审查员 7yf 第四轮低-2）。**顶层**的 `:+` / `:?` 由调用方先筛掉——它们本身
    不写缺省值。
    """
    text = text.strip()
    while text.startswith("${") and text.endswith("}"):
        inner = text[2:-1]
        m = _SHELL_INNER_VAR.match(inner)
        if m is None:
            return None, "dynamic"
        rest = inner[m.end():]
        op = _SHELL_OP.match(rest)
        if op is None:
            return None, "dynamic"
        if op.group(1) in _SHELL_NOT_DEFAULT_OPS:
            return None, "dynamic"
        text = rest[op.end():].strip()
    if "$(" in text or "`" in text or re.search(r"\$(?:\{)?[A-Za-z_]", text):
        return None, "dynamic"
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return text, "static"


def _shell_exempt_entries(code_register, registry):
    """读代码侧处置册 `_meta.shell_exempt_defaults` → `{(文件, 键): 理由}`。

    形状错（不是列表、缺 file/key/reason、键不在名册、重复）一律拒判（2）。
    键不在名册＝登记面与名册脱钩：这条跳过永远不会被 shell 臂用到（该臂先按名册
    取缺省），留着只会让"登记了却没判"静默通过（审查员 7yf 低-2）。
    """
    meta = code_register.get(CL.META_KEY)
    if not isinstance(meta, dict):
        return {}
    entries = meta.get(_SHELL_EXEMPT_FIELD)
    if entries is None:
        return {}
    if not isinstance(entries, list):
        _die("代码侧处置册 %s 的 %s 必须是列表" % (CL.META_KEY, _SHELL_EXEMPT_FIELD))
    out = {}
    for item in entries:
        if not isinstance(item, dict):
            _die("代码侧处置册 %s 的成员必须是对象" % _SHELL_EXEMPT_FIELD)
        rel, key, reason = item.get("file"), item.get("key"), item.get("reason")
        if not isinstance(rel, str) or not rel:
            _die("代码侧处置册 %s 的成员缺 file" % _SHELL_EXEMPT_FIELD)
        if not isinstance(key, str) or not key.startswith("YIBAN_"):
            _die("代码侧处置册 %s 的成员缺 key（YIBAN_ 开头）" % _SHELL_EXEMPT_FIELD)
        if not isinstance(reason, str) or not reason.strip():
            _die("代码侧处置册 %s 的 %s 缺 reason——跳过必须有理由"
                 % (_SHELL_EXEMPT_FIELD, key))
        if key not in registry:
            _die("代码侧处置册 %s 登记了名册里没有的键 %s:%s——"
                 "名册是配置键的唯一定义点，先登记键再用" % (_SHELL_EXEMPT_FIELD, rel, key))
        if (rel, key) in out:
            _die("代码侧处置册 %s 重复登记 %s:%s" % (_SHELL_EXEMPT_FIELD, rel, key))
        out[(rel, key)] = reason
    return out


def _check_shell_defaults(root, files, registry, cache, code_register):
    """判据 2 的 shell 臂：`${KEY:-值}` / `${KEY:=值}` 的缺省必须等于名册缺省。

    覆盖**所有有缺省的键**，不只开门键（审查员 7yf 低-11：非开门 shell 载体
    名册改值同样会分叉）。规则：
    - 静态值（含嵌套 `${INNER:-X}` 的内层静态值）按等值判，漂移即红；
    - 空值 `${KEY:-}` 按字面量与名册比；与名册不等的按"存在性判定/空缺省"处理，
      必须登记（见下）；
    - `$(…)` / `$VAR` 等动态值静态判不了，必须登记（§30：跳过≠绿）；
    - 非缺省操作符（`_SHELL_NOT_DEFAULT_OPS`：`:+` / `+` / `:?` / `?`）在**顶层**
      不写缺省值，不判（审查员 7yf 低-1：`${KEY:?must set}` 曾是假红）；同样的
      操作符出现在**嵌套内层**时，外层取值到运行期才定 ⇒ 按动态值处理，必须登记；
    - 登记面：代码侧处置册 `_meta.shell_exempt_defaults`（file + key + reason）。
      未登记的跳过、登记了却没再出现的（陈旧），都判 1。
    """
    exempt = _shell_exempt_entries(code_register, registry)
    seen = set()
    viol = 0
    for rel in files:
        for lineno, line in enumerate(_cached_lines(root, rel, cache), 1):
            if "${" not in line:
                continue
            for key, op, raw in _shell_defaults_in_line(line):
                if op in _SHELL_NOT_DEFAULT_OPS:
                    continue
                spec = registry.get(key)
                default = spec.get("default") if isinstance(spec, dict) else None
                if default is None:
                    continue
                value, state = _shell_static_value(raw)
                kind = None
                if state == "dynamic":
                    kind = "动态值"
                else:
                    allowed = _default_renderings(default)
                    if allowed and value in allowed:
                        continue
                    if value == "":
                        kind = "空值/存在性判定"
                    else:
                        print("shell 缺省漂移: %s 在 %s:%d 的 ${%s%s%s} 不等于名册缺省 %r"
                              "——两侧必需同值"
                              % (key, rel, lineno, key, op, raw,
                                 spec.get("default")))
                        viol += 1
                        continue
                seen.add((rel, key))
                if (rel, key) in exempt:
                    print("shell 缺省跳过（已登记）: %s:%d %s（%s）"
                          % (rel, lineno, key, exempt[(rel, key)]))
                else:
                    print("shell 缺省未登记: %s:%d %s 的 ${%s%s%s} 静态判不了"
                          "（%s）——登记到代码侧处置册的 %s"
                          % (rel, lineno, key, key, op, raw, kind,
                             _SHELL_EXEMPT_FIELD))
                    viol += 1
    for rel, key in sorted(set(exempt) - seen):
        print("陈旧 shell 跳过登记: %s:%s 已不再出现——删掉这条登记" % (rel, key))
        viol += 1
    return viol


def _docstring_line_ranges(root, rel):
    """该 `.py` 文件里属于 docstring 的行号集合（1-based）。解析失败返回 None。"""
    try:
        tree = ast.parse(_read(os.path.join(root, rel)))
    except SyntaxError:
        return None
    lines = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        body = getattr(node, "body", None) or []
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            start = first.lineno
            end = getattr(first, "end_lineno", start) or start
            lines.update(range(start, end + 1))
    return lines


#: 类别 → 路径判据。只覆盖能机械判定的四类；其余类别不可静态判定（不投票）。
#: `container_form` **只认部署件**：`docker/scheduler.py` 能读 JSON 名册，不是容器形态
#: 的值定义点（审查员 7f delta 中-1 第 4 条）。
_CLASS_PATH_RULES = (
    ("shell", lambda rel: rel.endswith(".sh")),
    ("container_form", lambda rel: rel == "docker-compose.yml"
                                  or (rel.startswith("docker/")
                                      and not rel.endswith(".py"))),
    ("harness", lambda rel: rel.startswith(("scripts/drill", "frontend/e2e/",
                                            "tests/"))),
    ("prose", lambda rel: rel.endswith(".py")),
)
_MECHANICAL_CLASSES = frozenset(name for name, _ in _CLASS_PATH_RULES)


def _class_file_matches(klass, rel):
    """该文件是否可能是这个类别的载体。无路径规则的类别**不投票**（返回 False）。"""
    for name, rule in _CLASS_PATH_RULES:
        if name == klass:
            return rule(rel)
    return False


def _check_class_facts(key, klasses, files, hits, root):
    """判据 5 的事实核对：豁免类别必须与命中载体的形状一致。

    规则（审查员 7f delta 中-1）：
    - 类别列表里只要有**机械类别**，每个命中文件必须命中至少一个机械类别；
      非机械类别（false_positive / write_value / other_semantics）不投票。
    - `prose` **在场即做行校验**：该文件的命中行必须落在 docstring 里
      （混合列表不再绕过）。
    - `false_positive` / `write_value` / `other_semantics` 的语义无法静态判定，
      只受上面的文件路径规则约束。
    """
    viol = 0
    mechanical = [k for k in klasses if k in _MECHANICAL_CLASSES]
    if mechanical:
        for rel in files:
            if any(_class_file_matches(k, rel) for k in mechanical):
                continue
            print("代码侧豁免类别不符: %s 把 %s 归为 %s，但文件不匹配任何机械类别——"
                  "类别必须与事实一致" % (key, rel, "、".join(klasses)))
            viol += 1
    if "prose" not in klasses:
        return viol
    for rel in files:
        if not rel.endswith(".py"):
            continue
        ranges = _docstring_line_ranges(root, rel)
        if ranges is None:
            print("代码侧豁免类别无法判定: %s 的 %s 解析失败，prose 核不了" % (key, rel))
            viol += 1
            continue
        bad = sorted(ln for r, ln in hits.get(key, []) if r == rel and ln not in ranges)
        if bad:
            print("代码侧豁免类别不符: %s 在 %s 的命中行 %s 不在 docstring 里——"
                  "prose 只覆盖文档串" % (key, rel, "、".join(str(n) for n in bad)))
            viol += 1
    return viol


def _check_code_dispositions(root, hits, registry, code_register):
    """判据 5：未开门键的代码侧命中必须逐键登记豁免，且台账与命中面一致。

    台账按**文件行数**比对（见模块 docstring 的已知弱化点）。形状问题（未知类别、
    缺理由、files 不是"非空文件→正整数"、开门键却登记豁免）是门禁自身的输入错误
    （退出码 2）；命中未登记、登记陈旧、行数漂移、类别与载体形状不符是内容违规
    （退出码 1）。
    """
    known = set(CL.registry_keys(registry))
    entries = {k: v for k, v in code_register.items() if k != CL.META_KEY}
    viol = 0
    stale = sorted(set(entries) - known)
    if stale:
        print("代码侧处置册多余键: %s——名册里没有这个键" % "、".join(stale))
        viol += 1
    for key in sorted(set(entries) & known):
        spec = registry.get(key)
        if not isinstance(spec, dict):
            _die("名册 %s 的条目不是对象，判据 5 无法判" % key)
        if spec.get("default_literal_gate") is True:
            _die("代码侧处置册 %s：该键在名册里开了默认值字面量门——"
                 "开门要的是清零，不许同时登记豁免" % key)
        entry = entries[key]
        if not isinstance(entry, dict):
            _die("代码侧处置册 %s 的值必须是对象（class + reason + files）" % key)
        klass = entry.get("class")
        klasses = [klass] if isinstance(klass, str) else klass
        if (not isinstance(klasses, list) or not klasses
                or any(k not in CODE_EXEMPT_CLASSES for k in klasses)):
            _die("代码侧处置册 %s 的 class %r 不在 %r 内（一个键命中多种形状时给列表）"
                 % (key, klass, list(CODE_EXEMPT_CLASSES)))
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            _die("代码侧处置册 %s 缺 reason——豁免必须有理由" % key)
        files = entry.get("files")
        if not isinstance(files, dict) or not files:
            _die("代码侧处置册 %s 缺 files（每个命中文件 → 行数）——"
                 "只登记「有没有」挡不住同类新增" % key)
        for rel, want in files.items():
            if not isinstance(want, int) or isinstance(want, bool) or want < 1:
                _die("代码侧处置册 %s 的 files[%r] 必须是正整数（实际 %r）"
                     % (key, rel, want))
        viol += _check_class_facts(key, klasses, files, hits, root)
    # —— 命中面必须有处置（判据 2 已判红的开门键不重复报）
    for key in sorted(hits):
        if key not in known:
            continue                      # 未登记键由判据 3 报
        spec = registry.get(key)
        if isinstance(spec, dict) and spec.get("default_literal_gate") is True:
            continue
        if key in entries:
            continue
        where = "、".join("%s:%d" % (rel, lineno) for rel, lineno in hits[key][:6])
        print("代码侧未处置: %s 在代码里与缺省字面量同行 %d 处（%s）——"
              "改成从名册取缺省，或在 %s 里登记豁免＋理由"
              % (key, len(hits[key]), where, CODE_REGISTER_RELATIVE))
        viol += 1
    # —— 台账必须与命中面一致（陈旧 / 漂移都红）
    for key in sorted(set(entries) & known):
        actual = {}
        for rel, _lineno in hits.get(key, []):
            actual[rel] = actual.get(rel, 0) + 1
        if not actual:
            print("陈旧代码侧豁免: %s 在代码侧已零命中——删掉这条登记" % key)
            viol += 1
            continue
        claimed = dict(entries[key]["files"])
        if actual != claimed:
            print("代码侧豁免行数漂移: %s 台账 %r，实际 %r——"
                  "重新判定这 %d 处命中后改台账" % (key, claimed, actual, len(hits[key])))
            viol += 1
    return viol


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


def _read_register(path, what="处置册"):
    """读处置册。读不了/坏 JSON/顶层不是对象 ⇒ 拒判（2）。

    "读不了"包括缺失文件、路径是目录、权限拒绝：它们都是环境错误（2），
    不是"有违规"（1）。不先 `isfile` 预判，`io.open` 的 OSError 统一收口。
    """
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            raw = json.load(f)
    except OSError as exc:
        _die("%s读不了: %s（%s）——每个名册键必须逐键处置（开门或豁免+理由）"
             % (what, path, exc))
    except ValueError as exc:
        _die("%s不是合法 JSON: %s（%s）" % (what, path, exc))
    if not isinstance(raw, dict):
        _die("%s顶层必须是对象: %s" % (what, path))
    return raw


def _validate_register(register, registry, occurrences):
    """判据 4/6 的前置：处置册必须覆盖每个名册键，每键恰好一种模板侧处置。

    另外钉住豁免类别与事实一致（三条零成本前提）：
    - `no_default` ⇒ 名册缺省为 None；
    - `no_template_value` ⇒ 三份载体零取值行；
    - `example_diverges` ⇒ 至少一行取值不等于缺省。
    豁免写错类别会让判据静默失去覆盖，故按违规（1）判红。

    判据 6 的两侧也在这里判：`carrier_example` 只许给无缺省的键、理由必填、
    且载体取值必须真的不一致（陈旧登记判 1）；无缺省键的载体取值不一致却没登记，
    同样判 1。有缺省的键由判据 4 按等值判，不给"刻意差"这条出路。

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
        default = spec.get("default")
        rows = occurrences.get(key, [])
        values = {value for _, _, value in rows}
        if "carrier_example" in entry:
            if default is not None:
                _die("处置册 %s 标了 carrier_example，但名册有缺省 %r——"
                     "有缺省的键模板必须等于缺省，不存在载体刻意差"
                     % (key, default))
            carrier = entry["carrier_example"]
            if not isinstance(carrier, dict):
                _die("处置册 %s 的 carrier_example 必须是对象（reason）" % key)
            reason = carrier.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                _die("处置册 %s 的 carrier_example 缺 reason——"
                     "刻意差必须写明为什么" % key)
            if len(values) < 2:
                print("处置册错配: %s 标了 carrier_example，但载体取值一致（%s）——"
                      "删掉这条登记" % (key, "、".join(sorted(values)) or "无取值行"))
                viol += 1
        elif not has_gate and default is None and len(values) >= 2:
            print("载体差异未登记: %s 在多份载体里取值不一致（%s）——"
                  "逐键登记 carrier_example 并写明为何刻意不同"
                  % (key, "、".join(sorted(values))))
            viol += 1
        if has_gate:
            if entry["default_literal_gate"] is not True:
                _die("处置册 %s 的 default_literal_gate 只允许 true" % key)
            if default is None:
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
                    " + 模板缺省等值 + 代码侧字面量逐键处置 + 载体示例差异登记")
    ap.add_argument("--root", default=_REPO_ROOT)
    ap.add_argument("--registry", default=None)
    ap.add_argument("--register", default=None,
                    help="逐键处置册；缺省 <root>/config/default-gate-register.json")
    ap.add_argument("--code-register", default=None,
                    help="代码侧豁免册；缺省 <root>/config/code-literal-register.json")
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
    code_register_path = args.code_register or os.path.join(
        root, CODE_REGISTER_RELATIVE)
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
    code_register = _read_register(code_register_path, what="代码侧处置册")

    _guard_scan_surface(root, scans)
    files = _scan_files(root, scans)
    files = [f for f in files if f != SELF]
    if len(files) < args.min_files:
        _die("只扫到 %d 个文件（< --min-files %d）——拒绝零命中即通过"
             % (len(files), args.min_files))

    print("名册: %s（%d 键）" % (registry_path, len(keys)))
    print("处置册: %s" % register_path)
    print("代码侧处置册: %s" % code_register_path)
    print("扫描面: %s（%d 个文件）" % (" ".join(scans), len(files)))

    viol = 0
    # ——判据 1：名册自校验（复用加载器的校验器，门禁不自抄第二套）
    try:
        CL.validate_registry(raw)
    except CL.RegistryError as exc:
        print("名册不合法: %s" % exc)
        viol += 1

    # ——判据 4/6 的前置：读模板载体（缺失/塌陷即拒判），再核逐键处置与载体差异
    occurrences = _read_template_occurrences(root)
    viol += _validate_register(register, raw, occurrences)

    # ——判据 2、3、5（代码面）。模板载体不参与判据 2：模板按设计写示例值，
    # 那由判据 4 按"等值"判，不按"清零"判。
    cache = {}
    code_files = [f for f in files if f not in TEMPLATE_CARRIERS]
    hits = _collect_code_literal_hits(root, code_files, raw, cache)
    lit_viol, gated = _check_default_literals(root, code_files, raw, cache, hits)
    print("默认值字面量门: %d 键（%s）"
          % (len(gated), "、".join(k for k, _ in gated) or "无"))
    viol += lit_viol
    viol += _check_shell_defaults(root, code_files, raw, cache, code_register)
    code_exempt = sum(1 for k in code_register if k != CL.META_KEY)
    print("代码侧豁免登记: %d 键" % code_exempt)
    viol += _check_code_dispositions(root, hits, raw, code_register)

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
    print("config-registry ok: %d 键、%d 键盘默认值字面量、%d 键代码侧豁免、"
          "%d 键模板缺省、扫描 %d 文件，全部合规"
          % (len(keys), len(gated), code_exempt, template_gated, len(files)))
    sys.exit(0)


if __name__ == "__main__":
    main()
