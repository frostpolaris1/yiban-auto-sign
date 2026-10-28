/**
 * **功能**
 * 窗口覆盖表单：开始、结束、原因；预填当前有效窗口。
 *
 * **归属**
 * `features/schedule` 展示层：请求体构造在 `drafts`，发请求在 `ScheduleView`。
 *
 * **口径**
 * 时间按上海墙钟填，提交时带固定 `+08:00` 偏移。
 * 原因必填 1..200 字；结束必须严格晚于开始。
 * 校验不过时「下一步：确认覆盖」置灰，不发任何请求。
 * 本件只在点下一步时把校验通过的请求体交给调用方，仍不写库。
 */

import { useEffect, useState } from 'react'
import type { ScheduleOverrideRequest, SchedulePlan, ScheduleWindow } from '@/lib/api/schedule'
import { Button } from '@/components/ui/Button'
import { TextAreaInput, TextInput } from '@/components/ui/Input'
import { Modal } from '@/components/ui/Modal'
import {
  EMPTY_OVERRIDE_DRAFT,
  OVERRIDE_REASON_MAX,
  buildOverrideBody,
  overrideDraftFromPlan,
  type OverrideDraft,
} from './drafts'

type ScheduleOverrideModalProps = {
  open: boolean
  target: ScheduleWindow | null
  plan: SchedulePlan | undefined
  returnFocusTo: HTMLElement | null
  onClose: () => void
  onNext: (body: ScheduleOverrideRequest, opener: HTMLElement) => void
}

export function ScheduleOverrideModal({
  open,
  target,
  plan,
  returnFocusTo,
  onClose,
  onNext,
}: ScheduleOverrideModalProps) {
  // 本件常驻挂载（关闭时 Radix 自己卸载内容），故草稿在每次打开时重新预填。
  const [draft, setDraft] = useState<OverrideDraft>(EMPTY_OVERRIDE_DRAFT)
  useEffect(() => {
    if (!open) return
    setDraft(plan ? overrideDraftFromPlan(plan) : EMPTY_OVERRIDE_DRAFT)
  }, [open, plan?.window_start, plan?.window_end])

  const startIssue = draft.start === '' ? '开始时间必填' : undefined
  const endIssue =
    draft.end === ''
      ? '结束时间必填'
      : draft.start !== '' && draft.end <= draft.start
        ? '结束时间必须晚于开始时间'
        : undefined
  const reasonText = draft.reason.trim()
  const reasonIssue =
    reasonText === ''
      ? '修改原因必填'
      : reasonText.length > OVERRIDE_REASON_MAX
        ? `修改原因最多 ${OVERRIDE_REASON_MAX} 字`
        : undefined
  const blocked = startIssue ?? endIssue ?? reasonIssue ?? null

  const body =
    blocked === null && target !== null
      ? buildOverrideBody({
          day: plan?.day ?? '',
          schoolId: target.school_id,
          taskId: target.task_id,
          draft,
        })
      : null

  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
      title="写入窗口覆盖？"
      // 本件总从预览弹窗打开：叠层遮罩压暗预览，避免两层内容抢注意力。
      layer={2}
      description={
        target ? `${target.school_name} · ${target.task_name} · ${plan?.day ?? ''}（覆盖只改当日窗口）` : undefined
      }
      returnFocusTo={returnFocusTo}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={body === null}
            onClick={(event) => {
              if (body !== null) onNext(body, event.currentTarget)
            }}
          >
            下一步：确认覆盖
          </Button>
        </>
      }
    >
      <div className="app-form-stack">
        <TextInput
          id="schedule-override-start"
          label="开始时间"
          type="datetime-local"
          step={60}
          required
          error={startIssue}
          value={draft.start}
          onChange={(event) =>
            setDraft((previous) => ({ ...previous, start: event.target.value }))
          }
        />
        <TextInput
          id="schedule-override-end"
          label="结束时间"
          type="datetime-local"
          step={60}
          required
          error={endIssue}
          value={draft.end}
          onChange={(event) => setDraft((previous) => ({ ...previous, end: event.target.value }))}
        />
        <TextAreaInput
          id="schedule-override-reason"
          label="修改原因"
          rows={3}
          required
          hint="记录修改原因，供日后审查。"
          error={reasonIssue}
          value={draft.reason}
          onChange={(event) =>
            setDraft((previous) => ({ ...previous, reason: event.target.value }))
          }
        />
      </div>
    </Modal>
  )
}
