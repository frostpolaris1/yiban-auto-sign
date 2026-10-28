/**
 * **功能**
 * 成功反馈的唯一出口：转交 sonner 的 `toast.success`。
 *
 * **归属**
 * `components` 通用件：不发请求，不读域状态。
 *
 * **口径**
 * 只有写操作成功才走这里。错误反馈留在模态或表单内，可回看，不改 toast。
 * 文案由调用方给，含操作结果与对象标识。颜色由 `Toaster` 的令牌映射决定。
 */

import { toast } from 'sonner'

/** **成功反馈**：调用即渲染一条短暂通知，不改页面内任何状态。 */
export function notifySuccess(text: string): void {
  toast.success(text)
}
