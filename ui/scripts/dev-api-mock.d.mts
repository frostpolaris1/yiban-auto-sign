import type { Plugin } from 'vite'

/** 仅 devmock 模式生效的 /api 假人中间件；其余模式下为空插件。 */
export function devApiMock(): Plugin
