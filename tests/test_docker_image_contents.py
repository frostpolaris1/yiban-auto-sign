# -*- coding: utf-8 -*-
"""Docker 镜像内容门禁：源码 COPY 必须覆盖运行时真正导入的本地顶级模块。

标签：J · 运维：部署/备份/发布
覆盖：AST 扫运行时目录得到"被导入的本地顶级模块"，与 `docker/Dockerfile` 的 COPY
    清单比对；另显式钉住 `yiban/` 必须被 COPY；根级 .md 必须是显式白名单（M84）——
    运行时读取的合规文档/更新日志必须在，本地运维文档不得因通配进镜像。
对应实现：`docker/Dockerfile`（COPY 清单）；扫描对象是 `web/`、`scripts/`、`yiban/`、
    `docker/` 四个运行时目录。
关键断言：漏 COPY 一个被导入的本地包就报红——镜像一旦构建，容器会在 **import 阶段
    直接崩**，而本地跑测试、裸机部署都发现不了（它们的 sys.path 里本来就有仓库根）。
    另钉命名卷挂点（`ba-p13-03`）：`docker-compose.yml` 顶层 `volumes:` 声明的**每一枚**
    命名卷挂点，都必须在 `docker/entrypoint.sh` 里准备到服务账号可写（mkdir + chown +
    chmod），准备失败必须响亮；README 必须教到"谁准备属主 + 用什么命令验证"。
依赖：纯静态——AST 解析源码 + 正则读 Dockerfile/compose/入口脚本文本；
    **不需要 docker CLI、不构建镜像**。

背景：`yiban/` 被 `scripts/signin.py`（`yiban.status`）与 `web/app.py`
（`yiban.attempt.jobs`）导入，而 Dockerfile 一度只 COPY 了 `web/` 与 `scripts/`。
"""
import ast
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERFILE = os.path.join(BASE, "docker", "Dockerfile")
COMPOSE = os.path.join(BASE, "docker-compose.yml")
ENTRYPOINT = os.path.join(BASE, "docker", "entrypoint.sh")
README = os.path.join(BASE, "README.md")

# 会被打进镜像的源码目录（与 Dockerfile 的 COPY 对应）
RUNTIME_DIRS = ("web", "scripts", "yiban", "docker")


def _iter_py(root):
    for dirpath, dirnames, filenames in os.walk(os.path.join(BASE, root)):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "vendor")]  #vendor/ 是随镜像一起拷的第三方码，不算本地顶级模块
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _top_level_imports():
    """运行时目录里 import 的顶级模块名（含 `from x.y import` 的 x）。"""
    names = set()
    for root in RUNTIME_DIRS:
        for path in _iter_py(root):
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 0 and node.module:
                        names.add(node.module.split(".")[0])
                    elif node.level:  # 相对导入：所属包的顶级名
                        rel = os.path.relpath(path, BASE).replace(os.sep, "/")
                        names.add(rel.split("/")[0])
    return names


def _local_module_names():
    """仓库里真实存在的本地顶级模块（目录或 .py），与第三方/标准库区分开。"""
    local = set()
    for name in os.listdir(BASE):
        full = os.path.join(BASE, name)
        if os.path.isdir(full) and os.path.exists(os.path.join(full, "__init__.py")):
            local.add(name)
        elif name.endswith(".py"):
            local.add(name[:-3])
    # scripts/ 与 web/ 之下是模块（sys.path 注入后可直接 import，如 db / account_crypto）
    for d in ("scripts", "web"):  #这两个目录靠 sys.path 注入直跑，其 .py 文件名也算本地模块名
        for name in os.listdir(os.path.join(BASE, d)):
            if name.endswith(".py"):
                local.add(name[:-3])
    return local


def _named_copies():
    """Dockerfile 中 `COPY <src> ...` 的源路径列表。"""
    with open(DOCKERFILE, encoding="utf-8") as f:
        content = f.read()
    copies = []
    for line in content.splitlines():
        m = re.match(r"\s*COPY\s+(.*)", line, re.I)
        if m:
            copies.append(m.group(1).strip())
    return content, copies


