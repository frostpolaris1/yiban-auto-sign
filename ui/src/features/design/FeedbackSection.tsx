import { Alert } from '@/components/ui/Alert'
import { Badge, Tag } from '@/components/ui/Badge'
import { Card } from '@/components/ui/Card'

/** Badges, tags, and alerts. DOM follows Adminator `src/ui.html`. */
export function FeedbackSection() {
  return (
    <Card className="col-6" eyebrow="Feedback" title="徽章与提示">
      <div className="demo-row">
        <Badge>默认</Badge>
        <Badge tone="primary">主色</Badge>
        <Badge tone="success" dot>
          正常
        </Badge>
        <Badge tone="warning" dot>
          等待
        </Badge>
        <Badge tone="danger" dot>
          失败
        </Badge>
        <Badge tone="info">信息</Badge>
        <Badge tone="purple">计划</Badge>
        <Badge solid>草稿</Badge>
      </div>

      <div className="demo-row" style={{ marginTop: 14 }}>
        <Tag tone="t-active">启用</Tag>
        <Tag tone="t-new">新建</Tag>
        <Tag tone="t-used">已用</Tag>
        <Tag tone="t-old">过期</Tag>
        <Tag tone="t-unavail">不可用</Tag>
        <Tag tone="t-info">信息</Tag>
      </div>

      <div style={{ marginTop: 18 }}>
        <Alert tone="primary" title="新版本 · v0.1.0" dismissible>
          前端骨架与设计令牌已就绪。
        </Alert>
        <Alert tone="success" title="检查通过" dismissible>
          36 个浅色 token 与 36 个深色 token 与上游一致。
        </Alert>
        <Alert tone="warning" title="保留期提醒" dismissible>
          图片保留期在后续批次接入；本提示只演示关闭行为。
        </Alert>
        <Alert tone="danger" title="凭据错误" dismissible>
          示例：凭据错误不算风控，也不触发自动重试。
        </Alert>
        <Alert tone="info">维护窗口示例：周六 02:00–03:00。</Alert>
      </div>
    </Card>
  )
}
