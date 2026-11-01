import { test, expect, type Locator, type Page } from '@playwright/test'
import { mockAccountsApi } from './support/fake-accounts-api'
import { createPhotoFixture, PHOTO_ALPHA, PHOTO_BETA, PHOTO_BYTES } from './support/fake-photos'
import { mockSchoolsApi } from './support/fake-schools-api'

/**
 * 照片域的不变式（编号见各用例标题）。
 *
 * 1. 管理员在账号抽屉看到归属用户的图片横条，字节只经单图端点取。
 * 2. 普通用户在本人账号上可上传；上传成功后库就地刷新。
 * 3. 上传被拒时库不变，服务端消息可见。
 * 4. 删除在大图预览里发起并经确认；确认前零请求；确认后缩略图消失。
 * 5. 他人账号只读：无上传、无删除，但管理员可设学校缺省图。
 * 6. 设为缺省图只送 photo_id；成功后给出可见回执。
 * 7. 学校抽屉显示缺省图并可清除；清除送显式 null。
 * 8. master 没有账号照片面：账号照片端点不含 master，学校抽屉也不给缺省图区。
 * 9. 设为缺省图失败时给危险提示，不挂成功样式。
 * 10. 所有者是 admin 时同样可上传（角色不改变所有者身份）。
 * 11. 缺省图字节读不到时退化为一句说明，不留破图。
 *
 * 角色口径来自契约 x-roles：账号照片列表归 user/admin；/api/me/photos 归三种
 * 角色且只操作自己的库；缺省图写入归 admin（界面只给 admin：master 没有字节面）。
 */

async function openAccounts(page: Page) {
  await page.goto('/accounts')
  await expect(page.getByRole('table', { name: '账号列表' })).toBeVisible()
}

async function openAccountDrawer(page: Page, phone: string) {
  await page.getByRole('button', { name: `查看 ${phone}` }).click()
  const drawer = page.getByRole('dialog', { name: '账号详情' })
  await expect(drawer).toBeVisible()
  return drawer
}

async function openSchoolDrawer(page: Page, name: string) {
  await page.goto('/schools')
  await expect(page.getByRole('table', { name: '学校列表' })).toBeVisible()
  await page.getByRole('button', { name: `查看 ${name}` }).click()
  const drawer = page.getByRole('dialog', { name: '学校详情' })
  await expect(drawer).toBeVisible()
  return drawer
}

function photoCalls(
  api: { calls: { method: string; path: string; body: unknown }[] },
  method: string,
) {
  return api.calls.filter((call) => call.method === method)
}

async function uploadFile(scope: Page | Locator) {
  await scope
    .locator('input[type="file"]')
    .setInputFiles({ name: 'synthetic-photo.png', mimeType: 'image/png', buffer: PHOTO_BYTES })
}

test('invariant 1: an admin sees the owner library as a strip and fetches bytes one by one', async ({
  page,
}) => {
  const api = await mockAccountsApi(page, { role: 'admin' })
  await openAccounts(page)
  const drawer = await openAccountDrawer(page, '138****8000')

  const strip = drawer.getByRole('list', { name: '照片缩略条' })
  await expect(strip).toBeVisible()
  await expect(drawer.getByRole('img', { name: `照片 ${PHOTO_ALPHA.id}` })).toBeVisible()
  await expect(drawer.getByRole('img', { name: `照片 ${PHOTO_BETA.id}` })).toBeVisible()
  expect(
    photoCalls(api, 'GET').some((call) => call.path === '/api/accounts/acc-1/photos'),
    'the drawer reads the account photo page',
  ).toBe(true)

  const src = await drawer.getByRole('img', { name: `照片 ${PHOTO_ALPHA.id}` }).getAttribute('src')
  expect(src, 'image bytes come from the single photo endpoint').toContain(
    `/api/me/photos/${PHOTO_ALPHA.id}`,
  )
})

