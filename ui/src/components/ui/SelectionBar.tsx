/**
 * **功能**
 * 批量选择条：页内一条，报已选数、放动作按钮、给清除出口。
 *
 * **归属**
 * `components` 通用件：动作按钮由页面传入，本组件不判断权限。
 *
 * **口径**
 * 只在有选中行时由页面渲染；选中清空即随页面卸载。
 * 上限提示只报事实：超过契约 10 条时页面负责禁用动作并给出原因。
 * 危险项与常规项的分隔由页面在 children 里用 `.app-batch-danger` 分组。
 */

import type { ReactNode } from 'react'
import { Button } from '@/components/ui/Button'

type SelectionBarProps = {
  count: number
  /** 单次批量上限；来自契约的 maxItems。 */
  limit: number
  onClear: () => void
  children: ReactNode
}

export function SelectionBar({ count, limit, onClear, children }: SelectionBarProps) {
  const overLimit = count > limit
  return (
    <div className="app-batch-bar" role="region" aria-label="批量操作">
      <span className="app-batch-count">
        已选 <strong>{count}</strong> 条
      </span>
      <div className="app-batch-actions">{children}</div>
      {overLimit ? (
        <span className="app-deny-note">
          一次最多 {limit} 条，请先取消多余选择
        </span>
      ) : null}
      <Button variant="ghost" size="sm" onClick={onClear}>
        清除选择
      </Button>
    </div>
  )
}
