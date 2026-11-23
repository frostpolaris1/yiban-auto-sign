# 易班自动签到 · 新线设计系统规范（Design System v1）

> 归属：前端视觉与交互唯一基准（与 `ui/src/styles/scale.css`、`ui/src/styles/tokens.css`、`ui/DESIGN-CONTRACT.md` 配套）。
> 适用范围：第 1 期（基准阶、共用组件、外壳、`/` 总览、`/accounts` 账号、`/app` 用户端主页）、第 2 期（覆盖层收编、`/accounts` 移动端行卡化、`/queue`、`/logs`、`/users`、`/schools`、`/audit`、`/settings`、`/system`、`/design` 全部管理端页面）与第 3 期（门户五页 `/login`、`/register`、`/forgot-password`、`/reset-password`、404 与用户端七页 `/app/*` 全量收口）。

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
   - **交互态**：`hover` 仅动底色与边框；`focus-visible` 统一 `2px solid var(--primary)` + `2px` 外偏；`disabled` 统一 `opacity: 0.45; cursor: not-allowed`（与全站 e2e 禁用降级契约一致）；`danger` 浅色走 `color-mix(in srgb, var(--danger) 75%, var(--t-base))` 保 WCAG AA，暗色走 `var(--danger)` 配 `var(--t-inverse)`。
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
- **账号（`/accounts`）**：页头主按钮与只读提示垂直居中对齐；卡内待审提示条与筛选栏紧凑排布（桌面全部控件精确 `36px` 基线对齐，移动搜索通栏 + 状态/学校 2 列并排 + 含已删除 44px 行）；桌面保持 48px 行高数据表，移动（`<= 720px`）自动转为 `.app-mobile-cards` 行卡式（每行一卡：姓名/手机/状态/元数据 + 44px 常驻操作按钮区，无需横滑）。
- **用户端主页（`/app`）**：共享管理端纸面卡（`12px` 圆角、`16px` 内边距、统一边框阴影）与 6 档字号音阶（`12/14/16/20/28px`），全部交互按钮统一 `44px` 触控高度；桌面采用非对称双栏（左：今日 Hero + 近 7 天条，右：账号身份卡），移动单列紧凑直出，总长约 `1.2` 屏。

### 4.4 第 2 期管理端全页面骨架差异表

> 第 2 期未新增 `scale.css` 令牌，全部消费既有七组令牌；并将第 1 期在 `app.css` 与 `user-app.css` 末尾追加的覆盖层按语义收编回原规则处，两文件同上下文选择器重复数均归零。

| 页面 | 路由 | 桌面列数 / 主体结构 | 筛选栏 | 移动端（`<= 720px`）降级形态 |
|---|---|---|---|---|
| 待办 | `/queue` | 5 格顶部遥测条 + 三组独立分页队列卡（5 列栅格列表） | 每组自带分段切换 + 学校下拉 + 关键字搜索（36px/44px） | 遥测条 6 列双排（2+3）；队列行按 `@container (max-width: 760px)` 切为行卡式，操作键 44px 满行可达 |
| 签到台账 | `/logs` | 双页签（`签到事件` / `运行巡检`）：5 格/4 格摘要 + 学校泳道 + 折叠历史检索表 | 历史检索展开后为业务日/状态/学校/检索 + 导出 CSV | 摘要 2 列网格；泳道任务行流式排布，44px `诊断` 键与 `暂停引擎` 首屏直达 |
| 用户 | `/users` | 分组计数切换 + 8/9 列数据表（48px 行高） | 邮箱前缀 + 角色 + 状态 + 含已删除（`.app-filters`） | 搜索通栏 + 角色/状态双列；表格转 `.app-mobile-cards` 行卡式，邮箱加粗与 44px `管理` 键常驻 |
| 学校 | `/schools` | 6 列数据表（48px 行高）+ 详情抽屉 | 无 | 表格转 `.app-mobile-cards` 行卡式，学校名/状态与 44px `更多` 键常驻，单屏直出 |
| 审计日志 | `/audit` | 页头链状态结论 + 5 列语义表（48px 行高）+ 详情抽屉 | 动作 + 操作者 + 起止日期 + 清除（36px/44px） | 筛选单列堆叠（满足日期纵序契约）；表体 `.a-table-scroll` 内横滚，尾列 sticky 44px `详情` 键常驻视口 |
| 账号设置 | `/settings` | 2 列顶卡（账号信息 + 修改密码）+ 全宽通知订阅 4×3 矩阵 + 全宽 SMTP 3 列网格 | 无 | 通知矩阵每类切为标题 + 3 渠道横排 44px 开关；SMTP 切为 2 列紧凑表单（320px 退为 1 列） |
| 系统设置 | `/system` | 6 锚链导航 + 6 分区（计划表 / 策略 3+1 卡 / 公告与渠道并排 / 名册双列 / 版本自检） | 计划分区带日期输入 + 重新发现 | 计划表转行卡式；策略成对字段/开关维持 2 列（320px 退 1 列）；名册与版本紧致堆叠 |
| 组件画廊 | `/design` | 9 卡编排（按钮/表格/色板全宽，其余 6 卡两两配对无空洞） | 表格示例内置工具栏 | 表单与进度条 2 列排布（320px 退 1 列）；色板流式芯片排布；分页器自动折行零溢出 |

