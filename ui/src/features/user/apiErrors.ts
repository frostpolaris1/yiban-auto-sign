/**
 * **功能**
 * 用户域写操作的错误分类：409 conflict 识别。
 *
 * **归属**
 * `features/user` 纯逻辑：无 React、无网络、无 DOM。
 *
 * **口径**
 * 服务端对「最后一名 master」「名下有账号」「邮箱被占用」都回
 * 409 `conflict`；调用方据此把英文错误替换成业务文案。
 * 其余错误原样走 `describeApiError`。
 */

import { ApiError } from '@/lib/api/client'

/** 判断错误是否为 409 conflict。 */
export function isConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && error.code === 'conflict'
}
