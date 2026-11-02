/**
 * **功能**
 * Chart.js 画布宿主：注册控制器、按主题重建、卸载即销毁。
 *
 * **归属**
 * `features/dashboard` 展示层：配置由 charts.ts 给，本组件只管生命周期。
 *
 * **口径**
 * 主题令牌从 `data-theme` 下的 CSS 变量读，不写色值。
 * 换肤时重建实例：Chart.js 的色值在创建时固化，改配置不足以换色。
 * 动画关闭：视觉基线需要落定帧。
 */

import { useEffect, useRef, useState } from 'react'
import {
  ArcElement,
  BarController,
  BarElement,
  CategoryScale,
  Chart,
  DoughnutController,
  Legend,
  LineController,
  LineElement,
  LinearScale,
  PointElement,
  Tooltip,
  type ChartConfiguration,
} from 'chart.js'
import type { ChartPalette } from './charts'
import { STATUS_VARS } from './charts'

Chart.register(
  ArcElement,
  BarController,
  BarElement,
  CategoryScale,
  DoughnutController,
  Legend,
  LineController,
  LineElement,
  LinearScale,
  PointElement,
  Tooltip,
)

/** 从当前主题读调色板。 */
export function readPalette(): ChartPalette {
  const style = getComputedStyle(document.documentElement)
  const token = (name: string) => style.getPropertyValue(name).trim()
  return {
    success: token(STATUS_VARS.success),
    danger: token(STATUS_VARS.failed),
    warning: token(STATUS_VARS.skipped),
    info: token(STATUS_VARS.pending),
    primary: token(STATUS_VARS.running),
    text: token('--t-base'),
    muted: token('--t-muted'),
    grid: token('--border'),
  }
}

type ChartCanvasProps = {
  ariaLabel: string
  /** 数据与主题就绪时返回配置；返回 null 时不建实例。 */
  build: (palette: ChartPalette) => ChartConfiguration | null
}

export function ChartCanvas({ ariaLabel, build }: ChartCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const [themeTick, setThemeTick] = useState(0)

  useEffect(() => {
    const observer = new MutationObserver(() => setThemeTick((tick) => tick + 1))
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    })
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const config = build(readPalette())
    if (!config) return
    const chart = new Chart(canvas, config)
    return () => chart.destroy()
  }, [build, themeTick])

  return <canvas ref={canvasRef} role="img" aria-label={ariaLabel} className="app-chart-canvas" />
}
