export type ClassValue = string | false | null | undefined

/** Minimal className joiner. Replaces clsx for this batch. */
export function cx(...values: ClassValue[]): string {
  return values.filter(Boolean).join(' ')
}
