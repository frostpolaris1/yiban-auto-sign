/**
 * **功能**
 * 用户端本人数据的查询键与取数钩子。
 *
 * **归属**
 * `features/me` 域容器：只组合 `lib/api` 的请求，不含展示。
 *
 * **口径**
 * 列表含已删除行，首页据此区分「无账号」与「已删除可恢复」。
 * 今日状态与诊断 30 秒轮询；手动签到后由调用方传 `fast` 提到 3 秒。
 * 日历按 from/to 分键，避免跨月串数据。
 * 公告失败静默；其余查询由各区块自己呈现错误与重试。
 */

import { useQuery } from '@tanstack/react-query'
import { listAccounts } from '@/lib/api/account'
import { getAnnouncement } from '@/lib/api/announcement'
import {
  getDiagnostics,
  getMailNotify,
  getPhotoRetention,
  getSignCalendar,
  getSignCalendarDay,
  getSignStatus,
} from '@/lib/api/me'
import { getTimePref } from '@/lib/api/schedule'
import { listAccountPhotos } from '@/lib/api/photo'
import { listAllSchools } from '@/lib/api/school'

export const ME_KEYS = {
  accounts: ['me', 'accounts'] as const,
  schools: ['schools'] as const,
  signStatus: ['me', 'sign-status'] as const,
  diagnostics: ['me', 'diagnostics'] as const,
  timePref: ['me', 'time-pref'] as const,
  calendar: (from: string, to: string) => ['me', 'sign-calendar', from, to] as const,
  calendarDay: (day: string) => ['me', 'sign-calendar-day', day] as const,
  announcement: ['announcement'] as const,
  photos: (accountId: string) => ['account', accountId, 'photos'] as const,
  mailNotify: ['me', 'mail-notify'] as const,
  photoRetention: ['me', 'photo-retention'] as const,
}

export function useMyAccounts() {
  return useQuery({
    queryKey: ME_KEYS.accounts,
    queryFn: () => listAccounts({ include_deleted: true }),
    staleTime: 0,
  })
}

/** 学校名只用于身份卡展示；user 只读 active 行。 */
export function useSchools() {
  return useQuery({
    queryKey: ME_KEYS.schools,
    queryFn: listAllSchools,
    staleTime: 300_000,
  })
}

export function useSignStatus(fast = false) {
  return useQuery({
    queryKey: ME_KEYS.signStatus,
    queryFn: getSignStatus,
    refetchInterval: fast ? 3_000 : 30_000,
    staleTime: 0,
  })
}

export function useDiagnostics(fast = false) {
  return useQuery({
    queryKey: ME_KEYS.diagnostics,
    queryFn: getDiagnostics,
    refetchInterval: fast ? 3_000 : 30_000,
    staleTime: 0,
  })
}

export function useTimePref() {
  return useQuery({
    queryKey: ME_KEYS.timePref,
    queryFn: getTimePref,
    staleTime: 0,
  })
}

export function useSignCalendar(from: string, to: string, enabled = true) {
  return useQuery({
    queryKey: ME_KEYS.calendar(from, to),
    queryFn: () => getSignCalendar(from, to),
    enabled,
    staleTime: 30_000,
  })
}

export function useSignCalendarDay(day: string | undefined) {
  return useQuery({
    queryKey: ME_KEYS.calendarDay(day ?? ''),
    queryFn: () => getSignCalendarDay(day!),
    enabled: day !== undefined,
    staleTime: 30_000,
    retry: false,
  })
}

export function useAnnouncement() {
  return useQuery({
    queryKey: ME_KEYS.announcement,
    queryFn: getAnnouncement,
    staleTime: 60_000,
    retry: false,
  })
}

export function useLatestPhoto(accountId: string | null) {
  return useQuery({
    queryKey: ME_KEYS.photos(accountId ?? 'none'),
    queryFn: () => listAccountPhotos(accountId!, { limit: 1 }),
    enabled: accountId !== null,
    staleTime: 30_000,
    retry: false,
  })
}

export function useMailNotify() {
  return useQuery({
    queryKey: ME_KEYS.mailNotify,
    queryFn: getMailNotify,
    staleTime: 0,
  })
}

export function usePhotoRetention() {
  return useQuery({
    queryKey: ME_KEYS.photoRetention,
    queryFn: getPhotoRetention,
    staleTime: 0,
  })
}
