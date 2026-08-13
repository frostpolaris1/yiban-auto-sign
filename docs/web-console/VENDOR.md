# 第三方资源溯源（web/static/vendor）

> 目的：记录 vendor 目录下每个文件的来源、版本与校验值，便于审计与复现。
> 校验命令：`sha256sum <文件>`（在仓库根目录执行）。

## 1. tailwind.js

| 项 | 值 |
|----|----|
| 文件 | `web/static/vendor/tailwind.js` |
| 版本 | 3.4.17（文件内版本标记；非 `tailwind.config` 版本） |
| 来源 | Tailwind CSS Play CDN 官方构建：`https://cdn.tailwindcss.com/3.4.17`（对应仓库 tag `v3.4.17`，发布页：`https://github.com/tailwindlabs/tailwindcss/releases/tag/v3.4.17`） |
| 用途 | 浏览器端按需生成工具类（Play CDN 运行时模式），本地化部署避免外网依赖；`darkMode: 'class'` 在页面内配置 |
| 大小 | 407362 字节 |
| sha256 | `f095de8d799a0281a19b0e349553ecb105c4b16dd4b94a2d568ad5fbd172cd79` |

## 2. fonts/misans/（MiSans 字体分片）

| 项 | 值 |
|----|----|
| 目录 | `web/static/vendor/fonts/misans/` |
| 字体 | 小米 MiSans（MiSans-Regular / MiSans-Demibold），woff2 按 unicode-range 分片 |
| 来源 | 小米官方字体站：`https://hyperos.mi.com/font/`（MiSans 开源字体，SIL OFL 1.1 许可证） |
| 用途 | 网页正文/标题字体；`misans.css` 声明分片 @font-face，浏览器按需下载分片 |
| 说明 | 分片文件 370 个 + `misans.css` 1 个；低分屏（<1.5x）回退系统字体（woff2 无 hinting 渲染模糊） |

## 3. md-render.js

| 项 | 值 |
|----|----|
| 文件 | `web/static/vendor/md-render.js` |
| 版本 | 无（自研） |
| 来源 | 本项目自研：轻量 Markdown 渲染器（标题/粗体/列表/代码块/链接等常用子集），用于更新日志弹窗渲染 |
| 说明 | 仅登录页与后台使用（`window.renderMarkdown` 存在性判断调用）；非第三方库，无外部依赖 |

## 4. logo.png

| 项 | 值 |
|----|----|
| 文件 | `web/static/vendor/logo.png` |
| 来源 | 站点占位 logo（项目自有素材），非第三方资源 |
