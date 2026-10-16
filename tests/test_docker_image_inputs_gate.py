# -*- coding: utf-8 -*-
"""镜像内容的**第二个输入**门：`.dockerignore` 必须和 `Dockerfile` 一起被读（140 §六-3）。

标签：J · 运维：部署/备份/发布

**本门挡的缺陷形状**：镜像内容由**两个**输入决定——`Dockerfile` 的 COPY 清单，与
`.dockerignore` 的排除清单。既有门 `tests/test_docker_image_contents.py` 只读
`Dockerfile`（140 §五 S4 现量确认："该运行期结果由两个输入决定、门只看其中一个"）。
⇒ 把 `.env` 从 `.dockerignore` 里删掉、或给 COPY 加一条被忽略的源，**没有任何东西会红**，
而后果是密钥进镜像 / 构建直接失败。

**本门钉的性质（三条）**：
  ① **COPY 的源必须活在构建上下文里**：被 `.dockerignore` 命中的源，构建时会报
     "not found"（或静默不同）——两个输入必须在**同一个判据**里对上。
  ② **敏感形态必须被忽略清单覆盖**：`.env` / `accounts.json` / `users.json` / `*.db` /
     `*.log` / `*.pyc` / `__pycache__` 这些根级路径必须命中 `.dockerignore`。
  ③ **合规文档必须留在上下文**：`USER_AGREEMENT.md` / `PRIVACY_POLICY.md` /
     `CHANGELOG.md` / `README.md` 都在 COPY 清单里，而忽略清单的头注释明写"不能整段忽略
     `*.md`"——故裸 `*.md` 模式一出现即红。

**匹配语义（口径写清，免得下一个人以为它比实际更准）**：`.dockerignore` 的每条模式与
`COPY` 源路径的**全路径**及其**每一层祖先前缀**比对（`fnmatch` 语义，`**` 跨层）。
这与 moby 的 `patternmatcher.Matches` 同形（先比文件本身，再比它的父目录链）。
它**不**实现 `.dockerignore` 的全部细节（如 `!` 取反、尾随 `/` 的目录语义、大小写）；
本仓现用的清单里没有这些写法——真要用到时，本门必须先扩，不许当它已经支持。

对应实现：`docker/Dockerfile`（COPY 清单）、`.dockerignore`（排除清单）。
关键断言：钉"两个输入在同一判据里对上""敏感形态被覆盖""合规文档不被误伤"；
    不钉任何构建结果。
依赖：纯静态——读两个文本文件 ＋ `git ls-files` 判入库；**不需要 docker CLI、不构建镜像**。
    整文件在本机执行，无 skip。
"""
import fnmatch
import os
import re
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERFILE = os.path.join(BASE, "docker", "Dockerfile")
DOCKERIGNORE = os.path.join(BASE, ".dockerignore")

#: 必须被忽略清单覆盖的根级敏感形态（键 = 探针路径，值 = 为什么）。
SENSITIVE_PROBES = {
    ".env": "账号密钥与运行期配置",
    ".env.local": "`.env.*` 一族的实例",
    "accounts.json": "账号库（明文敏感）",
    "users.json": "用户库",
    "yiban.db": "台账库",
    "sign.log": "日志",
    "mod.pyc": "编译产物",
    "__pycache__": "编译产物目录",
}

#: 必须在上下文里留存的合规文档（`Dockerfile` COPY 它们）。
COMPLIANCE_DOCS = ("USER_AGREEMENT.md", "PRIVACY_POLICY.md", "CHANGELOG.md", "README.md")


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def dockerignore_patterns(text=None):
    """解析 `.dockerignore`：去注释与空行，去前导 `./` 与尾随 `/`。"""
    text = read_text(DOCKERIGNORE) if text is None else text
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 只去**字面**的 `./` 前缀：`lstrip("./")` 会把 `.env` 剥成 `env`（本仓实踩）
        if line.startswith("./"):
            line = line[2:]
        line = line.rstrip("/")
        if line:
            out.append(line)
    return out


def _glob_re(pattern):
    """把一条 `.dockerignore` 模式翻成正则（`**` 跨层，`*` 不跨层）。"""
    parts = []
    for seg in pattern.split("/"):
        if seg == "**":
            parts.append(".*")
        else:
            parts.append(fnmatch.translate(seg)[4:-3])   # 去掉 `(?s:` 与 `)\\Z`
    return re.compile(r"^" + "/".join(parts) + r"\Z", re.S)


def is_ignored(path, patterns):
    """`path`（相对仓库根的 POSIX 路径）是否被忽略清单命中。

    比对全路径与它的**每一层祖先前缀**（与 moby `patternmatcher.Matches` 同形）。
    """
    path = path.strip("/").replace(os.sep, "/")
    if not path:
        return False
    prefixes = []
    segs = path.split("/")
    for i in range(1, len(segs) + 1):
        prefixes.append("/".join(segs[:i]))
    for pat in patterns:
        rx = _glob_re(pat)
        if any(rx.match(p) for p in prefixes):
            return True
    return False


