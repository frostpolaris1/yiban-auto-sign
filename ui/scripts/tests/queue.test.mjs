import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { TASK_STATE_LABELS } from '../../src/lib/identity.ts'
import { showcaseQueueAccounts, showcaseQueueTasks } from '../../src/lib/showcase.ts'
import {
  accountStatusMeta,
  applyRejectPreset,
  describeTaskResult,
  diagnosticsSuggestionMeta,
  formatBusinessDayLabel,
  formatPagedSummary,
  formatWindowSummary,
  schoolNameMap,
  taskResultGuideCodes,
  taskStateMeta,
} from '../../src/features/queue/queueDisplay.ts'

test('queueDisplay uses shared identity vocabulary for task states', () => {
  const source = readFileSync(
    new URL('../../src/features/queue/queueDisplay.ts', import.meta.url),
    'utf8',
  )
  for (const word of Object.values(TASK_STATE_LABELS)) {
    assert.ok(
      !source.includes(`'${word}'`),
      `${word} must come from lib/identity rather than being restated in queueDisplay.ts`,
    )
  }
  assert.equal(taskStateMeta('failed').label, '失败')
  assert.equal(taskStateMeta('pending').label, '待签')
  assert.equal(taskStateMeta('running').label, '进行中')
})

test('formatBusinessDayLabel formats Shanghai weekday and handles invalid input', () => {
  const valid = formatBusinessDayLabel('2026-10-31')
  assert.equal(valid.date, '2026-10-31')
  assert.match(valid.weekday, /周六/)

  const invalid = formatBusinessDayLabel('not-a-day')
  assert.equal(invalid.date, 'not-a-day')
  assert.equal(invalid.weekday, '')
})

test('formatWindowSummary handles empty, weekend_closed, single and multiple windows', () => {
  assert.equal(formatWindowSummary([]).label, '未配置')

  const weekend = formatWindowSummary([
    {
      school_id: 's1',
      school_name: '甲',
      task_id: 't1',
      task_name: '早签',
      enabled: true,
      day: '2026-10-31',
      start: null,
      end: null,
      source: 'weekend_closed',
      version: 1,
    },
  ])
  assert.equal(weekend.label, '今日休窗')
  assert.equal(weekend.closed, true)

  const single = formatWindowSummary([
    {
      school_id: 's1',
      school_name: '合成学校甲',
      task_id: 't1',
      task_name: '早签到',
      enabled: true,
      day: '2026-10-31',
      start: '2026-10-31T08:30:00+08:00',
      end: '2026-10-31T09:00:00+08:00',
      source: 'cache',
      version: 1,
    },
  ])
  assert.equal(single.label, '08:30 — 09:00')
  assert.equal(single.detail, '合成学校甲 · 早签到')

  const multi = formatWindowSummary([
    {
      school_id: 's1',
      school_name: '甲',
      task_id: 't1',
      task_name: '早',
      enabled: true,
      day: '2026-10-31',
      start: '2026-10-31T08:30:00+08:00',
      end: '2026-10-31T09:00:00+08:00',
      source: 'cache',
      version: 1,
    },
    {
      school_id: 's2',
      school_name: '乙',
      task_id: 't2',
      task_name: '晚',
      enabled: true,
      day: '2026-10-31',
      start: '2026-10-31T20:00:00+08:00',
      end: '2026-10-31T21:00:00+08:00',
      source: 'platform',
      version: 1,
    },
  ])
  assert.equal(multi.label, '08:30 — 21:00')
  assert.match(multi.detail, /2 个/)
})

test('accountStatusMeta, diagnosticsSuggestionMeta and schoolNameMap return consistent labels', () => {
  assert.equal(accountStatusMeta('pending').label, '待审核')
  assert.equal(accountStatusMeta('rejected').label, '已驳回')
  assert.equal(diagnosticsSuggestionMeta('retry_now').label, '可以立即重试')
  const map = schoolNameMap([
    {
      id: 'school-a',
      code: 'demo-a',
      name: '合成学校甲',
      status: 'active',
      timezone: 'Asia/Shanghai',
      created_at: '2026-10-01T00:00:00Z',
      updated_at: '2026-10-01T00:00:00Z',
    },
  ])
  assert.equal(map.get('school-a'), '合成学校甲')
})

