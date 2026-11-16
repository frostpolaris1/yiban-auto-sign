/**
 * **功能**
 * 队列分组容器：标题、状态圆点、可选分段器、独立游标分页、持久化动画列表。
 *
 * **归属**
 * `features/queue` 展示组件：只负责单组框体与分页控制，不发请求。
 *
 * **口径**
 * `<ul ref={listRef}>` 在非错误态下始终挂载，骨架屏、空态与真实行共用同一列表宿主，
 * 确保首次加载、末行清空退场与分段切换都具备完整双向过渡。
 */

import type { ReactNode } from 'react'
import { useAutoAnimate } from '@formkit/auto-animate/react'
import { AsyncState } from '@/components/ui/AsyncState'
import { Icon, type IconName } from '@/components/ui/Icon'
import { cx } from '@/lib/cx'

export type QueueSegmentOption<T extends string> = {
  value: T
  label: string
  count?: number | null
}

type QueueGroupProps<T extends string = string> = {
  id: string
  groupKey: string
  title: string
  description: string
  tone: 'warning' | 'danger' | 'info' | 'primary'
  pulse?: boolean
  badgeCount: number | string | null
  segments?: readonly QueueSegmentOption<T>[]
  activeSegment?: T
  onSegmentChange?: (next: T) => void
  hasPrevious: boolean
  hasNext: boolean
  pagingDisabled?: boolean
  onPrevious: () => void
  onNext: () => void
  isLoading: boolean
  isFetching: boolean
  error: string | null
  onRetry: () => void
  isEmpty: boolean
  emptyIcon: IconName
  emptyTitle: string
  emptyDescription: string
  children: ReactNode
}

export function QueueGroup<T extends string = string>({
  id,
  groupKey,
  title,
  description,
  tone,
  pulse,
  badgeCount,
  segments,
  activeSegment,
  onSegmentChange,
  hasPrevious,
  hasNext,
  pagingDisabled,
  onPrevious,
  onNext,
  isLoading,
  isFetching,
  error,
  onRetry,
  isEmpty,
  emptyIcon,
  emptyTitle,
  emptyDescription,
  children,
}: QueueGroupProps<T>) {
  const [listRef] = useAutoAnimate<HTMLUListElement>({
    duration: 180,
    disrespectUserMotionPreference: false,
  })

  const showPager = hasPrevious || hasNext

  return (
    <section
      id={id}
      className="q-card"
      aria-labelledby={`${id}-title`}
      data-queue-group={groupKey}
    >
      <header className="q-card-head">
        <div className="q-card-title-wrap">
          <span
            className={cx('q-dot', `is-${tone}`, pulse && 'is-pulse')}
            aria-hidden="true"
          />
          <div className="q-card-title-row">
            <h2 id={`${id}-title`} className="q-card-title">
              {title}
            </h2>
            {badgeCount !== null ? (
              <span
                className={cx('q-count-pill', `is-${tone}`)}
                data-queue-count
              >
                {badgeCount}
              </span>
            ) : null}
            <span className="q-card-desc">{description}</span>
            {isFetching && !isLoading ? (
              <span className="spinner sm" aria-label="刷新中" />
            ) : null}
          </div>
        </div>

        <div className="q-card-controls">
          {segments && activeSegment && onSegmentChange ? (
            <div className="q-seg" role="group" aria-label={`${title}状态切换`}>
              {segments.map((seg) => {
                const active = seg.value === activeSegment
                return (
                  <button
                    key={seg.value}
                    type="button"
                    className={cx('q-seg-btn', active && 'is-active')}
                    aria-pressed={active}
                    onClick={() => onSegmentChange(seg.value)}
                  >
                    <span>{seg.label}</span>
                    {seg.count !== undefined && seg.count !== null ? (
                      <span className="q-seg-count">{seg.count}</span>
                    ) : null}
                  </button>
                )
              })}
            </div>
          ) : null}

          {showPager ? (
            <nav className="q-pager" aria-label={`${title}分页`}>
              <button
                type="button"
                className="q-pager-btn"
                disabled={!hasPrevious || pagingDisabled}
                onClick={onPrevious}
              >
                <Icon name="chevronLeft" size={14} />
                上一页
              </button>
              <button
                type="button"
                className="q-pager-btn"
                disabled={!hasNext || pagingDisabled}
                onClick={onNext}
              >
                下一页
                <Icon name="chevronRight" size={14} />
              </button>
            </nav>
          ) : null}
        </div>
      </header>

      {error ? (
        <div className="q-group-error">
          <AsyncState status="error" error={error} onRetry={onRetry} children={null} />
        </div>
      ) : (
        <ul ref={listRef} className="q-list">
          {isLoading ? (
            [0, 1].map((idx) => (
              <li
                key={`__skel_${idx}`}
                className="q-skeleton-row"
                role="status"
                aria-label={idx === 0 ? `${title}加载中` : undefined}
              >
                <div className="flex flex-col gap-2 flex-1">
                  <div className="q-skel-bar w-44" />
                  <div className="q-skel-bar w-64" />
                </div>
                <div className="q-skel-bar w-24" />
              </li>
            ))
          ) : isEmpty ? (
            <li key="__empty__" className="q-empty" role="status">
              <div className="q-empty-icon">
                <Icon name={emptyIcon} size={18} />
              </div>
              <p className="q-empty-title">{emptyTitle}</p>
              <p className="q-empty-desc">{emptyDescription}</p>
            </li>
          ) : (
            children
          )}
        </ul>
      )}
    </section>
  )
}
