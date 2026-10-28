import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // 本地联调：把 API 转给 Go 后端（assembly 默认 127.0.0.1:47183）。
    // 只影响 `npm run dev`；构建与 e2e（vite preview）不走此代理。
    proxy: {
      '/api': 'http://127.0.0.1:47183',
    },
  },
  preview: {
    port: 4173,
    strictPort: true,
    host: '127.0.0.1',
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
