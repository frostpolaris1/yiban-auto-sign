/**
 * **功能**
 * 账号状态的文案与徽章色：管理端转出共用映射。
 *
 * **归属**
 * `features/accounts` 展示层：只做转出，表在 `lib/account-display`。
 *
 * **口径**
 * 管理端与用户端共用一张状态表；用户端用 `userStatusLabel` 去掉「管理员/用户」区分。
 * 本文件不另写标签或色值，避免两端漂移。
 */

export {
  ACCOUNT_STATUS_OPTIONS,
  editActionLabel,
  isPending,
  statusLabel,
  statusTone,
} from '@/lib/account-display'
