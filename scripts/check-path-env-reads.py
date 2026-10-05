#!/usr/bin/env python
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
B1 的静态门禁：登记在册的部署键不得用裸 `os.environ` 读。工单要求的判据形态是
"上限冻结"：允许上限 = 立门当天实测的命中数，新增一处即红，修掉一处必须同批降一档。

用法：
    scripts/check-path-env-reads.py [--root DIR] [--roster FILE] [--min-files N]
    默认 `--root` = 本脚本所在仓库根、`--roster` = scripts/gate/shared-facts.tsv、
    `--min-files` = 50。

**为什么判定要用 AST，不能用 grep**
判据要分得开"读"与"写"。`environ[K] = v` 是把配置交给子进程（db_export、
dev_visual_seed 一类），不是绕过解析器；grep 会把写侧一起数进来，于是每一处合法
写键都要放水。注释也不在判据里，AST 天然看不见注释。docstring 与字符串字面量在
判据里（与名册文件头同一口径），所以本门在 AST 之外再扫一遍字面量的文本。

**键集、扫描范围、上限都住在名册里**
本脚本只认名册 `判定模式` 列的 `ast:` 路由标记：`ast:<规则名>:<键1,键2,…>`。
本脚本不含任何一枚键名，也不含任何一枚上限数字——那是第二份名册，会与数据文件
各自漂移（tests/test_shared_facts_gate.py 对 awk 引擎立的是同一条规矩）。被
`ast:` 路由的登记行由 `scripts/check-shared-facts.sh` 让开，同一枚键只许一个引擎计数。

**判据（三条读位置形，K 必须在册且是字面字符串常量）**
    environ.get("K", …)          宿主名认 `os.environ` 与模块级 `environ`
    getenv("K") / os.getenv("K")
    environ["K"]                 Subscript 且 ctx=Load
写位置不计：`environ[K] = v`（Store）、`del environ[K]`（Del）、
`environ.setdefault(K, v)`。字面量与 docstring 里出现上述写法各计一次，位置取
该字面量节点的起始行。

**已知弱化点（如实登记，别当"已完全覆盖"）**
键名来自变量、f-string 拼接或别名导入（`from os import environ as e`）时，取不到
字面常量 ⇒ 不计。这类写法要收口那一刀顺带清掉；本门的活体反例只钉字面常量形状。

**归属**
门禁/运维侧脚本（`scripts/`）。守卫测试：`tests/test_path_env_read_gate.py`。
CI 接线：`scripts/dev-verify.sh` 的 `run_ci`。

**退出码**
    0  全部登记键的命中数都在允许上限内
    1  有键超标（逐条点名 文件:行 与键名）
    2  门禁自身的参数/环境错误：名册或根目录不存在、没有任何 `ast:` 路由行、
       规则名不认识、键集为空、名册行缺列、扫描范围在 `--root` 下不存在或贡献
       0 个 .py、被扫的 .py 语法错、扫到的文件数不足 `--min-files`。
       最后几条都是**防废门**：静默的 0 命中会被读成"合规"。
