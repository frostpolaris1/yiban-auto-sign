# 易班自动签到 · 新线设计系统规范（Design System v1）

> 归属：前端视觉与交互唯一基准（与 `ui/src/styles/scale.css`、`ui/src/styles/tokens.css`、`ui/DESIGN-CONTRACT.md` 配套）。
> 适用范围：第 1 期（基准阶、共用组件、外壳、`/` 总览、`/accounts` 账号、`/app` 用户端主页）、第 2 期（覆盖层收编、`/accounts` 移动端行卡化、`/queue`、`/logs`、`/users`、`/schools`、`/audit`、`/settings`、`/system`、`/design` 全部管理端页面）与第 3 期（门户五页 `/login`、`/register`、`/forgot-password`、`/reset-password`、404 与用户端七页 `/app/*` 全量收口）。

---

## 1. 设计人格与双端同源

管理端（`/` 等）与用户端（`/app` 等）不是两个割裂产品，而是**同一视觉母体的两种密度**：

- **同一表面语言**：纸面卡片统一使用 `var(--bg-card)` + `1px solid var(--border)` + `var(--shadow-card)` + `var(--radius-lg)`（`12px`），画布底统一使用 `var(--bg-body)`。
- **同一语义色体系**：严禁在任何非豁免文件写入颜色字面量，全部消费 `tokens.css` 的 36 浅 + 36 深令牌及 `color-mix(in srgb, ...)` 派生。
- **密度分档而不换骨**：
  - **管理端（桌面）**：默认控件高度 `40px`（对齐 Adminator 原生实测 `40.1px`），行内/分页/紧凑操作 `32px`，表格标准行高 `48px`（紧凑模式 `40px`），追求高信息吞吐与首屏直达。
  - **移动端（`<= 720px`）与用户端（`/app`）**：所有交互控件统一抬至 `48px`（原生 `.btn--lg` 实测 `47.7px`，同时高于 `44px` 触控下限），留白更松弛，保持清晰的视觉焦点与单手可达性。

---

## 2. 尺寸基准阶（`ui/src/styles/scale.css`）

### 2.1 间距阶（4px 基准）

全站外边距、内边距、栅格沟槽与元素间距默认取 4 的倍数；仅两处为对齐 Adminator 原生卡片呼吸感而登记例外：`--card-pad: 22px`（桌面 `.card` / `.kpi-card` 顶左右内距）与桌面 `.kpi-card` `gap: 14px`（详见 §2.8）。禁止新增其他 5 / 7 / 9 / 11 / 13 / 18 等散落数值。

| 令牌 | 像素值 | 典型用途 |
|---|---:|---|
| `--space-0` | `0px` | 重置 |
| `--space-1` | `4px` | 紧贴微间距（眉标/标题底距、图标与计数、表单字段内标签距输入、分段容器内边距） |
| `--space-2` | `8px` | 控件组间距、页头副标题顶距、紧凑横幅内边距、移动 `.kpi-compare` 顶距 |
| `--space-3` | `12px` | 移动栅格沟槽、移动卡头分隔、`.kpi-compare` 顶内距、工具栏/筛选栏间距、健康条上下内边距 |
| `--space-4` | `16px` | 移动卡片内边距、移动区块间距、移动页面左右与底边距、桌面卡头底内距与项间距、弹窗/抽屉内边距 |
| `--space-5` | `20px` | 桌面区块间距（`--section-gap`）、桌面栅格沟槽（`--grid-gap`）、桌面卡头底距、`.kpi-card` 桌面底内距、移动页面顶距 |
| `--space-6` | `24px` | 桌面主内容底距 |
| `--space-8` | `32px` | 桌面内容区顶与左右留白（`--page-pad-y` / `--page-pad-x`）、桌面顶栏左右内边距、图标输入左留空 / 下拉框右箭头留空 |
| `--space-10` | `40px` | 桌面 7 天日历格、表格紧凑行高 |
| `--space-12` | `48px` | 表格标准行高、用户端身份照片头像 |

### 2.2 控件高度三档

同类控件在同一视口下**只能有一个高度**；不得再由 `padding + line-height` 自由撑出非整数高度（如 20.1 / 24.6 / 31.8 / 37.5 / 40.1px）。

| 令牌 | 像素值 | 适用组件（桌面 `> 720px`） | 移动端（`<= 720px`） |
|---|---:|---|---|
| `--ctrl-h-sm` | `32px` | `.btn--sm`、表内操作键、行内更多图标键、`.app-cell-link`、`.pager-btn`、`.alert .close`、`.app-card-tabs button`、`.tabs.pills .tab` | 统一抬升至 `48px` |
| `--ctrl-h-md` | `40px` | `.btn`（默认）、`.input`、`.select`、`.app-select-trigger`、顶栏 `.cmd` / `.icon-btn` / `.avatar`、侧栏 `.nav-link`、`.tab`、`.dd-menu-item` | 统一抬升至 `48px` |
| `--ctrl-h-lg` | `48px` | `.btn--lg`、用户端全部按钮（`.uapp-button` / `.uapp-icon-btn` / `.uapp-avatar` / `.uapp-hero-link` / `.uapp-seg-item` / `.uapp-announcement-close`）与 `.uapp-input` | 移动端全部可点控件基准 |
| `--ctrl-h-textarea` | `96px` | `.textarea` 多行文本域最小高度 | `96px` |

> **例外契约**：弹窗/抽屉右上关闭键 `.mail-tool` 按既有行为测试保留 `24×24` 视觉盒，并通过 `::after { inset: -10px; }` 扩展至 `44×44` 触控热区。

### 2.3 高度契约（Adminator 原生实测反推）

> 口径：把 `web/static/vendor/adminator/adminator.css` 单文件渲染进 Chromium（原生 body `14px/1.55`）逐选择器量 `getBoundingClientRect().height`，不按字号估算。复跑探针 `/home/ubuntu/agy-runs/vendor-probe.js`（本地取证，不入库）。

