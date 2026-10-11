#!/usr/bin/env python
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
结构门禁：跑 `.importlinter` 的四条依赖方向契约，只对**基线之外的新增违规**判红。

用法：
    scripts/check-import-linter.py [--root DIR] [--config FILE] [--baseline FILE]
                                   [--min-modules N] [--write-baseline]

契约本体住 `.importlinter`（仓库根）。本脚本不复制契约内容。本脚本只做三件事：
读配置、跑契约、拿当前违规集与基线对账。

**观测模式（P0，docs/refactor/128 三）**
本批不改结构，只钉"不再变坏"。当前违规落进基线文件。基线只许缩小。
新增违规即红。存量收紧留给后续批次（P2c 出口＝基线清零或显式豁免清单）。

**为什么用 import-linter 而不是自己写 AST 扫描**
依赖方向判定要处理相对导入、包与模块的包含关系、间接依赖、重叠对跳过等边界。
自写版会在这些边界上漏判。漏判等于假绿。用户指令：优先经受打磨的外部库。

**为什么用 Python API 而不是解析 CLI 文本**
import-linter 2.15 的 CLI 没有机器可读输出格式。解析人读文本会在小版本升级时静默失配。
`use_cases.read_user_options` 与 `use_cases.create_report` 给出结构化报告。
代价：这两个入口不是文档化的公共 API。故本脚本对报告形态做**严格校验**，
形态不合即退 2，绝不退 0（防废门）。

**假绿的两条路，本门各堵一条**
一、图塌了：分析到的模块数低于 `--min-modules` 即退 2。
二、契约空转：声明的模块表达式在图里零命中即退 2。import-linter 对"目标模块不在图里"
    不报错，契约会恒 KEPT（实测 2026-10-11）。故本门自己查每条契约的命中数。

**违规的身份（基线的行格式）**
一行一条，制表符分三列：`契约 id <TAB> 导入者模块 <TAB> 被导入模块`。
身份用**契约 id**，不用契约的 `name`。`name` 是人读散句，改一次文案就让整份基线失效；
`id` 是配置段名 `[importlinter:contract:<id>]`，稳定。
行内的文件与行号不进身份。同一条违规换行不产生新条目。按此口径基线才对得上。

**已知弱化点（如实登记，别当"已完全覆盖"）**
字符串拼接的 import（`importlib.import_module("web.x")`）不在判定面内。
import-linter 走静态图，这条边界由它自身决定。这类写法要收口时另立门。
`tests/` 与 `frontend/` 不在 `root_packages` 内。本门只判生产树。
`exclude_type_checking_imports` 保持默认（False）。故 `if TYPE_CHECKING:` 里的导入
照常判定，这是刻意从严。

