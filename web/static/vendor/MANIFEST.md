# vendor 静态资源基线（M18）

本目录为**本地化第三方/自研前端资产**：页面不得引用外网 CDN（离线可用 +
供应链可控）。每次新增或替换本目录 JS 资产，必须在本清单登记来源、构建方式
与 SHA-256 基线；升级时先更新哈希再合入。

## md-render.js

| 项 | 值 |
| --- | --- |
| 用途 | 迷你 Markdown 渲染器（仅渲染更新日志 CHANGELOG 用到的语法：标题 / 列表 / 加粗 / 行内代码），经 `layout_*.html` 与 `core.js` 的更新日志模态使用 |
| 来源/构建 | **本项目自研**（零依赖、无构建步骤，手写 IIFE）；安全设计：先整体 HTML 转义再应用标记 → 无 XSS；`[^*\n]+` 不跨行匹配，防脱敏手机号 `138****8000` 的 `****` 与跨行 `**` 误加粗。修改后直接提交源文件即可 |
| 大小 | 2,733 字节 |
| SHA-256 | `0aea228c79e519058214fda93d2d92337f86c27730e5e25357b196d37ba4c3b2` |

## 校验方法

```bash
# Linux / macOS
sha256sum web/static/vendor/md-render.js
# Windows (PowerShell)
Get-FileHash -Algorithm SHA256 web/static/vendor/md-render.js
```

基线登记日期：2026-08-22（规范审查 M18）

---


| 项 | 值 |
| --- | --- |
| 用途 | 全站设计系统：设计 token（含暗色）、外壳布局、组件类（卡片/KPI/表格/表单/下拉/标签页/手风琴/月历）。经 `static/css/app.css` 引入，管理端与用户端、登录页统一使用 |
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

## 旧栈退役说明（2026-09-13，P4）

Tailwind Play CDN（`tailwind.js`）、daisyUI 子集（`daisyui/daisyui-subset.css`）、旧栈样式
（`static/css/legacy.css`）与旧单页壳（`templates/base.html` / `index.html` / `tabs/*` /
`partials/modals/*` / `partials/head_boot.html`）已整体退役：全站页面统一走 Adminator
（`adminator/adminator.css`）+ 项目样式（`static/css/app.css`），不再有第二套样式层。
现存模板仅在 `templates/partials/{tailwind_config,component_layer}.html` 保留旧组件层
规格文件，供设计令牌 / 组件层回归测试读取，运行时不加载。
`fonts/misans/`（4.6 MB，许可不实：MiSans 官方条款不允许再分发）已整体删除，
自托管中文字体只有 `fonts/{inter,jetbrains-mono,notosanssc}/`。
