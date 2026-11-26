import assert from 'node:assert/strict'
import test from 'node:test'

import { resolveLanding, landingForRole } from '../../src/lib/landing.ts'
import { skyPaletteFor, SKY_PALETTES } from '../../src/features/auth/sky.ts'
import {
  businessDateLabel,
  detectErrorText,
  greetingFor,
  heroClock,
  homeState,
  windowGeometry,
  nextRunText,
} from '../../src/features/me/display.ts'
import { ApiError } from '../../src/lib/api/client.ts'

/**
 * 用户端纯逻辑的不变式。
 *
 * 落地：user 只进 /app，master/admin 只进 /；登录/注册页自身不作回跳。
 * 大字时间去前导 0；窗口轴按整点小时布刻度：band=窗口，空心点=所选档，实心点=实际，
 * 实际点可落在窗口外并被夹住；自动分配时不画所选档。
 * 账号状态优先于今日结果；任何态都有主文案与下一步，不只写「暂无任务」。
 * 日期与问候固定 Asia/Shanghai，换机器不改文本。
 */

const PREF = {
  offset_min: 15,
  allowed: true,
  window: { start: '06:30', end: '07:50' },
  estimated: null,
  estimate_note: '',
}

test('landing sends each role to its own surface', () => {
  assert.equal(landingForRole('user'), '/app')
  assert.equal(landingForRole('admin'), '/')
  assert.equal(landingForRole('master'), '/')
  assert.equal(resolveLanding('user', undefined), '/app')
  assert.equal(resolveLanding('user', '/app/account'), '/app/account')
  assert.equal(resolveLanding('user', '/accounts'), '/app')
  assert.equal(resolveLanding('user', '/apple'), '/app')
  assert.equal(resolveLanding('master', '/app'), '/')
  assert.equal(resolveLanding('admin', '/apple'), '/apple')
  assert.equal(resolveLanding('master', '/login'), '/')
  assert.equal(resolveLanding('admin', '/register?redirect=%2Fapp'), '/')
})

test('hero clock drops the leading zero', () => {
  assert.equal(heroClock('2026-11-11T07:02:00+08:00'), '7:02')
  assert.equal(heroClock('2026-11-11T00:05:00+08:00'), '0:05')
  assert.equal(heroClock(null), null)
})

test('window geometry maps window, chosen slot and actual run onto hour ticks', () => {
  const geometry = windowGeometry(PREF, '2026-11-11T07:02:00+08:00')
  assert.ok(geometry)
  assert.equal(geometry.start, '06:30')
  assert.equal(geometry.end, '07:50')
  assert.deepEqual(geometry.scale, ['06:00', '07:00', '08:00'])
  assert.ok(Math.abs(geometry.bandLeft - 25) < 0.001)
  assert.ok(Math.abs(geometry.bandWidth - 66.6667) < 0.001)
  assert.ok(Math.abs(geometry.chosenLeft - 37.5) < 0.001)
  assert.ok(Math.abs(geometry.actualLeft - 51.6667) < 0.001)
  assert.equal(geometry.chosenText, '06:45')
  assert.equal(geometry.actualText, '07:02')
  assert.equal(geometry.caption, '你选的 06:45 · 实际 07:02')

  const outside = windowGeometry(PREF, '2026-11-11T06:00:00+08:00')
  assert.ok(outside)
  assert.equal(outside.actualLeft, 0)

  const auto = windowGeometry({ ...PREF, offset_min: null }, '2026-11-11T07:02:00+08:00')
  assert.ok(auto)
  assert.equal(auto.chosenLeft, null)
  assert.equal(auto.chosenText, null)
  assert.ok(Math.abs(auto.actualLeft - 51.6667) < 0.001)
})

