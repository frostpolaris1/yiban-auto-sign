# yiban-frontend — 前端翻新源码（Vue 3 + Vite + Element Plus）

前端翻新线的唯一源码根。**本目录只是源码与构建配置；运行期部署不需要 Node。**
计划与裁决：`docs/refactor/29-frontend-vue-refactor-plan.md`（J1–J5）。

## 命令

```bash
cd frontend
npm install        # 首次 / 依赖变更后
npm run dev        # 本地开发：需要同时跑着 Flask（默认代理到 127.0.0.1:8000，
                   # 可用 YB_DEV_PROXY_TARGET 覆盖）；开发壳是本目录 index.html
npm run build      # 产物 + manifest 落 ../web/static/vue/（提交入库）
```

## 与 Flask 的集成契约（勿在两侧各自变更）

1. **产物落点**：`build.outDir = ../web/static/vue`，`manifest: true` 额外产出
   `.vite/manifest.json`；两者都提交入库（Adminator 先例：只入库编译产物，部署期零 Node）。
2. **入口登记**：每个迁移页在 `vite.config.ts` 的构建输入里是一个 HTML 入口；Flask 侧
   `web/services/vue_assets.py` 按 entry 名（HTML 相对本目录的路径）解析出
   `{js, preloads, css}`，页面视图经 `_render_admin_page(extra=...)` 下发给模板。
3. **URL 拼接**：`vue_assets` 返回的路径**不含 script_root**；模板统一拼
   `request.script_root`（子路径部署唯一收口点）。构建用 `base: './'`，chunk 间引用是
   相对路径，任意挂载前缀下成立。
4. **缓存**：文件名含内容哈希 → 走 `/static` 既有长缓存；页面本身 `no-store`
   （新页路径登记进 `web/routes/pages.py` 的 `NO_STORE_PAGES`）。
5. **守卫**：`tests/test_web_vue_pilot.py` 钉住「页面引用的资产必须真实在盘」与
   「产物无 eval / new Function（CSP 无 unsafe-eval 的前提）」——改构建配置前先读它。

## 设计语言（裁决 J2）

- Element Plus 一切颜色/圆角/字体经 `src/styles/ep-theme.css` 映射 Adminator 的
  36 个主题 token（`:root[data-theme=...]` 作用域，高于 EP 自带 `html.dark` 的优先级）；
- 深浅色同步：外壳 `html[data-theme]` 是唯一事实源，`src/lib/theme.ts` 单向跟随并监听
  `yiban:theme`（**document** 上的 CustomEvent，见 core.js:882）；
- 组件内的自定义样式只允许 `var(--token)`，不得写死颜色（对齐 `app.css` 收口纪律）。

## 目录

```
src/
  lib/api.ts        薄 GET 客户端（BASE 感知；CSRF/401 重试层随第一个迁移页补齐）
  lib/theme.ts      EP html.dark ↔ 外壳 data-theme 同步
  styles/ep-theme.css  EP → Adminator token 映射
  pilot/            试点页（/work/pilot，管线验证用，不进侧栏）
```

新增迁移页时：复制 `pilot/` 的结构为 `src/<page>/`，在 `vite.config.ts` 登记入口，
Flask 侧加路由 + 模板 + `vue_assets(entry)` 下发——四步，参照 29 号计划 §3。
