# 易班自动签到 · 新线设计系统规范（Design System v1）

> 归属：前端视觉与交互唯一基准（与 `ui/src/styles/scale.css`、`ui/src/styles/tokens.css`、`ui/DESIGN-CONTRACT.md` 配套）。
> 适用范围：第 1 期（基准阶、共用组件、外壳、`/` 总览、`/accounts` 账号、`/app` 用户端主页），并作为第 2/3 期全站页面改造的执行准绳。

---

## 1. 设计人格与双端同源

管理端（`/` 等）与用户端（`/app` 等）不是两个割裂产品，而是**同一视觉母体的两种密度**：

- **同一表面语言**：纸面卡片统一使用 `var(--bg-card)` + `1px solid var(--border)` + `var(--shadow-card)` + `var(--radius-lg)`（`12px`），画布底统一使用 `var(--bg-body)`。
- **同一语义色体系**：严禁在任何非豁免文件写入颜色字面量，全部消费 `tokens.css` 的 36 浅 + 36 深令牌及 `color-mix(in srgb, ...)` 派生。
- **密度分档而不换骨**：
  - **管理端（桌面）**：默认控件高度 `36px`，行内/分页/紧凑操作 `32px`，表格标准行高 `48px`（紧凑模式 `40px`），追求高信息吞吐与首屏直达。
  - **移动端（`<= 720px`）与用户端（`/app`）**：所有交互控件统一抬至 `44px` 触控下限，留白更松弛，保持清晰的视觉焦点与单手可达性。

---

## 2. 尺寸基准阶（`ui/src/styles/scale.css`）

### 2.1 间距阶（4px 基准）

全站外边距、内边距、栅格沟槽与元素间距一律取 4 的倍数；禁止新增 5 / 7 / 9 / 11 / 13 / 18 / 22 等散落数值。

| 令牌 | 像素值 | 典型用途 |
|---|---:|---|
| `--space-0` | `0px` | 重置 |
| `--space-1` | `4px` | 紧贴微间距（图标与计数、表单字段内标签距输入、分段容器内边距） |
| `--space-2` | `8px` | 控件组间距、紧致行内操作、移动端页面上下边距、紧凑横幅内边距 |
| `--space-3` | `12px` | 区块间距（移动）、卡片头分隔、工具栏/筛选栏间距、指标格内边距 |
| `--space-4` | `16px` | 标准卡片内边距、桌面栅格沟槽、弹窗/抽屉内边距、桌面页面顶距 |
| `--space-5` | `20px` | 桌面主内容底距、大按钮水平内边距 |
| `--space-6` | `24px` | 桌面内容区左右留白、桌面顶栏左右内边距 |
| `--space-8` | `32px` | 图标输入左留空 / 下拉框右箭头留空 |
| `--space-10` | `40px` | 桌面 7 天日历格、表格紧凑行高 |
| `--space-12` | `48px` | 表格标准行高、用户端身份照片头像 |

### 2.2 控件高度三档

同类控件在同一视口下**只能有一个高度**；不得再由 `padding + line-height` 自由撑出非整数高度（如 20.1 / 24.6 / 31.8 / 37.5 / 40.1px）。

| 令牌 | 像素值 | 适用组件（桌面 `> 720px`） | 移动端（`<= 720px`） |
|---|---:|---|---|
| `--ctrl-h-sm` | `32px` | `.btn--sm`、表内操作键、行内更多图标键、`.app-cell-link`、`.pager-btn`、`.alert .close`、`.app-card-tabs button`、`.tabs.pills .tab` | 统一抬升至 `44px` |
| `--ctrl-h-md` | `36px` | `.btn`（默认）、`.input`、`.select`、`.app-select-trigger`、顶栏 `.cmd` / `.icon-btn` / `.avatar`、侧栏 `.nav-link`、`.tab`、`.dd-menu-item` | 统一抬升至 `44px` |
| `--ctrl-h-lg` | `44px` | `.btn--lg`、用户端全部按钮（`.uapp-button` / `.uapp-icon-btn` / `.uapp-avatar` / `.uapp-hero-link` / `.uapp-seg-item` / `.uapp-announcement-close`）与 `.uapp-input` | 全部可点控件基准 |
| `--ctrl-h-textarea` | `96px` | `.textarea` 多行文本域最小高度 | `96px` |

> **例外契约**：弹窗/抽屉右上关闭键 `.mail-tool` 按既有行为测试保留 `24×24` 视觉盒，并通过 `::after { inset: -10px; }` 扩展至 `44×44` 触控热区。

### 2.3 字号与行高音阶（六档整数）

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

### 2.4 圆角四档

