import assert from 'node:assert/strict'
import test from 'node:test'

import {
  SessionExpiredError,
  changePassword,
  isSessionExpiry,
} from '../../src/lib/api/auth.ts'
import { ApiError } from '../../src/lib/api/client.ts'
import { sanitizeRedirect } from '../../src/lib/redirect.ts'
import { buildLoginBody, isLoginPath } from '../../src/features/auth/login.ts'
import {
  buildForgotBody,
  buildResetBody,
  isValidResetToken,
  NETWORK_ERROR_MESSAGE,
  passwordErrorMessage,
} from '../../src/features/auth/password-reset.ts'

/**
 * 切片 3 纯逻辑不变式。
 *
 * redirect：只接受站内相对路径。站外、协议相对、脚本协议、反斜杠变体一律丢弃。
 * 登录请求体：只有契约的两个字段，草稿里的其他键不外泄。
 * 401 分类：登录接口是凭据错误，me 是会话探针，其余 401 才算会话过期。
 * 密码找回：令牌形状与请求体按契约；断网文案固定，不回显底层错误。
 */

test('sanitizeRedirect keeps same-site paths with their query and hash', () => {
  assert.equal(sanitizeRedirect('/accounts'), '/accounts')
  assert.equal(sanitizeRedirect('/accounts?cursor=c2'), '/accounts?cursor=c2')
  assert.equal(sanitizeRedirect('/accounts#top'), '/accounts#top')
  assert.equal(sanitizeRedirect('/'), '/')
})

test('sanitizeRedirect drops every off-site form', () => {
  const rejected = [
    '//evil.com',
    '//evil.com/path',
    'https://evil.com/x',
    'http://evil.com',
    'javascript:alert(1)',
    'data:text/html,hi',
    '/\\evil.com',
    '\\\\evil.com',
    'evil.com',
    '/\t//evil.com',
    '',
    null,
    undefined,
    42,
    {},
  ]
  for (const value of rejected) {
    assert.equal(sanitizeRedirect(value), null, `must reject ${JSON.stringify(value)}`)
  }
})

test('sanitizeRedirect treats an encoded path as a same-site path', () => {
  // %2F 在路径段里不是分隔符：浏览器不会跨站。原样保留。
  assert.equal(sanitizeRedirect('/%2F%2Fevil.com'), '/%2F%2Fevil.com')
})

test('sanitizeRedirect rejects dot-segment forms that normalize to a protocol-relative path', () => {
  // `/.//evil.com` 经 URL 规范化后 pathname 是 `//evil.com`：
  // origin 仍是基准域，但返回值再当相对导航用就是站外跳转。
  for (const value of ['/.//evil.com', '/..//evil.com', '/a/..//evil.com', '/%2e%2e//evil.com']) {
    assert.equal(sanitizeRedirect(value), null, `must reject ${value}`)
  }
})

test('buildLoginBody carries exactly the contract fields', () => {
  const body = buildLoginBody({
    email: 'a@example.com',
    password: ' pw ',
    remember: true,
    role: 'master',
  })
  assert.deepEqual(Object.keys(body).sort(), ['email', 'password'])
  assert.deepEqual(body, { email: 'a@example.com', password: ' pw ' })
})

test('only a business 401 counts as session expiry', () => {
  assert.equal(isSessionExpiry('/api/auth/login'), false)
  assert.equal(isSessionExpiry('/api/auth/me'), false)
  assert.equal(isSessionExpiry('/api/accounts'), true)
  assert.equal(isSessionExpiry('/api/accounts/acc-1/pause'), true)
})

test('every spelling the router treats as the login page is rejected as a redirect target', () => {
  // 路由器大小写不敏感、去尾斜杠、匹配前 URI 解码：这些拼写都会命中 /login。
  for (const target of [
    '/login',
    '/login/',
    '/login//',
    '/LOGIN',
    '/Login',
    '/login/.',
    '/login?x=1',
    '/%6Cogin',
    '/log%69n',
    '/%6C%6F%67%69%6E',
  ]) {
    assert.equal(isLoginPath(target), true, `must treat ${target} as login`)
  }
  for (const target of ['/', '/accounts', '/loginx', '/login-report', '/design']) {
    assert.equal(isLoginPath(target), false, `must not treat ${target} as login`)
  }
})

