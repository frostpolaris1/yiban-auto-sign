#!/usr/bin/env python3
"""构建 web/static/vendor/lucide/_sprite.svg（Lucide 图标 SVG symbol 精灵图）。

来源：lucide-icons/lucide 指定 tag 的 tarball（icons/*.svg），无需 npm。
详见 web/static/vendor/lucide/MANIFEST.md「重建 / 升级命令」。

用法：
    python scripts/build_lucide_sprite.py <lucide-repo-dir> [输出路径] [--check]

    <lucide-repo-dir> 例如解压后的 D:/tmp/lucide-1.45.0（含 icons/ 子目录）。
    `--check` 不写盘：生成物与既有文件逐字节一致时退 0，有差异或缺文件时退 1
    （供 CI / 验证脚本调用）。来源目录的 `package.json` 版本必须等于
    `LUCIDE_VERSION`，否则退 2（版本漂移时头部出处记录会与内容分叉）。

子集（70 个，按语义分组；均为 Lucide 1.45.0 的规范名，非已废弃别名）：
  导航 / 动作 / 状态 / 数据，见 ICONS。
"""
import contextlib
import gzip
import json
import os
import re
import subprocess
import sys
import tempfile

LUCIDE_VERSION = "1.45.0"
LUCIDE_COMMIT = "b998e2892b90b88004d62da2d0b64dab9959a520"

ICONS = [
    # 导航
    "gauge", "layout-dashboard", "users", "user", "list", "settings", "activity",
    "shield", "database", "server", "clock", "calendar", "mail", "bell",
    # 动作
    "plus", "pencil", "trash", "rotate-cw", "refresh-cw", "rotate-ccw", "download",
    "upload", "search", "funnel", "play", "pause", "circle-pause", "circle-stop",
    "power", "log-out", "copy", "external-link", "ellipsis-vertical", "ellipsis",
    "x", "check", "arrow-up", "arrow-down", "chevron-up", "chevron-down",
    "chevron-left", "chevron-right", "menu", "sun", "moon", "eye", "eye-off",
    "key", "lock", "lock-open", "link", "send", "save", "ban", "circle-minus",
    # 状态
    "circle-check", "circle-x", "triangle-alert", "circle-alert", "info",
    "circle-question-mark", "loader", "circle-slash",
    # 数据
    "chart-bar", "chart-pie", "trending-up", "trending-down", "chart-line",
    "table", "file-text",
]

HEADER = (
    "<!--\n"
    "  Lucide icon sprite (subset, {n} icons) - generated, do not edit by hand.\n"
    "  Source: lucide-icons/lucide\n"
    "  Version: {ver}  Commit: {sha}\n"
    "  License: ISC (see LICENSE in this directory; some icons are Feather-derived, MIT).\n"
    "  Build: python scripts/build_lucide_sprite.py <lucide-repo-dir> <output>\n"
    "  Usage: call ui.sprite() once after <body>, then ui.icon('trash', 'w-4 h-4').\n"
    "  Symbols inherit stroke=currentColor from the use context, so icons follow text color (dark mode safe).\n"
    "-->\n"
)

# 匹配 <svg ...> 内部内容；Lucide 图标为纯描边，无硬编码颜色。
_SVG_RE = re.compile(r"<svg\b[^>]*>(.*?)</svg>", re.S)
_TAG_WS_RE = re.compile(r">\s+<")
_ATTR_WS_RE = re.compile(r"\s+")


def load_icon(icons_dir: str, name: str) -> str:
    path = os.path.join(icons_dir, name + ".svg")
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    m = _SVG_RE.search(raw)
    if not m:
        raise ValueError(f"no <svg> in {path}")
    body = m.group(1)
    # 去掉子元素之间/首尾的空白，并把标签内多空白归一化（保留各元素本身）。
    body = _TAG_WS_RE.sub("><", body).strip()
    body = _ATTR_WS_RE.sub(" ", body)
    # 防御：剥离任何硬编码颜色，保证 currentColor 继承（当前上游无此情况）。
    body = re.sub(r'\s+(?:stroke|fill)="#[0-9a-fA-F]{3,8}"', "", body)
    return body


