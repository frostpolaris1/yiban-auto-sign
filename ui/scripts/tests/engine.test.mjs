import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { leaseRemainingText } from '../../src/features/run/engineDisplay.ts'

/**
 * 执行监控的不变式（UX-5）。
 *
 * 一、租约剩余是纯函数：负数不外泄，到期即「已到期」；
 *     分秒文本与 390 宽的表格列共存。
 * 二、监控词汇：页面源码不得出现「队列」字样——
 *     新栈没有队列实体，重建队列等于造假（aqn 拍板 A）。
 */

const NOW = Date.parse('2026-10-31T08:00:00+08:00')

test('lease remaining counts down in minutes and seconds', () => {
  assert.equal(leaseRemainingText(NOW + 65_000, NOW), '1 分 5 秒')
  assert.equal(leaseRemainingText(NOW + 45_000, NOW), '45 秒')
  assert.equal(leaseRemainingText(NOW + 90 * 60_000, NOW), '1 时 30 分')
})

test('lease remaining never leaks a negative value', () => {
  assert.equal(leaseRemainingText(NOW, NOW), '已到期')
  assert.equal(leaseRemainingText(NOW - 1, NOW), '已到期')
  assert.equal(leaseRemainingText(NOW - 10 * 60_000, NOW), '已到期')
})

test('engine sources never call it a queue', () => {
  const sources = [
    '../../src/features/run/EngineView.tsx',
    '../../src/features/run/engineDisplay.ts',
    '../../src/pages/EnginePage.tsx',
  ]
  for (const path of sources) {
    const source = readFileSync(new URL(path, import.meta.url), 'utf8')
    assert.ok(!source.includes('队列'), `${path} must not use the word 队列`)
  }
})