| 原生选择器 | 原生实测 | 我们的落地 | 取整依据 |
|---|---:|---|---|
| `.btn` | `40.1px` | `--ctrl-h-md: 40px` | `9px 14px` 内边距 + 13px 字继承 21.7px 行高；向 4px 网格取整 |
| `.btn--sm` | `31.8px` | `--ctrl-h-sm: 32px` | 原本即对齐，不动 |
| `.btn--lg` | `47.7px` | `--ctrl-h-lg: 48px` | `12px 18px` + 14px 字 |
| `.input` | `40.1px` | `40px`（同 `md` 档） | 与 `.btn` 等高的工具栏混排才齐平 |
| `.select` | `35px` | `40px`（同 `md` 档） | 原生 `line-height: normal` 导致的自身不一致，不复制；输入与下拉必须同高 |
| `.table thead th` | `38px` | 竖向 `10px / 12px`（新令牌，实测 `39px`） | 我们表头 `12px/16px` 字高于原生 `10px/15.5px`，只对齐内边距、不强推总高 |
| `.table tbody td` | `43.1px` | 行高 `48px`（`--table-row-h`，刻意高于原生） | 单元格带 `32px` 行内操作键 + 上下 `8px` 内边距时行天然为 `48px`，且 `accounts/schools` 行为用例把「行高 48px」钉成布局契约，不为对齐原生把行挤回 `44px` |
| `.badge` | `20.7px` | `20px`（既有） | 不动 |
| `.nav-link` | `33.6px` | `40px`（`--ctrl-h-md`） | 侧栏点击区按控件基准给足，不复制原生的紧凑内边距 |

**判定规则**（`scripts/consistency-check.mjs` 的 `badH` 项）：显式高度必须落在 4px 网格；新控件只能取 `32 / 40 / 48` 三档；`<= 720px` 一律 `48px`。

### 2.4 字号与行高音阶（六档整数）

全站禁用小于 `12px` 的可读文字，禁用非整数（11.5 / 12.5 / 12.8 / 13.5px），禁用随视口漂移的 `clamp()` 标题字号。

| 令牌 | 字号 | 行高令牌 | 行高 | 典型用途 |
|---|---:|---|---:|---|
| `--text-xs` | `12px` | `--leading-xs` | `16px` | 眉标 `.eyebrow`、表头 `th`、徽章 `.badge`/`.tag`、说明 `.app-muted`/`.app-caption`、表单标签/帮助/错误、紧凑按钮 `.btn--sm`、等宽辅码 `.data-cell-mono`、侧栏分组标签、页脚、时间轴刻度、健康条标签 |
| `--text-base` | `14px` | `--leading-base` | `20px` | 正文默认、侧栏导航项、面包屑、默认按钮 `.btn`、输入框/下拉框、表体文字 `td`、提示条正文、菜单项、页签、用户端正文与按钮 |
| `--text-lg` | `16px` | `--leading-lg` | `24px` | 卡片标题 `.card-title`、品牌名 `.brand-name`/`.uapp-brand-text`、弹窗/抽屉标题 `.modal-title`、KPI 单位 `sup`、用户端账号名 `.uapp-hero-name` |
| `--text-xl` | `20px` | `--leading-xl` | `24px` | 指标数值 `.app-metric-value`（精确保持 24px 行盒契约）、用户端问候 `.uapp-greeting`、用户端状态时间 `.uapp-hero-time` 与统计读数 `.uapp-stat-value` |
| `--text-2xl` | `28px` | `--leading-2xl` | `36px` | 管理端页头主标题 `h1.hero-title`、用户端今日结论主角 `.uapp-result .uapp-hero-title` |
| `--text-display` | `44px` | `--leading-display` | `44px` | 看板 KPI 读数 `.kpi-value`（精确保持 44px 行盒契约，紧凑密度不缩小） |

> 数字类指标与表格编码统一开启 `font-variant-numeric: tabular-nums`，避免刷新跳动。

### 2.5 圆角四档

以 `999px`（`--radius-full`）统一表达胶囊与正圆，**禁止再写 `50%`**，消除两套圆形计算值。

| 令牌 | 像素值 | 适用元素 |
|---|---:|---|
| `--radius-sm` | `6px` | 微件：`.kbd`、`.nav-badge--count`、`.app-health-chip`、`.mail-tool`、`.alert .close`、`.check .box`、`.app-select-item`、`code`、表行图标按钮 |
| `--radius-md` | `8px` | 控件与小容器：`.btn`、`.input`、`.select`、`.textarea`、`.cmd`、`.icon-btn`、`.pager-btn`、侧栏 `.nav-link`/`.workspace`、`.brand-logo`、`.kpi-icon`、`.alert`、`.app-pending-banner`、`.app-metric`、`.app-batch-bar`、用户端 `.uapp-button`/`.uapp-input`/`.uapp-day-cell`/`.uapp-hero-photo` |
| `--radius-lg` | `12px` | 主容器与浮层：`.card`、`.kpi-card`、`.app-health-strip`、`.modal-demo`、`.dd-menu`、`.app-select-content`、`.app-search-panel`、用户端 `.uapp-hero`/`.uapp-card` |
| `--radius-full` | `999px` | 胶囊与正圆：`.badge`、`.tag`、`.kpi-pill`、`.avatar`、`.workspace-avatar`、`.uapp-avatar`、`.uapp-top-link`、`.uapp-status`、`.switch .track`、`.progress`、`.app-card-tabs button`、状态圆点 |

### 2.6 卡片内边距（两档）

| 令牌 | 像素值 | 适用容器 |
|---|---:|---|
| `--card-pad` | `22px` | 桌面标准 `.card`、`.kpi-card`（顶左右 `22px`、底 `20px`）、用户端 `.uapp-card`、`.uapp-hero` / `.uapp-result`（Adminator 原生对齐例外，见 §2.8）；管理端移动端（`<= 720px`）`.card` / `.kpi-card` 覆盖为 `16px`（`--space-4`） |
| `--card-pad-sm` | `12px` | 紧凑单行状态条 `.app-health-strip`（桌面 `12px 22px`，移动 `12px 16px`） |

