/**
 * resolve-ts.mjs — lets Node's test runner load UI modules.
 *
 * The UI sources import siblings without an extension (the bundler resolves
 * them). Node's ESM loader requires one, so this hook appends `.ts` for a
 * relative specifier that has no extension. It is used only by `npm run
 * test:unit`, never by the app build.
 */
import { registerHooks } from 'node:module'

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith('.') && !/\.[cm]?[jt]sx?$/.test(specifier)) {
      return nextResolve(`${specifier}.ts`, context)
    }
    return nextResolve(specifier, context)
  },
})
