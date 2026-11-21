import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { devApiMock } from './scripts/dev-api-mock.mjs'

/** 版本号的唯一来源是 ui/package.json；构建期注入，src 不跨目录 import。 */
const pkg = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')) as {
  version: string
}

export default defineConfig(({ mode }) => ({
  plugins: [react(), devApiMock()],
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // 本地联调：把 API 转给 Go 后端（assembly 默认 127.0.0.1:47183）。
    // 只影响 `npm run dev`；构建与 e2e（vite preview）不走此代理。
    // `/healthz` 是公开探针的根路径（不挂 /api 前缀），不代理时 dev 下版本区恒显自检失败。
    // dev:mock 模式例外：不设代理，/api 由 dev-api-mock 插件应答。
    ...(mode === 'devmock'
      ? {}
      : {
          proxy: {
            '/api': 'http://127.0.0.1:47183',
            '/healthz': 'http://127.0.0.1:47183',
          },
        }),
  },
  preview: {
    port: 4173,
    strictPort: true,
    host: '127.0.0.1',
    // Vite 的 preview.proxy 缺省继承 server.proxy：活着真后端时 e2e 未 mock
    // 的请求会漏进真库（401 触发会话过期循环）。显式清空保 e2e 密闭。
    proxy: {},
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
}))
