/**
 * **功能**
 * 图标登记表与 `<Icon>` 渲染器。
 *
 * **归属**
 * `components` 通用件：全仓唯一图标来源。
 *
 * **口径**
 * 图标只从 `lucide-react` 取；TSX 里不手抄几何，也不混用第二套图标库。
 * CSS 的 data URI 是另一来源；每一处都逐条登记，未登记即红。
 * 品牌标记是设计资产，单独登记在 `BrandMark.tsx`。
 * 图标名即 `ICON_COMPONENTS` 的键。新增图标只在此表加一项，并具名导入。
 * 描边固定 1.8：与 Adminator 口径一致。lucide 默认为 2，偏粗。
 * 尺寸默认 24×24；调用方可用 `size` 覆盖，样式也可给宽高。
 * 默认 24 修一处旧缺陷：无宽高的容器里，旧 svg 算成 0×0，字形不可见。
 */

import {
  ArrowRight,
  Bell,
  Calendar,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  Columns3,
  Download,
  ExternalLink,
  Eye,
  Filter,
  Info,
  LayoutDashboard,
  LogOut,
  Mail,
  MapPin,
  Menu,
  Minus,
  Moon,
  Palette,
  Plus,
  School,
  Search,
  Settings,
  Shield,
  SquarePen,
  Sun,
  Trash2,
  TrendingDown,
  TrendingUp,
  UserCog,
  Users,
  X,
  type LucideIcon,
} from 'lucide-react'
import type { CSSProperties, ReactNode } from 'react'

/**
 * 图标注册表。壳与所有原语只从这里取图。
 * 键名同时是 `IconName` 的取值域。
 */
const ICON_COMPONENTS = {
  dashboard: LayoutDashboard,
  accounts: Users,
  school: School,
  design: Palette,
  calendar: Calendar,
  shield: Shield,
  settings: Settings,
  bell: Bell,
  mail: Mail,
  search: Search,
  menu: Menu,
  sun: Sun,
  moon: Moon,
  logout: LogOut,
  userCog: UserCog,
  chevronRight: ChevronRight,
  chevronLeft: ChevronLeft,
  chevronDown: ChevronDown,
  plus: Plus,
  trash: Trash2,
  check: Check,
  alertCircle: CircleAlert,
  info: Info,
  close: X,
  eye: Eye,
  edit: SquarePen,
  filter: Filter,
  mapPin: MapPin,
  columns: Columns3,
  arrowRight: ArrowRight,
  trendUp: TrendingUp,
  trendDown: TrendingDown,
  trendFlat: Minus,
  download: Download,
  external: ExternalLink,
} as const satisfies Record<string, LucideIcon>

export type IconName = keyof typeof ICON_COMPONENTS

type IconProps = {
  name: IconName
  size?: number
  className?: string
  style?: CSSProperties
}

export function Icon({ name, size, className, style }: IconProps): ReactNode {
  const Glyph = ICON_COMPONENTS[name]
  return (
    <Glyph
      size={size}
      strokeWidth={1.8}
      className={className}
      style={style}
      aria-hidden="true"
      focusable="false"
    />
  )
}
