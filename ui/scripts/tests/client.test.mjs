import assert from 'node:assert/strict'
import test from 'node:test'

import { apiRequest } from '../../src/lib/api/client.ts'

/**
 * Fetch-level checks for apiRequest.
 *
 * The invariant: a caller-supplied header never removes the defaults.
 * Node 24 runs this file with type stripping; the import names the .ts file
 * explicitly so the loader resolves it.
 */

function captureFetch() {
  const calls = []
  const original = globalThis.fetch
  globalThis.fetch = async (input, init) => {
    calls.push({ input, init })
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }
  return {
    calls,
    restore() {
      globalThis.fetch = original
    },
  }
}

test('a custom header keeps Accept and Content-Type', async () => {
  const capture = captureFetch()
  try {
    await apiRequest('/api/accounts', {
      method: 'POST',
      body: JSON.stringify({ school_id: 's1' }),
      headers: { 'X-CSRF-Token': 't' },
    })
  } finally {
    capture.restore()
  }

  assert.equal(capture.calls.length, 1)
  const headers = new Headers(capture.calls[0].init.headers)
  assert.equal(headers.get('X-CSRF-Token'), 't')
  assert.equal(headers.get('Accept'), 'application/json')
  assert.equal(headers.get('Content-Type'), 'application/json')
})

test('a custom header keeps Accept on a body-less request', async () => {
  const capture = captureFetch()
  try {
    await apiRequest('/api/auth/me', { headers: { 'X-Trace': 'x' } })
  } finally {
    capture.restore()
  }

  const headers = new Headers(capture.calls[0].init.headers)
  assert.equal(headers.get('X-Trace'), 'x')
  assert.equal(headers.get('Accept'), 'application/json')
  assert.equal(headers.get('Content-Type'), null)
})

test('credentials default to include', async () => {
  const capture = captureFetch()
  try {
    await apiRequest('/api/auth/me')
  } finally {
    capture.restore()
  }
  assert.equal(capture.calls[0].init.credentials, 'include')
})
