/**
 * **功能**
 * className 拼接：过滤假值，用一个空格连接。
 *
 * **归属**
 * `lib` 工具层：纯计算，不导入任何上层模块。
 *
 * **口径**
 * 只接受字符串与假值。数字与对象不参与。
 * 本批不引 clsx，保持零依赖。
 */

export type ClassValue = string | false | null | undefined

/**
 * 拼接类名。**空串与假值不产生多余空格**。
 * 调用点可以写 `cond && 'x'`，不必先过滤。
 */
export function cx(...values: ClassValue[]): string {
  return values.filter(Boolean).join(' ')
}