"""
import argparse
import ast
import io
import os
import re
import sys

ENGINE_PREFIX = "ast:"
#: 本脚本实现了哪些规则；名册写了别的规则名 ⇒ 硬失败（不许静默"没有键要管"）
KNOWN_RULES = ("bare_environ_read",)
COLUMNS = ("族", "键", "状态", "判定模式", "扫描范围", "允许上限", "口径备注")
#: 只认这两枚宿主名（模块级 `environ` 来自 `from os import environ`）
ENV_HOSTS = ("os.environ", "environ")
JUNK_DIRS = {"__pycache__", ".venv", ".pytest_cache", ".ruff_cache", ".git", "node_modules"}
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _die(msg, code=2):
    """环境/参数错误：点名问题并退 2。与"有键超标"（1）严格分开。"""
    print("check-path-env-reads: %s" % msg, file=sys.stderr)
    sys.exit(code)


def _read_text(path):
    """整份读出文本；读不了/解不了都当门禁自己的环境错误（退 2），不许冒 1 被读成"有键超标"。"""
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as exc:
        _die("读不了这个文件: %s（%s）" % (path, exc))


def _rows(roster):
    """读名册数据行。空行与 `#` 行（注释、表头）跳过；列数不对 ⇒ 硬失败。"""
    out = []
    for num, raw in enumerate(_read_text(roster).splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        cells = raw.split("\t")
        if len(cells) != len(COLUMNS):
            _die("名册 %s 第 %d 行列数 %d 应为 %d" % (roster, num, len(cells), len(COLUMNS)))
        out.append(dict(zip(COLUMNS, cells, strict=True)))
    if not out:
        _die("名册 %s 没有任何数据行" % roster)
    return out


def _routed(rows):
    """本门负责的登记行：`ast:` 开头的判定模式，且状态 active。"""
    routed = [r for r in rows if r["判定模式"].startswith(ENGINE_PREFIX)]
    if not routed:
        _die("名册里没有 %s 路由行——本门无事可做（该路由行不见了或标记被改了）"
             % ENGINE_PREFIX)
    for r in routed:
        if r["状态"] != "active":
            _die("路由键 '%s' 的状态是 '%s'，active 才计数" % (r["键"], r["状态"]))
        spec = r["判定模式"][len(ENGINE_PREFIX):].split(":", 1)
        rule = spec[0]
        keys = tuple(k.strip() for k in (spec[1].split(",") if len(spec) > 1 else []) if k.strip())
        if rule not in KNOWN_RULES:
            _die("路由键 '%s' 的规则名 '%s' 本脚本未实现（已实现：%s）"
                 % (r["键"], rule, "、".join(KNOWN_RULES)))
        if not keys:
            _die("路由键 '%s' 的判定模式没有键集" % r["键"])
        try:
            cap = int(r["允许上限"].strip())
        except ValueError:
            _die("路由键 '%s' 的允许上限不是整数: '%s'" % (r["键"], r["允许上限"]))
        if cap < 0:
            _die("路由键 '%s' 的允许上限是负数: %d" % (r["键"], cap))
        if not r["扫描范围"].strip():
            _die("路由键 '%s' 没有扫描范围" % r["键"])
        r["_rule"], r["_keys"], r["_cap"] = rule, keys, cap
    return routed


def _py_files(root, entry, key):
    """一个扫描范围条目下的 .py 相对路径（POSIX 分隔）。0 个文件 ⇒ 硬失败。"""
    target = os.path.join(root, entry)
    if not os.path.exists(target):
        _die("键 '%s' 的扫描范围 '%s' 在 --root=%s 下不存在" % (key, entry, root))
    found = []
    if os.path.isfile(target):
        if entry.endswith(".py"):
            found.append(entry)
    else:
        for cur, dnames, fnames in os.walk(target):
            dnames[:] = [d for d in dnames if d not in JUNK_DIRS]
            for fn in fnames:
                if fn.endswith(".py"):
                    rel = os.path.relpath(os.path.join(cur, fn), root)
                    found.append(rel.replace(os.sep, "/"))
    if not found:
        _die("键 '%s' 的扫描范围 '%s' 贡献 0 个 .py——键会静默变成废键" % (key, entry))
    return sorted(found)


def _dotted(node):
    """把 Name/Attribute 链拉成点号串（`os.environ`）；拉不出来返回 None。"""
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    return ".".join(reversed(parts))


def _literal_key(node, keys):
    """节点是字面字符串常量且在册 ⇒ 返回它，否则 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in keys:
        return node.value
    return None


def _call_key(node, keys):
    """这个 Call 是不是读法之一（`environ.get` / `[os.]getenv`）、首参在册 ⇒ 返回键名。"""
    func = node.func
    form = False
    if isinstance(func, ast.Attribute):
        host = _dotted(func.value)
        form = ((func.attr == "get" and host in ENV_HOSTS)
                or (func.attr == "getenv" and host == "os"))
    elif isinstance(func, ast.Name):
        form = func.id == "getenv"
    if not form or not node.args:
        return None
    return _literal_key(node.args[0], keys)


