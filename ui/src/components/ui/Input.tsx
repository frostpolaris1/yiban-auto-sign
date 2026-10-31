/**
 * **功能**
 * 表单控件：文本、下拉、多行。
 * 字段壳统一渲染标签、提示与错误。
 *
 * **归属**
 * `components` 通用件：只做呈现与无障碍关联。
 *
 * **口径**
 * 错误优先于提示。有错误时不再显示提示。
 * `aria-describedby` 只指向当前可见的那一条。
 */

import type { ComponentPropsWithoutRef, ReactNode } from 'react'
import { cx } from '@/lib/cx'
import { fieldDescribedBy, fieldErrorId, fieldHintId } from '@/lib/field-aria'
import { Icon, type IconName } from '@/components/ui/Icon'

type FieldShellProps = {
  id: string
  label: string
  hint?: string
  error?: string
  required?: boolean
  children: ReactNode
}

/**
 * 三个控件共用字段壳。标签、提示、错误的 id 都在这里生成。
 * 错误与提示互斥，故控件只关联当前可见的一条。
 */
function FieldShell({ id, label, hint, error, required, children }: FieldShellProps) {
  return (
    <div className="field">
      <label className="field-label" htmlFor={id}>
        {label}
        {required ? <span className="req">*</span> : null}
      </label>
      {children}
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

type CommonFieldProps = {
  id: string
  label: string
  hint?: string
  error?: string
}

export type TextInputProps = CommonFieldProps &
  Omit<ComponentPropsWithoutRef<'input'>, 'id'> & {
    icon?: IconName
    addon?: string
  }

export function TextInput({
  id,
  label,
  hint,
  error,
  required,
  icon,
  addon,
  className,
  ...rest
}: TextInputProps) {
  const describedBy = fieldDescribedBy(id, hint, error)
  const control = (
    <input
      id={id}
      className={cx('input', error && 'is-invalid', className)}
      required={required}
      aria-invalid={error ? true : undefined}
      aria-describedby={describedBy}
      {...rest}
    />
  )

  return (
    <FieldShell id={id} label={label} hint={hint} error={error} required={required}>
      {icon ? (
        <div className="input-icon">
          <span className="ico">
            <Icon name={icon} />
          </span>
          {control}
        </div>
      ) : addon ? (
        <div className="input-group">
          <span className="addon">{addon}</span>
          {control}
        </div>
      ) : (
        control
      )}
    </FieldShell>
  )
}

export type SelectInputProps = CommonFieldProps &
  Omit<ComponentPropsWithoutRef<'select'>, 'id'> & {
    options: Array<{ value: string; label: string }>
  }

export function SelectInput({ id, label, hint, error, required, options, className, ...rest }: SelectInputProps) {
  const describedBy = fieldDescribedBy(id, hint, error)
  return (
    <FieldShell id={id} label={label} hint={hint} error={error} required={required}>
      <select
        id={id}
        className={cx('select', error && 'is-invalid', className)}
        required={required}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        {...rest}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </FieldShell>
  )
}

export type TextAreaInputProps = CommonFieldProps & Omit<ComponentPropsWithoutRef<'textarea'>, 'id'>

export function TextAreaInput({ id, label, hint, error, required, className, ...rest }: TextAreaInputProps) {
  const describedBy = fieldDescribedBy(id, hint, error)
  return (
    <FieldShell id={id} label={label} hint={hint} error={error} required={required}>
      <textarea
        id={id}
        className={cx('textarea', error && 'is-invalid', className)}
        required={required}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        {...rest}
      />
    </FieldShell>
  )
}
