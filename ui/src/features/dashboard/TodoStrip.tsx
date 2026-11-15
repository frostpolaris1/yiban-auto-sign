/**
 * **功能**
 * 待办行动区：总览首屏第一块，三张整卡链接直达处理页。
 *
 * **归属**
 * `features/dashboard` 展示层：纯渲染，计数由 `display.todoItems` 派生。
 *
 * **口径**
 * 只放「等着我处理」的三件事：待审核/已驳回、今日失败、容量缺口。
 * 值 null 显「—」；全零降透明度但保留链接（零也是结论）。
 * 不与 KPI 行重复同一数字：待处理计数只在这里出现一次。
 */

import { Link } from '@tanstack/react-router'
import { Icon } from '@/components/ui/Icon'
import type { TodoItem } from './display'

type TodoStripProps = {
  items: TodoItem[]
}

export function TodoStrip({ items }: TodoStripProps) {
  return (
    <section className="app-todo-strip" aria-label="需要处理">
      {items.map((item) => (
        <Link
          key={item.key}
          to={item.href}
          className={`app-todo-card t-${item.tone}${item.value === 0 ? ' is-zero' : ''}`}
        >
          <span className="app-todo-head">
            <Icon name={item.icon} />
            {item.label}
          </span>
          <span className="app-todo-value">{item.value ?? '—'}</span>
          <span className="app-todo-note">{item.note}</span>
          <span className="app-todo-go" aria-hidden="true">
            →
          </span>
        </Link>
      ))}
    </section>
  )
}