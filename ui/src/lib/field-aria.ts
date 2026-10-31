/**
 * **功能**
 * 字段壳的无障碍 id 约定：提示与错误的派生键、控件的描述指向。
 *
 * **归属**
 * `lib` 工具层：纯计算，不导入任何上层模块。
 *
 * **口径**
 * 后缀固定 `${id}-hint`、`${id}-error`，与 `Input.tsx` 字段壳同约定。
 * 提示与错误互斥：控件只关联当前可见的那一条，**错误优先**。
 * 两者都不可见时不产出指向，返回 `undefined`。
 */

export function fieldHintId(id: string): string {
  return `${id}-hint`
}

export function fieldErrorId(id: string): string {
  return `${id}-error`
}

/** 错误优先于提示；空串与 `undefined` 同判为「不可见」。 */
export function fieldDescribedBy(id: string, hint?: string, error?: string): string | undefined {
  if (error) return fieldErrorId(id)
  if (hint) return fieldHintId(id)
  return undefined
}
