import type { Route } from '@playwright/test'
import type { Role } from './fake-accounts-api'

/**
 * 照片域的假 API。
 *
 * **功能**
 * 处理图片库五端点与学校缺省图读写。
 *
 * **归属**
 * `e2e/support`：账号页与学校页的假 API 都转发到这里。
 *
 * **口径**
 * 角色按契约 `x-roles` 判：图片库与字节只归 user/admin，上传与删除只归 user。
 * 跨归属读取与删除回 404，与契约同语义。
 * 调用记录由调用方（父假 API）统一收集，本模块只回响应。
 * 图片字节恒为同一张合成 PNG：1×1 单色，不含任何真实内容。
 */

export type Photo = {
  id: string
  sha256: string
  mime: 'image/jpeg' | 'image/png'
  width: number
  height: number
  bytes: number
  created_at: string
}

const SHA_ALPHA = 'a'.repeat(64)
const SHA_BETA = 'b'.repeat(64)

/** 合成照片元数据。ID 明示形状，sha256 取单字符重复，属登记表口径。 */
export const PHOTO_ALPHA: Photo = {
  id: 'photo-alpha',
  sha256: SHA_ALPHA,
  mime: 'image/png',
  width: 640,
  height: 480,
  bytes: 1024,
  created_at: '2026-10-20T01:00:00Z',
}

export const PHOTO_BETA: Photo = {
  id: 'photo-beta',
  sha256: SHA_BETA,
  mime: 'image/png',
  width: 800,
  height: 600,
  bytes: 2048,
  created_at: '2026-10-21T02:30:00Z',
}

/** 1×1 单色 PNG。合成值：像素数据全零，不对应任何真实图片。 */
export const PHOTO_BYTES = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
)

export type PhotoFixture = {
  /** 会话用户 ID；`/api/me/photos` 取它的库。 */
  owner: string
  /** 账号 ID → 归属用户 ID。 */
  accountOwner: Record<string, string>
  /** 归属用户 ID → 图片库。上传与删除都改这里。 */
  byOwner: Record<string, Photo[]>
  /** 学校 ID → 缺省图引用。 */
  schoolDefaults: Record<string, { photo_id: string | null }>
  /** 上传失败注入。缺省 201。 */
  uploadError?: { status: number; code: string; message: string }
  /** 设为缺省图的失败注入。 */
  defaultError?: { status: number; code: string; message: string }
}

/**
 * 造一份照片夹具。
 *
 * 默认形状：user-1 拥有 acc-1 与 acc-3，user-2 拥有 acc-2。
 * user-1 的库有两张，school-a 的缺省图是 photo-alpha。
 */
export function createPhotoFixture(overrides: Partial<PhotoFixture> = {}): PhotoFixture {
  return {
    owner: 'user-1',
    accountOwner: { 'acc-1': 'user-1', 'acc-2': 'user-2', 'acc-3': 'user-1' },
    byOwner: {
      'user-1': [{ ...PHOTO_ALPHA }, { ...PHOTO_BETA }],
      'user-2': [
        {
          id: 'photo-gamma',
          sha256: 'c'.repeat(64),
          mime: 'image/png',
          width: 320,
          height: 240,
          bytes: 512,
          created_at: '2026-10-22T03:00:00Z',
        },
      ],
    },
    schoolDefaults: {
      // 账号页的学校 ID 族是 school-a / school-b；学校页的是 school-alpha / school-beta。
      // 两套假 API 各有自己的 ID 族，这里都预置，避免任一侧取不到引用。
      'school-a': { photo_id: 'photo-alpha' },
      'school-b': { photo_id: null },
      'school-alpha': { photo_id: 'photo-alpha' },
      'school-beta': { photo_id: null },
    },
    ...overrides,
  }
}

function errorBody(code: string, message: string) {
  return JSON.stringify({
    error: { code, message, details: { source: 'fake-photos' }, trace_id: 'trace-test' },
  })
}

function etagOf(photo: Photo): string {
  return photo.sha256
}

function findPhoto(fixture: PhotoFixture, id: string): Photo | undefined {
  for (const photos of Object.values(fixture.byOwner)) {
    const hit = photos.find((photo) => photo.id === id)
    if (hit) return hit
  }
  return undefined
}

function ownerOf(fixture: PhotoFixture, id: string): string | undefined {
  for (const [owner, photos] of Object.entries(fixture.byOwner)) {
    if (photos.some((photo) => photo.id === id)) return owner
  }
  return undefined
}

/** 按落库顺序生成下一张图的 ID。ID 只用于测试断言，不模拟真实主键。 */
function nextPhotoId(fixture: PhotoFixture): string {
  const total = Object.values(fixture.byOwner).reduce((sum, photos) => sum + photos.length, 0)
  return `photo-upload-${total + 1}`
}

/**
 * 处理一条照片相关请求。
 *
 * `role()` 取当前会话角色：账号页的假 API 会在登录/过期时改变角色。
 * 返回 true 表示已回包；false 表示不是照片路径，调用方继续。
 */
