/**
 * **功能**
 * 字段壳：给外来控件（原生 input/select、Radix 触发器）补标签与描述关联。
 *
 * **归属**
 * `components` 通用件：只做呈现与无障碍关联。
 *
 * **口径**
 * 标签用 `htmlFor` 关联；提示与错误用 `aria-describedby`，错误优先，
 * id 派生取 `lib/field-aria.ts`。关联属性由本组件经 Radix `Slot` 合并进
 * 唯一子控件，调用方不手写 id，也不得在子控件上再传。
 * 错误渲染成 `role="alert"`，读屏可感知；有错误时不显示提示。
 */

import type { ReactElement } from 'react'
import { Slot } from 'radix-ui'
import { Icon } from '@/components/ui/Icon'
import { fieldDescribedBy, fieldErrorId, fieldHintId } from '@/lib/field-aria'

type FilterFieldProps = {
  id: string
  label: string
  hint?: string
  error?: string
  required?: boolean
  /** 唯一子控件。id、aria-invalid、aria-describedby 由本组件合并注入。 */
  children: ReactElement
}

export function FilterField({ id, label, hint, error, required, children }: FilterFieldProps) {
  return (
    <div className="field">
      <label className="field-label" htmlFor={id}>
        {label}
        {required ? <span className="req">*</span> : null}
      </label>
      <Slot.Root id={id} aria-invalid={error ? true : undefined} aria-describedby={fieldDescribedBy(id, hint, error)}>
        {children}
      </Slot.Root>
      {hint && !error ? (
        <div className="field-help" id={fieldHintId(id)}>
          {hint}
        </div>
      ) : null}
      {error ? (
        <div className="field-error" id={fieldErrorId(id)} role="alert">
          <Icon name="alertCircle" size={11} />
          {error}
        </div>
      ) : null}
    </div>
  )
}
