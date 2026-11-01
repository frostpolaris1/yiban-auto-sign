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
 * `capacity.changed`、`egress.health` 只进事件展示（onEvent），不触发刷新；
 * 其余事件（心跳、resync、end、snapshot）两者都不参与。
 * EventSource 可注入（参数 `createSource`），Playwright 以替换
 * `window.EventSource` 注入假体，组件代码不感知。
 * 重连是换代：代数进 effect 依赖，重连即拆旧连接、建新连接。
 * 打开即清除断线标记；`onerror` 记断线时刻。
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

export type RunLiveStatus = 'live' | 'disconnected'

export type RunLive = {
  status: RunLiveStatus
  /** 断线时刻的 HH:mm 文本；连接正常时为 null。 */
  since: string | null
  reconnect: () => void
}

export type RunLiveOptions = {
  /** 每个业务事件（含不触发刷新的五类）到达时回调；用于事件列表。 */
  onEvent?: (type: string) => void
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

export function useRunLive(
  queryClient: QueryClient,
  enabled: boolean,
  createSource: EventSourceFactory = defaultSourceFactory,
  options: RunLiveOptions = {},
): RunLive {
  const [status, setStatus] = useState<RunLiveStatus>('live')
  const [since, setSince] = useState<string | null>(null)
  // 连接代数：重连时加一。它在 effect 依赖里，重连即重建连接。
  const [generation, setGeneration] = useState(0)
  const generationRef = useRef(generation)
  generationRef.current = generation
  const onEventRef = useRef(options.onEvent)
  onEventRef.current = options.onEvent
  // 失效键经 ref 读取：调用方常以字面量传入，进依赖会让连接每渲染重建。
  const invalidateKeyRef = useRef(options.invalidateKey)
  invalidateKeyRef.current = options.invalidateKey

  const reconnect = useCallback(() => {
    setGeneration((value) => value + 1)
    setStatus('live')
    setSince(null)
  }, [])

  useEffect(() => {
    if (!enabled) return
    const thisGeneration = generationRef.current
    let source: EventSourceLike
    try {
      source = createSource('/api/admin/events')
    } catch {
      setStatus('disconnected')
      setSince((value) => value ?? clockText(new Date()))
      return
    }
    source.onopen = () => {
      if (generationRef.current !== thisGeneration) return
      setStatus('live')
      setSince(null)
    }
    source.onerror = () => {
      if (generationRef.current !== thisGeneration) return
      setStatus('disconnected')
      setSince((value) => value ?? clockText(new Date()))
    }
    for (const name of REFRESH_EVENTS) {
      source.addEventListener(name, () => {
        if (generationRef.current !== thisGeneration) return
        onEventRef.current?.(name)
        void queryClient.invalidateQueries({ queryKey: [...(invalidateKeyRef.current ?? ['run-page'])] })
      })
    }
    for (const name of OBSERVE_EVENTS) {
      source.addEventListener(name, () => {
        if (generationRef.current !== thisGeneration) return
        onEventRef.current?.(name)
      })
    }
    return () => {
      source.onopen = null
      source.onerror = null
      source.close()
    }
  }, [enabled, createSource, queryClient, generation])

  return { status, since, reconnect }
}
