import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { Hint } from '@/components/ui/Hint'

type ProgressRow = {
  label: string
  value: string
  percent: number
  fill: string
  thin?: boolean
}

const PROGRESS_ROWS: ProgressRow[] = [
  { label: '窗口占用', value: '7.4 / 15', percent: 49, fill: 'gradient' },
  { label: '接口配额', value: '82%', percent: 82, fill: 'warning striped' },
  { label: '失败率', value: '3%', percent: 3, fill: 'danger' },
  { label: '配置完成度', value: '100%', percent: 100, fill: 'success' },
  { label: '细条 · 4px', value: '60%', percent: 60, fill: '', thin: true },
]

/** Progress bars, spinners, avatar group, and tooltip. DOM follows `src/ui.html`. */
export function IndicatorsSection() {
  return (
    <Card className="col-6" eyebrow="Status" title="进度、加载与头像">
      <div className="app-demo-stack">
        {PROGRESS_ROWS.map((row) => (
          <div key={row.label}>
            <div className="app-progress-row" style={{ justifyContent: 'space-between' }}>
              <span className="app-muted">{row.label}</span>
              <span className="app-caption mono">{row.value}</span>
            </div>
            <div className={row.thin ? 'progress thin' : 'progress'}>
              <div
                className={`progress-fill ${row.fill}`.trim()}
                style={{ width: `${row.percent}%`, minWidth: row.percent < 6 ? 8 : undefined }}
              />
            </div>
          </div>
        ))}

        <div className="demo-row" style={{ gap: 24, padding: '8px 0' }}>
          <span className="spinner sm" role="status" aria-label="加载中（小）" />
          <span className="spinner" role="status" aria-label="加载中" />
          <span className="spinner lg" role="status" aria-label="加载中（大）" />
          <span
            className="spinner"
            role="status"
            aria-label="加载中（成功色）"
            style={{ borderTopColor: 'var(--success)' }}
          />
          <span
            className="spinner"
            role="status"
            aria-label="加载中（危险色）"
            style={{ borderTopColor: 'var(--danger)' }}
          />
        </div>

        <div className="demo-row">
          <div className="avatar-group">
            <span className="av ma-1">JD</span>
            <span className="av ma-2">SK</span>
            <span className="av ma-3">LR</span>
            <span className="av ma-4">MD</span>
            <span className="av ma-5">PV</span>
            <span className="av more">+8</span>
          </div>
        </div>

        <div className="demo-row" style={{ padding: '18px 0 6px' }}>
          <Hint label="签到窗口 06:30">
            <Button variant="ghost">悬停查看提示</Button>
          </Hint>
        </div>
      </div>
    </Card>
  )
}