以 `999px`（`--radius-full`）统一表达胶囊与正圆，**禁止再写 `50%`**，消除两套圆形计算值。

| 令牌 | 像素值 | 适用元素 |
|---|---:|---|
| `--radius-sm` | `6px` | 微件：`.kbd`、`.nav-badge--count`、`.app-health-chip`、`.mail-tool`、`.alert .close`、`.check .box`、`.app-select-item`、`code`、表行图标按钮 |
| `--radius-md` | `8px` | 控件与小容器：`.btn`、`.input`、`.select`、`.textarea`、`.cmd`、`.icon-btn`、`.pager-btn`、侧栏 `.nav-link`/`.workspace`、`.brand-logo`、`.kpi-icon`、`.alert`、`.app-pending-banner`、`.app-metric`、`.app-batch-bar`、用户端 `.uapp-button`/`.uapp-input`/`.uapp-day-cell`/`.uapp-hero-photo` |
| `--radius-lg` | `12px` | 主容器与浮层：`.card`、`.kpi-card`、`.app-health-strip`、`.modal-demo`、`.dd-menu`、`.app-select-content`、`.app-search-panel`、用户端 `.uapp-hero`/`.uapp-card` |
| `--radius-full` | `999px` | 胶囊与正圆：`.badge`、`.tag`、`.kpi-pill`、`.avatar`、`.workspace-avatar`、`.uapp-avatar`、`.uapp-top-link`、`.uapp-status`、`.switch .track`、`.progress`、`.app-card-tabs button`、状态圆点 |

### 2.5 卡片内边距（两档）

| 令牌 | 像素值 | 适用容器 |
|---|---:|---|
| `--card-pad` | `16px` | 全部标准 `.card`、`.kpi-card`、用户端 `.uapp-card`、`.uapp-hero` / `.uapp-result` |
| `--card-pad-sm` | `12px` | 紧凑单行状态条 `.app-health-strip`（`padding: 12px 16px`） |

> 显式切换到紧凑密度（`html[data-density="compact"]`）时，桌面 `.card` 按契约收缩为 `14px`，行高 `48px → 40px`。

### 2.6 外壳与容器尺寸

- 管理台侧栏：`--sidebar-w: 240px`（平板 `721–1100px` 收窄为 `--sidebar-w-rail: 68px`；移动 `<= 720px` 抽屉化）
- 管理台顶栏：`--topbar-h: 52px`（桌面）、`48px`（移动 `<= 720px`）
- 管理台内容区：`.content` 不设 `max-width`（全宽表格需要横向吞吐），内边距桌面 `16px 24px 20px`，移动 `8px 12px 16px`
- 用户端顶栏：`--uapp-bar-h: 52px`（移动）、`--uapp-bar-h-wide: 56px`（`>= 768px`）；底栏 `--uapp-bottom-h: 60px`
- 用户端内容最大宽：主页 `984px`、历史 `1120px`、设置 `760px`、表单 `560px`

---

## 3. 基础组件规格（`ui/src/components/ui/`）

1. **Button / BusyButton**
   - 默认 `md`（`36px`）、紧凑 `sm`（`32px`）、大号 `lg`（`44px`）、图标 `icon`（方框与档位同高）；`<= 720px` 统一 `44px`。
   - **交互态**：`hover` 仅动底色与边框；`focus-visible` 统一 `2px solid var(--primary)` + `2px` 外偏；`disabled` 统一 `opacity: 0.48; cursor: not-allowed`；`danger` 浅色走 `color-mix(in srgb, var(--danger) 75%, var(--t-base))` 保 WCAG AA，暗色走 `var(--danger)` 配 `var(--t-inverse)`。
2. **Input / Select / TextArea / FilterField / Switch / Checkbox**
   - 单行输入与原生/Radix 下拉统一 `36px`（桌面）/ `44px`（移动与用户端），`--radius-md`，`14px` 正文字号。
   - 聚焦态统一 `border-color: var(--primary)` + `box-shadow: 0 0 0 3px var(--primary-ring)`；错误态统一 `border-color: var(--danger)` + `12px` 错误提示。
   - 字段壳（`FieldShell` / `FilterField`）间距 `--space-1`（`4px`），标签与帮助文字统一 `12px`。
3. **Badge / Tag**
   - 统一 `12px / 16px`，`--radius-full`，状态点采用双载体（圆点 + 文字），浅色软底文字全部经 `color-mix` 压深满足 AA 对比度。
4. **Card / PageHeader**
   - `Card` 内边距 `16px`，圆角 `12px`；`.card-head` 底部留白 `12px`，标题 `16px` 粗体，眉标 `12px`。
   - `PageHeader`（`.hero`）采用左右居中对齐、底距 `12px`（移动 `8px`），标题固定 `28px`，副标题桌面 `14px` / 移动 `12px`，右侧动作组垂直居中对齐。
