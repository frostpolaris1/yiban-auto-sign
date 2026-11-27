/**
 * **功能**
 * 用户端操作错误文案：只把契约错误码翻译成可执行的下一步。
 *
 * **归属**
 * user-next 展示层；不依赖 React。
 *
 * **口径**
 * 不暴露内部 code；未知错误给通用建议。
 */

import { ApiError } from '@/lib/api/client'

export function signErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return '没有权限执行这个操作。'
    if (error.status === 404) return '账号不存在或已删除。'
    if (error.status === 429) return '操作太频繁，请稍后再试。'
    if (error.status === 503) return '系统维护中，请稍后再试。'
    if (error.status === 409) {
      switch (error.code) {
        case 'window_closed':
          return '今天的签到窗口已经关闭。'
        case 'already_signed':
          return '今天已经签过，不需要再试。'
        case 'sign_running':
          return '正在签到，请稍候。'
        case 'task_ambiguous':
          return '账号有多条任务，请联系管理员。'
        default:
          return '当前状态不能签到。'
      }
    }
    return '签到没有成功，请稍后再试。'
  }
  return '网络异常，请稍后再试。'
}

export function pauseErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 400) return '当前状态不允许这个操作。'
    if (error.status === 403) return '口令不正确，或没有权限。'
    if (error.status === 409) return '当前状态不允许这个操作。'
    if (error.status === 429) return '尝试过多，请稍后再试。'
    if (error.status === 503) return '系统维护中，请稍后再试。'
    return '操作失败，请稍后再试。'
  }
  return '网络异常，请稍后再试。'
}
