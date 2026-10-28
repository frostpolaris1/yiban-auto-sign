#!/usr/bin/env node
/**
 * license-gate.mjs — 生成并校验 ui 的第三方依赖许可证清单。
 *
 * 口径：`package-lock.json` 的全部包，含 devDependencies 与各平台可选包。
 * 许可证取自锁文件的 `license` 字段。
 * 平台可选包一律保留，清单与运行平台无关。
 * 根 package.json 的**直接依赖必须全部出现在扫描结果里**；缺失即扫描失败。
 * 清单写入 THIRD_PARTY_LICENSES.md，随发布件分发。
 * Adminator 的通知不在此文件，见 THIRD_PARTY_NOTICES.md。
 *
 * 退出码：
 *   0  通过
 *   1  许可证不在允许清单内，或清单与扫描结果不一致
 *   2  扫描失败或扫描为空（门没有真正运行）；含不可解析的锁文件
 *
 * 用法：
 *   node scripts/license-gate.mjs            校验
 *   node scripts/license-gate.mjs --write    重新生成清单
 *   node scripts/license-gate.mjs --self-test 验证门本身能变红
 */

import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const UI_ROOT = resolve(HERE, '..')
const MANIFEST = join(UI_ROOT, 'THIRD_PARTY_LICENSES.md')
const LOCK = join(UI_ROOT, 'package-lock.json')

/**
 * 允许清单的许可证。前八项与 server 门一致。
 * MPL-2.0（lightningcss）与 Unlicense（isbot）按实际 scope 登记：
 * 措辞由扫描结果生成，不写死。
 */
const ALLOWED = [
  'Apache-2.0',
  'BSD-2-Clause',
  'BSD-3-Clause',
  'ISC',
  'MIT',
  '0BSD',
  'CC0-1.0',
  'BlueOak-1.0.0',
  'MPL-2.0',
  'Unlicense',
]

/** 允许清单之外的例外项。运行时要提示。 */
const EXCEPTIONS = new Set(['MPL-2.0', 'Unlicense'])

/** 例外许可证的固定理由。scope 由扫描结果决定。 */
const EXCEPTION_NOTES = {
  'MPL-2.0': '文件级弱著佐权，不限制本项目代码的许可证',
  Unlicense: '公有领域，无传染问题',
}

class ScanError extends Error {}

/** licenseOf 读取锁条目的许可证字段。缺字段返回 UNKNOWN。 */
function licenseOf(pkg) {
  if (typeof pkg.license === 'string' && pkg.license.trim() !== '') {
    return pkg.license.trim()
  }
  if (pkg.license && typeof pkg.license.type === 'string') {
    return pkg.license.type.trim()
  }
  if (Array.isArray(pkg.licenses)) {
    const names = pkg.licenses
      .map((item) => (typeof item === 'string' ? item : item && item.type))
      .filter((name) => typeof name === 'string' && name.trim() !== '')
      .map((name) => name.trim())
    if (names.length > 0) {
      return names.join(' OR ')
    }
  }
  return 'UNKNOWN'
}

/** problemsFor 返回该许可证的违规说明。空数组表示通过。 */
function problemsFor(license) {
  if (license === '' || license === 'UNKNOWN' || license === 'NOASSERTION') {
    return [`no detected license (${license})`]
  }
  const problems = []
  // SPDX 表达式：逗号、OR、AND 都拆开逐项判定。只要有一项不允许就失败。
  for (const part of license.split(/,|\s+OR\s+|\s+AND\s+/)) {
    const name = part.trim()
    if (name === '') {
      continue
    }
    if (!ALLOWED.includes(name)) {
      problems.push(`license '${name}' is not allowed`)
    }
  }
  return problems
}

function comparePackages(a, b) {
  if (a.name !== b.name) {
    return a.name < b.name ? -1 : 1
  }
  return a.version < b.version ? -1 : a.version > b.version ? 1 : 0
}

/**
 * packagesFromLock 把锁文件转成包清单。
 * 平台可选包（os/cpu 不匹配本机）照常保留：清单不随平台漂移。
 */
function packagesFromLock(lock) {
  const packages = new Map()
  for (const [key, entry] of Object.entries(lock.packages ?? {})) {
    if (key === '') {
      continue
    }
    const marker = 'node_modules/'
    const at = key.lastIndexOf(marker)
    if (at === -1) {
      continue
    }
    const name = key.slice(at + marker.length)
    if (name === '') {
      continue
    }
    const version = typeof entry.version === 'string' ? entry.version : ''
    const id = `${name}@${version}`
    if (packages.has(id)) {
      continue
    }
    packages.set(id, {
      name,
      version,
      license: licenseOf(entry),
      scope: entry.dev === true ? '开发' : '运行时',
    })
  }
  return [...packages.values()].sort(comparePackages)
}

