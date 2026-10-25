#!/usr/bin/env node
/**
 * doc-ref-gate.mjs — keeps committed UI text free of uncommitted `work/` paths.
 *
 * `work/` holds local plans, hand-offs and review output. It is not in the
 * repository. A committed file that points into `work/` sends the reader to a
 * path that does not exist after a clone. Load-bearing facts must live in the
 * committed file itself (AGENTS section 24).
 *
 * Scan set: every text file under ui/ with a checked extension, except
 * node_modules, dist, test-results, playwright-report and lock files.
 *
 * Exit codes:
 *   0  no finding
 *   1  a `work/` path is referenced
 *   2  configuration error (empty scan set)
 *
 * Usage:
 *   node scripts/doc-ref-gate.mjs
 *   node scripts/doc-ref-gate.mjs --self-test
 */

import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const UI_ROOT = resolve(HERE, '..')
const EXTENSIONS = new Set(['.md', '.ts', '.tsx', '.css', '.html', '.mjs', '.json'])
const SKIP_DIRS = new Set(['node_modules', 'dist', 'test-results', 'playwright-report', 'blob-report'])
const SKIP_FILES = new Set(['package-lock.json'])
/** The gate documents the forbidden token and tests it, so it scans itself out. */
const EXEMPT = new Set(['scripts/doc-ref-gate.mjs'])
const RULE = /(^|[^\w])work\/[\w./-]*/g

class ConfigError extends Error {}

function toPosix(value) {
  return value.split(sep).join('/')
}

function walk(dir, acc = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name)) walk(join(dir, entry.name), acc)
      continue
    }
    if (!entry.isFile()) continue
    const dot = entry.name.lastIndexOf('.')
    if (dot === -1 || !EXTENSIONS.has(entry.name.slice(dot))) continue
    if (SKIP_FILES.has(entry.name)) continue
    acc.push(join(dir, entry.name))
  }
  return acc
}

function lineAndColumn(text, index) {
  const before = text.slice(0, index)
  return { line: before.split('\n').length, column: index - before.lastIndexOf('\n') }
}

function detect(text) {
  const hits = []
  RULE.lastIndex = 0
  let match
  while ((match = RULE.exec(text)) !== null) {
    const value = match[0].trim()
    const offset = match.index + match[0].indexOf(value)
    const { line, column } = lineAndColumn(text, offset)
    hits.push({ value, line, column })
  }
  return hits
}

function check(files) {
  if (files.length === 0) {
    throw new ConfigError('scan set is empty: no committed UI text files')
  }
  const findings = []
  for (const file of files) {
    const rel = toPosix(relative(UI_ROOT, file))
    if (EXEMPT.has(rel)) continue
    const text = readFileSync(file, 'utf8')
    for (const hit of detect(text)) {
      findings.push(`${rel}:${hit.line}:${hit.column}: references ${hit.value}`)
    }
  }
  return findings
}

function selfTest() {
  const failures = []
  const positives = [
    'Clone the vendor tree outside the repository.',
    'The vendored stylesheet is byte-identical to `web/static/vendor/adminator/adminator.css`.',
    'See `THIRD_PARTY_NOTICES.md` for the license.',
  ]
  const negatives = [
    'Clone: `work/adminator` (read only).',
    'Mapping lives in work/handoff/2026-10-25-p1b.md.',
    'See ./work/plans/x.md',
  ]
  positives.forEach((sample, index) => {
    if (detect(sample).length !== 0) failures.push(`positive ${index} was flagged: ${sample}`)
  })
  negatives.forEach((sample, index) => {
    if (detect(sample).length === 0) failures.push(`negative ${index} was not flagged: ${sample}`)
  })
  try {
    check([])
    failures.push('empty scan set did not raise a config error')
  } catch (error) {
    if (!(error instanceof ConfigError)) failures.push(`empty scan set raised the wrong error: ${error}`)
  }
  if (!EXEMPT.has('scripts/doc-ref-gate.mjs')) failures.push('self exemption is not registered')
  if (failures.length > 0) {
    for (const failure of failures) console.error(`doc-ref-gate self-test FAIL: ${failure}`)
    return 1
  }
  console.log(
    `doc-ref-gate self-test: ${positives.length} positive, ${negatives.length} negative, 1 config guard`,
  )
  return 0
}

function main() {
  if (process.argv.includes('--self-test')) return selfTest()
  const files = walk(UI_ROOT)
  const findings = check(files)
  if (findings.length > 0) {
    for (const finding of findings) console.error(`doc-ref-gate: ${finding}`)
    console.error(`doc-ref-gate: FAILED with ${findings.length} finding(s)`)
    return 1
  }
  console.log(`doc-ref-gate: ${files.length} committed text files, 0 work/ references`)
  console.log(`doc-ref-gate: exempt ${[...EXEMPT].join(', ')} — the gate documents and tests the token`)
  return 0
}

try {
  process.exit(main())
} catch (error) {
  if (error instanceof ConfigError) {
    console.error(`doc-ref-gate: CONFIG ERROR: ${error.message}`)
    process.exit(2)
  }
  throw error
}
