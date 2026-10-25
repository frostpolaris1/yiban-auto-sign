import type { ReactNode } from 'react'
import { Icon } from '@/components/ui/Icon'

export type DataTableColumn<T> = {
  key: string
  header: ReactNode
  render: (row: T) => ReactNode
  width?: string
  align?: 'left' | 'right'
  sorted?: 'asc' | 'desc'
}

export type DataTableProps<T> = {
  columns: Array<DataTableColumn<T>>
  rows: T[]
  rowKey: (row: T) => string
  page: number
  pageSize: number
  total: number
  onPageChange: (page: number) => void
  toolbar?: ReactNode
  caption?: string
  emptyLabel?: string
  /** Adminator demo keeps a 900px minimum so narrow viewports scroll inside `.table-scroll`. */
  minWidth?: number
}

/**
 * Server-paginated Adminator data table.
 *
 * The component renders one page of rows only. It never holds the full result
 * set, so a 10k account list cannot become a 10k-node DOM.
 */
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  page,
  pageSize,
  total,
  onPageChange,
  toolbar,
  caption = '数据表',
  emptyLabel = '暂无数据',
  minWidth = 900,
}: DataTableProps<T>) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  const first = total === 0 ? 0 : (page - 1) * pageSize + 1
  const last = Math.min(page * pageSize, total)
  const windowSize = Math.min(5, pageCount)
  const start = Math.max(1, Math.min(page - Math.floor(windowSize / 2), pageCount - windowSize + 1))
  const pageNumbers = Array.from({ length: windowSize }, (_, index) => start + index)

  return (
    <>
      {toolbar ? <div className="data-toolbar">{toolbar}</div> : null}

      <div className="table-scroll">
        <table className="data-table" style={{ minWidth }}>
          <caption className="app-visually-hidden">{caption}</caption>
          <thead>
            <tr>
              {columns.map((column) => (
                <th
                  key={column.key}
                  className={column.sorted ? `sorted-${column.sorted}` : undefined}
                  style={column.width ? { width: column.width } : undefined}
                  scope="col"
                >
                  {column.header}
                  {column.sorted ? (
                    <span className="sort">
                      <Icon name="chevronDown" />
                    </span>
                  ) : null}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={columns.length} className="app-muted">
                  {emptyLabel}
                </td>
              </tr>
            ) : (
              rows.map((row) => (
                <tr key={rowKey(row)} className="data-row">
                  {columns.map((column) => (
                    <td
                      key={column.key}
                      style={column.align === 'right' ? { textAlign: 'right' } : undefined}
                    >
                      {column.render(row)}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <div className="data-foot">
        <div className="data-foot-info">
          <span>
            显示 <strong>{first}–{last}</strong> / 共 <strong>{total}</strong> 条
          </span>
          <span className="app-toolbar-note">每页 {pageSize} 条</span>
        </div>
        <div className="pager">
          <button
            type="button"
            className="pager-btn"
            aria-label="上一页"
            disabled={page <= 1}
            onClick={() => onPageChange(page - 1)}
          >
            <Icon name="chevronLeft" />
          </button>
          {pageNumbers.map((number) => (
            <button
              key={number}
              type="button"
              className={number === page ? 'pager-btn is-active' : 'pager-btn'}
              aria-label={`第 ${number} 页`}
              aria-current={number === page ? 'page' : undefined}
              onClick={() => onPageChange(number)}
            >
              {number}
            </button>
          ))}
          <button
            type="button"
            className="pager-btn"
            aria-label="下一页"
            disabled={page >= pageCount}
            onClick={() => onPageChange(page + 1)}
          >
            <Icon name="chevronRight" />
          </button>
        </div>
      </div>
    </>
  )
}
