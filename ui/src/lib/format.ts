/**
 * **功能**
 * 数字、百分比与时间格式化。全仓只此一份。
 *
 * **归属**
 * `lib` 工具层：纯计算，无 React 依赖。
 *
 * **口径**
 * 语言固定 `zh-CN`，千分位随该语言。
 * 百分比小数位由调用方给。缺省 0 位即整数。
 * 时间固定按 `Asia/Shanghai` 渲染，换机器不改文本。
 * 空值与非日期文本原样退回，不抛错。
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
