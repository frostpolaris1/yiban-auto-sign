#!/usr/bin/env node
/**
 * token-gate.mjs — 钉住 React UI 的 Adminator 令牌契约。
 *
 * 三项检查按序执行。任一项失败即非零退出。
 *
 * 1. 颜色字面量。扫描 `src/` 全部文本文件与 `index.html`。
 *    规则覆盖 `#hex`、`rgb(...)`、`hsl(...)`、`oklch(...)`、
 *    `lab(...)`、`lch(...)` 与 `color(...)`。两个文件免扫：
 *    - `src/styles/tokens.css` 持有令牌真值。
 *    - `src/styles/adminator.css` 是 vendor 样式表的逐字节副本。
 *    两项豁免在此登记，每次运行都打印。
 *
 * 2. vendor 副本。`src/styles/adminator.css` 必须与
 *    `vendor/adminator/adminator.css` 逐字节相同。
 *    第 2 项检查是第 1 项豁免可信的前提。
 *
 * 3. 令牌对齐。`src/styles/tokens.css` 的令牌名与值必须与 vendor 完全一致，
 *    浅色与深色两套都比。vendor 每套 36 个令牌。
 *
 * 守卫：扫描集为空、全为豁免文件、规则集为空、vendor 文件缺失、
 * 或 `index.html` 掉出扫描集，都是配置错误（退出 2），不得当通过。
 *
 * 已知限制：CSS 命名颜色（如 `red`）不检测。
 * 完整名单有 148 个名字。部分名单会给出虚假信心。
 * 规则集覆盖本仓可能用到的全部函数式写法。
 *
 * 用法：
 *   node scripts/token-gate.mjs              # 跑全部检查
 *   node scripts/token-gate.mjs --self-test  # 对检查器自身做突变测试
 */

import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const UI_ROOT = resolve(HERE, '..')
const SRC_ROOT = join(UI_ROOT, 'src')
const INDEX_FILE = join(UI_ROOT, 'index.html')
const TOKENS_FILE = join(SRC_ROOT, 'styles', 'tokens.css')
const USER_NEXT_TOKENS_FILE = join(SRC_ROOT, 'styles', 'user-next', 'tokens.css')
const VENDOR_COPY = join(SRC_ROOT, 'styles', 'adminator.css')
const VENDOR_SOURCE = join(UI_ROOT, 'vendor', 'adminator', 'adminator.css')
const THEMES = ['light', 'dark']

/** 已登记豁免。名单保持短，每项写明理由。 */
const EXEMPT = new Map([
  [
    'src/styles/tokens.css',
    'canonical token values (36 light + 36 dark), checked by the parity rule below',
  ],
  [
    'src/styles/adminator.css',
    'verbatim copy of the audited vendor stylesheet, checked by the byte rule below',
  ],
  [
    'src/styles/user-next/tokens.css',
    'isolated user-next tokens, checked by the bare-root theme rule below',
  ],
])

