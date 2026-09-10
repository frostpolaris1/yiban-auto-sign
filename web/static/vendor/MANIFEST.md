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

---

## daisyui/daisyui-subset.css

| 项 | 值 |
| --- | --- |
| 用途 | daisyUI 5 组件层（**定制子集**）。经 `templates/base.html` 以 `<link>` 引入，**必须放在 `tailwind.js` 之后**（顺序敏感） |
| 来源 | `https://cdn.jsdelivr.net/npm/daisyui@5/`（MIT）。用 jsDelivr `combine` 只拼所需部件，不取全量 |
| 组成 | `base/properties.css` + 24 个 `components/*.css`（alert badge button card checkbox divider dropdown fieldset input label link loading menu modal progress radio select stat status table textarea toast toggle tooltip）+ `theme/light.css` + `theme/dark.css` |
| 本地改造 | ① **排除 `base/reset.css`**：经实测它就是 Tailwind **v4 的 preflight**（`*,:after,::backdrop,:before{box-sizing:border-box;border:0 solid;margin:0;padding:0}` + `html{font-family:var(--default-font-family,…)}`）。本项目已有 v3 preflight，引入会同时改变排版与字体栈。<br>② **排除 `base/rootcolor.css`**：它给 `:root` 设页面底色，会与 `body.bg-zinc-50` 抢。<br>③ **递归剥离全部 `@layer` 包装**：Tailwind v3 的 Play CDN 产出的是**无 layer 的普通 CSS**，而 CSS 级联里**「无 layer 的声明永远赢过任何 layer 内的声明」**；不剥离则 daisyUI 的组件会被 v3 preflight 整片压掉——实测 `.btn` 的 `padding` / `border-width` / `background-color` 全部落到 preflight 的 0 / transparent。 |
| 大小 | **468,261 字节**（全量 `daisyui.css` 为 1,127,157 字节，本子集省约 58%） |
| SHA-256 | `60581196bd16e1dbd71b5ddbba81d264a034641dcfdaed7409817766f40f68d2` |
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
