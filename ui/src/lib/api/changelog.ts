/**
 * **功能**
 * 更新日志读端点：服务端把 CHANGELOG.md 作为 Markdown 返回。
 *
 * **归属**
 * `lib/api` 工具层：只拼路径，不含渲染逻辑。
 *
 * **口径**
 * 公开端点，无会话、无 CSRF；文档未配置时返回固定兜底句。
 * 渲染只走服务端白名单；前端不解析原始 HTML。
 */

import type { components } from './schema'
import { apiRequest } from './client'

type Schemas = components['schemas']

export type Changelog = Schemas['Changelog']

export const CHANGELOG_PATH = '/api/changelog'

export function getChangelog(): Promise<Changelog> {
  return apiRequest<Changelog>(CHANGELOG_PATH)
}