/** directDependencyNames 返回根 package.json 的直接依赖名（运行时与开发）。 */
function directDependencyNames() {
  const pkg = JSON.parse(readFileSync(join(UI_ROOT, 'package.json'), 'utf8'))
  return [
    ...Object.keys(pkg.dependencies ?? {}),
    ...Object.keys(pkg.devDependencies ?? {}),
  ]
}

/** missingDirectDependencies 返回扫描结果里缺失的直接依赖名。 */
function missingDirectDependencies(names, packages) {
  const seen = new Set(packages.map((pkg) => pkg.name))
  return names.filter((name) => !seen.has(name))
}

/** collect 返回按 名称、版本 排序的锁文件包清单。 */
function collect() {
  let lock
  try {
    lock = JSON.parse(readFileSync(LOCK, 'utf8'))
  } catch (error) {
    throw new ScanError(`cannot read ${LOCK}: ${error.message}`)
  }
  const packages = packagesFromLock(lock)
  if (packages.length === 0) {
    throw new ScanError('package-lock.json has no packages; the gate did not run')
  }

  const missing = missingDirectDependencies(directDependencyNames(), packages)
  if (missing.length > 0) {
    throw new ScanError(`the scan missed direct dependencies: ${missing.join(', ')}`)
  }

  return packages
}

/** exceptionLines 按扫描结果生成例外许可证说明。scope 与包名取自实据。 */
function exceptionLines(packages) {
  const lines = []
  for (const license of EXCEPTIONS) {
    const hits = packages.filter((pkg) => pkg.license === license)
    if (hits.length === 0) {
      continue
    }
    const names = [...new Set(hits.map((pkg) => pkg.name))].sort().join('、')
    const scope = hits.some((pkg) => pkg.scope === '运行时') ? '运行时' : '构建期'
    lines.push(`${license}（${names}）只出现在${scope}依赖；${EXCEPTION_NOTES[license]}。`)
  }
  return lines
}

function render(packages) {
  const runtime = packages.filter((pkg) => pkg.scope === '运行时').length
  const dev = packages.length - runtime
  const lines = []
  lines.push('# 第三方依赖许可证清单（ui）')
  lines.push('')
  lines.push('> 本文件由 `npm run gate:license -- --write` 生成，禁止手改。')
  lines.push('> 允许清单与禁用清单由 `scripts/license-gate.mjs` 执行，本文件只作留档。')
  lines.push('> 口径：`package-lock.json` 的全部包，含 devDependencies 与各平台可选包。')
  lines.push('> 许可证取自锁文件的 license 字段。')
  lines.push('> Adminator 的通知单列在 `THIRD_PARTY_NOTICES.md`。')
  lines.push('')
  lines.push(
    '允许的许可证：Apache-2.0、BSD-2-Clause、BSD-3-Clause、ISC、MIT、0BSD、CC0-1.0、BlueOak-1.0.0、MPL-2.0、Unlicense。',
  )
  for (const line of exceptionLines(packages)) {
    lines.push(line)
  }
  lines.push('其余许可证一律使门失败。GPL 系、AGPL 系、未知与无许可证同样失败。')
  lines.push('门还校验根 package.json 的全部直接依赖都出现在扫描结果里。')
  lines.push('')
  lines.push('| 包 | 版本 | 许可证 | 依赖类型 |')
  lines.push('|---|---|---|---|')
  for (const pkg of packages) {
    lines.push(`| ${pkg.name} | ${pkg.version} | ${pkg.license} | ${pkg.scope} |`)
  }
  lines.push('')
  lines.push(`共 ${packages.length} 个包（运行时 ${runtime}，开发 ${dev}）。`)
  lines.push('')
  return lines.join('\n')
}

function normalize(text) {
  return text.replace(/\r\n/g, '\n')
}

