/**
 * **功能**
 * 走查工具共享底座：Playwright 入口、参数解析、登录（无 logout 副作用）。
 *
 * **归属**
 * `ui/tools` 本地走查工具链；不被 src 引用，不进 bundle。
 *
 * **口径**
 * 凭据一律由 --email/--password 或环境变量传入，仓库不落任何默认口令。
 * 登录不清会话：dev:mock 的会话是服务端全局标记，logout 会切换他人会话。
 * Git Bash(MSYS) 会把以 / 开头的参数转成 C:/Program Files/Git/...；unmsys 还原。
 */
import { pathToFileURL, fileURLToPath } from 'node:url'
import { join } from 'node:path'

/** ui 根目录（tools 的上一级），不写死盘符。 */
export const UI = fileURLToPath(new URL('..', import.meta.url))

export async function launch(opts) {
  const m = await import(pathToFileURL(join(UI, 'node_modules', 'playwright', 'index.mjs')).href)
  return m.chromium.launch(opts)
}

export const unmsys = (s) => s.replace(/^[A-Z]:\/Program Files\/Git(\/|$)/, '/')

export function arg(name, fallback = null) {
  const i = process.argv.indexOf(`--${name}`)
  return i >= 0 && process.argv[i + 1] ? unmsys(process.argv[i + 1]) : fallback
}

export function list(name, fallback) {
  const v = arg(name)
  return v ? v.split(',').map((s) => unmsys(s.trim())).filter(Boolean) : fallback
}

/** 登录一次；不清会话、不 logout，避免切换全局 mock 会话的副作用。 */
export async function login(page, base, email, password) {
  await page.goto(`${base}/login`, { waitUntil: 'load' })
  await page.locator('#login-email').fill(email)
  await page.locator('#login-password').fill(password)
  await page.locator('.auth-mode-pane[data-active="true"] .auth-submit').click()
  await page.waitForFunction(() => !['/login', '/register'].includes(location.pathname), null, { timeout: 20000 })
}

/** 凭据解析：参数优先，其次环境变量；缺失即报错退出，不用默认口令。 */
export function creds() {
  const email = arg('email') ?? process.env.UAPP_EMAIL ?? null
  const password = arg('password') ?? process.env.UAPP_PASSWORD ?? null
  if (!process.argv.includes('--no-login') && (!email || !password)) {
    console.error('需要 --email/--password 或 UAPP_EMAIL/UAPP_PASSWORD（合成凭据），或 --no-login。')
    process.exit(2)
  }
  return { email, password }
}

export const slug = (s) => s.replace(/^\//, '').replace(/[/?=&]/g, '-') || 'home'