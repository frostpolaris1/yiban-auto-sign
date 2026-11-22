/**
 * **功能**
 * demo 静态服务兜底：端口被占用则直接退出（不抢端口、不杀他人进程）。
 *
 * **归属**
 * `ui/tools` 走查工具链；设计 demo 复核用。
 *
 * **口径**
 * --root 必填（demo 目录不入库）；只读服务，不写文件。
 * 默认只绑 127.0.0.1：这是走查工具，不该让同网段读到本机文件。
 * **读路径必须落在 root 内**：URL 解码后先 resolve，再校验前缀，
 * 逃出 root 一律 403。只做字符串前缀比较会漏 `/root-evil` 这种同前缀兄弟目录，
 * 故比较时带上结尾分隔符（root 自身允许相等）。
 * 用法：node tools/serve-demo.mjs --root <demo-dir> [--port 8765] [--host 127.0.0.1]
 */
import { createServer } from 'node:http'
import { readFileSync, existsSync, statSync, realpathSync } from 'node:fs'
import { join, extname, resolve, sep } from 'node:path'
import { arg } from './lib.mjs'

const invokedDirectly = import.meta.url === `file://${process.argv[1]}`

const port = Number(arg('port', '8765'))
const host = arg('host', '127.0.0.1')
const root = invokedDirectly ? arg('root') : ''
if (invokedDirectly && !root) {
  console.error('需要 --root <demo-dir>（demo 产物不入库）。')
  process.exit(2)
}
const rootResolved = root ? resolve(root) : ''

/**
 * 把请求路径映射到磁盘路径；逃出 root 返回 null。
 * 解码失败（畸形百分号转义）也返回 null，不当作根路径。
 */
export function resolveWithinRoot(rootDir, rawUrl) {
  const rootAbs = resolve(rootDir)
  let decoded
  try {
    decoded = decodeURIComponent((rawUrl || '/').split('?')[0])
  } catch {
    return null
  }
  // 解码后才 resolve：%2e%2e 与 .. 必须走同一条判断。
  const candidate = resolve(join(rootAbs, decoded))
  if (candidate === rootAbs) return candidate
  return candidate.startsWith(rootAbs + sep) ? candidate : null
}

const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.svg': 'image/svg+xml', '.json': 'application/json' }

/** 真实路径也必须落在 root 内：root 里放一个指向外部的软链同样算逃逸。 */
function realFileWithinRoot(rootDir, candidate) {
  try {
    const realRoot = realpathSync(resolve(rootDir))
    const realFile = realpathSync(candidate)
    return realFile === realRoot || realFile.startsWith(realRoot + sep) ? realFile : null
  } catch {
    return null
  }
}

const srv = createServer((req, res) => {
  const p = resolveWithinRoot(rootResolved, req.url)
  if (p === null) { res.writeHead(403); res.end('forbidden'); return }
  const target = existsSync(p) && statSync(p).isFile() ? p : join(rootResolved, 'index.html')
  if (!existsSync(target)) { res.writeHead(404); res.end('not found'); return }
  const file = realFileWithinRoot(rootResolved, target)
  if (file === null) { res.writeHead(403); res.end('forbidden'); return }
  res.writeHead(200, { 'Content-Type': mime[extname(file)] || 'application/octet-stream' })
  res.end(readFileSync(file))
})
srv.on('error', (e) => {
  if (e.code === 'EADDRINUSE') { console.log(`port ${port} already served; exit`); process.exit(0) }
  throw e
})
if (invokedDirectly) {
  srv.listen(port, host, () => console.log(`serving ${root} at http://${host}:${port}`))
}
