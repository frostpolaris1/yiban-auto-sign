/**
 * **功能**
 * 认证页左侧介绍面板：品牌、一句话说明、能力清单、许可证页脚。
 *
 * **归属**
 * `features/auth` 域组件。
 *
 * **口径**
 * 纯文案，不发请求。
 * 只写产品确实具备的能力，不承诺会话期限与登录后的具体行为。
 * 不再使用「证言卡」：引言、头像与署名会伪装成用户背书。
 */

import { BrandMark } from '@/components/ui/BrandMark'
import { Icon } from '@/components/ui/Icon'

/** 能力清单。每条都能在 README 与后端能力里找到对应。 */
const POINTS = [
  { icon: 'calendar', text: '每天按时签到，窗口内错峰' },
  { icon: 'rotateCcw', text: '失败自动重试，不连击' },
  { icon: 'activity', text: '签到记录与统计都在' },
  { icon: 'shield', text: '账号凭据加密存储' },
] as const

export function AuthAside() {
  return (
    <aside className="auth-aside">
      <div className="auth-brand">
        <div className="logo">
          <BrandMark />
        </div>
        <div className="name">易班自动签到</div>
      </div>

      <div className="auth-aside-body">
        <span className="auth-aside-eyebrow">SELF-HOSTED · AGPL-3.0</span>
        <h1>
          把每天的签到，
          <br />
          交给它。
        </h1>
        <p className="auth-aside-lead">按你的时间偏好自动执行，失败会自己重试。</p>
        <ul className="auth-points">
          {POINTS.map((point) => (
            <li key={point.text}>
              <Icon name={point.icon} size={18} />
              <span>{point.text}</span>
            </li>
          ))}
        </ul>
      </div>

      <div className="auth-aside-footer">
        <span>2026</span>
        <span>SELF-HOSTED</span>
      </div>
    </aside>
  )
}
