/**
 * **功能**
 * 系统设置页（全站域）：页级角色门、页顶分区锚链与单列分区容器。
 *
 * **归属**
 * `features/system` 域容器：页面 `SystemPage` 只引用它。
 *
 * **口径**
 * 权限由 `SYSTEM_VIEW_ROLES`（master/admin）控制；user 渲染无权限卡片且零业务请求。
 * 本批开放「签到计划」分区（`#plans`），后续批次在同一单列容器内追加分区与锚链。
 * URL 携带 `#plans`（含 `/schedule` 重定向而来）时滚至对应分区并留出顶栏安全距。
 */

import { useEffect } from 'react'
import { useRouterState } from '@tanstack/react-router'
import { DeniedCard } from '@/components/ui/DeniedCard'
import { PageHeader } from '@/components/ui/PageHeader'
import { cx } from '@/lib/cx'
import { SYSTEM_VIEW_ROLES, roleAllowed } from '@/lib/roles'
import { useMe } from '@/lib/session'
import { ScheduleView } from './plans/ScheduleView'
import './system.css'

const SYSTEM_SECTIONS = [{ id: 'plans', label: '计划' }] as const

type SystemSectionId = (typeof SYSTEM_SECTIONS)[number]['id']

type SystemViewProps = {
  /** 查询串里的业务日；缺省由计划分区按上海日历今天处理。 */
  day: string | undefined
  onDayChange: (day: string) => void
}

function normalizeSectionHash(hash: string): SystemSectionId | null {
  const clean = hash.replace(/^#/, '')
  const matched = SYSTEM_SECTIONS.find((section) => section.id === clean)
  return matched?.id ?? null
}

export function SystemView({ day, onDayChange }: SystemViewProps) {
  const meQuery = useMe()
  const me = meQuery.data
  const hash = useRouterState({ select: (state) => state.location.hash })
  const allowed = me ? roleAllowed(me.role, SYSTEM_VIEW_ROLES) : false
  const targeted = normalizeSectionHash(hash)
  const activeAnchor: SystemSectionId = targeted ?? 'plans'

  useEffect(() => {
    if (!allowed || !targeted) return
    const element = document.getElementById(targeted)
    element?.scrollIntoView({ block: 'start', behavior: 'auto' })
  }, [allowed, targeted])

  // 壳在会话解析完成前不渲染出口；这里只防御缓存被清空的瞬间。
  if (!me) return null

  if (!allowed) {
    return (
      <DeniedCard
        eyebrow="系统 · 系统设置"
        title="无权访问"
        reason="你的角色无权查看系统设置"
      />
    )
  }

  return (
    <>
      <PageHeader
        eyebrow="系统 · 系统设置"
        title={
          <>
            系统<span className="accent">设置</span>
          </>
        }
        subtitle="全站运行配置与调度计划；本批开放签到计划分区。"
      />

      <nav className="sys-anchors" aria-label="系统设置分区">
        {SYSTEM_SECTIONS.map((section) => {
          const isCurrent = activeAnchor === section.id
          return (
            <a
              key={section.id}
              href={`#${section.id}`}
              className={cx('sys-anchor', isCurrent && 'is-active')}
              aria-current={isCurrent ? 'location' : undefined}
            >
              {section.label}
            </a>
          )
        })}
      </nav>

      <div className="sys-sections">
        <ScheduleView day={day} onDayChange={onDayChange} />
      </div>
    </>
  )
}
