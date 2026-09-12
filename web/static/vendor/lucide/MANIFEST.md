# vendor/lucide 图标精灵图基线

本目录为 **Lucide 图标子集**的本地化资产：页面不得引用外网 CDN（离线可用 +
供应链可控）。升级时先按下文重建、更新哈希，再合入。

## _sprite.svg

| 项 | 值 |
| --- | --- |
| 用途 | 管理端图标单一事实源：`<symbol id="i-*">` 精灵图，经 `web/templates/macros/ui.html` 的 `sprite()` 宏在 `<body>` 后内联一次，再由 `icon('name', 'classes')` 以 `<use href="#i-name">` 引用。`stroke="currentColor"` 使图标继承文字色，暗色模式无需额外样式 |
| 来源 | `https://github.com/lucide-icons/lucide`，tag **`1.45.0`**，commit **`b998e2892b90b88004d62da2d0b64dab9959a520`**（`git/ref/tags/1.45.0` 直指该 commit）。SVG 取自仓库 `icons/` 下的规范名文件；**不使用** npm 包，也不使用已废弃别名 |
| 子集范围 | **70 个图标**（导航 14 / 动作 41 / 状态 8 / 数据 7），完整「概念 → Lucide 名」对照与取舍见 `docs/refactor/08-icons-vendoring.md` |
| 构建与子集化 | ① 下载指定 tag 的 tarball：`curl -sSL -o lucide.tar.gz "https://codeload.github.com/lucide-icons/lucide/tar.gz/refs/tags/1.45.0"`（Windows Git Bash 的 schannel 若报吊销检查失败，加 `--ssl-no-revoke`）；② 解压得 `lucide-1.45.0/`；③ 运行仓库内构建脚本：`python scripts/build_lucide_sprite.py lucide-1.45.0 web/static/vendor/lucide/_sprite.svg`。脚本从 `icons/<name>.svg` 抽取图形、剥离硬编码颜色、压缩空白，输出单个 `<symbol>` 精灵图；图标清单硬编码在脚本 `ICONS` 中，可复现、可审计 |
| 大小 | **19,112 字节**（gzip -9：**3,360 字节**，约 17.6%） |
| SHA-256 | `c48ed989c0ac455b105d51a5a2d2aa1ce582ac2b878c9dbb45ed3695b43b0afa` |

## LICENSE

| 项 | 值 |
| --- | --- |
| 用途 | Lucide 上游许可证（ISC + 部分 Feather 派生图标的 MIT 条款）随资产一同分发 |
| 来源 | 同 tag/commit 的仓库根 `LICENSE`（`https://github.com/lucide-icons/lucide/blob/1.45.0/LICENSE`） |
| 大小 | 3,208 字节 |
| SHA-256 | `b495047bd93a9b06913511076f504daba17d5bbeb3e0650f3bb53a4220329c57` |

## 校验方法

```bash
# Linux / macOS
sha256sum web/static/vendor/lucide/_sprite.svg web/static/vendor/lucide/LICENSE
# Windows (PowerShell)
Get-FileHash -Algorithm SHA256 web/static/vendor/lucide/_sprite.svg, web/static/vendor/lucide/LICENSE
# gzip 体积（mtime=0，可复现）
python -c "import gzip,sys;b=open('web/static/vendor/lucide/_sprite.svg','rb').read();print(len(b),len(gzip.compress(b,9,mtime=0)))"
```

登记日期：2026-09-12
