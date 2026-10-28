# ui — 易班自动签到前端

React 19 + Vite 8 + TypeScript 7。视觉语言复用 Adminator 4.4.0。

本批是 P1F 骨架加账号页。仪表盘与画廊只渲染界面；账号页调用契约端点。

## 命令

| 命令 | 作用 |
|---|---|
| `npm install` | 安装依赖。需要 Node >= 22。同时安装 API 生成工具链 |
| `npm run dev` | 本地开发，端口 5173 |
| `npm run build` | 类型检查 + 生产构建到 `dist/` |
| `npm run lint` | TypeScript 类型检查。本批不引 ESLint |
| `npm run generate:api` | 由契约重写 `src/lib/api/schema.d.ts` |
| `npm run check:api` | 契约漂移门。生成物与契约不一致即红 |
| `npm run gate:tokens` | 设计令牌门 |
| `npm run gate:structure` | 导入方向门 |
| `npm run gate:docs` | 入库文档门。禁止指向未入库目录 |
| `npm run gate:license` | 依赖许可证门。禁用许可证或清单漂移即红 |
| `npm run test:unit` | fetch、查询默认值与账号页纯逻辑单测，运行器为 `node:test` |
| `npm run test:visual` | Playwright。先跑 `npm run build` |
| `npm run test:visual:update` | 重写视觉基线。先跑 `npm run build` |
| `npm run verify` | 类型检查 + 契约漂移 + 单测 + 各门自测 + 四门 + 构建 |

首次运行 Playwright 需要浏览器：`npx playwright install chromium`。

## 目录

```
ui/src/
├── app/            启动、Provider、路由
├── pages/          路由页。一页一文件，文件名以 Page 结尾
├── features/       按域组织：auth / dashboard / design / accounts
├── components/
│   ├── shell/      Sidebar / Topbar / Footer / AppShell / nav 配置
│   └── ui/         Button / Input / Card / Table / Modal / Drawer / Tabs / Switch / Badge / Menu / Icon
├── lib/            api 按域拆分 / query / theme / format / cx
└── styles/         tokens.css / adminator.css / app.css
```

依赖方向单向：`pages → features → components/ui → lib`。

`components` 不导入 `features` 或 `pages`。`features` 不互相导入。`lib` 不导入上层。

`scripts/structure-gate.mjs` 解析每个文件的 import、export 与 `import()`，把 `@/` 与相对路径解析成 `ui/src` 下的真实文件，再按 `structure.rules.tsv` 判方向。它检查全部说明符，不只第一条。无法解析的库内路径即红。规则为空或扫描集为空时退出码 2。

路径别名 `@/` 指向 `ui/src`。配置见 `tsconfig.json` 与 `vite.config.ts`。

## 契约类型

`server/contracts/openapi.yaml` 是前端类型的唯一来源。`src/lib/api/schema.d.ts` 由 `openapi-typescript` 生成，已入库。手写桩只做别名，不重写字段。

- 生成：`npm run generate:api`。
- 漂移门：`npm run check:api`。它把契约重新生成到临时目录，与入库文件逐字节比较。
- 生成器装在 `scripts/api-codegen/`，固定 `openapi-typescript 7.13.0` 与 `typescript 5.9.3`。原因：UI 编译器是 TypeScript 7，生成器需要 TypeScript 5 的编译器 API。两套编译器互不影响。
- `npm install` 的 `postinstall` 自动安装该工具链。缺少工具链时门报配置错误，退出码 2。

## 设计令牌

- `styles/tokens.css` 是唯一颜色来源。36 个浅色 token 加 36 个深色 token。取值与 Adminator `_tokens.scss` 一致。
- `styles/adminator.css` 是 `web/static/vendor/adminator/adminator.css` 的逐字节副本。源文件 68,956 B，sha256 `c94aa111a7a769f273e03e8a90bb266811965b305ce340a451e4e7268d52acbe`。
- `styles/app.css` 只放项目覆盖层。颜色、圆角、阴影只允许引用 token。
- 组件只用 Adminator 类名。`app-*` 类是登记过的扩展。扩展清单见 `app.css` 注释。

`scripts/token-gate.mjs` 做三项检查：

1. 扫 `src/` 与 `index.html` 的颜色字面量。规则覆盖 `#hex`、`rgb()`、`hsl()`、`oklch()`、`lab()`、`lch()`、`color()`。`tokens.css` 与 `adminator.css` 免扫。两项豁免都做了等值校验。
2. `adminator.css` 必须与 vendor 源逐字节一致。
3. `tokens.css` 的 token 名与值必须与 vendor 源完全一致，两套主题都比。

空扫描集、全豁免扫描集、空规则集都是配置错误，退出码 2。`--self-test` 对颜色规则、vendor 字节规则、token 对齐规则各跑正负样本。

