/**
 * **功能**
 * 整页无权限卡片：解释拒绝原因，并给出两条出路。
 *
 * **归属**
 * `components` 通用件：不含数据请求。
 *
 * **口径**
 * 不做静默重定向：卡片先解释状态，去向由用户选择。
 * 主按钮回看板；次按钮仅在本次进入带来源时出现。
 * 次按钮走浏览器历史，保留回退语义。
 * 标题与原因由调用方给：学校页与时间片页共用同一形状。
 */

import { useNavigate } from '@tanstack/react-router'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { Icon } from '@/components/ui/Icon'

type DeniedCardProps = {
  /** 卡片头的小字分类，例如 `Schools`。 */
  eyebrow: string
  /** 卡片标题，缺省「无权访问」。 */
  title?: string
  reason: string
}

export function DeniedCard({ eyebrow, title = '无权访问', reason }: DeniedCardProps) {
  const navigate = useNavigate()
  // document.referrer 是本次文档进入时的来源。站内切路由不改变它。
  const hasReferrer = document.referrer !== ''

  return (
    <Card eyebrow={eyebrow} title={title}>
      <div className="app-denied">
        <div className="app-denied-head">
          <span className="app-denied-icon">
            <Icon name="shield" size={20} />
          </span>
          <p className="app-denied-reason">{reason}</p>
        </div>
        <div className="app-denied-actions">
          <Button
            variant="primary"
            onClick={() => {
              void navigate({ to: '/' })
            }}
          >
            返回看板
          </Button>
          {hasReferrer ? (
            <Button
              variant="ghost"
              onClick={() => {
                window.history.back()
              }}
            >
              返回上一页
            </Button>
          ) : null}
        </div>
      </div>
    </Card>
  )
}
