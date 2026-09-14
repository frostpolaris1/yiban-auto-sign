# -*- coding: utf-8 -*-
"""给 self-hosted 字体 CSS 里引用的 woff2 分片追加内容哈希查询串（幂等，可重跑）。

## 解决什么问题

`web/app.py` 对 `/static/` 下发 `Cache-Control: public, max-age=2592000`（30 天强
缓存，强缓存期内浏览器不发请求，ETag 无从生效）。字体引用链现为：

    三个外壳直接 <link>
      fonts/inter/inter.css?v={{ web_version }}            ← 进程启动时间戳，每次发版变化
      fonts/jetbrains-mono/jetbrains-mono.css?v=…           ← 同上
      fonts/notosanssc/notosanssc.css?v=…                   ← 同上
        └─ url('xxx.woff2')                                 ← 固定 URL（本脚本负责打标）

外壳的 `?v={{ web_version }}` 让三个子 CSS 每次发版都是新鲜 URL；但子 CSS 内部的
woff2 分片仍以**固定 URL** 被 30 天强缓存。`build_cjk_font_slices.py` 重切片后分片
文件集合/内容变化，旧访客（浏览器里缓存着旧子 CSS）按旧 unicode-range 请求已被
删除的旧分片 → 404 → 中文回退宋体，最长 30 天（子 CSS 缓存过期）自愈。

## 修法（本脚本）

对**分片层**：每个 `url('…/*.woff2')` 追加 `?v=<woff2 内容哈希>` —— 分片内容不变时
URL 不变，30 天缓存继续生效；内容/文件名一变 URL 即变，永不 404。

配合外壳给子 CSS 的 `?v={{ web_version }}`（每次发版变化）共同打断失效链条：
子 CSS URL 变 → 拿到新子 CSS → 其中分片 URL 因内容哈希变化也是新的。

> 历史：以前 `fonts/fonts.css` 是 @import 聚合入口，@import 层也需版本化；改为三个
> 外壳直接并行 <link> 后该层不复存在（省 1–2 个 RTT），故本脚本不再处理 @import。
> `fonts/fonts.css` 现为纯注释说明文件，仅作本目录说明与 build 脚本的 scope 哨兵。

幂等性：处理前先剥掉 url 中已有的 `?v=…` 再重新计算哈希 —— 分片内容未变时二次
运行零 diff（哈希对文件内容收敛，与历史查询串无关）。`--check` 模式不写盘，
存在待更新内容时以退出码 1 报告（供 CI / 验证脚本调用）。

只处理相对路径的本地资源；`data:`、绝对路径与外网 URL 一律跳过。
用法：

    python scripts/stamp_font_versions.py            # 打标（默认 fonts 目录）
    python scripts/stamp_font_versions.py --check    # 只检查不写盘
"""
import hashlib
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_FONTS_DIR = os.path.join(REPO_ROOT, "web", "static", "vendor", "fonts")

HASH_LEN = 8

# url('path') / url("path") —— 本仓字体 CSS 均为单引号，双引号一并兼容
_URL_RE = re.compile(r"url\(\s*(['\"])([^'\")]+)\1\s*\)")


def _short_hash(path):
    """文件内容的 sha256 短哈希。"""
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:HASH_LEN]


def _is_local_relative(url):
    """只处理相对路径：绝对路径(/)、协议相对(//)、外网(scheme://)、data: 一律跳过。"""
    return not (url.startswith(("/", "data:", "//")) or "://" in url)


def _strip_version(url):
    """剥掉 url 上已有的 ?v=… 查询串（幂等的前提）。"""
    return url.split("?", 1)[0].split("#", 1)[0]


def stamp_fonts_dir(root, check=False):
    """对 root 下所有 CSS 里引用的 woff2 分片打内容哈希。返回 {文件相对路径: 是否(将)更新}。"""
    css_files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(".css"):
                css_files.append(os.path.join(dirpath, name))
    results = {}

    for css_path in css_files:
        base = os.path.dirname(css_path)

        def _font_url(m):
            quote, url = m.group(1), m.group(2)
            if not _is_local_relative(url):
                return m.group(0)
            target = os.path.normpath(os.path.join(base, _strip_version(url)))
            if not os.path.isfile(target):
                # 引用落空：打不了内容哈希，保留原样（缺文件是另一类问题，不在此掩盖）
                return m.group(0)
            stamped = "%s?v=%s" % (_strip_version(url), _short_hash(target))
            return "url(%s%s%s)" % (quote, stamped, quote)

        text = open(css_path, encoding="utf-8").read()
        new_text = _URL_RE.sub(_font_url, text)
        rel = os.path.relpath(css_path, root)
        results[rel] = new_text != text
        if not check and new_text != text:
            with open(css_path, "w", encoding="utf-8", newline="") as fh:
                fh.write(new_text)
        # 未变化不重写（保 mtime）
    return results


def main(argv=None):
    check = "--check" in (argv if argv is not None else sys.argv[1:])
    args = [a for a in (argv if argv is not None else sys.argv[1:]) if a != "--check"]
    root = os.path.abspath(args[0]) if args else DEFAULT_FONTS_DIR
    if not os.path.isdir(root):
        sys.exit("错误：目录不存在 %s" % root)
    results = stamp_fonts_dir(root, check=check)
    for rel, changed in sorted(results.items()):
        print("%s %s" % ("已更新" if changed else "无变化", rel))
    if check and any(results.values()):
        sys.exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
