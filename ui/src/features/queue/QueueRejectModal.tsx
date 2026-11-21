/**
 * **功能**
 * 驳回理由弹窗：专用于待审核组的「驳回」动作，提供快捷标签与双向进出动效。
 *
 * **归属**
 * `features/queue` 展示组件：收集理由与展示接口错误，写请求由容器发出。
 *
 * **口径**
 * 理由必填且不超过 200 字，与 `AccountReviewRequest` 契约一致。
 * 失败时保持弹窗打开并在框内报错，不关闭、不弹成功 toast。
 */

import { useEffect, useState } from 'react'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { TextAreaInput } from '@/components/ui/Input'
import { Modal } from '@/components/ui/Modal'
import { cx } from '@/lib/cx'
import type { Account } from '@/lib/api/account'
import { REJECT_REASON_MAX, REJECT_REASON_PRESETS, applyRejectPreset } from './queueDisplay'
import { usePresence } from './usePresence'

type QueueRejectModalProps = {
  account: Account | null
  schoolName: string
  pending: boolean
  error: string | null
  readOnly: boolean
  returnFocusTo: HTMLElement | null
  onClose: () => void
  onSubmit: (reason: string) => void
}

export function QueueRejectModal({
  account,
  schoolName,
  pending,
  error,
  readOnly,
  returnFocusTo,
  onClose,
  onSubmit,
}: QueueRejectModalProps) {
  const [reason, setReason] = useState('')
  const [snapshot, setSnapshot] = useState<{ account: Account; schoolName: string } | null>(null)
  const { mounted, closing } = usePresence(account !== null, 170)

  if (account !== null && (snapshot?.account.id !== account.id || snapshot.schoolName !== schoolName)) {
    setSnapshot({ account, schoolName })
  }

  useEffect(() => {
    setReason('')
  }, [account?.id])

  const shown = account ?? snapshot?.account ?? null
  const shownSchool = account ? schoolName : snapshot?.schoolName ?? ''

  const trimmed = reason.trim()
  const canSubmit = !pending && !readOnly && trimmed.length > 0 && trimmed.length <= REJECT_REASON_MAX

  return (
    <Modal
      open={mounted}
      closing={closing}
      className="q-modal-anim"
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
      title="驳回账号"
      description={
        shown
          ? `${shown.display_name || shown.phone_masked} · ${shown.phone_masked} · ${shownSchool}`
          : undefined
      }
      returnFocusTo={returnFocusTo}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            取消
          </Button>
          <Button
            variant="danger"
            disabled={!canSubmit}
            title={readOnly ? '展示模式只读' : undefined}
            onClick={() => onSubmit(trimmed)}
          >
            {pending ? '提交中…' : '确认驳回'}
          </Button>
        </>
      }
    >
      <div className="app-form-stack">
        <div className="q-presets" role="group" aria-label="常用驳回理由">
          {REJECT_REASON_PRESETS.map((preset) => {
            const selected = trimmed.split('；').some((part) => part.trim() === preset)
            return (
              <button
                key={preset}
                type="button"
                className={cx('q-preset-chip', selected && 'is-selected')}
                onClick={() => setReason((current) => applyRejectPreset(current, preset))}
              >
                {preset}
              </button>
            )
          })}
        </div>

        <TextAreaInput
          id="queue-reject-reason"
          label="驳回理由"
          required
          rows={3}
          maxLength={REJECT_REASON_MAX}
          placeholder="请填写原因（用户可见）"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
        />

        <div className="q-char-count">
          {reason.length} / {REJECT_REASON_MAX}
        </div>
      </div>

      {error ? (
        <div className="app-modal-error">
          <Alert tone="danger">{error}</Alert>
        </div>
      ) : null}
    </Modal>
  )
}
