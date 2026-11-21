/**
 * **功能**
 * 进出场生命周期钩子：关闭时延迟卸载并暴露 `closing` 标志，驱动双向退场动效。
 *
 * **归属**
 * `features/queue` 动画辅助钩子：仅供本域抽屉、弹窗与行内提示条使用。
 */

import { useEffect, useState } from 'react'

export function usePresence(open: boolean, exitMs = 180): { mounted: boolean; closing: boolean } {
  const [mounted, setMounted] = useState(open)
  const [closing, setClosing] = useState(false)

  if (open && (!mounted || closing)) {
    setMounted(true)
    setClosing(false)
  }

  useEffect(() => {
    if (open || !mounted) return
    const reduced =
      typeof window !== 'undefined' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches

    if (reduced) {
      const frame = window.requestAnimationFrame(() => {
        setMounted(false)
        setClosing(false)
      })
      return () => window.cancelAnimationFrame(frame)
    }

    const frame = window.requestAnimationFrame(() => {
      setClosing(true)
    })
    const timer = window.setTimeout(() => {
      setMounted(false)
      setClosing(false)
    }, exitMs)

    return () => {
      window.cancelAnimationFrame(frame)
      window.clearTimeout(timer)
    }
  }, [open, mounted, exitMs])

  return { mounted, closing }
}