> 显式切换到紧凑密度（`html[data-density="compact"]`）时，桌面 `.card` 按契约由标准 `22px` 收缩为 `14px`（窄屏 `16px → 12px`），行高 `48px → 40px`。

### 2.7 外壳与容器尺寸

- 管理台侧栏：`--sidebar-w: 240px`（平板 `721–1100px` 收窄为 `--sidebar-w-rail: 68px`；移动 `<= 720px` 抽屉化）
- 管理台顶栏：`--topbar-h: 52px`（桌面）、`48px`（移动 `<= 720px`）
- 管理台内容区：`.content` 不设 `max-width`（全宽表格需要横向吞吐），内边距桌面 `32px 32px 24px`，移动 `20px 16px 16px`
- 用户端顶栏：`--uapp-bar-h: 52px`（移动）、`--uapp-bar-h-wide: 56px`（`>= 768px`）；底栏 `--uapp-bottom-h: 60px`
- 用户端内容最大宽：主页 `984px`、历史 `1120px`、设置 `760px`、表单 `560px`

### 2.8 间距基准与 Adminator 原生对照

> 依据：`web/static/vendor/adminator/adminator.css`（`ui/src/styles/adminator.css` 逐字节副本）中的原生页骨架与卡片/KPI 规则，恢复「内紧外松」的三档视觉层级，消除全站间距塌缩到 `12/16px` 两档导致的紧迫感。

**间距层级不变式**：
- **桌面（`> 720px`）**：卡内 `gap` `14px` < 卡间栅格 `20px` = 区块间 `20px` < 页内距 `32px`。
- **移动（`<= 720px`）**：卡内/卡间 `12px` < 区块间 `16px` ≤ KPI 底距/页顶 `20px`。

#### 桌面（`> 720px`）与 Adminator 原生对照

| 位置 / 令牌 | Adminator 原生 | 我们的落地 | 来源 / 例外说明 |
|---|---|---|---|
| `--page-pad-x` / `--page-pad-y` | `32px` / `32px` | `32px` / `32px` | `scale.css` `:root` |
| `--section-gap` / `--grid-gap` | `20px` / `20px` | `20px` / `20px` | `scale.css` `:root` |
| `--card-pad` / `--card-pad-sm` | `22px` / — | `22px` / `12px` | `22px` 记为原生对齐例外；`--card-pad-sm` 保持 `12px` |
| `.content` padding | `32px 32px 24px` | `32px 32px 24px` | `var(--page-pad-y) var(--page-pad-x) var(--space-6)` |
| `.hero` margin-bottom | `28px` | `20px` | `var(--section-gap)`（兼顾首屏 KPI 顶距 `<= 340px`） |
| `.hero-text .eyebrow` / `.hero-title` mb / `.hero-sub` mt | `10 / 8 / 0px` | `4 / 4 / 8px` | `--space-1` / `--space-1` / `--space-2` |
| `.card` padding | `22px` | `22px` | `var(--card-pad)`（compact 仍覆盖 `14px`） |
| `.card-head` pb / mb / gap | `16 / 20 / 16px` | `16 / 20 / 16px` | `--space-4` / `--space-5` / `--space-4` |
| `.kpi-grid` gap / mb | `20 / 20px` | `20 / 20px` | `var(--grid-gap)` / `var(--section-gap)` |
| `.kpi-card` padding / gap | `22px 22px 20px` / `14px` | `22px 22px 20px` / `14px` | `var(--card-pad) var(--card-pad) var(--space-5)`；`gap: 14px` 记为原生对齐例外 |
| `.kpi-compare` pt / gap | `12 / 7px` | `12 / 8px` | `--space-3` / `--space-2` |
| `.grid` gap / `.app-dashboard-workbench` mb / `.app-health-strip` mt | `20px` / — / — | `20 / 20 / 20px` | `var(--grid-gap)` / `var(--section-gap)` |

#### 移动（`<= 720px`）基准

| 位置 / 令牌 | 落地值 | 来源 |
|---|---|---|
| `--page-pad-x` / `--page-pad-y` | `16px` / `20px` | `scale.css` `@media (max-width: 720px)` |
| `--section-gap` / `--grid-gap` | `16px` / `12px` | `scale.css` `@media (max-width: 720px)` |
| `.content` padding | `20px 16px 16px` | `var(--page-pad-y) var(--page-pad-x) var(--space-4)` |
| `.hero` margin-bottom | `16px` | `var(--section-gap)` |
| `.card` padding | `16px` | `var(--space-4)`（compact 覆盖 `12px`） |
| `.kpi-card` padding | `16px` | `var(--space-4)`（compact **不**覆盖；KPI 卡无 `.card` 类） |
| `.card-head` pb / mb / gap | `12 / 12 / 12px` | `var(--space-3)` |
| `.kpi-grid` gap / mb | `12 / 20px` | `var(--grid-gap)` / `var(--space-5)` |
| `.kpi-card` gap | `12px` | `var(--space-3)` |
| `.kpi-compare` pt / mt | `12 / 8px` | `var(--space-3)` / `var(--space-2)` |
| `.app-dashboard-workbench` mb / `.app-health-strip` mt | `16 / 16px` | `var(--section-gap)` |

---

## 3. 基础组件规格（`ui/src/components/ui/`）

1. **Button / BusyButton**
   - 默认 `md`（`40px`）、紧凑 `sm`（`32px`）、大号 `lg`（`48px`）、图标 `icon`（方框与档位同高）；`<= 720px` 统一 `48px`。
   - **交互态**：`hover` 仅动底色与边框；`focus-visible` 统一 `2px solid var(--primary)` + `2px` 外偏；`disabled` 统一 `opacity: 0.45; cursor: not-allowed`（与全站 e2e 禁用降级契约一致）；`danger` 浅色走 `color-mix(in srgb, var(--danger) 75%, var(--t-base))` 保 WCAG AA，暗色走 `var(--danger)` 配 `var(--t-inverse)`。
