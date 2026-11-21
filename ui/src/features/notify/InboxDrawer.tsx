/**
 * **功能**
 * 顶栏通知入口（铃铛＋未读角标）与通知收件箱右侧抽屉（游标分页、单条已读、全部已读）。
 *
 * **归属**
 * `features/notify` 域组件：对接 `/api/me/notifications*` 读/写端点。
 *
 * **口径**
 * 角色门：`user` 不渲染铃铛且零通知请求；只对 `master`/`admin` 挂载轮询与抽屉。
 * 缓存隔离：角标 `['notifications-unread']` 与列表 `['notifications', 'inbox']` 分键管理。
 * 30s 轮询未读数：页面隐藏或抽屉打开时自动暂停，关闭抽屉恢复。
 * 展示模式零网络请求：读合成夹具，全部写入口按 `READ_ONLY_REASON` 置灰。
 */

import { useRef, useState } from 'react'
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
} from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { AsyncState, type AsyncStatus } from '@/components/ui/AsyncState'
import { BusyButton } from '@/components/ui/BusyButton'
import { Button } from '@/components/ui/Button'
import { Drawer } from '@/components/ui/Drawer'
import { Icon } from '@/components/ui/Icon'
import type { SessionUser } from '@/lib/api/auth'
import { describeApiError } from '@/lib/api/client'
import {
  getUnreadCount,
  listNotifications,
  markAllNotificationsRead,
  markNotificationsRead,
  NOTIFICATION_PAGE_LIMIT,
  type NotificationPage,
} from '@/lib/api/notify'
import { INBOX_VIEW_ROLES, roleAllowed } from '@/lib/roles'
import { showcaseNotifications, showcaseUnreadCount } from '@/lib/showcase'
import { READ_ONLY_REASON, SHOWCASE } from '@/lib/showcase-mode'
import {
  flattenUniqueNotifications,
  formatUnreadBadge,
  NOTIFICATIONS_INBOX_QUERY_KEY,
  NOTIFICATIONS_UNREAD_QUERY_KEY,
  NOTIFY_UNREAD_POLL_MS,
  patchAllPagesRead,
  patchPagesRead,
} from './display'
import { InboxItem } from './InboxItem'

export function NotifyInbox({ me }: { me: SessionUser }) {
  if (!roleAllowed(me.role, INBOX_VIEW_ROLES)) return null
  return <ManagerNotifyInbox />
}

function ManagerNotifyInbox() {
  const [open, setOpen] = useState(false)
  const [returnFocusTo, setReturnFocusTo] = useState<HTMLElement | null>(null)

  const unreadQuery = useQuery({
    queryKey: NOTIFICATIONS_UNREAD_QUERY_KEY,
    queryFn: () =>
      SHOWCASE ? Promise.resolve(showcaseUnreadCount()) : getUnreadCount(),
    staleTime: 0,
    retry: false,
    refetchInterval: SHOWCASE || open ? false : NOTIFY_UNREAD_POLL_MS,
    refetchIntervalInBackground: false,
  })

  const unreadCount = unreadQuery.data?.count ?? 0
  const badgeText = formatUnreadBadge(unreadCount)

  return (
    <>
      <button
        type="button"
        className="icon-btn"
        aria-label={unreadCount > 0 ? `通知，${unreadCount} 条未读` : '通知'}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={(event) => {
          setReturnFocusTo(event.currentTarget)
          setOpen(true)
        }}
      >
        <Icon name="bell" />
        {badgeText !== null ? (
          <span className="count danger" aria-hidden="true">
            {badgeText}
          </span>
        ) : null}
      </button>

      <InboxDrawer
        open={open}
        unreadCount={unreadCount}
        returnFocusTo={returnFocusTo}
        onOpenChange={setOpen}
      />
    </>
  )
}

type InboxDrawerProps = {
  open: boolean
  unreadCount: number
  returnFocusTo: HTMLElement | null
  onOpenChange: (open: boolean) => void
}

