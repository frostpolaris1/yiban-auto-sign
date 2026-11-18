/**
 * **功能**
 * `/system#channels` 投递渠道连通测试分区（SMTP 测试邮件 + Webhook 测试推送）。
 *
 * **归属**
 * `features/system/channels` 分区组件。
 *
 * **口径**
 * - 测试邮件先弹出二次口令复核弹窗：`400/403`（及 reauth 节流）错误留在弹窗内，
 *   `409` 未配置/无邮箱、`429` 发送冷却、`502` 投递失败关闭弹窗并把反馈直接展示在按钮旁。
 * - 测试 Webhook 无弹窗、空体直发 `POST /api/admin/webhook/test`，结果内联在按钮旁。
 * - 仅主管理员可触发测试；`admin` 与展示模式置灰全部触发按钮并给出原因，绝不发出请求。
 */

import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { BusyButton } from '@/components/ui/BusyButton'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { notifySuccess } from '@/components/ui/toast'
import { sendTestMail, sendTestWebhook } from '@/lib/api/adminSettings'
import {
  formatMailReauthModalError,
  formatMailTestResultError,
  formatWebhookTestError,
  shouldKeepMailTestModalOpen,
} from './drafts'
import { MailTestReauthModal } from './MailTestReauthModal'

type ChannelsSectionProps = {
  canWrite: boolean
  readOnlyReason: string | null
}

type ChannelFeedback = {
  tone: 'success' | 'danger'
  text: string
}

export function ChannelsSection({ canWrite, readOnlyReason }: ChannelsSectionProps) {
  const [mailModalOpen, setMailModalOpen] = useState(false)
  const [mailModalError, setMailModalError] = useState<string | null>(null)
  const [mailResult, setMailResult] = useState<ChannelFeedback | null>(null)
  const [webhookResult, setWebhookResult] = useState<ChannelFeedback | null>(null)
  const [mailTriggerEl, setMailTriggerEl] = useState<HTMLElement | null>(null)

  const mailMutation = useMutation({
    mutationFn: (currentPassword: string) => sendTestMail({ current_password: currentPassword }),
    onSuccess: (res) => {
      setMailModalError(null)
      setMailModalOpen(false)
      setMailResult({
        tone: 'success',
        text: `测试邮件已接受投递（${res.message}）`,
      })
      notifySuccess('测试邮件发送成功')
    },
    onError: (err) => {
      if (shouldKeepMailTestModalOpen(err)) {
        setMailModalError(formatMailReauthModalError(err))
        return
      }
      setMailModalError(null)
      setMailModalOpen(false)
      setMailResult({
        tone: 'danger',
        text: formatMailTestResultError(err),
      })
    },
  })

  const webhookMutation = useMutation({
    mutationFn: () => sendTestWebhook(),
    onSuccess: (res) => {
      setWebhookResult({
        tone: 'success',
        text: `测试 Webhook 已发送（${res.message}）`,
      })
      notifySuccess('测试 Webhook 推送成功')
    },
    onError: (err) => {
      setWebhookResult({
        tone: 'danger',
        text: formatWebhookTestError(err),
      })
    },
  })

  return (
    <section
      id="channels"
      className="sys-section"
      tabIndex={-1}
      aria-labelledby="channels-section-heading"
    >
      <div className="sys-section-head">
        <h2 id="channels-section-heading" className="sys-section-title">
          渠道测试
        </h2>
        <p className="sys-section-desc">
          向预设的邮件出口与 Webhook 推送出口发起一次性连通探活；仅验证链路可用，不暴露密钥内容。
        </p>
      </div>

      <Card eyebrow="Channels" title="通知投递自检" className="sys-policy-card">
        <div className="sys-channel-list">
          <div className="sys-channel-row">
            <div className="sys-channel-info">
              <h3 className="sys-channel-name">邮件渠道测试（SMTP）</h3>
              <p className="sys-policy-desc">
                向当前登录主管理员邮箱投递验证邮件；需输入当前登录密码复核，存在 60 秒冷却。
              </p>
            </div>
            <div className="sys-channel-controls">
              <div className="sys-card-actions">
                <Button
                  type="button"
                  variant="outline-primary"
                  size="sm"
                  disabled={!canWrite || mailMutation.isPending}
                  title={readOnlyReason ?? undefined}
                  onClick={(e) => {
                    if (!canWrite) return
                    setMailTriggerEl(e.currentTarget)
                    setMailModalError(null)
                    setMailModalOpen(true)
                  }}
                >
                  测试邮件
                </Button>
                {readOnlyReason ? <span className="app-deny-note">{readOnlyReason}</span> : null}
              </div>
              {mailResult ? <Alert tone={mailResult.tone}>{mailResult.text}</Alert> : null}
            </div>
          </div>

          <div className="sys-channel-row">
            <div className="sys-channel-info">
              <h3 className="sys-channel-name">Webhook 推送测试</h3>
              <p className="sys-policy-desc">
                向已配置的 ServerChan 或自定义 Webhook 发送测试消息，存在 60 秒冷却。
              </p>
            </div>
            <div className="sys-channel-controls">
              <div className="sys-card-actions">
                <BusyButton
                  type="button"
                  variant="outline-primary"
                  size="sm"
                  busy={webhookMutation.isPending}
                  busyLabel="发送中…"
                  disabled={!canWrite}
                  title={readOnlyReason ?? undefined}
                  onClick={() => {
                    if (!canWrite || webhookMutation.isPending) return
                    setWebhookResult(null)
                    webhookMutation.mutate()
                  }}
                >
                  测试 Webhook
                </BusyButton>
                {readOnlyReason ? <span className="app-deny-note">{readOnlyReason}</span> : null}
              </div>
              {webhookResult ? (
                <Alert tone={webhookResult.tone}>{webhookResult.text}</Alert>
              ) : null}
            </div>
          </div>
        </div>
      </Card>

      <MailTestReauthModal
        open={mailModalOpen}
        busy={mailMutation.isPending}
        error={mailModalError}
        triggerEl={mailTriggerEl}
        onClearError={() => setMailModalError(null)}
        onClose={() => {
          setMailModalError(null)
          setMailModalOpen(false)
        }}
        onSubmit={(currentPassword) => {
          if (!canWrite || mailMutation.isPending) return
          setMailModalError(null)
          mailMutation.mutate(currentPassword)
        }}
      />
    </section>
  )
}
