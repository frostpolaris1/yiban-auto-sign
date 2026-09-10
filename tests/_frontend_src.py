# -*- coding: utf-8 -*-
"""前端源码聚合读取器（2026-09-10 引入，配合 A1/A3 的前端拆分）。

## 为什么需要它

前端重构把 `index.html` 的内联 CSS/JS 与区块外提成独立文件后，那些**直接读模板文件做静态断言**
的既有测试会以两种方式失效：

1. `assertIn` 类 —— 目标串搬走了，测试**报红**。显式可见，好处理。
2. `assertNotIn` 类 —— 文件里已经没有那段代码了，断言**恒真**。**静默失去覆盖**，这才是危险的。

本模块把"某页的完整前端源码"聚合出来（模板 + 递归 include 的片段 + 该页外链的**自研**静态资源），
让这些契约断言继续覆盖真实源码。后续再拆分文件（A4 拆 JS、V3 增 include）**无需再改测试**。

## 边界（有意为之）

- **不含 `web/static/vendor/`**：那是第三方压缩产物（`tailwind.js` 407 KB、`md-render.js` 等），
  扫描它既慢，又会污染 `count()` / `search()` 类断言的语义（第三方代码里出现同名字符串是巧合，不是契约）。
- 只跟随**同源** `/static/` 引用，且该引用必须对应磁盘上真实存在的文件；推不出路径的（如
  `/favicon.png`、外部 URL）一律跳过。
- 递归有 `seen` 去重与上限，防 include 成环。

## 用法

    from _frontend_src import frontend_source
    src = frontend_source("index.html")      # 或 ("web", "templates", "index.html") 形式的分段
"""
import os
import re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(BASE, "web", "templates")
STATIC_DIR = os.path.join(BASE, "web", "static")

_INCLUDE_RE = re.compile(r'{%-?\s*include\s+"([^"]+)"\s*-?%}')
_ASSET_RE = re.compile(r'(?:src|href)="([^"]*?/static/[^"]*)"')
_STATIC_REL_RE = re.compile(r"/static/(.+?)(?:\?.*)?$")
_MAX_FILES = 64


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _asset_disk_path(url):
    """由模板里的资源 URL 推出磁盘路径；非自研（vendor/）或文件不存在 → None。"""
    m = _STATIC_REL_RE.search(url)
    if not m:
        return None
    rel = m.group(1)
    if rel.startswith("vendor/"):
        return None
    path = os.path.join(STATIC_DIR, rel.replace("/", os.sep))
    return path if os.path.isfile(path) else None


def frontend_source(*parts):
    """返回指定模板的完整前端源码：模板 + include 片段 + 外链自研静态资源，按引用顺序拼接。

    `parts` 可写成 `frontend_source("index.html")`，也可写成
    `frontend_source("web", "templates", "index.html")`（后者会按最后一段相对 templates 解析）。
    """
    if len(parts) == 1:
        start = os.path.join(TEMPLATES_DIR, parts[0])
    else:
        start = os.path.join(BASE, *parts)
        if not os.path.isfile(start):
            start = os.path.join(TEMPLATES_DIR, parts[-1])

    chunks, seen = [], set()

    def walk(path):
        if path in seen or len(seen) >= _MAX_FILES or not os.path.isfile(path):
            return
        seen.add(path)
        src = _read(path)
        chunks.append(src)
        for m in _INCLUDE_RE.finditer(src):
            walk(os.path.join(TEMPLATES_DIR, m.group(1).replace("/", os.sep)))
        for m in _ASSET_RE.finditer(src):
            disk = _asset_disk_path(m.group(1))
            if disk:
                walk(disk)

    walk(start)
    return "\n".join(chunks)
