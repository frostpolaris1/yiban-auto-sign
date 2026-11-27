/**
 * **功能**
 * 今天页的纯展示派生：精确窗口标尺、成功细节与平静的状态文案。
 *
 * **归属**
 * user-next/lib：只做纯函数，不取数、不渲染。
 *
 * **口径**
 * 业务钟固定 Asia/Shanghai；窗口标尺按小时取整，band 只画真实窗口。
 * 没有选中档时不编造空心点；实际时间夹在轴内。
 * 状态文案只描述下一步，不冒充平台原因；失败不暴露内部错误码。
 */

import type { AccountStatus } from '@/lib/api/account'
import type { DiagnosticsSuggestion } from '@/lib/api/me'
import type { TimePref } from '@/lib/api/schedule'
import { formatClock } from '@/lib/format'
import { homeState, type HomeState, type HomeTone } from '@/features/me/display'

const SHANGHAI = 'Asia/Shanghai'

function parseClock(value: string): number | null {
  const match = /^(\d{2}):(\d{2})$/.exec(value)
  if (!match) return null
  const hour = Number(match[1])
  const minute = Number(match[2])
  if (hour > 23 || minute > 59) return null
  return hour * 60 + minute
}

function clockOf(minutes: number): string {
  const hour = String(Math.floor(minutes / 60)).padStart(2, '0')
  const minute = String(minutes % 60).padStart(2, '0')
  return `${hour}:${minute}`
}

function businessMinutes(now: Date): number {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: SHANGHAI,
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(now)
  const pick = (type: string) => Number(parts.find((part) => part.type === type)?.value ?? '0')
  return (pick('hour') % 24) * 60 + pick('minute')
}

export type RulerTick = {
  text: string
  left: number
  major: boolean
  bound: boolean
}

export type RulerGeometry = {
  start: string
  end: string
  domainStart: string
  domainEnd: string
  bandLeft: number
  bandWidth: number
  chosenLeft: number | null
  actualLeft: number | null
  nowLeft: number | null
  chosenText: string | null
  actualText: string | null
  caption: string
  ticks: RulerTick[]
}

/**
 * 窗口标尺几何。domain 取整点小时，band 是真实可签到窗口；
 * 取整后多出的 30 分钟上下文让 band 看起来是被框住的区间，而不是进度条。
 */
export function rulerGeometry(
  pref: TimePref | undefined,
  runAt: string | null | undefined,
  now: Date = new Date(),
): RulerGeometry | null {
  if (!pref?.window) return null
  const start = parseClock(pref.window.start)
  const end = parseClock(pref.window.end)
  if (start === null || end === null || end <= start) return null

  const domainStart = Math.floor(start / 60) * 60
  const domainEnd = Math.ceil(end / 60) * 60
  const span = domainEnd - domainStart
  if (span <= 0) return null
  const pct = (minutes: number) => ((minutes - domainStart) / span) * 100

  const offset = pref.offset_min
  const chosen =
    offset === null || offset === undefined
      ? null
      : start + Math.max(0, Math.min(offset, end - start))
  const actualClock = runAt ? formatClock(runAt) : '—'
  const actualMinute = actualClock === '—' ? null : parseClock(actualClock)

  const chosenText = chosen === null ? null : clockOf(chosen)
  const actualText = actualMinute === null ? null : clockOf(actualMinute)
  const actualLeft =
    actualMinute === null
      ? null
      : pct(Math.min(domainEnd, Math.max(domainStart, actualMinute)))
  const caption =
    chosenText === null
      ? actualText
        ? `系统按窗口分配 · 实际 ${actualText}`
        : '系统按窗口自动分配'
      : actualText
        ? `你选的 ${chosenText} · 实际 ${actualText}`
        : `你选的 ${chosenText}`

  const ticks: RulerTick[] = []
  for (let minute = domainStart; minute <= domainEnd; minute += 30) {
    ticks.push({
      text: clockOf(minute),
      left: pct(minute),
      major: minute % 60 === 0,
      bound: minute === start || minute === end,
    })
  }

  const nowMinute = businessMinutes(now)
  const nowInside = nowMinute >= domainStart && nowMinute <= domainEnd

  return {
    start: pref.window.start,
    end: pref.window.end,
    domainStart: clockOf(domainStart),
    domainEnd: clockOf(domainEnd),
    bandLeft: pct(start),
    bandWidth: pct(end) - pct(start),
    chosenLeft: chosen === null ? null : pct(chosen),
    actualLeft,
    nowLeft: nowInside ? pct(nowMinute) : null,
    chosenText,
    actualText,
    caption,
    ticks,
  }
}

