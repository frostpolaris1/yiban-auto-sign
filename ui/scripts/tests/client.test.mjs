import assert from 'node:assert/strict'
import test from 'node:test'

import { ApiError, apiRequest } from '../../src/lib/api/client.ts'

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

test('unsafe methods send the stored CSRF token, safe methods do not', async () => {
  const { setCsrfToken } = await import('../../src/lib/api/client.ts')
  const capture = captureFetch()
  try {
    setCsrfToken('csrf-value')
    await apiRequest('/api/accounts', { method: 'POST', body: '{}' })
    await apiRequest('/api/accounts')
  } finally {
    capture.restore()
    setCsrfToken(null)
  }

  const postHeaders = new Headers(capture.calls[0].init.headers)
  assert.equal(postHeaders.get('X-CSRF-Token'), 'csrf-value')
  const getHeaders = new Headers(capture.calls[1].init.headers)
  assert.equal(getHeaders.get('X-CSRF-Token'), null)
})

test('a caller header wins over the stored CSRF token', async () => {
  const { setCsrfToken } = await import('../../src/lib/api/client.ts')
  const capture = captureFetch()
  try {
    setCsrfToken('stored')
    await apiRequest('/api/accounts', { method: 'DELETE', headers: { 'X-CSRF-Token': 'explicit' } })
  } finally {
    capture.restore()
    setCsrfToken(null)
  }

  const headers = new Headers(capture.calls[0].init.headers)
  assert.equal(headers.get('X-CSRF-Token'), 'explicit')
})