### 4.5 第 2 期实测指标表（Showcase 构建实测）

| 路由 | 1440×900 `coreTop` | 1440×900 屏数 | 390×844 `coreTop` | 390×844 屏数 | 9 视口最大横向溢出 |
|---|---:|---:|---:|---:|---:|
| `/` | `292px` | `1.76` | `224px` | `2.39` | `0px` |
| `/accounts` | `204px` | `1.00` | `200px` | `2.15` | `0px` |
| `/queue` | `216px` | `1.11` | `168px` | `1.86` | `0px` |
| `/logs` | `204px` | `1.18` | `148px` | `1.45` | `0px` |
| `/logs?tab=patrol` | `204px` | `1.22` | `148px` | `1.25` | `0px` |
| `/users` | `204px` | `1.00` | `200px` | `1.61` | `0px` |
| `/schools` | `204px` | `1.00` | `216px` | `1.00` | `0px` |
| `/audit` | `208px` | `1.00` | `204px` | `1.37` | `0px` |
| `/settings` | `205px` | `1.76` | `151px` | `2.13` | `0px` |
| `/system` | `248px` | `3.09` | `216px` | `5.77`（六分区全展开；单分区 0.38–2.05 屏） | `0px` |
| `/design` | `207px` | `2.95` | `201px` | `4.00`（`3375px`） | `0px` |

---

## 5. 用户端与门户规范（第 3 期收口）

> 第 3 期未新增 `scale.css` 令牌，全部消费既有七组令牌（含 `--text-display: 44px` 承载 404 展示码），且两张主样式表（`app.css`、`user-app.css`）同上下文选择器重复数保持为 `0`。

### 5.1 门户五页共用结构（`/login`、`/register`、`/forgot-password`、`/reset-password`、404）

```text
桌面 (>= 901px)：居中分栏舞台 .auth-stage（max-width: 984px，12px 圆角，纸面卡边框与阴影）
┌──────────────────────────────────┬──────────────────────────────────┐
│ .auth-aside（品牌 + 天空画布）    │ .auth-main（--bg-card 表面）      │
│  ├── 品牌标识 (32px, 8px 圆角)    │  ├── 顶栏：右侧主题开关 (36×36)   │
│  ├── H1 (28/36) + 导语 (14/20)    │  ├── 表单区 .auth-card            │
│  ├── 能力清单 / 注册三步引导      │  │     ├── 登录/注册分段 (36px)   │
│  └── 开源与加密脚注 (12/16)       │  │     ├── H2 (20/24) + 说明      │
│                                  │  │     ├── 字段 (36px) + 主按钮   │
│                                  │  │     └── 切换提示 (32px 按钮)  │
│                                  │  └── 页脚：版本键 (32px) + 链接   │
└──────────────────────────────────┴──────────────────────────────────┘

移动 (<= 900px，min-height: 100svh)：
[ 顶栏：品牌标识 + 主题开关 (44×44) ]
  └── 表单卡 .auth-card（12px 圆角、16px 内边距、所有控件 44px）
        └── 沉底页脚（版本键 44px + 协议链接）

404（NotFoundPage）：
[ .error-shell（min-height: 100svh）→ .error-main（max-width: 560px）→ .error-card（12px 圆角、16px 顶距） ]
  ├── 品牌徽标 (36×36, 8px 圆角)
  ├── 404 展示码（--text-display: 44px / 44px，禁止 96/160px）
  ├── 标题 (20/24) + 说明 (14/20)
  └── 返回入口按钮（桌面 36px / 移动 44px）
```

- **四页门户复用同一外壳 `AuthLayout`**：登录与注册共用双签面板；忘记密码与重置密码经 `PublicAuthPage` 复用同一 `AuthLayout`，视觉与状态反馈完全一致。
- **四态一致性**：字段错误使用 `border-color: var(--danger)` + `12px` 说明；加载态采用骨架行 `.app-skeleton-lines`；提交中按钮展示进行中文案并禁用；所有禁用态统一 `opacity: 0.45`。
- **暗色天空画布**：暗色主题下 `AuthAside` 自动切入 `SKY_PALETTES.night`（`data-on-dark="true"`），配深底渐变衬底与高对比文字；`prefers-reduced-motion: reduce` 下冻结单帧不跑 `rAF`。

### 5.2 用户端七页骨架（`/app/*`）

