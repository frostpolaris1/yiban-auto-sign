/**
 * **功能**
 * 覆盖层画廊：弹窗与下拉菜单。
 * 弹窗外壳照 Adminator `src/ui.html`。
 *
 * **归属**
 * `features/design` 域组件：只组合通用件。
 *
 * **口径**
 * 确认动作只关闭弹窗。不发请求，不改数据。
 */

import { useState } from 'react'
import { Icon } from '@/components/ui/Icon'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { Menu, MenuDivider, MenuItem } from '@/components/ui/Menu'
import { Modal } from '@/components/ui/Modal'

export function OverlaysSection() {
  const [modalOpen, setModalOpen] = useState(false)

  return (
    <Card className="col-6" eyebrow="Overlays" title="弹窗与下拉">
      <div className="app-demo-stack">
        <div className="demo-row">
          <Button variant="primary" onClick={() => setModalOpen(true)}>
            打开弹窗
          </Button>

          <Menu
            trigger={
              <button type="button" className="btn btn--ghost" aria-label="账号操作">
                账号操作
                <Icon name="chevronDown" />
              </button>
            }
          >
            <div className="dd-head">
              <Icon name="userCog" />
              账号操作
            </div>
            <MenuItem onSelect={() => undefined}>
              <Icon name="settings" />
              账号设置
            </MenuItem>
            <MenuItem onSelect={() => undefined}>
              <Icon name="eye" />
              查看诊断
            </MenuItem>
            <MenuDivider />
            <MenuItem danger onSelect={() => undefined}>
              <Icon name="trash" />
              删除账号
            </MenuItem>
          </Menu>
        </div>

        <p className="app-muted">
          Radix 提供焦点陷阱、Esc 关闭、键盘导航。Adminator 提供面板外观。
        </p>
      </div>

      <Modal
        open={modalOpen}
        onOpenChange={setModalOpen}
        title="删除账号？"
        description="删除后无法恢复。本批不发送请求。"
        footer={
          <>
            <Button variant="ghost" onClick={() => setModalOpen(false)}>
              取消
            </Button>
            <Button variant="danger" onClick={() => setModalOpen(false)}>
              删除账号
            </Button>
          </>
        }
      >
        <p className="app-muted">
          示例对象：<strong>ACC-1042</strong> · Sara Kim。确认动作只关闭弹窗。
        </p>
      </Modal>
    </Card>
  )
}
