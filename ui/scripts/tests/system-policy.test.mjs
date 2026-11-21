/**
 * `/system` 运行策略、全站公告与渠道自检的纯逻辑单元测试。
 *
 * Node 24 经 `resolve-ts.mjs` 解析 `@/` 别名后执行。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { ApiError } from '../../src/lib/api/client.ts'
import {
  ANNOUNCEMENT_MAX_CHARS,
  countAnnouncementChars,
  formatAnnouncementError,
  validateAnnouncementDraft,
} from '../../src/features/system/announcement/drafts.ts'
import {
  extractRetryAfterSeconds,
  formatMailReauthModalError,
  formatMailTestResultError,
  formatWebhookTestError,
  shouldKeepMailTestModalOpen,
} from '../../src/features/system/channels/drafts.ts'
import {
  buildQuotaResetPatch,
  buildQuotaSettingsPatch,
  buildScheduleResetPatch,
  buildScheduleSettingsPatch,
  createQuotaPolicyDraft,
  createSchedulePolicyDraft,
  formatSystemWriteError,
  minutesToClockInput,
  parseClockInputToMinutes,
  validateQuotaPolicyDraft,
  validateSchedulePolicyDraft,
} from '../../src/features/system/policy/drafts.ts'

const SAMPLE_SCHEDULE_REPORT = {
  edge_front_sec: { value: 60, source: 'default' },
  edge_back_sec: { value: 60, source: 'default' },
  allow_time_pref: { value: false, source: 'default' },
  sunday_sign: { value: false, source: 'default' },
  saturday_sign: { value: false, source: 'default' },
  default_sign_start_min: { value: 390, source: 'default' },
  default_sign_end_min: { value: 470, source: 'default' },
  sign_dist: { value: 'front', source: 'default' },
}

const SAMPLE_QUOTA_REPORT = {
  max_users: { value: 1, source: 'default' },
  max_accounts: { value: 1, source: 'default' },
}

test('clock helpers convert minutes [0,1439] and HH:MM strictly', () => {
  assert.equal(minutesToClockInput(390), '06:30')
  assert.equal(minutesToClockInput(470), '07:50')
  assert.equal(minutesToClockInput(0), '00:00')
  assert.equal(minutesToClockInput(1439), '23:59')
  assert.equal(parseClockInputToMinutes('06:30'), 390)
  assert.equal(parseClockInputToMinutes('07:50'), 470)
  assert.equal(parseClockInputToMinutes('25:00'), null)
  assert.equal(parseClockInputToMinutes('6:30'), null)
  assert.equal(parseClockInputToMinutes('invalid'), null)
})

test('schedule policy validation accepts [0,300] and strict start < end', () => {
  const base = createSchedulePolicyDraft(SAMPLE_SCHEDULE_REPORT)
  assert.deepEqual(validateSchedulePolicyDraft(base), {})

  assert.deepEqual(
    validateSchedulePolicyDraft({ ...base, edgeFrontSec: '0', edgeBackSec: '300' }),
    {},
  )

  const outOfRange = validateSchedulePolicyDraft({
    ...base,
    edgeFrontSec: '301',
    edgeBackSec: '-1',
  })
  assert.ok(outOfRange.edgeFrontSec)
  assert.ok(outOfRange.edgeBackSec)

  const equalWindow = validateSchedulePolicyDraft({
    ...base,
    defaultSignStartClock: '07:50',
    defaultSignEndClock: '07:50',
  })
  assert.ok(equalWindow.defaultSignEndClock)

  const invertedWindow = validateSchedulePolicyDraft({
    ...base,
    defaultSignStartClock: '08:15',
    defaultSignEndClock: '07:50',
  })
  assert.ok(invertedWindow.defaultSignEndClock)
})

test('buildScheduleSettingsPatch emits minimal changes and couples default window pair', () => {
  const initialDraft = createSchedulePolicyDraft(SAMPLE_SCHEDULE_REPORT)
  assert.equal(buildScheduleSettingsPatch(initialDraft, SAMPLE_SCHEDULE_REPORT), null)

  // 修改 start 时必须同时携带 start 与 end，保持成对一致。
  const windowPatch = buildScheduleSettingsPatch(
    { ...initialDraft, defaultSignStartClock: '07:00', signDist: 'uniform' },
    SAMPLE_SCHEDULE_REPORT,
  )
  assert.deepEqual(windowPatch, {
    default_sign_start_min: 420,
    default_sign_end_min: 470,
    sign_dist: 'uniform',
  })

  assert.deepEqual(buildScheduleResetPatch(), {
    edge_front_sec: null,
    edge_back_sec: null,
    allow_time_pref: null,
    sunday_sign: null,
    saturday_sign: null,
    default_sign_start_min: null,
    default_sign_end_min: null,
    sign_dist: null,
  })
})

test('quota validation enforces [0,100000] integers and patch drops unchanged keys', () => {
  const draft = createQuotaPolicyDraft(SAMPLE_QUOTA_REPORT)
  assert.deepEqual(validateQuotaPolicyDraft(draft), {})
  assert.equal(buildQuotaSettingsPatch(draft, SAMPLE_QUOTA_REPORT), null)

  assert.deepEqual(validateQuotaPolicyDraft({ maxUsers: '0', maxAccounts: '100000' }), {})
  const invalid = validateQuotaPolicyDraft({ maxUsers: '-1', maxAccounts: '100001' })
  assert.ok(invalid.maxUsers)
  assert.ok(invalid.maxAccounts)

  const decimal = validateQuotaPolicyDraft({ maxUsers: '1.5', maxAccounts: '' })
  assert.ok(decimal.maxUsers)
  assert.ok(decimal.maxAccounts)

  assert.deepEqual(
    buildQuotaSettingsPatch({ maxUsers: '0', maxAccounts: '1' }, SAMPLE_QUOTA_REPORT),
    { max_users: 0 },
  )
  assert.deepEqual(buildQuotaResetPatch(), { max_users: null, max_accounts: null })
})

test('announcement validation trims, counts Unicode code points, and rejects line breaks', () => {
  assert.equal(ANNOUNCEMENT_MAX_CHARS, 200)
  assert.equal(countAnnouncementChars('  你好🎉  '), 3)

  assert.deepEqual(validateAnnouncementDraft('  今晚 23:00 系统维护  '), {
    valid: true,
    text: '今晚 23:00 系统维护',
  })

  const empty = validateAnnouncementDraft('   \n  ')
  assert.equal(empty.valid, false)

  const multiLine = validateAnnouncementDraft('第一行\n第二行')
  assert.equal(multiLine.valid, false)

  const sep = validateAnnouncementDraft('第一行\u2028第二行')
  assert.equal(sep.valid, false)

  for (const ch of ['\v', '\f', '\u0085', '\u001c']) {
    assert.equal(validateAnnouncementDraft(`前${ch}后`).valid, false)
  }

  const maxOk = validateAnnouncementDraft('🎉'.repeat(200))
  assert.equal(maxOk.valid, true)

  const over = validateAnnouncementDraft('🎉'.repeat(201))
  assert.equal(over.valid, false)
})

test('channel error routing keeps reauth failures inside modal and surfaces delivery/cooldown outside', () => {
  assert.equal(
    shouldKeepMailTestModalOpen(new ApiError(400, { code: 'validation', message: 'missing password' })),
    true,
  )
  assert.equal(
    shouldKeepMailTestModalOpen(new ApiError(403, { code: 'forbidden', message: 'wrong password' })),
    true,
  )
  assert.equal(
    shouldKeepMailTestModalOpen(
      new ApiError(429, { code: 'rate_limited', message: 'too many failed reauth attempts' }, 60),
    ),
    true,
  )

  const cooldown429 = new ApiError(
    429,
    {
      code: 'rate_limited',
      message: 'a test email was attempted recently; retry later',
      details: { retry_after_seconds: 45 },
    },
    45,
  )
  assert.equal(shouldKeepMailTestModalOpen(cooldown429), false)
  assert.equal(extractRetryAfterSeconds(cooldown429), 45)
  assert.match(formatMailTestResultError(cooldown429), /45 秒/)

  assert.equal(
    shouldKeepMailTestModalOpen(new ApiError(409, { code: 'conflict', message: 'smtp disabled' })),
    false,
  )
  assert.equal(
    shouldKeepMailTestModalOpen(new ApiError(502, { code: 'bad_gateway', message: 'smtp failed' })),
    false,
  )
  assert.match(
    formatWebhookTestError(
      new ApiError(429, { code: 'rate_limited', message: 'cooldown', details: { retry_after_seconds: 30 } }),
    ),
    /30 秒/,
  )

  const csrf403 = new ApiError(403, { code: 'forbidden', message: 'missing or invalid CSRF token' })
  assert.equal(shouldKeepMailTestModalOpen(csrf403), false)
  assert.match(formatMailTestResultError(csrf403), /CSRF/)

  assert.match(
    formatMailReauthModalError(new ApiError(400, { code: 'validation', message: 'missing' })),
    /请输入当前登录密码/,
  )
  assert.match(
    formatMailReauthModalError(new ApiError(403, { code: 'forbidden', message: 'wrong password' })),
    /不正确/,
  )
  assert.match(
    formatMailReauthModalError(
      new ApiError(429, { code: 'rate_limited', message: 'too many failed reauth attempts' }, 75),
    ),
    /75 秒/,
  )
  assert.match(formatMailReauthModalError(new TypeError('network down')), /网络异常/)

  assert.match(
    formatSystemWriteError(new ApiError(403, { code: 'forbidden', message: 'master required' })),
    /仅主管理员可修改/,
  )
  assert.match(
    formatSystemWriteError(new ApiError(429, { code: 'rate_limited', message: 'slow down' }, 15)),
    /15 秒/,
  )
  assert.match(formatSystemWriteError(new Error('offline')), /网络异常/)

  assert.match(
    formatAnnouncementError(new ApiError(400, { code: 'validation', message: 'too long' })),
    /未通过校验/,
  )
  assert.match(formatAnnouncementError(new Error('timeout')), /网络异常/)
})
