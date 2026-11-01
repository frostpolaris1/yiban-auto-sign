/**
 * **功能**
 * 学校管理页：只挂载学校域容器。
 *
 * **归属**
 * `pages` 路由页：页面逻辑留在 `features/schools`。
 *
 * **口径**
 * 路径 `/schools` 与 `nav.ts` 的叶子一一对应。
 * user 直接访问时由容器渲染无权限卡片，不发列表请求。
 */

import { SchoolsView } from '@/features/schools/SchoolsView'
import { SchoolDefaultPhotoPanel } from '@/features/photos/SchoolDefaultPhotoPanel'
import { SHOWCASE, useMe } from '@/lib/session'

export function SchoolsPage() {
  const me = useMe()
  const role = me.data?.role ?? null
  // 缺省图写入只归 admin；master 可读引用但无字节面，故本页只给 admin。
  const canManageDefault = SHOWCASE || role === 'admin'
  return (
    <SchoolsView
      defaultPhotoPanel={
        canManageDefault
          ? (school) => <SchoolDefaultPhotoPanel schoolId={school.id} />
          : undefined
      }
    />
  )
}
