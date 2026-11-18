/**
 * **功能**
 * 审计筛选栏：动作、操作者、起止日期与清除。
 *
 * **归属**
 * `features/audit` 展示层：只受控渲染，不改筛选状态。
 *
 * **口径**
 * 控件用 `FilterField` 壳：标签、错误与 `aria-describedby` 关联由壳合并注入。
 * 动作与操作者是文本条件，防抖由调用方持有；回车立即提交。
 * 起止日期用原生 `<input type="date">`（不引日期库）；错误只挂在出错字段，
 * 草稿值保留以便改正；成对提交与区间校验都在调用方。
 * 「清除筛选」是唯一主入口，空态不再重复放清除按钮。
 */

import { Button } from '@/components/ui/Button'
import { FilterField } from '@/components/ui/FilterField'
import { Icon } from '@/components/ui/Icon'
import type { AuditFiltersControls } from './useAuditFilters'

type AuditFiltersProps = {
  controls: AuditFiltersControls
}

export function AuditFilters({ controls }: AuditFiltersProps) {
  const fromError = controls.error?.field === 'from' ? controls.error.message : undefined
  const toError = controls.error?.field === 'to' ? controls.error.message : undefined

  return (
    <div className="a-filters" role="search" aria-label="审计筛选">
      <div className="a-field-item a-field-text">
        <FilterField id="audit-filter-action" label="动作筛选">
          <div className="input-icon">
            <span className="ico">
              <Icon name="search" size={14} />
            </span>
            <input
              className="input a-input a-input-search"
              type="search"
              // 包装层吃掉了 FilterField 注入的 id，label 落不到 input；
              // 照账号筛选与队列搜索的先例直接命名。
              aria-label="动作筛选"
              placeholder="例如 auth.login"
              value={controls.actionInput}
              onChange={(event) => controls.setActionInput(event.target.value)}
              onKeyDown={(event) => {
                // 输入法用回车选词时不能当成提交：先看组合态。
                if (event.key !== 'Enter' || event.nativeEvent.isComposing) return
                event.preventDefault()
                controls.commitNow()
              }}
            />
          </div>
        </FilterField>
      </div>

      <div className="a-field-item a-field-actor">
        <FilterField id="audit-filter-actor" label="操作者筛选">
          <input
            className="input a-input"
            type="search"
            placeholder="邮箱或掩码"
            value={controls.actorInput}
            onChange={(event) => controls.setActorInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== 'Enter' || event.nativeEvent.isComposing) return
              event.preventDefault()
              controls.commitNow()
            }}
          />
        </FilterField>
      </div>

      <div className="a-field-item a-field-date">
        <FilterField id="audit-filter-from" label="起始日期" error={fromError}>
          <input
            className="input a-input a-date"
            type="date"
            value={controls.fromDraft}
            onChange={(event) => controls.setFrom(event.target.value)}
          />
        </FilterField>
      </div>

      <div className="a-field-item a-field-date">
        <FilterField id="audit-filter-to" label="结束日期" error={toError}>
          <input
            className="input a-input a-date"
            type="date"
            value={controls.toDraft}
            onChange={(event) => controls.setTo(event.target.value)}
          />
        </FilterField>
      </div>

      {controls.hasActive ? (
        <div className="a-filter-actions">
          <Button variant="ghost" size="sm" className="a-clear" onClick={controls.clear}>
            清除筛选
          </Button>
        </div>
      ) : null}
    </div>
  )
}

