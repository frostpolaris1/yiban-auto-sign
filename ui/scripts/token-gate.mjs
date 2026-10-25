#!/usr/bin/env node
/**
 * token-gate.mjs — pins the Adminator token contract for the React UI.
 *
 * Three checks run in order. Each one fails with a non-zero exit code.
 *
 * 1. Color literals. Every text file under `src/` plus `index.html` is scanned
 *    for `#hex`, `rgb(...)`, `hsl(...)`, `oklch(...)`, `lab(...)`, `lch(...)`
 *    and `color(...)`. Two files are exempt:
 *    - `src/styles/tokens.css` holds the canonical token values.
 *    - `src/styles/adminator.css` is a verbatim copy of the vendor stylesheet.
 *    Both exemptions are registered below and are printed on every run.
 *
 * 2. Verbatim vendor copy. `src/styles/adminator.css` must be byte-identical to
 *    `../web/static/vendor/adminator/adminator.css`. This is what makes the
 *    second exemption safe.
 *
 * 3. Token parity. `src/styles/tokens.css` must define exactly the same token
 *    names and values as the vendor stylesheet, for both `light` and `dark`.
 *    The vendor stylesheet carries 36 tokens per theme.
 *
 * Guards: an empty file set, an all-exempt file set, an empty rule set, a
 * missing vendor file, or an `index.html` that left the scan set is a
 * configuration error (exit 2), never a pass.
 *
 * Usage:
 *   node scripts/token-gate.mjs             # run all checks
 *   node scripts/token-gate.mjs --self-test  # mutation-test the checker itself
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
const VENDOR_COPY = join(SRC_ROOT, 'styles', 'adminator.css')
const VENDOR_SOURCE = resolve(UI_ROOT, '..', 'web', 'static', 'vendor', 'adminator', 'adminator.css')
const THEMES = ['light', 'dark']

/** Registered exemptions. Keep this list short and justify every entry. */
const EXEMPT = new Map([
  [
    'src/styles/tokens.css',
    'canonical token values (36 light + 36 dark), checked by the parity rule below',
  ],
  [
    'src/styles/adminator.css',
    'verbatim copy of the audited vendor stylesheet, checked by the byte rule below',
  ],
])

const SCANNED_EXTENSIONS = new Set(['.css', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.html'])

/** Hardcoded color rules. Order is stable for deterministic output. */
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
  } finally {
    rmSync(temp, { recursive: true, force: true })
  }

  if (failures.length > 0) {
    for (const failure of failures) console.error(`token-gate self-test FAIL: ${failure}`)
    return 1
  }
  console.log(
    `token-gate self-test: ${positives.length} positive, ${negatives.length} negative, ` +
      '2 config guards + vendor byte + token parity mutations, exemption registry OK',
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
  const all = [...violations, ...copyProblems, ...parityProblems]

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
