/**
 * **功能**
 * 快速 a11y 扫描：触达尺寸/图标按钮名称/标题跳级/文本对比度/横向溢出/
 * :focus-visible 与 prefers-reduced-motion 是否在样式中。无外部依赖。
 *
 * **归属**
 * `ui/tools` 走查工具链；每页收口前必跑。
 *
 * **口径**
 * 对比度按 WCAG 相对亮度，读浏览器计算样式（非静态扫 CSS）。计算色解析支持
 * rgb()/rgba()（0-255 与百分比、逗号或空格 + /alpha）与 color(srgb …)（含
 * color(srgb, …) 旧逗号写法；数值 0-1 与百分比两档；/alpha 数值与百分比）。
 * Chrome 把 color-mix(in srgb, …) 的计算值序列化为 color(srgb r g b / a)——
 * 即 04c F-08 的触发面：旧实现按 0-255 抓数字，color(srgb) 会被压成 ≈黑或整条跳过。
 * oklch/oklab/hsl 等写法不解析：计入 contrast-parse-skip 汇总行，不再静默跳过。
 * 半透明前景/背景不与底色合成，按不透明近似（沿用旧口径）。命中行含
 * 选择器/前景→背景/字号字重：contrast 比值<需求 "文本" 前景→背景 @选择器 字号/字重。
 * 对比度扫描对 `::after` 扩热区控件标「需实测」，不直接判缺陷。
 *
 * **自验**（纯 Node，不开浏览器、不需要凭据）
 * node tools/uapp-a11y.mjs --self-test
 *
 * **用法**
 * node tools/uapp-a11y.mjs --url http://localhost:5174/users --viewport 390 \
 *   --email <e> --password <p> [--theme light|dark] [--scope <选择器>] [--no-login]
 * --theme 在页面脚本前写 localStorage 键 dash26-theme（与 theme-boot.js 同键），
 * 供双主题复测；省略则跟随浏览器默认。
 */
import { launch, arg, login, creds } from './lib.mjs'

/**
 * 解析计算后颜色串 → 归一化 sRGB {r,g,b,a}∈[0,1]；不支持的写法返回 null。
 * 自包含：同时以源码形式注入浏览器（addInitScript），Node 自验与页面扫描共用同一实现。
 */
export function parseColor(c) {
  if (typeof c !== 'string') return null
  const s = c.trim().toLowerCase()
  if (!s || s === 'transparent' || s === 'currentcolor') return null
  const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v)
  const pct = (v) => (v.endsWith('%') ? parseFloat(v) / 100 : parseFloat(v))
  let m = /^color\(\s*srgb(?:\s+|,)(.+)\)$/.exec(s)
  if (m) {
    let body = m[1].trim()
    let alpha = null
    const slash = body.indexOf('/')
    if (slash >= 0) {
      alpha = body.slice(slash + 1).trim()
      if (/\s/.test(alpha)) return null
      body = body.slice(0, slash).trim()
    }
    let parts = body.split(/[\s,]+/).filter(Boolean)
    if (parts.length === 4 && alpha === null) {
      alpha = parts[3]
      parts = parts.slice(0, 3)
    }
    if (parts.length !== 3) return null
    const chan = parts.map((p) => pct(p))
    const a = alpha === null ? 1 : pct(alpha)
    if (chan.some((v) => !Number.isFinite(v)) || !Number.isFinite(a)) return null
    return { r: clamp01(chan[0]), g: clamp01(chan[1]), b: clamp01(chan[2]), a: clamp01(a) }
  }
  m = /^rgba?\(\s*(.+)\)$/.exec(s)
  if (m) {
    let body = m[1].trim()
    let alpha = null
    const slash = body.indexOf('/')
    if (slash >= 0) {
      alpha = body.slice(slash + 1).trim()
      if (/\s/.test(alpha)) return null
      body = body.slice(0, slash).trim()
    }
    const parts = body.split(/[\s,]+/).filter(Boolean)
    if (parts.length !== 3 && parts.length !== 4) return null
    const chan = parts.slice(0, 3).map((p) => (p.endsWith('%') ? parseFloat(p) / 100 : parseFloat(p) / 255))
    let a = 1
    if (parts.length === 4) a = parts[3].endsWith('%') ? parseFloat(parts[3]) / 100 : parseFloat(parts[3])
    if (alpha !== null) a = alpha.endsWith('%') ? parseFloat(alpha) / 100 : parseFloat(alpha)
    if (chan.some((v) => !Number.isFinite(v)) || !Number.isFinite(a)) return null
    return { r: clamp01(chan[0]), g: clamp01(chan[1]), b: clamp01(chan[2]), a: clamp01(a) }
  }
  return null
}