const SCANNED_EXTENSIONS = new Set(['.css', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.html'])

/** 颜色规则写死。顺序固定，保证输出可重复。 */
const RULES = [
  { id: 'hex', label: '#hex', pattern: /#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b/g },
  { id: 'rgb', label: 'rgb()/rgba()', pattern: /\brgba?\s*\(/g },
  { id: 'hsl', label: 'hsl()/hsla()', pattern: /\bhsla?\s*\(/g },
  { id: 'oklch', label: 'oklch()', pattern: /\boklch\s*\(/g },
  { id: 'lab', label: 'lab()', pattern: /\blab\s*\(/g },
  { id: 'lch', label: 'lch()', pattern: /\blch\s*\(/g },
  { id: 'color', label: 'color()', pattern: /\bcolor\s*\(/g },
]

class ConfigError extends Error {}

function toPosix(value) {
  return value.split(sep).join('/')
}

function walk(dir) {
  const files = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) {
      files.push(...walk(full))
    } else if (entry.isFile() && SCANNED_EXTENSIONS.has(entry.name.slice(entry.name.lastIndexOf('.')))) {
      files.push(full)
    }
  }
  return files
}

function scanSet() {
  if (!existsSync(SRC_ROOT)) {
    throw new ConfigError(`source root is missing: ${SRC_ROOT}`)
  }
  const files = walk(SRC_ROOT)
  if (!files.includes(INDEX_FILE)) files.push(INDEX_FILE)
  return files
}

function lineAndColumn(text, index) {
  const before = text.slice(0, index)
  const line = before.split('\n').length
  const column = index - before.lastIndexOf('\n')
  return { line, column }
}

function detectTokens(text) {
  const hits = []
  for (const rule of RULES) {
    rule.pattern.lastIndex = 0
    let match
    while ((match = rule.pattern.exec(text)) !== null) {
      const { line, column } = lineAndColumn(text, match.index)
      hits.push({ rule: rule.label, value: match[0], line, column, index: match.index })
    }
  }
  hits.sort((a, b) => a.index - b.index)
  return hits
}

function verifyScanSet(files) {
  if (files.length === 0) {
    throw new ConfigError('scan set is empty: no files under src/ or index.html')
  }
  const usable = files.filter((file) => !EXEMPT.has(toPosix(relative(UI_ROOT, file))))
  if (usable.length === 0) {
    throw new ConfigError('scan set contains only exempt files: the gate would be a no-op')
  }
  if (RULES.length === 0) {
    throw new ConfigError('rule set is empty: the gate would always pass')
  }
  return usable
}

function checkColors(files) {
  const violations = []
  for (const file of files) {
    const rel = toPosix(relative(UI_ROOT, file))
    if (EXEMPT.has(rel)) continue
    const text = readFileSync(file, 'utf8')
    for (const hit of detectTokens(text)) {
      violations.push(`${rel}:${hit.line}:${hit.column}: ${hit.rule} literal ${JSON.stringify(hit.value)}`)
    }
  }
  return violations
}

function checkVendorCopy(sourcePath = VENDOR_SOURCE, copyPath = VENDOR_COPY) {
  let source
  try {
    source = readFileSync(sourcePath)
  } catch {
    throw new ConfigError(`vendor stylesheet is missing: ${sourcePath}`)
  }
  let copy
  try {
    copy = readFileSync(copyPath)
  } catch {
    throw new ConfigError(`vendored copy is missing: ${copyPath}`)
  }
  if (Buffer.compare(source, copy) !== 0) {
    return [
      `${toPosix(relative(UI_ROOT, copyPath))} differs from ${toPosix(relative(UI_ROOT, sourcePath))} ` +
        `(${copy.length} B vs ${source.length} B)`,
    ]
  }
  return []
}

function extractTokenBlocks(css) {
  const blocks = {}
  const pattern = /:root\[data-theme=["']?(light|dark)["']?\]\s*\{([^}]*)\}/g
  let match
  while ((match = pattern.exec(css)) !== null) {
    const theme = match[1]
    const body = match[2].replace(/\/\*[\s\S]*?\*\//g, '')
    if (blocks[theme]) {
      throw new ConfigError(`duplicate :root[data-theme=${theme}] block`)
    }
    const map = new Map()
    for (const raw of body.split(';')) {
      const declaration = raw.trim()
      if (!declaration) continue
      const colon = declaration.indexOf(':')
      if (colon === -1) {
        throw new ConfigError(`malformed token declaration: ${declaration}`)
      }
      const name = declaration.slice(0, colon).trim()
      const value = declaration
        .slice(colon + 1)
        .trim()
        .replace(/\s+/g, ' ')
      if (!name.startsWith('--')) {
        throw new ConfigError(`declaration is not a custom property: ${declaration}`)
      }
      if (map.has(name)) {
        throw new ConfigError(`duplicate token ${name} in ${theme}`)
      }
      map.set(name, value)
    }
    blocks[theme] = map
  }
  return blocks
}

function checkTokenParity(tokensPath = TOKENS_FILE, vendorPath = VENDOR_SOURCE) {
  let tokensCss
  let vendorCss
  try {
    tokensCss = readFileSync(tokensPath, 'utf8')
  } catch {
    throw new ConfigError(`token file is missing: ${tokensPath}`)
  }
  try {
    vendorCss = readFileSync(vendorPath, 'utf8')
  } catch {
    throw new ConfigError(`vendor stylesheet is missing: ${vendorPath}`)
  }
  const tokensBlocks = extractTokenBlocks(tokensCss)
  const vendorBlocks = extractTokenBlocks(vendorCss)
  const problems = []
  for (const theme of THEMES) {
    const expected = vendorBlocks[theme]
    const actual = tokensBlocks[theme]
    if (!expected) throw new ConfigError(`vendor stylesheet has no ${theme} token block`)
    if (!actual) {
      problems.push(`tokens.css has no :root[data-theme="${theme}"] block`)
      continue
    }
    for (const [name, value] of expected) {
      if (!actual.has(name)) {
        problems.push(`${theme}: ${name} missing`)
      } else if (actual.get(name) !== value) {
        problems.push(`${theme}: ${name} is ${actual.get(name)}, vendor says ${value}`)
      }
    }
    for (const name of actual.keys()) {
      if (!expected.has(name)) problems.push(`${theme}: ${name} is not a vendor token`)
    }
  }
  return problems
}


function extractScopedTokenBlocks(css) {
  const blocks = {}
  const pattern = /:root\[data-theme=["']?(light|dark)["']?\]\s*\{([^}]*)\}/g
  let match
  while ((match = pattern.exec(css)) !== null) {
    const theme = match[1]
    if (blocks[theme]) {
      throw new ConfigError(`duplicate :root[data-theme=${theme}] block`)
    }
    const body = match[2].replace(/\/\*[\s\S]*?\*\//g, '')
    const map = new Map()
    for (const raw of body.split(';')) {
      const declaration = raw.trim()
      if (!declaration) continue
      const colon = declaration.indexOf(':')
      if (colon === -1) {
        throw new ConfigError(`malformed user-next token declaration: ${declaration}`)
      }
      const name = declaration.slice(0, colon).trim()
      const value = declaration
        .slice(colon + 1)
        .trim()
        .replace(/\s+/g, ' ')
      if (!name.startsWith('--')) {
        throw new ConfigError(`user-next declaration is not a custom property: ${declaration}`)
      }
      if (!value) {
        throw new ConfigError(`user-next token has no value: ${name}`)
      }
      if (map.has(name)) {
        throw new ConfigError(`duplicate user-next token ${name} in ${theme}`)
      }
      map.set(name, value)
    }
    blocks[theme] = map
  }
  return blocks
}

function checkUserNextTokens(tokensPath = USER_NEXT_TOKENS_FILE) {
  let css
  try {
    css = readFileSync(tokensPath, 'utf8')
  } catch {
    throw new ConfigError(`user-next token file is missing: ${tokensPath}`)
  }
  const clean = css.replace(/\/\*[\s\S]*?\*\//g, '')
  const blocks = extractScopedTokenBlocks(clean)
  const problems = []
  const pattern = /:root\[data-theme=["']?(light|dark)["']?\]\s*\{([^}]*)\}/g
  const outside = clean.replace(pattern, '')
  if (outside.trim() !== '') {
    problems.push(
      /--un-[\w-]*\s*:/.test(outside)
        ? 'user-next tokens must live inside :root[data-theme] blocks'
        : 'user-next token file may only contain bare :root[data-theme] blocks',
    )
  }
  const rootSelectors = (clean.match(/:root\[data-theme=["']?(?:light|dark)["']?\]/g) ?? []).length
  const scopedSelectors = Object.keys(blocks).length
  if (rootSelectors !== scopedSelectors) {
    problems.push(`user-next tokens must be bare :root[data-theme] blocks (found ${rootSelectors} theme selectors, ${scopedSelectors} parsed blocks)`)
  }
  for (const theme of THEMES) {
    if (!blocks[theme]) problems.push(`user-next tokens have no :root[data-theme="${theme}"] block`)
  }
  for (const [theme, map] of Object.entries(blocks)) {
    for (const name of map.keys()) {
      if (!name.startsWith('--un-')) problems.push(`${theme}: ${name} is not an --un- token`)
    }
  }
  if (blocks.light && blocks.dark) {
    const lightNames = [...blocks.light.keys()].sort()
    const darkNames = [...blocks.dark.keys()].sort()
    if (lightNames.length < 10) problems.push(`user-next tokens have only ${lightNames.length} light tokens; at least 10 are required`)
    if (lightNames.join('\n') !== darkNames.join('\n')) {
      const missingDark = lightNames.filter((name) => !blocks.dark.has(name))
      const missingLight = darkNames.filter((name) => !blocks.light.has(name))
      if (missingDark.length > 0) problems.push(`dark: missing ${missingDark.join(', ')}`)
      if (missingLight.length > 0) problems.push(`light: missing ${missingLight.join(', ')}`)
    }
  }
  return problems
}

function selfTest() {
  const positives = [
    'color: #fff;',
    'color: #FFFFFF;',
    'color: #2563ebcc;',
    'background: rgb(1 2 3);',
    'box-shadow: 0 0 0 3px rgba(0, 0, 0, 0.2);',
    'border-color: hsl(210 40% 98%);',
    'outline-color: hsla(210, 40%, 98%, 0.5);',
    'color: oklch(0.7 0.1 200);',
    'color: lab(50% 40 59);',
    'color: lch(50% 40 30);',
    'color: color(display-p3 1 0 0);',
  ]
  const negatives = [
    'color: var(--primary);',
    'background: var(--bg-card);',
    'href="#"',
    'id="design-tab-a"',
    'const token = "var(--primary)"',
    'className="nav-link is-active"',
  ]
  const failures = []

  positives.forEach((sample, index) => {
    if (detectTokens(sample).length === 0) failures.push(`positive sample ${index} was not detected: ${sample}`)
  })
  negatives.forEach((sample, index) => {
    const hits = detectTokens(sample)
    if (hits.length !== 0) failures.push(`negative sample ${index} was detected: ${sample} -> ${hits.map((h) => h.value).join(', ')}`)
  })

  const expectConfigError = (label, run) => {
    try {
      run()
      failures.push(`${label} did not raise a config error`)
    } catch (error) {
      if (!(error instanceof ConfigError)) failures.push(`${label} raised the wrong error: ${error}`)
    }
  }
  expectConfigError('empty scan set', () => verifyScanSet([]))
  expectConfigError('all-exempt scan set', () => verifyScanSet([TOKENS_FILE]))
  verifyScanSet([join(SRC_ROOT, 'main.tsx')])
  expectConfigError('missing vendor file', () => checkVendorCopy(join(UI_ROOT, 'nope.css'), VENDOR_COPY))

  if (!EXEMPT.has('src/styles/adminator.css') || !EXEMPT.has('src/styles/tokens.css')) {
    failures.push('expected exemptions are not registered')
  }
  if (RULES.length === 0) failures.push('rule set is empty')
  if (!scanSet().includes(INDEX_FILE)) failures.push('index.html left the scan set')

  const temp = mkdtempSync(join(tmpdir(), 'token-gate-'))
  try {
    const vendorCss =
      ':root[data-theme="light"] { --a: 1px; --b: 2px; }\n' +
      ':root[data-theme="dark"] { --a: 3px; --b: 4px; }\n'
    const vendorFile = join(temp, 'vendor.css')
    const copyFile = join(temp, 'copy.css')
    writeFileSync(vendorFile, vendorCss)
    writeFileSync(copyFile, vendorCss)
    if (checkVendorCopy(vendorFile, copyFile).length !== 0) {
      failures.push('vendor byte rule flagged an identical copy')
    }
    writeFileSync(copyFile, `${vendorCss} `)
    if (checkVendorCopy(vendorFile, copyFile).length === 0) {
      failures.push('vendor byte rule missed a changed byte')
    }

    const tokensFile = join(temp, 'tokens.css')
    writeFileSync(tokensFile, vendorCss)
    if (checkTokenParity(tokensFile, vendorFile).length !== 0) {
      failures.push('token parity rule flagged aligned tokens')
    }
    writeFileSync(
      tokensFile,
      ':root[data-theme="light"] { --a: 1px; }\n' +
        ':root[data-theme="dark"] { --a: 3px; --b: 4px; }\n',
    )
    if (!checkTokenParity(tokensFile, vendorFile).some((problem) => problem.includes('--b missing'))) {
      failures.push('token parity rule missed a missing token')
    }
    writeFileSync(
      tokensFile,
      ':root[data-theme="light"] { --a: 9px; --b: 2px; }\n' +
        ':root[data-theme="dark"] { --a: 3px; --b: 4px; }\n',
    )
    if (!checkTokenParity(tokensFile, vendorFile).some((problem) => problem.includes('--a is 9px'))) {
      failures.push('token parity rule missed a wrong value')
    }

    const userNextFile = join(temp, 'user-next-tokens.css')
    const lightTokens = Array.from({ length: 10 }, (_, index) => `--un-t${index}: ${index}px;`).join(' ')
    const darkTokens = Array.from({ length: 10 }, (_, index) => `--un-t${index}: ${index + 10}px;`).join(' ')
    const scopedBlock = (theme, body) => `:root[data-theme="${theme}"] { ${body} }\n`
    writeFileSync(userNextFile, scopedBlock('light', lightTokens) + scopedBlock('dark', darkTokens))
    if (checkUserNextTokens(userNextFile).length !== 0) {
      failures.push('user-next token rule flagged a valid root pair')
    }
    writeFileSync(userNextFile, scopedBlock('light', lightTokens))
    if (!checkUserNextTokens(userNextFile).some((problem) => problem.includes('no :root[data-theme="dark"]'))) {
      failures.push('user-next token rule missed a missing dark theme')
    }
    writeFileSync(
      userNextFile,
      scopedBlock('light', lightTokens.replace('--un-t0:', '--t0:')) +
        scopedBlock('dark', darkTokens.replace('--un-t0:', '--t0:')),
    )
    if (!checkUserNextTokens(userNextFile).some((problem) => problem.includes('not an --un- token'))) {
      failures.push('user-next token rule missed an unprefixed token')
    }
    writeFileSync(
      userNextFile,
      scopedBlock('light', `${lightTokens} --un-t0: 99px;`) + scopedBlock('dark', darkTokens),
    )
    expectConfigError('duplicate user-next token', () => checkUserNextTokens(userNextFile))
    writeFileSync(
      userNextFile,
      scopedBlock('light', lightTokens) +
        scopedBlock('dark', darkTokens) +
        ':root[data-theme="light"] .un-root { --un-bare: 1px; }\n',
    )
    if (!checkUserNextTokens(userNextFile).some((problem) => problem.includes('bare :root'))) {
      failures.push('user-next token rule missed a scoped block')
    }
    writeFileSync(
      userNextFile,
      scopedBlock('light', lightTokens) + scopedBlock('dark', darkTokens) + '.un-root { --un-raw: 1px; }\n',
    )
    if (!checkUserNextTokens(userNextFile).some((problem) => problem.includes('must live inside'))) {
      failures.push('user-next token rule missed a token outside a theme block')
    }
    writeFileSync(
      userNextFile,
      scopedBlock('light', lightTokens) +
        scopedBlock('dark', darkTokens) +
        '.un-root { color: var(--un-ink); }\n',
    )
    if (
      !checkUserNextTokens(userNextFile).some((problem) =>
        problem.includes('may only contain'),
      ) ||
      checkUserNextTokens(userNextFile).some((problem) => problem.includes('must live inside'))
    ) {
      failures.push('user-next token rule misclassified a var reference outside a theme block')
    }
    writeFileSync(
      userNextFile,
      scopedBlock('light', lightTokens) + scopedBlock('dark', darkTokens) + '.un-root { color: red; }\n',
    )
    if (!checkUserNextTokens(userNextFile).some((problem) => problem.includes('may only contain'))) {
      failures.push('user-next token rule missed a bare declaration outside a theme block')
    }
    writeFileSync(
      userNextFile,
      scopedBlock('light', lightTokens) +
        scopedBlock('dark', darkTokens.replace(' --un-t9: 19px;', '')),
    )
    if (!checkUserNextTokens(userNextFile).some((problem) => problem.includes('dark: missing --un-t9'))) {
      failures.push('user-next token rule missed a theme name mismatch')
    }
  } finally {
    rmSync(temp, { recursive: true, force: true })
  }

  if (failures.length > 0) {
    for (const failure of failures) console.error(`token-gate self-test FAIL: ${failure}`)
    return 1
  }
  console.log(
    `token-gate self-test: ${positives.length} positive, ${negatives.length} negative, ` +
      '2 config guards + vendor byte + token parity + user-next root-scope mutations, exemption registry OK',
  )
  return 0
}

function main() {
  if (process.argv.includes('--self-test')) {
    return selfTest()
  }

  const files = scanSet()
  const usable = verifyScanSet(files)
  const violations = checkColors(files)
  const copyProblems = checkVendorCopy()
  const parityProblems = checkTokenParity()
  const userNextProblems = checkUserNextTokens()
  const all = [...violations, ...copyProblems, ...parityProblems, ...userNextProblems]

  if (all.length > 0) {
    for (const problem of all) console.error(`token-gate: ${problem}`)
    console.error(`token-gate: FAILED with ${all.length} finding(s)`)
    return 1
  }

  for (const [path, reason] of EXEMPT) {
    console.log(`token-gate: exempt ${path} — ${reason}`)
  }
  console.log(
    `token-gate: scanned ${files.length} files (src/ + index.html) ` +
      `(${files.length - usable.length} exempt, ${usable.length} enforced), ` +
      `rules ${RULES.map((rule) => rule.label).join(', ')}, 0 violations`,
  )
  console.log('token-gate: vendor copy byte-identical; 36 light + 36 dark tokens match vendor values')
  console.log('token-gate: user-next tokens live under bare :root[data-theme]; --un- prefix; identical light/dark names')
  return 0
}

try {
  process.exit(main())
} catch (error) {
  if (error instanceof ConfigError) {
    console.error(`token-gate: CONFIG ERROR: ${error.message}`)
    process.exit(2)
  }
  throw error
}
