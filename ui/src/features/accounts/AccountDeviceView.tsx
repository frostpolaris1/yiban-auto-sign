/**
 * **功能**
 * 一次设备读回的只读视图。
 *
 * **归属**
 * `features/accounts` 展示层：详情抽屉与建号向导第 2 页共用。
 *
 * **口径**
 * 设备码是掩码文本，不是输入框：全文只在服务端，响应只给掩码。
 * 来源说明固定一句：字段由平台登记，界面不可改。
 */

import { Badge } from '@/components/ui/Badge'
import type { AccountDevice } from '@/lib/api/account'

export function AccountDeviceView({ device }: { device: AccountDevice }) {
  return (
    <dl className="app-detail-list">
      <dt>设备识别码</dt>
      <dd className="data-cell-mono" id="account-create-device">
        {device.device_code_masked || '—'}
      </dd>
      <dt>来源</dt>
      <dd>来自易班平台登记，不可修改</dd>
      <dt>授权状态</dt>
      <dd>
        <Badge tone={device.authorized ? 'success' : 'warning'} dot>
          {device.authorized ? '已授权' : '未授权'}
        </Badge>
      </dd>
      <dt>机型</dt>
      <dd>{device.phone_model || '—'}</dd>
    </dl>
  )
}
