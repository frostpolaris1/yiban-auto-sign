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

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

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

type ThemeContextValue = {
  theme: Theme
  setTheme: (theme: Theme) => void
  toggleTheme: () => void
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

  const toggleTheme = useCallback(() => {
    setThemeState((previous) => {
      const next: Theme = previous === 'dark' ? 'light' : 'dark'
      persistTheme(next)
      return next
    })
  }, [])

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