test('login stores the CSRF token returned in the body', async () => {
  const client = await import('../../src/lib/api/client.ts')
  const { login } = await import('../../src/lib/api/auth.ts')
  const calls = []
  const original = globalThis.fetch
  globalThis.fetch = async (input, init) => {
    calls.push({ input, init })
    return new Response(JSON.stringify({ csrf_token: 'from-login' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }
  try {
    client.setCsrfToken(null)
    await login({ email: 'a@example.com', password: 'secret' })
    assert.equal(client.getCsrfToken(), 'from-login')

    await client.apiRequest('/api/accounts', { method: 'POST', body: '{}' })
    const headers = new Headers(calls[1].init.headers)
    assert.equal(headers.get('X-CSRF-Token'), 'from-login')
  } finally {
    globalThis.fetch = original
    client.setCsrfToken(null)
  }
})

test('apiUpload keeps multipart boundaries and sends the CSRF token', async () => {
  const { apiUpload, setCsrfToken } = await import('../../src/lib/api/client.ts')
  const capture = captureFetch()
  try {
    setCsrfToken('csrf-upload')
    const form = new FormData()
    form.append('image', new Blob([new Uint8Array([1, 2, 3])], { type: 'image/png' }), 'synthetic.png')
    await apiUpload('/api/me/photos', form)
  } finally {
    capture.restore()
    setCsrfToken(null)
  }

  const [call] = capture.calls
  assert.equal(call.init.method, 'POST')
  const headers = new Headers(call.init.headers)
  assert.equal(
    headers.get('Content-Type'),
    null,
    'a hand-written JSON type would drop the multipart boundary',
  )
  assert.equal(headers.get('X-CSRF-Token'), 'csrf-upload')
  assert.ok(call.init.body instanceof FormData)
  assert.ok(call.init.body.get('image') instanceof Blob)
})

test('uploadPhoto puts the file under the image field', async () => {
  const { uploadPhoto } = await import('../../src/lib/api/photo.ts')
  const capture = captureFetch()
  try {
    const file = new File([new Uint8Array([1, 2, 3])], 'synthetic.png', { type: 'image/png' })
    await uploadPhoto(file)
  } finally {
    capture.restore()
  }

  const body = capture.calls[0].init.body
  assert.ok(body instanceof FormData)
  assert.equal([...body.keys()].join(','), 'image')
  assert.ok(body.get('image') instanceof File)
})

/**
 * Conditional GET cache.
 *
 * The invariant: a 304 answer hands back the exact object from the earlier
 * 200, so TanStack structural sharing keeps the same rows. Requests carry the
 * cached ETag only for GET; an explicit caller header always wins.
 */

function queueFetch(responses) {
  const calls = []
  const original = globalThis.fetch
  globalThis.fetch = async (input, init) => {
    calls.push({ input, init })
    const next = responses.shift()
    if (!next) throw new Error(`unexpected fetch: ${String(input)}`)
    return next()
  }
  return {
    calls,
    restore() {
      globalThis.fetch = original
    },
  }
}

function jsonResponse(body, etag, status = 200) {
  return () =>
    new Response(JSON.stringify(body), {
      status,
      headers: {
        'Content-Type': 'application/json',
        ...(etag ? { ETag: etag } : {}),
      },
    })
}

function notModified(etag) {
  return () => new Response(null, { status: 304, headers: { ETag: etag } })
}

function sentHeader(call, name) {
  return new Headers(call.init.headers).get(name)
}

test('a GET reuses the cached body on 304 and sends If-None-Match', async () => {
  const path = '/api/conditional-reuse'
  const first = { items: [{ id: 'a' }] }
  const capture = queueFetch([jsonResponse(first, '"v1"'), notModified('"v1"')])
  let one
  let two
  try {
    one = await apiRequest(path)
    two = await apiRequest(path)
  } finally {
    capture.restore()
  }

  assert.deepEqual(one, first)
  assert.equal(two, one, '304 must return the same object reference')
  assert.equal(sentHeader(capture.calls[0], 'If-None-Match'), null)
  assert.equal(sentHeader(capture.calls[1], 'If-None-Match'), '"v1"')
})

test('a 200 with a new ETag refreshes the cached body', async () => {
  const path = '/api/conditional-refresh'
  const capture = queueFetch([
    jsonResponse({ n: 1 }, '"v1"'),
    // The server changed: the old validator misses and a new body arrives.
    jsonResponse({ n: 2 }, '"v2"'),
    notModified('"v2"'),
  ])
  const versions = []
  try {
    versions.push(await apiRequest(path))
    versions.push(await apiRequest(path))
    versions.push(await apiRequest(path))
  } finally {
    capture.restore()
  }

  assert.deepEqual(versions[1], { n: 2 })
  assert.equal(versions[2], versions[1], 'the refreshed body is what 304 reuses')
  assert.equal(sentHeader(capture.calls[1], 'If-None-Match'), '"v1"')
  assert.equal(sentHeader(capture.calls[2], 'If-None-Match'), '"v2"')
})

test('a write request never carries If-None-Match', async () => {
  const path = '/api/conditional-write'
  const capture = queueFetch([
    jsonResponse({ n: 1 }, '"v1"'),
    jsonResponse({ ok: true }, null, 201),
  ])
  try {
    await apiRequest(path)
    await apiRequest(path, { method: 'POST', body: '{}' })
  } finally {
    capture.restore()
  }

  assert.equal(sentHeader(capture.calls[1], 'If-None-Match'), null)
})

test('an explicit If-None-Match header wins over the cache', async () => {
  const path = '/api/conditional-explicit'
  const capture = queueFetch([
    jsonResponse({ n: 1 }, '"v1"'),
    jsonResponse({ n: 2 }, '"v2"'),
    notModified('"v2"'),
  ])
  try {
    await apiRequest(path)
    await apiRequest(path, { headers: { 'If-None-Match': '"manual"' } })
    await apiRequest(path)
  } finally {
    capture.restore()
  }

  assert.equal(sentHeader(capture.calls[1], 'If-None-Match'), '"manual"')
  assert.equal(sentHeader(capture.calls[2], 'If-None-Match'), '"v2"')
})

test('a 304 without a cached body is an error', async () => {
  const capture = queueFetch([notModified('"v1"')])
  try {
    await assert.rejects(apiRequest('/api/conditional-miss'), (error) => {
      return error instanceof ApiError && error.status === 304
    })
  } finally {
    capture.restore()
  }
})

test('a 200 without ETag drops the cached entry', async () => {
  const path = '/api/conditional-noetag'
  const capture = queueFetch([
    jsonResponse({ n: 1 }, '"v1"'),
    jsonResponse({ n: 2 }, null),
    jsonResponse({ n: 3 }, null),
  ])
  try {
    await apiRequest(path)
    await apiRequest(path)
    await apiRequest(path)
  } finally {
    capture.restore()
  }

  assert.equal(sentHeader(capture.calls[1], 'If-None-Match'), '"v1"')
  assert.equal(
    sentHeader(capture.calls[2], 'If-None-Match'),
    null,
    'a response without ETag must not keep a stale validator',
  )
})

test('the cache keeps at most one hundred entries and drops the oldest', async () => {
  const responses = []
  for (let i = 0; i < 101; i += 1) {
    responses.push(jsonResponse({ i }, `"e${i}"`))
  }
  responses.push(jsonResponse({ i: 0 }, '"e0"'))
  const capture = queueFetch(responses)
  try {
    for (let i = 0; i < 101; i += 1) {
      await apiRequest(`/api/conditional-cap-${i}`)
    }
    await apiRequest('/api/conditional-cap-0')
  } finally {
    capture.restore()
  }

  assert.equal(
    sentHeader(capture.calls[101], 'If-None-Match'),
    null,
    'the oldest entry is evicted once the cap is exceeded',
  )
})

test('an explicit If-None-Match 304 never reuses the cached body', async () => {
  const path = '/api/conditional-explicit-304'
  const capture = queueFetch([
    jsonResponse({ n: 1 }, '"v1"'),
    // The explicit validator matched on the server; the local cache holds
    // another validator, so its body is not provably the answer.
    notModified('"manual"'),
  ])
  try {
    await apiRequest(path)
    await assert.rejects(
      apiRequest(path, { headers: { 'If-None-Match': '"manual"' } }),
      (error) => error instanceof ApiError && error.status === 304,
    )
  } finally {
    capture.restore()
  }

  assert.equal(sentHeader(capture.calls[1], 'If-None-Match'), '"manual"')
})

test('a 304 hit refreshes the cache order so polling keys survive eviction', async () => {
  const responses = []
  for (let i = 0; i < 100; i += 1) {
    responses.push(jsonResponse({ i }, `"e${i}"`))
  }
  // One revalidation of the oldest key, then one insert past the cap.
  responses.push(notModified('"e0"'))
  responses.push(jsonResponse({ i: 100 }, '"e100"'))
  // The checks: key 0 must still be cached, key 1 must have been evicted.
  responses.push(jsonResponse({ i: 0 }, '"e0"'))
  responses.push(jsonResponse({ i: 1 }, '"e1"'))

  const capture = queueFetch(responses)
  try {
    for (let i = 0; i < 100; i += 1) {
      await apiRequest(`/api/conditional-order-${i}`)
    }
    await apiRequest('/api/conditional-order-0')
    await apiRequest('/api/conditional-order-100')
    await apiRequest('/api/conditional-order-0')
    await apiRequest('/api/conditional-order-1')
  } finally {
    capture.restore()
  }

  assert.equal(sentHeader(capture.calls[100], 'If-None-Match'), '"e0"')
  assert.equal(
    sentHeader(capture.calls[102], 'If-None-Match'),
    '"e0"',
    'a revalidated key must be moved to the newest position',
  )
  assert.equal(
    sentHeader(capture.calls[103], 'If-None-Match'),
    null,
    'the key that was actually oldest is evicted',
  )
})
