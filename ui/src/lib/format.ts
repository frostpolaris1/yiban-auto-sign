/**
 * **功能**
 * 数字与百分比格式化。全仓只此一份。
 *
 * **归属**
 * `lib` 工具层：纯计算，无 React 依赖。
 *
 * **口径**
 * 语言固定 `zh-CN`，千分位随该语言。
 * 百分比小数位由调用方给。缺省 0 位即整数。
 */

const numberFormatter = new Intl.NumberFormat('zh-CN')

export function formatNumber(value: number): string {
  return numberFormatter.format(value)
}

export function formatPercent(value: number, fractionDigits = 0): string {
  return `${value.toFixed(fractionDigits)}%`
}
