/**
 * **功能**
 * 密度状态：读取、切换、持久化，并写 `<html data-density>`。
 *
 * **归属**
 * `lib` 工具层：上下文与存储都在此，UI 只消费。形状与 `theme.tsx` 对齐。
 *
 * **口径**
 * 唯一 localStorage 键是 `dash26-density`，值只有 `standard` 或 `compact`。
 * 缺省即 `standard`（现几何）：不落盘、不覆盖，基线口径不变。
 * 只在用户显式切换后写入——首访会话不产生该键（gallery 白名单的核对点）。
 * 紧凑档只改行高与内边距，判据在 app.css 的覆盖层；本层只管状态。
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

export type Density = 'standard' | 'compact'

export const DENSITY_STORAGE_KEY = 'dash26-density'

function isDensity(value: unknown): value is Density {
  return value === 'standard' || value === 'compact'
}

function readStoredDensity(): Density {
  // localStorage 可能被隐私模式禁用。读不到按缺省档处理。
  try {
    const value = window.localStorage.getItem(DENSITY_STORAGE_KEY)
    return isDensity(value) ? value : 'standard'
  } catch {
    return 'standard'
  }
}

function persistDensity(density: Density): void {
  try {
    window.localStorage.setItem(DENSITY_STORAGE_KEY, density)
  } catch {
    /* 持久化失败不阻断切换。内存中的密度照样生效。 */
  }
}

type DensityContextValue = {
  density: Density
  setDensity: (density: Density) => void
  toggleDensity: () => void
}

const DensityContext = createContext<DensityContextValue | null>(null)

export function DensityProvider({ children }: { children: ReactNode }) {
  const [density, setDensityState] = useState<Density>(readStoredDensity)

  useEffect(() => {
    document.documentElement.setAttribute('data-density', density)
  }, [density])

  const setDensity = useCallback((next: Density) => {
    setDensityState(next)
    persistDensity(next)
  }, [])

  const toggleDensity = useCallback(() => {
    setDensityState((current) => {
      const next: Density = current === 'compact' ? 'standard' : 'compact'
      persistDensity(next)
      return next
    })
  }, [])

  const value = useMemo(
    () => ({ density, setDensity, toggleDensity }),
    [density, setDensity, toggleDensity],
  )

  return <DensityContext.Provider value={value}>{children}</DensityContext.Provider>
}

/**
 * 取密度上下文。**必须在 DensityProvider 内调用**。
 * 缺失时抛错，避免静默退回缺省档。
 */
export function useDensity(): DensityContextValue {
  const value = useContext(DensityContext)
  if (value === null) {
    throw new Error('useDensity must be used inside DensityProvider')
  }
  return value
}
