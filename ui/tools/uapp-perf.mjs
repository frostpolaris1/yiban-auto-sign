/**
 * **功能**
 * 首屏性能探针：FCP/LCP/导航 timing/请求瀑布/dist chunk 体积，输出 md。
 *
 * **归属**
 * `ui/tools` 走查工具链；性能批次前后对比用。
 *
 * **口径**
 * 只读测量，不改页面状态；登录复用会话无 logout 副作用。
 * 用法：node tools/uapp-perf.mjs --url http://localhost:5174/users --out perf.md --email <e> --password <p>
 */
import { writeFileSync, readdirSync, statSync } from 'node:fs'
import { launch, arg, login, creds, UI } from './lib.mjs'

const url = arg('url', 'http://localhost:5174/users')
const out = arg('out', null)
const { email, password } = creds()
const noLogin = process.argv.includes('--no-login')

const b = await launch()
const page = await b.newPage({ viewport: { width: 1440, height: 980 } })
await page.addInitScript(() => {
  window.__perf = { fcp: null, lcp: null }
  new PerformanceObserver((l) => {
    for (const e of l.getEntries()) if (e.name === 'first-contentful-paint') window.__perf.fcp = Math.round(e.startTime)
  }).observe({ type: 'paint', buffered: true })
  new PerformanceObserver((l) => {
    const es = l.getEntries()
    window.__perf.lcp = Math.round(es[es.length - 1].startTime)
  }).observe({ type: 'largest-contentful-paint', buffered: true })
})
if (!noLogin) await login(page, new URL(url).origin, email, password)
await page.goto(url, { waitUntil: 'load' })
await page.waitForTimeout(2500)
const perf = await page.evaluate(() => {
  const nav = performance.getEntriesByType('navigation')[0]
  const res = performance.getEntriesByType('resource').map((e) => ({
    url: e.name.replace(/^https?:\/\/[^/]+/, ''), ms: Math.round(e.responseEnd), kb: Math.round((e.transferSize || 0) / 1024),
  }))
  return { fcp: window.__perf.fcp, lcp: window.__perf.lcp, dcl: Math.round(nav.domContentLoadedEventEnd), load: Math.round(nav.loadEventEnd), res }
})
await b.close()

const chunks = readdirSync(`${UI}/dist/assets`).filter((f) => f.endsWith('.js') || f.endsWith('.css'))
  .map((f) => ({ f, kb: Math.round(statSync(`${UI}/dist/assets/${f}`).size / 1024) }))
  .sort((a, b2) => b2.kb - a.kb)

const md = [
  `# 首屏性能 ${url}`,
  '',
  '| 指标 | ms |',
  '|---|---|',
  `| FCP | ${perf.fcp} |`,
  `| LCP | ${perf.lcp} |`,
  `| DCL | ${perf.dcl} |`,
  `| load | ${perf.load} |`,
  '',
  '## 请求瀑布（前 20）',
  ...perf.res.slice(0, 20).map((r) => `- ${r.ms}ms ${r.kb}KB ${r.url}`),
  '',
  '## dist chunks',
  ...chunks.map((c) => `- ${c.kb}KB ${c.f}`),
].join('\n')
console.log(md)
if (out) writeFileSync(out, md)