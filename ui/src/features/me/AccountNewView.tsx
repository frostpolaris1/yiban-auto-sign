/**
 * **功能**
 * 用户端新建账号深链：账号 hub＋自动打开新建向导模态。
 *
 * **归属**
 * `features/me` 装配口：流程本体在 `AccountWizard`，hub 在 `AccountView`。
 *
 * **口径**
 * 新建不再独占整页（弹窗式表单优先）；/new 仅保留深链与测试入口，
 * 关闭向导后 URL 归位 /app/account。detect-first 三步由 `AccountWizard` 管。
 */

import { AccountView } from './AccountView'

export function AccountNewView() {
  return <AccountView initialWizardOpen />
}