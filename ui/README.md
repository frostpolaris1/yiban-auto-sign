# ui — 易班自动签到前端

React 19 + Vite 8 + TypeScript 7。视觉语言复用 Adminator 4.4.0。

本批是 P1F 骨架。只渲染界面。不调用后端。不写凭据。

## 命令

| 命令 | 作用 |
|---|---|
| `npm install` | 安装依赖。需要 Node >= 22 |
| `npm run dev` | 本地开发，端口 5173 |
| `npm run build` | 类型检查 + 生产构建到 `dist/` |
| `npm run lint` | TypeScript 类型检查。本批不引 ESLint |
| `npm run gate:tokens` | 设计令牌门 |
| `npm run test:visual` | Playwright。先跑 `npm run build` |
| `npm run test:visual:update` | 重写视觉基线。先跑 `npm run build` |
| `npm run verify` | 类型检查 + 门自测 + 门 + 构建 |

首次运行 Playwright 需要浏览器：`npx playwright install chromium`。

## 目录

```
ui/src/
├── app/            启动、Provider、路由
├── pages/          路由页。一页一文件，文件名以 Page 结尾
├── features/       按域组织：auth / dashboard / design
├── components/
│   ├── shell/      Sidebar / Topbar / Footer / AppShell / nav 配置
│   └── ui/         Button / Input / Card / Table / Modal / Tabs / Switch / Badge / Menu / Icon
├── lib/            api 按域拆分 / query / theme / format / cx
└── styles/         tokens.css / adminator.css / app.css
```

依赖方向单向：`pages → features → components/ui → lib`。

`components` 不导入 `features` 或 `pages`。`features` 不互相导入。

路径别名 `@/` 指向 `ui/src`。配置见 `tsconfig.json` 与 `vite.config.ts`。

## 设计令牌

- `styles/tokens.css` 是唯一颜色来源。36 个浅色 token 加 36 个深色 token。取值与 Adminator `_tokens.scss` 一致。
- `styles/adminator.css` 是 `web/static/vendor/adminator/adminator.css` 的逐字节副本。源文件 68,956 B，sha256 `c94aa111a7a769f273e03e8a90bb266811965b305ce340a451e4e7268d52acbe`。
- `styles/app.css` 只放项目覆盖层。颜色、圆角、阴影只允许引用 token。
- 组件只用 Adminator 类名。`app-*` 类是登记过的扩展。扩展清单见 `app.css` 注释。

`scripts/token-gate.mjs` 做三项检查：

1. 扫 `src/` 的颜色字面量。`tokens.css` 与 `adminator.css` 免扫。两项豁免都做了等值校验。
2. `adminator.css` 必须与 vendor 源逐字节一致。
3. `tokens.css` 的 token 名与值必须与 vendor 源完全一致，两套主题都比。

空扫描集、全豁免扫描集、空规则集都是配置错误，退出码 2。

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

矩阵：3 条路由 × 2 套主题 × 2 个宽度（1440 / 390） = 12 张截图。

另有 11 条行为与无溢出断言。总计 23 条。

基线目录：`e2e/visual.spec.ts-snapshots/`。

基线平台是 win32 / Chromium 1248。Linux 基线未生成。换平台必须重跑 `npm run test:visual:update`。

## Adminator 源

- 克隆：`work/adminator`（只读）。
- commit：`4c768545ad0c981c77669a7bf84de75b94d082e0`（2026-10-13）。
- 版本：4.4.0。
- 许可证：MIT，版权归 Aigars Silkalns。全文见 `THIRD_PARTY_NOTICES.md`。

组件与源文件的对应关系见 `work/handoff/2026-10-25-p1b.md`。

## 本批不做

- 不调后端。`lib/api/*` 是契约桩。
- 不接 TanStack Table、Chart.js、react-day-picker、SSE。
- 不做登录会话与路由守卫。P1 认证批次加入。
- 不引 Tailwind、shadcn、Element Plus、Bootstrap、Lucide、Recharts。
