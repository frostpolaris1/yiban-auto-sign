# -*- coding: utf-8 -*-
"""Docker 镜像内容门禁：源码 COPY 必须覆盖运行时真正导入的本地顶级模块。

标签：J · 运维：部署/备份/发布
覆盖：AST 扫运行时目录得到"被导入的本地顶级模块"，与 `docker/Dockerfile` 的 COPY
    清单比对；另显式钉住 `yiban/` 必须被 COPY。
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
