/**
 * **功能**
 * 学校抽屉的缺省图区：显示当前缺省图、清除缺省图。
 *
 * **归属**
 * `features/photos` 展示层：取数与写操作经 `lib/api/photo`。
 *
 * **口径**
 * 设置入口在账号照片区（选中某张图）；本区只负责展示与清除。
 * 本区只由 admin 装配：master 能改引用却读不到字节，界面不给入口。
 * 引用为空、或引用的图片已被删除时，接口 `photo_id` 为 null，本区显示「未设置」。
 * 展示构建不加载真实图片，也不发请求。
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { describeApiError } from '@/lib/api/client'
import {
  getSchoolDefaultPhoto,
  photoBytesPath,
  setSchoolDefaultPhoto,
  type SchoolDefaultPhoto,
} from '@/lib/api/photo'
import { SHOWCASE } from '@/lib/showcase-mode'

type SchoolDefaultPhotoPanelProps = {
  schoolId: string
}

/** 展示构建的固定回执：无后端，缺省图恒为空。 */
const EMPTY_DEFAULT: SchoolDefaultPhoto = { photo_id: null, etag: '' }

export function SchoolDefaultPhotoPanel({ schoolId }: SchoolDefaultPhotoPanelProps) {
  const queryClient = useQueryClient()

  const query = useQuery({
    queryKey: ['school-default-photo', schoolId],
    queryFn: (): Promise<SchoolDefaultPhoto> =>
      SHOWCASE ? Promise.resolve(EMPTY_DEFAULT) : getSchoolDefaultPhoto(schoolId),
  })

  const clear = useMutation({
    mutationFn: () => setSchoolDefaultPhoto(schoolId, null),
    onSuccess: async (value) => {
      queryClient.setQueryData(['school-default-photo', schoolId], value)
      await queryClient.invalidateQueries({ queryKey: ['school-default-photo', schoolId] })
    },
  })

  const photoId = query.data?.photo_id ?? null

  return (
    <section className="app-photo-section" aria-label="学校缺省图">
      <h3 className="app-photo-title">学校缺省图</h3>

      {query.isError ? (
        <Alert tone="danger" title="缺省图加载失败">
          {describeApiError(query.error)}
        </Alert>
      ) : null}

      {!query.isError && query.isLoading ? <p className="app-muted">加载中…</p> : null}

      {!query.isError && !query.isLoading && photoId ? (
        <div className="app-photo-tile">
          <img
            className="app-photo-image"
            src={photoBytesPath(photoId)}
            alt="学校缺省图"
            loading="lazy"
          />
          {!SHOWCASE ? (
            <div className="app-photo-actions">
              <Button
                variant="ghost"
                size="sm"
                disabled={clear.isPending}
                onClick={() => clear.mutate()}
              >
                清除缺省图
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}

      {!query.isError && !query.isLoading && !photoId ? (
        <p className="app-muted">未设置缺省图</p>
      ) : null}

      {clear.isError ? (
        <Alert tone="danger" title="清除失败">
          {describeApiError(clear.error)}
        </Alert>
      ) : null}

      <p className="app-muted app-photo-note">
        无图账号签到时取本图；本图也未设置时按缺图拦截。
      </p>
    </section>
  )
}