function selfTest() {
  const cases = [
    [{ license: 'MIT' }, true],
    [{ license: 'Apache-2.0' }, true],
    [{ license: { type: 'BSD-3-Clause' } }, true],
    [{ licenses: [{ type: 'MIT' }, { type: 'Apache-2.0' }] }, true],
    [{ license: 'GPL-3.0-only' }, false],
    [{ license: 'AGPL-3.0-or-later' }, false],
    [{}, false],
    [{ license: 'UNKNOWN' }, false],
  ]
  let failed = 0
  const fail = (message) => {
    console.error(`license-gate self-test: ${message}`)
    failed += 1
  }
  for (const [pkg, want] of cases) {
    const got = problemsFor(licenseOf(pkg)).length === 0
    if (got !== want) {
      fail(`${JSON.stringify(pkg)} -> ${got}, want ${want}`)
    }
  }
  const sample = [
    { name: 'a', version: '1.0.0', license: 'MIT', scope: '运行时' },
    { name: 'b', version: '2.0.0', license: 'Apache-2.0', scope: '开发' },
  ]
  const base = render(sample)
  const tampered = base.replace('| a | 1.0.0 | MIT |', '| a | 1.0.0 | GPL-3.0-only |')
  if (normalize(base) === normalize(tampered)) {
    fail('drift comparison cannot see a changed row')
  }
  // 直接依赖覆盖：缺失的直接依赖必须被报出来。
  const covered = [{ name: 'a' }, { name: 'b' }]
  if (missingDirectDependencies(['a', 'b'], covered).length !== 0) {
    fail('coverage check reports a false miss')
  }
  if (missingDirectDependencies(['a', 'c'], covered).join(',') !== 'c') {
    fail('coverage check cannot see a missing direct dependency')
  }
  // 例外说明按实际 scope 生成：运行时依赖不得写成构建期。
  const notes = exceptionLines([
    { name: 'lightningcss', license: 'MPL-2.0', scope: '开发' },
    { name: 'isbot', license: 'Unlicense', scope: '运行时' },
  ]).join('\n')
  if (!notes.includes('MPL-2.0（lightningcss）只出现在构建期依赖') ||
      !notes.includes('Unlicense（isbot）只出现在运行时依赖')) {
    fail('exception wording does not follow the scan scope')
  }
  // 锁文件口径：另一平台的包必须被确定性含入，不许按本机 os/cpu 过滤。
  const platformLock = {
    packages: {
      '': { name: 'fixture', version: '1.0.0' },
      'node_modules/alpha': { version: '1.0.0', license: 'MIT' },
      'node_modules/beta': { version: '2.0.0', license: 'MIT', dev: true },
      'node_modules/binding-linux-x64': {
        version: '3.0.0',
        license: 'MIT',
        os: ['linux'],
        cpu: ['x64'],
      },
      'node_modules/binding-win32-x64': {
        version: '3.0.0',
        license: 'MIT',
        os: ['win32'],
        cpu: ['x64'],
      },
    },
  }
  const fromLock = packagesFromLock(platformLock)
  const names = fromLock.map((pkg) => pkg.name)
  const wantNames = ['alpha', 'beta', 'binding-linux-x64', 'binding-win32-x64']
  if (JSON.stringify(names) !== JSON.stringify(wantNames)) {
    fail(`lock scan must include every platform: got ${JSON.stringify(names)}`)
  }
  if (fromLock.find((pkg) => pkg.name === 'beta')?.scope !== '开发') {
    fail('lock scan must mark dev entries')
  }
  const reversed = {
    packages: Object.fromEntries(Object.entries(platformLock.packages).reverse()),
  }
  if (JSON.stringify(packagesFromLock(reversed)) !== JSON.stringify(fromLock)) {
    fail('lock scan must be deterministic across key order')
  }
  const onlyForeign = packagesFromLock({
    packages: { 'node_modules/only-darwin': { version: '1.0.0', license: 'MIT', os: ['darwin'] } },
  })
  if (onlyForeign.length !== 1) {
    fail('a package for another platform must not be dropped')
  }
  const missingLicense = packagesFromLock({
    packages: { 'node_modules/gamma': { version: '1.0.0' } },
  })
  if (problemsFor(missingLicense[0].license).length === 0) {
    fail('a lock entry without a license must fail')
  }
  if (failed !== 0) {
    console.error(`license-gate self-test: FAIL (${failed})`)
    return 1
  }
  console.log(
    `license-gate self-test: ok (${cases.length} license cases + lock/platform cases)`,
  )
  return 0
}

function main() {
  const args = process.argv.slice(2)
  if (args.includes('--self-test')) {
    return selfTest()
  }
  const write = args.includes('--write')

  let packages
  try {
    packages = collect()
  } catch (error) {
    if (error instanceof ScanError) {
      console.error(`license-gate: ${error.message}`)
      return 2
    }
    console.error(`license-gate: ${error.message}`)
    return 2
  }

  let violations = 0
  for (const pkg of packages) {
    const problems = problemsFor(pkg.license)
    for (const problem of problems) {
      console.error(`license-gate: ${pkg.name}@${pkg.version} ${problem}`)
      violations += 1
    }
    if (!problems.length && EXCEPTIONS.has(pkg.license)) {
      console.log(`license-gate: registered exception ${pkg.license} for ${pkg.name}@${pkg.version}`)
    }
  }
  if (violations !== 0) {
    console.error(`license-gate: FAIL: ${violations} license problem(s)`)
    return 1
  }

  const expected = render(packages)
  if (write) {
    writeFileSync(MANIFEST, expected, 'utf8')
    console.log(`license-gate: wrote ${MANIFEST}`)
    return 0
  }

  let actual
  try {
    actual = readFileSync(MANIFEST, 'utf8')
  } catch {
    console.error('license-gate: missing THIRD_PARTY_LICENSES.md; run --write')
    return 1
  }
  if (normalize(actual) !== normalize(expected)) {
    console.error('license-gate: THIRD_PARTY_LICENSES.md is stale; run --write')
    return 1
  }

  console.log(`license-gate: ok (${packages.length} packages)`)
  return 0
}

try {
  process.exit(main())
} catch (error) {
  console.error(`license-gate: CONFIG ERROR: ${error.message}`)
  process.exit(2)
}
