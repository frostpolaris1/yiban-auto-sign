/**
 * **功能**
 * 系统设置页（全站域）：页级角色门、页顶分区锚链与单列分区容器
 * （计划 / 策略 / 公告 / 渠道 / 名册 / 版本）。
 *
 * **归属**
 * `features/system` 域容器：页面 `SystemPage` 只引用它。
 *
 * **口径**
 * 权限由 `SYSTEM_VIEW_ROLES`（master/admin）控制；user 渲染无权限卡片且零业务请求。
 * 页顶锚链覆盖 `#plans`、`#policy`、`#announcement`、`#channels`、`#roster`、`#version`
 * 六个单列分区，URL 携带 hash 时自动滚动并聚焦目标分区，滚动时同步更新当前可见锚点。
 */

import { useEffect, useRef, useState } from 'react'
import { useRouterState } from '@tanstack/react-router'
import { DeniedCard } from '@/components/ui/DeniedCard'
import { PageHeader } from '@/components/ui/PageHeader'
import { cx } from '@/lib/cx'
import { SYSTEM_VIEW_ROLES, roleAllowed } from '@/lib/roles'
import { useMe } from '@/lib/session'
import { READ_ONLY_REASON, SHOWCASE } from '@/lib/showcase-mode'
import { AnnouncementSection } from './announcement/AnnouncementSection'
import { ChannelsSection } from './channels/ChannelsSection'
import { ScheduleView } from './plans/ScheduleView'
import { POLICY_READ_ONLY_REASON } from './policy/drafts'
import { PolicySection } from './policy/PolicySection'
import { RosterSection } from './roster/RosterSection'
import { VersionSection } from './version/VersionSection'
import './system.css'

const SYSTEM_SECTIONS = [
  { id: 'plans', label: '计划' },
  { id: 'policy', label: '策略' },
  { id: 'announcement', label: '公告' },
  { id: 'channels', label: '渠道' },
  { id: 'roster', label: '名册' },
  { id: 'version', label: '版本' },
] as const

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

function isNearDocumentBottom(): boolean {
  if (typeof window === 'undefined') return false
  const doc = document.documentElement
  if (doc.scrollHeight <= window.innerHeight + 8) return false
  return window.scrollY + window.innerHeight >= doc.scrollHeight - 4
}

export function SystemView({ day, onDayChange }: SystemViewProps) {
  const meQuery = useMe()
  const me = meQuery.data
  const hash = useRouterState({ select: (state) => state.location.hash })
  const allowed = me ? roleAllowed(me.role, SYSTEM_VIEW_ROLES) : false
  const targeted = normalizeSectionHash(hash)
  const [observedAnchor, setObservedAnchor] = useState<SystemSectionId | null>(null)
  const activeAnchor: SystemSectionId = observedAnchor ?? targeted ?? 'plans'
  const lockUntilRef = useRef(0)
  const lockTimerRef = useRef<number | null>(null)
  const resolveActiveRef = useRef<() => void>(() => {})
  const sectionsContainerRef = useRef<HTMLDivElement>(null)

  const lockAnchorSync = (durationMs: number) => {
    lockUntilRef.current = Date.now() + durationMs
    if (lockTimerRef.current !== null) window.clearTimeout(lockTimerRef.current)
    lockTimerRef.current = window.setTimeout(() => {
      lockTimerRef.current = null
      resolveActiveRef.current()
    }, durationMs + 16)
  }

  useEffect(() => {
    return () => {
      if (lockTimerRef.current !== null) window.clearTimeout(lockTimerRef.current)
    }
  }, [])

  useEffect(() => {
    if (!allowed || !targeted) return
    setObservedAnchor(targeted)
    lockAnchorSync(900)
    const scrollToTarget = (focusSection: boolean) => {
      const element = document.getElementById(targeted)
      if (!element) return
      element.scrollIntoView({ block: 'start', behavior: 'auto' })
      if (focusSection) element.focus({ preventScroll: true })
    }
    scrollToTarget(true)

    // 异步区块加载展开后可能顶偏下方 hash 目标；短暂观察容器高度直到用户主动交互或超时。
    if (targeted === 'plans' || typeof ResizeObserver === 'undefined') return
    const container = sectionsContainerRef.current
    if (!container) return
    let active = true
    const stop = () => {
      active = false
      observer.disconnect()
      window.removeEventListener('wheel', stop)
      window.removeEventListener('touchstart', stop)
      window.removeEventListener('pointerdown', stop)
    }
    const observer = new ResizeObserver(() => {
      if (!active) return
      lockAnchorSync(300)
      scrollToTarget(false)
    })
    observer.observe(container)
    window.addEventListener('wheel', stop, { passive: true })
    window.addEventListener('touchstart', stop, { passive: true })
    window.addEventListener('pointerdown', stop, { passive: true })
    const timer = window.setTimeout(stop, 1200)
    return () => {
      window.clearTimeout(timer)
      stop()
    }
  }, [allowed, targeted])

  useEffect(() => {
    if (!allowed || typeof IntersectionObserver === 'undefined') return
    const visible = new Set<SystemSectionId>()

    const resolveActive = () => {
      if (Date.now() < lockUntilRef.current) return
      if (isNearDocumentBottom()) {
        setObservedAnchor(SYSTEM_SECTIONS[SYSTEM_SECTIONS.length - 1].id)
        return
      }
      const firstVisible = SYSTEM_SECTIONS.find((section) => visible.has(section.id))
      if (firstVisible) setObservedAnchor(firstVisible.id)
    }
    resolveActiveRef.current = resolveActive

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const id = normalizeSectionHash(entry.target.id)
          if (!id) continue
          if (entry.isIntersecting) visible.add(id)
          else visible.delete(id)
        }
        resolveActive()
      },
      { rootMargin: '-18% 0px -55% 0px', threshold: 0 },
    )
    for (const section of SYSTEM_SECTIONS) {
      const el = document.getElementById(section.id)
      if (el) observer.observe(el)
    }
    const handleScroll = () => resolveActive()
    window.addEventListener('scroll', handleScroll, { passive: true })
    window.addEventListener('scrollend', handleScroll, { passive: true })
    return () => {
      resolveActiveRef.current = () => {}
      observer.disconnect()
      window.removeEventListener('scroll', handleScroll)
      window.removeEventListener('scrollend', handleScroll)
    }
  }, [allowed])

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

  const canWrite = me.role === 'master' && !SHOWCASE
  const readOnlyReason = SHOWCASE
    ? READ_ONLY_REASON
    : me.role === 'master'
      ? null
      : POLICY_READ_ONLY_REASON

  return (
    <>
      <PageHeader
        eyebrow="系统 · 系统设置"
        title={
          <>
            系统<span className="accent">设置</span>
          </>
        }
        subtitle="全站运行策略、公告、渠道测试、签到计划、配置名册与版本自检控制台。"
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
              onClick={() => {
                lockAnchorSync(900)
                setObservedAnchor(section.id)
              }}
            >
              {section.label}
            </a>
          )
        })}
      </nav>

      <div ref={sectionsContainerRef} className="sys-sections">
        <ScheduleView day={day} onDayChange={onDayChange} />
        <PolicySection canWrite={canWrite} readOnlyReason={readOnlyReason} />
        <AnnouncementSection canWrite={canWrite} readOnlyReason={readOnlyReason} />
        <ChannelsSection canWrite={canWrite} readOnlyReason={readOnlyReason} />
        <RosterSection />
        <VersionSection />
      </div>
    </>
  )
}
