/**
 * **功能**
 * 学校抽屉的缺省图区：显示当前缺省图、清除缺省图。
 *
 * **归属**
 * `features/photos` 展示层：取数与写操作经 `lib/api/photo`。
 *
 * **口径**
 * 设置入口在账号照片区（选中某张图）；本区只负责展示与清除。
 * 清除是不可逆的写：按钮只开确认弹窗，弹窗里点「确认清除」才发 PUT null，
 * 文案写明后果（未上传照片的账号将按缺图拦截）。
 * 读取失败给「重试」，只重取本区的缺省图引用，不影响学校信息与照片区。
 * 本区只由 admin 装配：缺省图写入归 admin；master 可读引用但无字节面，界面不给入口。
 * D6 乙案：设默认＝复制登记学校自有资产行，`photo_id` 回显资产行 id；
 * 源属主删图/注销/purge 都不动默认，仅显式清除（PUT null）才为 null。
 * 字节经 `GET /api/me/photos/{photo_id}` 读取（绑定校 admin 对资产行放行）；
 * 读取失败以 onError 退化为一句说明，不留破图。
 * 展示构建不加载真实图片，也不发请求。
 */

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { ConfirmModal } from '@/components/ui/ConfirmModal'
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
  const [brokenId, setBrokenId] = useState<string | null>(null)
  const [confirmingClear, setConfirmingClear] = useState(false)
  const [clearOpener, setClearOpener] = useState<HTMLElement | null>(null)

  const query = useQuery({
    queryKey: ['school-default-photo', schoolId],
    queryFn: (): Promise<SchoolDefaultPhoto> =>
      SHOWCASE ? Promise.resolve(EMPTY_DEFAULT) : getSchoolDefaultPhoto(schoolId),
  })

  const clear = useMutation({
    mutationFn: () => setSchoolDefaultPhoto(schoolId, null),
    onSuccess: async (value) => {
      queryClient.setQueryData(['school-default-photo', schoolId], value)
      setConfirmingClear(false)
      await queryClient.invalidateQueries({ queryKey: ['school-default-photo', schoolId] })
    },
  })

  const photoId = query.data?.photo_id ?? null

  // 引用变了就重新给字节一次机会；失败只在当前 id 上记住。
  useEffect(() => {
    setBrokenId(null)
  }, [photoId])

  return (
    <section className="app-photo-section" aria-label="学校缺省图">
      <h3 className="app-photo-title">学校缺省图</h3>

      {query.isError ? (
        <Alert tone="danger" title="缺省图加载失败">
          {describeApiError(query.error)}{' '}
          <Button variant="ghost" size="sm" onClick={() => void query.refetch()}>
            重试
          </Button>
        </Alert>
      ) : null}

      {!query.isError && query.isLoading ? <p className="app-muted">加载中…</p> : null}

      {!query.isError && !query.isLoading && photoId ? (
        <div className="app-photo-tile">
          {brokenId === photoId ? (
            <p className="app-muted">缺省图暂不可读：属主账号可能已停用。</p>
          ) : (
            <img
              className="app-photo-image"
              src={photoBytesPath(photoId)}
              alt="学校缺省图"
              loading="lazy"
              onError={() => setBrokenId(photoId)}
            />
          )}
          {!SHOWCASE ? (
            <div className="app-photo-actions">
              <Button
                variant="ghost"
                size="sm"
                disabled={clear.isPending}
                onClick={(event) => {
                  setClearOpener(event.currentTarget)
                  setConfirmingClear(true)
                }}
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

      {!confirmingClear && clear.isError ? (
        <Alert tone="danger" title="清除失败">
          {describeApiError(clear.error)}
        </Alert>
      ) : null}

      <p className="app-muted app-photo-note">
        无图账号签到时取本图；本图也未设置时按缺图拦截。
      </p>

      <ConfirmModal
        open={confirmingClear}
        // 本区常驻在学校抽屉里：抽屉是第 1 层，确认弹窗必须叠到第 2 层。
        layer={2}
        title="清除学校缺省图？"
        description="清除后，本学校还没上传照片的账号签到时将按缺图拦截，直到重新设置缺省图。审计保留本次清除记录。"
        confirmText="确认清除"
        tone="danger"
        pending={clear.isPending}
        error={clear.isError ? describeApiError(clear.error) : null}
        returnFocusTo={clearOpener}
        onConfirm={() => clear.mutate()}
        onClose={() => {
          setConfirmingClear(false)
          clear.reset()
        }}
      />
    </section>
  )
}
