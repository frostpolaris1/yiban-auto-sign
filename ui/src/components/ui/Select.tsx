/**
 * **功能**
 * 下拉选择：Radix Select + 令牌样式，触发器沿用 `.field/.select` 视觉。
 *
 * **归属**
 * `components/ui` 通用件：只负责呈现与键盘交互，不取数。
 *
 * **口径**
 * 与 `Input.SelectInput`（原生 select）并存：原生件仍是大部分筛选面的
 * 基线，本组件先供总览容量卡，迁移按页推进，避免一次性改全部断言。
 * 触发器带 `aria-label`，选项走 `role="option"`：读屏与 e2e 共用同一语义。
 * 菜单动画在 `prefers-reduced-motion` 下关闭。
 */

import { useId } from 'react'
import { Select as RSelect } from 'radix-ui'
import { Icon } from '@/components/ui/Icon'
import { cx } from '@/lib/cx'

export type SelectOption = { value: string; label: string }

type SelectProps = {
  id?: string
  label: string
  options: SelectOption[]
  value: string
  disabled?: boolean
  className?: string
  onChange: (value: string) => void
}

export function Select({ id, label, options, value, disabled, className, onChange }: SelectProps) {
  const autoId = useId()
  const controlId = id ?? autoId
  const current = options.find((option) => option.value === value)
  return (
    <div className={cx('field', className)}>
      <label className="field-label" htmlFor={controlId}>
        {label}
      </label>
      <RSelect.Root value={value} onValueChange={onChange} disabled={disabled}>
        <RSelect.Trigger id={controlId} className="select app-select-trigger" aria-label={label}>
          <span className="app-select-value">{current?.label ?? '—'}</span>
          <RSelect.Icon>
            <Icon name="chevronDown" size={14} />
          </RSelect.Icon>
        </RSelect.Trigger>
        <RSelect.Portal>
          <RSelect.Content
            className="app-select-content"
            position="popper"
            sideOffset={6}
            align="start"
          >
            <RSelect.Viewport className="app-select-viewport">
              {options.map((option) => (
                <RSelect.Item
                  key={option.value}
                  value={option.value}
                  className="app-select-item"
                >
                  <RSelect.ItemText>{option.label}</RSelect.ItemText>
                  <RSelect.ItemIndicator className="app-select-check">
                    <Icon name="check" size={13} />
                  </RSelect.ItemIndicator>
                </RSelect.Item>
              ))}
            </RSelect.Viewport>
          </RSelect.Content>
        </RSelect.Portal>
      </RSelect.Root>
    </div>
  )
}