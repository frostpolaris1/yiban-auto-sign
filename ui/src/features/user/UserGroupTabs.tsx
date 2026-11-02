/**
 * **功能**
 * 用户分组切换：全部 / 待处理 / 正式 / 空用户，各带全局计数。
 *
 * **归属**
 * `features/user` 展示层：只受控渲染，不改 URL。
 *
 * **口径**
 * 计数来自服务端 `UserPage.counts`，不随筛选或分页收窄。
 * 全部＝三个未删分组之和；已删除视图不是分组，选中时四个标签都不高亮。
 * 点击标签由调用方写 URL；切组会退出已删除视图。
 */

import type { UserCounts, UserGroup } from '@/lib/api/user'
import { Button } from '@/components/ui/Button'
import { USER_GROUP_LABELS } from './display'

type UserGroupTabsProps = {
  group?: UserGroup
  deleted?: boolean
  counts?: UserCounts
  onGroupChange: (group?: UserGroup) => void
}

const TABS: Array<{ key?: UserGroup; label: string }> = [
  { key: undefined, label: '全部' },
  { key: 'pending', label: USER_GROUP_LABELS.pending },
  { key: 'normal', label: USER_GROUP_LABELS.normal },
  { key: 'vacant', label: USER_GROUP_LABELS.vacant },
]

export function UserGroupTabs({ group, deleted, counts, onGroupChange }: UserGroupTabsProps) {
  const total = counts ? counts.pending + counts.normal + counts.vacant : undefined
  const countOf = (key?: UserGroup) => (key === undefined ? total : counts?.[key])
  return (
    <div className="app-group-tabs" role="group" aria-label="用户分组">
      {TABS.map((tab) => {
        const active = !deleted && group === tab.key
        const count = countOf(tab.key)
        return (
          <Button
            key={tab.key ?? 'all'}
            size="sm"
            variant={active ? 'primary' : 'ghost'}
            aria-pressed={active}
            onClick={() => onGroupChange(tab.key)}
          >
            {tab.label}
            {count !== undefined ? <span className="app-group-tab-count">{count}</span> : null}
          </Button>
        )
      })}
    </div>
  )
}
