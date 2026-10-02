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
依赖：纯静态——AST 解析源码 + 正则读 Dockerfile 文本；**不需要 docker CLI、不构建镜像**。

背景：`yiban/` 被 `scripts/signin.py`（`yiban.status`）与 `web/app.py`
（`yiban.attempt.jobs`）导入，而 Dockerfile 一度只 COPY 了 `web/` 与 `scripts/`。
"""
import ast
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERFILE = os.path.join(BASE, "docker", "Dockerfile")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
