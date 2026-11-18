/**
 * **功能**
 * `/system#policy` 运行策略分区容器：按两栏/全宽组合 4 张独立保存策略卡。
 *
 * **归属**
 * `features/system/policy` 分区入口。
 */

import { PolicyQuotaCard } from './PolicyQuotaCard'
import { PolicyScheduleCard } from './PolicyScheduleCard'
import {
  PolicyAutoApproveCard,
  PolicyRegistrationCard,
  type PolicyCardAccessProps,
} from './PolicySwitchesCards'

export function PolicySection({ canWrite, readOnlyReason }: PolicyCardAccessProps) {
  return (
    <section
      id="policy"
      className="sys-section"
      tabIndex={-1}
      aria-labelledby="policy-section-heading"
    >
      <div className="sys-section-head">
        <h2 id="policy-section-heading" className="sys-section-title">
          运行策略
        </h2>
        <p className="sys-section-desc">
          所有策略卡均需点击各卡独立保存按钮后生效；普通管理员只读查看，不可变更配置。
        </p>
      </div>
      <div className="sys-policy-grid">
        <PolicyRegistrationCard canWrite={canWrite} readOnlyReason={readOnlyReason} />
        <PolicyAutoApproveCard canWrite={canWrite} readOnlyReason={readOnlyReason} />
        <PolicyQuotaCard canWrite={canWrite} readOnlyReason={readOnlyReason} />
        <PolicyScheduleCard canWrite={canWrite} readOnlyReason={readOnlyReason} />
      </div>
    </section>
  )
}
