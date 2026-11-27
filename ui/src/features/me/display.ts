/**
 * **功能**
 * 用户端首页的纯展示派生：问候、日期、窗口位置、下一次与状态文案。
 *
 * **归属**
 * `features/me` 展示层：纯函数，无 React、无网络。
 *
 * **口径**
 * 业务日与墙钟固定 Asia/Shanghai；不按机器时区。
 * 窗口轴范围取整点小时，band 画窗口、空心点画所选档、实心点画实际。
 * 没有时间片数据时不画空心点，轴不编造所选档。
 * 状态文案只描述下一步，不冒充平台原因。
 * 错误文案按 HTTP 状态收敛为一句可行动提示，未知状态回落到通用文案。
 */

import type { AccountStatus } from '@/lib/api/account'
import { ApiError } from '@/lib/api/client'
import type { DiagnosticsSuggestion, SignCalendarDay } from '@/lib/api/me'
import type { TimePref } from '@/lib/api/schedule'
import type { School } from '@/lib/api/school'
import { formatClock } from '@/lib/format'

/** 日结果主态；none 表示未来、无记录或没有任何结果。 */
export type CalendarDayTone = 'success' | 'danger' | 'warning' | 'pending' | 'none'

/**
 * 一天的主态：失败 > 成功 > 待确认 > 未执行 > 无结果。
 *
 * 记录页日历与「我的」30 天热力条共用这一条优先级，
 * 避免同一天在不同页面显示成不同状态；日明细按账号分别展示。
 */
export function calendarDayTone(
  info: SignCalendarDay | undefined,
  future = false,
): CalendarDayTone {
  if (future || !info || info.total === 0) return 'none'
  if (info.failed > 0) return 'danger'
  if (info.success > 0) return 'success'
  if (info.unknown > 0) return 'pending'
  if (info.skipped > 0) return 'warning'
  return 'none'
}

const SHANGHAI = 'Asia/Shanghai'

const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: SHANGHAI,
  month: 'long',
  day: 'numeric',
})

const hourFormatter = new Intl.DateTimeFormat('en-US', {
  timeZone: SHANGHAI,
  hour: 'numeric',
  hourCycle: 'h23',
})

export function greetingFor(date: Date): string {
  const hour = Number(hourFormatter.format(date))
  if (hour < 6) return '夜深了'
  if (hour < 12) return '早上好'
  if (hour < 18) return '下午好'
  return '晚上好'
}

export function businessDateLabel(date: Date): string {
  return `${dateFormatter.format(date)} · ${weekdayFormatter.format(date)}`
}

/** 首页大字时间去掉前导 0：07:02 → 7:02；无值返回 null。 */
export function heroClock(value: string | null | undefined): string | null {
  if (!value) return null
  const text = formatClock(value)
  if (text === '—') return null
  return text.startsWith('0') ? text.slice(1) : text
}

export function greetingName(name: string): string {
  return name.trim() === '' ? '你好' : name
}

export type WindowGeometry = {
  bandLeft: number
  bandWidth: number
  chosenLeft: number | null
  actualLeft: number | null
  scale: string[]
  start: string
  end: string
  caption: string
  chosenText: string | null
  actualText: string | null
}

function clockMinutes(value: string): number | null {
  const match = /^(\d{2}):(\d{2})$/.exec(value)
  if (!match) return null
  const hour = Number(match[1])
  const minute = Number(match[2])
  if (hour > 23 || minute > 59) return null
  return hour * 60 + minute
}

function minutesToClock(minutes: number): string {
  const hour = String(Math.floor(minutes / 60)).padStart(2, '0')
  const minute = String(minutes % 60).padStart(2, '0')
  return `${hour}:${minute}`
}

/**
 * 窗口轴几何。轴范围取窗口两端的小时（floor/ceil），小时刻度打点；
 * band 画整个可签到窗口，空心点画用户选的 5 分钟档（`offset_min` 相对窗口起点），
 * 实心点画实际执行（RFC3339，取上海墙钟，夹到轴两端）。
 * 窗口缺失或起止非法时返回 null；自动分配时没有空心点。
 */
