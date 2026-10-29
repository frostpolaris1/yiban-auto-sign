# ui — 易班自动签到前端

React 19 + Vite 8 + TypeScript 7。视觉语言复用 Adminator 4.4.0。

本批包含 P1F 骨架、登录接线、账号页、学校页、时间片页、账号设置页、用户管理页与看板数据接线。画廊只渲染界面。
看板按角色取数：user 读签到状态与诊断，master/admin 读窗口与容量体检。
仪表盘的时间偏好卡只对 user 渲染，并调用 `/api/me/time-pref`。
账号设置页的改密卡接登录态 `POST /api/auth/change-password`，成功即清待改密标记。
用户管理页只归 master：`/users` 走 `GET /api/users` 的服务端游标分页，本页只读。

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
| `npm run test:unit` | fetch、查询默认值、游标、账号页、学校页、时间片页、账号设置页、用户管理页、看板与认证纯逻辑单测，加上图标来源门（`icon-source`：lucide 唯一、TS 字符串载荷无内联 svg、CSS data URI 登记、品牌标记 JSX 全量哈希、导入白名单），运行器为 `node:test` |
| `npm run build:showcase` | 构建 `VITE_SHOWCASE=1` 的展示产物到 `dist-showcase/` |
| `npm run test:e2e` | 全量 Playwright（269 条），串行。先跑 `npm run build` |
| `npm run test:e2e:fast` | 收口快路：行为 4 并发 + 视觉 1 并发。先跑 `npm run build` |
| `npm run test:behavior` | 只跑行为用例，4 并发（`e2e/` 下除视觉外的全部 spec） |
| `npm run test:shots` | 只跑视觉用例，1 并发 |
| `npm run test:shots:update` | 重写视觉基线。先跑 `npm run build` |
| `npm run test:visual` | 旧名，等价于 `playwright test`（参数可透传） |
| `npm run test:visual:update` | 旧名，重写全部基线 |
| `npm run verify` | 类型检查 + 契约漂移 + 单测 + 各门自测 + 四门 + 构建 |

首次运行 Playwright 需要浏览器：`npx playwright install chromium`。

测试节奏（2026-10-29 实测；含两个 webServer 启动，不含 `npm run build`）：

- 迭代只跑受影响的 spec 或 `--grep`，不进全量。
- 收口跑 `test:e2e:fast`：Windows 约 2.9 分（行为 1.1–1.3＋视觉 1.7–2.0），
  WSL 约 2.0–3.9 分（行为 39–89 秒＋视觉 1.1–2.2 分）。
- 全量 `test:e2e`：Windows 约 4.4–4.8 分，WSL 约 2.7–5.2 分；只在整包核对时跑。
- 视觉用例不再统一等 `networkidle` 与固定 200ms；每个用例先断言数据锚点再拍。
  菜单入场动画会改变整页合成层，菜单态用例自带 200ms 落定等待。
- 行为用例全局带 `reducedMotion: 'reduce'`：auto-animate 不启用，退场节点不进 DOM，
  同名行按钮不会在过渡窗口里重复。视觉用例显式改回 `no-preference`，基线口径不变。
- `test:behavior` 的覆盖由 `scripts/tests/test-plan.test.mjs` 守卫：
  递归发现 `e2e/` 下全部 spec，新增行为 spec 忘了登记即红；扫空也红。

## 目录

