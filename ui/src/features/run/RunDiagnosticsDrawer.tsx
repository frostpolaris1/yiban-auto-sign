/**
 * **功能**
 * 行诊断抽屉：调单账号诊断端点，展示状态、建议与窗口。
 *
 * **归属**
 * `features/run` 域组件：只取单账号诊断，不改数据。
 *
 * **口径**
 * 数据来自 `GET /api/accounts/{id}/diagnostics`（lib/api/task）。
 * 加载、错误、数据三态分开：失败不渲染成「暂无」。
 * 跨校或账号已删回 404：文案给出「账号不可达」，不再重试同一 id。
 * 组件不 import 其它功能目录；诊断类型取生成的 schema。
 */

import { useQuery } from '@tanstack/react-query'
import { AsyncState } from '@/components/ui/AsyncState'
import { Badge } from '@/components/ui/Badge'
import { Drawer } from '@/components/ui/Drawer'
import { ApiError, describeApiError } from '@/lib/api/client'
import { getAccountDiagnostics, type AdminTaskRow } from '@/lib/api/task'
import { formatDateTime } from '@/lib/format'
import { runStateLabel, runStateTone } from './display'

type RunDiagnosticsDrawerProps = {
  row: AdminTaskRow | null
  returnFocusTo: HTMLElement | null
  onClose: () => void
}

const SUGGESTION_LABELS: Record<string, string> = {
  wait: '等待窗口',
  retry_now: '可以立即重试',
  re_arm: '需要重新布点',
  contact_admin: '联系管理员',
}

export function RunDiagnosticsDrawer({ row, returnFocusTo, onClose }: RunDiagnosticsDrawerProps) {
  const query = useQuery({
    queryKey: ['run-diagnostics', row?.account_id ?? null],
    queryFn: () => getAccountDiagnostics(row?.account_id ?? ''),
    enabled: row !== null,
    // 抽屉每次打开都要新数：任务状态随时在变。
    staleTime: 0,
    retry: false,
  })

  return (
    <Drawer
      open={row !== null}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
      title={row ? `诊断 · ${row.account_phone_masked}` : '诊断'}
      description={row ? `${row.school_name} · ${row.task_name}` : undefined}
      returnFocusTo={returnFocusTo}
    >
      {row === null ? null : query.isError ? (
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
        <dl className="run-diag">
          <div>
            <dt>当前状态</dt>
            <dd>
              <Badge tone={runStateTone(query.data.state ?? '')}>
                {query.data.state === null ? '今日无任务' : runStateLabel(query.data.state)}
              </Badge>
            </dd>
          </div>
          <div>
            <dt>计划时刻</dt>
            <dd>{formatDateTime(query.data.run_at)}</dd>
          </div>
          <div>
            <dt>尝试次数</dt>
            <dd>{query.data.attempts}</dd>
          </div>
          <div>
            <dt>结果</dt>
            <dd>{query.data.result === '' ? '—' : query.data.result}</dd>
          </div>
          <div>
            <dt>下次重试</dt>
            <dd>{formatDateTime(query.data.next_retry_at)}</dd>
          </div>
          <div>
            <dt>建议</dt>
            <dd>{SUGGESTION_LABELS[query.data.suggestion] ?? query.data.suggestion}</dd>
          </div>
          {query.data.window ? (
            <div>
              <dt>当日窗口</dt>
              <dd>
                {formatDateTime(query.data.window.start)} — {formatDateTime(query.data.window.end)}
                （{query.data.window.source}）
              </dd>
            </div>
          ) : null}
          {query.data.egress ? (
            <div>
              <dt>出口</dt>
              <dd>{query.data.egress.identity}</dd>
            </div>
          ) : null}
        </dl>
      )}
    </Drawer>
  )
}
