/**
 * **功能**
 * 队列分组容器：标题、计数、可选分段器、组内学校/关键字筛选、列头、
 * 独立游标分页与持久化动画列表。
 *
 * **归属**
 * `features/queue` 展示组件：只负责单组框体与输入回调，不发请求。
 *
 * **口径**
 * `<ul ref={listRef}>` 在非错误态下始终挂载，骨架屏、空态与真实行共用同一列表宿主，
 * 确保首次加载、末行清空退场与分段切换都具备完整双向过渡。
 * 列头是纯视觉对齐（`aria-hidden`），行内字段自带可读文本，不冒充表格语义；
 * 错误、加载与空态不渲染列头，避免空表头孤悬。
 */

import type { ReactNode } from 'react'
import { useAutoAnimate } from '@formkit/auto-animate/react'
import { AsyncState } from '@/components/ui/AsyncState'
import { Button } from '@/components/ui/Button'
import { Icon, type IconName } from '@/components/ui/Icon'
import { cx } from '@/lib/cx'
import type { School } from '@/lib/api/school'
import { formatPagedSummary } from './queueDisplay'

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
  schools: readonly School[]
  selectedSchoolId: string
  onSchoolChange: (schoolId: string) => void
  searchInput: string
  searchPlaceholder: string
  searchAriaLabel: string
  onSearchInputChange: (value: string) => void
  onSearchCommit: () => void
  hasActiveFilters: boolean
  onClearFilters: () => void
  columnHeaders: readonly string[]
  pageIndex: number
  rowCount: number
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
  schools,
  selectedSchoolId,
  onSchoolChange,
  searchInput,
  searchPlaceholder,
  searchAriaLabel,
  onSearchInputChange,
  onSearchCommit,
  hasActiveFilters,
  onClearFilters,
  columnHeaders,
  pageIndex,
  rowCount,
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

  const showColumnHead = !error && !isLoading && !isEmpty
  const showPagerFoot = !error && !isLoading && (rowCount > 0 || hasPrevious || hasNext)

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

          <select
            className="select q-filter-select"
            aria-label={`${title}学校筛选`}
            value={selectedSchoolId}
            onChange={(event) => onSchoolChange(event.target.value)}
          >
            <option value="">全部学校</option>
            {schools.map((school) => (
              <option key={school.id} value={school.id}>
                {school.name}
              </option>
            ))}
          </select>

          <div className="input-icon q-filter-search">
            <span className="ico">
              <Icon name="search" size={14} />
            </span>
            <input
              type="search"
              className="input q-search-input"
              aria-label={searchAriaLabel}
              placeholder={searchPlaceholder}
              value={searchInput}
              onChange={(event) => onSearchInputChange(event.target.value)}
              onKeyDown={(event) => {
                // 输入法用回车选词时不能当成提交：先看组合态
                if (event.key !== 'Enter' || event.nativeEvent.isComposing) return
                event.preventDefault()
                onSearchCommit()
              }}
            />
          </div>

          {hasActiveFilters ? (
            <Button variant="ghost" size="sm" onClick={onClearFilters}>
              清除筛选
            </Button>
          ) : null}
        </div>
      </header>

      {showColumnHead ? (
        <div className={cx('q-list-head', `is-${groupKey}`)} aria-hidden="true">
          {columnHeaders.map((header) => (
            <span key={header}>{header}</span>
          ))}
        </div>
      ) : null}

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
                <div className="q-skel-col">
                  <div className="q-skel-bar q-skel-line-a" />
                  <div className="q-skel-bar q-skel-line-b" />
                </div>
                <div className="q-skel-bar q-skel-line-c" />
              </li>
            ))
          ) : isEmpty ? (
            <li key="__empty__" className="q-empty" role="status">
              <div className="q-empty-icon">
                <Icon name={emptyIcon} size={18} />
              </div>
              <p className="q-empty-title">
                {hasActiveFilters ? '当前筛选无匹配记录' : emptyTitle}
              </p>
              <p className="q-empty-desc">
                {hasActiveFilters ? '尝试换学校或清空关键字' : emptyDescription}
              </p>
              {hasActiveFilters ? (
                <Button variant="secondary" size="sm" onClick={onClearFilters}>
                  清空筛选
                </Button>
              ) : null}
            </li>
          ) : (
            children
          )}
        </ul>
      )}

      {showPagerFoot ? (
        <footer className="q-pager-foot">
          <span className="q-pager-info">{formatPagedSummary(pageIndex, rowCount, hasNext)}</span>
          {hasPrevious || hasNext ? (
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
        </footer>
      ) : null}
    </section>
  )
}
