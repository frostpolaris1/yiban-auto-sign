/**
 * **功能**
 * 真页截图矩阵：page × viewport × theme，fullPage 落盘＋manifest。
 *
 * **归属**
 * `ui/tools` 走查工具链；设计复核与基线前置核对用。
 *
 * **口径**
 * 无 logout 副作用；同一 context 内登录一次复用会话。
 * 用法：node tools/uapp-shot.mjs --pages app,app/settings --viewports 390,1440 --themes light,dark --out <dir> --email <e> --password <p>
 */
import { mkdirSync } from 'node:fs'
import { launch, arg, list, login, creds, slug } from './lib.mjs'

const base = arg('base', 'http://localhost:5174')
const pages = list('pages', ['app', 'app/history', 'app/settings', 'app/account'])
  .map((p) => (p.startsWith('/') ? p : `/${p}`))
const viewports = list('viewports', ['390', '1440']).map((w) => ({ w: Number(w), h: Number(w) < 768 ? 844 : 980 }))
const themes = list('themes', ['light', 'dark'])
const out = arg('out', 'shots/uapp-shot')
const { email, password } = creds()
const noLogin = process.argv.includes('--no-login')

mkdirSync(out, { recursive: true })
const b = await launch()
const manifest = []
for (const theme of themes) {
  for (const vp of viewports) {
    const ctx = await b.newContext({ viewport: { width: vp.w, height: vp.h } })
    const page = await ctx.newPage()
    await page.addInitScript((t) => { try { localStorage.setItem('dash26-theme', t) } catch {} }, theme)
    if (!noLogin) await login(page, base, email, password)
    for (const p of pages) {
      await page.goto(base + p, { waitUntil: 'load' })
      await page.waitForTimeout(700)
      const file = `${out}/${slug(p)}-${vp.w}-${theme}.png`
      await page.screenshot({ path: file, fullPage: true })
      manifest.push({ page: p, viewport: vp.w, theme, file })
      console.log('shot', file)
    }
    await ctx.close()
  }
}
await b.close()
console.log(JSON.stringify({ count: manifest.length, out }))