2. **Input / Select / TextArea / FilterField / Switch / Checkbox**
   - 单行输入与原生/Radix 下拉统一 `40px`（桌面）/ `48px`（移动与用户端），`--radius-md`，`14px` 正文字号。
   - 聚焦态统一 `border-color: var(--primary)` + `box-shadow: 0 0 0 3px var(--primary-ring)`；错误态统一 `border-color: var(--danger)` + `12px` 错误提示。
   - 字段壳（`FieldShell` / `FilterField`）间距 `--space-1`（`4px`），标签与帮助文字统一 `12px`。
3. **Badge / Tag**
   - 统一 `12px / 16px`，`--radius-full`，状态点采用双载体（圆点 + 文字），浅色软底文字全部经 `color-mix` 压深满足 AA 对比度。
4. **Card / PageHeader**
   - `Card` 内边距桌面 `22px`（移动 `16px`），圆角 `12px`；`.card-head` 桌面 `padding-bottom: 16px` / `margin-bottom: 20px` / `gap: 16px`（移动 `12 / 12 / 12px`），标题 `16px` 粗体，眉标 `12px`。
   - `PageHeader`（`.hero`）采用左右居中对齐、底距 `20px`（移动 `16px`），眉标与标题底距 `4px`、副标题顶距 `8px`，标题固定 `28px`，副标题桌面 `14px` / 移动 `12px`，右侧动作组垂直居中对齐。
5. **Alert / 状态横幅**
   - 普通内联 `Alert` 采用 `12px 16px` 内边距与 `8px` 圆角；顶部系统横幅（`.app-alert-block` / `.app-announcement-banner`）采用 `8px 12px` 单行流式排布，避免多行横幅挤掉首屏核心区。
6. **DataTable / SelectionBar / Pager**
   - 表头 `th` 统一 `12px`、行体 `td` 统一 `14px`、等宽编号/时间 `.data-cell-mono` 统一 `12px`。
   - 行高 `48px`（紧凑 `40px`），表头竖向内边距取原生 `10px / 12px`（实测 `39px`），窄屏由 `.table-scroll` 内部横向滚动，页面本身零横向溢出。
   - 分页按钮 `.pager-btn` 桌面 `32px` / 移动 `48px`，圆角 `8px`。
7. **Tabs / Menu / Modal / Drawer / Hint**
   - 浮层统一 `--radius-lg`（`12px`）+ `var(--shadow-lg)` + `1px solid var(--border)`；菜单项与下拉项桌面 `40px`、圆角 `6px`。
   - 弹窗与抽屉头/体/脚内边距统一 `16px`，标题 `16px`，正文 `14px`。

---

## 4. 页面骨架与首屏预算

### 4.1 标准管理页骨架

```text
[ Topbar (52px 桌面 / 48px 移动) ]
  └── Main Content (pad: 32px 32px 24px 桌面 / 20px 16px 16px 移动)
        ├── 系统横幅（可选，行内紧凑 ≤ 40px）
        ├── PageHeader（眉标 + H1 + 一行说明 + 右侧主操作，底距 20px 桌面 / 16px 移动）
        ├── 快捷条 / 汇总 KPI（可选，底距 20px）
        └── 核心功能区（Card：内距 22px 桌面 / 16px 移动，工具栏/筛选栏 + DataTable / 图表 / 表单）
              └── Footer（紧凑单行）
```

### 4.2 硬验收预算（基准页与后续页共守）

| 指标 | 桌面 `1440×900` | 移动 `390×844` | 极窄 `320×568` / `360×800` |
|---|---|---|---|
| 核心功能区顶部位置 | `<= 340px` | `<= 280px` | 首屏可见 |
| 页面总长 | 紧凑无大段空洞 | 列表页 `<= 2.35` 屏；总览 `<= 2.6` 屏；用户端主页 `<= 1.6` 屏 | 零横向溢出 |
| 横向溢出 `scrollWidth - clientWidth` | `0px` | `0px` | `0px` |
| 可点控件最小尺寸 | 同类统一 `32 / 40 / 48px` | `>= 44×44px` | `>= 44×44px` |

> 2026-11-24 间距恢复后复测（§2.8）：桌面最大 `coreTop` 为看板 `324px`、移动最大 `268px`；上界按原生 `32px` 页内距与 `22px` 卡内距重设，首屏仍完整可见 4 张 KPI 卡。列表页 `2.2→2.35`、总览 `2.2→2.6` 同为卡内距/页内距恢复后的实测增量（accounts `2.30`、总览 `2.59`），非版式回退。

### 4.3 基准页布局决策

- **总览（`/`）**：压缩顶栏、横幅与页头后，4 张 KPI 卡直接落于首屏上半区；中段两栏（容量体检 + 拥挤度）与 30 天趋势/失败明细收紧图表与内边距；底部系统健康与审计状态条桌面 `12px 22px`、移动 `12px 16px` 沉底。移动 `390×844` 下 KPI 保持 2×2 网格、图表降高至 `152/168px`、失败明细堆叠展示，整页控制在 `2.6` 屏以内。
- **账号（`/accounts`）**：页头主按钮与只读提示垂直居中对齐；卡内待审提示条与筛选栏紧凑排布（桌面全部控件精确 `40px` 基线对齐，移动搜索通栏 + 状态/学校 2 列并排 + 含已删除 48px 行）；桌面保持 48px 行高数据表，移动（`<= 720px`）自动转为 `.app-mobile-cards` 行卡式（每行一卡：姓名/手机/状态/元数据 + 48px 常驻操作按钮区，无需横滑）。桌面操作列只留一条状态主操作（待审→评审、正常→暂停、已暂停→恢复、驳回/凭据异常→验证），其余写动作收进「更多操作」菜单并逐条带置灰原因；置灰说明不再挂行内，改为表下 `.app-table-deny-bar` 汇总条（行级 `title` 同步可读），1440/1280 下表格零横向滚动。
- **用户端主页（`/app`）**：共享管理端纸面卡（`12px` 圆角、`22px` 内边距、统一边框阴影）与 6 档字号音阶（`12/14/16/20/28px`），全部交互按钮统一 `48px` 触控高度；桌面采用非对称双栏（左：今日 Hero + 近 7 天条，右：账号身份卡），移动单列紧凑直出，总长约 `1.2` 屏。

