/**
 * **功能**
 * 调度运行策略卡：完整读写 `GET/PUT /api/admin/schedule-settings` 的全部 8 个配置键。
 *
 * **归属**
 * `features/system/policy` 子组件。
 *
 * **口径**
 * - 分三组 `<fieldset>`：`边界缩进与自选偏好`（3 键）、`周末门与默认窗口`（4 键）、
 *   `计划铺点分布档`（`front` / `uniform` 单选按钮组）。
 * - 写回返回 `SettingsReport` 后同步写入 `ADMIN_SETTINGS_QUERY_KEY` 并失效 `SCHEDULE_SETTINGS_QUERY_KEY`。
 */

import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { AsyncState } from '@/components/ui/AsyncState'
import { Badge } from '@/components/ui/Badge'
import { BusyButton } from '@/components/ui/BusyButton'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { TextInput } from '@/components/ui/Input'
import { SwitchControl } from '@/components/ui/SwitchControl'
import { notifySuccess } from '@/components/ui/toast'
import {
  ADMIN_SETTINGS_QUERY_KEY,
  getScheduleSettings,
  SCHEDULE_SETTINGS_QUERY_KEY,
  updateScheduleSettings,
  type AdminScheduleSettingsUpdate,
  type ScheduleSettingSource,
} from '@/lib/api/adminSettings'
import { showcaseScheduleSettings } from '@/lib/showcase'
import { SHOWCASE } from '@/lib/showcase-mode'
import {
  buildScheduleResetPatch,
  buildScheduleSettingsPatch,
  createSchedulePolicyDraft,
  formatSystemWriteError,
  settingSourceLabel,
  SIGN_DIST_OPTIONS,
  validateSchedulePolicyDraft,
  type SchedulePolicyDraft,
} from './drafts'
import type { PolicyCardAccessProps } from './PolicySwitchesCards'

function sourceTone(source: ScheduleSettingSource) {
  if (source === 'db') return 'info'
  if (source === 'env') return 'warning'
  return undefined
}

