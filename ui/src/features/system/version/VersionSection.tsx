/**
 * **功能**
 * `/system#version` 版本与自检分区：前端版本如实展示，后端版本陈述来源缺失，
 * 存活自检走公开探针 `GET /healthz`。
 *
 * **归属**
 * `features/system/version` 分区组件：仅由 `SystemView` 装配。
 *
 * **口径**
 * - 前端版本＝`lib/version` 的构建期常量（vite 注入 `ui/package.json` 版本），真实可得。
 * - 后端版本三元组（version/commit/buildTime）经发布 ldflags 注入 `server/internal/buildinfo`，
 *   只由 `yiban-server --version` 与 CLI 打印；契约无任何端点导出它，名册键亦无版本项。
 *   依「无真来源不渲染、不造」：本区只陈述该事实，不显示任何臆造值。
 * - 自检＝恰一次 `GET /healthz`（公开、无会话、不触库），成功显「进程在应答」，
 *   失败给显式错误与重试；展示构建走合成夹具，零网络。
 */

import { useQuery } from '@tanstack/react-query'
import { AsyncState } from '@/components/ui/AsyncState'
import { Badge } from '@/components/ui/Badge'
import { Card } from '@/components/ui/Card'
import { HEALTH_QUERY_KEY, getHealthStatus } from '@/lib/api/health'
import { showcaseHealthStatus } from '@/lib/showcase'
import { SHOWCASE } from '@/lib/showcase-mode'
import { APP_VERSION } from '@/lib/version'

export function VersionSection() {
  const healthQuery = useQuery({
    queryKey: HEALTH_QUERY_KEY,
    queryFn: () => (SHOWCASE ? Promise.resolve(showcaseHealthStatus()) : getHealthStatus()),
    staleTime: 30_000,
  })

  const healthStatus = healthQuery.isPending
    ? 'loading'
    : healthQuery.isError
      ? 'error'
      : healthQuery.data?.status === 'ok'
        ? 'data'
        : 'empty'

  return (
    <section id="version" className="sys-section" tabIndex={-1} aria-labelledby="version-section-heading">
      <div className="sys-section-head">
        <h2 id="version-section-heading" className="sys-section-title">
          版本与自检
        </h2>
        <p className="sys-section-desc">
          版本事实与进程存活自检：有来源的如实展示，无来源的不渲染、不臆造。
        </p>
      </div>

      <Card eyebrow="Version & Health" title="版本与存活自检" className="sys-policy-card sys-policy-card-wide">
        <div className="sys-version-rows">
          <div className="sys-version-row">
            <span className="sys-version-label">前端版本</span>
            <span className="sys-version-value">v{APP_VERSION}</span>
            <span className="sys-version-note">构建期注入 <code>ui/package.json</code> 版本，随前端产物发布。</span>
          </div>
          <div className="sys-version-row">
            <span className="sys-version-label">后端版本</span>
            <span className="sys-version-value">暂无来源</span>
            <span className="sys-version-note">
              后端版本三元组（version / commit / buildTime）由发布构建注入二进制，仅 CLI
              与 <code>--version</code> 打印可见；契约端点与配置名册均不导出，故此处不渲染数值。
            </span>
          </div>
          <div className="sys-version-row">
            <span className="sys-version-label">存活自检</span>
            <span className="sys-version-check" aria-live="polite">
              <AsyncState
                status={healthStatus}
                error="自检失败：服务未在应答"
                onRetry={() => void healthQuery.refetch()}
                emptyLabel="自检响应异常：状态非 ok"
              >
                <Badge tone="success" dot>
                  进程在应答
                </Badge>
              </AsyncState>
            </span>
          </div>
        </div>
      </Card>
    </section>
  )
}
