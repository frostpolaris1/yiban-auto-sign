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
| 用途 | 网页正文字体 + 标题回退（标题主字体为下方 fangyuan）；`misans.css` 声明分片 @font-face，浏览器按需下载分片 |
| 许可证 | SIL Open Font License 1.1（许可证全文见 `fonts/misans/OFL.txt`，含版权声明与来源；OFL 要求重新分发附带许可证文本） |
| 说明 | 分片文件 370 个 + `misans.css` 1 个 + `OFL.txt` 1 个；低分屏（<1.5x）回退系统字体（woff2 无 hinting 渲染模糊） |

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
| 来源 | 部署者自行放置的品牌图标（**不入库**，版权归属部署者；缺失时页面自动回退为内联 SVG 占位符，见 README「自定义 Web 图标」） |

## 5. fonts/fangyuan/（阿里妈妈方圆体 VF · 标题 Web 字体）

| 项 | 值 |
|----|----|
| 文件 | `web/static/vendor/fonts/fangyuan/AlimamaFangYuanTiVF.woff2` |
| 版本 | 可变字体（wght 200–700；由官方 TTF 转 WOFF2） |
| 来源 | 阿里妈妈官方免费商用字体「阿里妈妈方圆体」（官网 `https://www.alibabafonts.com/`，允许免费商用与自托管嵌入） |
| 用途 | **仅页面标题**（h1~h4 与 `.font-semibold.tracking-tight` 标题组合）：三模板 + 文档页 CSS 内 @font-face 自托管下发，`local()` 置顶——访客设备已安装该字体时零下载 |
| 大小 | 2934504 字节（2.8 MB） |
| sha256 | `07a7bccae6d78f99fd3468a9c79832f49b13615ff7ab534b782233b6578c4b8f` |
| 说明 | **不入库**（体积原因，.gitignore 已忽略目录）：新环境部署时需手动放置本文件，否则已安装字体的设备仍可用（local 分支），其余访客回退 MiSans/系统字体 |
