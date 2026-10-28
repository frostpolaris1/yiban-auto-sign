/**
 * **功能**
 * 数字、百分比与时间格式化，加上日期解析与窗口时间换算。全仓只此一份。
 *
 * **归属**
 * `lib` 工具层：纯计算，无 React 依赖。
 *
 * **口径**
 * 语言固定 `zh-CN`，千分位随该语言。
 * 百分比小数位由调用方给。缺省 0 位即整数。
 * 时间固定按 `Asia/Shanghai` 渲染，换机器不改文本。
 * 格式化函数对空值与非日期文本原样退回，不抛错。
 * 业务日 `YYYY-MM-DD` 与上海墙钟的换算也在此处：上海时区固定 +08:00，没有夏令时。
 * `shanghaiInputToRfc3339` 是唯一的例外：非法输入抛错，由调用方先校验。
 */

const numberFormatter = new Intl.NumberFormat('zh-CN')

export function formatNumber(value: number): string {
  return numberFormatter.format(value)
}

export function formatPercent(value: number, fractionDigits = 0): string {
  return `${value.toFixed(fractionDigits)}%`
}

const dateTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return dateTimeFormatter.format(date)
}

/** 上海墙钟的年月日时分。`hourCycle: 'h23'` 保证 0 点写作 `00` 而不是 `24`。 */
type ShanghaiParts = {
  year: string
  month: string
  day: string
  hour: string
  minute: string
}

const shanghaiPartsFormatter = new Intl.DateTimeFormat('en-US', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
})

function shanghaiParts(value: Date): ShanghaiParts {
  const parts = shanghaiPartsFormatter.formatToParts(value)
  const pick = (type: Intl.DateTimeFormatPartTypes): string =>
    parts.find((part) => part.type === type)?.value ?? ''
  return {
    year: pick('year'),
    month: pick('month'),
    day: pick('day'),
    hour: pick('hour'),
    minute: pick('minute'),
  }
}

/** 日期输入与查询串用的业务日。按上海日历取，不按机器时区。 */
export function dayOf(date: Date): string {
  const { year, month, day } = shanghaiParts(date)
  return `${year}-${month}-${day}`
}

const DAY_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/

/** 只认 `YYYY-MM-DD` 且必须是真实日历日；其余返回 null。 */
export function parseDay(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const match = DAY_PATTERN.exec(value)
  if (!match) return null
  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const date = new Date(Date.UTC(year, month - 1, day))
  if (
    date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month - 1 ||
    date.getUTCDate() !== day
  ) {
    return null
  }
  return value
}

/** 窗口时间只显示时分，例 `08:10`。 */
export function formatClock(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const { hour, minute } = shanghaiParts(date)
  return `${hour}:${minute}`
}

/** RFC3339 转 `<input type="datetime-local">` 的值：上海墙钟 `YYYY-MM-DDTHH:mm`。 */
export function isoToShanghaiInput(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const { year, month, day, hour, minute } = shanghaiParts(date)
  return `${year}-${month}-${day}T${hour}:${minute}`
}

export const INPUT_PATTERN = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})$/

/** 值是否是合法的 datetime-local 输入（形状、日历日、时分范围全过）。 */
export function isValidShanghaiInput(value: string): boolean {
  const match = INPUT_PATTERN.exec(value)
  if (!match) return false
  const hour = Number(match[2])
  const minute = Number(match[3])
  return parseDay(match[1]) !== null && hour <= 23 && minute <= 59
}

/**
 * `<input type="datetime-local">` 的值转 RFC3339。
 * 输入按上海墙钟解释，输出带固定的 `+08:00` 偏移。
 */
export function shanghaiInputToRfc3339(value: string): string {
  if (!isValidShanghaiInput(value)) {
    throw new Error(`时间格式必须是 YYYY-MM-DDTHH:mm，收到 ${value}`)
  }
  const match = INPUT_PATTERN.exec(value)!
  return `${match[1]}T${match[2]}:${match[3]}:00+08:00`
}
