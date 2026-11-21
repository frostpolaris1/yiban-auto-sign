/**
 * **功能**
 * 通知订阅卡：读取并即时保存当前管理员本人的 4 类 × 3 渠道订阅矩阵。
 *
 * **归属**
 * `features/settings` 展示层：状态与纯转换在 `notify-prefs.ts`。
 *
 * **口径**
 * 行式开关即切即存：切换任意一格发一次全量 `PUT /api/admin/notify`；
 * 成功弹出 toast 并失效重读 `['admin-notify']`；
 * 失败时不改缓存（开关留在原位），并在对应行内展示 `role="alert"` 文案。
 * 展示构建下读合成夹具、零网络请求，全部开关置灰并标明 `展示模式只读`。
 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AsyncState } from '@/components/ui/AsyncState'
import { Card } from '@/components/ui/Card'
import { SwitchControl } from '@/components/ui/SwitchControl'
import { notifySuccess } from '@/components/ui/toast'
import {
  getAdminNotify,
  setAdminNotify,
  type NotifyCategory,
  type NotifyChannel,
  type NotifyPref,
} from '@/lib/api/adminNotify'
import { showcaseAdminNotify } from '@/lib/showcase'
import { READ_ONLY_REASON, SHOWCASE } from '@/lib/showcase-mode'
import {
  ADMIN_NOTIFY_QUERY_KEY,
  NOTIFY_SAVED_TOAST,
  buildNotifyMatrix,
  buildNotifyUpdateBody,
  notifyReadErrorMessage,
  notifySaveErrorMessage,
} from './notify-prefs'
import './settings.css'

type NotifyPrefsCardProps = {
  className?: string
}

type PendingCell = {
  category: NotifyCategory
  channel: NotifyChannel
}

type RowError = {
  category: NotifyCategory
  message: string
}

export function NotifyPrefsCard({ className }: NotifyPrefsCardProps) {
  const queryClient = useQueryClient()
  const [pendingCell, setPendingCell] = useState<PendingCell | null>(null)
  const [rowError, setRowError] = useState<RowError | null>(null)

  const readOnlyReason = SHOWCASE ? READ_ONLY_REASON : null

  const query = useQuery({
    queryKey: ADMIN_NOTIFY_QUERY_KEY,
    queryFn: () => (SHOWCASE ? Promise.resolve(showcaseAdminNotify()) : getAdminNotify()),
    staleTime: SHOWCASE ? Infinity : 0,
    retry: false,
  })

  const mutation = useMutation({
    mutationFn: (variables: {
      items: readonly NotifyPref[]
      category: NotifyCategory
      channel: NotifyChannel
      nextEnabled: boolean
    }) =>
      setAdminNotify(
        buildNotifyUpdateBody(
          variables.items,
          variables.category,
          variables.channel,
          variables.nextEnabled,
        ),
      ),
    onSuccess: (data) => {
      setRowError(null)
      queryClient.setQueryData(ADMIN_NOTIFY_QUERY_KEY, data)
      void queryClient.invalidateQueries({ queryKey: ADMIN_NOTIFY_QUERY_KEY })
      notifySuccess(NOTIFY_SAVED_TOAST)
    },
    onError: (error, variables) => {
      // 不做乐观翻转：失败时缓存保持原值，开关留在原位并给出行内错误说明。
      setRowError({
        category: variables.category,
        message: notifySaveErrorMessage(error),
      })
    },
    onSettled: () => {
      setPendingCell(null)
    },
  })

  const items = query.data?.items ?? []
  const matrix = buildNotifyMatrix(items)

  const status = query.isPending
    ? 'loading'
    : query.isError
      ? 'error'
      : matrix.rows.length === 0
        ? 'empty'
        : 'data'

  return (
    <Card
      className={className}
      eyebrow="Notifications"
      title="通知订阅"
      action={
        mutation.isPending ? (
          <span className="s-notify-saving">
            <span className="spinner sm" aria-hidden="true" />
            保存中…
          </span>
        ) : (
          <span className="app-caption">{readOnlyReason ?? '仅本人接收 · 即时保存'}</span>
        )
      }
    >
      <p className="app-muted">
        控制发送给当前账号的系统通知渠道；仅影响你本人接收，切换开关即保存。
      </p>

      <AsyncState
        status={status}
        loadingLabel="加载通知订阅…"
        emptyLabel="暂无可配置的通知订阅项"
        error={query.isError ? notifyReadErrorMessage(query.error) : undefined}
        onRetry={() => {
          setRowError(null)
          void query.refetch()
        }}
      >
        <div className="s-notify-wrap">
          <table className="s-notify-table">
            <caption className="app-visually-hidden">通知订阅矩阵</caption>
            <thead>
              <tr>
                <th scope="col">通知类别</th>
                {matrix.channels.map((channel) => (
                  <th key={channel.id} scope="col">
                    {channel.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {matrix.rows.map((row) => {
                const descId = row.description ? `notify-desc-${row.category}` : undefined
                const activeRowError = rowError?.category === row.category ? rowError.message : null
                return (
                  <tr key={row.category}>
                    <th scope="row">
                      <div className="s-notify-cat-title">{row.label}</div>
                      {row.description ? (
                        <p className="s-notify-cat-desc" id={descId}>
                          {row.description}
                        </p>
                      ) : null}
                      {activeRowError ? (
                        <p className="s-notify-row-error" role="alert">
                          {activeRowError}
                        </p>
                      ) : null}
                    </th>
                    {matrix.channels.map((channel) => {
                      const enabled = row.cells[channel.id]
                      if (enabled === undefined) {
                        return (
                          <td key={channel.id} className="s-notify-cell">
                            <span className="s-notify-cell-label" aria-hidden="true">
                              {channel.label}
                            </span>
                            <span aria-hidden="true">—</span>
                            <span className="app-visually-hidden">{`${row.label} · ${channel.label} 不可用`}</span>
                          </td>
                        )
                      }
                      const isCellPending =
                        mutation.isPending &&
                        pendingCell?.category === row.category &&
                        pendingCell?.channel === channel.id
                      return (
                        <td key={channel.id} className="s-notify-cell">
                          <span className="s-notify-cell-label" aria-hidden="true">
                            {channel.label}
                          </span>
                          <SwitchControl
                            className="s-notify-switch"
                            label={`${row.label} · ${channel.label}`}
                            hideLabel
                            checked={enabled}
                            disabled={readOnlyReason !== null}
                            aria-disabled={mutation.isPending}
                            aria-busy={isCellPending}
                            aria-describedby={descId}
                            title={readOnlyReason ?? undefined}
                            onCheckedChange={(nextEnabled) => {
                              if (readOnlyReason !== null || mutation.isPending) return
                              setRowError(null)
                              setPendingCell({ category: row.category, channel: channel.id })
                              mutation.mutate({
                                items,
                                category: row.category,
                                channel: channel.id,
                                nextEnabled,
                              })
                            }}
                          />
                        </td>
                      )
                    })}
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        <p className="s-notify-note">
          每日失败摘要只控制管理员本人订阅；普通用户的签到失败邮件由其个人开关单独决定。
        </p>
      </AsyncState>
    </Card>
  )
}
