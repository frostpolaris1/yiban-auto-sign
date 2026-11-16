/**
 * **功能**
 * 队列对象诊断抽屉：查看账号状态、当日任务执行详情、系统建议、窗口与出口，带双向滑入滑出动效。
 *
 * **归属**
 * `features/queue` 域组件：调用 `GET /api/accounts/{id}/diagnostics`，只读不写。
 *
 * **口径**
 * 跨校或已删除账号返回 404 时提示「账号不可达」，不再重试。
 * 展示模式读取合成诊断夹具，零网络请求。
 */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AsyncState } from '@/components/ui/AsyncState'
import { Badge } from '@/components/ui/Badge'
import { Drawer } from '@/components/ui/Drawer'
import { ApiError, describeApiError } from '@/lib/api/client'
import { getAccountDiagnostics } from '@/lib/api/task'
import { formatDateTime } from '@/lib/format'
import { SHOWCASE } from '@/lib/session'
import { showcaseQueueDiagnostics } from '@/lib/showcase'
import {
  accountStatusMeta,
  diagnosticsSuggestionMeta,
  taskStateMeta,
} from './queueDisplay'
import { usePresence } from './usePresence'

export type QueueDiagnosticsTarget = {
  accountId: string
  title: string
  subtitle: string
}

type QueueDiagnosticsDrawerProps = {
  target: QueueDiagnosticsTarget | null
  returnFocusTo: HTMLElement | null
  onClose: () => void
}

export function QueueDiagnosticsDrawer({
  target,
  returnFocusTo,
  onClose,
}: QueueDiagnosticsDrawerProps) {
  const [snapshot, setSnapshot] = useState<QueueDiagnosticsTarget | null>(null)
  const { mounted, closing } = usePresence(target !== null, 180)

  if (target !== null && snapshot?.accountId !== target.accountId) {
    setSnapshot(target)
  }

  const activeTarget = target ?? snapshot

  const query = useQuery({
    queryKey: ['queue-diagnostics', activeTarget?.accountId ?? null],
    queryFn: () => {
      const id = activeTarget?.accountId ?? ''
      if (SHOWCASE) return Promise.resolve(showcaseQueueDiagnostics(id))
      return getAccountDiagnostics(id)
    },
    enabled: target !== null,
    staleTime: 0,
    retry: false,
  })

  const suggestion = query.data ? diagnosticsSuggestionMeta(query.data.suggestion) : null
  const accMeta = query.data ? accountStatusMeta(query.data.account_status) : null
  const taskMeta = query.data?.state ? taskStateMeta(query.data.state) : null

  return (
    <Drawer
      open={mounted}
      closing={closing}
      className="q-drawer-anim"
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
      title={activeTarget ? `诊断 · ${activeTarget.title}` : '诊断'}
      description={activeTarget?.subtitle}
      returnFocusTo={returnFocusTo}
    >
      {activeTarget === null ? null : query.isError ? (
        <AsyncState
          status="error"
          children={null}
          error={
            query.error instanceof ApiError && query.error.status === 404
              ? '账号不可达：可能已删除或不在你的范围。'
              : describeApiError(query.error)
          }
          onRetry={
            query.error instanceof ApiError && query.error.status === 404
              ? undefined
              : () => {
                  void query.refetch()
                }
          }
        />
      ) : query.isPending ? (
        <AsyncState status="loading" children={null} />
      ) : (
        <div>
          {suggestion && accMeta ? (
            <div className="q-diag-summary">
              <div className="q-diag-badges">
                <Badge tone={accMeta.tone}>账号：{accMeta.label}</Badge>
                {taskMeta ? (
                  <Badge tone={taskMeta.tone} dot>
                    任务：{taskMeta.label}
                  </Badge>
                ) : (
                  <Badge tone="info">今日无任务</Badge>
                )}
                <Badge tone={suggestion.tone}>建议：{suggestion.label}</Badge>
              </div>
              <p className="q-diag-suggestion-desc">{suggestion.description}</p>
            </div>
          ) : null}

          <dl className="q-diag-dl">
            <div className="q-diag-item">
              <dt>账号备注</dt>
              <dd>{query.data.display_name}</dd>
            </div>
            <div className="q-diag-item">
              <dt>手机号</dt>
              <dd>{query.data.phone_masked}</dd>
            </div>
            <div className="q-diag-item">
              <dt>计划时刻</dt>
              <dd>{formatDateTime(query.data.run_at)}</dd>
            </div>
            <div className="q-diag-item">
              <dt>尝试次数</dt>
              <dd>{query.data.attempts}</dd>
            </div>
            <div className="q-diag-item">
              <dt>最近结果</dt>
              <dd className="q-mono-code">
                {query.data.result === '' ? '—' : query.data.result}
              </dd>
            </div>
            <div className="q-diag-item">
              <dt>下次重试</dt>
              <dd>{formatDateTime(query.data.next_retry_at)}</dd>
            </div>
            {query.data.window ? (
              <div className="q-diag-item">
                <dt>当日窗口</dt>
                <dd>
                  {formatDateTime(query.data.window.start)} —{' '}
                  {formatDateTime(query.data.window.end)}（{query.data.window.source}）
                </dd>
              </div>
            ) : null}
            {query.data.egress ? (
              <div className="q-diag-item">
                <dt>出口标识</dt>
                <dd className="q-mono-code">{query.data.egress.identity}</dd>
              </div>
            ) : null}
          </dl>
        </div>
      )}
    </Drawer>
  )
}
