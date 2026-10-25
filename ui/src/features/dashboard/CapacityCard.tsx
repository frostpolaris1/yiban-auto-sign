type CapacityRow = {
  label: string
  percent: number
  fillClass: string
}

const ROWS: CapacityRow[] = [
  { label: '北校区 · 早签', percent: 62, fillClass: '' },
  { label: '南校区 · 早签', percent: 88, fillClass: 'warning' },
  { label: '北校区 · 晚签', percent: 31, fillClass: 'success' },
]

/** Window and capacity card. Uses `.progress` and `.monthly-footer` from Adminator. */
export function CapacityCard() {
  return (
    <section className="col-6 card">
      <div className="card-head">
        <div className="card-title-wrap">
          <span className="eyebrow">Capacity</span>
          <h2 className="card-title">窗口与容量</h2>
        </div>
        <span className="badge primary">示例</span>
      </div>

      <div className="app-demo-stack">
        {ROWS.map((row) => (
          <div key={row.label}>
            <div className="app-progress-row" style={{ justifyContent: 'space-between' }}>
              <span className="app-muted">{row.label}</span>
              <span className="app-caption">{row.percent}%</span>
            </div>
            <div className="progress">
              <div
                className={`progress-fill ${row.fillClass}`.trim()}
                style={{ width: `${row.percent}%` }}
              />
            </div>
          </div>
        ))}
      </div>

      <div className="monthly-footer">
        <div className="stat-cell">
          <div className="stat-cell-label">时间片</div>
          <div className="stat-cell-value">5 分钟</div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">窗口</div>
          <div className="stat-cell-value">80 分钟</div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">配速</div>
          <div className="stat-cell-value">front</div>
        </div>
        <div className="stat-cell">
          <div className="stat-cell-label">出口</div>
          <div className="stat-cell-value">3</div>
        </div>
      </div>
    </section>
  )
}