export function PolicyScheduleCard({ canWrite, readOnlyReason }: PolicyCardAccessProps) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<SchedulePolicyDraft | null>(null)
  const [touched, setTouched] = useState<Partial<Record<keyof SchedulePolicyDraft, boolean>>>({})
  const [submitted, setSubmitted] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [statusNote, setStatusNote] = useState<string | null>(null)

  const query = useQuery({
    queryKey: SCHEDULE_SETTINGS_QUERY_KEY,
    queryFn: () => (SHOWCASE ? Promise.resolve(showcaseScheduleSettings()) : getScheduleSettings()),
    staleTime: 30_000,
  })

  const mutation = useMutation({
    mutationFn: (body: AdminScheduleSettingsUpdate) => updateScheduleSettings(body),
    onSuccess: async (settingsReport) => {
      setSubmitError(null)
      setStatusNote(null)
      queryClient.setQueryData(ADMIN_SETTINGS_QUERY_KEY, settingsReport)
      // PUT 回传 SettingsReport（不含 5 枚 DB-only 键）；先等新 ScheduleSettingsReport 落袋再清草稿。
      await queryClient.invalidateQueries({ queryKey: SCHEDULE_SETTINGS_QUERY_KEY })
      setDraft(null)
      setTouched({})
      setSubmitted(false)
      notifySuccess('调度运行策略已保存')
    },
    onError: (err) => {
      setStatusNote(null)
      setSubmitError(formatSystemWriteError(err))
    },
  })

  const initialDraft = query.data ? createSchedulePolicyDraft(query.data) : null
  const effectiveDraft = draft ?? initialDraft
  const isDirty = Boolean(
    initialDraft &&
      effectiveDraft &&
      (effectiveDraft.edgeFrontSec !== initialDraft.edgeFrontSec ||
        effectiveDraft.edgeBackSec !== initialDraft.edgeBackSec ||
        effectiveDraft.allowTimePref !== initialDraft.allowTimePref ||
        effectiveDraft.saturdaySign !== initialDraft.saturdaySign ||
        effectiveDraft.sundaySign !== initialDraft.sundaySign ||
        effectiveDraft.defaultSignStartClock !== initialDraft.defaultSignStartClock ||
        effectiveDraft.defaultSignEndClock !== initialDraft.defaultSignEndClock ||
        effectiveDraft.signDist !== initialDraft.signDist),
  )
  const rawErrors = effectiveDraft ? validateSchedulePolicyDraft(effectiveDraft) : {}
  const shownErrors = {
    edgeFrontSec: submitted || touched.edgeFrontSec ? rawErrors.edgeFrontSec : undefined,
    edgeBackSec: submitted || touched.edgeBackSec ? rawErrors.edgeBackSec : undefined,
    defaultSignStartClock:
      submitted || touched.defaultSignStartClock ? rawErrors.defaultSignStartClock : undefined,
    defaultSignEndClock:
      submitted || touched.defaultSignEndClock || touched.defaultSignStartClock
        ? rawErrors.defaultSignEndClock
        : undefined,
  }

  function updateField<K extends keyof SchedulePolicyDraft>(key: K, value: SchedulePolicyDraft[K]) {
    if (!effectiveDraft) return
    setSubmitError(null)
    setStatusNote(null)
    setDraft({ ...effectiveDraft, [key]: value })
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!canWrite || mutation.isPending || !query.data || !effectiveDraft) return
    setSubmitted(true)
    setSubmitError(null)
    setStatusNote(null)
    const errors = validateSchedulePolicyDraft(effectiveDraft)
    if (errors.edgeFrontSec) {
      document.getElementById('policy-edge-front')?.focus()
      return
    }
    if (errors.edgeBackSec) {
      document.getElementById('policy-edge-back')?.focus()
      return
    }
    if (errors.defaultSignStartClock) {
      document.getElementById('policy-default-start')?.focus()
      return
    }
    if (errors.defaultSignEndClock) {
      document.getElementById('policy-default-end')?.focus()
      return
    }
    const patch = buildScheduleSettingsPatch(effectiveDraft, query.data)
    if (!patch) {
      setStatusNote('当前调度策略未发生变化')
      return
    }
    mutation.mutate(patch)
  }

  function handleReset() {
    if (!canWrite || mutation.isPending || !query.data) return
    setSubmitError(null)
    setStatusNote(null)
    mutation.mutate(buildScheduleResetPatch())
  }

  const asyncStatus = query.isPending ? 'loading' : query.isError ? 'error' : 'data'
  const hasDbOverride = Boolean(
    query.data &&
      [
        query.data.edge_front_sec.source,
        query.data.edge_back_sec.source,
        query.data.allow_time_pref.source,
        query.data.sunday_sign.source,
        query.data.saturday_sign.source,
        query.data.default_sign_start_min.source,
        query.data.default_sign_end_min.source,
        query.data.sign_dist.source,
      ].includes('db'),
  )

  return (
    <Card
      id="policy-schedule-card"
      eyebrow="Schedule Policy"
      title="调度与窗口策略"
      className="sys-policy-card sys-policy-card-wide"
    >
      <p className="sys-policy-desc">
        边界缩进、默认窗口、周末门与分布档将在下次日切构建计划时生效；「允许自选时段」次请求立即生效。
      </p>
      <AsyncState
        status={asyncStatus}
        error={query.isError ? formatSystemWriteError(query.error) : undefined}
        onRetry={() => void query.refetch()}
      >
        {query.data && effectiveDraft ? (
          <form
            className="app-form-stack"
            onSubmit={handleSubmit}
            aria-labelledby="policy-schedule-heading"
            noValidate
          >
            <span id="policy-schedule-heading" className="app-visually-hidden">
              调度与窗口策略
            </span>

            <fieldset className="sys-fieldset">
              <legend className="sys-legend">窗口边界与自选偏好</legend>
              <div className="sys-schedule-fields">
                <div className="sys-field-block">
                  <div className="sys-source-row">
                    <span className="sys-source-key">前边界来源</span>
                    <Badge tone={sourceTone(query.data.edge_front_sec.source)}>
                      {settingSourceLabel(query.data.edge_front_sec.source)}
                    </Badge>
                  </div>
                  <TextInput
                    id="policy-edge-front"
                    label="前边界缩进（秒）"
                    inputMode="numeric"
                    required
                    disabled={!canWrite || mutation.isPending}
                    value={effectiveDraft.edgeFrontSec}
                    hint="范围 0–300 秒；避开窗口刚开启的瞬时高峰"
                    error={shownErrors.edgeFrontSec}
                    onChange={(e) => updateField('edgeFrontSec', e.target.value)}
                    onBlur={() => setTouched((prev) => ({ ...prev, edgeFrontSec: true }))}
                  />
                </div>

                <div className="sys-field-block">
                  <div className="sys-source-row">
                    <span className="sys-source-key">后边界来源</span>
                    <Badge tone={sourceTone(query.data.edge_back_sec.source)}>
                      {settingSourceLabel(query.data.edge_back_sec.source)}
                    </Badge>
                  </div>
                  <TextInput
                    id="policy-edge-back"
                    label="后边界缩进（秒）"
                    inputMode="numeric"
                    required
                    disabled={!canWrite || mutation.isPending}
                    value={effectiveDraft.edgeBackSec}
                    hint="范围 0–300 秒；预留窗口结束前的重试余量"
                    error={shownErrors.edgeBackSec}
                    onChange={(e) => updateField('edgeBackSec', e.target.value)}
                    onBlur={() => setTouched((prev) => ({ ...prev, edgeBackSec: true }))}
                  />
                </div>
              </div>

              <div className="sys-switch-field">
                <div className="sys-source-row">
                  <SwitchControl
                    name="policy-allow-time-pref"
                    label="允许用户自选签到时段"
                    checked={effectiveDraft.allowTimePref}
                    disabled={!canWrite || mutation.isPending}
                    onCheckedChange={(next) => updateField('allowTimePref', next)}
                  />
                  <Badge tone={sourceTone(query.data.allow_time_pref.source)}>
                    {settingSourceLabel(query.data.allow_time_pref.source)}
                  </Badge>
                </div>
                <p className="sys-control-hint">关闭后普通用户仅可查看当前默认排期，无法改写个人偏好。</p>
              </div>
            </fieldset>

            <fieldset className="sys-fieldset">
              <legend className="sys-legend">周末门与全站默认窗口</legend>
              <div className="sys-schedule-switches">
                <div className="sys-switch-field">
                  <div className="sys-source-row">
                    <SwitchControl
                      name="policy-saturday-sign"
                      label="周六开启签到"
                      checked={effectiveDraft.saturdaySign}
                      disabled={!canWrite || mutation.isPending}
                      onCheckedChange={(next) => updateField('saturdaySign', next)}
                    />
                    <Badge tone={sourceTone(query.data.saturday_sign.source)}>
                      {settingSourceLabel(query.data.saturday_sign.source)}
                    </Badge>
                  </div>
                </div>
                <div className="sys-switch-field">
                  <div className="sys-source-row">
                    <SwitchControl
                      name="policy-sunday-sign"
                      label="周日开启签到"
                      checked={effectiveDraft.sundaySign}
                      disabled={!canWrite || mutation.isPending}
                      onCheckedChange={(next) => updateField('sundaySign', next)}
                    />
                    <Badge tone={sourceTone(query.data.sunday_sign.source)}>
                      {settingSourceLabel(query.data.sunday_sign.source)}
                    </Badge>
                  </div>
                </div>
              </div>

              <div className="sys-schedule-fields">
                <div className="sys-field-block">
                  <div className="sys-source-row">
                    <span className="sys-source-key">默认窗口来源</span>
                    <Badge tone={sourceTone(query.data.default_sign_start_min.source)}>
                      {settingSourceLabel(query.data.default_sign_start_min.source)}
                    </Badge>
                  </div>
                  <TextInput
                    id="policy-default-start"
                    type="time"
                    label="默认开始时间（上海时间）"
                    required
                    disabled={!canWrite || mutation.isPending}
                    value={effectiveDraft.defaultSignStartClock}
                    hint="内置缺省 06:30；两项成对提交生效"
                    error={shownErrors.defaultSignStartClock}
                    onChange={(e) => updateField('defaultSignStartClock', e.target.value)}
                    onBlur={() => setTouched((prev) => ({ ...prev, defaultSignStartClock: true }))}
                  />
                </div>

                <div className="sys-field-block">
                  <div className="sys-source-row">
                    <span className="sys-source-key">默认结束对齐</span>
                    <Badge tone={sourceTone(query.data.default_sign_end_min.source)}>
                      {settingSourceLabel(query.data.default_sign_end_min.source)}
                    </Badge>
                  </div>
                  <TextInput
                    id="policy-default-end"
                    type="time"
                    label="默认结束时间（上海时间）"
                    required
                    disabled={!canWrite || mutation.isPending}
                    value={effectiveDraft.defaultSignEndClock}
                    hint="内置缺省 07:50；必须严格晚于开始时间"
                    error={shownErrors.defaultSignEndClock}
                    onChange={(e) => updateField('defaultSignEndClock', e.target.value)}
                    onBlur={() => setTouched((prev) => ({ ...prev, defaultSignEndClock: true }))}
                  />
                </div>
              </div>
            </fieldset>

            <fieldset className="sys-fieldset">
              <legend className="sys-legend">
                计划铺点分布档
                <Badge tone={sourceTone(query.data.sign_dist.source)}>
                  {settingSourceLabel(query.data.sign_dist.source)}
                </Badge>
              </legend>
              <div className="sys-radio-group">
                {SIGN_DIST_OPTIONS.map((option) => {
                  const id = `policy-sign-dist-${option.value}`
                  return (
                    <label key={option.value} htmlFor={id} className="sys-radio-option">
                      <input
                        id={id}
                        type="radio"
                        name="policy-sign-dist"
                        value={option.value}
                        checked={effectiveDraft.signDist === option.value}
                        disabled={!canWrite || mutation.isPending}
                        onChange={() => updateField('signDist', option.value)}
                      />
                      <span className="sys-radio-copy">
                        <span className="sys-radio-title">{option.label}</span>
                        <span className="sys-radio-detail">{option.detail}</span>
                      </span>
                    </label>
                  )
                })}
              </div>
            </fieldset>

            {submitError ? <Alert tone="danger">{submitError}</Alert> : null}
            {statusNote ? (
              <p className="sys-status-note" role="status">
                {statusNote}
              </p>
            ) : null}

            <div className="sys-card-actions">
              <BusyButton
                type="submit"
                variant="primary"
                size="sm"
                busy={mutation.isPending}
                busyLabel="保存中…"
                disabled={!canWrite}
                title={readOnlyReason ?? undefined}
              >
                保存调度策略
              </BusyButton>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={!canWrite || mutation.isPending || !hasDbOverride}
                title={readOnlyReason ?? undefined}
                onClick={handleReset}
              >
                恢复调度默认
              </Button>
              {canWrite && isDirty && !mutation.isPending ? (
                <span className="sys-dirty-note" role="status">
                  有未保存变更
                </span>
              ) : null}
              {readOnlyReason ? <span className="app-deny-note">{readOnlyReason}</span> : null}
            </div>
          </form>
        ) : null}
      </AsyncState>
    </Card>
  )
}
