/**
 * **功能**
 * 今日签到状态卡：每账号一行，状态徽章、时刻与过期标记。
 *
 * **归属**
 * `features/dashboard` 展示层：数据与回调由 `DashboardView` 给。
 *
 * **口径**
 * 端点 `GET /api/me/sign-status` 只对 user 开放；本卡不自行取数。
 * 账号只有 `account_id`，没有掩码手机号：主行显示序号，副行显示短 id。
 * `stale=true` 的行在时刻后加「数据已过期」徽章。
 * 空列表说「今日暂无签到任务」，不渲染空白表。
 * 状态标签与色取契约词汇，未知值原样显示。
 */

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import type { SignStatusItem } from '@/lib/api/me'
import { formatClock } from '@/lib/format'
import { AccountLabel } from './AccountLabel'
import { EMPTY_SIGN_TEXT, STALE_TEXT, stateLabel, stateTone } from './display'

type SignStatusCardProps = {
  items: readonly SignStatusItem[]
  updatedAt: string | null
  loading: boolean
  error: string | null
  onRetry: () => void
}

const COLUMNS = 3

export function SignStatusCard({
  items,
  updatedAt,
  loading,
  error,
  onRetry,
}: SignStatusCardProps) {
  return (
    <Card
      className="col-12"
      eyebrow="Status"
      title="今日签到状态"
      action={
        <span className="app-caption">
          {updatedAt === null ? '每 30 秒刷新' : `更新于 ${formatClock(updatedAt)}`}
        </span>
      }
    >
      <div className="table-scroll">
        <table className="table">
          <caption className="app-visually-hidden">今日签到状态</caption>
          <thead>
            <tr>
              <th scope="col">账号</th>
              <th scope="col">状态</th>
              <th scope="col">时刻</th>
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
                <td>
                  <span className="data-cell-mono">{formatClock(item.run_at)}</span>
                  {item.stale ? (
                    <>
                      {' '}
                      <Badge tone="warning">{STALE_TEXT}</Badge>
                    </>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}
