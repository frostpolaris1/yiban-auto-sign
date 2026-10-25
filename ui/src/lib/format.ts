/** Number and text formatting. One locale, one formatter per concern. */
const numberFormatter = new Intl.NumberFormat('zh-CN')

export function formatNumber(value: number): string {
  return numberFormatter.format(value)
}

export function formatPercent(value: number, fractionDigits = 0): string {
  return `${value.toFixed(fractionDigits)}%`
}
