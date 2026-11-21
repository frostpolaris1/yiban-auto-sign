/**
 * **功能**
 * 首屏即可触发滚动的数字组件：首次数据到位时从 0 滚动到目标值，后续变化平滑衔接。
 *
 * **归属**
 * `features/queue` 展示组件：包装全局 `AnimatedNumber`，补齐首次挂载不滚的缺口。
 */

import { useEffect, useState } from 'react'
import { AnimatedNumber } from '@/components/ui/AnimatedNumber'

export function QueueAnimatedNumber({ value }: { value: number | null }) {
  const [display, setDisplay] = useState<number>(0)

  const reduced =
    typeof window !== 'undefined' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches

  useEffect(() => {
    if (value === null) return
    const frame = window.requestAnimationFrame(() => {
      setDisplay(value)
    })
    return () => window.cancelAnimationFrame(frame)
  }, [value])

  if (value === null) return <>—</>
  return <AnimatedNumber value={reduced ? value : display} />
}
