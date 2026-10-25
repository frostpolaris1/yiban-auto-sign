/**
 * resolve-ts.mjs — lets Node's test runner load UI modules.
 *
 * The UI sources import siblings without an extension (the bundler resolves
 * them). Node's ESM loader requires one, so this hook tries the TypeScript
 * candidates in order. A specifier that already has an extension, including a
 * non-JavaScript one such as `.json`, is passed through unchanged. It is used
 * only by `npm run test:unit`, never by the app build.
 */
import { registerHooks } from 'node:module'

const CANDIDATES = ['.ts', '.tsx', '/index.ts', '/index.tsx']

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (!specifier.startsWith('.')) return nextResolve(specifier, context)
    if (/\.[a-z0-9]+$/i.test(specifier)) return nextResolve(specifier, context)
    for (const suffix of CANDIDATES) {
      try {
        return nextResolve(`${specifier}${suffix}`, context)
      } catch {
        /* try the next candidate */
      }
    }
    return nextResolve(specifier, context)
  },
})
