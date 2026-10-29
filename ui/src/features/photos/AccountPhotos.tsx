/**
 * **功能**
 * 账号抽屉的照片区：归属用户的图片库横条、上传、大图预览、单张删除、设为学校缺省图。
 *
 * **归属**
 * `features/photos` 展示层：取数与写操作经 `lib/api/photo`，取数缓存走 react-query。
 *
 * **口径**
 * 图片归属账号的**所有者**，不是当前操作者。
 * 契约：图片库与字节归 user/admin；上传与删除只归 user 且只归所有者本人。
 * 管理员看他人账号时该区只读；master 不在图片契约内，本区不渲染。
 * 横条只放缩略图；设为缺省图与删除都在大图预览里做，避免误触。
 * 字节只经单图端点取给 `<img src>`，本组件不读文件内容、不生成对象 URL。
 * 本大图与学校缺省图用不同缓存键；设为缺省图后两者一起失效。
 * 展示构建不加载真实图片：缩略位显示合成占位，上传与缺省图动作置灰。
 */

import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { ConfirmModal } from '@/components/ui/ConfirmModal'
import { Modal } from '@/components/ui/Modal'
import {
  deletePhoto,
  listAccountPhotos,
  photoBytesPath,
  setSchoolDefaultPhoto,
  uploadPhoto,
  type Photo,
  type PhotoPage,
} from '@/lib/api/photo'
import { describeApiError } from '@/lib/api/client'
import { SHOWCASE } from '@/lib/showcase-mode'
import { showcasePhotos } from '@/lib/showcase'
import { useMe } from '@/lib/session'

type AccountPhotosProps = {
  accountId: string
  schoolId: string
  ownerUserId: string
}


/** 抽屉里的页容量。多于此数只提示，不提供抽屉内翻页。 */
const PAGE_LIMIT = 24

