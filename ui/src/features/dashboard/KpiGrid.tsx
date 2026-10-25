import { Icon, type IconName } from '@/components/ui/Icon'
import { formatNumber } from '@/lib/format'

type KpiItem = {
  tone: 'success' | 'danger' | 'purple' | 'primary'
  icon: IconName
  label: string
  pillTone: 'up' | 'down' | 'flat' | 'info'
  pillIcon: IconName
  pillText: string
  value: number
  unit: string
  compareTone: 'up' | 'down' | 'flat' | 'info'
  compareIcon: IconName
  compareStrong: string
  compareTail: string
}

const KPI_ITEMS: KpiItem[] = [
  {
    tone: 'success',
    icon: 'check',
    label: '今日已签',
    pillTone: 'up',
    pillIcon: 'trendUp',
    pillText: '98.2%',
    value: 806,
    unit: '个',
    compareTone: 'up',
    compareIcon: 'trendUp',
    compareStrong: '+11',
    compareTail: '较昨日',
  },
  {
    tone: 'danger',
    icon: 'alertCircle',
    label: '待处理',
    pillTone: 'down',
    pillIcon: 'trendDown',
    pillText: '−3',
    value: 12,
    unit: '个',
    compareTone: 'down',
    compareIcon: 'trendDown',
    compareStrong: '2',
    compareTail: '含凭据错误',
  },
  {
    tone: 'purple',
    icon: 'accounts',
    label: '托管账号',
    pillTone: 'flat',
    pillIcon: 'trendFlat',
    pillText: '~2.4%',
    value: 1024,
    unit: '个',
    compareTone: 'flat',
    compareIcon: 'trendFlat',
    compareStrong: '10,000',
    compareTail: '注册上限',
  },
  {
    tone: 'primary',
    icon: 'shield',
    label: '出口健康',
    pillTone: 'info',
    pillIcon: 'trendFlat',
    pillText: '3 / 3',
    value: 100,
    unit: '%',
    compareTone: 'info',
    compareIcon: 'trendFlat',
    compareStrong: '30 分钟',
    compareTail: '稳定',
  },
]

/** KPI card DOM copied from Adminator `src/index.html`. */
export function KpiGrid() {
  return (
    <section className="kpi-grid" aria-label="关键指标">
      {KPI_ITEMS.map((item) => (
        <article key={item.label} className={`kpi-card c-${item.tone}`}>
          <div className="kpi-top">
            <div className="kpi-identity">
              <div className={`kpi-icon ${item.tone}`}>
                <Icon name={item.icon} />
              </div>
              <div className="kpi-label">{item.label}</div>
            </div>
            <span className={`kpi-pill ${item.pillTone}`}>
              <Icon name={item.pillIcon} />
              {item.pillText}
            </span>
          </div>
          <div className="kpi-value">
            {formatNumber(item.value)}
            <sup>{item.unit}</sup>
          </div>
          <div className="kpi-compare">
            <Icon name={item.compareIcon} className={item.compareTone} />
            <strong>{item.compareStrong}</strong>
            <span className="sep">·</span>
            {item.compareTail}
          </div>
        </article>
      ))}
    </section>
  )
}