### 4.4 第 2 期管理端全页面骨架差异表

> 第 2 期未新增 `scale.css` 令牌，全部消费既有七组令牌；并将第 1 期在 `app.css` 与 `user-app.css` 末尾追加的覆盖层按语义收编回原规则处，两文件同上下文选择器重复数均归零。

| 页面 | 路由 | 桌面列数 / 主体结构 | 筛选栏 | 移动端（`<= 720px`）降级形态 |
|---|---|---|---|---|
| 待办 | `/queue` | 5 格顶部遥测条 + 三组独立分页队列卡（5 列栅格列表） | 每组自带分段切换 + 学校下拉 + 关键字搜索（40px/48px） | 遥测条 6 列双排（2+3）；队列行按 `@container (max-width: 760px)` 切为行卡式，操作键 48px 满行可达 |
| 签到台账 | `/logs` | 双页签（`签到事件` / `运行巡检`）：5 格/4 格摘要 + 学校泳道 + 折叠历史检索表 | 历史检索展开后为业务日/状态/学校/检索 + 导出 CSV | 摘要 2 列网格；泳道任务行流式排布，48px `诊断` 键与 `暂停引擎` 首屏直达 |
| 用户 | `/users` | 分组计数切换 + 8/9 列数据表（48px 行高） | 邮箱前缀 + 角色 + 状态 + 含已删除（`.app-filters`） | 搜索通栏 + 角色/状态双列；表格转 `.app-mobile-cards` 行卡式，邮箱加粗与 48px `管理` 键常驻 |
| 学校 | `/schools` | 6 列数据表（48px 行高）+ 详情抽屉 | 无 | 表格转 `.app-mobile-cards` 行卡式，学校名/状态与 48px `更多` 键常驻，单屏直出 |
| 审计日志 | `/audit` | 页头链状态结论 + 5 列语义表（48px 行高）+ 详情抽屉 | 动作 + 操作者 + 起止日期 + 清除（40px/48px） | 筛选单列堆叠（满足日期纵序契约）；表体 `.a-table-scroll` 内横滚，尾列 sticky 48px `详情` 键常驻视口 |
| 账号设置 | `/settings` | 2 列顶卡（账号信息 + 修改密码）+ 全宽通知订阅 4×3 矩阵 + 全宽 SMTP 3 列网格 | 无 | 通知矩阵每类切为标题 + 3 渠道横排 48px 开关；SMTP 切为 2 列紧凑表单（320px 退为 1 列） |
| 系统设置 | `/system` | 6 锚链导航 + 6 分区（计划表 / 策略 3+1 卡 / 公告与渠道并排 / 名册双列 / 版本自检） | 计划分区带日期输入 + 重新发现 | 计划表转行卡式；策略成对字段/开关维持 2 列（320px 退 1 列）；名册与版本紧致堆叠 |
| 组件画廊 | `/design` | 9 卡编排（按钮/表格/色板全宽，其余 6 卡两两配对无空洞） | 表格示例内置工具栏 | 表单与进度条 2 列排布（320px 退 1 列）；色板流式芯片排布；分页器自动折行零溢出 |

### 4.5 第 2 期实测指标表（Showcase 构建实测）

| 路由 | 1440×900 `coreTop` | 1440×900 屏数 | 390×844 `coreTop` | 390×844 屏数 | 9 视口最大横向溢出 |
|---|---:|---:|---:|---:|---:|
| `/` | `324px` | `1.93` | `268px` | `2.59` | `0px` |
| `/accounts` | `236px` | `1.10` | `244px` | `2.30` | `0px` |
| `/queue` | `246px` | `1.17` | `208px` | `1.97` | `0px` |
| `/logs` | `236px` | `1.32` | `188px` | `1.57` | `0px` |
| `/logs?tab=patrol` | `236px` | `1.40` | `188px` | `1.38` | `0px` |
| `/users` | `236px` | `1.00` | `244px` | `1.66` | `0px` |
| `/schools` | `236px` | `1.00` | `260px` | `1.00` | `0px` |
| `/audit` | `236px` | `1.02` | `240px` | `1.48` | `0px` |
| `/settings` | `236px` | `1.92` | `188px` | `2.24` | `0px` |
| `/system` | `280px` | `3.17` | `260px` | `6.07`（六分区全展开；单分区 0.38–2.05 屏） | `0px` |
| `/design` | `236px` | `3.28` | `232px` | `4.36`（`3679px`） | `0px` |

> **口径说明**：上表为 2026-11-24 间距恢复后复测（§2.8）；此前 2026-11-23（高度对齐 §2.3）同口径为 桌面 `1.77 / 1.03 / 1.00 / 1.00 / 1.12 / 1.19 / 1.00 / 1.79 / 2.90`、移动 `2.41 / 2.23 / 1.60 / 1.00 / 1.91 / 1.48 / 1.43 / 2.19 / 5.91`，差值即本次卡内距/页内距增量。9 视口横向溢出仍为 `0px`。

---

## 5. 用户端与门户规范（第 3 期收口）

> 第 3 期未新增 `scale.css` 令牌，全部消费既有七组令牌（含 `--text-display: 44px` 承载 404 展示码），且两张主样式表（`app.css`、`user-app.css`）同上下文选择器重复数保持为 `0`。

### 5.1 门户五页共用结构（`/login`、`/register`、`/forgot-password`、`/reset-password`、404）

