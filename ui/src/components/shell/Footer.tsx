/**
 * **功能**
 * 页脚：产品名、版本、设计语言。
 *
 * **归属**
 * `components/shell`：壳的固定部分。
 *
 * **口径**
 * 版本号手抄自 `package.json`。改版本必须同步这里。
 */

export function Footer() {
  return (
    <footer className="d-footer">
      <div>易班自动签到 · P1 前端骨架</div>
      <div className="d-footer-meta">
        <span>ui v0.1.0</span>
        <span>Adminator 4</span>
      </div>
    </footer>
  )
}
