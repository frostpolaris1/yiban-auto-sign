/**
 * **功能**
 * 运行域的实时刷新钩子：SSE 到达即失效查询，断线出横幅并可重连。
 *
 * **归属**
 * `features/run` 域钩子：不渲染，只管连接与查询失效。
 * 运行页与执行监控页（UX-5）共用同一条连接纪律。
 *
 * **口径**
 * 复用 `GET /api/admin/events`，不新增事件类型。
 * `task.progress`、`task.concluded`、`window.changed` 触发刷新；
 * `capacity.changed`、`egress.health` 只进事件展示（onEvent），不触发刷新。
 * 每个 MessageEvent 的 lastEventId 记入 `lastEventId`（空串不覆盖），
 * 事件列表据此证明续传位置。
 * `heartbeat` 只算「流还活着」：回 live、更新最近时刻，不进 onEvent、不刷新。
 * `resync` 进补发态并刷新；`snapshot` 回实时并刷新；两者都进 onEvent。
 * `end` 进结束态、记 reason 并关闭 source，阻止 EventSource 自动重连；
 * 结束后的 onerror 不得把状态改回断线。
 * 自动重连由浏览器 EventSource 负责（带 Last-Event-ID，服务端补发）；
 * 钩子不因 onerror 主动关闭 source。
 * 心跳看门狗：45 秒（15 秒心跳 × 3）没有任意事件就把 live/resyncing
 * 降为 disconnected，但不关闭 source（浏览器可能自愈）；事件一到即回 live。
 * 手动重连是换代：新连接没有 Last-Event-ID，等于全量重同步，
 * 因此立即失效查询走 REST，并清除断线时刻。
 * 断线期间每 10 秒轮询一次（降级，由调用方执行）。
 * 卸载即关闭连接，不留下孤儿流。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { QueryClient } from '@tanstack/react-query'
import { formatClock } from '@/lib/format'

/** 触发刷新的事件名集合。与契约 `/api/admin/events` 的词表一致。 */
const REFRESH_EVENTS = ['task.progress', 'task.concluded', 'window.changed'] as const

/** 只进事件展示、不触发刷新的业务事件名。 */
const OBSERVE_EVENTS = ['capacity.changed', 'egress.health'] as const

/** 断线降级轮询的间隔。 */
export const RUN_POLL_INTERVAL_MS = 10000

/** 心跳看门狗阈值：15 秒心跳 × 3。 */
const HEARTBEAT_TIMEOUT_MS = 45_000

/** 看门狗的检查间隔。 */
const WATCHDOG_INTERVAL_MS = 5_000

/** `end` 帧没带 reason 时的缺省文案。 */
const DEFAULT_END_REASON = '服务器关闭连接'

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
  /** 最近一个带 id 的事件位置；从未收到时为 null。 */
  lastEventId: string | null
  /** `end` 帧的关闭原因；未结束时为 null。 */
  endedReason: string | null
  reconnect: () => void
}

