/**
 * **功能**
 * 计划预览模态：打开即读预览，展示有效窗口、三个数字、直方图与逐行计划。
 *
 * **归属**
 * `features/schedule` 展示层：取数在 `ScheduleView`，本件只排版。
 *
 * **口径**
 * 预览只读：本件不发请求。两个写动作的确认由调用方管。
 * 停用任务照样可预览，但「落库此计划」「写入覆盖」置灰并给原因。
 * 展示构建只读：两个写按钮始终置灰，原因写在正文顶部。
 * 窗口版本 `v0` 表示纯预览；覆盖重建后版本非零。
 * 三个数字用滚动组件渲染，颜色继承 `app-metric-*` 容器。
 */

import type { SchedulePlan, ScheduleWindow } from '@/lib/api/schedule'
import { Alert } from '@/components/ui/Alert'
import { AnimatedNumber } from '@/components/ui/AnimatedNumber'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Modal } from '@/components/ui/Modal'
import { formatDateTime } from '@/lib/format'
import { READ_ONLY_REASON, SHOWCASE } from '@/lib/showcase-mode'
import { histogramBars } from './drafts'
import { sourceLabel, sourceTone } from './display'
import { ScheduleHistogram } from './ScheduleHistogram'

type SchedulePreviewModalProps = {
  target: ScheduleWindow | null
  day: string
  plan: SchedulePlan | undefined
  pending: boolean
  error: string | null
  returnFocusTo: HTMLElement | null
  onClose: () => void
  onRetry: () => void
  onOverride: (opener: HTMLElement) => void
  onBuild: (opener: HTMLElement) => void
}

export function SchedulePreviewModal({
  target,
  day,
  plan,
  pending,
  error,
  returnFocusTo,
  onClose,
  onRetry,
  onOverride,
  onBuild,
}: SchedulePreviewModalProps) {
  const readOnlyReason = SHOWCASE ? READ_ONLY_REASON : null
  const writable = plan?.enabled === true && !pending && readOnlyReason === null
  const denyTitle = readOnlyReason ?? '任务已停用，无法落库或覆盖'

  return (
    <Modal
      open={target !== null}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
      title="计划预览"
      scrollBody
      description={
        target ? `${target.school_name} · ${target.task_name} · ${day}（只读预览，不写库）` : undefined
      }
      returnFocusTo={returnFocusTo}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            关闭
          </Button>
          <Button
            variant="secondary"
            disabled={!writable}
            title={writable ? undefined : denyTitle}
            onClick={(event) => onOverride(event.currentTarget)}
          >
            写入覆盖
          </Button>
          <Button
            variant="primary"
            disabled={!writable}
            title={writable ? undefined : denyTitle}
            onClick={(event) => onBuild(event.currentTarget)}
          >
            落库此计划
          </Button>
        </>
      }
    >
      {readOnlyReason ? (
        <p>
          <span className="app-deny-note">{readOnlyReason}</span>
        </p>
      ) : null}

      {pending && !plan ? <p className="app-muted">正在取计划…</p> : null}

      {error ? (
        <div className="app-modal-error">
          <Alert tone="danger" title="预览失败">
            {error}
          </Alert>
          <div className="app-plan-retry">
            <Button variant="ghost" size="sm" onClick={onRetry}>
              重试
            </Button>
          </div>
        </div>
      ) : null}

      {plan ? (
        <>
          {plan.enabled ? null : (
            <div className="app-alert-block">
              <Alert tone="warning" title="任务已停用">
                任务已停用，落库与覆盖将被拒绝。
              </Alert>
            </div>
          )}

          <dl className="app-detail-list">
            <dt>日期</dt>
            <dd className="data-cell-mono">{plan.day}</dd>
            <dt>来源</dt>
            <dd>
              <Badge tone={sourceTone(plan.source)}>{sourceLabel(plan.source)}</Badge>
            </dd>
            <dt>生效窗口</dt>
            <dd className="data-cell-mono">
              {formatDateTime(plan.window_start)} – {formatDateTime(plan.window_end)}
            </dd>
            <dt>窗口版本</dt>
            <dd className="data-cell-mono">v{plan.version}</dd>
          </dl>

          <div className="app-metric-grid">
            <div className="app-metric">
              <span className="app-metric-label">容量</span>
              <span className="app-metric-value">
                <AnimatedNumber value={plan.capacity} />
              </span>
            </div>
            <div className="app-metric">
              <span className="app-metric-label">需求</span>
              <span className="app-metric-value">
                <AnimatedNumber value={plan.demand} />
              </span>
            </div>
            <div className="app-metric">
              <span className="app-metric-label">缺口</span>
              <span
                className={`app-metric-value${plan.shortfall > 0 ? ' app-metric-alert' : ''}`}
              >
                <AnimatedNumber value={plan.shortfall} />
              </span>
            </div>
          </div>

          <ScheduleHistogram bars={histogramBars(plan.histogram)} />

          <div className="app-plan-rows">
            <table className="data-table app-row-table">
              <caption className="app-visually-hidden">计划行</caption>
              <thead>
                <tr>
                  <th scope="col">账号</th>
                  <th scope="col">执行时间</th>
                </tr>
              </thead>
              <tbody>
                {plan.rows.map((row) => (
                  <tr key={row.account_id}>
                    <td className="data-cell-mono">{row.account_id}</td>
                    <td className="data-cell-mono">{formatDateTime(row.run_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </Modal>
  )
}
