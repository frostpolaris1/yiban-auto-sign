/**
 * **功能**
 * 测试计划守卫：`test:behavior` 的覆盖必须等于 Playwright 会发现的行为 spec 全集。
 *
 * **归属**
 * `scripts/tests`：纯 Node 单测，读 `package.json` 与 `e2e/` 目录。
 *
 * **口径**
 * 发现规则与 Playwright 默认 `testMatch` 同步：递归、`*.spec|test.(c|m)?[jt]s(x)`。
 * 新增行为 spec 却忘了加进 `test:behavior` 时红；目录扫空也红（扫空不得当绿）。
 * `test:shots` 只跑视觉 spec，并显式钉住 1 并发。
 * `test:e2e` 必须仍是整包入口（`playwright test`），否则新 spec 会漏跑。
 */

import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const here = dirname(fileURLToPath(import.meta.url))
const uiRoot = join(here, '..', '..')
const pkg = JSON.parse(readFileSync(join(uiRoot, 'package.json'), 'utf8'))

/** Playwright 默认 testMatch 的扩展名片段；发现与参数解析共用。 */
const SPEC_EXT = '(?:[cm])?[jt]sx?'
const SPEC_FILE = new RegExp(String.raw`\.(?:spec|test)\.${SPEC_EXT}$`)

/** 递归列出 e2e/ 下所有 spec，路径统一成 `e2e/...` 形式。 */
function discoverSpecs(dir) {
  const found = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) {
      found.push(...discoverSpecs(full))
    } else if (SPEC_FILE.test(entry.name)) {
      found.push(relative(uiRoot, full).split(sep).join('/'))
    }
  }
  return found.sort()
}

/** 从脚本串里取出 spec 参数，支持子目录与 .tsx。 */
function specArgs(script) {
  const pattern = new RegExp(String.raw`e2e/[\w./-]+\.(?:spec|test)\.${SPEC_EXT}`, 'g')
  return (script.match(pattern) ?? []).sort()
}

function script(name) {
  const value = pkg.scripts[name]
  assert.equal(typeof value, 'string', `package.json 缺脚本 ${name}`)
  return value
}

test('test:behavior covers every spec except the visual one', () => {
  const allSpecs = discoverSpecs(join(uiRoot, 'e2e'))
  assert.ok(allSpecs.length > 0, 'e2e/ 下没有发现任何 spec：扫空不得当绿')
  const expected = allSpecs.filter((name) => name !== 'e2e/visual.spec.ts')
  assert.ok(expected.length > 0, '除视觉外没有行为 spec：扫空不得当绿')
  assert.deepEqual(specArgs(script('test:behavior')), expected)
})

test('test:shots owns exactly the visual spec at one worker', () => {
  assert.deepEqual(specArgs(script('test:shots')), ['e2e/visual.spec.ts'])
  assert.deepEqual(specArgs(script('test:shots:update')), ['e2e/visual.spec.ts'])
  assert.match(script('test:shots'), /--workers=1$/)
  assert.match(script('test:shots:update'), /--workers=1 /)
  assert.match(script('test:shots:update'), /--update-snapshots/)
})

test('test:e2e stays the whole-suite entry point', () => {
  assert.equal(script('test:e2e'), 'playwright test')
  assert.equal(script('test:e2e:fast'), 'npm run test:behavior && npm run test:shots')
  assert.equal(script('test:visual'), 'playwright test')
})

test('the fast path runs behaviour in parallel and shots serially', () => {
  assert.match(script('test:behavior'), /--workers=4$/)
  assert.doesNotMatch(script('test:behavior'), /e2e\/visual\.spec\.ts/)
})