test('invariant 2: the owner uploads a photo and the library refreshes', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: 'user' })
  await openAccounts(page)
  const drawer = await openAccountDrawer(page, '138****8000')

  await uploadFile(drawer)

  await expect(drawer.getByRole('img')).toHaveCount(3)
  const upload = photoCalls(api, 'POST').find((call) => call.path === '/api/me/photos')
  expect(upload, 'upload posts the multipart body').toBeTruthy()
  expect(api.photos.byOwner['user-1']).toHaveLength(3)
  await expect(drawer).not.toContainText('上传失败')
})

test('invariant 3: a rejected upload keeps the library and shows the server message', async ({
  page,
}) => {
  const fixture = createPhotoFixture({
    uploadError: { status: 413, code: 'payload_too_large', message: '图片超过 10 MiB' },
  })
  await mockAccountsApi(page, { role: 'user', photos: fixture })
  await openAccounts(page)
  const drawer = await openAccountDrawer(page, '138****8000')

  await uploadFile(drawer)

  await expect(drawer).toContainText('图片超过 10 MiB')
  await expect(drawer.getByRole('img')).toHaveCount(2)
})

test('invariant 4: delete starts in the preview and asks before removing', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: 'user' })
  await openAccounts(page)
  const drawer = await openAccountDrawer(page, '138****8000')

  await drawer.getByRole('button', { name: `查看照片 ${PHOTO_ALPHA.id}` }).click()
  const preview = page.getByRole('dialog', { name: '照片预览' })
  await expect(preview).toBeVisible()
  await expect(preview.getByRole('button', { name: /设为缺省图/ })).toHaveCount(0)

  await preview.getByRole('button', { name: `删除图片 ${PHOTO_ALPHA.id}` }).click()
  const confirm = page.getByRole('dialog', { name: '删除图片' })
  await expect(confirm).toBeVisible()
  expect(photoCalls(api, 'DELETE'), 'no delete request before the confirmation').toHaveLength(0)

  await confirm.getByRole('button', { name: '删除图片' }).click()
  await expect(preview).toBeHidden()
  await expect(drawer.getByRole('img', { name: `照片 ${PHOTO_ALPHA.id}` })).toHaveCount(0)
  await expect(drawer.getByRole('img')).toHaveCount(1)
  expect(
    photoCalls(api, 'DELETE').some((call) => call.path === `/api/me/photos/${PHOTO_ALPHA.id}`),
    'the confirmation sends the delete',
  ).toBe(true)
})

test("invariant 5: another owner's library is read-only for an admin", async ({ page }) => {
  await mockAccountsApi(page, { role: 'admin' })
  await openAccounts(page)
  const drawer = await openAccountDrawer(page, '139****9000')

  const other = drawer.getByRole('img', { name: '照片 photo-gamma' })
  await expect(other).toBeVisible()
  // 可见不等于加载成功：admin 的跨属主读在这里必须真取到字节。
  expect(await other.evaluate((el) => (el as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)
  await expect(drawer.getByRole('button', { name: '上传照片' })).toHaveCount(0)

  await drawer.getByRole('button', { name: '查看照片 photo-gamma' }).click()
  const preview = page.getByRole('dialog', { name: '照片预览' })
  await expect(preview.getByRole('button', { name: '设为缺省图 photo-gamma' })).toBeVisible()
  await expect(preview.getByRole('button', { name: '删除图片 photo-gamma' })).toHaveCount(0)
})

test('invariant 6: setting the school default sends only the photo id', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: 'admin' })
  await openAccounts(page)
  const drawer = await openAccountDrawer(page, '138****8000')

  await drawer.getByRole('button', { name: `查看照片 ${PHOTO_BETA.id}` }).click()
  const preview = page.getByRole('dialog', { name: '照片预览' })
  await preview.getByRole('button', { name: `设为缺省图 ${PHOTO_BETA.id}` }).click()

  await expect(drawer).toContainText('已设为学校缺省图')
  await expect(preview).toBeHidden()
  const put = photoCalls(api, 'PUT').find(
    (call) => call.path === '/api/schools/school-a/default-photo',
  )
  expect(put, 'the request goes to the account school').toBeTruthy()
  expect(Object.keys(put!.body as object)).toEqual(['photo_id'])
  expect((put!.body as { photo_id: string }).photo_id).toBe(PHOTO_BETA.id)
})

