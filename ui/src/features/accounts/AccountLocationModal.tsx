/**
 * **功能**
 * 位置策略模态：mode 单选；fixed 带经纬度与理由。
 *
 * **归属**
 * `features/accounts` 展示层：草稿校验在 drafts，发请求由调用方做。
 *
 * **口径**
 * 契约没有位置读端点：首开盲态，注明「当前策略不可读取」。
 * 保存成功的响应由调用方写会话内缓存，下次打开按账号预填。
 * auto 时经纬度输入禁用且不进请求体。
 */

import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { Account, AccountLocation } from '@/lib/api/account'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Modal } from '@/components/ui/Modal'
import { SelectInput, TextAreaInput, TextInput } from '@/components/ui/Input'
import { locationError, type LocationDraft } from './drafts'

type AccountLocationModalProps = {
  account: Account | null
  pending: boolean
  error: string | null
  returnFocusTo: HTMLElement | null
  onClose: () => void
  onConfirm: (draft: LocationDraft) => void
}

export function AccountLocationModal({
  account,
  pending,
  error,
  returnFocusTo,
  onClose,
  onConfirm,
}: AccountLocationModalProps) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<LocationDraft>({
    mode: 'auto',
    lng: '',
    lat: '',
    reason: '',
  })
  // 一次性预填读缓存，不订阅：写入发生在保存成功时。
  const cached = account
    ? queryClient.getQueryData<AccountLocation>(['account-location', account.id])
    : undefined

  useEffect(() => {
    if (!account) return
    setDraft({
      mode: cached?.mode ?? 'auto',
      lng: cached?.lng != null ? String(cached.lng) : '',
      lat: cached?.lat != null ? String(cached.lat) : '',
      reason: cached?.reason ?? '',
    })
    // 账号或缓存变化时重置草稿；保存成功即预填来源。
  }, [account, cached, queryClient])

  const validation = locationError(draft)
  const fixed = draft.mode === 'fixed'

  return (
    <Modal
      open={account !== null}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
      title="设置位置策略？"
      description={account ? `账号 ${account.phone_masked}` : undefined}
      returnFocusTo={returnFocusTo}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={pending || validation !== null}
            onClick={() => onConfirm(draft)}
          >
            保存策略
          </Button>
        </>
      }
    >
      {cached ? (
        <p className="app-muted">已载入本会话内缓存的策略。</p>
      ) : (
        <p className="app-muted">当前策略不可读取。保存成功后本会话内可预填。</p>
      )}
      <div className="app-form-stack">
        <SelectInput
          id="account-location-mode"
          label="定位模式"
          options={[
            { value: 'auto', label: '自动＝窗口内自动定点' },
            { value: 'fixed', label: '固定＝指定经纬度' },
          ]}
          value={draft.mode}
          onChange={(event) =>
            setDraft((previous) => ({
              ...previous,
              mode: event.target.value === 'fixed' ? 'fixed' : 'auto',
            }))
          }
        />
        <TextInput
          id="account-location-lng"
          label="经度"
          placeholder="-180..180"
          inputMode="decimal"
          disabled={!fixed}
          error={fixed ? (validation?.includes('经度') ? validation : undefined) : undefined}
          value={draft.lng}
          onChange={(event) =>
            setDraft((previous) => ({ ...previous, lng: event.target.value }))
          }
        />
        <TextInput
          id="account-location-lat"
          label="纬度"
          placeholder="-90..90"
          inputMode="decimal"
          disabled={!fixed}
          error={fixed ? (validation?.includes('纬度') ? validation : undefined) : undefined}
          value={draft.lat}
          onChange={(event) =>
            setDraft((previous) => ({ ...previous, lat: event.target.value }))
          }
        />
        <TextAreaInput
          id="account-location-reason"
          label="设置理由"
          maxLength={200}
          hint="解释固定点越界用；平台暂不下发，最多 200 字。"
          disabled={!fixed}
          value={draft.reason}
          onChange={(event) =>
            setDraft((previous) => ({ ...previous, reason: event.target.value }))
          }
        />
      </div>
      {error ? (
        <div className="app-modal-error">
          <Alert tone="danger" title="请求失败">
            {error}
          </Alert>
        </div>
      ) : null}
    </Modal>
  )
}