class DockerImageContentsTest(unittest.TestCase):
    def test_imported_local_packages_are_copied(self):
        """被导入的本地顶级包必须在 Dockerfile 里 COPY（漏了容器起不来）。"""
        local = _local_module_names()
        needed = {n for n in _top_level_imports() if n in local and os.path.isdir(
            os.path.join(BASE, n))}
        _, copies = _named_copies()
        missing = []
        for pkg in sorted(needed):
            # 目录包：`COPY <pkg>/ ...` 或把整个仓库根拷进去都算覆盖
            if any(c.startswith(pkg + "/") or c.startswith("./" + pkg + "/")  #只认 `COPY <pkg>/` 这一种写法：`COPY . ...` 不满足本判据，别指望它兜底
                   for c in copies):
                continue
            missing.append(pkg)
        self.assertEqual(
            missing, [],
            "Dockerfile 未 COPY 这些被导入的本地包（容器会在 import 阶段崩）："
            f"{missing}；当前 COPY: {copies}",
        )


def _md_copy_sources():
    """Dockerfile 中 COPY 的根级 .md 源文件名集合（保持顺序无关）。"""
    content, copies = _named_copies()
    names = set()
    for c in copies:
        parts = c.split()
        if len(parts) < 2:
            continue
        for src in parts[:-1]:
            if src.endswith(".md"):
                names.add(os.path.basename(src))
    return content, names


class DockerImageMdWhitelistTest(unittest.TestCase):
    """M84：根级 .md 必须显式白名单 COPY，不得再用 `COPY ./*.md ./`。

    运行时真正读取的 .md（web/render.py 的合规页、web/routes/settings_api.py 的更新
    日志接口）必须在白名单里；同时本地运维文档（AGENTS.md / PROMPT*.md / HANDOFF*.md /
    MEMORY.md / MAINTENANCE.md 等）不得因通配而进镜像——它们含服务器 IP 与部署细节。
    """

    # 运行时读取方 → 文档（web/render.py 的 _DOC_FILES 与 api_changelog 的文件名）
    RUNTIME_DOCS = ("USER_AGREEMENT.md", "PRIVACY_POLICY.md", "CHANGELOG.md")
    LOCAL_OPS_DOCS = ("AGENTS.md", "PROMPT.md", "HANDOFF_2026.md", "MEMORY.md",
                      "MAINTENANCE.md", "PROXY_DEPLOY_GUIDE.md")

    def test_no_md_wildcard_copy_left(self):
        content, _ = _named_copies()
        offenders = [ln.strip() for ln in content.splitlines()
                     if re.match(r"\s*COPY\s", ln, re.I) and "*.md" in ln]
        self.assertEqual([], offenders,
                         "根级 .md 不得用通配 COPY——哪些文档进镜像会取决于两份 ignore "
                         "清单的差集：%s" % offenders)

    def test_whitelist_covers_docs_read_at_runtime(self):
        _content, names = _md_copy_sources()
        for doc in self.RUNTIME_DOCS:
            self.assertIn(doc, names,
                          "运行时读取的 %s 必须进镜像（否则合规页/更新日志接口降级）"
                          "；当前白名单：%s" % (doc, sorted(names)))
        self.assertIn("README.md", names, "自述文档应在白名单内；当前：%s" % sorted(names))

    def test_local_ops_docs_are_not_in_whitelist(self):
        """活体反例：本地运维文档一旦落到仓库根，也不得被白名单带进镜像。"""
        _content, names = _md_copy_sources()
        for doc in self.LOCAL_OPS_DOCS:
            self.assertNotIn(doc, names,
                             "本地运维文档 %s 不得进镜像（含服务器 IP/部署细节）" % doc)


