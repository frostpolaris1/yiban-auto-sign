/**
 * **功能**
 * 快速 a11y 扫描：触达尺寸/图标按钮名称/标题跳级/文本对比度/横向溢出/
 * :focus-visible 与 prefers-reduced-motion 是否在样式中。无外部依赖。
 *
 * **归属**
 * `ui/tools` 走查工具链；每页收口前必跑。
 *
 * **口径**
 * 对比度按 WCAG 相对亮度；::after 扩热区的控件标注「需实测」不直接判缺陷。
 * 用法：node tools/uapp-a11y.mjs --url http://localhost:5174/users --viewport 390 --email <e> --password <p>
 */
import { launch, arg, login, creds } from './lib.mjs'

const url = arg('url', 'http://localhost:5174/users')
const vp = Number(arg('viewport', '1440'))
const scope = arg('scope', null)
const { email, password } = creds()
const noLogin = process.argv.includes('--no-login')

const b = await launch()
const page = await b.newPage({ viewport: { width: vp, height: vp < 768 ? 844 : 980 } })
if (!noLogin) await login(page, new URL(url).origin, email, password)
await page.goto(url, { waitUntil: 'load' })
await page.waitForTimeout(900)

const findings = await page.evaluate((scopeSel) => {
  const root = scopeSel ? document.querySelector(scopeSel) : document
  if (!root) return ['scope 选择器未命中: ' + scopeSel]
  const out = []
  const lum = (c) => {
    const m = c.match(/\d+(\.\d+)?/g)
    if (!m || m.length < 3) return null
    const [r, g, b2] = m.slice(0, 3).map((v) => {
      const s = Number(v) / 255
      return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
    })
    return 0.2126 * r + 0.7152 * g + 0.0722 * b2
  }
  const bgOf = (el) => {
    let n = el
    while (n && n !== document.documentElement) {
      const c = getComputedStyle(n).backgroundColor
      if (c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') return c
      n = n.parentElement
    }
    return getComputedStyle(document.body).backgroundColor
  }
  for (const el of root.querySelectorAll('button, a, [role="switch"], input[type="checkbox"]')) {
    const r = el.getBoundingClientRect()
    if (r.width === 0 || r.height === 0) continue
    if (r.width < 44 || r.height < 44) {
      const cs = getComputedStyle(el, '::after')
      const pad = cs && cs.content !== 'none' ? '（有 ::after 扩区，需实测）' : ''
      out.push(`touch ${Math.round(r.width)}x${Math.round(r.height)} <${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}> ${(el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 18)}${pad}`)
    }
  }
  for (const el of root.querySelectorAll('button')) {
    const text = (el.textContent || '').trim()
    if (!text && !el.getAttribute('aria-label') && !el.getAttribute('title')) out.push(`aria 图标按钮无名称 <${el.className}>`)
  }
  let prev = 0
  for (const h of root.querySelectorAll('h1,h2,h3,h4,h5,h6')) {
    const lv = Number(h.tagName[1])
    if (prev && lv > prev + 1) out.push(`heading 跳级 h${prev}→h${lv} ${(h.textContent || '').trim().slice(0, 16)}`)
    prev = lv
  }
  const seen = new Set()
  for (const el of root.querySelectorAll('p,span,h1,h2,h3,h4,button,a,td,th,label')) {
    const t = (el.textContent || '').trim()
    if (!t || el.children.length > 0 || seen.has(t)) continue
    seen.add(t)
    const cs = getComputedStyle(el)
    const f = lum(cs.color), b2 = lum(bgOf(el))
    if (f === null || b2 === null) continue
    const ratio = (Math.max(f, b2) + 0.05) / (Math.min(f, b2) + 0.05)
    const size = parseFloat(cs.fontSize)
    const need = size >= 18 || (size >= 14 && Number(cs.fontWeight) >= 700) ? 3 : 4.5
    if (ratio < need) out.push(`contrast ${ratio.toFixed(2)}<${need} "${t.slice(0, 16)}" ${cs.color}`)
    if (seen.size > 400) break
  }
  if (document.documentElement.scrollWidth > window.innerWidth) out.push(`overflow scrollWidth ${document.documentElement.scrollWidth} > ${window.innerWidth}`)
  let fv = false, rm = false
  for (const ss of document.styleSheets) {
    try {
      for (const r of ss.cssRules) {
        const txt = r.cssText || ''
        if (txt.includes(':focus-visible')) fv = true
        if (txt.includes('prefers-reduced-motion')) rm = true
      }
    } catch {}
  }
  if (!fv) out.push('style 缺 :focus-visible')
  if (!rm) out.push('style 缺 prefers-reduced-motion 兜底')
  return out
}, scope)
await b.close()
console.log(findings.length ? findings.join('\n') : 'CLEAN')
console.log(`\n${findings.length} findings @${vp}px ${url}`)