function minuteGapText(chosenText: string | null, actualText: string | null): string | null {
  if (!chosenText || !actualText) return null
  const chosen = parseClock(chosenText)
  const actual = parseClock(actualText)
  if (chosen === null || actual === null) return null
  const diff = actual - chosen
  if (diff === 0) return '与预选时间一致'
  const absolute = Math.abs(diff)
  const hours = Math.floor(absolute / 60)
  const minutes = absolute % 60
  const amount = hours > 0 ? `${hours} 小时${minutes > 0 ? ` ${minutes} 分钟` : ''}` : `${minutes} 分钟`
  return diff > 0 ? `比预选时间晚 ${amount}` : `比预选时间早 ${amount}`
}

export function successDetail(
  attempts: number | null | undefined,
  chosenText: string | null,
  actualText: string | null,
): string {
  const parts: string[] = []
  if (attempts && attempts > 0) parts.push(`第 ${attempts} 次尝试`)
  const gap = minuteGapText(chosenText, actualText)
  if (gap) parts.push(gap)
  if (parts.length === 0) parts.push('窗口内自动完成')
  return parts.join(' · ')
}

export type TodayStory = HomeState & {
  detail: string | null
}

/** 平静、具体、只描述下一步的今日文案。 */
export function todayStory(
  status: AccountStatus | null,
  deleted: boolean,
  todayState: string | null,
  suggestion: DiagnosticsSuggestion | null,
  attempts: number | null | undefined,
  chosenText: string | null,
  actualText: string | null,
): TodayStory {
  const base = homeState(status, deleted, todayState, suggestion, null)

  if (status === null) {
    return {
      ...base,
      title: '还没有签到账号',
      note: '添加后每天自动签到，审核通过前不会执行。',
      detail: null,
      primary: 'create',
      secondary: null,
    }
  }
  if (deleted) {
    return {
      ...base,
      title: '账号已删除',
      note: '7 天内可以恢复；恢复后继续自动签到。',
      detail: null,
      primary: 'restore',
      secondary: null,
    }
  }
  switch (status) {
    case 'pending':
      return {
        ...base,
        title: '账号正在审核',
        note: '通过后会自动开始签到，不需要重复提交。',
        detail: null,
        primary: null,
        secondary: 'progress',
      }
    case 'rejected':
      return {
        ...base,
        title: '资料需要修改',
        note: '按驳回理由改好资料，再提交审核。',
        detail: null,
        primary: 'edit',
        secondary: null,
      }
    case 'credential_error':
      return {
        ...base,
        title: '易班口令需要更新',
        note: '更新后恢复自动签到。',
        detail: null,
        primary: 'password',
        secondary: null,
      }
    case 'paused_admin':
      return {
        ...base,
        title: '账号被管理员暂停',
        note: '暂停期间不会签到；请联系管理员了解原因。',
        detail: null,
        primary: null,
        secondary: 'contact',
      }
    case 'paused_user':
      return {
        ...base,
        title: '账号已暂停',
        note: '暂停期间不会签到；恢复后从下一次开始。',
        detail: null,
        primary: 'resume',
        secondary: null,
      }
    default:
      break
  }

  switch (todayState) {
    case 'success':
      return {
        ...base,
        title: '今天已签到',
        note: '窗口内自动完成，不需要再操作。',
        detail: successDetail(attempts, chosenText, actualText),
        primary: null,
        secondary: 'log',
      }
    case 'running':
      return {
        ...base,
        title: '正在签到',
        note: '通常几十秒内完成，结果会自己出现。',
        detail: null,
        primary: null,
        secondary: null,
      }
    case 'failed':
      return {
        ...base,
        title: '今天没签上',
        note:
          suggestion === 'retry_now'
            ? '可以再试一次；系统也会在窗口内继续重试。'
            : '系统会按计划继续重试；如果仍然失败，请联系管理员。',
        detail: null,
        primary: suggestion === 'retry_now' ? 'sign' : null,
        secondary: suggestion === 'contact_admin' ? 'contact' : 'log',
      }
    case 'skipped':
      return {
        ...base,
        title: '今天没有执行',
        note: '窗口外或没有可签到的目标；明天会自动继续。',
        detail: null,
        primary: null,
        secondary: 'log',
      }
    case 'pending':
      return {
        ...base,
        title: '今天还没到签到时间',
        note: '到点会自动开始，不需要手动操作。',
        detail: null,
        primary: null,
        secondary: 'log',
      }
    default:
      return {
        ...base,
        title: '今天不用签到',
        note: '明天会自动继续；记录里可以回看。',
        detail: null,
        primary: null,
        secondary: 'log',
      }
  }
}

export function toneFromStory(story: TodayStory): HomeTone {
  return story.tone
}
