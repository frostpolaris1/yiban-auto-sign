/**
 * **功能**
 * 主题状态：读取、切换、持久化，并写 `<html data-theme>`。
 *
 * **归属**
 * `lib` 工具层：上下文与存储都在此，UI 只消费。
 *
 * **口径**
 * 唯一 localStorage 键是 `dash26-theme`。
 * 只存 `light` 或 `dark`。**凭据永不写入此处**。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { flushSync } from 'react-dom'

export type Theme = 'light' | 'dark'

/**
 * UI 拥有的唯一 localStorage 键。
 * 只存主题字符串。写入凭据即违反隐私口径。
 */
export const THEME_STORAGE_KEY = 'dash26-theme'

function isTheme(value: unknown): value is Theme {
  return value === 'light' || value === 'dark'
}

function readStoredTheme(): Theme | null {
  // localStorage 可能被隐私模式禁用。读不到按未设置处理。
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY)
    return isTheme(value) ? value : null
  } catch {
    return null
  }
}

function readSystemTheme(): Theme {
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

/** 与 index.html 的引导脚本同算法。改一处必须改两处，否则首屏闪色。 */
export function resolveInitialTheme(): Theme {
  return readStoredTheme() ?? readSystemTheme()
}

function persistTheme(theme: Theme): void {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme)
  } catch {
    /* 持久化失败不阻断切换。内存中的主题照样生效。 */
  }
}

export type ToggleOrigin = { x: number; y: number }

/** 点击位置换算成圆形揭幕的原点：键盘触发落在按钮中心。 */
export function originFromEvent(event: {
  currentTarget: Element
  clientX: number
  clientY: number
  detail: number
}): ToggleOrigin {
  const rect = event.currentTarget.getBoundingClientRect()
  if (event.detail === 0) {
    return { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2) }
  }
  return { x: event.clientX, y: event.clientY }
}

type ViewTransitionDocument = Document & {
  startViewTransition?: (callback: () => void) => { ready: Promise<void>; finished: Promise<void> }
}

/** 圆要罩住最远的视口角。 */
function unfoldRadius(origin: ToggleOrigin): number {
  const w = window.innerWidth
  const h = window.innerHeight
  return Math.ceil(Math.hypot(Math.max(origin.x, w - origin.x), Math.max(origin.y, h - origin.y)))
}

function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}

type ThemeContextValue = {
  theme: Theme
  setTheme: (theme: Theme) => void
  toggleTheme: (origin?: ToggleOrigin, clipTarget?: string) => void
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(resolveInitialTheme)

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
  }, [theme])

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next)
    persistTheme(next)
  }, [])

  const toggleTheme = useCallback((origin?: ToggleOrigin, clipTarget?: string) => {
    const next: Theme = theme === 'dark' ? 'light' : 'dark'
    // 回调内先同步落 data-theme，再 flush 提交 React 状态：
    // 晚一步新快照就拍不到新主题。
    const update = () => {
      document.documentElement.setAttribute('data-theme', next)
      persistTheme(next)
      flushSync(() => setThemeState(next))
    }
    const start = (document as ViewTransitionDocument).startViewTransition
    if (!origin || typeof start !== 'function' || prefersReducedMotion()) {
      update()
      return
    }
    const transition = start.call(document, update)
    const r = unfoldRadius(origin)
    const clip = [
      `circle(0px at ${origin.x}px ${origin.y}px)`,
      `circle(${r}px at ${origin.x}px ${origin.y}px)`,
    ]
    const pseudo = clipTarget ?? '::view-transition-new(root)'
    transition.ready
      .then(() => {
        document.documentElement.animate(
          { clipPath: clip },
          {
            duration: 520,
            easing: 'cubic-bezier(0.4, 0.14, 0.3, 1)',
            fill: 'both',
            pseudoElement: pseudo,
          },
        )
        document.documentElement.animate(
          { opacity: [0, 1] },
          { duration: 120, easing: 'ease-out', fill: 'both', pseudoElement: pseudo },
        )
      })
      .catch(() => {
        /* 快照竞态失败保持直切结果，不重试动画。 */
      })
  }, [theme])

  const value = useMemo(() => ({ theme, setTheme, toggleTheme }), [theme, setTheme, toggleTheme])

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

/**
 * 取主题上下文。**必须在 ThemeProvider 内调用**。
 * 缺失时抛错，避免静默退回默认主题。
 */
export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext)
  if (value === null) {
    throw new Error('useTheme must be used inside ThemeProvider')
  }
  return value
}
