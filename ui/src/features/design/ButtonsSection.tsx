/**
 * **功能**
 * 按钮画廊：11 种变体、3 种尺寸、按钮组。
 * 类名与 DOM 照 Adminator `src/buttons.html`。
 *
 * **归属**
 * `features/design` 域组件：只组合通用件。
 *
 * **口径**
 * 只演示外观，不带业务动作。
 */

import { Icon } from '@/components/ui/Icon'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'

export function ButtonsSection() {
  return (
    <Card className="col-12" eyebrow="Actions" title="按钮">
      <div className="app-demo-stack">
        <div className="demo-row">
          <Button variant="primary">主操作</Button>
          <Button variant="secondary">次级</Button>
          <Button variant="ghost">取消</Button>
          <Button variant="success">通过</Button>
          <Button variant="warning">谨慎</Button>
          <Button variant="danger">删除</Button>
          <Button variant="info">信息</Button>
        </div>
        <div className="demo-row">
          <Button variant="soft-primary">浅底主色</Button>
          <Button variant="soft-danger">浅底危险</Button>
          <Button variant="outline-primary">描边主色</Button>
          <Button variant="outline-danger">描边危险</Button>
          <Button variant="ghost" disabled>
            禁用
          </Button>
        </div>
        <div className="demo-row">
          <Button variant="primary" size="sm">
            小号
          </Button>
          <Button variant="primary" size="lg">
            大号
          </Button>
          <Button variant="primary" size="icon" aria-label="新增">
            <Icon name="plus" />
          </Button>
          <div className="btn-group">
            <button type="button" className="btn btn--ghost btn--sm is-active">
              日
            </button>
            <button type="button" className="btn btn--ghost btn--sm">
              周
            </button>
            <button type="button" className="btn btn--ghost btn--sm">
              月
            </button>
          </div>
        </div>
      </div>
    </Card>
  )
}