/** WCAG 相对亮度（0.03928 阈值沿用旧口径）；不可解析返回 null。 */
export function colorLuminance(c) {
  const p = parseColor(c)
  if (!p) return null
  const lin = (v) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4))
  return 0.2126 * lin(p.r) + 0.7152 * lin(p.g) + 0.0722 * lin(p.b)
}

/** --self-test 用例：两种 color(srgb) 写法 × 两档数值 × alpha，外加 rgb/rgba 回归与历史比值锚点。 */
function runSelfTest() {
  const rows = []
  const add = (desc, ok, detail = '') => rows.push({ desc, ok: !!ok, detail })
  const n = (a, b, tol = 0.011) => Number.isFinite(a) && Number.isFinite(b) && Math.abs(a - b) <= tol
  const lum = colorLuminance
  const contrastOf = (fg, bg) => {
    const f = lum(fg)
    const g = lum(bg)
    if (f === null || g === null) return null
    return (Math.max(f, g) + 0.05) / (Math.min(f, g) + 0.05)
  }
  const l64748b = lum('rgb(100, 116, 139)')
  // color(srgb)：空格写法 0-1 档；逗号写法；百分比档 × 两种写法
  add('color(srgb 0-1 档) 亮度≈rgb(100,116,139)（6 位小数截断，1e-4）', n(lum('color(srgb 0.392157 0.454902 0.545098)'), l64748b, 1e-4))
  add('color(srgb, 逗号写法) 同值', n(lum('color(srgb, 0.392157 0.454902 0.545098)'), l64748b, 1e-4))
  add('color(srgb 百分比档) 同值', n(lum('color(srgb 39.2157% 45.4902% 54.5098%)'), l64748b, 1e-4))
  add('color(srgb, 百分比档) 同值', n(lum('color(srgb, 39.2157% 45.4902% 54.5098%)'), l64748b, 1e-4))
  // alpha：数值档与百分比档；无 alpha 默认 1；alpha 不改变亮度（旧口径：不合成）
  const pA = parseColor('color(srgb 0.392157 0.454902 0.545098 / 0.86)')
  add('color(srgb / alpha 数值档)', !!pA && n(pA.a, 0.86, 1e-9) && n(pA.r, 0.392157, 1e-6))
  const pB = parseColor('color(srgb, 0.392157 0.454902 0.545098 / 86%)')
  add('color(srgb, / alpha 百分比档)', !!pB && n(pB.a, 0.86, 1e-9))
  add('color(srgb) 无 alpha 默认 1', (parseColor('color(srgb 0.1 0.2 0.3)') || {}).a === 1)
  add('alpha 不改变亮度（不合成口径）', n(lum('color(srgb 0.1 0.2 0.3 / 0.5)'), lum('color(srgb 0.1 0.2 0.3)'), 1e-9))
  const pC = parseColor('color(srgb, 0.4, 0.5, 0.6, 0.5)')
  add('color(srgb, 四参逗号旧草案) 宽容解析', !!pC && n(pC.r, 0.4, 1e-9) && n(pC.a, 0.5, 1e-9))
  // 旧缺陷回归：color(srgb 1 1 1) 曾被按 1/255 当黑，导致误报/漏报
  add('color(srgb 1 1 1) = 纯白（旧缺陷回归）', n(lum('color(srgb 1 1 1)'), 1, 1e-9))
  // color-mix(in srgb) 的 Chrome 计算序列化可测：danger 75% 混 t-base 在白底应达标
  const mixRatio = contrastOf('color(srgb 0.717647 0.222549 0.241176)', 'rgb(255, 255, 255)')
  add('color-mix(in srgb) 计算值红字/白底 ≥ 4.5', mixRatio !== null && mixRatio > 4.5, 'ratio=' + mixRatio)
  // 历史比值锚点（明色，手算复核过）
  add('#64748B/#F0F4F8 ≈ 4.31（muted 历史值）', n(contrastOf('rgb(100, 116, 139)', 'rgb(240, 244, 248)'), 4.31))
  add('#EF4444/白 ≈ 3.76（红字历史值）', n(contrastOf('rgb(239, 68, 68)', 'rgb(255, 255, 255)'), 3.76))
  add('#94A3B8/白 ≈ 2.56（明色表头历史值）', n(contrastOf('rgb(148, 163, 184)', 'rgb(255, 255, 255)'), 2.56))
  // rgb()/rgba() 回归：逗号、空格 + /alpha、百分比
  add('rgba(100,116,139,1) 亮度同值', n(lum('rgba(100, 116, 139, 1)'), l64748b, 1e-9))
  add('rgb(100 116 139 / 1) 亮度同值', n(lum('rgb(100 116 139 / 1)'), l64748b, 1e-9))
  add('rgb 百分比写法归一同值', n(lum('rgb(39.2157% 45.4902% 54.5098%)'), l64748b, 1e-6))
  add('rgba alpha 不影响亮度', n(lum('rgba(240, 244, 248, 0.6)'), lum('rgb(240, 244, 248)'), 1e-9))
  // 不支持的写法 → null（扫描时计入 parse-skip，不再静默）
  add('oklab(…) → null', parseColor('oklab(0.722741 -0.165537 0.0972287 / 0.14)') === null)
  add('oklch(…) → null', parseColor('oklch(0.623 0.188 25.5)') === null)
  add('hsl(…) → null', parseColor('hsl(215, 16%, 47%)') === null)
  add('transparent → null', parseColor('transparent') === null)
  add('空串 → null', parseColor('') === null)
  let failed = 0
  for (const r of rows) {
    if (!r.ok) failed++
    console.log((r.ok ? 'ok   ' : 'FAIL ') + r.desc + (r.detail ? ' → ' + r.detail : ''))
  }
  console.log(`\nself-test ${failed ? 'FAIL' : 'OK'} ${rows.length - failed}/${rows.length}`)
  return failed
}

