/**
 * **功能**
 * 看板三张图的纯逻辑：数据 → Chart.js 配置与摘要文案。
 *
 * **归属**
 * `features/dashboard` 展示层：无 React、无网络；canvas 生命周期在 ChartCanvas。
 *
 * **口径**
 * 只画契约给的字段：日桶互斥、by_status 桶间可重叠，摘要不许把桶相加当总数。
 * 空数据返回 null，由卡片给空态文案，不画空坐标轴。
 * 颜色只从调用方给的主题调色板取，不在本文件写色值。
 * 趋势只画三天主序列；待签/进行中只在窗口内出现过时才进图例。
 */

import type { ChartConfiguration } from 'chart.js'
import type { PrefStatsReport, SignStatsReport } from '@/lib/api/stats'

export type ChartPalette = {
  success: string
  danger: string
  warning: string
  info: string
  primary: string
  text: string
  muted: string
  grid: string
}

export const STATUS_LABELS = {
  success: '成功',
  failed: '失败',
  skipped: '跳过',
  pending: '待签',
  running: '进行中',
} as const

export const STATUS_VARS = {
  success: '--success',
  failed: '--danger',
  skipped: '--warning',
  pending: '--info',
  running: '--primary',
} as const

const BASE_OPTIONS = {
  responsive: true,
  maintainAspectRatio: false,
  animation: false as const,
  plugins: {
    legend: { position: 'bottom' as const, labels: { boxWidth: 10, boxHeight: 10 } },
  },
}

/** 趋势图：按日堆叠柱。无任何非零计数时返回 null。 */
export function trendConfig(
  report: SignStatsReport | undefined,
  palette: ChartPalette,
): ChartConfiguration | null {
  if (!report) return null
  const totals = trendTotals(report)
  if (totals.total === 0) return null
  const labels = report.days.map((day) => day.day.slice(5))
  const series = [
    { key: 'success', label: STATUS_LABELS.success, color: palette.success },
    { key: 'failed', label: STATUS_LABELS.failed, color: palette.danger },
    { key: 'skipped', label: STATUS_LABELS.skipped, color: palette.warning },
  ] as const
  const datasets: Array<{
    label: string
    data: number[]
    backgroundColor: string
    stack: string
  }> = series.map((item) => ({
    label: item.label,
    data: report.days.map((day) => day[item.key]),
    backgroundColor: item.color,
    stack: 'sign',
  }))
  // 只有窗口内真出现过的状态才进图例：零序列不占图例位。
  if (report.by_status.pending > 0) {
    datasets.push({
      label: STATUS_LABELS.pending,
      data: report.days.map((day) => day.pending),
      backgroundColor: palette.info,
      stack: 'sign',
    })
  }
  if (report.by_status.running > 0) {
    datasets.push({
      label: STATUS_LABELS.running,
      data: report.days.map((day) => day.running),
      backgroundColor: palette.primary,
      stack: 'sign',
    })
  }
  return {
    type: 'bar',
    data: { labels, datasets },
    options: {
      ...BASE_OPTIONS,
      plugins: {
        ...BASE_OPTIONS.plugins,
        legend: {
          ...BASE_OPTIONS.plugins.legend,
          labels: { ...BASE_OPTIONS.plugins.legend.labels, color: palette.muted },
        },
      },
      scales: {
        x: {
          stacked: true,
          ticks: { color: palette.muted, maxRotation: 0, autoSkip: true },
          grid: { color: palette.grid },
        },
        y: {
          stacked: true,
          beginAtZero: true,
          ticks: { color: palette.muted, precision: 0 },
          grid: { color: palette.grid },
        },
      },
    },
  }
}

/** 分布图：按状态去重的账号数。全零时返回 null。 */
export function distributionConfig(
  report: SignStatsReport | undefined,
  palette: ChartPalette,
): ChartConfiguration<'doughnut'> | null {
  if (!report) return null
  const keys = (Object.keys(STATUS_LABELS) as Array<keyof typeof STATUS_LABELS>).filter(
    (key) => report.by_status[key] > 0,
  )
  if (keys.length === 0) return null
  const colors: Record<string, string> = {
    success: palette.success,
    failed: palette.danger,
    skipped: palette.warning,
    pending: palette.info,
    running: palette.primary,
  }
  return {
    type: 'doughnut',
    data: {
      labels: keys.map((key) => STATUS_LABELS[key]),
      datasets: [
        {
          data: keys.map((key) => report.by_status[key]),
          backgroundColor: keys.map((key) => colors[key]),
          borderWidth: 0,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      cutout: '62%',
      plugins: {
        legend: {
          position: 'bottom',
          labels: { boxWidth: 10, boxHeight: 10, color: palette.muted },
        },
      },
    },
  }
}

/** 自选拥挤度：柱=人数，线=每片容量。无槽时返回 null。 */
export function prefConfig(
  report: PrefStatsReport | undefined,
  palette: ChartPalette,
): ChartConfiguration | null {
  if (!report || report.slots.length === 0) return null
  return {
    type: 'bar',
    data: {
      labels: report.slots.map((slot) => slot.label),
      datasets: [
        {
          label: '自选人数',
          data: report.slots.map((slot) => slot.count),
          backgroundColor: palette.primary,
          order: 2,
        },
        {
          type: 'line',
          label: '每片容量',
          data: report.slots.map((slot) => slot.cap),
          borderColor: palette.warning,
          backgroundColor: palette.warning,
          borderDash: [6, 4],
          pointRadius: 0,
          borderWidth: 2,
          order: 1,
        },
      ],
    },
    options: {
      ...BASE_OPTIONS,
      plugins: {
        ...BASE_OPTIONS.plugins,
        legend: {
          ...BASE_OPTIONS.plugins.legend,
          labels: { ...BASE_OPTIONS.plugins.legend.labels, color: palette.muted },
        },
      },
      scales: {
        x: {
          ticks: { color: palette.muted, maxRotation: 0, autoSkip: true },
          grid: { color: palette.grid },
        },
        y: {
          beginAtZero: true,
          ticks: { color: palette.muted, precision: 0 },
          grid: { color: palette.grid },
        },
      },
    },
  }
}

export type TrendTotals = {
  success: number
  failed: number
  skipped: number
  pending: number
  running: number
  total: number
  missing: number
}

/** 趋势摘要：窗口内各终态合计与缺失天数。 */
export function trendTotals(report: SignStatsReport): TrendTotals {
  const totals = report.days.reduce(
    (sum, day) => ({
      success: sum.success + day.success,
      failed: sum.failed + day.failed,
      skipped: sum.skipped + day.skipped,
      pending: sum.pending + day.pending,
      running: sum.running + day.running,
    }),
    { success: 0, failed: 0, skipped: 0, pending: 0, running: 0 },
  )
  const settled = totals.success + totals.failed + totals.skipped
  return {
    ...totals,
    total: settled + totals.pending + totals.running,
    missing: report.days_missing.length,
  }
}

export type PrefSummary = {
  users: number
  hottest: { label: string; count: number } | null
}

/** 自选摘要：人数合计与最热时段；全零时不编造「最热」。 */
export function prefSummary(report: PrefStatsReport): PrefSummary {
  let users = 0
  let hottest: { label: string; count: number } | null = null
  for (const slot of report.slots) {
    users += slot.count
    if (slot.count > 0 && (hottest === null || slot.count > hottest.count)) {
      hottest = { label: slot.label, count: slot.count }
    }
  }
  return { users, hottest }
}
