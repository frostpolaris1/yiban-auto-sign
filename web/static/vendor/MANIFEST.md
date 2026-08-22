# vendor 静态资源基线（M18）

本目录为**本地化第三方/自研前端资产**：页面不得引用外网 CDN（离线可用 +
供应链可控）。每次新增或替换本目录 JS 资产，必须在本清单登记来源、构建方式
与 SHA-256 基线；升级时先更新哈希再合入。

## tailwind.js

| 项 | 值 |
| --- | --- |
| 用途 | Tailwind CSS Play CDN 构建：浏览器内即时编译工具类，模板经 `<script src="/static/vendor/tailwind.js?v={{ web_version }}">` 引入（login/index/user） |
| 来源 | 官方 Play CDN `https://cdn.tailwindcss.com/3.x`（下载时的最新 3.x 构建产物；文件内含其特征警告串 "cdn.tailwindcss.com should not be used in production"，可据此辨识真伪） |
| 构建 | 无需本地构建——官方预编译 IIFE 单文件；升级 = 重新从上述 URL 下载后重算哈希并更新本表。注意：该构建官方定位为开发/原型用途，生产推荐 PostCSS 插件/CLI 预编译，当前体量下接受此权衡 |
| 大小 | 407,362 字节 |
| SHA-256 | `f095de8d799a0281a19b0e349553ecb105c4b16dd4b94a2d568ad5fbd172cd79` |

## md-render.js

| 项 | 值 |
| --- | --- |
| 用途 | 迷你 Markdown 渲染器（仅渲染更新日志 CHANGELOG 用到的语法：标题 / 列表 / 加粗 / 行内代码），与 tailwind.js 同页引入 |
| 来源/构建 | **本项目自研**（零依赖、无构建步骤，手写 IIFE）；安全设计：先整体 HTML 转义再应用标记 → 无 XSS；`[^*\n]+` 不跨行匹配，防脱敏手机号 `138****8000` 的 `****` 与跨行 `**` 误加粗。修改后直接提交源文件即可 |
| 大小 | 2,786 字节 |
| SHA-256 | `ac58221422d603e844405a05d474fe6a810d38176a74be7f0433b89799628c61` |

## 字体（fonts/misans/）

MiSans Demibold 子集化 woff2 分片（编号分片由子集化工具产出），本地加载不出网。
分片数量多，不逐一登记哈希；替换字体需整目录原子替换并在此处补充说明。

## 校验方法

```bash
# Linux / macOS
sha256sum web/static/vendor/tailwind.js web/static/vendor/md-render.js
# Windows (PowerShell)
Get-FileHash -Algorithm SHA256 web/static/vendor/tailwind.js, web/static/vendor/md-render.js
```

基线登记日期：2026-08-22（规范审查 M18）
