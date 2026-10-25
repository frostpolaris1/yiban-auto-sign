import assert from 'node:assert/strict'
import test from 'node:test'

import { queryClient } from '../../src/lib/query.ts'

/**
 * The query client must not poll by default.
 *
 * A global `refetchInterval` applies to every query, including the admin
 * screens that will use SSE. Pages that need polling opt in locally.
 */

test('global query defaults carry no refetchInterval', () => {
  const defaults = queryClient.getDefaultOptions()
  assert.equal(Object.hasOwn(defaults.queries ?? {}, 'refetchInterval'), false)
  assert.equal(defaults.queries?.refetchInterval, undefined)
})

test('query defaults keep a stale window and disable focus refetch', () => {
  const defaults = queryClient.getDefaultOptions().queries
  assert.equal(defaults?.staleTime, 30_000)
  assert.equal(defaults?.refetchOnWindowFocus, false)
})
