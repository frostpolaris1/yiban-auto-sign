/**
 * **功能**
 * 登录左栏天空画布的时段配色：按上海墙钟分拂晓、白昼、黄昏、夜蓝四档。
 *
 * **归属**
 * `features/auth` 域逻辑：纯函数 + 常量表，无 React、无网络。
 *
 * **口径**
 * 时段固定 Asia/Shanghai，不读机器时区——和签到口径同源。
 * 边界：05–08 拂晓，08–16 白昼，16–19 黄昏，其余夜蓝。
 * 色值只以 0–1 浮点数组存在（喂 WebGL uniform），源码里不出现字面色——token 门口径不破。
 * 夜蓝档 onDark=true：左栏文字翻白（`--t-inverse`），其余三档保持 `--t-base` 深字。
 * 页面存续期间不重算：登录页停留跨时段无人在乎，刷新即换。
 */

export type SkyTones = {
  main: number[]
  low: number[]
  mid: number[]
  high: number[]
  /** 云受光色：昼档近白、夜档暗navy（保白字对比）。 */
  cloud: number[]
}

export type SkyPaletteName = 'dawn' | 'day' | 'dusk' | 'night'

export type SkyPalette = {
  name: SkyPaletteName
  tones: SkyTones
  onDark: boolean
}

const SHANGHAI = 'Asia/Shanghai'

const hourFormatter = new Intl.DateTimeFormat('en-US', {
  timeZone: SHANGHAI,
  hour: 'numeric',
  hourCycle: 'h23',
})

/**
 * 以天气预报产品（Apple Weather 那类真实大气天空）为基准，不自行调香。
 * 天空是垂直大气渐变：天顶最深、地平线最亮（shader 按屏幕高度插值四色）。
 * 色调角色：low=天顶、main=上层、mid=下层、high=地平线。
 * 色值取自设计师复刻 Apple Weather 的通行实色（iOS15+ 天空背景近似）：
 *  day   白昼=2E63B8→4A90D9→7FB8E8→A8D5F2（深蓝 zenith→浅蓝地平线）。
 *  dusk  晚霞=1F2A5C→6C4A8A→D9707A→F5A25D（深navy→紫→玫瑰橙→金地平线）。
 *  dawn  拂晓=35507A→7A5C8E→C96F7B→FBC98B（蓝→紫→玫瑰→橙金地平线）。
 *  night 夜蓝=070B1F→101A3C→1B2A4A→25355E（近黑navy→navy→亮navy地平线，非纯黑）。
 * 弃此前 pastel 平涂（用户判「不过关」）：真实天空有 zenith→horizon 明暗纵深。
 */
export const SKY_PALETTES: Record<SkyPaletteName, SkyPalette> = {
  dawn: {
    name: 'dawn',
    tones: {
      low: [0.208, 0.314, 0.478], // 35507A 天顶蓝
      main: [0.478, 0.361, 0.557], // 7A5C8E 上层紫
      mid: [0.788, 0.435, 0.482], // C96F7B 下层玫瑰
      high: [0.984, 0.788, 0.545], // FBC98B 地平线橙金
      cloud: [1, 1, 1], // 昼云近白
    },
    onDark: false,
  },
  day: {
    name: 'day',
    tones: {
      low: [0.18, 0.388, 0.722], // 2E63B8 天顶深蓝
      main: [0.29, 0.565, 0.851], // 4A90D9 上层
      mid: [0.498, 0.722, 0.91], // 7FB8E8 下层
      high: [0.659, 0.835, 0.949], // A8D5F2 地平线浅蓝
      cloud: [1, 1, 1], // 昼云近白
    },
    onDark: false,
  },
  dusk: {
    name: 'dusk',
    tones: {
      low: [0.122, 0.165, 0.361], // 1F2A5C 天顶深navy
      main: [0.424, 0.29, 0.541], // 6C4A8A 上层紫
      mid: [0.851, 0.439, 0.478], // D9707A 下层玫瑰橙
      high: [0.961, 0.635, 0.365], // F5A25D 地平线金
      cloud: [1, 0.98, 0.96], // 晚霞云略带暖白
    },
    onDark: false,
  },
  night: {
    name: 'night',
    tones: {
      low: [0.027, 0.043, 0.122], // 070B1F 天顶近黑
      main: [0.063, 0.102, 0.235], // 101A3C 上层navy
      mid: [0.106, 0.165, 0.29], // 1B2A4A 下层
      high: [0.145, 0.208, 0.369], // 25355E 地平线亮navy
      cloud: [0.16, 0.22, 0.36], // 夜云暗navy，保白字对比
    },
    onDark: true,
  },
}

export function skyPaletteFor(date: Date): SkyPalette {
  const hour = Number(hourFormatter.format(date))
  if (hour >= 5 && hour < 8) return SKY_PALETTES.dawn
  if (hour >= 8 && hour < 16) return SKY_PALETTES.day
  if (hour >= 16 && hour < 19) return SKY_PALETTES.dusk
  return SKY_PALETTES.night
}
