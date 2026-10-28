#!/usr/bin/env python
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
结构门禁：跑 `.importlinter` 的依赖方向契约，对**任何**违规判红。

用法：
    scripts/check-import-linter.py [--root DIR] [--config FILE] [--baseline FILE]
                                   [--min-modules N]

契约本体住 `.importlinter`（仓库根）。本脚本不复制契约内容。本脚本只做三件事：
读配置、跑契约、把违规判红。

**豁免口径（工单 jlmv）**
基线机制**已退役**：import-linter 的 forbidden 契约把整条契约聚成一个模块对
（源码 `_modules_overlap` 的聚合粒度是 `(source_pkg, forbidden_pkg)`），故登记一行
基线等于放行该方向**全部**边——"新增即红"失效。确需接受的边一律写进
`.importlinter` 对应契约的 `ignore_imports`，**逐边列名**；import-linter 对
无匹配的豁免条目报错（退 2），生命周期自证。`scripts/gate/import-linter-baseline.txt`
成为墓碑文件：只允许文件头，任何数据行都判环境错误。

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

**违规的身份**
报告元数据里的 `(导入者, 被导入者)` 对逐条打印。身份不再进基线文件，故不需要
稳定 id 映射；契约 id 仍用于输出定位。

**已知弱化点（如实登记，别当"已完全覆盖"）**
字符串拼接的 import（`importlib.import_module("web.x")`）不在判定面内。
import-linter 走静态图，这条边界由它自身决定。这类写法要收口时另立门。
`tests/` 与 `frontend/` 不在 `root_packages` 内。本门只判生产树。
`exclude_type_checking_imports` 保持默认（False）。故 `if TYPE_CHECKING:` 里的导入
照常判定，这是刻意从严。
有节点零边的图不设防（零边即全 KEPT），已知边界：本门只守模块数，不守依赖边数。

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
    "# import-linter 基线墓碑（机制已退役，工单 jlmv）\n"
    "# 本文件只允许文件头。基线按模块对放行 = 放行整条方向，\"新增即红\"会失明。\n"
    "# 确需接受的边写进 .importlinter 对应契约的 ignore_imports（逐边列名）；\n"
    "# import-linter 对无匹配的豁免条目报错退 2，生命周期自证。\n"
)


def _die(msg, code=2):
    """环境/参数错误：点名问题并退 2。与"有违规"（1）严格分开。"""
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


def _assert_baseline_is_empty(text, path):
    """基线文件只允许注释；任何数据行都判环境错误（工单 jlmv）。

    为什么拒绝而不是忽略：留着数据行会让人以为"这里有豁免"，而门其实按任何违规
    判红——两套口径并存必然误判。豁免的唯一出口是 `.importlinter` 的
    `ignore_imports`（逐边列名，无匹配条目会被 import-linter 自己拒绝）。
    """
    for num, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        _die("基线文件 %s 第 %d 行仍有数据行 %r。基线机制已退役（工单 jlmv）："
             "按模块对登记会放行整条方向。请把确需接受的边逐条写进 `.importlinter` "
             "对应契约的 `ignore_imports`，并把本文件恢复为只有文件头"
             % (path, num, raw))


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
                    # grimp 的 `X.*` 只匹配 X 的**子模块**，不含 X 本身（实测 2026-10-11：
                    # `find_matching_modules("pkg.deadend.*") == []`，尽管 `pkg.deadend` 是包）。
                    # 故命中只按 `base + "."` 前缀判，不许把 `m == base` 也算命中——算了就会
                    # 放过"base 包在、零子模块"的空转配置（审查 H1）。
                    hits = [m for m in graph_modules if m.startswith(base + ".")]
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


def _build_parser():
    """构建参数解析器。独立成函数：元测试正面钉住 `--min-modules` 的默认下限（M3）。

    下限是"图塌后假绿"的守卫，而元测试真跑时恒传 `--min-modules 1`。故默认值只能
    由解析器对象本身钉住，否则把默认改成 0 也不会有红。
    """
    ap = argparse.ArgumentParser(
        prog="scripts/check-import-linter.py",
        description="import-linter 依赖方向契约门（任何违规即红；豁免走 ignore_imports）")
    ap.add_argument("--root", default=REPO_ROOT)
    ap.add_argument("--config", default=None, help="默认 <root>/.importlinter")
    ap.add_argument("--baseline", default=None,
                    help="墓碑文件：只允许文件头（默认 <root>/scripts/gate/import-linter-baseline.txt）")
    # 128-P0 定值：实测 131 模块，取下限 100 防图塌。跌破 100 必须同批重定并在 PR 写理由。
    ap.add_argument("--min-modules", type=int, default=100,
                    help="分析到的模块数下限；低于它判环境错误（防图塌后假绿）")
    ap.add_argument("--write-baseline", action="store_true",
                    help="已退役：基线按模块对放行会失明，改用 .importlinter 的 ignore_imports")
    return ap


def main(argv=None):
    ap = _build_parser()
    args = ap.parse_args(argv)

    if not os.path.isdir(args.root):
        _die("根目录不存在: %s" % args.root)
    if args.write_baseline:
        _die("--write-baseline 已退役（工单 jlmv）：基线按模块对登记会放行整条方向。"
             "确需接受的边请写进 `.importlinter` 对应契约的 `ignore_imports`（逐边列名）")
    root = os.path.abspath(args.root)
    config = args.config or os.path.join(root, ".importlinter")
    baseline_path = args.baseline or os.path.join(root, "scripts/gate/import-linter-baseline.txt")
    _assert_baseline_is_empty(_read_text(baseline_path, "基线文件"), baseline_path)

    use_cases = _load_importlinter()
    _report, violations = _build_report(use_cases, config, root, args.min_modules)

    print("当前违规 %d 条（基线机制已退役：任何违规即红）" % len(violations))
    for name, importer, imported in sorted(violations):
        print("    违规 %s: %s -> %s" % (name, importer, imported))
    if violations:
        print("这些依赖方向违反结构契约。要么改代码消除它；确需接受时在 `.importlinter` "
              "对应契约的 `ignore_imports` 里逐边列名（无匹配条目会被 import-linter 拒绝）。",
              file=sys.stderr)
        return 1

    print("import-linter ok: 依赖方向契约零违规")
    return 0


if __name__ == "__main__":
    sys.exit(main())