export function windowGeometry(
  pref: TimePref | undefined,
  runAt: string | null | undefined,
): WindowGeometry | null {
  if (!pref?.window) return null
  const start = clockMinutes(pref.window.start)
  const end = clockMinutes(pref.window.end)
  if (start === null || end === null || end <= start) return null
  const domainStart = Math.floor(start / 60) * 60
  const domainEnd = Math.ceil(end / 60) * 60
  const domain = domainEnd - domainStart
  const toPercent = (minutes: number) =>
    Math.min(100, Math.max(0, ((minutes - domainStart) / domain) * 100))
  const scale: string[] = []
  for (let m = domainStart; m <= domainEnd; m += 60) scale.push(minutesToClock(m))
  const offset = pref.offset_min
  const chosen =
    offset === null || offset === undefined ? null : start + Math.max(0, Math.min(offset, end - start))
  const actual = runAt ? clockMinutes(formatClock(runAt)) : null
  const selectedText = chosen === null ? null : minutesToClock(chosen)
  const actualText = actual === null ? null : minutesToClock(actual)
  const caption =
    selectedText === null
      ? '系统按窗口自动分配'
      : actualText
        ? `你选的 ${selectedText} · 实际 ${actualText}`
        : `你选的 ${selectedText}`
  return {
    bandLeft: toPercent(start),
    bandWidth: toPercent(end) - toPercent(start),
    chosenLeft: chosen === null ? null : toPercent(chosen),
    actualLeft: actual === null ? null : toPercent(actual),
    scale,
    start: pref.window.start,
    end: pref.window.end,
    caption,
    chosenText: selectedText,
    actualText,
  }
}

/**
 * 下一次执行的近似文案；只由窗口与时间片推导，不承诺精确。
 *
 * **「明天」只在窗口确实已过时出现。** 早间窗口（如 06:30）在当天尚未到点时，
 * 文案必须写「今天 06:30 左右」——写死「明天」会让用户以为今天不签了。
 * 判据取业务钟（Asia/Shanghai）当天的当前分钟，与 `todayKey` 同一时区来源，
 * 不读机器时区。窗口起点尚未到（`now < start`）即今天还能签。
 *
 * 下游只把它当提示，不参与任何调度：执行时刻由窗口与平台决定。
 */
export function nextRunText(pref: TimePref | undefined, now: Date = new Date()): string {
  const start = pref?.window ? clockMinutes(pref.window.start) : null
  if (start === null) {
    if (!pref?.window) return '明天早上'
    return `明天 ${pref.window.start}`
  }
  const offset = pref?.offset_min ?? 0
  const minutes = Math.min(23 * 60 + 59, start + offset)
  const hour = String(Math.floor(minutes / 60)).padStart(2, '0')
  const minute = String(minutes % 60).padStart(2, '0')
  const clock = `${hour}:${minute}`
  return businessNowMinutes(now) <= minutes ? `今天 ${clock} 左右` : `明天 ${clock} 左右`
}

/** 业务钟（Asia/Shanghai）当天的分钟数；不读机器时区。 */
function businessNowMinutes(now: Date): number {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: SHANGHAI,
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(now)
  const pick = (type: string) => Number(parts.find((part) => part.type === type)?.value ?? '0')
  return pick('hour') * 60 + pick('minute')
}

export type HomeTone = 'success' | 'warning' | 'danger' | 'info'

export type HomeState = {
  title: string
  note: string
  time: string | null
  tone: HomeTone
  primary:
    | 'sign'
    | 'resume'
    | 'edit'
    | 'password'
    | 'restore'
    | 'create'
    | null
  secondary: 'log' | 'contact' | 'progress' | null
}

