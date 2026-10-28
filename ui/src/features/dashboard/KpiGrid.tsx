/**
 * **功能**
 * 四格 KPI：数字、单位与一句说明，全部由调用方给的卡片描述驱动。
 *
 * **归属**
 * `features/dashboard` 展示层：只排版，不取数、不定义指标算法。
 *
 * **口径**
 * 派生计数在 `display.ts`；本组件不认角色，也不认端点。
 * `value === null` 显示 `—`：没有数据就不编数字。
 * 数字用 `formatNumber`，千分位随 `zh-CN`。
 */

import { Icon } from '@/components/ui/Icon'
import { formatNumber } from '@/lib/format'
import type { KpiCard } from './display'

/** 卡片 DOM 照 Adminator `src/index.html`；环比与趋势标记已去掉。 */
export function KpiGrid({ cards }: { cards: KpiCard[] }) {
  return (
    <section className="kpi-grid" aria-label="关键指标">
      {cards.map((card) => (
        <article key={card.label} className={`kpi-card c-${card.tone}`}>
          <div className="kpi-top">
            <div className="kpi-identity">
              <div className={`kpi-icon ${card.tone}`}>
                <Icon name={card.icon} />
              </div>
              <div className="kpi-label">{card.label}</div>
            </div>
          </div>
          <div className="kpi-value">
            {card.value === null ? '—' : formatNumber(card.value)}
            {card.value === null ? null : <sup>{card.unit}</sup>}
          </div>
          <div className="kpi-compare">
            <span>{card.note}</span>
          </div>
        </article>
      ))}
    </section>
  )
}