5. **Alert / 状态横幅**
   - 普通内联 `Alert` 采用 `12px 16px` 内边距与 `8px` 圆角；顶部系统横幅（`.app-alert-block` / `.app-announcement-banner`）采用 `8px 12px` 单行流式排布，避免多行横幅挤掉首屏核心区。
6. **DataTable / SelectionBar / Pager**
   - 表头 `th` 统一 `12px`、行体 `td` 统一 `14px`、等宽编号/时间 `.data-cell-mono` 统一 `12px`。
   - 行高 `48px`（紧凑 `40px`），窄屏由 `.table-scroll` 内部横向滚动，页面本身零横向溢出。
   - 分页按钮 `.pager-btn` 桌面 `32px` / 移动 `44px`，圆角 `8px`。
7. **Tabs / Menu / Modal / Drawer / Hint**
   - 浮层统一 `--radius-lg`（`12px`）+ `var(--shadow-lg)` + `1px solid var(--border)`；菜单项与下拉项桌面 `36px`、圆角 `6px`。
   - 弹窗与抽屉头/体/脚内边距统一 `16px`，标题 `16px`，正文 `14px`。

---

## 4. 页面骨架与首屏预算

### 4.1 标准管理页骨架

```text
[ Topbar (52px 桌面 / 48px 移动) ]
  └── Main Content (pad: 16px 24px 桌面 / 8px 12px 移动)
        ├── 系统横幅（可选，行内紧凑 ≤ 40px）
        ├── PageHeader（眉标 + H1 + 一行说明 + 右侧主操作，底距 12px）
        ├── 快捷条 / 汇总 KPI（可选，底距 16px 桌面 / 12px 移动）
        └── 核心功能区（Card：工具栏/筛选栏 + DataTable / 图表 / 表单）
              └── Footer（紧凑单行）
```

### 4.2 硬验收预算（基准页与后续页共守）

| 指标 | 桌面 `1440×900` | 移动 `390×844` | 极窄 `320×568` / `360×800` |
|---|---|---|---|
| 核心功能区顶部位置 | `<= 300px` | `<= 260px` | 首屏可见 |
| 页面总长 | 紧凑无大段空洞 | 列表页 `<= 2.2` 屏；总览 `<= 2.6` 屏；用户端主页 `<= 1.6` 屏 | 零横向溢出 |
| 横向溢出 `scrollWidth - clientWidth` | `0px` | `0px` | `0px` |
| 可点控件最小尺寸 | 同类统一 `32 / 36 / 44px` | `>= 44×44px` | `>= 44×44px` |

### 4.3 基准页布局决策

- **总览（`/`）**：压缩顶栏、横幅与页头后，4 张 KPI 卡直接落于首屏上半区；中段两栏（容量体检 + 拥挤度）与 30 天趋势/失败明细收紧图表与内边距；底部系统健康与审计状态条用 `12px 16px` 沉底。移动 `390×844` 下 KPI 保持 2×2 网格、图表降高至 `152/168px`、失败明细堆叠展示，整页控制在 `2.2` 屏以内。
- **账号（`/accounts`）**：页头主按钮与只读提示垂直居中对齐；卡内待审提示条与筛选栏紧凑排布（桌面全部控件精确 `36px` 基线对齐，移动搜索通栏 + 状态/学校 2 列并排 + 含已删除 44px 行）；表内操作按钮桌面统一 `32px`、移动统一 `44px`，彻底清零 20.1 / 25 / 30 / 31.8px 离散值。
- **用户端主页（`/app`）**：共享管理端纸面卡（`12px` 圆角、`16px` 内边距、统一边框阴影）与 6 档字号音阶（`12/14/16/20/28px`），全部交互按钮统一 `44px` 触控高度；桌面采用非对称双栏（左：今日 Hero + 近 7 天条，右：账号身份卡），移动单列紧凑直出，总长约 `1.2` 屏。

---

## 5. 第 2 / 3 期执行约束

后续改造 `/queue`、`/logs`、`/users`、`/schools`、`/audit`、`/settings`、`/system`、`/design` 及用户端其余页面时，必须遵守：

1. **禁止引入新尺寸阶**：只消费 `scale.css` 变量，不新增字号、圆角、按钮高度或卡片内边距档位。
2. **禁止新增颜色字面量**：所有色值走 `tokens.css` 与 `color-mix`，改完必跑 `npm run gate:tokens`。
3. **不破坏契约与测试**：保留文案、`aria-label`、`data-testid`、路由与行高/密度契约，每次修改后执行 `npm run verify` 与对应 behavior spec。