/** 账号状态优先于今日结果；任何态都不只写「今日暂无任务」。 */
export function homeState(
  status: AccountStatus | null,
  deleted: boolean,
  todayState: string | null,
  suggestion: DiagnosticsSuggestion | null,
  runAt: string | null,
): HomeState {
  if (status === null) {
    return {
      title: '还没有签到账号',
      note: '添加后每天自动签到；审核通过前不会执行。',
      time: null,
      tone: 'info',
      primary: 'create',
      secondary: null,
    }
  }
  if (deleted) {
    return {
      title: '账号已删除',
      note: '7 天内可以恢复；恢复后继续自动签到。',
      time: null,
      tone: 'info',
      primary: 'restore',
      secondary: null,
    }
  }
  switch (status) {
    case 'pending':
      return {
        title: '账号正在审核',
        note: '通过后会自动开始签到；不需要重复提交。',
        time: null,
        tone: 'warning',
        primary: null,
        secondary: 'progress',
      }
    case 'rejected':
      return {
        title: '需要修改资料后重新审核',
        note: '按驳回理由改好资料再提交。',
        time: null,
        tone: 'danger',
        primary: 'edit',
        secondary: null,
      }
    case 'credential_error':
      return {
        title: '易班口令需要更新',
        note: '更新后恢复自动签到。',
        time: null,
        tone: 'danger',
        primary: 'password',
        secondary: null,
      }
    case 'paused_admin':
      return {
        title: '账号被管理员暂停',
        note: '暂停期间不签到；请联系管理员了解原因。',
        time: null,
        tone: 'info',
        primary: null,
        secondary: 'contact',
      }
    case 'paused_user':
      return {
        title: '账号已暂停',
        note: '暂停期间不签到；恢复后从下一次开始。',
        time: null,
        tone: 'info',
        primary: 'resume',
        secondary: null,
      }
    default:
      break
  }

  switch (todayState) {
    case 'success':
      return {
        title: '今天签到成功',
        note: '本次已成功，不需要再操作。',
        time: heroClock(runAt),
        tone: 'success',
        primary: null,
        secondary: 'log',
      }
    case 'running':
      return {
        title: '正在帮你签到',
        note: '通常几十秒内完成。',
        time: heroClock(runAt),
        tone: 'info',
        primary: null,
        secondary: null,
      }
    case 'failed':
      return {
        title: '今天没签上',
        note:
          suggestion === 'retry_now'
            ? '可以再试一次；系统也会在窗口内继续重试。'
            : '这次没有成功；系统会按计划继续重试。',
        time: heroClock(runAt),
        tone: 'danger',
        primary: suggestion === 'retry_now' ? 'sign' : null,
        secondary: 'log',
      }
    case 'skipped':
      return {
        title: '今天没有执行签到',
        note: '窗口外或没有可签到的目标；明天会自动继续。',
        time: heroClock(runAt),
        tone: 'warning',
        primary: null,
        secondary: 'log',
      }
    case 'pending':
      return {
        title: '今天还没到签到时间',
        note: '到点会自动开始；不需要手动操作。',
        time: null,
        tone: 'info',
        primary: null,
        secondary: 'log',
      }
    default:
      return {
        title: '今天不用签到',
        note: '明天会自动继续；可以在历史里回看记录。',
        time: null,
        tone: 'info',
        primary: null,
        secondary: 'log',
      }
  }
}

const weekdayFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: SHANGHAI,
  weekday: 'short',
})

/** 7 天条的星期标签；`day` 是业务日 YYYY-MM-DD。 */
export function weekdayLabel(day: string): string {
  const date = new Date(`${day}T12:00:00+08:00`)
  if (Number.isNaN(date.getTime())) return day
  return weekdayFormatter.format(date)
}

export function todayKey(date: Date): string {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: SHANGHAI,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(date)
  const pick = (type: string) => parts.find((part) => part.type === type)?.value ?? ''
  return `${pick('year')}-${pick('month')}-${pick('day')}`
}

/** detect（平台探测）失败文案；404 表示平台院校尚未登记，需管理员先建校。 */
export function detectErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 400) return '请填写正确的手机号与易班口令。'
    if (error.status === 403) return '没有权限执行这个操作。'
    if (error.status === 404) return '该学校尚未登记，请联系管理员创建后再试。'
    if (error.status === 429) return '尝试过多，请稍后再试。'
    if (error.status === 502) return '平台暂时不可用，请稍后再试或手动选学校。'
    if (error.status === 503) return '系统维护中，请稍后再试或手动选学校。'
    return '没有找到账号，请检查手机号与口令。'
  }
  return '网络异常，请稍后重试。'
}

/** 学校名兜底：优先匹配；查询失败、未登记、加载中给三种不同提示。 */
export function schoolLabel(
  schools: School[] | undefined,
  isError: boolean,
  schoolId: string | null | undefined,
): string {
  const matched = schools?.find((school) => school.id === schoolId)?.name
  if (matched) return matched
  if (isError) return '学校信息暂时读不到'
  if (schools) return '学校未登记'
  return '学校信息加载中'
}

export function photoUploadErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 413) return '照片过大，请压缩后再试。'
    if (error.status === 400 || error.status === 415) return '仅支持 PNG 或 JPEG 照片，请重新选择。'
    if (error.status === 429) return '上传过于频繁，请稍后再试。'
    if (error.status === 503) return '系统维护中，请稍后再试。'
    return '照片上传失败，请稍后重试。'
  }
  return '网络异常，请稍后重试。'
}
