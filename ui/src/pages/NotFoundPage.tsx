import { Link } from '@tanstack/react-router'

export function NotFoundPage() {
  return (
    <div className="error-shell">
      <section className="error-card">
        <div className="error-code">404</div>
        <div className="error-title">页面不存在</div>
        <div className="error-sub">该路径没有对应页面。P1 骨架只提供首页、组件画廊与登录页。</div>
        <div className="error-actions">
          <Link className="btn btn--primary" to="/">
            返回仪表盘
          </Link>
        </div>
      </section>
    </div>
  )
}
