#!/usr/bin/env node
/**
 * structure-gate.mjs — enforces the ui/src import direction on resolved paths.
 *
 * The Gate problem it replaces: a string-prefix check (`text.includes('@/features')`)
 * misses relative imports, skips the `lib` layer, and looks at only the first
 * match per file. This gate parses each file into an AST, collects every module
 * specifier, resolves it to a real path under `ui/src`, and applies the rules
 * from `structure.rules.tsv` to the two resolved paths.
 *
 * Specifier handling:
 *   `@/x`        resolves under ui/src.
 *   `./x`, `../x` resolves next to the importing file.
 *   bare names   (react, @tanstack/*) are external and carry no direction.
 *
 * A relative or alias specifier that does not resolve, or that resolves outside
 * `ui/src`, is a finding. The gate never skips an unresolved in-project path.
 *
 * Exit codes:
 *   0  no finding
 *   1  violation
 *   2  configuration error (missing/empty rules file, empty scan set, bad rule)
 *
 * Usage:
 *   node scripts/structure-gate.mjs [--rules <file>]
 *   node scripts/structure-gate.mjs --self-test
 */

import { parse } from '@babel/parser'
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const UI_ROOT = resolve(HERE, '..')
const SRC_ROOT = join(UI_ROOT, 'src')
const DEFAULT_RULES = join(UI_ROOT, 'structure.rules.tsv')
const MODES = new Set(['deny', 'cross-feature'])
const JS_EXTENSIONS = ['', '.ts', '.tsx', '.d.ts', '/index.ts', '/index.tsx', '/index.d.ts']

class ConfigError extends Error {}

function toPosix(value) {
  return value.split(sep).join('/')
}

function isInside(parent, child) {
  const rel = relative(parent, child)
  return rel !== '' && !rel.startsWith('..') && !isAbsolute(rel)
}

function walk(dir) {
  const files = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name === '.git') continue
    const full = join(dir, entry.name)
    if (entry.isDirectory()) {
      files.push(...walk(full))
    } else if (entry.isFile() && (entry.name.endsWith('.ts') || entry.name.endsWith('.tsx'))) {
      files.push(full)
    }
  }
  return files.sort()
}

/** Parse `from <TAB> to <TAB> mode <TAB> note` rows. An empty file is fatal. */
export function loadRules(path) {
  let text
  try {
    text = readFileSync(path, 'utf8')
  } catch {
    throw new ConfigError(`rules file is missing: ${path}`)
  }
  const rules = []
  text.split(/\r?\n/).forEach((raw, index) => {
    const line = raw.trim()
    if (line === '' || line.startsWith('#')) return
    const parts = raw.split('\t')
    if (parts.length < 3) {
      throw new ConfigError(`${path}:${index + 1}: want from, to and mode`)
    }
    const from = parts[0].trim()
    const to = parts[1].trim()
    const mode = parts[2].trim()
    if (!from || !to) throw new ConfigError(`${path}:${index + 1}: empty layer name`)
    if (!MODES.has(mode)) throw new ConfigError(`${path}:${index + 1}: unknown mode ${JSON.stringify(mode)}`)
    rules.push({ from, to, mode, line: index + 1 })
  })
  if (rules.length === 0) {
    throw new ConfigError(`${path}: no rules; refused to pass`)
  }
  return rules
}

/** Every module specifier in one file, with its line. */
export function extractSpecifiers(file) {
  const text = readFileSync(file, 'utf8')
  let ast
  try {
    ast = parse(text, {
      sourceType: 'module',
      plugins: ['typescript', 'jsx'],
    })
  } catch (error) {
    throw new Error(`${file}: cannot parse: ${error.message}`)
  }

  const found = []
  const add = (node) => {
    if (!node || node.type !== 'StringLiteral') return
    found.push({ value: node.value, line: node.loc?.start.line ?? 0 })
  }
  const visit = (node) => {
    if (!node || typeof node !== 'object') return
    if (Array.isArray(node)) {
      for (const child of node) visit(child)
      return
    }
    switch (node.type) {
      case 'ImportDeclaration':
      case 'ExportNamedDeclaration':
      case 'ExportAllDeclaration':
        add(node.source)
        break
      case 'CallExpression':
        if (node.callee?.type === 'Import') add(node.arguments?.[0])
        break
      case 'TSImportType':
        if (node.argument?.type === 'TSLiteralType') add(node.argument.literal)
        break
      default:
        break
    }
    for (const key of Object.keys(node)) {
      if (key === 'loc' || key === 'start' || key === 'end' || key.endsWith('Comments') || key === 'extra') {
        continue
      }
      visit(node[key])
    }
  }
  visit(ast)
  return found
}

/**
 * Resolve one specifier against a source root.
 * Returns `{ kind: 'external' }`, `{ kind: 'resolved', path }` or
 * `{ kind: 'error', reason }`.
 */