test('invariant 7: the school drawer shows the default photo and clears it', async ({ page }) => {
  const api = await mockSchoolsApi(page, { role: 'admin' })
  const drawer = await openSchoolDrawer(page, '合成学校甲')

  await expect(drawer.getByRole('img', { name: '学校缺省图' })).toBeVisible()
  await drawer.getByRole('button', { name: '清除缺省图' }).click()
  await expect(drawer).toContainText('未设置缺省图')
  await expect(drawer.getByRole('img', { name: '学校缺省图' })).toHaveCount(0)

  const put = photoCalls(api, 'PUT').find(
    (call) => call.path === '/api/schools/school-alpha/default-photo',
  )
  expect(put, 'clearing sends an explicit null').toBeTruthy()
  expect((put!.body as { photo_id: string | null }).photo_id).toBeNull()
})

test('invariant 8: master has no account-photo surface', async ({
  page,
}) => {
  await mockAccountsApi(page, { role: 'master' })
  await openAccounts(page)
  const accountDrawer = await openAccountDrawer(page, '138****8000')
  await expect(accountDrawer.getByRole('region', { name: '账号照片' })).toHaveCount(0)
  await expect(accountDrawer.getByRole('button', { name: '上传照片' })).toHaveCount(0)

  await mockSchoolsApi(page, { role: 'master' })
  const schoolDrawer = await openSchoolDrawer(page, '合成学校甲')
  await expect(schoolDrawer.getByRole('region', { name: '学校缺省图' })).toHaveCount(0)
})

test('invariant 9: a failed default-photo write shows a danger alert, not a success one', async ({
  page,
}) => {
  const fixture = createPhotoFixture({
    defaultError: { status: 400, code: 'validation', message: '图片不属于本校' },
  })
  await mockAccountsApi(page, { role: 'admin', photos: fixture })
  await openAccounts(page)
  const drawer = await openAccountDrawer(page, '138****8000')

  await drawer.getByRole('button', { name: `查看照片 ${PHOTO_BETA.id}` }).click()
  const preview = page.getByRole('dialog', { name: '照片预览' })
  await preview.getByRole('button', { name: `设为缺省图 ${PHOTO_BETA.id}` }).click()

  const alert = preview.getByRole('alert')
  await expect(alert).toBeVisible()
  await expect(alert).toContainText('图片不属于本校')

  // 用页脚「关闭」关预览：该路径曾经保留过期错误。重开同一张图时不得再出现。
  await preview.getByRole('button', { name: '关闭', exact: true }).click()
  await expect(preview).toBeHidden()
  await expect(drawer).toBeVisible()
  await expect(drawer).not.toContainText('已设为学校缺省图')

  await drawer.getByRole('button', { name: `查看照片 ${PHOTO_BETA.id}` }).click()
  const reopened = page.getByRole('dialog', { name: '照片预览' })
  await expect(reopened).toBeVisible()
  await expect(reopened.getByRole('alert')).toHaveCount(0)
})

test('fixture contract: a user cannot read or delete another owner photo', async ({ page }) => {
  await mockAccountsApi(page, { role: 'user' })
  await openAccounts(page)

  // 页面从不发这两个请求；这里直接钉住假 API 与契约一致的语义（越权＝不存在）。
  const readStatus = await page.evaluate(async () => {
    const response = await fetch('/api/me/photos/photo-gamma')
    return response.status
  })
  expect(readStatus, 'cross-owner read answers 404').toBe(404)

  const deleteStatus = await page.evaluate(async () => {
    const response = await fetch('/api/me/photos/photo-gamma', { method: 'DELETE' })
    return response.status
  })
  expect(deleteStatus, 'cross-owner delete answers 404').toBe(404)
})

