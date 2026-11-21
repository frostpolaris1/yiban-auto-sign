/**
 * **功能**
 * demo 静态服务兜底：端口被占用则直接退出（不抢端口、不杀他人进程）。
 *
 * **归属**
 * `ui/tools` 走查工具链；设计 demo 复核用。
 *
 * **口径**
 * --root 必填（demo 目录不入库）；只读服务，不写文件。
 * 用法：node tools/serve-demo.mjs --root <demo-dir> [--port 8765]
 */
import { createServer } from 'node:http'
import { readFileSync, existsSync, statSync } from 'node:fs'
import { join, extname } from 'node:path'
import { arg } from './lib.mjs'

const port = Number(arg('port', '8765'))
const root = arg('root')
if (!root) {
  console.error('需要 --root <demo-dir>（demo 产物不入库）。')
  process.exit(2)
}
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.svg': 'image/svg+xml', '.json': 'application/json' }

const srv = createServer((req, res) => {
  const p = join(root, decodeURIComponent((req.url || '/').split('?')[0]))
  const file = existsSync(p) && statSync(p).isFile() ? p : join(root, 'index.html')
  if (!existsSync(file)) { res.writeHead(404); res.end('not found'); return }
  res.writeHead(200, { 'Content-Type': mime[extname(file)] || 'application/octet-stream' })
  res.end(readFileSync(file))
})
srv.on('error', (e) => {
  if (e.code === 'EADDRINUSE') { console.log(`port ${port} already served; exit`); process.exit(0) }
  throw e
})
srv.listen(port, () => console.log(`serving ${root} at http://localhost:${port}`))