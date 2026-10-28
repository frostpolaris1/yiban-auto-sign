import assert from 'node:assert/strict'
import test from 'node:test'

import { isSessionExpiry } from '../../src/lib/api/auth.ts'
import { sanitizeRedirect } from '../../src/lib/redirect.ts'
import { buildLoginBody, isLoginPath } from '../../src/features/auth/login.ts'

/**
 * 切片 3 纯逻辑不变式。
 *
 * redirect：只接受站内相对路径。站外、协议相对、脚本协议、反斜杠变体一律丢弃。
 * 登录请求体：只有契约的两个字段，草稿里的其他键不外泄。
 * 401 分类：登录接口是凭据错误，me 是会话探针，其余 401 才算会话过期。
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