test('describeTaskResult only names codes the failed path really produces', () => {
  const credential = describeTaskResult('credential_invalid')
  assert.equal(credential.label, '凭据无效')
  assert.match(credential.hint, /凭据|密码/)

  // 三个终态在真实失败组里都出现过，绝不能落到通用兜底
  for (const code of ['rejected', 'photo_rejected', 'blocked']) {
    assert.notEqual(describeTaskResult(code).label, '平台返回异常', `${code} 缺中文结论`)
  }
  assert.equal(describeTaskResult('photo_rejected').label, '照片未通过')

  assert.equal(describeTaskResult('  ').label, '执行失败')
  assert.equal(describeTaskResult('platform_captcha_required').label, '平台返回异常')
})

test('the result guide is a subset of the server outcome vocabulary', () => {
  const contract = readFileSync(
    new URL('../../../server/internal/signing/contract/contract.go', import.meta.url),
    'utf8',
  )
  const vocabulary = new Set(
    [...contract.matchAll(/Outcome\w*\s+Outcome\s*=\s*"([a-z_]+)"/g)].map((m) => m[1]),
  )
  assert.ok(vocabulary.size >= 10, '契约词表解析为空，门没有真正运行')

  const codes = taskResultGuideCodes()
  assert.ok(codes.length > 0)
  for (const code of codes) {
    assert.ok(vocabulary.has(code), `${code} 不在契约 Outcome 词表内`)
  }
  // worker 判 failed 的七个码必须在册，漏一个就会掉进「平台返回异常」
  for (const code of [
    'credential_invalid',
    'rejected',
    'photo_rejected',
    'blocked',
    'transient',
    'pending_upstream',
    'unknown',
  ]) {
    assert.ok(codes.includes(code), `失败路径的 ${code} 未登记中文结论`)
  }
})

test('formatPagedSummary never pretends the page size is the total', () => {
  assert.equal(formatPagedSummary(0, 2, false), '第 1 页 · 本页 2 条')
  assert.equal(formatPagedSummary(1, 10, true), '第 2 页 · 本页 10 条（还有更多）')
})

test('applyRejectPreset appends to hand-written text instead of wiping it', () => {
  assert.equal(applyRejectPreset('', '重复提交账号'), '重复提交账号')
  assert.equal(applyRejectPreset('重复提交账号', '重复提交账号'), '')
  assert.equal(
    applyRejectPreset('账号归属人联系不上', '手机号码有误'),
    '账号归属人联系不上；手机号码有误',
  )
  assert.equal(applyRejectPreset('手机号码有误；另外照片模糊', '手机号码有误'), '另外照片模糊')
  const long = '详情'.repeat(99)
  assert.equal(applyRejectPreset(long, '重复提交账号'), long)
})

test('showcase queue fixtures honour school and keyword filters', () => {
  const bySchool = showcaseQueueTasks('failed', { schoolId: 'school-a' })
  assert.ok(bySchool.items.length > 0)
  assert.ok(bySchool.items.every((item) => item.school_id === 'school-a'))

  const byPhone = showcaseQueueTasks('failed', { q: '138****8003' })
  assert.equal(byPhone.items.length, 1)
  assert.equal(byPhone.items[0].school_id, 'school-a')

  assert.equal(showcaseQueueTasks('failed', { q: '不存在的关键字' }).items.length, 0)
  assert.ok(showcaseQueueAccounts('pending', { schoolId: 'school-a' }).items.length > 0)
  assert.equal(showcaseQueueAccounts('pending', { schoolId: 'school-b' }).items.length, 0)
})