export type RunLiveOptions = {
  /** 每个业务事件（含不触发刷新的五类）与控制事件到达时回调；用于事件列表。 */
  onEvent?: (type: string, id?: string) => void
  /** 事件到达时失效的查询键前缀。运行页缺省 ['run-page']，监控页给 ['engine-page']。 */
  invalidateKey?: readonly unknown[]
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

/** HH:mm 文本（上海墙钟，与页面其余时间同口径）。断线横幅展示「最近更新」。 */
function clockText(at: Date): string {
  return formatClock(at.toISOString())
}

/** 解析 `end` 帧 data 里的 reason；缺省或非字符串一律用缺省文案。 */
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
  const [lastEventId, setLastEventId] = useState<string | null>(null)
  const [endedReason, setEndedReason] = useState<string | null>(null)
  // 连接代数：重连时加一。它在 effect 依赖里，重连即重建连接。
  const [generation, setGeneration] = useState(0)
  const generationRef = useRef(generation)
  generationRef.current = generation
  // 状态同时进 ref：事件回调与看门狗读到的是最新值，不是闭包快照。
  const statusRef = useRef<RunLiveStatus>('live')
  const endedRef = useRef(false)
  // 最近任意事件（含心跳）的时刻；看门狗只看它。
  const lastEventAtRef = useRef(0)
  const updateStatus = useCallback((next: RunLiveStatus) => {
    statusRef.current = next
    setStatus(next)
  }, [])
  const onEventRef = useRef(options.onEvent)
  onEventRef.current = options.onEvent
  // 失效键经 ref 读取：调用方常以字面量传入，进依赖会让连接每渲染重建。
  const invalidateKeyRef = useRef(options.invalidateKey)
  invalidateKeyRef.current = options.invalidateKey

  const invalidate = useCallback(() => {
    void queryClient.invalidateQueries({
      queryKey: [...(invalidateKeyRef.current ?? ['run-page'])],
    })
  }, [queryClient])

  const reconnect = useCallback(() => {
    endedRef.current = false
    setEndedReason(null)
    updateStatus('live')
    setSince(null)
    setGeneration((value) => value + 1)
    // 新连接不带 Last-Event-ID：服务端不会补发，必须走 REST 拿全量。
    invalidate()
  }, [invalidate, updateStatus])

  useEffect(() => {
    if (!enabled) return
    const thisGeneration = generationRef.current
    lastEventAtRef.current = Date.now()
    let source: EventSourceLike
    try {
      source = createSource('/api/admin/events')
    } catch {
      updateStatus('disconnected')
      setSince((value) => value ?? clockText(new Date()))
      return
    }

    /** 事件通用前缀：代数守卫、记录续传位置、过滤结束后的迟到帧。 */
    const accept = (event: MessageEvent): boolean => {
      if (generationRef.current !== thisGeneration) return false
      if (event.lastEventId) setLastEventId(event.lastEventId)
      return !endedRef.current
    }

    /** 任意事件都证明流还活着：更新时刻、清断线时刻、回实时。 */
    const alive = () => {
      lastEventAtRef.current = Date.now()
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
      // 结束后的关闭不回落成断线；其余情况保留 source，让浏览器自动重连。
      if (endedRef.current) return
      updateStatus('disconnected')
      setSince((value) => value ?? clockText(new Date()))
    }
    for (const name of REFRESH_EVENTS) {
      source.addEventListener(name, (event) => {
        if (!accept(event)) return
        alive()
        onEventRef.current?.(name, event.lastEventId || undefined)
        invalidate()
      })
    }
    for (const name of OBSERVE_EVENTS) {
      source.addEventListener(name, (event) => {
        if (!accept(event)) return
        alive()
        onEventRef.current?.(name, event.lastEventId || undefined)
      })
    }
    // 心跳只续命：不进事件列表、不触发查询失效。
    source.addEventListener('heartbeat', (event) => {
      if (!accept(event)) return
      alive()
    })
    source.addEventListener('resync', (event) => {
      if (!accept(event)) return
      lastEventAtRef.current = Date.now()
      setSince(null)
      updateStatus('resyncing')
      onEventRef.current?.('resync', event.lastEventId || undefined)
      invalidate()
    })
    source.addEventListener('snapshot', (event) => {
      if (!accept(event)) return
      alive()
      onEventRef.current?.('snapshot', event.lastEventId || undefined)
      invalidate()
    })
    source.addEventListener('end', (event) => {
      if (generationRef.current !== thisGeneration) return
      if (event.lastEventId) setLastEventId(event.lastEventId)
      if (endedRef.current) return
      endedRef.current = true
      updateStatus('ended')
      setEndedReason(endReasonFrom(event.data))
      onEventRef.current?.('end', event.lastEventId || undefined)
      invalidate()
      // 服务端已经结束这条流；不关闭的话 EventSource 会自动重连。
      source.close()
    })
    return () => {
      source.onopen = null
      source.onerror = null
      source.close()
    }
  }, [enabled, createSource, queryClient, generation, invalidate, updateStatus])

  // 心跳看门狗：任意事件超过 45 秒没来，就把在线的两种状态降为断线。
  // 不关闭 source：浏览器自愈（带 Last-Event-ID 自动重连）仍有机会救回。
  useEffect(() => {
    if (!enabled) return
    lastEventAtRef.current = Date.now()
    const timer = window.setInterval(() => {
      if (endedRef.current) return
      const current = statusRef.current
      if (current !== 'live' && current !== 'resyncing') return
      if (Date.now() - lastEventAtRef.current < HEARTBEAT_TIMEOUT_MS) return
      updateStatus('disconnected')
      setSince((value) => value ?? clockText(new Date()))
    }, WATCHDOG_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [enabled, updateStatus])

  return { status, since, lastEventId, endedReason, reconnect }
}
