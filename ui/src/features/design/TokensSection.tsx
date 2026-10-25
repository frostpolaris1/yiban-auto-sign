/**
 * **功能**
 * 色板：按组展示设计令牌名与色块。
 *
 * **归属**
 * `features/design` 域组件：只读令牌，不定义取值。
 *
 * **口径**
 * 取值只在 `tokens.css`。本文件只写 `var(--token)`。
 * 除 `tokens.css` 与逐字节复制件外，本仓不允许颜色字面量。
 */

import { Card } from '@/components/ui/Card'

const SURFACE_TOKENS = ['--bg-body', '--bg-card', '--bg-sidebar', '--bg-hover', '--bg-muted'] as const
const TEXT_TOKENS = ['--t-base', '--t-muted', '--t-light', '--t-inverse'] as const
const BORDER_TOKENS = ['--border', '--border-soft'] as const
const BRAND_TOKENS = ['--primary', '--primary-light', '--primary-dark', '--primary-soft'] as const
const SEMANTIC_TOKENS = [
  '--success',
  '--warning',
  '--danger',
  '--info',
  '--purple',
  '--pink',
  '--teal',
  '--orange',
] as const

const GROUPS = [
  { label: 'Surfaces', tokens: SURFACE_TOKENS },
  { label: 'Text', tokens: TEXT_TOKENS },
  { label: 'Border', tokens: BORDER_TOKENS },
  { label: 'Brand', tokens: BRAND_TOKENS },
  { label: 'Semantic', tokens: SEMANTIC_TOKENS },
]

export function TokensSection() {
  return (
    <Card className="col-6" eyebrow="Tokens" title="色板">
      <div className="app-demo-stack">
        {GROUPS.map((group) => (
          <div key={group.label}>
            <div className="section-h" style={{ marginBottom: 8, fontSize: 12 }}>
              {group.label}
            </div>
            <div className="app-swatch-grid">
              {group.tokens.map((token) => (
                <div className="app-swatch" key={token}>
                  <span className="app-swatch-chip" style={{ background: `var(${token})` }} />
                  <span className="mono">{token}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
        <hr className="app-rule" />
        <p className="app-muted">
          色板只读取 `var(--token)`。除 tokens.css 与 verbatim 的 adminator.css 外，本仓不允许颜色字面量。
        </p>
      </div>
    </Card>
  )
}
