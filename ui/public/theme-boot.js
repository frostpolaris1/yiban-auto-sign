/**
 * 主题引导：在任何 CSS 加载前写入 data-theme。
 *
 * 目的：深色用户不会先看到一帧浅色。
 * 36 个 Adminator 令牌只在 :root[data-theme="light"]
 * 或 :root[data-theme="dark"] 下解析。
 * 本脚本只写一个键：dash26-theme。不存任何凭据。
 *
 * 必须是外置脚本且同步加载（无 defer/async）：
 * 服务端 CSP 的 script-src 只有 'self'，内联脚本会被浏览器拦掉。
 */
(function () {
  var KEY = 'dash26-theme'
  var theme = null
  try {
    var saved = localStorage.getItem(KEY)
    if (saved === 'light' || saved === 'dark') theme = saved
  } catch (error) {
    /* localStorage 可能被禁用。读不到就落到媒体查询。 */
  }
  if (!theme) {
    try {
      theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
    } catch (error) {
      theme = 'light'
    }
  }
  document.documentElement.setAttribute('data-theme', theme)
})()