export function resolveSpecifier(importer, specifier, srcRoot) {
  const clean = specifier.replace(/[?#].*$/, '')
  let base
  if (clean.startsWith('@/')) {
    base = resolve(srcRoot, clean.slice(2))
  } else if (clean.startsWith('./') || clean.startsWith('../')) {
    base = resolve(dirname(importer), clean)
  } else {
    return { kind: 'external' }
  }

  if (!isInside(srcRoot, base) && base !== srcRoot) {
    return { kind: 'error', reason: `resolves outside ui/src: ${specifier}` }
  }

  for (const suffix of JS_EXTENSIONS) {
    const candidate = `${base}${suffix}`
    if (existsSync(candidate) && statSync(candidate).isFile()) {
      return { kind: 'resolved', path: candidate }
    }
  }
  return { kind: 'error', reason: `cannot resolve ${specifier}` }
}

function layerOf(relPath) {
  const parts = relPath.split('/')
  return { layer: parts[0], feature: parts[0] === 'features' && parts.length > 2 ? parts[1] : null }
}

function judge(importerRel, targetRel, rules) {
  const from = layerOf(importerRel)
  const to = layerOf(targetRel)
  for (const rule of rules) {
    if (rule.from !== from.layer || rule.to !== to.layer) continue
    if (rule.mode === 'deny') {
      return `${from.layer} must not import ${to.layer} (structure.rules.tsv:${rule.line})`
    }
    if (rule.mode === 'cross-feature') {
      if (!from.feature || !to.feature) {
        return `feature import is not scoped to one feature (structure.rules.tsv:${rule.line})`
      }
      if (from.feature !== to.feature) {
        return `feature ${from.feature} must not import feature ${to.feature} (structure.rules.tsv:${rule.line})`
      }
    }
  }
  return null
}

/**
 * Analyse a file set. Throws ConfigError when the gate would be a no-op.
 * Returns `{ problems, files, specifiers }`.
 */
export function analyze(files, rules, srcRoot) {
  if (files.length === 0) {
    throw new ConfigError('scan set is empty: no .ts/.tsx files under ui/src')
  }
  if (rules.length === 0) {
    throw new ConfigError('rule set is empty: the gate would always pass')
  }
  const problems = []
  let specifiers = 0
  for (const file of files) {
    const importerRel = toPosix(relative(srcRoot, file))
    let found
    try {
      found = extractSpecifiers(file)
    } catch (error) {
      problems.push(`ui/src/${importerRel}: ${error.message}`)
      continue
    }
    for (const specifier of found) {
      specifiers += 1
      const resolved = resolveSpecifier(file, specifier.value, srcRoot)
      if (resolved.kind === 'external') continue
      if (resolved.kind === 'error') {
        problems.push(`ui/src/${importerRel}:${specifier.line}: ${resolved.reason}`)
        continue
      }
      const targetRel = toPosix(relative(srcRoot, resolved.path))
      const verdict = judge(importerRel, targetRel, rules)
      if (verdict) {
        problems.push(
          `ui/src/${importerRel}:${specifier.line}: imports ui/src/${targetRel}: ${verdict}`,
        )
      }
    }
  }
  return { problems, files, specifiers }
}

function writeFixture(root, files) {
  for (const [rel, body] of Object.entries(files)) {
    const full = join(root, ...rel.split('/'))
    mkdirSync(dirname(full), { recursive: true })
    writeFileSync(full, body)
  }
}

const POSITIVES = {
  'pages/HomePage.tsx': "import { A } from '@/features/auth/A'\n",
  'features/auth/A.tsx': "import { Button } from '@/components/ui/Button'\n",
  'components/ui/Button.tsx': "import { cx } from '@/lib/cx'\n",
  'features/auth/B.ts': "import { A } from './A'\n",
  'lib/util.ts': "import { cx } from './cx'\n",
  'lib/cx.ts': 'export function cx() {}\n',
  'pages/OtherPage.tsx': "import { A } from '../features/auth/A'\n",
  'features/auth/C.tsx': "import { A } from '@/features/auth/A'\n",
  'components/ui/Icon.tsx': 'export function Icon() {}\n',
  'features/school/S.ts': 'export const S = 1\n',
  'features/auth/A2.ts': 'export const A = 1\n',
}

const NEGATIVES = {
  'components/ui/BadAlias.tsx': "import { A } from '@/features/auth/A'\n",
  'components/shell/BadRelative.tsx': "import { A } from '../../features/auth/A'\n",
  'features/auth/BadSiblingRelative.tsx': "import { S } from '../school/S'\n",
  'features/auth/BadSiblingAlias.tsx': "import { S } from '@/features/school/S'\n",
  'lib/bad-feature.ts': "import { A } from '@/features/auth/A'\n",
  'lib/bad-components.ts': "import { B } from '../components/ui/Button'\n",
  'features/auth/BadPage.tsx': "import { H } from '@/pages/HomePage'\n",
  'components/ui/BadPage.tsx': "import { H } from '@/pages/HomePage'\n",
  'features/auth/BadSecond.tsx': "import { A } from './A'\nimport { S } from '@/features/school/S'\n",
  'components/ui/Unresolved.tsx': "import { X } from '@/features/missing/X'\n",
  'lib/Escape.ts': "import { X } from '../../outside/X'\n",
}

function selfTest() {
  const failures = []
  const expected = [
    'components/ui/BadAlias.tsx',
    'components/shell/BadRelative.tsx',
    'features/auth/BadSiblingRelative.tsx',
    'features/auth/BadSiblingAlias.tsx',
    'lib/bad-feature.ts',
    'lib/bad-components.ts',
    'features/auth/BadPage.tsx',
    'components/ui/BadPage.tsx',
    'features/auth/BadSecond.tsx',
    'components/ui/Unresolved.tsx',
    'lib/Escape.ts',
  ]

  const root = mkdtempSync(join(tmpdir(), 'structure-gate-'))
  const srcRoot = join(root, 'src')
  try {
    mkdirSync(srcRoot, { recursive: true })
    for (const [rel, body] of Object.entries(POSITIVES)) writeFixture(root, { [`src/${rel}`]: body })
    for (const [rel, body] of Object.entries(NEGATIVES)) writeFixture(root, { [`src/${rel}`]: body })
    writeFixture(root, { 'outside/X.ts': 'export const X = 1\n' })
    const rulesPath = join(root, 'rules.tsv')
    writeFileSync(rulesPath, readFileSync(DEFAULT_RULES, 'utf8'))

    const rules = loadRules(rulesPath)
    const files = walk(srcRoot)
    const { problems } = analyze(files, rules, srcRoot)

    for (const rel of expected) {
      const hit = problems.some((problem) => problem.startsWith(`ui/src/${rel}:`))
      if (!hit) failures.push(`negative fixture was not detected: ${rel}`)
    }
    for (const rel of Object.keys(POSITIVES)) {
      const hit = problems.some((problem) => problem.startsWith(`ui/src/${rel}:`))
      if (hit) failures.push(`positive fixture was flagged: ${rel}`)
    }
    if (!problems.some((problem) => problem.includes('cannot resolve @/features/missing/X'))) {
      failures.push('unresolved alias was not reported')
    }
    if (!problems.some((problem) => problem.includes('resolves outside ui/src'))) {
      failures.push('path escaping ui/src was not reported')
    }

    const expectConfigError = (label, run) => {
      try {
        run()
        failures.push(`${label} did not raise a config error`)
      } catch (error) {
        if (!(error instanceof ConfigError)) failures.push(`${label} raised the wrong error: ${error}`)
      }
    }
    expectConfigError('empty scan set', () => analyze([], rules, srcRoot))
    expectConfigError('empty rule set', () => analyze(files, [], srcRoot))
    const emptyRules = join(root, 'empty.tsv')
    writeFileSync(emptyRules, '# only a comment\n')
    expectConfigError('empty rules file', () => loadRules(emptyRules))
    const badMode = join(root, 'bad-mode.tsv')
    writeFileSync(badMode, 'lib\tfeatures\tallow\n')
    expectConfigError('unknown mode', () => loadRules(badMode))

    if (failures.length > 0) {
      for (const failure of failures) console.error(`structure-gate self-test FAIL: ${failure}`)
      return 1
    }
    console.log(
      `structure-gate self-test: ${Object.keys(POSITIVES).length} positive, ` +
        `${expected.length} negative, 4 config guards, ${problems.length} finding(s) on the fixture`,
    )
    return 0
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
}

function main() {
  const argv = process.argv.slice(2)
  if (argv.includes('--self-test')) return selfTest()

  let rulesPath = DEFAULT_RULES
  const flagIndex = argv.indexOf('--rules')
  if (flagIndex !== -1) {
    if (!argv[flagIndex + 1]) {
      console.error('structure-gate: CONFIG ERROR: --rules needs a path')
      return 2
    }
    rulesPath = resolve(argv[flagIndex + 1])
  }

  const rules = loadRules(rulesPath)
  if (!existsSync(SRC_ROOT)) {
    throw new ConfigError(`source root is missing: ${SRC_ROOT}`)
  }
  const files = walk(SRC_ROOT)
  const { problems, specifiers } = analyze(files, rules, SRC_ROOT)
  if (problems.length > 0) {
    for (const problem of problems) console.error(`structure-gate: ${problem}`)
    console.error(`structure-gate: FAILED with ${problems.length} finding(s)`)
    return 1
  }
  console.log(
    `structure-gate: ${files.length} files, ${specifiers} module specifiers, ` +
      `${rules.length} rules, 0 findings`,
  )
  return 0
}

try {
  process.exit(main())
} catch (error) {
  if (error instanceof ConfigError) {
    console.error(`structure-gate: CONFIG ERROR: ${error.message}`)
    process.exit(2)
  }
  throw error
}
