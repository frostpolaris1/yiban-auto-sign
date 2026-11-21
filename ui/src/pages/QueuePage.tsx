/**
 * **功能**
 * 待办页：一屏处理待审核账号、今日失败任务与今日未签任务。
 *
 * **归属**
 * `pages` 路由页：只负责挂载 `QueueView`，域逻辑留在 `features/queue`。
 */

import { QueueView } from '@/features/queue/QueueView'

export function QueuePage() {
  return <QueueView />
}