def _code_hits(tree, keys):
    """AST 节点里的裸读位置：返回 [(行号, 键名)]。写位置与 Del 不计。"""
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            key = _call_key(node, keys)
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) \
                and _dotted(node.value) in ENV_HOSTS:
            key = _literal_key(node.slice, keys)
        else:
            key = None
        if key:
            hits.append((node.lineno, key))
    return hits


def _text_re(keys):
    """字面量文本的判定式：三形读法 + 在册键名。由名册给的键集现拼，脚本不含键名。"""
    alt = "|".join(re.escape(k) for k in sorted(keys))
    return re.compile(r"(?:os\.)?(?:environ\.get\(|getenv\(|environ\[)\s*[\"'](%s)[\"']" % alt)


def _string_hits(tree, pattern):
    """docstring 与字符串字面量里抄写的裸读：返回 [(行号, 键名)]。"""
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            hits.extend((node.lineno, m) for m in pattern.findall(node.value))
    return hits


def main(argv=None):
    ap = argparse.ArgumentParser(prog="scripts/check-path-env-reads.py",
                                 description="在册部署键的裸 os.environ 读取点上限门（AST 判定）")
    ap.add_argument("--root", default=REPO_ROOT)
    ap.add_argument("--roster", default=os.path.join(REPO_ROOT, "scripts/gate/shared-facts.tsv"))
    ap.add_argument("--min-files", type=int, default=50)
    args = ap.parse_args(argv)

    if not os.path.isdir(args.root):
        _die("根目录不存在: %s" % args.root)
    if not os.path.isfile(args.roster):
        _die("名册不存在: %s（门禁必须自包含，CI runner 上没有仓外 census）" % args.roster)
    routed = _routed(_rows(args.roster))

    # 先把文件名册定下来，再判定：扫描面塌了必须先响，不许先打印一句"ok"。
    for row in routed:
        files = []
        for entry in row["扫描范围"].split():
            files.extend(_py_files(args.root, entry, row["键"]))
        row["_files"] = sorted(set(files))
    scanned = set().union(*(set(row["_files"]) for row in routed))
    if len(scanned) < args.min_files:
        _die("只扫到 %d 个文件（< --min-files %d）——拒绝零命中即通过"
             % (len(scanned), args.min_files))

    print("AST路由: %d 枚登记行由本门判定" % len(routed))
    viol = 0
    for row in routed:
        pattern = _text_re(row["_keys"])
        hits = []
        for rel in row["_files"]:
            src = _read_text(os.path.join(args.root, rel.replace("/", os.sep)))
            try:
                tree = ast.parse(src)
            except (SyntaxError, ValueError) as exc:
                # 解析不了的文件里可能正藏着绕过点：跳过它继续报绿就是放水 ⇒ 硬失败。
                # ValueError 是 ast.parse 对含 NUL 字节的源报的错（不是 SyntaxError）。
                _die("被扫文件解析不了: %s: %s" % (rel, exc))
            for line, key in sorted(_code_hits(tree, row["_keys"])
                                    + _string_hits(tree, pattern)):
                hits.append((rel, line, key))
        if len(hits) > row["_cap"]:
            viol = 1
            print("超标: %s / %s —— 命中 %d/%d（允许上限 %d，超出 %d 处；以下逐条点名）"
                  % (row["族"], row["键"], len(hits), row["_cap"],
                     row["_cap"], len(hits) - row["_cap"]))
        else:
            print("ok: %s / %s —— 命中 %d/%d" % (row["族"], row["键"], len(hits), row["_cap"]))
            if len(hits) < row["_cap"]:
                print("降档: 当前数 %d 低于允许上限 %d——同批把名册上限降到 %d，"
                      "并从口径备注的白名单里删掉修掉的那一处" % (len(hits), row["_cap"], len(hits)))
        for rel, line, key in hits:
            print("    %s:%d %s" % (rel, line, key))

    print("扫描 %d 个文件" % len(scanned))
    if viol:
        sys.exit(1)
    print("path-env-reads ok: 在册部署键的裸 os.environ 读取点都在允许上限内")
    sys.exit(0)


if __name__ == "__main__":
    main()
