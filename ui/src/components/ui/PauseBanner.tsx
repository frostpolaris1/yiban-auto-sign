/**
 * **功能**
 * 执行引擎暂停横幅：常驻提示当前已暂停、影响范围与恢复入口。
 *
 * **归属**
 * `components` 通用件：不发请求，读域数据的职责在页面层。
 *
 * **口径**
 * 只在暂停态渲染；恢复键按角色收紧（仅 master 可恢复），
 * admin 见到置灰键与原因，不隐藏入口。恢复动作走调用方注入的回调。
 * 展示模式由调用方置只读，本组件只呈现 disabled 与原因。
 */

import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'

type PauseBannerProps = {
  /** 在飞任务数：暂停后仍会跑完。 */
  inflight: number
  /** 到点未领取的任务数：恢复后按序补跑。 */
  dueNow: number
  canResume: boolean
  /** 不可恢复时的原因（如「仅主管理员可恢复」或展示模式只读）。 */
  resumeBlockedReason?: string
  onResume: () => void
}

export function PauseBanner({
  inflight,
  dueNow,
  canResume,
  resumeBlockedReason,
  onResume,
}: PauseBannerProps) {
  return (
    <div className="app-pause-banner">
      <Alert tone="warning" title="执行引擎已暂停">
        <div className="app-pause-banner-body">
          <span>
            {`本管辖范围：在飞 ${inflight} 条任务继续完成；待执行 ${dueNow} 条暂停领取，恢复后按序补跑。暂停为全局开关。`}
          </span>
          <Button
            variant="primary"
            size="sm"
            disabled={!canResume}
            title={canResume ? undefined : resumeBlockedReason}
            onClick={onResume}
          >
            恢复引擎
          </Button>
        </div>
      </Alert>
    </div>
  )
}
