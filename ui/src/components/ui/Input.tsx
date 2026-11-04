/**
 * **功能**
 * 表单控件：文本、密码、下拉、多行。
 * 字段壳统一渲染标签、提示与错误。
 *
 * **归属**
 * `components` 通用件：只做呈现与无障碍关联。
 *
 * **口径**
 * 错误优先于提示。有错误时不再显示提示。
 * `aria-describedby` 只指向当前可见的那一条。
 */

import { useState, type ComponentPropsWithoutRef, type ReactNode } from 'react'
import { cx } from '@/lib/cx'
import { fieldDescribedBy, fieldErrorId, fieldHintId } from '@/lib/field-aria'
import { Icon, type IconName } from '@/components/ui/Icon'

type FieldShellProps = {
  id: string
  label: string
  hint?: string
  error?: string
  required?: boolean
  /** 标签行右侧的附加操作，例如「忘记密码」。 */
  labelAction?: ReactNode
  children: ReactNode
}

/**
 * 三个控件共用字段壳。标签、提示、错误的 id 都在这里生成。
 * 错误与提示互斥，故控件只关联当前可见的一条。
 */
function FieldShell({ id, label, hint, error, required, labelAction, children }: FieldShellProps) {
  const labelNode = (
    <label className="field-label" htmlFor={id}>
      {label}
      {required ? (
        <span className="req" aria-hidden="true">
          *
        </span>
      ) : null}
    </label>
  )

  return (
    <div className="field">
      {labelAction ? (
        <div className="field-row">
          {labelNode}
          {labelAction}
        </div>
      ) : (
        labelNode
      )}
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

export type PasswordInputProps = CommonFieldProps &
  Omit<ComponentPropsWithoutRef<'input'>, 'id' | 'type'> & {
    labelAction?: ReactNode
  }

/**
 * 密码框：右侧一枚显隐按钮。
 * 按钮不参与表单提交，`aria-pressed` 表示当前是否明文。
 */
export function PasswordInput({
  id,
  label,
  hint,
  error,
  required,
  className,
  labelAction,
  ...rest
}: PasswordInputProps) {
  const [visible, setVisible] = useState(false)
  const describedBy = fieldDescribedBy(id, hint, error)
  // 同一页可能有两枚密码框：按钮名带上字段标签，读屏不会听到两个「显示密码」。
  const visibilityLabel = `${visible ? '隐藏' : '显示'}${label}`

  return (
    <FieldShell
      id={id}
      label={label}
      hint={hint}
      error={error}
      required={required}
      labelAction={labelAction}
    >
      <div className="input-affix">
        <input
          id={id}
          type={visible ? 'text' : 'password'}
          className={cx('input', error && 'is-invalid', className)}
          required={required}
          spellCheck={false}
          autoCapitalize="off"
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy}
          {...rest}
        />
        <button
          type="button"
          className="affix-btn"
          aria-label={visibilityLabel}
          aria-pressed={visible}
          aria-controls={id}
          title={visibilityLabel}
          onClick={() => setVisible((current) => !current)}
        >
          <Icon name={visible ? 'eyeOff' : 'eye'} />
        </button>
      </div>
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
