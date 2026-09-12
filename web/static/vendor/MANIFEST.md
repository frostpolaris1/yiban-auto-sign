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
| 大小 | 407,279 字节 |
| SHA-256 | `176e894661aa9cdc9a5cba6c720044cbbf7b8bd80d1c9a142a7c24b1b6c50d15` |

## md-render.js

| 项 | 值 |
| --- | --- |
| 用途 | 迷你 Markdown 渲染器（仅渲染更新日志 CHANGELOG 用到的语法：标题 / 列表 / 加粗 / 行内代码），与 tailwind.js 同页引入 |
| 来源/构建 | **本项目自研**（零依赖、无构建步骤，手写 IIFE）；安全设计：先整体 HTML 转义再应用标记 → 无 XSS；`[^*\n]+` 不跨行匹配，防脱敏手机号 `138****8000` 的 `****` 与跨行 `**` 误加粗。修改后直接提交源文件即可 |
| 大小 | 2,733 字节 |
| SHA-256 | `0aea228c79e519058214fda93d2d92337f86c27730e5e25357b196d37ba4c3b2` |

## 校验方法

```bash
# Linux / macOS
sha256sum web/static/vendor/tailwind.js web/static/vendor/md-render.js
# Windows (PowerShell)
Get-FileHash -Algorithm SHA256 web/static/vendor/tailwind.js, web/static/vendor/md-render.js
```

基线登记日期：2026-08-22（规范审查 M18）

---

## daisyui/daisyui-subset.css

| 项 | 值 |
| --- | --- |
| 用途 | daisyUI 5 组件层（**定制子集**）。经 `templates/base.html` 以 `<link>` 引入，**必须放在 `tailwind.js` 之后**（顺序敏感） |
| 来源 | `https://cdn.jsdelivr.net/npm/daisyui@5/`（MIT）。用 jsDelivr `combine` 只拼所需部件，不取全量 |
| 组成 | `base/properties.css` + 24 个 `components/*.css`（alert badge button card checkbox divider dropdown fieldset input label link loading menu modal progress radio select stat status table textarea toast toggle tooltip）+ `theme/light.css` + `theme/dark.css` |
| 本地改造 | ① **排除 `base/reset.css`**：经实测它就是 Tailwind **v4 的 preflight**（`*,:after,::backdrop,:before{box-sizing:border-box;border:0 solid;margin:0;padding:0}` + `html{font-family:var(--default-font-family,…)}`）。本项目已有 v3 preflight，引入会同时改变排版与字体栈。<br>② **排除 `base/rootcolor.css`**：它给 `:root` 设页面底色，会与 `body.bg-zinc-50` 抢。<br>③ **递归剥离全部 `@layer` 包装**：Tailwind v3 的 Play CDN 产出的是**无 layer 的普通 CSS**，而 CSS 级联里**「无 layer 的声明永远赢过任何 layer 内的声明」**；不剥离则 daisyUI 的组件会被 v3 preflight 整片压掉——实测 `.btn` 的 `padding` / `border-width` / `background-color` 全部落到 preflight 的 0 / transparent。 |
| 大小 | **468,165 字节**（全量 `daisyui.css` 为 1,127,157 字节，本子集省约 58%） |
| SHA-256 | `6d9ae669e84bfd8b10aa937cfbf772bf50e3b97cc9742849a53e3f0d954fddc4` |
| ⑤ 补齐分片组装漏掉的变量 | **`--fx-noise`**：它只定义在 daisyUI **单体包**的 base 段（`:root{--fx-noise:url("data:image/svg+xml,…")}`），**不在任何 `base/*.css` 分片**里，按分片 combine 必然漏掉；而 button/menu/toggle/checkbox/radio/badge/alert **七个已引入组件**都以 `background-image: none, var(--fx-noise)` **无回退**引用它，缺失会让整条声明作废。本项目 `--noise:0`（关闭噪点），故补 `none`。**`--color-black`** 则是**上游自身也从未定义**的变量（status 组件无回退引用），一并补上。补法见文件末尾 COMPAT 注释。 |
| 验证 | 无头 Chrome 实测：`.btn` / `.badge` / `.card` 在 Tailwind v3 下 computed style 完整生效；子集内**无任何全局元素级规则**（不会波及现有元素）；`.yb-*` 自研类仍由本项目样式胜出。 |

### 重建 / 升级命令

构建脚本（**本地，不入库**，含个人路径）：`.superpowers/sdd/web-renewal/build-daisyui-subset.py`

```bash
<python> .superpowers/sdd/web-renewal/build-daisyui-subset.py web/static/vendor/daisyui/daisyui-subset.css
```

脚本用 jsDelivr `combine` 一次取齐全部部件，再递归剥离 `@layer`。
**剥离 `@layer` 这一步不可省**，否则样式会被 Tailwind v3 的 preflight 压掉（原因见上表「本地改造 ③」）。
升级后必须同步更新本表的「大小 / SHA-256」，并在浏览器复验 `.btn` 的 computed style。

登记日期：2026-09-10（前端模块化 V1）

## Adminator 设计系统（adminator/）