# ---- 命名卷挂点准备（yiban-auto-sign-ba-p13-03）------------------------------
#
# 为什么按 compose 名册驱动、不硬编码 `/backups`：下一个加命名卷的人若不准备属主，会以
# 完全相同的形状复现这个缺陷。名册驱动让守卫在他加卷的那一刻变红（AGENTS.md §14/§15）。
#
# 缺陷形状：Docker 首次创建命名卷时挂点是 root:root 0755；写备份的进程却是 supervisord 按
# `user=yiban` 降级后的 uid 10001（compose 的 CHOWN/DAC_OVERRIDE 只到 root 阶段为止）。
# 入口漏准备 ⇒ 备份 100% EACCES，默认部署只能靠人工 grep 日志才发现。


def _read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _abs_tokens(line):
    """一行里出现的绝对路径参数：剥掉 `-R` 这类选项与 `2>/dev/null` 这类重定向。"""
    toks = []
    for raw in line.split():
        if raw.startswith("-") or any(c in raw for c in "><&"):
            continue
        if raw.startswith("/"):
            toks.append(raw.strip("\"'"))
    return toks


def _verb_targets(text, verb):
    """脚本里每一行 `verb …` 的绝对路径参数全集（mkdir 一行带多个，全收）。"""
    out = set()
    for line in text.splitlines():
        if re.match(r"\s*(if\s+!\s*)?" + verb + r"\b", line):
            out.update(_abs_tokens(line))
    return out


def _declared_named_volumes():
    """compose 顶层 `volumes:` 块声明的命名卷名（bind 挂载不在内）。"""
    names = set()
    lines = _read_text(COMPOSE).splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith("volumes:")]
    if not starts:
        return names
    for ln in lines[starts[0] + 1:]:
        if ln and not ln[:1].isspace():      # 回到顶层键，块结束
            break
        stripped = ln.strip()
        if stripped and not stripped.startswith("#"):
            names.add(stripped.split(":")[0].strip())
    return names


def _named_volume_mounts():
    """[(卷名, 容器内挂点)]——只取源为命名卷的挂载行。

    `./data:/data` 这类 bind 挂载的属主由宿主目录决定（部署者的机器）；命名卷的属主由
    Docker 决定（首次创建 root:root 0755），只有后者需要容器入口准备。
    """
    declared = _declared_named_volumes()
    pairs = []
    for line in _read_text(COMPOSE).splitlines():
        m = re.match(r"\s*-\s+([A-Za-z0-9][A-Za-z0-9_.-]*):(/[^\s:]+)(?::[^\s]*)?$", line)
        if m and m.group(1) in declared:
            pairs.append((m.group(1), m.group(2)))
    return pairs


def _branch_is_loud(lines, i):
    """`if ! …; then` 之后的分支里是否真有一行把警告写到 stderr。"""
    for nxt in lines[i + 1:i + 4]:
        if re.match(r"\s*fi\s*$", nxt):
            return False
        if "echo" in nxt and ">&2" in nxt:
            return True
    return False


def _prep_ops(text, verb):
    """[{path, recursive, mode, loud}]：入口脚本里 `verb` 作用到的绝对路径。"""
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        m = re.match(r"\s*(if\s+!\s*)?" + verb + r"\b(.*)$", line)
        if not m:
            continue
        targets = _abs_tokens(m.group(2))
        if not targets:
            continue
        mode = re.search(r"\s([0-7]{3,4})\s", m.group(2))
        out.append({"path": targets[-1],
                    "recursive": bool(re.search(r"(^|\s)-[A-Za-z]*R(\s|$)", m.group(2))),
                    "mode": int(mode.group(1), 8) if mode else None,
                    "loud": bool(m.group(1)) and _branch_is_loud(lines, i)})
    return out


def _covers(ops, path, exact_only=False):
    """`ops` 里是否有一条作用到这个挂点本身（递归祖先也算，除非要求精确）。"""
    for o in ops:
        if o["path"] == path:
            return True
        if not exact_only and o.get("recursive") and \
                path.startswith(o["path"].rstrip("/") + "/"):
            return True
    return False


def _mkdir_covers(paths, path):
    """`mkdir -p` 是否让这个挂点存在：它自己、它的祖先、或它的某个孩子都算。"""
    for p in paths:
        if p == path or path.startswith(p.rstrip("/") + "/") or p.startswith(path.rstrip("/") + "/"):
            return True
    return False


