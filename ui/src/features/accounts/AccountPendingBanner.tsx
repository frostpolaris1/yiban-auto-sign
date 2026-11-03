/**
 * **功能**
 * 账号页待审提示条：显示未软删账号的待审核与已拒绝计数，并提供
 * 「只看待审核」快捷筛选。
 *
 * **归属**
 * `features/accounts` 展示层：只受控渲染；计数由容器从
 * `GET /api/accounts/summary`（缓存键 `['account-summary']`）取得后传入。
 *
 * **口径**
 * 计数与列表缺省口径同源：软删行不进任何一个数。
 * 两数都到齐才渲染；加载中或失败由容器整条隐藏，不显示半截数据。
 */

import { Button } from '@/components/ui/Button'

type AccountPendingBannerProps = {
  pending: number
  rejected: number
  /** 把状态筛选设为 pending，由容器执行（连带复位游标栈）。 */
  onFilterPending: () => void
}

export function AccountPendingBanner({ pending, rejected, onFilterPending }: AccountPendingBannerProps) {
  return (
    <div className="app-pending-banner" role="status">
      <span>待审核 {pending} · 已拒绝 {rejected}</span>
      <Button variant="secondary" size="sm" onClick={onFilterPending}>
        只看待审核
      </Button>
    </div>
  )
}
