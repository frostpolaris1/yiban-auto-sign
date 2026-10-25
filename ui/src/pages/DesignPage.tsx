import { Badge } from '@/components/ui/Badge'
import { PageHeader } from '@/components/ui/PageHeader'
import { ButtonsSection } from '@/features/design/ButtonsSection'
import { FeedbackSection } from '@/features/design/FeedbackSection'
import { FormsSection } from '@/features/design/FormsSection'
import { IndicatorsSection } from '@/features/design/IndicatorsSection'
import { NavigationSection } from '@/features/design/NavigationSection'
import { OverlaysSection } from '@/features/design/OverlaysSection'
import { TableSection } from '@/features/design/TableSection'
import { TokensSection } from '@/features/design/TokensSection'

/**
 * Component gallery.
 * Every visual primitive reuses an Adminator class; behavior comes from Radix.
 */
export function DesignPage() {
  return (
    <>
      <PageHeader
        eyebrow="系统 · 设计系统"
        title={
          <>
            Adminator <span className="accent">组件画廊</span>
          </>
        }
        subtitle="每个视觉原语都复用 Adminator 类。行为由 Radix 提供，外观由设计令牌提供。"
        actions={<Badge tone="success">token gate 已启用</Badge>}
      />

      <div className="grid">
        <ButtonsSection />
        <FormsSection />
        <FeedbackSection />
        <IndicatorsSection />
        <OverlaysSection />
        <TableSection />
        <NavigationSection />
        <TokensSection />
      </div>
    </>
  )
}