def _readme_backup_section(text):
    """README 里「容器形态现在自带定时备份」那一节（到下一个标题为止）。"""
    m = re.search(r"^#### 容器形态现在自带定时备份.*?(?=^#{3,4} )", text, re.M | re.S)
    return m.group(0) if m else ""


class DockerNamedVolumePrepTest(unittest.TestCase):
    """compose 声明的命名卷，容器入口必须把挂点准备到服务账号可写。"""

    def test_compose_named_volume_census_is_readable(self):
        """名册解析自己先可读——否则下面两条会因为"什么都没解析到"而假绿。"""
        self.assertIn("yiban-backups", _declared_named_volumes(),
                      "compose 顶层 volumes: 里读不到 yiban-backups（解析式失效或卷被改名）")
        self.assertTrue(_named_volume_mounts(),
                        "compose 里读不到任何 `命名卷:容器路径` 挂载行，本门失去意义")

    def test_named_volume_mounts_are_prepared_in_entrypoint(self):
        """每枚命名卷挂点都要 mkdir + chown 给服务账号；chmod 不得留组/其他可写位。"""
        text = _read_text(ENTRYPOINT)
        mkdirs = _verb_targets(text, "mkdir")
        chowns, chmods = _prep_ops(text, "chown"), _prep_ops(text, "chmod")
        missing = []
        for name, path in _named_volume_mounts():
            if not _mkdir_covers(mkdirs, path):
                missing.append("%s→%s 未 mkdir" % (name, path))
            if not _covers(chowns, path):
                missing.append("%s→%s 未 chown（写它的进程是 uid 10001，不是 root）" % (name, path))
            if not _covers(chmods, path, exact_only=True):
                missing.append("%s→%s 未对挂点本身 chmod" % (name, path))
            else:
                mode = [o["mode"] for o in chmods if o["path"] == path][-1]
                if mode is not None and mode & 0o022:
                    missing.append("%s→%s 权限位 %03o 组/其他可写" % (name, path, mode))
        self.assertEqual([], missing,
                         "命名卷挂点没准备好，非 root 的业务子进程写不进去：%s" % missing)

    def test_volume_prep_failure_is_loud_not_swallowed(self):
        """属主/权限位准备失败必须走响亮分支（echo 到 stderr），不许 `|| true` 吞掉。"""
        text = _read_text(ENTRYPOINT)
        swallowed = [ln.strip() for ln in text.splitlines()
                     if re.search(r"\b(chown|chmod)\b.*\|\|\s*true", ln)]
        self.assertEqual([], swallowed,
                         "卷准备被 || true 吞掉，受限卷会静默不可写：%s" % swallowed)
        quiet = ["%s %s" % (verb, o["path"]) for verb in ("chown", "chmod")
                 for o in _prep_ops(text, verb) if not o["loud"]]
        self.assertEqual([], quiet,
                         "卷准备不在 `if ! …; then echo … >&2; fi` 分支里，失败即静默：%s" % quiet)


class DockerVolumePrepDocTest(unittest.TestCase):
    """README 必须教到"谁准备属主 + 怎么验证"（AGENTS.md §13：改行为不改教法是第二处缺陷）。"""

    def test_readme_teaches_owner_prep_and_verification(self):
        text = _read_text(README)
        self.assertIn("#### 容器形态现在自带定时备份", text,
                      "README 的容器备份小节标题被改名，本门的段抽取失效")
        seg = _readme_backup_section(text)
        self.assertIn("/backups", seg, "容器备份小节不再提落点 /backups？")
        self.assertRegex(seg, r"entrypoint\.sh",
                         "必须写明命名卷属主由容器入口准备（而不是让部署者手动 chown）")
        self.assertRegex(seg, r"属主|归属",
                         "必须点明准备的是挂点属主，否则部署者不知道这一格谁负责")
        self.assertRegex(seg, r"ls -ld /backups",
                         "必须给一条可复制的验证命令，让部署者自己确认属主准备真的生效")


if __name__ == "__main__":
    unittest.main(verbosity=2)
