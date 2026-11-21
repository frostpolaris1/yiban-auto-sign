/**
 * `/system` 系统名册与版本自检的纯逻辑单元测试。
 *
 * Node 24 经 `resolve-ts.mjs` 解析 `@/` 别名后执行。
 * 判据：来源三态文案如实；敏感视图绝不消费 `value`；读取错误只报类别。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { ApiError } from '../../src/lib/api/client.ts'
import {
  ROSTER_SOURCE_LABELS,
  SENSITIVE_MASK,
  formatRosterReadError,
  formatSettingValue,
  rosterSourceLabel,
  rosterSourceTone,
  sensitiveSettingView,
} from '../../src/features/system/roster/drafts.ts'

test('formatSettingValue renders each wire type as-is', () => {
  assert.equal(formatSettingValue(null), '未设置')
  assert.equal(formatSettingValue(undefined), '未设置')
  assert.equal(formatSettingValue(true), 'true')
  assert.equal(formatSettingValue(false), 'false')
  assert.equal(formatSettingValue(60), '60')
  assert.equal(formatSettingValue(':8080'), ':8080')
  assert.equal(formatSettingValue(''), '空字符串')
})

test('sensitiveSettingView only consumes the sensitive object, never value', () => {
  const setItem = {
    key: 'YIBAN_MAIL_PASS',
    group: '通知',
    source: 'env',
    // 畸形出包：敏感键带了原值。视图必须无视它。
    value: 'plain-secret-should-never-show',
    sensitive: { set: true, masked: '****', source: 'env' },
  }
  const view = sensitiveSettingView(setItem)
  assert.equal(view.stateLabel, '已设置')
  assert.equal(view.masked, SENSITIVE_MASK)
  assert.equal(view.source, 'env')
  const rendered = JSON.stringify(view)
  assert.ok(!rendered.includes('plain-secret-should-never-show'))

  const unsetItem = {
    key: 'YIBAN_WEBHOOK_SECRET',
    group: '通知',
    source: 'unknown',
    value: null,
    sensitive: { set: false, masked: '', source: 'unknown' },
  }
  const unsetView = sensitiveSettingView(unsetItem)
  assert.equal(unsetView.stateLabel, '未设置')
  assert.equal(unsetView.masked, '')
})

test('rosterSourceLabel covers the contract enum and falls back to the raw value', () => {
  assert.deepEqual(ROSTER_SOURCE_LABELS, {
    env: '环境变量',
    file: '配置文件',
    db: '数据库覆盖',
    default: '默认',
    unknown: '未知',
  })
  assert.equal(rosterSourceLabel('db'), '数据库覆盖')
  assert.equal(rosterSourceLabel('file'), '配置文件')
  // 兜底：超出枚举的来源原样回显，不吞错。
  assert.equal(rosterSourceLabel('future'), 'future')
})

test('rosterSourceTone distinguishes db and env from the rest', () => {
  assert.equal(rosterSourceTone('db'), 'info')
  assert.equal(rosterSourceTone('env'), 'warning')
  assert.equal(rosterSourceTone('default'), undefined)
  assert.equal(rosterSourceTone('unknown'), undefined)
})

test('formatRosterReadError names the failure class without echoing the wire message', () => {
  const serverError = new ApiError(500, { code: 'internal_error', message: 'db exploded' })
  assert.equal(formatRosterReadError(serverError), '名册读取失败，请稍后重试')
  const expired = new ApiError(401, { code: 'unauthorized', message: 'session expired' })
  assert.match(formatRosterReadError(expired), /登录已过期/)
  const forbidden = new ApiError(403, { code: 'forbidden', message: 'nope' })
  assert.match(formatRosterReadError(forbidden), /权限不足/)
  assert.match(formatRosterReadError(new Error('boom')), /网络异常/)
  // 底层报文不回显：管理员界面不出现服务端错误原文。
  assert.ok(!formatRosterReadError(serverError).includes('db exploded'))
})
