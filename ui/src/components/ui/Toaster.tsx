/**
 * **功能**
 * 全局 toast 宿主：位置、主题跟随，以及 Adminator 令牌到 sonner 槽位的映射。
 *
 * **归属**
 * `components` 通用件：只读主题上下文，不读域状态、不发请求。
 *
 * **口径**
 * 主题跟随 `useTheme` 的 `light`／`dark`，与 `<html data-theme>` 同源。
 * 右上角，落在 60px 顶栏下方 8px（`offset`）。
 * `richColors` 打开后 sonner 才读类型槽位；本件只映射常规三槽与成功三槽。
 * 成功配色对齐站内 `Alert.success`：`--success-soft` 底、`--t-base` 字、`--border` 边。
 * 颜色全部引用 `tokens.css` 的变量，不写颜色字面量。
 * 错误 toast 不启用：错误反馈留在发起的模态或表单内。
 */

import type { CSSProperties } from 'react'
import { Toaster as SonnerToaster } from 'sonner'
import { useTheme } from '@/lib/theme'

/** sonner 的变量名 → Adminator 令牌。改这里即改全部 toast 的配色。 */
const TOAST_TOKENS = {
  '--normal-bg': 'var(--bg-card)',
  '--normal-text': 'var(--t-base)',
  '--normal-border': 'var(--border)',
  '--success-bg': 'var(--success-soft)',
  '--success-text': 'var(--t-base)',
  '--success-border': 'var(--border)',
} as CSSProperties

/** 顶栏高 60px；toast 起点再留 8px，不压搜索框与账号菜单。 */
const TOAST_OFFSET = 68

/** **单实例**：挂在 Provider 内一次，所有页面共用。 */
export function Toaster() {
  const { theme } = useTheme()
  return (
    <SonnerToaster
      theme={theme}
      position="top-right"
      offset={TOAST_OFFSET}
      richColors
      toastOptions={{ style: TOAST_TOKENS }}
    />
  )
}
