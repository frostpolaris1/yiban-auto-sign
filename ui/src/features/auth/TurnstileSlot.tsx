/**
 * **功能**
 * 注册页的人机验证位：注册状态带站点密钥时加载 Cloudflare Turnstile 并渲染控件，
 * 把令牌回调给表单；没有站点密钥时整块不渲染（含占位），等同该功能不存在。
 *
 * **归属**
 * `features/auth` 域组件，由 `RegisterForm` 在协议勾选之后、提交按钮之前渲染。
 *
 * **口径**
 * 站点密钥是公开值，随 `RegistrationStatus.turnstile_site_key` 下发；服务端在 on 档
 * 校验令牌，本组件只负责拿到它并原样回传，不做任何放行判断。
 * 脚本按需加载一次（`?render=explicit`），失败时就地给「重新加载」按钮，不让用户
 * 只能刷新整页；加载/失败状态经 `onStatusChange` 上抛，父组件据此换提示文案。
 * 主题跟随 `useTheme()`：切主题会重建控件，否则 iframe 里还是旧配色。
 * 重挂载会先 `remove` 旧控件，避免 StrictMode 双执行留下两个。
 * 令牌是短时凭据：只存在组件状态里，不写 localStorage，也不进控制台。
 * 未配置站点密钥时返回 null：不显示控件、不显示占位、也不占版面高度。
 */

import { useEffect, useRef, useState } from 'react'
import { useTheme } from '@/lib/theme'

const SCRIPT_SRC = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit'

type TurnstileRenderOptions = {
  sitekey: string
  theme?: 'light' | 'dark' | 'auto'
  callback?: (token: string) => void
  'expired-callback'?: () => void
  'error-callback'?: () => void
}

type TurnstileApi = {
  render: (container: HTMLElement, options: TurnstileRenderOptions) => string
  remove: (widgetId: string) => void
}

declare global {
  interface Window {
    turnstile?: TurnstileApi
  }
}

/** 脚本加载上限：CDN 既不 onload 也不 onerror 时，不能把表单永远锁在「加载中」。 */
const LOAD_TIMEOUT_MS = 10_000

/** 脚本只加载一次；失败后清空句柄并移除脚本，允许下次挂载重试。 */
let scriptPromise: Promise<TurnstileApi> | null = null

function loadTurnstile(): Promise<TurnstileApi> {
  if (window.turnstile) return Promise.resolve(window.turnstile)
  if (scriptPromise) return scriptPromise
  const promise = new Promise<TurnstileApi>((resolve, reject) => {
    const script = document.createElement('script')
    script.src = SCRIPT_SRC
    script.async = true
    let settled = false
    let timer = 0
    const fail = (message: string) => {
      if (settled) return
      settled = true
      window.clearTimeout(timer)
      // 失败就把脚本摘掉：留在 head 里会让下一次挂载复用一个永远不完成的请求。
      script.remove()
      reject(new Error(message))
    }
    timer = window.setTimeout(() => fail('turnstile script timed out'), LOAD_TIMEOUT_MS)
    script.onload = () => {
      if (settled) return
      if (!window.turnstile) {
        fail('turnstile script loaded without the API')
        return
      }
      settled = true
      window.clearTimeout(timer)
      resolve(window.turnstile)
    }
    script.onerror = () => fail('turnstile script failed to load')
    document.head.appendChild(script)
  }).catch((error: unknown) => {
    scriptPromise = null
    throw error
  })
  scriptPromise = promise
  return promise
}

type TurnstileSlotProps = {
  /** 站点密钥；空串或缺失即未启用人机验证，组件不渲染任何内容。 */
  siteKey?: string
  /** 令牌回调：拿到令牌、过期或出错时都会调用，出错/过期回 null。 */
  onToken: (token: string | null) => void
  /**
   * 换新控件用的计数：+1 即卸载旧控件、清空令牌并重新渲染。
   * 令牌一次性，提交失败后服务端已消费掉它，父组件据此要求用户重新过验证。
   */
  resetSignal?: number
  /** 加载状态上抛：父组件据此在「还没加载完」与「加载失败」时给不同提示。 */
  onStatusChange?: (status: TurnstileStatus) => void
}

export type TurnstileStatus = 'idle' | 'loading' | 'ready' | 'failed'

export function TurnstileSlot({
  siteKey,
  onToken,
  resetSignal = 0,
  onStatusChange,
}: TurnstileSlotProps) {
  const hostRef = useRef<HTMLDivElement>(null)
  const { theme } = useTheme()
  const [state, setState] = useState<TurnstileStatus>('idle')
  // 失败后的「重新加载」不改 siteKey，靠这个计数器重跑同一条加载路径。
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    onStatusChange?.(state)
  }, [state, onStatusChange])

  useEffect(() => {
    if (!siteKey) {
      setState('idle')
      return
    }
    let cancelled = false
    let widgetId: string | null = null
    setState('loading')
    loadTurnstile()
      .then((api) => {
        if (cancelled || hostRef.current === null) return
        // 替身/旧控件可能留下子节点：渲染前清空，避免重挂载出现两个控件。
        hostRef.current.replaceChildren()
        widgetId = api.render(hostRef.current, {
          sitekey: siteKey,
          theme,
          callback: (token) => {
            if (cancelled) return
            // 控件内重试成功后要退出失败态，否则提示一直挂着。
            setState('ready')
            onToken(token)
          },
          'expired-callback': () => {
            if (!cancelled) onToken(null)
          },
          'error-callback': () => {
            if (cancelled) return
            setState('failed')
            onToken(null)
          },
        })
        if (!cancelled) setState('ready')
      })
      .catch((error: unknown) => {
        // 只记脚本自身的失败原因（超时/被拦/缺 API），令牌不经过这条路径。
        if (!cancelled) {
          console.warn('[turnstile] script load failed:', error)
          setState('failed')
        }
      })
    return () => {
      cancelled = true
      // 卸载/换控件时令牌一律作废：旧控件的令牌已经不能再用。
      onToken(null)
      if (widgetId !== null) window.turnstile?.remove(widgetId)
    }
    // 主题变了要重建控件：theme 是 render 时的一次性参数。
  }, [siteKey, onToken, resetSignal, theme, attempt])

  // 没有站点密钥 = 该功能未启用：整块不渲染，版面里不留空位，也不占高度。
  if (!siteKey) return null

  return (
    <div className="app-turnstile-status">
      <div className="app-turnstile-slot" ref={hostRef} data-turnstile-slot="register" />
      {state === 'loading' ? (
        <p className="app-turnstile-note" role="status">
          人机验证加载中…
        </p>
      ) : null}
      {state === 'failed' ? (
        <div className="app-turnstile-note app-turnstile-note--error" role="alert">
          <span>人机验证加载失败，请检查网络后重试</span>
          <button
            type="button"
            className="app-inline-link app-turnstile-retry"
            onClick={() => setAttempt((count) => count + 1)}
          >
            重新加载
          </button>
        </div>
      ) : null}
    </div>
  )
}