export async function handlePhotoRoute(
  route: Route,
  request: { method(): string; headers(): Record<string, string> },
  url: URL,
  fixture: PhotoFixture,
  role: () => Role | null,
): Promise<boolean> {
  const json = (body: unknown, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  const fail = (status: number, code: string, message: string) =>
    route.fulfill({ status, contentType: 'application/json', body: errorBody(code, message) })

  const method = request.method()
  const path = url.pathname
  const current = role()

  const accountMatch = /^\/api\/accounts\/([^/]+)\/photos$/.exec(path)
  if (accountMatch && method === 'GET') {
    if (current !== 'user' && current !== 'admin') {
      await fail(403, 'forbidden', '当前角色不能读取图片库')
      return true
    }
    const accountId = decodeURIComponent(accountMatch[1])
    const owner = fixture.accountOwner[accountId]
    if (!owner) {
      await fail(404, 'not_found', '账号不存在')
      return true
    }
    if (current === 'user' && owner !== fixture.owner) {
      await fail(404, 'not_found', '账号不存在')
      return true
    }
    await json({ items: fixture.byOwner[owner] ?? [], next_cursor: '' })
    return true
  }

  const schoolDefaultMatch = /^\/api\/schools\/([^/]+)\/default-photo$/.exec(path)
  if (schoolDefaultMatch) {
    const schoolId = decodeURIComponent(schoolDefaultMatch[1])
    const currentDefault = fixture.schoolDefaults[schoolId]
    if (!currentDefault) {
      await fail(404, 'not_found', '学校不存在')
      return true
    }
    if (method === 'GET') {
      const photo = currentDefault.photo_id
        ? findPhoto(fixture, currentDefault.photo_id)
        : undefined
      await json({
        photo_id: currentDefault.photo_id,
        etag: photo ? etagOf(photo) : '',
      })
      return true
    }
    if (method === 'PUT') {
      if (current !== 'master' && current !== 'admin') {
        await fail(403, 'forbidden', '只有管理员可以设置缺省图')
        return true
      }
      if (fixture.defaultError) {
        const { status, code, message } = fixture.defaultError
        await fail(status, code, message)
        return true
      }
      const body = route.request().postDataJSON() as { photo_id: string | null }
      if (body.photo_id !== null && !findPhoto(fixture, body.photo_id)) {
        await fail(404, 'not_found', '图片不存在')
        return true
      }
      fixture.schoolDefaults[schoolId] = { photo_id: body.photo_id }
      const photo = body.photo_id ? findPhoto(fixture, body.photo_id) : undefined
      await json({ photo_id: body.photo_id, etag: photo ? etagOf(photo) : '' })
      return true
    }
  }

  if (path === '/api/me/photos') {
    if (method === 'GET') {
      if (current !== 'user') {
        await fail(403, 'forbidden', '图片库只归普通用户')
        return true
      }
      await json({ items: fixture.byOwner[fixture.owner] ?? [], next_cursor: '' })
      return true
    }
    if (method === 'POST') {
      if (current !== 'user') {
        await fail(403, 'forbidden', '图片库只归普通用户')
        return true
      }
      if (fixture.uploadError) {
        const { status, code, message } = fixture.uploadError
        await fail(status, code, message)
        return true
      }
      const library = fixture.byOwner[fixture.owner] ?? []
      const created: Photo = {
        id: nextPhotoId(fixture),
        sha256: 'd'.repeat(64),
        mime: 'image/png',
        width: 1,
        height: 1,
        bytes: PHOTO_BYTES.byteLength,
        created_at: '2026-10-30T00:00:00Z',
      }
      library.push(created)
      fixture.byOwner[fixture.owner] = library
      await json(created, 201)
      return true
    }
  }

  const photoMatch = /^\/api\/me\/photos\/([^/]+)$/.exec(path)
  if (photoMatch) {
    if (current !== 'user' && current !== 'admin') {
      await fail(403, 'forbidden', '当前角色不能读取图片')
      return true
    }
    const id = decodeURIComponent(photoMatch[1])
    const photo = findPhoto(fixture, id)
    if (!photo) {
      await fail(404, 'not_found', '图片不存在')
      return true
    }
    if (current === 'user' && ownerOf(fixture, id) !== fixture.owner) {
      await fail(404, 'not_found', '图片不存在')
      return true
    }
    if (method === 'GET') {
      const ifNoneMatch = request.headers()['if-none-match']
      if (ifNoneMatch === `"${photo.sha256}"`) {
        await route.fulfill({ status: 304, headers: { ETag: `"${photo.sha256}"` } })
        return true
      }
      await route.fulfill({
        status: 200,
        contentType: photo.mime,
        headers: { ETag: `"${photo.sha256}"` },
        body: PHOTO_BYTES,
      })
      return true
    }
    if (method === 'DELETE') {
      // 两段判，与后端一致：动作无角色 403；跨属主按 owner_user_id 过滤后无行 404。
      if (current !== 'user') {
        await fail(403, 'forbidden', '图片库只归普通用户')
        return true
      }
      if (ownerOf(fixture, id) !== fixture.owner) {
        await fail(404, 'not_found', '图片不存在')
        return true
      }
      for (const [owner, photos] of Object.entries(fixture.byOwner)) {
        fixture.byOwner[owner] = photos.filter((item) => item.id !== id)
      }
      await route.fulfill({ status: 204 })
      return true
    }
  }

  return false
}