export function AccountPhotos({ accountId, schoolId, ownerUserId }: AccountPhotosProps) {
  const me = useMe()
  const queryClient = useQueryClient()
  const fileInput = useRef<HTMLInputElement>(null)
  const [preview, setPreview] = useState<Photo | null>(null)
  const [previewOpener, setPreviewOpener] = useState<HTMLElement | null>(null)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [deleteOpener, setDeleteOpener] = useState<HTMLElement | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [defaultError, setDefaultError] = useState<string | null>(null)

  const photosQuery = useQuery({
    queryKey: ['account-photos', accountId],
    queryFn: (): Promise<PhotoPage> =>
      SHOWCASE
        ? Promise.resolve(showcasePhotos())
        : listAccountPhotos(accountId, { limit: PAGE_LIMIT }),
  })

  const role = me.data?.role ?? null
  // 契约：图片库与字节归 user/admin；上传与删除只归 user；缺省图设置归 admin。
  const isOwner = Boolean(me.data && me.data.id === ownerUserId)
  const canUpload = role === 'user' && isOwner && !SHOWCASE
  const canDelete = role === 'user' && isOwner && !SHOWCASE
  const defaultAllowed = role === 'admin' && !SHOWCASE

  const upload = useMutation({
    mutationFn: (file: File) => uploadPhoto(file),
    onMutate: () => setUploadError(null),
    onSuccess: async () => {
      setNotice(null)
      await queryClient.invalidateQueries({ queryKey: ['account-photos', accountId] })
    },
    onError: (error) => setUploadError(describeApiError(error)),
  })

  const remove = useMutation({
    mutationFn: (photoId: string) => deletePhoto(photoId),
    onSuccess: async () => {
      setConfirmingDelete(false)
      setPreview(null)
      await queryClient.invalidateQueries({ queryKey: ['account-photos', accountId] })
    },
  })

  const setDefault = useMutation({
    mutationFn: (photoId: string) => setSchoolDefaultPhoto(schoolId, photoId),
    onMutate: () => {
      setDefaultError(null)
      setNotice(null)
    },
    onSuccess: async () => {
      setNotice('已设为学校缺省图')
      setPreview(null)
      await queryClient.invalidateQueries({ queryKey: ['school-default-photo', schoolId] })
    },
    onError: (error) => setDefaultError(describeApiError(error)),
  })

  const items = photosQuery.data?.items ?? []
  const truncated = Boolean(photosQuery.data?.next_cursor)

  return (
    <section className="app-photo-section" aria-label="账号照片">
      <div className="app-photo-head">
        <h3 className="app-photo-title">照片</h3>
        {canUpload ? (
          <>
            <input
              ref={fileInput}
              className="app-visually-hidden"
              type="file"
              accept="image/png,image/jpeg"
              aria-label="选择照片文件"
              onChange={(event) => {
                const file = event.target.files?.[0]
                if (file) upload.mutate(file)
                event.target.value = ''
              }}
            />
            <Button
              variant="secondary"
              size="sm"
              disabled={upload.isPending}
              onClick={() => fileInput.current?.click()}
            >
              上传照片
            </Button>
          </>
        ) : null}
      </div>

      <p className="app-muted app-photo-note">图片属于账号所有者，只能由其本人上传。</p>

      {uploadError ? (
        <Alert tone="danger" title="上传失败" dismissible onDismiss={() => setUploadError(null)}>
          {uploadError}
        </Alert>
      ) : null}
      {notice ? (
        <Alert tone="success" dismissible onDismiss={() => setNotice(null)}>
          {notice}
        </Alert>
      ) : null}

      {photosQuery.isError ? (
        <Alert tone="danger" title="照片加载失败">
          {describeApiError(photosQuery.error)}
        </Alert>
      ) : null}

      {!photosQuery.isError && photosQuery.isLoading ? (
        <p className="app-muted">加载中…</p>
      ) : null}

      {!photosQuery.isError && !photosQuery.isLoading && items.length === 0 ? (
        <p className="app-muted">还没有照片</p>
      ) : null}

      {items.length > 0 ? (
        <ul className="app-photo-strip" aria-label="照片缩略条">
          {items.map((photo) => (
            <li key={photo.id} className="app-photo-thumb">
              <button
                type="button"
                className="app-photo-open"
                aria-label={`查看照片 ${photo.id}`}
                onClick={(event) => {
                  setPreviewOpener(event.currentTarget)
                  setPreview(photo)
                }}
              >
                {SHOWCASE ? (
                  <span className="app-photo-placeholder" role="img" aria-label={`照片 ${photo.id}`}>
                    示例
                  </span>
                ) : (
                  <img
                    className="app-photo-image"
                    src={photoBytesPath(photo.id)}
                    alt={`照片 ${photo.id}`}
                    loading="lazy"
                  />
                )}
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      {truncated ? (
        <p className="app-muted app-photo-note">仅显示最近 {PAGE_LIMIT} 张。</p>
      ) : null}

      <Modal
        open={preview !== null}
        onOpenChange={(open) => {
          if (!open) {
            setPreview(null)
            setDefaultError(null)
          }
        }}
        title="照片预览"
        description={preview ? `图片 ${preview.id}` : undefined}
        layer={2}
        returnFocusTo={previewOpener}
        footer={
          preview ? (
            <div className="app-photo-preview-actions">
              {defaultAllowed ? (
                <Button
                  variant="outline-primary"
                  aria-label={`设为缺省图 ${preview.id}`}
                  disabled={setDefault.isPending}
                  onClick={() => setDefault.mutate(preview.id)}
                >
                  设为缺省图
                </Button>
              ) : null}
              {canDelete ? (
                <Button
                  variant="outline-danger"
                  aria-label={`删除图片 ${preview.id}`}
                  onClick={(event) => {
                    setDeleteOpener(event.currentTarget)
                    setConfirmingDelete(true)
                  }}
                >
                  删除
                </Button>
              ) : null}
              <Button
                variant="ghost"
                onClick={() => {
                  setPreview(null)
                  setDefaultError(null)
                }}
              >
                关闭
              </Button>
            </div>
          ) : undefined
        }
      >
        {preview ? (
          <>
            {SHOWCASE ? (
              <div
                className="app-photo-preview app-photo-placeholder"
                role="img"
                aria-label="照片预览占位"
              >
                示例图片
              </div>
            ) : (
              <img
                className="app-photo-preview"
                src={photoBytesPath(preview.id)}
                alt={`照片 ${preview.id}`}
              />
            )}
            {defaultError ? (
              <Alert tone="danger" title="设为缺省图失败">
                {defaultError}
              </Alert>
            ) : null}
          </>
        ) : null}
      </Modal>

      <ConfirmModal
        open={confirmingDelete}
        title="删除图片"
        description={
          preview
            ? `图片 ${preview.id} 将从图片库移除。审计保留删除记录，字节按保留期回收。`
            : undefined
        }
        confirmText="删除图片"
        tone="danger"
        layer={3}
        pending={remove.isPending}
        error={remove.error ? describeApiError(remove.error) : null}
        returnFocusTo={deleteOpener}
        onConfirm={() => {
          if (preview) remove.mutate(preview.id)
        }}
        onClose={() => {
          setConfirmingDelete(false)
          remove.reset()
        }}
      />
    </section>
  )
}
