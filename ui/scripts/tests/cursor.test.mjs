import assert from 'node:assert/strict'
import test from 'node:test'

import {
  currentCursor,
  emptyCursorStack,
  goNext,
  goPrevious,
  hasPreviousPage,
} from '../../src/lib/cursor.ts'

/**
 * 客户端游标栈的不变式。栈是共享原语：账号页与学校页同用一份。
 *
 * 上一页只能回到已访问过的游标。空游标不前进。
 * 改道后丢弃被放弃的后续页。输入不被就地修改。
 */

test('an untouched cursor stack has no previous page', () => {
  const stack = emptyCursorStack()
  assert.equal(currentCursor(stack), undefined)
  assert.equal(hasPreviousPage(stack), false)
})

test('next pushes the returned cursor and previous walks back to it', () => {
  const first = emptyCursorStack()
  const second = goNext(first, 'c2')
  assert.equal(currentCursor(second), 'c2')
  assert.equal(hasPreviousPage(second), true)
  assert.equal(currentCursor(first), undefined, 'goNext must not mutate its input')

  const back = goPrevious(second)
  assert.equal(currentCursor(back), undefined)
  assert.equal(hasPreviousPage(back), false)
})

test('previous never walks below the first page', () => {
  const stack = goPrevious(goPrevious(emptyCursorStack()))
  assert.equal(currentCursor(stack), undefined)
  assert.equal(hasPreviousPage(stack), false)
})

test('an empty next cursor does not open a new page', () => {
  const stack = goNext(emptyCursorStack(), '')
  assert.equal(currentCursor(stack), undefined)
  assert.equal(hasPreviousPage(stack), false)
})

test('a new branch drops the abandoned forward pages', () => {
  let stack = goNext(emptyCursorStack(), 'c2')
  stack = goNext(stack, 'c3')
  stack = goPrevious(stack)
  assert.equal(currentCursor(stack), 'c2')

  stack = goNext(stack, 'cx')
  assert.equal(currentCursor(stack), 'cx')
  assert.equal(currentCursor(goPrevious(stack)), 'c2', 'c3 must not be reachable again')
})