```text
[ 固定顶栏 .uapp-bar（52px 移动 / 56px >=768px） ]
  └── .uapp-bar-inner（max-width: 1120px 居中：品牌 + 主页/历史/设置 + 主题键 + 头像菜单）
[ 主内容 .uapp-main → .uapp-content → .uapp-page（居中 margin: 0 auto，左右留白差 0px） ]
  ├── 页头（眉标 12/16 + H1 20/24 移动 / 28/36 桌面）
  └── 核心内容网格（桌面双栏填充全宽；移动单列紧致堆叠，coreTop <= 130px）
[ 移动底栏 .uapp-bottom（< 768px 显示，60px，主页 / 历史 / 设置三签） ]
```

| 路由 | 容器宽度（桌面 `1440×900`） | 桌面形态（`>= 1024px`） | 移动形态（`<= 720px`） |
|---|---|---|---|
| `/app` | `1120px`（`1024–1279px` 为 `984px`） | 非对称双栏 `1.72fr : 1fr`（左：今日 Hero + 近 7 天；右：账号身份卡 `width: 100%` 满栏填充） | 单列紧致堆叠，总长约 `1.21` 屏 |
| `/app/history` | `1120px` | 双栏 `1fr : 380px`（左：月历；右：当天明细，日期 `44px` 展示字） | 单列堆叠；`<=360px` 日格自适应去 `min-width` 保 `320px` 零溢出；总长 `1.00` 屏 |
| `/app/settings` | `984px` | 等高双栏（左：偏好；右：账号与安全），DOM 与视觉同序 | 单列堆叠（偏好在前、账号与安全在后），`coreTop = 120px`，总长 `1.00` 屏 |
| `/app/account` | `1120px`（`1024–1279px` 为 `984px`） | 通栏身份 Hero + 等宽双栏（左：资料/设备/照片；右：签到概览/控制） | 标题与返回同行；设备只读双列与工具键双列紧致排布，总长 `<= 1.49` 屏 |
| `/app/account/new` | `1120px` + 宽弹窗 | 账号页 + `AccountWizard` 三步向导弹窗（`scrollBody`） | 同向导弹窗，全控件 `44px` |
| `/app/settings/time-pref` | `984px` + 宽弹窗 | 设置页 + `TimePrefDialog` 横滑 5 分钟刻度尺（`44px` 档高） | 同弹窗，方向键/横滑可达，关闭归位 `/app/settings` |
| `/app/settings/password` | `984px` + 标准弹窗 | 设置页 + `PasswordDialog`（实时策略校验 + 会话过期回跳 `/login`） | 同弹窗，全控件 `44px`，关闭归位 `/app/settings` |

### 5.3 门户与用户端令牌映射（桌面 vs 移动）

| 控件类别 | 桌面（`>= 721px`） | 移动（`<= 720px`） | 圆角 | 字号 / 行高 |
|---|---|---|---|---|
| 顶栏图标键 / 头像 / 主题开关 / 导航胶囊 | `36px`（`--ctrl-h-md`） | `44px`（`--ctrl-h-lg`） | `8px` / `999px` | `14px / 20px` |
| 标准按钮（`.btn` / `.uapp-button` / 分段签） | `36px`（`--ctrl-h-md`）；主提交 CTA 可取 `44px` | `44px`（`--ctrl-h-lg`） | `8px` / `6px` | `14px / 20px` |
| 紧凑操作键 / 行内链接键 / 开关 / 页脚版本键 | `32px`（`--ctrl-h-sm`，`::after` 外扩至 `>=44px` 热区） | `44px`（`--ctrl-h-lg`） | `6px` / `8px` / `999px` | `12px / 16px` |
| 双行列表行按钮 / 月历日格 / 时间片刻度 | `44px`（`--ctrl-h-lg`） | `44px`（`--ctrl-h-lg`） | `8px` / `6px` | `14px / 20px` + `12px / 16px` |
| 文本 / 密码输入框（`.input` / `.uapp-input`） | `36px`（`--ctrl-h-md`） | `44px`（`--ctrl-h-lg`） | `8px`（`--radius-md`） | `14px / 20px` |
| 卡片与门户舞台表面 | 顶内距 `16px`（`--card-pad`） | 顶内距 `16px`（`--card-pad`） | `12px`（`--radius-lg`） | 标题 `16/24` 或 `20/24` |
| 键盘焦点环（`:focus-visible`） | `2px solid var(--primary)` + `0 0 0 3px var(--primary-soft)` | 同桌面 | 继承控件 | — |
| 禁用态（`:disabled`） | `opacity: 0.45; cursor: not-allowed` | 同桌面 | — | — |

---

## 6. 持续维护约束

后续迭代管理端、用户端或门户任何页面时，必须遵守：

1. **禁止引入新尺寸阶**：只消费 `scale.css` 变量，不新增字号、圆角、按钮高度或卡片内边距档位。
2. **禁止新增颜色字面量**：所有色值走 `tokens.css` 与 `color-mix`，改完必跑 `npm run gate:tokens`。
3. **不破坏契约与测试**：保留文案、`aria-label`、`data-testid`、路由与行高/密度契约，每次修改后执行 `npm run verify` 与对应 behavior spec。
4. **零重复选择器**：`app.css` 与 `user-app.css` 同一媒体上下文下不得出现重复选择器。
