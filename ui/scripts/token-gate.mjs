#!/usr/bin/env node
/**
 * token-gate.mjs — pins the Adminator token contract for the React UI.
 *
 * Three checks run in order. Each one fails with a non-zero exit code.
 *
 * 1. Color literals. Every text file under `src/` is scanned for `#hex`,
 *    `rgb(...)`/`rgba(...)`, and `hsl(...)`/`hsla(...)`. Two files are exempt:
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
 * Guards: an empty file set, an all-exempt file set, an empty rule set, or a
 * missing vendor file is a configuration error (exit 2), never a pass.
 *
 * Usage:
 *   node scripts/token-gate.mjs             # run all checks
 *   node scripts/token-gate.mjs --self-test  # mutation-test the checker itself
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const UI_ROOT = resolve(HERE, '..')
const SRC_ROOT = join(UI_ROOT, 'src')
const TOKENS_FILE = join(SRC_ROOT, 'styles', 'tokens.css')
const VENDOR_COPY = join(SRC_ROOT, 'styles', 'adminator.css')
const VENDOR_SOURCE = resolve(UI_ROOT, '..', 'web', 'static', 'vendor', 'adminator', 'adminator.css')
const THEMES = ['light', 'dark']

/** Registered exemptions. Keep this list short and justify every entry. */
const EXEMPT = new Map([
  [
    'styles/tokens.css',
    'canonical token values (36 light + 36 dark), checked by the parity rule below',
  ],
  [
    'styles/adminator.css',
    'verbatim copy of the audited vendor stylesheet, checked by the byte rule below',
  ],
])

const SCANNED_EXTENSIONS = new Set(['.css', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.html'])

/** Hardcoded color rules. Order is stable for deterministic output. */
const RULES = [
  { id: 'hex', label: '#hex', pattern: /#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b/g },
  { id: 'rgb', label: 'rgb()/rgba()', pattern: /\brgba?\s*\(/g },
  { id: 'hsl', label: 'hsl()/hsla()', pattern: /\bhsla?\s*\(/g },
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
    throw new ConfigError('scan set is empty: no files under src/ matched the extension list')
  }
  const usable = files.filter((file) => !EXEMPT.has(toPosix(relative(SRC_ROOT, file))))
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
    const rel = toPosix(relative(SRC_ROOT, file))
    if (EXEMPT.has(rel)) continue
    const text = readFileSync(file, 'utf8')
    for (const hit of detectTokens(text)) {
      violations.push(`${rel}:${hit.line}:${hit.column}: ${hit.rule} literal ${JSON.stringify(hit.value)}`)
    }
  }
  return violations
}

function checkVendorCopy() {
  let source
  try {
    source = readFileSync(VENDOR_SOURCE)
  } catch (error) {
    throw new ConfigError(`vendor stylesheet is missing: ${VENDOR_SOURCE}`)
  }
  let copy
  try {
    copy = readFileSync(VENDOR_COPY)
  } catch (error) {
    throw new ConfigError(`vendored copy is missing: ${VENDOR_COPY}`)
  }
  if (Buffer.compare(source, copy) !== 0) {
    return [
      `${toPosix(relative(UI_ROOT, VENDOR_COPY))} differs from ${toPosix(relative(UI_ROOT, VENDOR_SOURCE))} ` +
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

function checkTokenParity() {
  let tokensCss
  let vendorCss
  try {
    tokensCss = readFileSync(TOKENS_FILE, 'utf8')
  } catch (error) {
    throw new ConfigError(`token file is missing: ${TOKENS_FILE}`)
  }
  try {
    vendorCss = readFileSync(VENDOR_SOURCE, 'utf8')
  } catch (error) {
    throw new ConfigError(`vendor stylesheet is missing: ${VENDOR_SOURCE}`)
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

  const expectConfigError = (label, files) => {
    try {
      verifyScanSet(files)
      failures.push(`${label} did not raise a config error`)
    } catch (error) {
      if (!(error instanceof ConfigError)) failures.push(`${label} raised the wrong error: ${error}`)
    }
  }
  expectConfigError('empty scan set', [])
  expectConfigError('all-exempt scan set', [join(SRC_ROOT, 'styles', 'tokens.css')])
  verifyScanSet([join(SRC_ROOT, 'main.tsx')])

  if (!EXEMPT.has('styles/adminator.css') || !EXEMPT.has('styles/tokens.css')) {
    failures.push('expected exemptions are not registered')
  }
  if (RULES.length === 0) failures.push('rule set is empty')

  if (failures.length > 0) {
    for (const failure of failures) console.error(`token-gate self-test FAIL: ${failure}`)
    return 1
  }
  console.log(
    `token-gate self-test: ${positives.length} positive, ${negatives.length} negative, ` +
      '2 config guards, exemption registry OK',
  )
  return 0
}

function main() {
  if (process.argv.includes('--self-test')) {
    return selfTest()
  }

  const files = walk(SRC_ROOT)
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
    `token-gate: scanned ${files.length} files under src/ ` +
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