| 项 | 值 |
| --- | --- |
| 用途 | 管理端设计系统：设计 token（含暗色）、外壳布局、组件类（卡片/KPI/表格/表单/下拉/标签页/手风琴/月历）。经 `static/css/app.css` 引入，登录页与用户页暂时仍用 tailwind.js + daisyui 旧栈（两套样式层隔离，见 `static/css/legacy.css`） |
| 来源 | https://github.com/puikinsh/Adminator-admin-dashboard |
| 版本 | 4.3.0，commit `3ec0b93b05a3d540e3562e4dd5e22fa58642e26c`（MIT，见同目录 LICENSE） |
| 构建 | 该仓库 `npm ci && npm run build`（Node >= 22.22.2），再以裁剪入口 `src/assets/styles/2026/index.scss` 重建：仅保留 tokens/base/animations/shell/dropdowns/components/forms/ui/auth/error/data/charts/dashboard/calendar/responsive，剔除演示页专用的 chat/email/palette/fullcalendar。**只入库编译产物**，部署期不需要 Node。未入库其 JS：其 `mountShell()` 以 outerHTML 覆盖占位元素且导航取自内置常量，无法承载按角色渲染的导航，交互层改由本项目 `static/js/core.js` 实现 |
| 体积 | 68956 字节（gzip 12669 字节） |
| SHA-256 | `c94aa111a7a769f273e03e8a90bb266811965b305ce340a451e4e7268d52acbe` |

## Chart.js（chartjs/）

| 项 | 值 |
| --- | --- |
| 用途 | 统计页图表（折线/柱状/环形）。仅数据总览页加载 |
| 来源 | https://www.npmjs.com/package/chart.js（同目录 LICENSE.md，MIT） |
| 版本 | 4.5.1 |
| 构建 | 按需注册构建：仅注册 Line/Bar/Doughnut 控制器与 Category/Linear 刻度、Point/Line/Bar/Arc 元素及 Tooltip/Legend/Filler，避免整包（完整版 513075 字节）。构建命令记录于 `docs/refactor/04-asset-research.md` |
| 体积 | 190568 字节（gzip 66327 字节） |
| SHA-256 | `0817eece20f7f8c1efc44f49f5ac9673828fa9fa4babe14a5a5c9759cb4059fd` |

## 图标精灵图（lucide/）

| 项 | 值 |
| --- | --- |
| 用途 | 全站线性图标。经 `macros/ui.html` 的 `sprite()`/`icon()` 使用；symbol 用 `stroke="currentColor"`，随文字色适配暗色 |
| 来源 | https://github.com/lucide-icons/lucide（同目录 LICENSE，ISC；部分图标源自 Feather，MIT） |
| 版本 | 1.45.0，commit `b998e2892b90b88004d62da2d0b64dab9959a520` |
| 构建 | `python3 scripts/build_lucide_sprite.py <lucide 仓库目录> web/static/vendor/lucide/_sprite.svg`，子集化出 70 个图标，可复现 |
| 体积 | 19112 字节（gzip 3372 字节） |
| SHA-256 | `c48ed989c0ac455b105d51a5a2d2aa1ce582ac2b878c9dbb45ed3695b43b0afa` |

## 自托管字体（fonts/）

三方均为 **SIL OFL-1.1**（许可证全文见 `fonts/OFL.txt`），允许自托管与再分发；详细来源、下载命令与逐字重体积见 `fonts/MANIFEST.md`。

| 目录 | 文件数 | 总体积 | 聚合 SHA-256 |
| --- | --- | --- | --- |
| `inter/*.woff2` | 8 | 534816 | `29adf4e22d86348703104aab38f1229bc1e88b976d4b1ea4c53758f37732a44d` |
| `jetbrains-mono/*.woff2` | 1 | 21168 | `b346d592a3e572324ee55023406624bc0f1454e2ed0dbd7cde1d4bc7bd3546bb` |
| `notosanssc/*.woff2` | 96 | 7145564 | `3d0dbd37bbc9cbbdcb47e0c0ecb24715e0fd9e4805a0725feb79351b2364d6af` |

> 中文按 `unicode-range` 分片下发：浏览器只取页面实际用到的分片，故单页 CJK 载荷远小于目录总体积。
> `fonts/notosanssc/` 从 npm `md2note-fonts@1.0.0`（OFL-1.1）内嵌的完整 Noto Sans SC
> Regular/Bold 静态字体按字频重切片，实测本项目界面文本（893 个 CJK/全角码点）
> 单字重命中 3 片 ≈ **0.121 MiB**（400）/ **0.122 MiB**（700）
> （旧 Google Fonts 方案 23/101 片 ≈ 1.28 MiB）；覆盖基本区 20,976/20,992、
> 标点 64/64、全角 224/240、扩展 A 常用 77。来源、构建脚本与覆盖明细见 `fonts/MANIFEST.md`，
> 换源说明见 `docs/refactor/15-font-source-npm.md`。
> 2026-09 评估 npm `noto-sans-sc@37.0.0` 后确认**不换源**：该包实为 Google 网页子集再分发
> （并集覆盖基本区 12,242/20,992，低于本基线），其余完整字体候选成熟度不更优。
> 评估明细见 `docs/refactor/16-font-source-mature.md`。

> 体积与 gzip 的测量方法：`gzip -9c <file> | wc -c`（与 `docs/refactor/04-asset-research.md` 一致）。

## 迁移期共存说明

`tailwind.js`、`daisyui/daisyui-subset.css` 目前只被已退役、无路由渲染的 `base.html` / `index.html` 引用，
待旧栈退役时一并移除。`fonts/misans/`（4.6 MB，许可不实：MiSans 官方条款不允许再分发）已整体删除，
自托管中文字体只有 `fonts/{inter,jetbrains-mono,notosanssc}/`。
迁移期间两套样式层严格隔离：旧栈走 `static/css/legacy.css`，管理端走 `static/css/app.css`。