def build(repo_dir: str) -> str:
    icons_dir = os.path.join(repo_dir, "icons")
    if not os.path.isdir(icons_dir):
        sys.exit(f"icons/ not found under {repo_dir}")
    symbols = []
    for name in ICONS:
        body = load_icon(icons_dir, name)
        symbols.append(
            f'<symbol id="i-{name}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{body}</symbol>'
        )
    sprite = (
        '<svg xmlns="http://www.w3.org/2000/svg" style="display:none">\n'
        + HEADER.format(n=len(ICONS), ver=LUCIDE_VERSION, sha=LUCIDE_COMMIT)
        + "\n".join(symbols)
        + "\n</svg>\n"
    )
    return sprite


def _verify_source(repo_dir: str) -> None:
    """校验来源目录与文件头写死的版本/commit 一致；不一致即退 2。

    头部那两行是这份生成物唯一的出处记录。拿错 tag 的目录重跑一次，图标内容换了
    而头部仍自称 LUCIDE_VERSION——出处与内容分叉且无任何提示（工单 ba-p11-12）。
    `package.json` 是 npm tarball 的必备件；缺件同样退 2（无法校验 ≠ 校验通过）。
    """
    manifest = os.path.join(repo_dir, "package.json")
    if not os.path.isfile(manifest):
        print(f"来源目录缺少 package.json，无法校验版本: {repo_dir}", file=sys.stderr)
        sys.exit(2)
    try:
        with open(manifest, encoding="utf-8") as f:
            version = json.load(f).get("version")
    except (OSError, ValueError) as e:
        print(f"读不了来源版本（{manifest}）: {e}", file=sys.stderr)
        sys.exit(2)
    if version != LUCIDE_VERSION:
        print(f"来源版本不匹配：目录是 {version!r}，脚本写死 {LUCIDE_VERSION!r}。"
              "升级请同批改 LUCIDE_VERSION / LUCIDE_COMMIT 并重跑本脚本", file=sys.stderr)
        sys.exit(2)
    # tarball 不是 git 仓库；只有来源恰好是 checkout 时才核 commit。
    git_dir = os.path.join(repo_dir, ".git")
    if os.path.exists(git_dir):
        try:
            r = subprocess.run(["git", "-C", repo_dir, "rev-parse", "HEAD"],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return
        if r.returncode == 0 and r.stdout.strip() != LUCIDE_COMMIT:
            print(f"来源 commit 不匹配：目录是 {r.stdout.strip()}，"
                  f"脚本写死 {LUCIDE_COMMIT}。请换到对应 tag 或同批改常量", file=sys.stderr)
            sys.exit(2)


def main() -> int:
    argv = [a for a in sys.argv[1:] if a != "--check"]
    check = "--check" in sys.argv[1:]
    if not argv:
        sys.exit(__doc__)
    repo_dir = argv[0]
    out = argv[1] if len(argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "web", "static", "vendor", "lucide", "_sprite.svg",
    )
    _verify_source(repo_dir)
    sprite = build(repo_dir)
    if check:
        if not os.path.isfile(out):
            print(f"缺少生成物: {out}", file=sys.stderr)
            return 1
        with open(out, encoding="utf-8") as f:
            current = f.read()
        if current == sprite:
            print(f"无变化 {out}")
            return 0
        print(f"生成物与来源不一致: {out}（重新运行本脚本且不带 --check 以更新）",
              file=sys.stderr)
        return 1
    # tmp + os.replace：写一半失败不会留下半个 sprite（读者始终看到完整文件）。
    fd, tmp = tempfile.mkstemp(prefix=".lucide-sprite-", dir=os.path.dirname(os.path.abspath(out)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(sprite)
        os.replace(tmp, out)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
    raw = sprite.encode("utf-8")
    gz = gzip.compress(raw, 9, mtime=0)
    print(f"wrote {out}")
    print(f"icons={len(ICONS)} raw={len(raw)}B gzip={len(gz)}B")
    return 0


if __name__ == "__main__":
    sys.exit(main())