```text
桌面 (>= 901px)：居中分栏舞台 .auth-stage（max-width: 984px，12px 圆角，纸面卡边框与阴影）
┌──────────────────────────────────┬──────────────────────────────────┐
│ .auth-aside（品牌 + 天空画布）    │ .auth-main（--bg-card 表面）      │
│  ├── 品牌标识 (32px, 8px 圆角)    │  ├── 顶栏：右侧主题开关 (40×40)   │
│  ├── H1 (28/36) + 导语 (14/20)    │  ├── 表单区 .auth-card            │
│  ├── 能力清单 / 注册三步引导      │  │     ├── 登录/注册分段 (40px)   │
│  └── 开源与加密脚注 (12/16)       │  │     ├── H2 (20/24) + 说明      │
│                                  │  │     ├── 字段 (40px) + 主按钮   │
│                                  │  │     └── 切换提示 (32px 按钮)  │
│                                  │  └── 页脚：版本键 (32px) + 链接   │
└──────────────────────────────────┴──────────────────────────────────┘

移动 (<= 900px，min-height: 100svh)：
[ 顶栏：品牌标识 + 主题开关 (48×48) ]
  └── 表单卡 .auth-card（12px 圆角、22px 内边距、所有控件 48px）
        └── 沉底页脚（版本键 48px + 协议链接）

404（NotFoundPage）：
[ .error-shell（min-height: 100svh）→ .error-main（max-width: 560px）→ .error-card（12px 圆角、16px 顶距） ]
  ├── 品牌徽标 (36×36, 8px 圆角)
  ├── 404 展示码（--text-display: 44px / 44px，禁止 96/160px）
  ├── 标题 (20/24) + 说明 (14/20)
  └── 返回入口按钮（桌面 40px / 移动 48px）
```

- **四页门户复用同一外壳 `AuthLayout`**：登录与注册共用双签面板；忘记密码与重置密码经 `PublicAuthPage` 复用同一 `AuthLayout`，视觉与状态反馈完全一致。
- **四态一致性**：字段错误使用 `border-color: var(--danger)` + `12px` 说明；加载态采用骨架行 `.app-skeleton-lines`；提交中按钮展示进行中文案并禁用；所有禁用态统一 `opacity: 0.45`。
- **暗色天空画布**：暗色主题下 `AuthAside` 自动切入 `SKY_PALETTES.night`（`data-on-dark="true"`），配深底渐变衬底与高对比文字；`prefers-reduced-motion: reduce` 下冻结单帧不跑 `rAF`。

### 5.2 用户端七页骨架（`/app/*`）

```text
[ 固定顶栏 .uapp-bar（52px 移动 / 56px >=768px） ]
  └── .uapp-bar-inner（max-width: 1120px 居中：品牌 + 主页/历史/设置 + 主题键 + 头像菜单）
[ 主内容 .uapp-main → .uapp-content → .uapp-page（唯一容器 max-width 1120，居中，左右留白差 0px） ]
  ├── 页头（眉标 12px + H1 20/24 移动 / 28/36 桌面，七页同构）
  └── 核心内容网格（桌面双栏填充全宽；移动单列紧致堆叠，coreTop <= 130px）
[ 移动底栏 .uapp-bottom（< 768px 显示，60px，主页 / 历史 / 设置三签） ]
```

| 路由 | 容器宽度（桌面 `1440×900`） | 桌面形态（`>= 1024px`） | 移动形态（`<= 720px`） |
|---|---|---|---|
| `/app` | `1120px` | 非对称双栏 `1.72fr : 1fr`（左：今日 Hero + 近 7 天；右：账号身份卡 `width: 100%` 满栏填充） | 单列紧致堆叠，总长约 `1.21` 屏 |
| `/app/history` | `1120px` | 双栏 `minmax(320px, 440px) : 1fr`（左：月历，日格上限 `56px`；右：当天明细，日期 `44px` 展示字） | 单列堆叠；日格 `<= 403px` 视口自适应收缩，`320–430px` 全档零溢出；总长 `1.00` 屏 |
| `/app/settings` | `1120px` | 等高双栏（左：偏好；右：账号与安全），DOM 与视觉同序 | 单列堆叠（偏好在前、账号与安全在后），`coreTop = 120px`，总长 `1.00` 屏 |
| `/app/account` | `1120px` | 通栏身份 Hero + 等宽双栏（左：资料/设备/照片；右：签到概览/控制） | 页头眉标＋标题与返回同行；设备只读双列与工具键双列紧致排布，总长 `<= 1.83` 屏 |
| `/app/account/new` | `1120px` + 宽弹窗 | 账号页 + `AccountWizard` 三步向导弹窗（`scrollBody`） | 同向导弹窗，全控件 `48px` |
| `/app/settings/time-pref` | `1120px` + 宽弹窗 | 设置页 + `TimePrefDialog` 横滑 5 分钟刻度尺（`48px` 档高） | 同弹窗，方向键/横滑可达，关闭归位 `/app/settings` |
| `/app/settings/password` | `1120px` + 标准弹窗 | 设置页 + `PasswordDialog`（实时策略校验 + 会话过期回跳 `/login`） | 同弹窗，全控件 `48px`，关闭归位 `/app/settings` |

**5.2.1 三处只允许有一个值（2026-11-23 收口）**

1. **页面容器**：七页共用 `.uapp-page { max-width: var(--container-wide) }`，不再按页面给 `984px`/`1040px` 等变体。理由：切页时页头与卡片左边界必须不跳；旧的三段媒体覆盖（`>=1024`、`>=1280`）会在 `1279→1280` 之间把容器从 `984` 跳到 `1120`，同一页在相邻两档视口里左右各差 `68px`。新建/编辑账号走弹窗向导，不再需要窄表单页，故 `.uapp-page--form` 已删除。
2. **月历几何**：`.uapp-cal-weekdays` 与 `.uapp-cal-grid` 都是 `repeat(7, minmax(0, 1fr))` 且 `max-width: 404px`（`7×56 + 6×2`，居中）。桌面卡片再宽也不把日格拉成 `96×48` 的扁条；`<= 403px` 视口（`7×48 + 6×2 = 348` 放不下时）单元格解除 `min-width` 收缩，避免末列顶出卡片右边框。
3. **卡片间距**：堆叠与分栏都只允许一个来源。`.uapp-card + .uapp-card { margin-top: var(--space-3) }` 只服务竖排堆叠；四张网格容器（`.uapp-home-bento`/`.uapp-history-grid`/`.uapp-settings-grid`/`.uapp-account-grid`）的直接子卡一律 `margin-top: 0`，间距只由 `gap` 给。理由：网格右栏第一张卡也是相邻兄弟，会同时吃到 `gap` 与兄弟外边距 —— `1440` 历史页与设置页右栏曾比左栏低 `12px`（`156 vs 144`、`160 vs 148`），设置页移动端间距也因此从 `24px` 回到 `12px`。历史页网格改常驻单列（`gap: 12px`），否则清掉外边距后两张卡会贴在一起。

