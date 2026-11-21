/**
 * **功能**
 * 404 页：说明与返回入口。
 *
 * **归属**
 * `pages` 路由页。
 *
 * **口径**
 * 未知路径或已移除路由均落在这里。
 * 返回入口按当前会话角色分流：`user` 返回用户端首页 `/app`，其余返回管理端 `/`。
 */

import { Link } from '@tanstack/react-router'
import { landingForRole } from '@/lib/landing'
import { useMe } from '@/lib/session'

export function NotFoundPage() {
  const meQuery = useMe()
  const role = meQuery.data?.role
  const homeTo = role ? landingForRole(role) : '/'

  return (
    <div className="error-shell">
      <section className="error-card">
        <div className="error-code">404</div>
        <div className="error-title">页面不存在</div>
        <div className="error-sub">该路径没有对应页面，请检查地址是否正确。</div>
        <div className="error-actions">
          <Link className="btn btn--primary" to={homeTo}>
            返回仪表盘
          </Link>
        </div>
      </section>
    </div>
  )
}
