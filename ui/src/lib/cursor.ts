/**
 * **功能**
 * 客户端游标栈：记录已访问页的游标与当前位置。
 *
 * **归属**
 * `lib` 纯逻辑：账号页与学校页共用一份实现。无 React、无网络、无 DOM。
 *
 * **口径**
 * 第一页的游标是 `undefined`，恒在栈底。
 * `goNext` 只接受非空游标；空游标不前进。
 * `goPrevious` 不越过第一页。回退后前进会丢弃被放弃的后续页。
 */

export type CursorStack = {
  cursors: Array<string | undefined>
  index: number
}

export function emptyCursorStack(): CursorStack {
  return { cursors: [undefined], index: 0 }
}

export function currentCursor(stack: CursorStack): string | undefined {
  return stack.cursors[stack.index]
}

export function hasPreviousPage(stack: CursorStack): boolean {
  return stack.index > 0
}

export function goPrevious(stack: CursorStack): CursorStack {
  if (!hasPreviousPage(stack)) return stack
  return { cursors: stack.cursors, index: stack.index - 1 }
}

export function goNext(stack: CursorStack, nextCursor: string): CursorStack {
  if (!nextCursor) return stack
  const cursors = [...stack.cursors.slice(0, stack.index + 1), nextCursor]
  return { cursors, index: cursors.length - 1 }
}
