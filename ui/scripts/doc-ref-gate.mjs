#!/usr/bin/env node
/**
 * doc-ref-gate.mjs — 阻止入库 UI 文本指向未入库的 work/ 路径。
 *
 * `work/` 存本地计划、交接与审查产物。它不入库。
 * 入库文件一旦指向 `work/`，克隆后就指向不存在的路径。
 * 承重结论必须写在入库文件本身（AGENTS 第 24 条）。
 *
 * 扫描集：ui/ 下按扩展名筛选的文本文件。
 * 排除 node_modules、dist、test-results、playwright-report 与 lock 文件。
 *
 * 退出码：
 *   0  无发现
 *   1  引用了 work/ 路径
 *   2  配置错误（扫描集为空）
 *
 * 用法：
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
/** 本门要记录并自测这个禁用词，故把自己排除在扫描外。 */
const EXEMPT = new Set(['scripts/doc-ref-gate.mjs'])
// 匹配本地 work/ 引用：两种分隔符、任意大小写。
// 先去掉 URL，避免把链接路径误判成本地路径。
// 扫描范围只有 ui/。范围登记在 STRUCTURE.md。
// docs/ 与 server/ 由 Python 侧文档门与审查覆盖。
const URL = /[a-z][a-z0-9+.-]*:\/\/\S+/gi
const RULE = /(?<![\w])work[\\/][\w.\\/-]*/gi

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
  const cleaned = text.replace(URL, (url) => ' '.repeat(url.length))
  RULE.lastIndex = 0
  let match
  while ((match = RULE.exec(cleaned)) !== null) {
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
    'Windows path work\\handoff\\x.md',
    'Upper case Work/plans/x.md',
  ]
  const urls = ['See https://example.com/work/x for details.']
  urls.forEach((sample, index) => {
    if (detect(sample).length !== 0) failures.push(`url ${index} was flagged: ${sample}`)
  })
  if (detect('see ftp://host/work/x').length !== 0) {
    failures.push('non-http scheme was flagged as a local path')
  }
  const withSeparator = detect('See ./work/plans/x.md')
  if (withSeparator.length !== 1 || withSeparator[0].value !== 'work/plans/x.md') {
    failures.push('finding keeps a leading separator or misses the path')
  }
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
