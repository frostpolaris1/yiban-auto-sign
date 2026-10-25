import type { ComponentPropsWithoutRef, ReactNode } from 'react'
import { cx } from '@/lib/cx'
import { Icon, type IconName } from '@/components/ui/Icon'

type FieldShellProps = {
  id: string
  label: string
  hint?: string
  error?: string
  required?: boolean
  children: ReactNode
}

function FieldShell({ id, label, hint, error, required, children }: FieldShellProps) {
  return (
    <div className="field">
      <label className="field-label" htmlFor={id}>
        {label}
        {required ? <span className="req">*</span> : null}
      </label>
      {children}
      {hint && !error ? (
        <div className="field-help" id={`${id}-hint`}>
          {hint}
        </div>
      ) : null}
      {error ? (
        <div className="field-error" id={`${id}-error`} role="alert">
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
  const describedBy = error ? `${id}-error` : hint ? `${id}-hint` : undefined
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
  const describedBy = error ? `${id}-error` : hint ? `${id}-hint` : undefined
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
  const describedBy = error ? `${id}-error` : hint ? `${id}-hint` : undefined
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
