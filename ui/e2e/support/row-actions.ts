/**
 * **功能**
 * 账号表行内动作：桌面端操作列只露一条主操作，其余写动作（签到/验证/评审/暂停）收在
 * 「更多操作」菜单里；这里给出统一的打开与点击姿势。
 *
 * **归属**
 * `e2e/support`：只被 e2e 用例引用，不进产物。
 *
 * **口径**
 * 条目名用「开头正则」匹配 —— 条目里还会渲染置灰原因（`.dd-item-reason`），
 * 用全名精确匹配会随文案变化而碎。
 */

import { expect, type Locator, type Page } from '@playwright/test'

/** 点开某一行的「更多操作」，返回菜单容器。 */
export async function openRowMenu(page: Page, phone: string): Promise<Locator> {
  await page.getByRole('button', { name: `更多操作 ${phone}` }).click()
  const menu = page.getByRole('menu')
  await expect(menu).toBeVisible()
  return menu
}

/** 点开行菜单并点掉某个条目。 */
export async function clickRowMenuItem(page: Page, phone: string, name: RegExp): Promise<void> {
  const menu = await openRowMenu(page, phone)
  await menu.getByRole('menuitem', { name }).click()
  await expect(page.getByRole('menu')).toBeHidden()
}