### 5.3 门户与用户端令牌映射（桌面 vs 移动）

| 控件类别 | 桌面（`>= 721px`） | 移动（`<= 720px`） | 圆角 | 字号 / 行高 |
|---|---|---|---|---|
| 顶栏图标键 / 头像 / 主题开关 / 导航胶囊 | `40px`（`--ctrl-h-md`） | `48px`（`--ctrl-h-lg`） | `8px` / `999px` | `14px / 20px` |
| 标准按钮（`.btn` / `.uapp-button` / 分段签） | `40px`（`--ctrl-h-md`）；主提交 CTA 可取 `48px` | `48px`（`--ctrl-h-lg`） | `8px` / `6px` | `14px / 20px` |
| 紧凑操作键 / 行内链接键 / 开关 / 页脚版本键 | `32px`（`--ctrl-h-sm`，`::after` 外扩至 `>=44px` 热区） | `48px`（`--ctrl-h-lg`） | `6px` / `8px` / `999px` | `12px / 16px` |
| 双行列表行按钮 / 月历日格 / 时间片刻度 | `48px`（`--ctrl-h-lg`） | `48px`（`--ctrl-h-lg`） | `8px` / `6px` | `14px / 20px` + `12px / 16px` |
| 文本 / 密码输入框（`.input` / `.uapp-input`） | `40px`（`--ctrl-h-md`） | `48px`（`--ctrl-h-lg`） | `8px`（`--radius-md`） | `14px / 20px` |
| 卡片与门户舞台表面 | 顶内距 `22px`（`--card-pad`） | 管理端 `16px` / 用户端 `22px`（`--card-pad`） | `12px`（`--radius-lg`） | 标题 `16/24` 或 `20/24` |
| 键盘焦点环（`:focus-visible`） | `2px solid var(--primary)` + `0 0 0 3px var(--primary-soft)` | 同桌面 | 继承控件 | — |
| 禁用态（`:disabled`） | `opacity: 0.45; cursor: not-allowed` | 同桌面 | — | — |

---

## 6. 持续维护约束

后续迭代管理端、用户端或门户任何页面时，必须遵守：

1. **禁止引入新尺寸阶**：只消费 `scale.css` 变量，不新增字号、圆角、按钮高度或卡片内边距档位。
2. **禁止新增颜色字面量**：所有色值走 `tokens.css` 与 `color-mix`，改完必跑 `npm run gate:tokens`。
3. **不破坏契约与测试**：保留文案、`aria-label`、`data-testid`、路由与行高/密度契约，每次修改后执行 `npm run verify` 与对应 behavior spec。
4. **零重复选择器**：`app.css` 与 `user-app.css` 同一媒体上下文下不得出现重复选择器。

---

## 7. 一致性契约（对齐矩阵 / 交互范式 / 密度预算）

> 本节把 Adminator 的原生排版规则翻译成可执行判据，是跨页「看起来一样」的判定依据。
> 机器复跑：`node scripts/consistency-check.mjs`（见 §7.5）。

### 7.1 列对齐矩阵（表头与单元格必须同向）

Adminator 的原生分工是**价格/数量右、日期/编码左**。逐表落地：

| 表 | 左对齐列 | 右对齐列 |
|---|---|---|
| `/` 今日失败明细 | 失败结果、处置建议 | 条数、占比（`tabular-nums`；`<= 640px` 堆叠态恢复左） |
| `/accounts` 账号表 | 备注名、手机号、机型、学校、状态、邮箱、更新时间、删除时间 | 操作 |
| `/users` 用户表 | 邮箱、角色、状态、需要改密、创建时间、上次登录 | 「账号」数量列（倒数第 5 列，首列复选框显隐不影响）、操作 |
| `/schools` 学校表 | 学校、代码、状态、时区、更新时间 | 操作 |
| `/queue` 待办三组 | 全部业务列 | 操作 |
| `/logs` 签到事件 / 在飞 / 待重试 | 时间、状态、账号、学校、任务、结果 | 在飞表「租约剩余」、待重试表「已试」、事件表「诊断」尾列 |
| `/audit` 审计表 | 时间、动作、操作者、对象、详情预览 | 「详情」表头（按钮贴右，预览文本自身仍左） |
| `/system` 计划表 | 学校、任务、状态、窗口、来源 | 操作 |
| `/settings` 通知矩阵 | 通知项名称 | 2–4 列开关居中（矩阵语义，不套左右对齐） |

**规则**：新增数值/计数/时长列 → 右对齐 + `font-variant-numeric: tabular-nums`；
新增时间戳、手机号、定长编码 → 左对齐，禁止为了「看起来齐」把时间右推。

### 7.2 交互范式（同一类动作同一落位）

1. **主按钮永远最右**。动作组统一 `display:flex; flex-wrap:wrap; justify-content:flex-end`；
   说明/置灰原因/未保存提示在左，且 `margin-right:auto` 吃余量，不得把主按钮顶离右锚点。
2. **表格行内动作**：`[详情(ghost)] → [次要(ghost)] → [主操作(primary)]`；
   行卡（`<= 640px`）同一右贴，原因文案 `text-align:right`。
3. **卡脚 = 弹窗页脚**：`[状态说明][次要][主按钮]`；`/system` 四张策略卡、公告卡、渠道卡全部同向。
4. **页头动作**：`[置灰原因][主按钮]`，主按钮贴右；`/accounts`、`/users`、`/schools` 三页同构。
5. **键值列表**：左键右值（`dd { text-align:right }`），覆盖账号抽屉、审计详情、队列诊断、日志诊断。

