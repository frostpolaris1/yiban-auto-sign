/**
 * **功能**
 * 图片域 API：类型别名、路径表与调用。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径与请求体，不含展示逻辑。
 *
 * **口径**
 * 类型来自生成的 `schema.d.ts`，不手工重写。
 * 图片字节只经 `GET /api/me/photos/{id}` 取；`photoBytesPath` 是唯一拼法。
 * 上传走 multipart，字段名 `image`；请求体上限由服务端判，前端不预判尺寸。
 * 列表是游标式：末页 `next_cursor` 是空串。
 */

import type { components, operations } from './schema'
import { apiRequest, apiUpload } from './client'

type Schemas = components['schemas']

export type Photo = Schemas['Photo']
export type PhotoPage = Schemas['PhotoPage']
export type SchoolDefaultPhoto = Schemas['SchoolDefaultPhoto']

/** 查询串视图。字段直接取契约生成的参数，不另写一份。 */
export type PhotoQuery = NonNullable<operations['PhotoList']['parameters']['query']>

export const PHOTO_PATHS = {
  mine: '/api/me/photos',
  schoolDefault: (schoolId: string) =>
    `/api/schools/${encodeURIComponent(schoolId)}/default-photo`,
  account: (accountId: string) => `/api/accounts/${encodeURIComponent(accountId)}/photos`,
} as const

/** 单图字节地址。给 `<img src>` 用：会话 cookie 同源自动携带。 */
export function photoBytesPath(photoId: string): string {
  return `/api/me/photos/${encodeURIComponent(photoId)}`
}

export function buildPhotoSearch(query: PhotoQuery = {}): string {
  const params = new URLSearchParams()
  if (query.cursor) params.set('cursor', query.cursor)
  if (query.limit) params.set('limit', String(query.limit))
  const search = params.toString()
  return search ? `?${search}` : ''
}

export function listAccountPhotos(
  accountId: string,
  query: PhotoQuery = {},
): Promise<PhotoPage> {
  return apiRequest<PhotoPage>(
    `${PHOTO_PATHS.account(accountId)}${buildPhotoSearch(query)}`,
  )
}

export function uploadPhoto(file: File): Promise<Photo> {
  const form = new FormData()
  form.append('image', file)
  return apiUpload<Photo>(PHOTO_PATHS.mine, form)
}

export function deletePhoto(photoId: string): Promise<void> {
  return apiRequest<void>(photoBytesPath(photoId), { method: 'DELETE' })
}

export function getSchoolDefaultPhoto(schoolId: string): Promise<SchoolDefaultPhoto> {
  return apiRequest<SchoolDefaultPhoto>(PHOTO_PATHS.schoolDefault(schoolId))
}

export function setSchoolDefaultPhoto(
  schoolId: string,
  photoId: string | null,
): Promise<SchoolDefaultPhoto> {
  return apiRequest<SchoolDefaultPhoto>(PHOTO_PATHS.schoolDefault(schoolId), {
    method: 'PUT',
    body: JSON.stringify({ photo_id: photoId }),
  })
}
