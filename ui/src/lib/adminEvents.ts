/**
 * **功能**
 * 管理端实时事件钩子：连接 `GET /api/admin/events`（SSE），触发查询失效并处理断线、补发与结束态。
 *
 * **归属**
 * `lib` 工具层：供运行页、执行监控页与待办工作台共用，不含渲染逻辑。
 *
 * **口径**
 * `task.progress`、`task.concluded`、`window.changed` 触发刷新；
 * `capacity.changed`、`egress.health` 只通知 `onEvent`，不触发刷新。
 * 支持可选 `coalesceMs` 合并高频突发事件，以及 `onInvalidate` 按事件类型精准失效缓存。
 * 心跳看门狗：45 秒无任意事件转 `disconnected`；断线重连后立即全量重同步。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { QueryClient } from '@tanstack/react-query'
import { formatClock } from '@/lib/format'

/** 触发刷新的事件名集合。与契约 `/api/admin/events` 的词表一致。 */
const REFRESH_EVENTS = ['task.progress', 'task.concluded', 'window.changed'] as const

/** 只进事件展示、不触发刷新的业务事件名。 */
const OBSERVE_EVENTS = ['capacity.changed', 'egress.health'] as const

/** 断线降级轮询的间隔。 */
export const RUN_POLL_INTERVAL_MS = 10_000

/** 心跳看门狗阈值：15 秒心跳 × 3。 */
const HEARTBEAT_TIMEOUT_MS = 45_000

/** 看门狗的检查间隔。 */
const WATCHDOG_INTERVAL_MS = 5_000

/** `end` 帧没带 reason 时的缺省文案。 */
export const DEFAULT_END_REASON = '服务器关闭连接'

export type RunLiveStatus = 'live' | 'disconnected' | 'resyncing' | 'ended'

/** 实时角标文案：视图只引用这一份词表。 */
export const RUN_LIVE_STATUS_LABELS: Record<RunLiveStatus, string> = {
  live: '实时',
  disconnected: '已停止实时',
  resyncing: '补发中',
  ended: '已结束',
}

export type RunLive = {
  status: RunLiveStatus
  /** 断线时刻的 HH:mm 文本；连接正常时为 null。 */
  since: string | null
  /** `end` 帧的关闭原因；未结束时为 null。 */
  endedReason: string | null
  reconnect: () => void
}

export type RunLiveOptions = {
  /** 每个业务事件与控制事件到达时回调；用于事件列表。 */
  onEvent?: (type: string, id?: string) => void
  /** 事件到达时失效的查询键前缀。缺省 ['run-page']。 */
  invalidateKey?: readonly unknown[]
  /** 合并高频业务事件的尾随防抖窗口（毫秒）。0 表示立即失效。 */
  coalesceMs?: number
  /** 自定义按事件类型的缓存失效逻辑；未提供时统一失效 `invalidateKey`。 */
  onInvalidate?: (eventTypes: readonly string[]) => void
}

type EventSourceLike = {
  close(): void
  onopen: ((event: Event) => void) | null
  onerror: ((event: Event) => void) | null
  addEventListener(type: string, listener: (event: MessageEvent) => void): void
}

/** 事件源工厂：返回 EventSource 的最小形状，便于测试替换。 */
export type EventSourceFactory = (url: string) => EventSourceLike

function defaultSourceFactory(url: string): EventSourceLike {
  return new EventSource(url)
}

function clockText(at: Date): string {
  return formatClock(at.toISOString())
}

function endReasonFrom(data: unknown): string {
  if (typeof data === 'string' && data !== '') {
    try {
      const parsed: unknown = JSON.parse(data)
      if (parsed !== null && typeof parsed === 'object') {
        const reason = (parsed as { reason?: unknown }).reason
        if (typeof reason === 'string' && reason.trim() !== '') return reason
      }
    } catch {
      // 非 JSON 的 data 不构成 reason，走缺省文案。
    }
  }
  return DEFAULT_END_REASON
}

