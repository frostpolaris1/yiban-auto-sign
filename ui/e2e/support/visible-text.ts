import type { Locator } from '@playwright/test'

/**
 * 读取用户可见文本：穿透开放 shadow root，跳过不渲染的节点。
 *
 * 数字滚动组件把数字渲染进开放 shadow root，并在其中保留 0–9 全部字形。
 * 只有当前字形参与布局，其余是 `display:none`。宿主元素的 `textContent`
 * 读不到 shadow root，直接取它只剩单位（如「项」）；`innerText` 也看不到。
 * 本函数取合成树：遍历开放 shadow root，按计算样式判可见性，
 * 最后去掉空白与零宽空格。
 *
 * 断言的是用户看到的文本，不依赖具体组件实现。
 */
export async function visibleText(locator: Locator): Promise<string> {
  return locator.evaluate((root) => {
    const parts: string[] = []
    const walk = (node: Node): void => {
      if (node.nodeType === Node.TEXT_NODE) {
        parts.push(node.textContent ?? '')
        return
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return
      const element = node as HTMLElement
      const style = window.getComputedStyle(element)
      if (style.display === 'none' || style.visibility === 'hidden') return
      const children = element.shadowRoot ?? element
      children.childNodes.forEach(walk)
    }
    walk(root)
    return parts.join('').replace(/[\s\u200b]/g, '')
  })
}