**归属**
门禁/结构侧脚本（`scripts/`）。守卫测试：`tests/test_import_linter_gate.py`。
CI 接线：`scripts/dev-verify.sh` 的 `run_ci`。契约本体：`.importlinter`。
"""
import argparse
import io
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE_HEADER = (
    "# import-linter 违规基线（P0 观测模式，docs/refactor/128 §三）\n"
    "# 一行一条：契约 id <TAB> 导入者模块 <TAB> 被导入模块\n"
    "# 契约 id 取配置段名 [importlinter:contract:<id>]，不用人读的 name（改文案不能让基线失效）。\n"
    "# 语义：基线之外的违规即红。基线只许缩小（ratchet）。\n"
    "# 收紧基线：修掉违规后跑 scripts/check-import-linter.py --write-baseline。\n"
    "# 本文件不许手工加宽：加宽等于放水，须在 PR 描述里逐条写理由。\n"
)


def _die(msg, code=2):
    """环境/参数错误：点名问题并退 2。与"有新增违规"（1）严格分开。"""
    print("check-import-linter: %s" % msg, file=sys.stderr)
    sys.exit(code)


def _read_text(path, what):
    """整份读出文本。读不了当门禁自己的环境错误（退 2）。"""
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as exc:
        _die("读不了%s: %s（%s）" % (what, path, exc))


def _extract_violations(metadata):
    """从契约报告的元数据里抽出 (导入者, 被导入者) 对。

    递归找同时含 `downstream_module` 与 `upstream_module` 的字典。
    forbidden 与 independence 两种契约都用这个形状。递归找是为了不绑死具体层级。
    """
    found = []

    def walk(node):
        if isinstance(node, dict):
            if "downstream_module" in node and "upstream_module" in node:
                found.append((node["downstream_module"], node["upstream_module"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(metadata)
    return found


def _parse_baseline(text, path):
    """读基线行。返回 (契约名, 导入者, 被导入者) 的集合。列数不对即硬失败。"""
    out = set()
    for num, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        cells = raw.rstrip("\r").split("\t")
        if len(cells) != 3 or not all(c.strip() for c in cells):
            _die("基线 %s 第 %d 行列数应为 3 且各列非空: %r" % (path, num, raw))
        out.add((cells[0].strip(), cells[1].strip(), cells[2].strip()))
    return out


def _load_importlinter():
    """导入 import-linter 的两个入口。缺件即退 2，绝不静默跳过。

    必须先 `configuration.configure()`：否则 settings 里的 ini 读取器没注册，
    `read_user_options` 会抛 KeyError('USER_OPTION_READERS')。CLI 入口自己会配置，
    走 API 的调用方要自己配置（`importlinter.api` 模块就是这么做的前例）。
    """
    try:
        from importlinter import configuration
        from importlinter.application import use_cases
    except ImportError as exc:
        _die("import-linter 不可用（%s）。本门不许静默跳过。装上再跑：\n"
             "    python -m pip install import-linter==2.15" % exc)
    configuration.configure()
    return use_cases


def _contract_ids(user_options):
    """契约的 `name` → 契约 id 映射。

    报告只给出契约的 `name`（配置段里的 `name =` 那行，是人读散句）。
    基线要用稳定的 id，故这里从配置选项把 `id` 取出来。ini 读取器给每条契约填了
    `id`（配置段名 `[importlinter:contract:<id>]` 的后缀）。两条契约同名即撞车，判环境错误。
    """
    by_name = {}
    for opt in user_options.contracts_options:
        cid, name = opt.get("id"), opt.get("name")
        if not cid or not name:
            _die("配置里有契约缺 id 或缺 name：%r（基线要用 id，缺了就没法对账）" % (opt,))
        if name in by_name:
            _die("配置里两条契约用了同一个 name：%s（违规身份会撞车）" % name)
        by_name[name] = cid
    return by_name


def _assert_contracts_have_targets(user_options, graph_modules):
    """每条契约声明的模块表达式都必须在分析图里有命中。空命中＝契约空转，退 2。

    为什么必须自己查：import-linter 对"目标模块不在图里"不报错。实测（2026-10-11）：
    契约写 `forbidden_modules = pkg.nonexistent` 时它把该目标从判定面里滤掉，
    契约恒 KEPT、退出 0——一条死掉的契约被读成合规，正是本门最该拦的假绿。
    源模块缺件它会抛错（ValueError），目标模块缺件它不抛，故这一侧必须本门自己兜。
    """
    for opt in user_options.contracts_options:
        cid = opt.get("id", "<无 id>")
        for key in ("source_modules", "forbidden_modules", "modules"):
            for raw in opt.get(key) or ():
                expr = raw.strip()
                if not expr:
                    continue
                if expr.endswith(".*"):
                    base = expr[:-2]
                    hits = [m for m in graph_modules
                            if m == base or m.startswith(base + ".")]
                else:
                    hits = [m for m in graph_modules if m == expr]
                if not hits:
                    _die("契约 '%s' 的 %s 里的 '%s' 在分析图里零命中——该契约空转（恒通过）。"
                         "拒绝把它读成合规：要么模块名打错，要么该模块已被删除、"
                         "对应的契约该一起退役" % (cid, key, expr))


def _build_report(use_cases, config, root, min_modules):
    """跑配置里的全部契约，返回 (report, [(契约 id, 导入者, 被导入者)])。"""
    if not os.path.isfile(config):
        _die("契约配置不存在: %s（本门必须自包含，CI runner 上没有本地件）" % config)
    os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        user_options = use_cases.read_user_options(config)
    except Exception as exc:  # 配置读不了/选项非法：门自己的错，不是"有违规"
        _die("读不了契约配置（%s）：%s" % (config, exc))
    id_by_name = _contract_ids(user_options)
    # 走 API 就必须自己注册契约类型。CLI 入口（lint_imports）内部会做这一步，
    # create_report 不做（它假定已注册）。缺这个私有入口即判升版失配，退 2。
    register = getattr(use_cases, "_register_contract_types", None)
    if register is None:
        _die("import-linter 的 API 形态变了：use_cases._register_contract_types 不见了。"
             "本门按 2.15 的形态写，升版后须同步本脚本，不许默认放行")
    try:
        register(user_options)
        # cache_dir=None 关掉缓存：门禁要可复现，不许读上一次的图。
        report = use_cases.create_report(user_options, cache_dir=None)
    except Exception as exc:
        _die("契约跑不起来（%s）：%s" % (config, exc))

    if report.could_not_run or report.invalid_contract_options:
        _die("有契约选项非法，本跑不作数：%s"
             % sorted(report.invalid_contract_options))
    if report.module_count < min_modules:
        _die("只分析到 %d 个模块（< --min-modules %d）。图塌了，拒绝零违规即通过"
             % (report.module_count, min_modules))
    if not report.contracts:
        _die("配置里一条契约都没跑（配置被清空或 root_packages 打错？）")
    _assert_contracts_have_targets(user_options, report.graph.modules)

    violations = []
    for contract, check in report.get_contracts_and_checks():
        if check.kept:
            continue
        cid = id_by_name.get(contract.name)
        if cid is None:
            _die("报告里的契约 '%s' 不在配置选项里，取不到 id——配置与报告的对应关系断了"
                 % contract.name)
        pairs = _extract_violations(check.metadata)
        if not pairs:
            # 破了却抽不出违规：元数据形态变了。静默当"零违规"会变成假绿。
            _die("契约 '%s' 判破，却抽不出违规条目——元数据形态与预期不符"
                 "（import-linter 升版了？）" % contract.name)
        for importer, imported in pairs:
            violations.append((cid, importer, imported))

    print("契约数 %d（kept %d / broken %d），模块 %d，依赖 %d"
          % (len(report.contracts), report.kept_count, report.broken_count,
             report.module_count, report.import_count))
    for contract, check in report.get_contracts_and_checks():
        print("    %s [%s] %s"
              % ("KEPT  " if check.kept else "BROKEN", id_by_name[contract.name], contract.name))
    return report, sorted(violations)


def _write_baseline(path, violations):
    """把当前违规集写成基线文件（含文件头）。返回写入条数。"""
    body = "".join("%s\t%s\t%s\n" % row for row in violations)
    try:
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(BASELINE_HEADER)
            f.write(body)
    except OSError as exc:
        _die("基线写不进去: %s（%s）" % (path, exc))
    return len(violations)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="scripts/check-import-linter.py",
        description="import-linter 四条依赖方向契约门（基线外新增违规即红）")
    ap.add_argument("--root", default=REPO_ROOT)
    ap.add_argument("--config", default=None, help="默认 <root>/.importlinter")
    ap.add_argument("--baseline", default=None,
                    help="默认 <root>/scripts/gate/import-linter-baseline.txt")
    ap.add_argument("--min-modules", type=int, default=100,
                    help="分析到的模块数下限；低于它判环境错误（防图塌后假绿）")
    ap.add_argument("--write-baseline", action="store_true",
                    help="把当前违规集写回基线（收紧用；加宽须在 PR 里写理由）")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.root):
        _die("根目录不存在: %s" % args.root)
    root = os.path.abspath(args.root)
    config = args.config or os.path.join(root, ".importlinter")
    baseline_path = args.baseline or os.path.join(root, "scripts/gate/import-linter-baseline.txt")

    use_cases = _load_importlinter()
    _report, violations = _build_report(use_cases, config, root, args.min_modules)
    current = set(violations)

    if args.write_baseline:
        n = _write_baseline(baseline_path, violations)
        print("%s ok: 已写入 %d 条违规为基线（%s）" % (ap.prog, n, baseline_path))
        return 0

    baseline = _parse_baseline(_read_text(baseline_path, "基线文件"), baseline_path)
    new = sorted(current - baseline)
    gone = sorted(baseline - current)

    print("当前违规 %d 条，基线 %d 条" % (len(current), len(baseline)))
    for name, importer, imported in sorted(current):
        print("    违规 %s: %s -> %s" % (name, importer, imported))
    if gone:
        print("降档: 基线里有 %d 条已不再出现——同批把基线收紧到当前集：" % len(gone))
        for name, importer, imported in gone:
            print("    已消失 %s: %s -> %s" % (name, importer, imported))
        print("    收紧命令: scripts/check-import-linter.py --write-baseline")

    if new:
        print("新增违规 %d 条（不在基线内）：" % len(new))
        for name, importer, imported in new:
            print("    %s: %s -> %s" % (name, importer, imported))
        print("这些依赖方向违反 128 的结构约定。"
              "要么改代码消除它，要么按流程把基线收紧后重开一条工单登记豁免。")
        sys.exit(1)

    print("import-linter ok: 四条依赖方向契约没有基线之外的新增违规")
    return 0


if __name__ == "__main__":
    sys.exit(main())
