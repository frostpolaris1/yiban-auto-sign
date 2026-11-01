/**
 * **功能**
 * 执行监控的展示构建：租约剩余的纯函数与时钟推进钩子。
 *
 * **归属**
 * `features/run` 展示层：只做格式化，不取数、不判角色。
 *
 * **口径**
 * 服务端只给租约的**绝对到期时间**（lease_expires_at）；剩余量由
 * 页面按客户端时钟渲染，每秒推进。剩余 <= 0 显示「已到期」：
 * 到期未回收的行在下一轮 tick 被回收器放回，监控如实显示当下。
 * 负数不外泄：调用方拿不到「还剩 -3 秒」这样的文本。
 */

import { useEffect, useState } from 'react'

/** 租约剩余的展示文本。at 是当前时刻毫秒数；due 是到期时刻的毫秒数。 */
export function leaseRemainingText(dueMs: number, nowMs: number): string {
  const remainMs = dueMs - nowMs
  if (remainMs <= 0) return '已到期'
  const totalSeconds = Math.floor(remainMs / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes <= 0) return `${seconds} 秒`
  if (minutes < 60) return `${minutes} 分 ${seconds} 秒`
  const hours = Math.floor(minutes / 60)
  return `${hours} 时 ${minutes % 60} 分`
}

/** useNow 每隔 intervalMs 推进一次当前时刻毫秒数。卸载即停表。 */
export function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(timer)
  }, [intervalMs])
  return now
}