def copy_sources(text=None):
    """`Dockerfile` 的 COPY 源路径列表（跳过 `--from=` 这类旗标与目标位）。"""
    text = read_text(DOCKERFILE) if text is None else text
    out = []
    for line in text.splitlines():
        m = re.match(r"\s*COPY\s+(.*)", line, re.I)
        if not m:
            continue
        tokens = [t for t in m.group(1).split() if not t.startswith("--")]
        if len(tokens) < 2:
            continue
        out.extend(tokens[:-1])          # 末位是目标
    return out


def input_violations(patterns, sources, docs=COMPLIANCE_DOCS, probes=SENSITIVE_PROBES):
    """给定"忽略清单 ＋ COPY 源"，返回三类违规。**判据本体**，可被合成输入喂进。"""
    ignored_sources = sorted(s for s in sources if is_ignored(s, patterns))
    uncovered = sorted(p for p in probes if not is_ignored(p, patterns))
    ignored_docs = sorted(d for d in docs if is_ignored(d, patterns))
    bare_md = sorted(p for p in patterns if p in ("*.md", "**.md"))
    return {"ignored_sources": ignored_sources, "uncovered_sensitive": uncovered,
            "ignored_docs": ignored_docs, "bare_md_pattern": bare_md}


class DockerImageInputsTest(unittest.TestCase):
    """`Dockerfile` 与 `.dockerignore` 必须在同一个判据里对上。"""

    def test_两个输入都在库内(self):
        """前置就绪：两个输入都存在且被 git 跟踪。

        少了它，`.dockerignore` 被删或被 gitignore 掉时，本门会拿一份空清单当"没问题"。
        """
        for path in (DOCKERFILE, DOCKERIGNORE):
            self.assertTrue(os.path.isfile(path), "缺输入文件：%s" % path)
        rel = [os.path.relpath(DOCKERIGNORE, BASE).replace(os.sep, "/"),
               os.path.relpath(DOCKERFILE, BASE).replace(os.sep, "/")]
        out = subprocess.run(["git", "ls-files", "--", *rel], cwd=BASE,
                             capture_output=True, text=True)
        self.assertEqual(
            out.returncode, 0,
            "前置就绪：`git ls-files` 跑不起来（rc=%s，stderr=%r）——本判据无法成立，"
            "按红处置而不是放过" % (out.returncode, (out.stderr or "")[-300:]))
        tracked = {line.strip() for line in (out.stdout or "").splitlines() if line.strip()}
        self.assertEqual(tracked, set(rel),
                         "镜像内容的两个输入都必须入库（否则干净检出里判据失效）：%r"
                         % (sorted(tracked),))
        self.assertGreater(len(dockerignore_patterns()), 0, "忽略清单解析出 0 条：判据空转")
        self.assertGreater(len(copy_sources()), 0, "COPY 源解析出 0 条：判据空转")

    def test_COPY源都活在构建上下文里(self):
        viol = input_violations(dockerignore_patterns(), copy_sources())
        self.assertEqual(
            viol["ignored_sources"], [],
            "这些 COPY 源被 `.dockerignore` 命中 ⇒ 构建时找不到（或与本地不同）：%s。"
            "镜像内容由两个输入决定，改任一个都要在这里对上。" % viol["ignored_sources"])

    def test_敏感形态被忽略清单覆盖(self):
        viol = input_violations(dockerignore_patterns(), copy_sources())
        self.assertEqual(
            viol["uncovered_sensitive"], [],
            "这些敏感形态没有被 `.dockerignore` 覆盖 ⇒ 它们会随构建上下文进镜像：%s"
            % viol["uncovered_sensitive"])

    def test_合规文档不被误伤(self):
        viol = input_violations(dockerignore_patterns(), copy_sources())
        self.assertEqual(
            viol["ignored_docs"], [],
            "合规文档被忽略清单命中 ⇒ 容器读不到它（运行期合规面破）：%s"
            % viol["ignored_docs"])
        self.assertEqual(
            viol["bare_md_pattern"], [],
            "忽略清单出现了裸 `*.md`：忽略清单头注释明写'不能整段忽略 *.md'"
            "（合规文档必须进镜像）。实际：%s" % viol["bare_md_pattern"])

    def test_检查器有牙(self):
        """合成输入：把 COPY 的源写进忽略清单、把敏感形态从清单里删掉 ⇒ 三类都报。"""
        sources = copy_sources()
        self.assertTrue(sources, "前置：必须解析出 COPY 源，否则本用例空转")
        victim = sources[0]
        viol = input_violations([victim, "docs"], sources,
                                docs=("USER_AGREEMENT.md",),
                                probes=SENSITIVE_PROBES)
        self.assertIn(victim, viol["ignored_sources"],
                      "检查器对'COPY 源被忽略'无牙")
        self.assertIn(".env", viol["uncovered_sensitive"],
                      "检查器对'敏感形态未被覆盖'无牙")
        self.assertEqual(viol["ignored_docs"], [],
                         "合成清单不该误伤未被命中的文档")
        viol2 = input_violations(["*.md"], sources, docs=("USER_AGREEMENT.md",),
                                 probes={".env": "x"})
        self.assertIn("USER_AGREEMENT.md", viol2["ignored_docs"])
        self.assertIn("*.md", viol2["bare_md_pattern"],
                      "检查器对'裸 *.md 模式'无牙")


if __name__ == "__main__":
    unittest.main(verbosity=2)
