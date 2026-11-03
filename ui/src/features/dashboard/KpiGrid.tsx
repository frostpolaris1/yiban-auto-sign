/**
 * **功能**
 * KPI 格：数字、单位与一句说明，全部由调用方给的卡片描述驱动。
 *
 * **归属**
 * `features/dashboard` 展示层：只排版，不取数、不定义指标算法。
 *
 * **口径**
 * 派生计数在 `display.ts`；本组件不认角色，也不认端点。
 * `value === null` 显示 `—`：没有数据就不编数字。
 * 数字用滚动组件渲染，语言 `zh-CN`，千分位与该语言一致。
 * 卡片可带一枚环比 pill（`.kpi-pill`），样式取自 Adminator 既有令牌。
 * 卡片可带 `href`：整卡渲染为链接（如待处理账号卡跳账号页），
 * 可访问名称来自卡内文本；无 `href` 仍是普通卡。
 * 终值是可见文本：数据到位后直接等于期望数字。
 */

import { Link } from '@tanstack/react-router'
import { AnimatedNumber } from '@/components/ui/AnimatedNumber'
import { Icon } from '@/components/ui/Icon'
import type { KpiCard } from './display'

/** 卡面本体：普通卡与链接卡共用同一 DOM 结构（Adminator `src/index.html`）。 */
function cardBody(card: KpiCard) {
  return (
    <>
      <div className="kpi-top">
        <div className="kpi-identity">
          <div className={`kpi-icon ${card.tone}`}>
            <Icon name={card.icon} />
          </div>
          <div className="kpi-label">{card.label}</div>
        </div>
      </div>
      <div className="kpi-value">
        {card.value === null ? '—' : <AnimatedNumber value={card.value} />}
        {card.value === null ? null : <sup>{card.unit}</sup>}
      </div>
      <div className="kpi-compare">
        {card.pill ? <span className={`kpi-pill ${card.pill.cls}`}>{card.pill.text}</span> : null}
        <span>{card.note}</span>
      </div>
    </>
  )
}

export function KpiGrid({ cards }: { cards: KpiCard[] }) {
  return (
    <section className="kpi-grid" aria-label="关键指标">
      {cards.map((card) =>
        card.href ? (
          <Link key={card.label} className={`kpi-card c-${card.tone}`} to={card.href}>
            {cardBody(card)}
          </Link>
        ) : (
          <article key={card.label} className={`kpi-card c-${card.tone}`}>
            {cardBody(card)}
          </article>
        ),
      )}
    </section>
  )
}
