/**
 * **功能**
 * 容量体检卡：任务选择器、负载进度条、容量／需求／缺口三指标、来源与窗口起止。
 *
 * **归属**
 * `features/dashboard` 展示层：数据与回调由 `DashboardView` 给。
 *
 * **口径**
 * 端点 `GET /api/capacity` 的 `x-roles` 是 master/admin；本卡不自行取数。
 * 选项只取「启用且起止时间都非 null」的今日窗口；默认选第一项。
 * 今日窗口计数（总数/已报/停用/可用）以注脚呈现：窗口数据到了就有，
 * 与容量报告成败无关；窗口读不到就没有注脚。
 * `shortfall>0` 用 danger 徽章；其余用 success。
 * 三个数字用滚动组件渲染，颜色继承所在容器（正文色或徽章色）。
 * 缺口为正时给一条可执行建议：通道瓶颈给 YIBAN_WORKERS 数值与复制按钮，
 * 出口瓶颈只指向 YIBAN_EGRESS_RATE 或增加出口标识，不给数字。
 * 建议块只在 `shortfall>0` 时出现；无缺口不渲染。
 * 复制失败时在建议块内留可见提示，配置行本身可手选复制。
 * 窗口失败与容量失败各有错误态与「重试」，不整页挂。
 * 无可用任务时只说「今日无可用窗口」，不发容量请求。
 */

import { useState } from 'react'
import { Alert } from '@/components/ui/Alert'
import { AnimatedNumber } from '@/components/ui/AnimatedNumber'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { Select } from '@/components/ui/Select'
import { notifySuccess } from '@/components/ui/toast'
import type { CapacityReport } from '@/lib/api/schedule'
import { formatClock } from '@/lib/format'
import {
  EMPTY_WINDOW_TEXT,
  type CapacityOption,
  capacityMeter,
  capacitySourceLabel,
  capacitySourceTone,
  type WindowCounts,
} from './display'

type CapacityCardProps = {
  options: CapacityOption[]
  selectedValue: string
  onSelect: (value: string) => void
  report: CapacityReport | undefined
  /** 今日窗口计数注脚：原窗口 KPI 并入本卡，窗口读不到时为 null（无注脚）。 */
  counts: WindowCounts | null
  windowsLoading: boolean
  windowsError: string | null
  capacityLoading: boolean
  capacityError: string | null
  onRetryWindows: () => void
  onRetryCapacity: () => void
}

/** 复制配置行；成功发 toast，失败交给建议块内的可见提示。 */
function copyConfigLine(line: string, onFailure: () => void): void {
  const clipboard = navigator.clipboard
  if (!clipboard) {
    onFailure()
    return
  }
  void clipboard.writeText(line).then(
    () => notifySuccess('已复制配置行'),
    () => onFailure(),
  )
}

/** 缺口建议：按瓶颈归属给可执行动作，出口瓶颈不给数字。 */
function CapacityHint({ report }: { report: CapacityReport }) {
  const [copyFailed, setCopyFailed] = useState(false)
  if (report.shortfall <= 0) return null
  const suggested = report.suggested_workers ?? 0
  if (report.binding === 'workers' && suggested > 0) {
    const line = `YIBAN_WORKERS=${suggested}`
    return (
      <div className="app-capacity-hint">
        <p>通道是瓶颈：把 YIBAN_WORKERS 提到 {suggested}。</p>
        <div className="app-capacity-hint-line">
          <code>{line}</code>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setCopyFailed(false)
              copyConfigLine(line, () => setCopyFailed(true))
            }}
          >
            复制
          </Button>
        </div>
        {copyFailed ? (
          <p className="app-capacity-copy-error" role="alert">
            复制失败，请手动选择文本
          </p>
        ) : null}
      </div>
    )
  }
  if (report.binding === 'egress_rate') {
    return (
      <div className="app-capacity-hint">
        <p>出口速率是瓶颈：提高 YIBAN_EGRESS_RATE 或增加出口标识。</p>
        <div className="app-capacity-hint-line">
          <code>YIBAN_EGRESS_RATE</code>
        </div>
      </div>
    )
  }
  return null
}

export function CapacityCard({
  options,
  selectedValue,
  onSelect,
  report,
  counts,
  windowsLoading,
  windowsError,
  capacityLoading,
  capacityError,
  onRetryWindows,
  onRetryCapacity,
}: CapacityCardProps) {
  const meter = report ? capacityMeter(report) : null

  return (
    <Card
      className="col-6"
      eyebrow="Capacity"
      title="容量体检"
      action={
        counts !== null ? (
          <span className="app-caption">
            可用窗口 {counts.usable} / {counts.total}
          </span>
        ) : null
      }
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
            <Select
              id="capacity-task"
              label="任务"
              options={options.map((option) => ({ value: option.value, label: option.label }))}
              value={selectedValue}
              disabled={capacityLoading}
              onChange={onSelect}
            />
            {report ? (
              <div className="app-capacity-window-chip">
                <Badge tone={capacitySourceTone(report.source)}>
                  {capacitySourceLabel(report.source)}
                </Badge>
                <span className="app-caption">
                  窗口 {formatClock(report.window_start)}–{formatClock(report.window_end)} · 业务日{' '}
                  {report.day}
                </span>
              </div>
            ) : null}
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
                  <div className="app-metric-value">
                    <AnimatedNumber value={report.capacity} />
                  </div>
                </div>
                <div className="app-metric">
                  <div className="app-metric-label">需求</div>
                  <div className="app-metric-value">
                    <AnimatedNumber value={report.demand} />
                  </div>
                </div>
                <div className="app-metric">
                  <div className="app-metric-label">缺口</div>
                  <div
                    className={
                      report.shortfall > 0
                        ? 'app-metric-value is-danger'
                        : 'app-metric-value is-success'
                    }
                  >
                    <AnimatedNumber value={report.shortfall} />
                  </div>
                </div>
              </div>

              {meter ? (
                <div className="app-capacity-meter" aria-label="容量负载比">
                  <div className="app-capacity-meter-head">
                    <span className="app-caption">需求 / 容量水位</span>
                    <span className={`app-capacity-meter-status t-${meter.tone}`}>
                      {meter.statusText}
                    </span>
                  </div>
                  <div className="progress">
                    <div
                      className={`app-capacity-meter-fill t-${meter.tone}`}
                      style={{ width: `${meter.fillWidth}%` }}
                    />
                  </div>
                </div>
              ) : null}

              <CapacityHint
                key={`${report.task_id}:${report.suggested_workers ?? 0}:${report.shortfall}`}
                report={report}
              />
            </>
          )}
        </>
      )}
      {counts !== null ? (
        <p className="app-muted app-capacity-meta">
          今日窗口 {counts.total} 项 · 已报 {counts.reported} · 停用 {counts.disabled} · 可用{' '}
          {counts.usable}
        </p>
      ) : null}
    </Card>
  )
}
