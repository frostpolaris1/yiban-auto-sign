/**
 * **功能**
 * 更新日志弹窗：认证页脚版本号点开，读 `GET /api/changelog` 的服务端 Markdown 源文本。
 *
 * **归属**
 * `features/auth` 域组件：外壳与取数在这里，渲染保持纯文本。
 *
 * **口径**
 * 服务端已决定返回源文本还是兜底句；前端按纯文本展示，不解析原始 HTML（XSS 口径）。
 * 打开即取数；失败留在弹窗内，给重试，不静默关窗。
 * 关闭后焦点回到版本按钮。
 */

import { useQuery } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { Modal } from '@/components/ui/Modal'
import { getChangelog } from '@/lib/api/changelog'
import { ApiError } from '@/lib/api/client'

function changelogErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 503) return '系统维护中，请稍后再试。'
    return '更新日志读取失败，请稍后再试。'
  }
  return '网络异常，请稍后再试。'
}

export function ChangelogDialog({
  open,
  onOpenChange,
  returnFocusTo,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  returnFocusTo: HTMLElement | null
}) {
  const query = useQuery({
    queryKey: ['changelog'],
    queryFn: getChangelog,
    enabled: open,
  })
  const body: ReactNode = query.isPending ? (
    <p className="app-changelog-text">加载中…</p>
  ) : query.isError ? (
    <p className="app-changelog-text" role="alert">
      {changelogErrorText(query.error)}
    </p>
  ) : (
    <pre className="app-changelog-text">{query.data.text}</pre>
  )

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title="更新日志"
      description="服务端 CHANGELOG 原文，按纯文本展示。"
      returnFocusTo={returnFocusTo}
      scrollBody
      footer={
        <>
          {query.isError ? (
            <button
              type="button"
              className="btn btn--secondary"
              onClick={() => void query.refetch()}
            >
              重试
            </button>
          ) : null}
          <button type="button" className="btn btn--secondary" onClick={() => onOpenChange(false)}>
            关闭
          </button>
        </>
      }
    >
      {body}
    </Modal>
  )
}
