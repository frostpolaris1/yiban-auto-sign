/**
 * **功能**
 * 账号只读详情抽屉：读 `GET /api/accounts/{id}`，展示全部字段。
 *
 * **归属**
 * `features/accounts` 展示层：取数走 react-query，写操作不在这里。
 *
 * **口径**
 * 打开即取详情。列表里的摘要先渲染，详情到达后替换。
 * 学校名从同一份学校列表解析；取不到时退回学校 ID。
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { BusyButton } from '@/components/ui/BusyButton'
import { Button } from '@/components/ui/Button'
import { Drawer } from '@/components/ui/Drawer'
import { getAccount, refreshDevice, type Account } from '@/lib/api/account'
import { AccountDeviceView } from './AccountDeviceView'
import { describeApiError } from '@/lib/api/client'
import type { School } from '@/lib/api/school'
import { formatDateTime } from '@/lib/format'
import { statusLabel, statusTone } from './display'
import { SHOWCASE } from '@/lib/session'

type AccountDetailDrawerProps = {
  account: Account | null
  schools: School[]
  returnFocusTo: HTMLElement | null
  onClose: () => void
}

export function AccountDetailDrawer({
  account,
  schools,
  returnFocusTo,
  onClose,
}: AccountDetailDrawerProps) {
  const detail = useQuery({
    queryKey: ['account', account?.id ?? null],
    // 展示模式：无后端，列表摘要已够展示，不发详情请求。
    queryFn: () => (SHOWCASE ? Promise.resolve(account!) : getAccount(account!.id)),
    enabled: account !== null,
  })

  const data = detail.data ?? account
  const schoolName = data
    ? (schools.find((school) => school.id === data.school_id)?.name ?? data.school_id)
    : ''

  // 设备信息块：读回动作驱动。明文设备码从不出服务端，这里只有掩码。
  const device = useMutation({
    mutationFn: () => refreshDevice(data!.id),
  })
  const deviceError = device.isError ? describeApiError(device.error) : null

  return (
    <Drawer
      open={account !== null}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
      title="账号详情"
      description={account ? `手机号 ${account.phone_masked}` : undefined}
      returnFocusTo={returnFocusTo}
      footer={
        <Button variant="ghost" onClick={onClose}>
          关闭
        </Button>
      }
    >
      {detail.isError ? (
        <Alert tone="danger" title="详情加载失败">
          {describeApiError(detail.error)}
        </Alert>
      ) : null}

      {!detail.isError && !data ? <p className="app-muted">加载中…</p> : null}

      {data ? (
        <dl className="app-detail-list">
          <dt>账号 ID</dt>
          <dd className="data-cell-mono">{data.id}</dd>
          <dt>手机号</dt>
          <dd className="data-cell-mono">{data.phone_masked}</dd>
          <dt>学校</dt>
          <dd>{schoolName}</dd>
          <dt>归属用户</dt>
          <dd className="data-cell-mono">{data.owner_user_id}</dd>
          <dt>状态</dt>
          <dd>
            <Badge tone={statusTone(data.status)} dot>
              {statusLabel(data.status)}
            </Badge>
          </dd>
          <dt>驳回理由</dt>
          <dd>{data.reject_reason || '—'}</dd>
          <dt>审核人</dt>
          <dd>{data.reviewed_by ?? '—'}</dd>
          <dt>审核时间</dt>
          <dd>{formatDateTime(data.reviewed_at)}</dd>
          <dt>创建时间</dt>
          <dd>{formatDateTime(data.created_at)}</dd>
          <dt>更新时间</dt>
          <dd>{formatDateTime(data.updated_at)}</dd>
        </dl>
      ) : null}

      {data ? (
        <section className="app-detail-block" aria-label="设备信息">
          {device.isPending ? <p className="app-muted">读取中…</p> : null}
          {deviceError ? (
            <Alert tone="warning" title="设备信息读取失败">
              {deviceError} 可稍后重试，已有数据不变。
            </Alert>
          ) : null}
          {device.data ? <AccountDeviceView device={device.data} /> : null}
          {!device.isPending && !deviceError && !device.data ? (
            <p className="app-muted">尚未读取设备信息。可从易班平台读回登记字段。</p>
          ) : null}
          <BusyButton
            variant="ghost"
            size="sm"
            busy={device.isPending}
            busyLabel="读取中…"
            disabled={SHOWCASE}
            aria-label="刷新设备"
            onClick={() => device.mutate()}
          >
            刷新设备
          </BusyButton>
        </section>
      ) : null}
    </Drawer>
  )
}
