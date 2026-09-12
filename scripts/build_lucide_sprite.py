#!/usr/bin/env python3
"""构建 web/static/vendor/lucide/_sprite.svg（Lucide 图标 SVG symbol 精灵图）。

来源：lucide-icons/lucide 指定 tag 的 tarball（icons/*.svg），无需 npm。
详见 web/static/vendor/lucide/MANIFEST.md「重建 / 升级命令」。

用法：
    python scripts/build_lucide_sprite.py <lucide-repo-dir> [输出路径]

    <lucide-repo-dir> 例如解压后的 D:/tmp/lucide-1.45.0（含 icons/ 子目录）。

子集（70 个，按语义分组；均为 Lucide 1.45.0 的规范名，非已废弃别名）：
  导航 / 动作 / 状态 / 数据，见 ICONS。
"""
import gzip
import os
import re
import sys

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


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    repo_dir = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "web", "static", "vendor", "lucide", "_sprite.svg",
    )
    sprite = build(repo_dir)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(sprite)
    raw = sprite.encode("utf-8")
    gz = gzip.compress(raw, 9, mtime=0)
    print(f"wrote {out}")
    print(f"icons={len(ICONS)} raw={len(raw)}B gzip={len(gz)}B")


if __name__ == "__main__":
    main()
