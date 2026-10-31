/**
 * **功能**
 * 异步区块四态：加载、错误、空、数据。
 *
 * **归属**
 * `components` 通用件：只呈现状态，不取数。
 *
 * **口径**
 * 错误与空**语义分离**：错误是 `role="alert"` 的危险提示条，空是
 * `role="status"` 的静默文案。失败不得渲染成「暂无」。
 * 错误态给可选重试；加载与空都带 `role="status"`。
 * 数据态直接渲染 children，不加包裹层。
 * 外观复用既有件：错误用 `Alert`（danger），重试用 `Button`，等待环用 `.spinner`。
 */

import type { ReactNode } from 'react'
import { cx } from '@/lib/cx'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'

export type AsyncStatus = 'loading' | 'error' | 'empty' | 'data'

type AsyncStateProps = {
  status: AsyncStatus
  /** 错误态正文。缺省给固定文案，不回显底层错误。 */
  error?: ReactNode
  /** 重试回调。给出才渲染重试按钮；缺省时错误态只有说明。 */
  onRetry?: () => void
  retryLabel?: string
  loadingLabel?: string
  emptyLabel?: string
  /** 非数据态的外层类。数据态不加包裹层，故该值不生效。 */
  className?: string
  /** 数据态内容。其余四态下不渲染。 */
  children: ReactNode
}

const DEFAULT_ERROR = '读取失败，请稍后重试'
const DEFAULT_RETRY = '重试'
const DEFAULT_LOADING = '加载中…'
const DEFAULT_EMPTY = '暂无数据'

export function AsyncState({
  status,
  error = DEFAULT_ERROR,
  onRetry,
  retryLabel = DEFAULT_RETRY,
  loadingLabel = DEFAULT_LOADING,
  emptyLabel = DEFAULT_EMPTY,
  className,
  children,
}: AsyncStateProps) {
  if (status === 'data') {
    return <>{children}</>
  }
  if (status === 'error') {
    // 空串与 null 同判为缺省：错误态不能渲染成没有文字的 alert。
    const shownError = error ? error : DEFAULT_ERROR
    return (
      <div className={cx('app-async-error', className)}>
        <Alert tone="danger">{shownError}</Alert>
        {onRetry ? (
          <Button variant="outline-danger" size="sm" onClick={onRetry}>
            {retryLabel}
          </Button>
        ) : null}
      </div>
    )
  }
  return (
    <div className={cx('app-async-state', className)} role="status">
      {status === 'loading' ? <span className="spinner sm" aria-hidden="true" /> : null}
      {status === 'loading' ? loadingLabel : emptyLabel}
    </div>
  )
}