export function InboxDrawer({
  open,
  unreadCount,
  returnFocusTo,
  onOpenChange,
}: InboxDrawerProps) {
  const queryClient = useQueryClient()
  const [actionError, setActionError] = useState<string | null>(null)
  const [markingIds, setMarkingIds] = useState<ReadonlySet<string>>(() => new Set())
  const fetchingMoreRef = useRef(false)

  const inboxQuery = useInfiniteQuery({
    queryKey: NOTIFICATIONS_INBOX_QUERY_KEY,
    initialPageParam: '',
    queryFn: ({ pageParam }) =>
      SHOWCASE
        ? Promise.resolve(showcaseNotifications())
        : listNotifications({
            limit: NOTIFICATION_PAGE_LIMIT,
            cursor: pageParam || undefined,
          }),
    getNextPageParam: (lastPage) =>
      lastPage.next_cursor ? lastPage.next_cursor : undefined,
    enabled: open,
    staleTime: 0,
    retry: false,
  })

  const markReadMutation = useMutation({
    mutationFn: (id: string) => markNotificationsRead([id]),
    onMutate: (id) => {
      setActionError(null)
      setMarkingIds((previous) => {
        const next = new Set(previous)
        next.add(id)
        return next
      })
    },
    onSuccess: (_data, id) => {
      const nowIso = new Date().toISOString()
      queryClient.setQueryData<InfiniteData<NotificationPage>>(
        NOTIFICATIONS_INBOX_QUERY_KEY,
        (previous) => (previous ? patchPagesRead(previous, [id], nowIso) : previous),
      )
      void queryClient.invalidateQueries({ queryKey: NOTIFICATIONS_UNREAD_QUERY_KEY })
    },
    onError: (error) => {
      setActionError(describeApiError(error))
    },
    onSettled: (_data, _error, id) => {
      setMarkingIds((previous) => {
        if (!previous.has(id)) return previous
        const next = new Set(previous)
        next.delete(id)
        return next
      })
    },
  })

  const markAllMutation = useMutation({
    mutationFn: markAllNotificationsRead,
    onMutate: () => {
      setActionError(null)
    },
    onSuccess: () => {
      const nowIso = new Date().toISOString()
      queryClient.setQueryData<InfiniteData<NotificationPage>>(
        NOTIFICATIONS_INBOX_QUERY_KEY,
        (previous) => (previous ? patchAllPagesRead(previous, nowIso) : previous),
      )
      void queryClient.invalidateQueries({ queryKey: NOTIFICATIONS_UNREAD_QUERY_KEY })
    },
    onError: (error) => {
      setActionError(describeApiError(error))
    },
  })

  const items = flattenUniqueNotifications(inboxQuery.data?.pages)
  const hasRefreshError =
    inboxQuery.isError && !inboxQuery.isFetchNextPageError && items.length > 0

  let listStatus: AsyncStatus = 'data'
  if (inboxQuery.isPending) listStatus = 'loading'
  else if (inboxQuery.isError && !inboxQuery.isFetchNextPageError && items.length === 0) {
    listStatus = 'error'
  } else if (items.length === 0) {
    listStatus = 'empty'
  }

  const requestNextPage = () => {
    if (fetchingMoreRef.current || inboxQuery.isFetchingNextPage) return
    fetchingMoreRef.current = true
    void inboxQuery.fetchNextPage({ cancelRefetch: false }).finally(() => {
      fetchingMoreRef.current = false
    })
  }

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen) setActionError(null)
    onOpenChange(nextOpen)
  }

  return (
    <Drawer
      open={open}
      onOpenChange={handleOpenChange}
      title="通知收件箱"
      closeLabel="关闭收件箱"
      returnFocusTo={returnFocusTo}
      footer={
        <Button variant="ghost" onClick={() => handleOpenChange(false)}>
          关闭
        </Button>
      }
    >
      <div className="app-inbox-toolbar">
        <span className="app-inbox-summary">
          {unreadCount > 0 ? `${unreadCount} 条未读` : '暂无未读消息'}
        </span>
        <div className="app-inbox-toolbar-actions">
          <BusyButton
            variant="ghost"
            size="sm"
            busy={markAllMutation.isPending}
            busyLabel="处理中…"
            disabled={SHOWCASE || inboxQuery.isPending || items.length === 0}
            title={SHOWCASE ? READ_ONLY_REASON : undefined}
            onClick={() => {
              if (SHOWCASE) return
              markAllMutation.mutate()
            }}
          >
            <Icon name="checkCheck" size={14} />
            全部已读
          </BusyButton>
          {SHOWCASE ? <span className="app-deny-note">{READ_ONLY_REASON}</span> : null}
        </div>
      </div>

      {hasRefreshError ? (
        <div className="app-inbox-error">
          <AsyncState
            status="error"
            error={describeApiError(inboxQuery.error)}
            onRetry={() => {
              void inboxQuery.refetch()
            }}
            children={null}
          />
        </div>
      ) : null}

      {actionError ? (
        <div className="app-inbox-error">
          <Alert tone="danger" title="操作失败" dismissible onDismiss={() => setActionError(null)}>
            {actionError}
          </Alert>
        </div>
      ) : null}

      <AsyncState
        status={listStatus}
        emptyLabel="暂无通知"
        error={inboxQuery.isError ? describeApiError(inboxQuery.error) : undefined}
        onRetry={() => {
          void inboxQuery.refetch()
        }}
      >
        <ul className="app-inbox-list" aria-label="通知列表">
          {items.map((item) => (
            <InboxItem
              key={item.id}
              item={item}
              marking={markingIds.has(item.id) || markAllMutation.isPending}
              readOnly={SHOWCASE}
              onMarkRead={(id) => {
                if (SHOWCASE || markingIds.has(id)) return
                markReadMutation.mutate(id)
              }}
            />
          ))}
        </ul>

        {inboxQuery.isFetchNextPageError ? (
          <div className="app-inbox-more-error">
            <div className="app-async-error">
              <Alert tone="danger">{describeApiError(inboxQuery.error)}</Alert>
              <BusyButton
                variant="outline-danger"
                size="sm"
                busy={inboxQuery.isFetchingNextPage}
                busyLabel="加载中…"
                onClick={requestNextPage}
              >
                重试
              </BusyButton>
            </div>
          </div>
        ) : inboxQuery.hasNextPage ? (
          <div className="app-inbox-more">
            <BusyButton
              variant="ghost"
              size="sm"
              busy={inboxQuery.isFetchingNextPage}
              busyLabel="加载中…"
              onClick={requestNextPage}
            >
              加载更多
            </BusyButton>
          </div>
        ) : null}
      </AsyncState>
    </Drawer>
  )
}