test('a reset token must have the shape the server generator produces', () => {
  // 32 字节经 base64url 无填充编码＝43 个字符。
  assert.equal(isValidResetToken('A'.repeat(43)), true)
  assert.equal(isValidResetToken('0'.repeat(43)), true)
  assert.equal(isValidResetToken(`${'A'.repeat(42)}-_`.slice(0, 43)), true)

  const rejected = [
    undefined,
    null,
    '',
    'A'.repeat(42),
    'A'.repeat(44),
    `${'A'.repeat(42)}!`,
    `${'A'.repeat(42)}=`,
    ` ${'A'.repeat(43)}`,
    `${'A'.repeat(43)}\n`,
  ]
  for (const value of rejected) {
    assert.equal(isValidResetToken(value), false, `must reject ${JSON.stringify(value)}`)
  }
})

test('the password reset bodies carry exactly the contract fields', () => {
  const forgot = buildForgotBody({ email: 'a@example.com', role: 'master' })
  assert.deepEqual(Object.keys(forgot).sort(), ['email'])
  assert.deepEqual(forgot, { email: 'a@example.com' })

  const token = 'A'.repeat(43)
  const reset = buildResetBody({ token, new_password: 'new-secret', confirmation: 'new-secret' })
  assert.deepEqual(Object.keys(reset).sort(), ['new_password', 'token'])
  assert.deepEqual(reset, { token, new_password: 'new-secret' })
})

test('a non-envelope failure becomes one fixed network message', () => {
  assert.equal(passwordErrorMessage(new TypeError('Failed to fetch')), NETWORK_ERROR_MESSAGE)
  assert.equal(
    passwordErrorMessage(new ApiError(400, { code: 'validation_error', message: 'invalid email' })),
    'validation_error · invalid email',
  )
})

test('a public reset 401 is not a session expiry', () => {
  // 两页都是 public 端点：401 只表示令牌问题，不得标记会话过期。
  assert.equal(isSessionExpiry('/api/auth/forgot-password'), false)
  assert.equal(isSessionExpiry('/api/auth/reset-password'), false)
})

const CHANGE_BODY = {
  current_password: 'not-a-real-password',
  new_password: 'not-a-real-new-password',
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function errorResponse(status, code, message) {
  return jsonResponse({ error: { code, message } }, status)
}

/** 按调用顺序回放响应；返回实际请求过的路径。 */
async function withFetch(responses, run) {
  const calls = []
  const original = globalThis.fetch
  globalThis.fetch = async (input) => {
    calls.push(String(input))
    return responses[calls.length - 1]
  }
  try {
    await run()
  } finally {
    globalThis.fetch = original
  }
  return calls
}

test('a change-password 401 probes me: a live session means a wrong password', async () => {
  const calls = await withFetch(
    [
      errorResponse(401, 'unauthorized', 'invalid email or password'),
      jsonResponse({ csrf_token: 'probe-token' }),
    ],
    async () => {
      await assert.rejects(
        changePassword(CHANGE_BODY),
        (error) => error instanceof ApiError && error.status === 401,
        'a live session keeps the 401 as a password field error',
      )
    },
  )

  assert.deepEqual(calls, ['/api/auth/change-password', '/api/auth/me'])
})

test('a change-password 401 with a dead session raises the session-expired signal', async () => {
  const calls = await withFetch(
    [
      errorResponse(401, 'unauthorized', 'authentication required'),
      errorResponse(401, 'unauthorized', 'authentication required'),
    ],
    async () => {
      await assert.rejects(
        changePassword(CHANGE_BODY),
        (error) => error instanceof SessionExpiredError,
        'a dead session must not look like a wrong current password',
      )
    },
  )

  assert.deepEqual(calls, ['/api/auth/change-password', '/api/auth/me'])
})

test('a non-401 change-password failure does not probe the session', async () => {
  const calls = await withFetch(
    [errorResponse(409, 'conflict', 'new password must differ from the current password')],
    async () => {
      await assert.rejects(
        changePassword(CHANGE_BODY),
        (error) => error instanceof ApiError && error.status === 409,
      )
    },
  )

  assert.deepEqual(calls, ['/api/auth/change-password'])
})

test('a probe failure that is not a 401 is reported, not guessed', async () => {
  const calls = await withFetch(
    [
      errorResponse(401, 'unauthorized', 'authentication required'),
      errorResponse(500, 'internal_error', 'session lookup failed'),
    ],
    async () => {
      await assert.rejects(
        changePassword(CHANGE_BODY),
        (error) => error instanceof ApiError && error.status === 500,
        'an inconclusive probe must not claim the session is dead',
      )
    },
  )

  assert.deepEqual(calls, ['/api/auth/change-password', '/api/auth/me'])
})