if (process.argv.includes('--self-test')) process.exit(runSelfTest() ? 1 : 0)

const url = arg('url', 'http://localhost:5174/users')
const vp = Number(arg('viewport', '1440'))
const scope = arg('scope', null)
const theme = arg('theme', null)
if (theme && theme !== 'light' && theme !== 'dark') {
  console.error('--theme 仅支持 light|dark')
  process.exit(2)
}
const { email, password } = creds()
const noLogin = process.argv.includes('--no-login')

const b = await launch()
const page = await b.newPage({ viewport: { width: vp, height: vp < 768 ? 844 : 980 } })
// 颜色解析器注入页面：Node 自验与浏览器扫描共用同一函数源。
await page.addInitScript({
  content: parseColor.toString() + '\n' + colorLuminance.toString() + '\nwindow.__uappColor = { parseColor, colorLuminance }',
})
if (theme) {
  await page.addInitScript({ content: `try { localStorage.setItem('dash26-theme', ${JSON.stringify(theme)}) } catch {}` })
}
if (!noLogin) await login(page, new URL(url).origin, email, password)
await page.goto(url, { waitUntil: 'load' })
// SPA 内容渲染稳定后再扫：先等正文出现文本（上限 8s），再等元素数连续两轮不变。
// 背景：mock/dev 下路由数据链是异步的，load+固定延时期间常是 21 元素空白骨架，扫描会空转。
await page.waitForFunction(() => (document.body?.innerText || '').trim().length > 20, { timeout: 8000 }).catch(() => {})
for (let i = 0; i < 14; i++) {
  const n = await page.evaluate(() => document.querySelectorAll('*').length)
  await page.waitForTimeout(400)
  const n2 = await page.evaluate(() => document.querySelectorAll('*').length)
  if (n === n2 && n > 20) break
}
await page.waitForTimeout(300)

const findings = await page.evaluate((scopeSel) => {
  const root = scopeSel ? document.querySelector(scopeSel) : document
  if (!root) return ['scope 选择器未命中: ' + scopeSel]
  const { parseColor, colorLuminance } = window.__uappColor
  const out = []
  const bgOf = (el) => {
    let n = el
    while (n && n !== document.documentElement) {
      const c = getComputedStyle(n).backgroundColor
      if (c && c !== 'transparent') {
        const p = parseColor(c)
        if (p && p.a > 0) return c
      }
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
  let skipped = 0
  let skipEg = ''
  for (const el of root.querySelectorAll('p,span,h1,h2,h3,h4,button,a,td,th,label')) {
    const t = (el.textContent || '').trim()
    if (!t || el.children.length > 0 || seen.has(t)) continue
    seen.add(t)
    const cs = getComputedStyle(el)
    const bg = bgOf(el)
    const f = colorLuminance(cs.color)
    const b2 = colorLuminance(bg)
    if (f === null || b2 === null) {
      skipped++
      if (!skipEg) skipEg = f === null ? cs.color : bg
      continue
    }
    const ratio = (Math.max(f, b2) + 0.05) / (Math.min(f, b2) + 0.05)
    const size = parseFloat(cs.fontSize)
    const need = size >= 18 || (size >= 14 && Number(cs.fontWeight) >= 700) ? 3 : 4.5
    if (ratio < need) {
      let sel = el.tagName.toLowerCase()
      if (el.id) sel += '#' + el.id
      const cls = Array.from(el.classList).slice(0, 2).join('.')
      if (cls) sel += '.' + cls
      out.push(`contrast ${ratio.toFixed(2)}<${need} "${t.slice(0, 16)}" ${cs.color}→${bg} @${sel} ${size}px/${cs.fontWeight}`)
    }
    if (seen.size > 400) break
  }
  if (skipped > 0) out.push(`contrast-parse-skip ${skipped}（颜色写法未支持，如 ${skipEg}）`)
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
console.log(`\n${findings.length} findings @${vp}px${theme ? ' theme=' + theme : ''} ${url}`)
