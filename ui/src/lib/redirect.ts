/**
 * **功能**
 * 登录后回跳地址的净化：只放行站内相对路径。
 *
 * **归属**
 * `lib` 工具层：纯函数，无 React、无网络。
 *
 * **口径**
 * 空值、非字符串、站外绝对地址、协议相对地址（`//host`）、
 * 脚本协议、含反斜杠的变体一律返回 `null`。
 * 判定用 `URL` 解析后的 origin 比对，不做字符串前缀匹配。
 * 反斜杠会被浏览器当作路径分隔符，故先单独拒绝。
 */

/** 解析基准。任取一个不可路由的保留域名，只用于比对 origin。 */
const SAME_SITE_BASE = 'http://ui.invalid'

export function sanitizeRedirect(value: unknown): string | null {
  if (typeof value !== 'string' || value === '') return null
  if (!value.startsWith('/')) return null
  if (value.includes('\\')) return null

  let url: URL
  try {
    url = new URL(value, SAME_SITE_BASE)
  } catch {
    return null
  }
  if (url.origin !== SAME_SITE_BASE) return null
  // 点段规范化可把 `/.//evil.com` 变成 pathname `//evil.com`：
  // origin 仍是基准域，但返回的串再被用作相对导航时是协议相对地址。
  if (url.pathname.startsWith('//')) return null

  return `${url.pathname}${url.search}${url.hash}`
}
