/**
 * **功能**
 * 认证页左侧介绍面板：天空画布背景、品牌、一句话说明、能力清单、许可证页脚。
 *
 * **归属**
 * `features/auth` 域组件。
 *
 * **口径**
 * 文案纯展示，不发请求。
 * 只写产品确实具备的能力，不承诺会话期限与登录后的具体行为。
 * 不再使用「证言卡」：引言、头像与署名会伪装成用户背书。
 * 背景为 `SkyGradient` 装饰层（aria-hidden），文字对比由浅天空保证深字可读。
 */

import { BrandMark } from '@/components/ui/BrandMark'
import { Icon } from '@/components/ui/Icon'
import { useTheme } from '@/lib/theme'
import { SkyGradient } from './SkyGradient'
import { SKY_PALETTES, skyPaletteFor } from './sky'

/** 能力清单。每条都能在 README 与后端能力里找到对应。 */
const POINTS = [
  { icon: 'calendar', text: '每天按时签到，窗口内错峰' },
  { icon: 'rotateCcw', text: '失败自动重试，不连击' },
  { icon: 'activity', text: '30 天签到记录都在' },
  { icon: 'shield', text: '账号凭据加密存储' },
] as const

/** 注册引导三步：与真实流程一致（注册 → 新建账号识别学校 → 自动签到），口吻亲和。 */
const STEPS = [
  { title: '创建账号', text: '一个邮箱、一个密码，马上就好' },
  { title: '绑定易班账号', text: '选一下学校，凭据我们加密收好' },
  { title: '自动签到', text: '每天按时帮你打卡，失败会自己再试' },
] as const

type AuthAsideProps = {
  /** 注册签换开场文案与三步引导；登录/找回页保持能力清单。 */
  mode?: 'login' | 'register'
}

export function AuthAside({ mode = 'login' }: AuthAsideProps) {
  const { theme } = useTheme()
  // 亮色按北京时段取色；暗色直接入夜蓝档——天空随主题入夜，而非整块藏起。
  // 夜蓝 onDark=true，左栏文字翻白（配色规则 06 从底色反推）。
  const palette = theme === 'dark' ? SKY_PALETTES.night : skyPaletteFor(new Date())
  const registering = mode === 'register'
  return (
    <aside className={`auth-aside auth-aside--${palette.name}`} data-on-dark={palette.onDark ? 'true' : 'false'}>
      <SkyGradient className="auth-sky" palette={palette} />
      {/* 可读层：天空云层明暗不定，文字区垫一层随明暗取向的柔渐变保 AA（功能非装饰）。 */}
      <div className="auth-scrim" aria-hidden="true" />
      <div className="auth-brand">
        <div className="logo">
          <BrandMark />
        </div>
        <div className="name">易班自动签到</div>
      </div>

      {/* 两签文案常驻堆叠，切换同步淡入淡出（与右栏表单同口径），天空不动。 */}
      <div className="auth-aside-body">
        <div className="auth-aside-copy" data-active={registering ? 'false' : 'true'} aria-hidden={registering}>
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
        <div className="auth-aside-copy" data-active={registering ? 'true' : 'false'} aria-hidden={!registering}>
          <h1>
            从这里，
            <br />
            开始自动签到。
          </h1>
          <p className="auth-aside-lead">花一分钟创建账号，绑定易班后，签到就交给它。</p>
          <ol className="auth-steps">
            {STEPS.map((step, index) => (
              <li key={step.title}>
                <span className="auth-step-no" aria-hidden="true">
                  {index + 1}
                </span>
                <span className="auth-step-text">
                  <b>{step.title}</b>
                  <span>{step.text}</span>
                </span>
              </li>
            ))}
          </ol>
        </div>
      </div>

      <div className="auth-aside-footer">
        <span>AGPL-3.0 开源自部署</span>
        <span>凭据加密存储</span>
      </div>
    </aside>
  )
}