test('home state covers empty, pending, success and failure', () => {
  const empty = homeState(null, false, null, null, null)
  assert.equal(empty.title, '还没有签到账号')
  assert.equal(empty.primary, 'create')

  const pending = homeState('pending', false, null, null, null)
  assert.equal(pending.title, '账号正在审核')
  assert.equal(pending.primary, null)
  assert.equal(pending.secondary, 'progress')

  const success = homeState(
    'active',
    false,
    'success',
    'wait',
    '2026-11-11T07:02:00+08:00',
  )
  assert.equal(success.title, '今天签到成功')
  assert.equal(success.time, '7:02')
  assert.equal(success.primary, null)
  assert.equal(success.secondary, 'log')

  const failed = homeState(
    'active',
    false,
    'failed',
    'retry_now',
    '2026-11-11T07:12:00+08:00',
  )
  assert.equal(failed.title, '今天没签上')
  assert.equal(failed.primary, 'sign')

  const failedNoRetry = homeState(
    'active',
    false,
    'failed',
    'wait',
    '2026-11-11T07:12:00+08:00',
  )
  assert.equal(failedNoRetry.primary, null)
})

test('date and greeting use the Shanghai wall clock', () => {
  const morning = new Date('2026-11-11T08:00:00+08:00')
  assert.equal(greetingFor(morning), '早上好')
  assert.equal(businessDateLabel(morning), '11月11日 · 周三')
})

test('sky palette follows the Shanghai wall clock, not the machine zone', () => {
  assert.equal(skyPaletteFor(new Date('2026-11-11T06:30:00+08:00')).name, 'dawn')
  // 东京 12:00 = 上海 11:00：换机器不改时段。
  assert.equal(skyPaletteFor(new Date('2026-11-11T12:00:00+09:00')).name, 'day')
  assert.equal(skyPaletteFor(new Date('2026-11-11T16:00:00+08:00')).name, 'dusk')
  assert.equal(skyPaletteFor(new Date('2026-11-11T18:59:00+08:00')).name, 'dusk')
  assert.equal(skyPaletteFor(new Date('2026-11-11T22:00:00+08:00')).name, 'night')
  assert.equal(skyPaletteFor(new Date('2026-11-11T02:00:00+08:00')).name, 'night')
  assert.equal(SKY_PALETTES.night.onDark, true)
  assert.equal(SKY_PALETTES.day.onDark, false)
  for (const palette of Object.values(SKY_PALETTES)) {
    for (const tone of Object.values(palette.tones)) {
      assert.equal(tone.length, 3)
      for (const channel of tone) {
        assert.ok(channel >= 0 && channel <= 1)
      }
    }
  }
})

test('next run says today while the window is still ahead', () => {
  // 早间窗口 06:30 + 15min = 06:45。06:00 时还没到点，当天仍会执行。
  const beforeStart = new Date('2026-11-11T06:00:00+08:00')
  assert.equal(nextRunText(PREF, beforeStart), '今天 06:45 左右')
  // 06:45 整点仍算今天（含等号），文案不跳变。
  assert.equal(nextRunText(PREF, new Date('2026-11-11T06:45:00+08:00')), '今天 06:45 左右')
  // 过了 06:45 才轮到明天。
  assert.equal(nextRunText(PREF, new Date('2026-11-11T06:46:00+08:00')), '明天 06:45 左右')
  assert.equal(nextRunText(PREF, new Date('2026-11-11T23:00:00+08:00')), '明天 06:45 左右')
  // 业务钟：东京 07:00 = 上海 06:00，仍算今天；换机器不改结论。
  assert.equal(nextRunText(PREF, new Date('2026-11-11T07:00:00+09:00')), '今天 06:45 左右')
  // 无窗口时不编造时刻。
  assert.equal(nextRunText(undefined, beforeStart), '明天早上')
})

test('detect error text names an unregistered school and falls back for unknown status', () => {
  assert.equal(
    detectErrorText(new ApiError(404, { code: 'not_found', message: 'school is not registered' })),
    '该学校尚未登记，请联系管理员创建后再试。',
  )
  assert.equal(
    detectErrorText(new ApiError(502, { code: 'upstream_error', message: 'platform failed' })),
    '平台暂时不可用，请稍后再试或手动选学校。',
  )
  // 未单列的状态回落到通用文案，不冒充 404。
  assert.equal(
    detectErrorText(new ApiError(409, { code: 'conflict', message: 'conflict' })),
    '没有找到账号，请检查手机号与口令。',
  )
  assert.equal(detectErrorText(new Error('network down')), '网络异常，请稍后重试。')
})
