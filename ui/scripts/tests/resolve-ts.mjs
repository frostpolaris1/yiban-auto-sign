/**
 * resolve-ts.mjs — lets Node's test runner load UI modules.
 *
 * The UI sources import siblings without an extension (the bundler resolves
 * them) and use the `@/` alias for `ui/src`. Node's ESM loader requires an
 * extension and treats `@/` as a package name, so this hook rewrites both forms
 * to a file URL and tries the TypeScript candidates in order. A specifier that
 * already has an extension, including a non-JavaScript one such as `.json`, is
 * passed through unchanged. It is used only by `npm run test:unit`, never by
 * the app build.
 */
import { registerHooks } from 'node:module'

const CANDIDATES = ['.ts', '.tsx', '/index.ts', '/index.tsx']
const SRC_ROOT = new URL('../../src/', import.meta.url)

/** 把无扩展名的本地引用解析成 TypeScript 候选。 */
function resolveLocal(reference, context, nextResolve) {
  for (const suffix of CANDIDATES) {
    try {
      return nextResolve(`${reference}${suffix}`, context)
    } catch {
      /* try the next candidate */
    }
  }
  return nextResolve(reference, context)
}

registerHooks({
  resolve(specifier, context, nextResolve) {
    // `@/x` 是 `ui/src/x` 的别名。先还原成文件 URL，再补扩展名。
    if (specifier.startsWith('@/')) {
      const target = new URL(specifier.slice(2), SRC_ROOT).href
      if (/\.[a-z0-9]+$/i.test(target)) return nextResolve(target, context)
      return resolveLocal(target, context, nextResolve)
    }
    if (!specifier.startsWith('.')) return nextResolve(specifier, context)
    if (/\.[a-z0-9]+$/i.test(specifier)) return nextResolve(specifier, context)
    return resolveLocal(specifier, context, nextResolve)
  },
})