```sh
node scripts/token-gate.mjs --self-test
node scripts/token-gate.mjs
```

## 主题

- 主题写在 `<html data-theme="light|dark">`。
- 唯一 localStorage 键是 `dash26-theme`。不存令牌。
- `index.html` 的内联脚本先于 CSS 执行。它写入 `data-theme`，避免闪烁。
- 切换按钮是 Topbar 的 `#themeToggle`。

## 视觉回归

矩阵：4 条路由 × 2 套主题 × 2 个宽度（1440 / 390） = 16 张截图。
另有账号页抽屉与模态各 2 张，共 20 张。

行为用例 32 条：画廊 10 条、账号页 21 条、无溢出 1 条。Playwright 总计 52 条。

基线目录：`e2e/visual.spec.ts-snapshots/`。

基线按平台分套，Chromium 1248。win32 与 linux 两套都已入库。
WSL 读 `-chromium-linux.png`，Windows 读 `-chromium-win32.png`。
换平台或升 Chromium 必须重跑 `npm run test:visual:update`。

## Adminator 源

- 仓库：<https://github.com/puikinsh/adminator-admin-dashboard>
- commit：`4c768545ad0c981c77669a7bf84de75b94d082e0`（2026-10-13）。
- 版本：4.4.0。
- 许可证：MIT，版权归 Aigars Silkalns。全文见 `THIRD_PARTY_NOTICES.md`。
- 其余依赖的清单见 `THIRD_PARTY_LICENSES.md`（由 `npm run gate:license -- --write` 生成）。

源文件与我们的组件对应关系：

| Adminator 源（commit 4c76854） | 提取内容 | 我们的落点 |
|---|---|---|
| `src/index.html` | `shell` 占位、`hero`、`kpi-grid`、`monthly-footer`、`.table` + `tag` | `AppShell`、`PageHeader`、`KpiGrid`、`SignStatusCard`、`CapacityCard` |
| `src/signin.html` | `auth-shell`、`auth-aside`、`auth-card`、`auth-form`、`field-row` | `LoginPage`、`AuthAside`、`LoginForm` |
| `src/ui.html` | `alert`、`badge`、`tag`、`progress`、`spinner`、`tabs`、`modal-demo`、`popover-demo`、`avatar-group` | `FeedbackSection`、`IndicatorsSection`、`OverlaysSection`、`NavigationSection`、`Alert`、`Badge`、`Modal`、`Tabs`、`Hint` |
| `src/forms.html` | `form-grid`、`field`、`input`、`select`、`textarea`、`check`、`switch`、`form-actions` | `FormsSection`、`Input`、`SwitchControl` |
| `src/basic-table.html` | `.table`、`.cell-name`、`.cell-date`、`.tag` | `SignStatusCard` 的表格 |
| `src/datatable.html` | `data-toolbar`、`data-table`、`data-cell-user`、`data-cell-mono`、`data-cell-actions`、`pager` | `TableSection`、`DataTable` |
| `src/charts.html` | `chart-canvas-wrap`、`chart-meta-row`、`btn-group` | 本批未移植。类名已记录 |
| `src/calendar.html` | `cal-shell`、`cal-rail`、`mini-cal-grid`、`upc-list` | 本批未移植。类名已记录 |
| `src/assets/scripts/2026/Shell.js` | `NAV`、侧栏、顶栏、页脚 DOM、抽屉钩子 | `shell/nav.ts`、`Sidebar`、`Topbar`、`Footer`、`AppShell` |
| `src/assets/scripts/2026/init.js` | 主题切换、移动抽屉、下拉关闭 | `lib/theme.tsx`、`Topbar`、`AppShell` |
| `_tokens.scss` | 36 × 2 令牌 | `styles/tokens.css` |
| `_shell.scss` | shell、sidebar、topbar、footer、drawer | 壳组件消费 vendor 类 |
| `_components.scss` | hero、card、btn、table、tag、grid | `PageHeader`、`Card`、`Button`、仪表盘 |
| `_forms.scss` | field、input、check、switch | `Input`、`SwitchControl`、`app.css` 状态映射 |
| `_ui.scss` | alert、badge、progress、spinner、tabs、modal、popover、avatar、`demo-row`、`section-h` | `Alert`、`Badge`、`Tabs`、`Modal`、`Hint`、各画廊卡片 |
| `_data.scss` | data table、cell、pager | `DataTable`、`TableSection` |

## 本批不做

- 仪表盘与画廊不调后端。`lib/api/*` 其余域仍是契约桩。
- 不接 TanStack Table、Chart.js、react-day-picker、SSE。
- 不做登录会话与路由守卫。P1 认证批次加入。
- 不引 Tailwind、shadcn、Element Plus、Bootstrap、Lucide、Recharts。
