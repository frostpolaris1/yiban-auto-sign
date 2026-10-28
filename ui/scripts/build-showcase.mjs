/**
 * build-showcase.mjs — 构建展示模式产物到 `dist-showcase`。
 *
 * **功能**
 * 置 `VITE_SHOWCASE=1` 后调用 Vite 构建同一份源码。
 *
 * **归属**
 * ui 构建脚本：只服务 `e2e/showcase.spec.ts` 与展示部署；不参与 `npm run verify`。
 *
 * **口径**
 * 产物目录与常规 `dist/` 分开，避免覆盖。
 * 环境变量在调用 `build()` 前写入 `process.env`：Vite 只注入 `VITE_` 前缀值。
 * 本脚本不做类型检查；类型门在 `npm run lint` 与 `npm run verify`。
 */

import { build } from 'vite'

process.env.VITE_SHOWCASE = '1'

await build({
  build: {
    outDir: 'dist-showcase',
    emptyOutDir: true,
  },
})