test('fixture contract: an admin who does not own the photo gets 404', async ({ page }) => {
  await mockAccountsApi(page, { role: 'admin' })
  await openAccounts(page)

  // 会话用户是 user-1；photo-gamma 归 user-2。契约里删除只按属主过滤。
  const status = await page.evaluate(async () => {
    const response = await fetch('/api/me/photos/photo-gamma', { method: 'DELETE' })
    return response.status
  })
  expect(status, 'a delete by a non-owner answers 404').toBe(404)
})

test('invariant 10: an admin who owns the account may upload', async ({ page }) => {
  const api = await mockAccountsApi(page, { role: 'admin' })
  await openAccounts(page)
  const drawer = await openAccountDrawer(page, '138****8000')

  // 会话用户是 user-1，acc-1 也归 user-1：此时 admin 就是所有者本人。
  await expect(drawer.getByRole('button', { name: '上传照片' })).toBeVisible()
  await uploadFile(drawer)
  await expect(drawer.getByRole('img')).toHaveCount(3)
  expect(
    photoCalls(api, 'POST').some((call) => call.path === '/api/me/photos'),
    'the owner role does not gate the upload',
  ).toBe(true)

  await drawer.getByRole('button', { name: `查看照片 ${PHOTO_BETA.id}` }).click()
  const preview = page.getByRole('dialog', { name: '照片预览' })
  await preview.getByRole('button', { name: `删除图片 ${PHOTO_BETA.id}` }).click()
  await page
    .getByRole('dialog', { name: '删除图片' })
    .getByRole('button', { name: '删除图片' })
    .click()
  await expect(drawer.getByRole('img', { name: `照片 ${PHOTO_BETA.id}` })).toHaveCount(0)
  expect(
    photoCalls(api, 'DELETE').some((call) => call.path === `/api/me/photos/${PHOTO_BETA.id}`),
    'the owner role does not gate the delete either',
  ).toBe(true)
})

test('invariant 11: an unreadable default photo degrades to a note', async ({ page }) => {
  await mockAccountsApi(page, { role: 'admin' })
  // 模拟属主账号已停用：引用还在，字节端点回 404。
  await page.route('**/api/me/photos/photo-alpha', (route) =>
    route.fulfill({ status: 404, contentType: 'application/json', body: '{}' }),
  )
  await openSchoolDrawer(page, '合成学校甲')

  const drawer = page.getByRole('dialog', { name: '学校详情' })
  await expect(drawer.getByText('缺省图暂不可读：属主账号可能已停用。')).toBeVisible()
  await expect(drawer.getByRole('img', { name: '学校缺省图' })).toHaveCount(0)
})

test('fixture contract: a signed-out read answers 401', async ({ page }) => {
  await mockAccountsApi(page, { role: null })
  await page.goto('/login')

  const listStatus = await page.evaluate(async () => (await fetch('/api/me/photos')).status)
  expect(listStatus, 'a signed-out library read answers 401').toBe(401)

  const putStatus = await page.evaluate(async () => {
    const response = await fetch('/api/schools/school-alpha/default-photo', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ photo_id: null }),
    })
    return response.status
  })
  expect(putStatus, 'a signed-out default-photo write answers 401').toBe(401)
})

test('fixture contract: deleting the same photo twice answers 204 both times', async ({ page }) => {
  await mockAccountsApi(page, { role: 'user' })
  await page.goto('/accounts')

  const statuses = await page.evaluate(async () => {
    const first = await fetch('/api/me/photos/photo-alpha', { method: 'DELETE' })
    const second = await fetch('/api/me/photos/photo-alpha', { method: 'DELETE' })
    return [first.status, second.status]
  })
  expect(statuses, 'delete is idempotent').toEqual([204, 204])
})
