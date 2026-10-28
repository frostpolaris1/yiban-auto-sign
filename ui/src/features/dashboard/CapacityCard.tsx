/**
 * **功能**
 * 容量体检卡：任务选择器、容量／需求／缺口三个指标，来源与窗口起止。
 *
 * **归属**
 * `features/dashboard` 展示层：数据与回调由 `DashboardView` 给。
 *
 * **口径**
 * 端点 `GET /api/capacity` 的 `x-roles` 是 master/admin；本卡不自行取数。
 * 选项只取「启用且起止时间都非 null」的今日窗口；默认选第一项。
 * `shortfall>0` 用 danger 徽章；其余用 success。
 * 窗口失败与容量失败各有错误态与「重试」，不整页挂。
 * 无可用任务时只说「今日无可用窗口」，不发容量请求。
 */

import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { SelectInput } from '@/components/ui/Input'
import type { CapacityReport } from '@/lib/api/schedule'
import { formatClock, formatNumber } from '@/lib/format'
import {
  EMPTY_WINDOW_TEXT,
  type CapacityOption,
  capacitySourceLabel,
  capacitySourceTone,
} from './display'

type CapacityCardProps = {
  options: CapacityOption[]
  selectedValue: string
  onSelect: (value: string) => void
  report: CapacityReport | undefined
  windowsLoading: boolean
  windowsError: string | null
  capacityLoading: boolean
  capacityError: string | null
  onRetryWindows: () => void
  onRetryCapacity: () => void
}

export function CapacityCard({
  options,
  selectedValue,
  onSelect,
  report,
  windowsLoading,
  windowsError,
  capacityLoading,
  capacityError,
  onRetryWindows,
  onRetryCapacity,
}: CapacityCardProps) {
  return (
    <Card
      className="col-12"
      eyebrow="Capacity"
      title="容量体检"
      action={<span className="app-caption">缺省读当日窗口缓存</span>}
    >
      {windowsError !== null ? (
        <div className="app-alert-block">
          <Alert tone="danger" title="读取失败">
            {windowsError}{' '}
            <Button variant="ghost" size="sm" onClick={onRetryWindows}>
              重试
            </Button>
          </Alert>
        </div>
      ) : windowsLoading && options.length === 0 ? (
        <p className="app-muted">加载中…</p>
      ) : options.length === 0 ? (
        <p className="app-muted">{EMPTY_WINDOW_TEXT}</p>
      ) : (
        <>
          <div className="app-schedule-toolbar">
            <SelectInput
              id="capacity-task"
              label="任务"
              options={options.map((option) => ({ value: option.value, label: option.label }))}
              value={selectedValue}
              disabled={capacityLoading}
              onChange={(event) => onSelect(event.target.value)}
            />
          </div>

          {capacityError !== null ? (
            <div className="app-alert-block">
              <Alert tone="danger" title="读取失败">
                {capacityError}{' '}
                <Button variant="ghost" size="sm" onClick={onRetryCapacity}>
                  重试
                </Button>
              </Alert>
            </div>
          ) : report === undefined ? (
            <p className="app-muted">加载中…</p>
          ) : (
            <>
              <div className="app-metric-grid">
                <div className="app-metric">
                  <div className="app-metric-label">容量</div>
                  <div className="app-metric-value">{formatNumber(report.capacity)}</div>
                </div>
                <div className="app-metric">
                  <div className="app-metric-label">需求</div>
                  <div className="app-metric-value">{formatNumber(report.demand)}</div>
                </div>
                <div className="app-metric">
                  <div className="app-metric-label">缺口</div>
                  <div className="app-metric-value">
                    <Badge tone={report.shortfall > 0 ? 'danger' : 'success'}>
                      {formatNumber(report.shortfall)}
                    </Badge>
                  </div>
                </div>
              </div>

              <p className="app-muted app-capacity-meta">
                <Badge tone={capacitySourceTone(report.source)}>
                  {capacitySourceLabel(report.source)}
                </Badge>{' '}
                窗口 {formatClock(report.window_start)}–{formatClock(report.window_end)} · 业务日{' '}
                {report.day}
              </p>
            </>
          )}
        </>
      )}
    </Card>
  )
}