```
ui/src/
├── app/            启动、Provider、路由
├── pages/          路由页。一页一文件，文件名以 Page 结尾
├── features/       按域组织：auth / dashboard / design / accounts / schools / user / schedule / settings
├── components/
│   ├── shell/      Sidebar / Topbar / Footer / AppShell / nav 配置
│   └── ui/         Button / Input / Card / Table / Modal / Drawer / Tabs / Switch / Badge / Menu / Icon / BrandMark / Toaster
├── lib/            api 按域拆分 / query / theme / format / cursor / roles / cx
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
- 组件只用 Adminator 类名。`app-*` 是项目扩展层：样式规则集中在 `styles/app.css`，少数类只作行为或测试钩子（如 `.app-account-row`），没有样式规则。

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

## 微交互

- 成功反馈走 toast：右上角，落在顶栏下方。文案含操作结果与对象标识。
  `Toaster` 只映射常规与成功两组槽位（六键），成功配色对齐站内 `Alert.success`，
  颜色全部取自 `tokens.css`，不写颜色字面量。
- 错误反馈留在发起的模态或表单内，不改 toast。错误要可回看。
- 登录与登出靠整页跳转反馈，不发 toast。
- 看板 KPI、容量三格与预览三格的数字由 `@number-flow/react` 渲染，颜色继承外层。
  终值是可见文本：数据到位后可直接断言等于期望数字。
- 账号表、学校表与时间片窗口表的行增删与重排走 180ms 过渡，
  尊重系统「减少动态效果」偏好。行点击、菜单与焦点还原语义不变。

## 视觉回归

矩阵：8 条常驻路由与看板的两个视角（user/admin）× 2 套主题 × 2 个宽度 = 40 张截图。
时间片页按固定业务日 `2026-10-28` 另拍 1440 与 390 两个宽度（缺省业务日会随日历漂移，不进常驻矩阵）。
另有账号页 6 种开态 × 2 套主题 = 12 张，学校页 6 种开态 × 2 套主题 = 12 张，
时间片页列表（1440/390）／预览／覆盖／无权限与仪表盘偏好卡共 6 种开态 × 2 套主题 = 12 张，
profile 菜单与会话过期提示各 2 张，toast 明暗各 1 张，共 82 张（按 spec 计，每平台应各 82 张）。
win32 套 82 个文件已入库（2026-10-29 实测 `ls e2e/visual.spec.ts-snapshots | grep -c win32`）。
linux 套现有 74 个，缺口见下。两平台文件数合计 156（2026-10-29 实测目录文件数）。

行为用例 186 条：账号页 28 条、认证 36 条、看板 14 条、画廊 9 条、学校页 21 条、
时间片页与时间偏好 21 条、账号设置页 14 条、用户管理页 10 条、展示模式 8 条、微交互 25 条。
视觉文件另有无溢出 1 条。Playwright 总计 269 条（2026-10-29 实测 `npx playwright test --list`）。

**基线缺口登记（2026-10-29）**：
- linux 套缺 8 张：`/settings` 4 张与 `/users` 4 张。本机只有 win32 Chromium，未生成。
- linux 套另有过期 16 张：它们含侧栏，导航新增「用户管理」后须重拍。
- linux 与 win32 补法相同：在目标平台跑 `npm run test:shots:update`。
- win32 既有偏差 5 条（本批之前即存在，本批未重拍）：`forgot-password` 四张与
  `reset-password` 一张。2026-10-29 在本机 win32 实测差 9、2、7、4、4 像素。
  `npm run test:shots` 因此仍报 5 条红；红集合与本批之前相同。

展示模式用例由 `npm run build:showcase` 构建的 `dist-showcase` 提供。
Playwright 的第二个 webServer 在 4174 端口预览它；`e2e/showcase.spec.ts` 用它钉零请求。

基线目录：`e2e/visual.spec.ts-snapshots/`。

基线按平台分套，Chromium 1248。win32 与 linux 两套都已入库。
WSL 读 `-chromium-linux.png`，Windows 读 `-chromium-win32.png`。
换平台或升 Chromium 必须重跑 `npm run test:shots:update`（或 `npm run test:visual:update` 重写全部）。

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
| `src/signin.html` | `auth-shell`、`auth-aside`、`auth-card`、`auth-form`、`field-row` | `LoginPage`、`AuthAside`、`LoginForm`、`PublicAuthPage`、`ForgotPasswordPage`、`ResetPasswordPage` |
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

- 用户管理页已接线：`GET /api/users` 只归 master，服务端游标分页，
  筛选走 `role`／`status`／`q`。建号表单（`POST /api/users`）不在本批。
- 看板已接线：user 读 `/api/me/sign-status`（30 秒轮询）与 `/api/me/diagnostics`，
  master/admin 读 `/api/schedule/windows` 与 `/api/capacity`。看板不画图表。
- 不接 TanStack Table、Chart.js、react-day-picker、SSE。
- 忘记密码与重置密码两页已接公开的令牌流。
  登录态改密已接 `POST /api/auth/change-password`：409 是新旧相同。
  401 有两种来源：当前口令错、会话失效。界面收到 401 后探一次 `GET /api/auth/me`：
  me 200 显示「当前密码不正确」；me 401 走会话过期处置并落登录页。
  成功撤销本人其余会话并清 `must_change_password`；本页不显示上次改密时间，
  因为 `GET /api/auth/me` 不返回该字段。
  服务端尚未接线令牌投递：它只创建令牌，不发邮件。界面文案按产品要求固定。
- 不引 Tailwind、shadcn、Element Plus、Bootstrap、Recharts。图标统一取 `lucide-react`，
  由 `src/components/ui/Icon.tsx` 收口；`scripts/tests/icon-source.test.mjs` 钉住来源面。
