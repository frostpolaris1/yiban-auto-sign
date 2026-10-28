/**
 * **功能**
 * 签到诊断卡：每账号的状态、尝试次数、结果、下次重试与建议。
 *
 * **归属**
 * `features/dashboard` 展示层：数据与回调由 `DashboardView` 给。
 *
 * **口径**
 * 端点 `GET /api/me/diagnostics` 对 user 与 admin 开放；看板只对 user 挂载。
 * 建议是契约枚举，中文标签四值穷尽；未知值不会出现（类型层保证）。
 * 空 `result` 与空时间显示 `—`。
 * 空列表与签到状态卡同一句空态文案。
 */

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import type { DiagnosticsItem } from '@/lib/api/me'
import { formatClock } from '@/lib/format'
import { AccountLabel } from './AccountLabel'
import {
  EMPTY_SIGN_TEXT,
  SUGGESTION_LABELS,
  SUGGESTION_TONES,
  stateLabel,
  stateTone,
} from './display'

type DiagnosticsCardProps = {
  items: readonly DiagnosticsItem[]
  loading: boolean
  error: string | null
  onRetry: () => void
}

const COLUMNS = 6

export function DiagnosticsCard({ items, loading, error, onRetry }: DiagnosticsCardProps) {
  return (
    <Card
      className="col-6"
      eyebrow="Diagnostics"
      title="签到诊断"
      action={<span className="app-caption">仅本人账号</span>}
    >
      <div className="table-scroll">
        <table className="table">
          <caption className="app-visually-hidden">签到诊断</caption>
          <thead>
            <tr>
              <th scope="col">账号</th>
              <th scope="col">状态</th>
              <th scope="col">尝试</th>
              <th scope="col">结果</th>
              <th scope="col">下次重试</th>
              <th scope="col">建议</th>
            </tr>
          </thead>
          <tbody>
            {error ? (
              <tr>
                <td colSpan={COLUMNS}>
                  <span className="app-muted">{error}</span>{' '}
                  <Button variant="ghost" size="sm" onClick={onRetry}>
                    重试
                  </Button>
                </td>
              </tr>
            ) : null}

            {!error && loading && items.length === 0 ? (
              <tr>
                <td colSpan={COLUMNS} className="app-muted">
                  加载中…
                </td>
              </tr>
            ) : null}

            {!error && !loading && items.length === 0 ? (
              <tr>
                <td colSpan={COLUMNS} className="app-muted">
                  {EMPTY_SIGN_TEXT}
                </td>
              </tr>
            ) : null}

            {items.map((item, index) => (
              <tr key={item.account_id}>
                <td className="cell-name">
                  <AccountLabel accountId={item.account_id} index={index} />
                </td>
                <td>
                  <Badge tone={stateTone(item.state)}>{stateLabel(item.state)}</Badge>
                </td>
                <td className="data-cell-mono">{`${item.attempts} 次`}</td>
                <td>{item.result === '' ? '—' : item.result}</td>
                <td className="data-cell-mono">{formatClock(item.next_retry_at)}</td>
                <td>
                  <Badge tone={SUGGESTION_TONES[item.suggestion]}>
                    {SUGGESTION_LABELS[item.suggestion]}
                  </Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}