export function useRunLive(
  queryClient: QueryClient,
  enabled: boolean,
  createSource: EventSourceFactory = defaultSourceFactory,
  options: RunLiveOptions = {},
): RunLive {
  const [status, setStatus] = useState<RunLiveStatus>('live')
  const [since, setSince] = useState<string | null>(null)
  const [endedReason, setEndedReason] = useState<string | null>(null)
  const [generation, setGeneration] = useState(0)
  const generationRef = useRef(generation)
  generationRef.current = generation
  const statusRef = useRef<RunLiveStatus>('live')
  const endedRef = useRef(false)
  const lastEventAtRef = useRef(0)
  const rearmWatchdogRef = useRef<() => void>(() => {})

  const updateStatus = useCallback((next: RunLiveStatus) => {
    statusRef.current = next
    setStatus(next)
  }, [])

  const onEventRef = useRef(options.onEvent)
  onEventRef.current = options.onEvent
  const invalidateKeyRef = useRef(options.invalidateKey)
  invalidateKeyRef.current = options.invalidateKey
  const onInvalidateRef = useRef(options.onInvalidate)
  onInvalidateRef.current = options.onInvalidate
  const coalesceMs = options.coalesceMs ?? 0

  const pendingTypesRef = useRef<Set<string>>(new Set())
  const coalesceTimerRef = useRef<number | null>(null)

  const flushInvalidation = useCallback(() => {
    coalesceTimerRef.current = null
    const types = [...pendingTypesRef.current]
    pendingTypesRef.current.clear()
    if (onInvalidateRef.current) {
      onInvalidateRef.current(types)
      return
    }
    void queryClient.invalidateQueries({
      queryKey: [...(invalidateKeyRef.current ?? ['run-page'])],
    })
  }, [queryClient])

  const invalidate = useCallback(
    (eventType = 'snapshot', immediate = false) => {
      pendingTypesRef.current.add(eventType)
      if (immediate || coalesceMs <= 0) {
        if (coalesceTimerRef.current !== null) {
          window.clearTimeout(coalesceTimerRef.current)
        }
        flushInvalidation()
        return
      }
      if (coalesceTimerRef.current !== null) return
      coalesceTimerRef.current = window.setTimeout(flushInvalidation, coalesceMs)
    },
    [coalesceMs, flushInvalidation],
  )

  useEffect(() => {
    const pendingTypes = pendingTypesRef.current
    return () => {
      if (coalesceTimerRef.current !== null) {
        window.clearTimeout(coalesceTimerRef.current)
        coalesceTimerRef.current = null
      }
      pendingTypes.clear()
    }
  }, [])

  const noteEvent = useCallback(() => {
    lastEventAtRef.current = Date.now()
    rearmWatchdogRef.current()
  }, [])

  const reconnect = useCallback(() => {
    endedRef.current = false
    setEndedReason(null)
    updateStatus('live')
    setSince(null)
    setGeneration((value) => value + 1)
    invalidate('reconnect', true)
  }, [invalidate, updateStatus])

  useEffect(() => {
    if (!enabled) return
    if (endedRef.current) return
    const thisGeneration = generationRef.current
    lastEventAtRef.current = Date.now()
    let source: EventSourceLike
    try {
      source = createSource('/api/admin/events')
    } catch {
      updateStatus('disconnected')
      setSince((value) => value ?? clockText(new Date(lastEventAtRef.current)))
      return
    }

    const accept = (): boolean => {
      if (generationRef.current !== thisGeneration) return false
      return !endedRef.current
    }

    const alive = () => {
      noteEvent()
      setSince(null)
      updateStatus('live')
    }

    source.onopen = () => {
      if (generationRef.current !== thisGeneration) return
      if (endedRef.current) return
      alive()
    }
    source.onerror = () => {
      if (generationRef.current !== thisGeneration) return
      if (endedRef.current) return
      updateStatus('disconnected')
      setSince((value) => value ?? clockText(new Date(lastEventAtRef.current)))
    }
    for (const name of REFRESH_EVENTS) {
      source.addEventListener(name, (event) => {
        if (!accept()) return
        alive()
        onEventRef.current?.(name, event.lastEventId || undefined)
        invalidate(name)
      })
    }
    for (const name of OBSERVE_EVENTS) {
      source.addEventListener(name, (event) => {
        if (!accept()) return
        alive()
        onEventRef.current?.(name, event.lastEventId || undefined)
      })
    }
    source.addEventListener('heartbeat', () => {
      if (!accept()) return
      alive()
    })
    source.addEventListener('resync', (event) => {
      if (!accept()) return
      noteEvent()
      setSince(null)
      updateStatus('resyncing')
      onEventRef.current?.('resync', event.lastEventId || undefined)
      invalidate('resync', true)
    })
    source.addEventListener('snapshot', (event) => {
      if (!accept()) return
      alive()
      onEventRef.current?.('snapshot', event.lastEventId || undefined)
      invalidate('snapshot', true)
    })
    source.addEventListener('end', (event) => {
      if (generationRef.current !== thisGeneration) return
      if (endedRef.current) return
      endedRef.current = true
      updateStatus('ended')
      setEndedReason(endReasonFrom(event.data))
      onEventRef.current?.('end', event.lastEventId || undefined)
      invalidate('end', true)
      source.close()
    })
    return () => {
      source.onopen = null
      source.onerror = null
      source.close()
    }
  }, [enabled, createSource, queryClient, generation, invalidate, noteEvent, updateStatus])

  useEffect(() => {
    if (!enabled) return
    let timer: number | null = null
    const arm = () => {
      if (timer !== null) window.clearInterval(timer)
      timer = window.setInterval(() => {
        if (endedRef.current) return
        const current = statusRef.current
        if (current !== 'live' && current !== 'resyncing') return
        if (Date.now() - lastEventAtRef.current < HEARTBEAT_TIMEOUT_MS) return
        updateStatus('disconnected')
        setSince((value) => value ?? clockText(new Date(lastEventAtRef.current)))
      }, WATCHDOG_INTERVAL_MS)
    }
    rearmWatchdogRef.current = arm
    lastEventAtRef.current = Date.now()
    arm()
    return () => {
      rearmWatchdogRef.current = () => {}
      if (timer !== null) window.clearInterval(timer)
    }
  }, [enabled, updateStatus])

  return { status, since, endedReason, reconnect }
}
