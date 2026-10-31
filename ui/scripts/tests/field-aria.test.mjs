import assert from 'node:assert/strict'
import test from 'node:test'

import { fieldDescribedBy, fieldErrorId, fieldHintId } from '../../src/lib/field-aria.ts'

/**
 * 字段壳 id 约定不变式。
 *
 * 标签、提示、错误的派生键只有一处来源（`lib/field-aria.ts`）。
 * 提示与错误互斥：控件只关联当前可见的那一条，错误优先。
 * 这条约定同时被 `FilterField` 与 `Input.tsx` 的字段壳使用。
 */

test('hint and error ids derive from the field id with fixed suffixes', () => {
  assert.equal(fieldHintId('demo-filter-keyword'), 'demo-filter-keyword-hint')
  assert.equal(fieldErrorId('demo-filter-keyword'), 'demo-filter-keyword-error')
  // 形状对 id 无假设：含点号与中文也只做拼接。
  assert.equal(fieldHintId('a.b'), 'a.b-hint')
})

test('describedby points at the error whenever one exists, hint otherwise', () => {
  const id = 'q'
  assert.equal(fieldDescribedBy(id, '按学校名筛选', '关键字不能为空'), 'q-error')
  assert.equal(fieldDescribedBy(id, undefined, '关键字不能为空'), 'q-error')
  assert.equal(fieldDescribedBy(id, '按学校名筛选', undefined), 'q-hint')
  assert.equal(fieldDescribedBy(id, undefined, undefined), undefined)
  // 空串与 undefined 同判：没有可见文本就是没有描述。
  assert.equal(fieldDescribedBy(id, '', ''), undefined)
  assert.equal(fieldDescribedBy(id, '按学校名筛选', ''), 'q-hint')
})

test('describedby switches sides only when visibility flips', () => {
  // 错误退场、提示在场：描述必须从错误换回提示。
  const withError = fieldDescribedBy('q', '按学校名筛选', '关键字不能为空')
  const afterError = fieldDescribedBy('q', '按学校名筛选', undefined)
  assert.notEqual(withError, afterError)
  assert.equal(withError, 'q-error')
  assert.equal(afterError, 'q-hint')
})
