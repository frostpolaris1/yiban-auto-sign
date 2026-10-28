/**
 * **功能**
 * 会话解析期间的整页加载态。
 *
 * **归属**
 * `components` 通用件：壳与登录页共用的等待画面。
 *
 * **口径**
 * 守卫在首次 `me` 返回前显示它，不先渲染登录页。
 * `data-session-loading` 是判据选择器；删掉它等于删掉不变式 8 的锚点。
 */

export function SessionLoading() {
  return (
    <div
      className="app-session-loading"
      data-session-loading
      role="status"
      aria-label="加载会话"
    >
      <span className="spinner lg" />
    </div>
  )
}