### 7.3 形态契约（表面层次）

| 层 | 底色 | 说明 |
|---|---|---|
| 卡头 `.card-head` / `.q-card-head` / `.a-card-head` | `var(--bg-card)` | 卡头不铺灰；层次靠标题字重与 `border-bottom` |
| 列表列头带 `.q-list-head` / `.a-list-head` | `--bg-hover` 混色 | 灰底只留给列头带，语义唯一 |
| 分页脚 `.q-pager-foot` / `.a-pager-foot` | `var(--bg-card)` | 与 `.data-foot` 同表面 |

**三态高度**：表内空/载/错单格行 `height:144px; text-align:center`（= 3 × 48px 表行）；
卡内 `.app-async-state/.app-async-skeleton` `min-height:96px` 且内容垂直居中；
`.q-empty` 与 `.a-empty` 统一 `144px`——切换筛选时不塌、不跳。

### 7.4 密度预算（1440×900，屏数 = 文档高 ÷ 视口高）

| 页面类型 | 预算 | 当前 |
|---|---:|---:|
| 列表（accounts/users/schools/audit） | ≤ 1.15 | 1.00–1.10 |
| 工作台（queue/logs） | ≤ 1.45 | 1.17–1.40 |
| 看板（dashboard） | ≤ 2.00 | 1.93 |
| 设置（settings） | ≤ 2.00 | 1.92 |
| 多分区（system） | ≤ 3.20 | 3.17（六分区，三处分区已内滚/三列化） |
| 移动（390×844） | ≤ 2.60 | 列表 1.00–2.30、工作台 1.38–1.97、system 6.07（单列后撤销内滚，见下） |

**例外说明**：`/system` 是六个独立分区的设置中枢（计划/策略/公告/渠道/名册/版本），
每分区自身 ≤ 0.5 屏且页顶锚链可达；`.sys-roster-groups`（260px 内滚）、
`#plans .table-scroll`（240px 内滚）已把名册与窗口表收紧；`<= 1024px` 两者一起撤销限高，
窄屏交给页面滚动（行卡变高时内滚会切在行中间，触控也不该出现嵌套滚动）。要继续压到 1.8 屏只能折叠分区，
与 `system-roster.spec.ts` 的「三组同时可见、零按钮」守卫冲突，故不做。

**看板预算由 `1.80` 放宽至 `2.00`**：按 Adminator 原生恢复 `22px` 卡内距与 `32px` 页内距后实测 `1.93` 屏（原 `1.77`）；增量来自呼吸感而非版式回退，首屏 4 张 KPI 仍完整可见。设置预算同步由 `1.60` 放宽至 `2.00`（实测 `1.92`），多分区由 `1.80` 放宽至 `3.20`（实测 `3.17`，折叠约束见下）。

### 7.5 复跑与判定

```bash
cd ui
npm run build:showcase                 # 产出 dist-showcase
npm run preview:showcase &             # 4174
node scripts/consistency-check.mjs     # 输出 /tmp/ui-consistency.json + 逐行判定
```

判定项：`mismatch`（表头/单元格同向）、`actionLeft`（操作尾列右贴）、
`numLeft`（数值列右对齐）、`primNotLast`（主按钮最右 + 动作组右贴）、
`badH`（控件高度 % 4）、`screens`（屏数）。**验收线：前四项为 0**；
`badH` 仅允许原生 checkbox/radio 在未显式设尺寸时出现（现已全部归 16px）；
移动端失败明细堆叠态的「左对齐数值」与 `/queue`、`/audit` 的列表列头灰底属**白名单**，
断言器会各计一次，不视为违规。

### 7.6 间距/圆角归位

- 横向组间距 `10px` → `var(--space-2)`；非标圆角 `10px` → `var(--radius-md)`。
- 功能性小值豁免：`1px` 描边、`2px/3px` 徽章点与内胆、`6px` 分段容器内距、月历日格内距。
- 收件箱图标盒与移动端色板等一律取 `--ctrl-h-sm` / 4 的倍数（如 14px → 16px）。

---

## 8. 本轮一致性收口的改动索引

| 文件 | 改动 |
|---|---|
| `ui/src/styles/scale.css` | 控件基准对齐 Adminator 原生实测（§2.3）：`--ctrl-h-md` 36→40、`--ctrl-h-lg` 44→48，新增表头内边距令牌 `--table-head-pad-top/bottom` 10/12px（`--table-row-h` 保持 48px） |
| `ui/src/styles/app.css` + `features/{audit,queue,system}/*.css` | 表头内边距改用新令牌；注释里的 36/44 同步为 40/48 |
| `ui/src/styles/app.css` | 操作尾列 `.app-col-action` 右贴；`.app-row-actions/.app-account-actions` flex-end；users 账号列右；失败表数值右（窄屏复位）；运行表数值/诊断右；抽屉 `dd` 右；三态 144/96px；筛选复选框 16px；`gap:10px`/`radius:10px` 归令牌 |
| `ui/src/features/{accounts,user,schools,logs,system/plans}/` | 表头/单元格补 `app-col-action`；页头原因前置 |
| `ui/src/features/queue/QueueView.tsx` | 待审 `[详情][驳回][通过]`、失败 `[详情][重试]` |
| `ui/src/features/queue/queue.css` + `features/audit/audit.css` | 卡头/分页脚白底；`.q-empty` 144px；审计尾列表头右 |
| `ui/src/features/system/` | 四卡脚 [原因][未保存][次][主]；渠道卡原因前置；名册三列 260px 内滚；计划表 240px 内滚；调度三列；版本三列；单选 16px |
| `ui/src/features/logs/EventsTab.tsx` | 页脚升级 `.data-foot`（第 X 页 · 本页 Y 条） |
| `ui/scripts/consistency-check.mjs` | 一致性断言器（新增） |

