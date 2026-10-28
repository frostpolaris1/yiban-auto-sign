/**
 * **功能**
 * 数字滚动显示：终值即用户可见文本，排版与普通文本一致。
 *
 * **归属**
 * `components` 通用件：只读传入的数字，不发请求、不读域状态。
 *
 * **口径**
 * 语言固定 `zh-CN`，千分位随该语言。颜色继承外层容器。
 * 动画库的遮罩内边距会撑高行盒并下压字形；`app.css` 的 `.app-number` 抵消它。
 * 数据到位后字形都在开放 shadow root 内：`textContent` 含 0–9 全部字形，
 * 读数须取可见字形（见 e2e 助手 `visible-text.ts`）。
 */

import NumberFlow from '@number-flow/react'

/** **单值数字**：只换渲染方式，不改文案与布局。 */
export function AnimatedNumber({ value }: { value: number }) {
  return <NumberFlow className="app-number" value={value} locales="zh-CN" />
}
