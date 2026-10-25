# tools —— 开发期构建与代码生成

## 放什么

只在构建机或本地运行的开发工具：字体切片与字重打标、Lucide 精灵图生成、
视觉演示种子、演示数据生成、肘界影子分析。它们改写产物或造演示环境。

## 不放什么

不放生产运行时依赖：`yiban/`、`web/`、`scripts/`、`docker/` 不得 import 本目录。
不放部署执行件、密钥与真实数据。镜像不 COPY 本目录。

## 入口

各文件头有用法。主要入口：
`build_cjk_font_slices.py`（字体分片）、`build_lucide_sprite.py`（图标精灵）、
`stamp_font_versions.py`（内容哈希打标）、`generate_demo_data.py`（演示库）、
`dev_visual_seed.py`（演示环境）、`elbow_shadow.py`（肘界影子分析）。

## 遗留与删除条件

本目录 B1（2026-10-25）自 `scripts/` 移入，运行时零 import。
删除条件：某个工具不再被任何门或构建流程调用，且产物已入库且可复核时，单文件删除。
